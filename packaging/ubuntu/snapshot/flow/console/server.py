import asyncio
import contextlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from account_pacing import account_status, resume_after_review, set_operator_pause
from adapter import AccountWindowBusy, PreSubmissionTransient, PreSubmissionFailure
from api_keys import APIKeys
from account_insights import Insights
from console_store import MAX_GENERATION_RETRIES, RETRY_KINDS, SESSION_IMPORT_INTERRUPTED, Store
from dispatch_limit import DispatchLimit
from flow_proxy_guard import exit_bindings, mapped_ports, release_removed_exits, reserve_confirmed_exit
from flow_references import REFERENCE_FIELDS, ReferenceStore, task_reference_ids
from web_auth import WebAuth
from login_sessions import ImportRestoredOnCancel, Vault, normalize_bundle
from material_library import Library
from material_tags import SEARCH_LABELS
from preflight import pilot_dispatch_reasons
from remote_login import RemoteLogins

HERE = Path(__file__).resolve().parent
PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
KINDS = {"sync", "login", "media", "doctor", "quote", "image", "video"}
TERMINAL_INPUT_STOPS = frozenset((
    'pasted_text_mismatch', 'prompt_input_timeout',
    'prompt_prefix_timeout', 'prompt_prefix_failed',
    'prompt_paste_timeout', 'prompt_paste_failed',
    'prompt_verify_timeout', 'prompt_verify_failed',
))
RETRYABLE_INPUT_STOPS = frozenset(('pre_submit_failed:pointer_not_over_input', 'pre_submit_failed:desktop_keyboard_authorization_timeout',
                                  'window_focus_lost', 'composer_unavailable', 'window_activation_unconfirmed',
                                  'prompt_focus_timeout', 'prompt_focus_failed'))
WAITING_FOR_PROXY = '等待已确认空闲的专属代理。分配后，原同步任务会自动继续核对。'
BRIDGE_LEGACY_ID = 'kbnibljeinmjmmpholicjkaiehmilbbk'
BRIDGE_ID = 'aaipdbnbbgeadlodajhcobjmomnemclj'
BRIDGE_IDS = (BRIDGE_ID, BRIDGE_LEGACY_ID)
BRIDGE_ORIGINS = frozenset('chrome-extension://' + ident for ident in BRIDGE_IDS)
BALANCE_SWEEP_SECONDS = 300
BALANCE_READ_WORKERS = 2
BALANCE_RETRY_DELAYS = (10, 25)


def safe_error(exc):
    text = str(exc)[:1800]
    text = re.sub(r"https?://[^\s<>\"\)]+", "[服务地址已省略]", text)
    text = re.sub(
        r"(?i)(cookie|csrf|authorization|token|sapisid|sid)\s*[:=]\s*\S+", r"\1=[已隐藏]", text
    )
    return text


class Console:
    def __init__(self, root, adapter, test=False):
        self.root, self.adapter, self.test = Path(root), adapter, test
        self.store = Store(self.root / "console-data")
        self.keys = APIKeys(self.store)
        self.insights = Insights(self.store, self.root / "data/studiowright.db")
        self.balance_wake = asyncio.Event()
        self.balance_requested = set()
        self.balance_full_requested = False
        self.balance_refreshing = False
        self.balance_busy = set()
        dispatch_state = (self.root / 'console-data') if test else (Path.home() / '.local/state/creative-unified-workbench')
        self.library = Library(self.root / "material-library", dispatch_state / "real-pool.sqlite3")
        self.vault = Vault(self.root / "console-data")
        self.remote = RemoteLogins(self.root, fixture=test)
        self.remote_gate = asyncio.Lock()
        self.store.execute(
            "CREATE TABLE IF NOT EXISTS bridge_devices(hash TEXT PRIMARY KEY,created REAL)"
        )
        self.busy = set()
        self.generation_busy = {}
        self.generation_projects = set()
        self.running = set()
        self.dispatch_limit = DispatchLimit(dispatch_state / 'dispatch')
        self.references = ReferenceStore(dispatch_state)
        self.worker = None
        self.keyboard_warmup = None
        for profile in adapter.discover():
            if not self.store.account(profile):
                self.store.add_account(profile, profile)

    def release_profile(self, task):
        profile = task['profile']
        if task['kind'] in ('image', 'video') and self.generation_busy.get(profile, 0):
            self.generation_projects.discard((profile, task['project']))
            remaining = self.generation_busy[profile] - 1
            if remaining:
                self.generation_busy[profile] = remaining
                return
            del self.generation_busy[profile]
        self.busy.discard(profile)

    def dispatch_preflight(self, profiles):
        if self.test:
            return {profile: [] for profile in profiles}
        holds = self.store.page_incidents()
        return {profile: (['flow_page_warning'] if profile in holds
                          else pilot_dispatch_reasons(self.root, profile)) for profile in profiles}

    def session_import_waits_for_proxy(self, profile, email):
        if not isinstance(email, str) or profile != 'account-' + self.store.digest(email)[:12]:
            return False
        try:
            return profile not in mapped_ports(self.root / 'bin/exit-map.txt')
        except (OSError, ValueError):
            return True

    def recycle_removed_exits(self):
        removed = {row['profile'] for row in self.store.rows("""SELECT r.profile FROM flow_removed_accounts r
            WHERE NOT EXISTS (SELECT 1 FROM tasks t WHERE t.profile=r.profile AND t.status IN ('queued','running'))""")}
        return release_removed_exits(self.root, removed - self.busy - self.balance_busy)

    def ready_session_imports(self, busy, request_keys):
        ready = set()
        for row in self.store.rows("""SELECT t.id,t.profile,t.params,t.request_key,a.email FROM tasks t
            JOIN accounts a ON a.profile=t.profile
            WHERE t.kind='session_import' AND t.status='queued' AND NOT EXISTS
              (SELECT 1 FROM flow_removed_accounts r WHERE r.profile=t.profile) ORDER BY t.created,t.id"""):
            if row['profile'] in busy or (request_keys is not None and row['request_key'] not in request_keys):
                continue
            params = json.loads(row['params'])
            email = params.get('expected_email')
            if self.session_import_waits_for_proxy(row['profile'], email):
                if (row['email'] or '').lower() != email:
                    continue
                ident = params.get('bundle_id')
                try:
                    self.vault.get(ident)
                except (TypeError, ValueError):
                    updated = self.store.execute("""UPDATE tasks SET status='failed',finished=?,error=?
                        WHERE id=? AND status='queued'""", (time.time(), '登录包已过期或不可读取，请重新同步。', row['id']))
                    if updated:
                        with contextlib.suppress(OSError, TypeError, ValueError):
                            self.vault.remove(ident)
                    continue
                if reserve_confirmed_exit(self.root, row['profile']) is None:
                    continue
            ready.add(row['id'])
        return ready

    async def execute(self, task, *, on_video_submitted=None):
        # This second check is before the adapter is called, so a racing restriction
        # can safely leave the existing task in the queue without resubmission.
        ordinal = task.get('attempt_ordinal', 0)
        try:
            if task['kind'] in RETRY_KINDS and not self.desktop_keyboard_ready():
                self.store.release_unsubmitted(task['id'], task.get('attempt_ordinal', 0))
                self.release_profile(task)
                return
            database = self.root / 'data/studiowright.db'
            if task['kind'] in {'image', 'video'} and not (self.test and not database.exists()):
                status = account_status([{'profile': task['profile']}], database)[0]
                preflight = await asyncio.to_thread(self.dispatch_preflight, [task['profile']])
                detail = self.store.retry_detail(task['id'])
                switched = (task.get('retry_reason') or (detail or {}).get('reason')) == 'switch_account'
                if (preflight[task['profile']] or status['generation_state'] != 'not_cooling_down'
                        or status['risk_paused'] is not False
                        or (switched and (task['profile'] not in self.switch_ready_profiles()
                                          or not self.retry_project_matches(task['profile'], task['project'])))):
                    self.store.release_unsubmitted(task['id'], task.get('attempt_ordinal', 0))
                    self.release_profile(task)
                    return
        except Exception:
            # Do not turn a failure to release the claim into a provider failure.
            self.release_profile(task)
            raise
        before = []
        keep_bundle = False
        try:
            before = [dict(r)["id"] for r in self.adapter.source_jobs(task["profile"])]
            account = self.store.account(task["profile"])
            if on_video_submitted is None:
                result = await self.adapter.run(task, account)
            else:
                result = await self.adapter.run(task, account, on_video_submitted=on_video_submitted)
            if task["kind"] in {"sync", "session_import"}:
                self.store.update_account(task["profile"], result["email"], "verified")
                if not self.test:
                    from task_coordinator import Coordinator
                    Coordinator().release_verified(task['profile'])
                for project in result["projects"]:
                    self.store.project(task["profile"], project["id"], project["title"])
            if task['kind'] in ('image','video'):
                from task_coordinator import Coordinator
                result['self_recovery'] = [e for e in Coordinator().events(task['id']) if e['created'] >= (task.get('started') or 0)]
            self.store.finish(task["id"], result, attempt_ordinal=ordinal)
        except ImportRestoredOnCancel:
            if task['kind'] == 'session_import':
                with contextlib.suppress(OSError, ValueError, sqlite3.Error):
                    self.vault.get(task['params']['bundle_id'])
                    keep_bundle = self.store.requeue_restored_import(task['id'])
            raise
        except asyncio.CancelledError:
            # Leave running state for recovery. Never imply generation was cancelled.
            raise
        except Exception as exc:
            provenance = []
            retry_evidence = None
            with contextlib.suppress(Exception):
                exact = getattr(self.adapter, 'source_jobs_for_task', None)
                linked = self.source_receipts(task) if exact and task['kind'] in ('image', 'video') else []
                rows = linked if task['kind'] in ('image', 'video') else self.adapter.source_jobs(task["profile"])
                for r in rows:
                    r = dict(r)
                    if linked or r["id"] not in before:
                        raw = json.loads(r["result"]) if r.get("result") else {}
                        provenance.append(
                            {
                                "job_id": r["id"],
                                "project_id": r["project_id"],
                                "status": r["status"],
                                "stage": raw.get("stage"),
                                "media_ids": raw.get("media_ids", []),
                                "files": raw.get("files", []),
                            }
                        )
                # A source record is admissible only when exactly correlated with
                # this console task and still proves no accepted/generated media.
                if len(linked) == 1:
                    source = dict(linked[0])
                    raw = json.loads(source.get('result') or '{}')
                    evidence, stage = raw.get('retry_evidence'), raw.get('stage')
                    # Only explicitly allowed local input guards before the send
                    # checkpoint count; require the matching persisted source record.
                    stop = exc.args[0] if len(exc.args) == 1 and isinstance(exc.args[0], str) else None
                    source_error = str(source.get('error') or '')
                    if (stage == 'preparing' and type(exc).__name__ == 'InputNotSubmitted'
                            and type(exc).__module__ == 'foreground_input'
                            and (source_error == f'InputNotSubmitted: {stop}'
                                 or source_error.startswith(f'InputNotSubmitted: {stop} ('))):
                        if stop in TERMINAL_INPUT_STOPS:
                            evidence = 'confirmed_before_submit'
                        elif stop in RETRYABLE_INPUT_STOPS:
                            evidence = 'transient_before_submit'
                    if (source['status'] == 'failed' and source['project_id'] == task['project']
                            and source['profile'] == task['profile'] and source['kind'] == task['kind'] and not raw.get('media_ids') and not raw.get('files')
                            and ((evidence in ('transient_before_submit', 'confirmed_before_submit') and stage == 'preparing')
                                 or (evidence == 'provider_rejected_unusual' and stage == 'submitting'
                                     and task['params'].get('count') == 1))):
                        retry_evidence = evidence
                elif (not linked and self.store.retry_detail(task['id']) and (
                        isinstance(exc, PreSubmissionTransient) or (
                            type(exc).__module__ == 'task_coordinator'
                            and type(exc).__name__ == 'CoordinationBlocked'
                            and exc.args in (('existing_flow_attachments',), ('existing_flow_draft',))))):
                    retry_evidence = 'transient_before_submit'
            if task["kind"] in {"sync", "session_import"} and not isinstance(exc, AccountWindowBusy):
                account = self.store.account(task["profile"])
                from login_sessions import ImportRestored
                if account["state"] != "verified" or not isinstance(exc, ImportRestored):
                    self.store.update_account(
                        task["profile"], account["email"], "attention", safe_error(exc)
                    )
            result = {"source_jobs": provenance}
            if isinstance(exc, PreSubmissionFailure) and not provenance:
                result["submission"] = {"status":"not_submitted","stage":"preparing"}
                retry_evidence = "confirmed_before_submit"
            if task['kind'] in ('image','video'):
                from task_coordinator import Coordinator
                result['self_recovery'] = [e for e in Coordinator().events(task['id']) if e['created'] >= (task.get('started') or 0)]
            self.store.finish(task["id"], result, safe_error(exc) or type(exc).__name__, retry_evidence, attempt_ordinal=ordinal)
        finally:
            if task["kind"] == "session_import" and not keep_bundle:
                with contextlib.suppress(Exception):
                    self.vault.remove(task["params"]["bundle_id"])
            self.release_profile(task)

    async def execute_with_slot(self, task, slot, *, scoped=False):
        background = None

        def on_video_submitted():
            nonlocal background
            if background is None:
                background = self.dispatch_limit.track_submitted_video(slot, task['profile'])

        handoff = on_video_submitted if task['kind'] == 'video' and not scoped else None
        try:
            await self.execute(task, on_video_submitted=handoff)
        finally:
            slot.close()
            if background is not None:
                background.close()
            # A retryable completion selects directly from the current healthy
            # pool after releasing this attempt's account and dispatch slot.
            # The periodic scheduler only backs up tasks already waiting.
            if task['kind'] in RETRY_KINDS:
                info = self.store.retry_detail(task['id'])
                if (info and info['state'] == 'pending_switch'
                        and not self.dispatch_limit.marker.exists()
                        and not self.dispatch_limit.marker.is_symlink()
                        and not (self.dispatch_limit.root/'flow-stress-once.json').exists()
                        and self.desktop_keyboard_ready()):
                    with contextlib.suppress(Exception):
                        self.schedule_pending_switches()

    def source_receipts(self, task):
        get = self.adapter.source_jobs_for_task
        ordinal = task.get('attempt_ordinal', 0)
        return list(get(task['profile'], task['id'], ordinal) if ordinal else get(task['profile'], task['id']))

    def reconcile_rejected_windows(self, coordinator=None):
        """A provider rejection is not an unknown result, if correlated to one failed attempt."""
        if self.test:
            return
        from task_coordinator import Coordinator
        coordinator = coordinator or Coordinator()
        for window in coordinator.pending_rejections():
            if window['profile'] in self.busy:
                continue
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                task = self.store.latest_execution(window['task_id'], profile=window['profile'])
                if (not task or task['status'] != 'failed' or not task['finished']
                        or task['profile'] != window['profile'] or task['kind'] not in ('image', 'video')
                        or task['params'].get('count') != 1):
                    continue
                linked = self.source_receipts(task)
                if len(linked) != 1:
                    continue
                source = dict(linked[0])
                result = json.loads(source['result'] or '{}')
                if (isinstance(result, dict) and source['status'] == 'failed' and source['profile'] == task['profile']
                        and source['project_id'] == task['project'] and source['kind'] == task['kind']
                        and result.get('stage') == 'submitting'
                        and result.get('retry_evidence') == 'provider_rejected_unusual'
                        and not result.get('media_ids') and not result.get('files')):
                    coordinator.release_confirmed_rejection(task['profile'], task['id'])

    def desktop_keyboard_ready(self):
        if self.test:
            return True
        try:
            from input_backend import open_keyboard
            return open_keyboard(None).ready()
        except ImportError:
            return False
        except Exception:
            # A missing /dev/uinput or a failed helper must only make the queue
            # wait; it must never escape the scheduler loop and stop dispatch.
            return False

    async def prepare_desktop_keyboard(self):
        # A single shared permission request, outside account/dispatch slots.
        try:
            from flow_browser_state import gui_env
            from input_backend import open_keyboard
            env = await asyncio.to_thread(gui_env)
            await open_keyboard(None, gui_environment=env).ensure_ready(timeout=45)
        except (ImportError, OSError, RuntimeError, subprocess.SubprocessError, ValueError):
            pass  # The queue remains intact; continue observing the existing session.

    async def scheduler(self):
        next_switch_check = 0.0
        next_window_check = 0.0
        while True:
            for ident, s in list(self.remote.sessions.items()):
                if s["expires"] < time.time():
                    await asyncio.to_thread(self.remote.close, ident)
                    self.busy.discard(s["profile"])
            self.running = {t for t in self.running if not t.done()}
            if self.dispatch_limit.marker.exists() or self.dispatch_limit.marker.is_symlink():
                await asyncio.sleep(0.2)
                continue
            stress_marker = self.dispatch_limit.root/'flow-stress-once.json'
            if not stress_marker.exists() and time.monotonic() >= next_window_check:
                next_window_check = time.monotonic() + 5.0
                with contextlib.suppress(OSError, ValueError, sqlite3.Error):
                    self.reconcile_rejected_windows()
                if not self.test:
                    with contextlib.suppress(OSError, ValueError, sqlite3.Error):
                        self.recycle_removed_exits()
            keyboard_ready = self.desktop_keyboard_ready()
            if keyboard_ready:
                if self.keyboard_warmup and self.keyboard_warmup.done():
                    self.keyboard_warmup = None
            else:
                demand = self.store.rows("""SELECT 1 FROM flow_task_execution_rows WHERE status='queued' AND kind IN ('image','video')
                    UNION ALL SELECT 1 FROM flow_retry_roots WHERE state='pending_switch' LIMIT 1""")
                if demand and self.keyboard_warmup is None:
                    self.keyboard_warmup = asyncio.create_task(self.prepare_desktop_keyboard())
                elif not demand and self.keyboard_warmup and self.keyboard_warmup.done():
                    self.keyboard_warmup = None
            if keyboard_ready and not stress_marker.exists() and time.monotonic() >= next_switch_check:
                # Missing projects, stale balance, or uncertain account state all wait.
                with contextlib.suppress(Exception):
                    self.schedule_pending_switches()
                next_switch_check = time.monotonic() + 1.0
            from flow_stress_scope import read_scope, acquire_scope_slot, validate_task
            scope = read_scope(self.dispatch_limit.root)
            while True:
                slot = (acquire_scope_slot(self.dispatch_limit, scope) if scope
                        else self.dispatch_limit.try_acquire())
                if slot is None:
                    break
                try:
                    database = self.root / 'data/studiowright.db'
                    accounts = self.store.accounts()
                    states = account_status(accounts, database)
                    allowed = {a['profile'] for a in states if a['generation_state'] == 'not_cooling_down'
                               and a['risk_paused'] is False and not a.get('flow_removed')}
                    if not self.test:
                        from task_coordinator import Coordinator
                        held = {w['profile'] for w in Coordinator().status()['windows']
                                if w['state'] in ('submitting','submitted','generated','result_unknown','needs_verification')}
                        allowed -= held - self.busy
                    if self.test and not database.exists():
                        allowed = {a['profile'] for a in accounts}  # FakeAdapter fixture only.
                    if not keyboard_ready:
                        allowed.clear()
                    if allowed and not self.test:
                        queued = {r['profile'] for r in self.store.rows(
                            "SELECT DISTINCT profile FROM flow_task_execution_rows WHERE status='queued' AND kind IN ('image','video')")}
                        checks = await asyncio.to_thread(self.dispatch_preflight, allowed & queued)
                        allowed = {profile for profile, reasons in checks.items() if not reasons}
                    busy = self.busy | self.balance_busy
                    request_keys = set(scope['requests']) if scope else None
                    import_ready = self.ready_session_imports(busy, request_keys)
                    task = self.store.claim(busy, allowed, self.switch_ready_profiles(),
                                            set(), self.generation_projects,
                                            request_keys=request_keys, session_import_allowed=import_ready)
                    if not task:
                        slot.close()
                        break
                    if scope and not validate_task(task, scope, self.store):
                        self.store.release_unsubmitted(task['id'], task.get('attempt_ordinal', 0))
                        slot.close()
                        break
                    self.busy.add(task["profile"])
                    if task['kind'] in ('image', 'video'):
                        self.generation_busy[task['profile']] = self.generation_busy.get(task['profile'], 0) + 1
                        self.generation_projects.add((task['profile'], task['project']))
                    self.running.add(asyncio.create_task(self.execute_with_slot(task, slot, scoped=scope is not None)))
                except BaseException:
                    slot.close()
                    raise
            await asyncio.sleep(0.2)

    def retry_project_matches(self, profile, project):
        self.store.sync_observed_project_names()
        account = self.store.account(profile)
        email = (account or {}).get('email') or ''
        return ('@' in email and account['state'] == 'verified' and
                [p['id'] for p in self.store.projects(profile)
                 if p['title'] == 'FLOW+' + email.split('@', 1)[0]] == [project])

    def schedule_pending_switches(self):
        now = time.time()
        self.store.expire_unstarted_switches(now)
        roots = self.store.pending_retry_roots()
        if not roots:
            return
        self.store.sync_observed_project_names()
        from task_coordinator import Coordinator
        states = account_status(self.store.accounts(), self.root / 'data/studiowright.db', now)
        allowed = {a['profile'] for a in states if a['state'] == 'verified'
                   and a['generation_state'] == 'not_cooling_down' and a['risk_paused'] is False
                   and not a.get('flow_removed') and a['profile'] not in self.busy and a['profile'] not in self.balance_busy}
        if not self.test:
            allowed -= {w['profile'] for w in Coordinator().status()['windows']
                        if w['state'] not in ('idle',)}
            allowed = {profile for profile, reasons in self.dispatch_preflight(allowed).items() if not reasons}
        balances = {r['profile']:r['balance'] for r in self.store.rows(
            "SELECT profile,balance FROM account_balances WHERE status='ready' AND observed BETWEEN ? AND ?",
            (now-900,now))}
        projects = []
        for profile in allowed & balances.keys():
            account = self.store.account(profile)
            email = account.get('email') or ''
            if '@' not in email:
                continue
            owned = [p for p in self.store.projects(profile) if p['title'] == 'FLOW+' + email.split('@', 1)[0]]
            if len(owned) == 1:
                projects.append((profile, owned[0]['id'], email, balances[profile]))
        projects.sort(key=lambda entry: (-entry[3], entry[0]))
        for row in roots:
            root = row['root_id']
            original = self.store.task(root)
            info = self.store.retry_detail(root)
            if not original or not info or original['kind'] not in RETRY_KINDS:
                self.store.stop_retry_wait(root, 'no_account')
                continue
            params = original['params']
            attempts = info['attempts']
            used = {a['profile'] for a in attempts}
            ordinal = attempts[-1]['ordinal'] + 1
            minimum = 1
            eligible = [p for p in projects if p[0] not in used and p[3] >= minimum]
            if not eligible:
                # Cooling, busy windows, stale credit or missing projects are
                # temporary: keep the root queued until an account is usable.
                pool = {account['profile'] for account in self.store.accounts()}
                if pool and pool <= used:
                    self.store.stop_retry_wait(root, 'no_account')
                continue
            source_account = self.store.account(original['profile'])
            for profile, project, email, _ in eligible:
                retry_params = params
                if params.keys() & REFERENCE_FIELDS:
                    try:
                        retry_params = self.references.clone_retry(original['kind'], params,
                            (original['profile'],original['project'],(source_account or {}).get('email')),
                            (profile,project,email), f'{root}:{ordinal}:{profile}:{project}')
                    except ValueError:
                        self.store.stop_retry_wait(root, 'references_unavailable')
                        break
                if self.store.enqueue_switch(root, profile, project, retry_params, ordinal):
                    projects = [p for p in projects if p[0] != profile]
                    break

    def switch_ready_profiles(self, now=None):
        now = time.time() if now is None else now
        try:
            return {r['profile'] for r in self.store.rows("SELECT b.profile FROM account_balances b JOIN accounts a ON a.profile=b.profile WHERE a.state='verified' AND b.status='ready' AND b.balance>0 AND b.observed BETWEEN ? AND ?", (now - 900, now))}
        except sqlite3.Error:
            return set()  # no trusted balance: block switch, keep the ordinary queue running

    async def archive_worker(self):
        while True:
            # Restart recovery reads persisted task results, never repeats generation.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.library.reconcile, self.store, self.root / "output")
                await asyncio.to_thread(self.library.drain)
            await asyncio.sleep(5)

    async def index_worker(self):
        while True:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.library.drain_index)
            await asyncio.sleep(5)

    async def read_balance_batch(self, profiles):
        from balance_reader import read_balance, BalancePanelUnavailable, BalanceReadDeferred
        from playwright.async_api import async_playwright
        self.balance_refreshing = True
        try:
            async with async_playwright() as pw:
                accounts = account_status(self.store.accounts(), self.root / 'data/studiowright.db')
                incidents = self.store.page_incidents()
                parallel_reads = asyncio.Semaphore(BALANCE_READ_WORKERS)

                async def read_account(account):
                    async with parallel_reads:
                        return await read_available_account(account)

                async def read_available_account(account):
                    profile = account['profile']
                    if (self.dispatch_limit.paused() or account.get('flow_removed')
                            or profile in incidents or account.get('risk_paused') is True
                            or account.get('generation_state') == 'cooling_down'):
                        return None
                    if profile in self.busy or profile in self.balance_busy or self.remote.for_profile(profile):
                        return profile
                    slot = self.dispatch_limit.try_acquire()
                    if slot is None:
                        return profile
                    self.balance_busy.add(profile)
                    try:
                        balance = await asyncio.wait_for(
                            read_balance(account, pw, include_details=True, incident_root=self.root), timeout=20)
                        self.insights.record(profile, balance['balance'], next_credit_date=balance.get('next_credit_date'))
                    except BalanceReadDeferred:
                        return profile
                    except BalancePanelUnavailable as exc:
                        self.insights.record(profile, error=str(exc), transient=True)
                        return profile
                    except Exception as exc:
                        message = str(exc) if isinstance(exc, ValueError) else '余额读取暂未完成，保留上次记录。'
                        self.insights.record(profile, error=message)
                    finally:
                        self.balance_busy.discard(profile)
                        slot.close()
                    return None

                async with asyncio.TaskGroup() as group:
                    reads = [group.create_task(read_account(account)) for account in accounts
                             if profiles is None or account['profile'] in profiles]
                return {task.result() for task in reads} - {None}
        finally:
            self.balance_refreshing = False

    async def balance_worker(self):
        if self.test:
            return
        loop = asyncio.get_running_loop()
        next_sweep = loop.time()
        pending = {}
        while True:
            self.balance_wake.clear()
            delay = BALANCE_SWEEP_SECONDS
            if not self.dispatch_limit.paused():
                now = loop.time()
                full = self.balance_full_requested or now >= next_sweep
                requested = self.balance_requested
                self.balance_requested = set()
                self.balance_full_requested = False
                due = {profile for profile, (_, at) in pending.items() if at <= now}
                selected = requested | due
                if full:
                    next_sweep = now + BALANCE_SWEEP_SECONDS
                    pending.clear()
                if full or selected:
                    try:
                        retry = await self.read_balance_batch(None if full else selected)
                    except Exception:
                        retry = set()
                    for profile in selected | retry:
                        count, _ = pending.pop(profile, (0, 0))
                        if profile in retry and count < len(BALANCE_RETRY_DELAYS):
                            pending[profile] = (count + 1, loop.time() + BALANCE_RETRY_DELAYS[count])
                delay = max(0, min([next_sweep, *(at for _, at in pending.values())]) - loop.time())
            try:
                await asyncio.wait_for(self.balance_wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def validate(self, body, with_references=False):
        profile, project, kind = body.get("profile"), body.get("project"), body.get("kind")
        if (
            not isinstance(profile, str)
            or not PROFILE.fullmatch(profile)
            or not self.store.account(profile)
        ):
            raise ValueError("请选择已登记账号。")
        if kind not in KINDS:
            raise ValueError("不支持的操作。")
        if kind not in {"sync", "login"}:
            try:
                if str(uuid.UUID(project)) != project:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError("请选择完整、有效的已有项目 ID。") from None
            if not self.store.has_project(profile, project):
                raise ValueError("该项目未登记在此账号下，请先读取项目或登记已有项目。")
            account = self.store.account(profile)
            if account["state"] != "verified":
                raise ValueError("请先检查此账号的在线登录和项目，再操作项目。")
        else:
            project = None
        params = {}
        if kind in {"image", "video", "quote"}:
            p = body.get("params", {})
            if not isinstance(p, dict):
                raise ValueError("任务参数格式不正确。")
            if isinstance(p.get("model"), str):
                aliases = {"omni-flash-1.1": "omni-flash", "omni-flash-1.2": "omni-flash-2"}
                if p["model"] in aliases:
                    p = dict(p, model=aliases[p["model"]])
            prompt = p.get("prompt", "").strip() if isinstance(p.get("prompt", ""), str) else ""
            if not prompt or len(prompt) > 10000:
                raise ValueError("请填写提示词，最多 10000 个字。")
            if type(p.get("count", 1)) is not int or p.get("count", 1) not in (1, 2, 3, 4):
                raise ValueError("生成数量须为 1～4。")
            params = {
                "prompt": prompt,
                "model": p.get("model"),
                "aspect": p.get("aspect", "16:9"),
                "count": p.get("count", 1),
                "resolution": p.get("resolution", "720p"),
                "duration": p.get("duration", 8),
            }
            if 'style_id' in p:
                style_id = p['style_id']
                if not isinstance(style_id, str) or not style_id.strip() or len(style_id) > 256 or any(0xD800 <= ord(c) <= 0xDFFF for c in style_id):
                    raise ValueError('画风 ID 须为 1～256 个字符的非空字符串。')
                if kind not in ('image', 'video'):
                    raise ValueError('只有生图和生视频任务可以设置画风 ID。')
                params['style_id'] = style_id
            if with_references:
                if kind not in ('image', 'video'):
                    raise ValueError('仅图片和视频任务可以使用参考素材。')
                if p.keys() - params.keys() - {'max_credits'} - REFERENCE_FIELDS:
                    raise ValueError('参考图任务包含不支持的参数。')
                refs = task_reference_ids(kind, p)
                self.references.check(refs, profile, project, account.get('email'), allow_video=p['mode'] == 'ingredients')
                params.update({key: p[key] for key in p.keys() & REFERENCE_FIELDS})
            elif p.keys() & (REFERENCE_FIELDS | {'refs', 'start', 'end'}):
                raise ValueError('含参考图的任务必须使用对应的提交入口。')
            if params["aspect"] not in (
                ["16:9", "9:16"] if kind != "image" else ["16:9", "4:3", "1:1", "3:4", "9:16"]
            ):
                raise ValueError("当前生成类型不支持这个画面比例。")
            if (
                type(params["duration"]) is not int
                or params["duration"] not in (tuple(range(3, 16)) if params.get("model") == "omni-flash-2" else (4, 6, 8, 10))
                or params["resolution"] not in ("360p", "720p")
            ):
                raise ValueError("视频时长或清晰度不正确。")
            self.adapter.validate("video" if kind == "quote" else kind, params)
            if kind in {"image", "video"} and body.get("confirm_generation") is not True:
                raise ValueError("提交前请确认账号、项目和可能消耗的积分。")
            if kind == "video":
                cap = p.get("max_credits")
                if type(cap) is not int or not 0 <= cap <= 10000:
                    raise ValueError("请填写本次允许的视频积分上限（0～10000）。")
                params["max_credits"] = cap
        key = body.get("request_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key):
            raise ValueError("缺少提交编号，请重新提交。")
        return profile, project, kind, params, key

    def task_view(self, row, *, load_retry=True):
        # Keep the public ID and submission metadata; resolve the current attempt separately.
        out = dict(row)
        retry = self.store.retry_detail(row['id']) if load_retry else None
        if not retry or retry['ordinal'] != 0 or not retry['attempts']:
            return out, retry
        latest = retry['attempts'][-1]
        state = retry['state']
        ordinal = latest.get('attempt_ordinal', 0)
        current = (row if latest['id'] == row['id'] and not ordinal else
                   self.store.execution(latest['id'], ordinal))
        if current is None:
            out.update(status='interrupted',result=None,error='重试记录待核对。')
            return out, retry
        out['submission'] = (current.get('result') or {}).get('submission')
        out.update(effective_profile=current['profile'], effective_project=current['project'],
                   result=current['result'],error=current['error'],finished=current['finished'])
        if state == 'pending_switch':
            out.update(status='queued',result=None,error=None,finished=None)
        elif state == 'active':
            out.update(status='queued' if latest['status']=='queued' else 'running',
                       result=None,error=None,finished=None)
        elif state == 'succeeded' and latest['status'] == 'succeeded':
            out.update(status='succeeded',error=None)
        elif state in ('interrupted', 'needs_review') or state == 'succeeded':
            not_submitted = (current.get('result') or {}).get('submission',{}).get('status') == 'not_submitted'
            no_result = re.search(r'no image reply within \d+ s, and no new image is in the project', str(current.get('error') or ''))
            out.update(status='failed' if no_result or not_submitted else 'interrupted',result=None,
                       error=current.get('error') or '原结果需核对，已停止重试。')
        elif state == 'cancelled':
            out.update(status='cancelled',result=None,error=None)
        else:
            out.update(status='failed',result=None,error=current['error'] or '重试未能完成。')
        return out, retry

    def public_task(self, row, *, load_retry=True):
        row, retry = self.task_view(row, load_retry=load_retry)
        result = row.get("result") or {}
        files = []
        for path in result.get("files", []):
            try:
                resolved = Path(path).resolve()
                if resolved.is_relative_to((self.root / "output").resolve()) and resolved.is_file():
                    files.append(
                        {"name": resolved.name, "url": f"/api/tasks/{row['id']}/files/{len(files)}"}
                    )
            except (OSError, TypeError):
                pass
        row["downloads"] = files
        result = dict(result)
        result["files"] = [Path(f).name for f in result.get("files", [])]
        row["result"] = result if row.get("result") is not None else None
        row.pop("request_key", None)
        row['retry'] = retry
        if row["kind"] == "session_import":
            row["params"] = {"expected_email": row["params"].get("expected_email")}
            if row['status'] == 'interrupted':
                row['error'] = SESSION_IMPORT_INTERRUPTED
        return row


def create_app(root, adapter, test=False):
    c = Console(root, adapter, test)
    cookie_name = "sw_console_fixture" if test else "sw_console"
    web_auth = WebAuth(c.store)

    @asynccontextmanager
    async def lifespan(app):
        c.store.recover()
        for row in c.store.rows(
            "SELECT params FROM tasks WHERE kind='session_import' AND status NOT IN ('queued','running')"
        ):
            with contextlib.suppress(Exception):
                c.vault.remove(json.loads(row["params"])["bundle_id"])
        c.worker = asyncio.create_task(c.scheduler())
        archive_worker = asyncio.create_task(c.archive_worker())
        index_worker = asyncio.create_task(c.index_worker())
        balance_worker = asyncio.create_task(c.balance_worker())
        from flow_window_maintenance import worker as maintain_windows
        maintenance_worker = asyncio.create_task(maintain_windows(c)) if not test else None
        yield
        c.worker.cancel()
        if c.keyboard_warmup and not c.keyboard_warmup.done():
            c.keyboard_warmup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await c.keyboard_warmup
        archive_worker.cancel()
        index_worker.cancel()
        balance_worker.cancel()
        if maintenance_worker:
            maintenance_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):await maintenance_worker
        with contextlib.suppress(asyncio.CancelledError):
            await balance_worker
        with contextlib.suppress(asyncio.CancelledError):
            await index_worker
        with contextlib.suppress(asyncio.CancelledError):
            await archive_worker
        with contextlib.suppress(asyncio.CancelledError):
            await c.worker
        for task in c.running:
            task.cancel()
        await asyncio.gather(*c.running, return_exceptions=True)
        await c.remote.shutdown()

    async def index(request):
        return FileResponse(HERE / "static/index.html")

    def local_browser(request):
        return not test and request.client is not None and request.client.host in ("127.0.0.1", "::1")

    async def remote_page(request):
        if not local_browser(request) and not c.store.authenticated(request.cookies.get(cookie_name)):
            return JSONResponse({"error": "请先连接管理后台。"}, status_code=401)
        return FileResponse(HERE / "static/remote.html")

    async def health(request):
        return JSONResponse({"ok": True, "application": "studiowright-console", "test": test,
                             "bridge_id": BRIDGE_ID, "bridge_check": True, "source_version": os.getenv('SW_SOURCE_VERSION'),
                             "source_commit": os.getenv('SW_SOURCE_COMMIT')}, headers={"Cache-Control": "no-store"})

    async def payload(request, *, allow_empty=False):
        limit = 524288 if request.url.path.endswith("/session") else 32768
        if int(request.headers.get("content-length", "0")) > limit:
            raise ValueError("提交内容过大。")
        data = await request.body()
        if len(data) > limit:
            raise ValueError("提交内容过大。")
        value = {} if allow_empty and not data else json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("提交内容格式不正确。")
        return value

    async def connect(request):
        try:
            data = await payload(request)
            ticket = data.get("ticket", "")
            token = (
                c.store.exchange(ticket) if isinstance(ticket, str) and len(ticket) < 200 else None
            )
            if not token:
                return JSONResponse(
                    {"error": "连接码无效或已过期，请重新打开本机后台入口。"}, status_code=401
                )
            response = JSONResponse({"ok": True})
            response.set_cookie(
                cookie_name, token, httponly=True, samesite="strict", max_age=7 * 86400
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        except (ValueError, TypeError):
            return JSONResponse({"error": "连接请求不正确。"}, status_code=400)

    async def auth_status(request):
        return JSONResponse({'configured': web_auth.configured()}, headers={'Cache-Control': 'no-store'})

    async def password_login(request):
        try:
            body = await payload(request)
            remember = body.get('remember', False)
            token, age = await asyncio.to_thread(web_auth.login, body.get('username'), body.get('password'), remember)
            response = JSONResponse({'ok': True})
            response.set_cookie(cookie_name, token, httponly=True, samesite='strict',
                                secure=request.url.scheme == 'https', max_age=age if remember else None)
            return response
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=401)

    async def password_settings(request):
        try:
            body = await payload(request)
            await asyncio.to_thread(web_auth.set_credentials, body.get('username'), body.get('password'), body.get('current_password'))
            token, age = web_auth.new_session(True)
            response = JSONResponse({'ok': True, 'message': '后台账号密码已保存，其他浏览器的旧登录已退出。'})
            response.set_cookie(cookie_name, token, httponly=True, samesite='strict',
                                secure=request.url.scheme == 'https', max_age=age)
            return response
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)

    async def logout(request):
        token = request.cookies.get(cookie_name, "")
        c.store.execute("DELETE FROM sessions WHERE hash=?", (c.store.digest(token),))
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie_name)
        return response

    def state_snapshot(balance_refreshing, remote_logins):
        rows = c.store.tasks()
        retry_ids = {row['task_id'] for row in c.store.rows('SELECT task_id FROM flow_retry_attempts')}
        tasks = [c.public_task(row, load_retry=row['id'] in retry_ids) for row in rows]
        now = time.time()
        accounts = account_status(c.store.accounts(), c.root / "data/studiowright.db", now)
        active_accounts = [a for a in accounts if not a.get('flow_removed')]
        preflight = c.dispatch_preflight([a['profile'] for a in active_accounts])
        incidents = c.store.page_incidents()
        bindings = exit_bindings(c.root)
        for account in accounts:
            profile = account['profile']
            account['dispatch_preflight_reasons'] = preflight.get(profile, [])
            account['flow_page_incident'] = incidents.get(profile)
            account['exit_info'] = bindings.get(profile)
        insights = c.insights.report(active_accounts, now, balance_refreshing)
        return JSONResponse(
            {
                "test": test,
                "dispatch": c.dispatch_limit.public_status(),
                "coordination": dict(__import__('task_coordinator').Coordinator().status(), video_retry_max=3,
                                     desktop_keyboard_ready=c.desktop_keyboard_ready()) if not test else
                                {"window_policy":"one_per_account","video_retry_max":3,"desktop_keyboard_ready":True},
                "accounts": accounts,
                "account_insights": insights,
                "projects": c.store.projects(),
                "retry_projects": c.store.retry_projects(),
                "tasks": tasks,
                "models": adapter.models,
                "source_version": os.getenv('SW_SOURCE_VERSION'),
                "source_commit": os.getenv('SW_SOURCE_COMMIT'),
                "console_api_version": "2026-10-31.account-insights.1",
                "remote_login_ready": c.remote.supported(),
                "remote_logins": remote_logins,
                "time": now,
            }
        ).body

    state_in_progress = None

    async def state(request):
        nonlocal state_in_progress
        if state_in_progress is None or state_in_progress.done():
            remote_logins = [c.remote.public(s) for s in c.remote.sessions.values()]
            state_in_progress = asyncio.create_task(asyncio.to_thread(state_snapshot, c.balance_refreshing, remote_logins))
        return Response(await asyncio.shield(state_in_progress), media_type='application/json')

    async def accounts(request):
        try:
            data = await payload(request)
            profile, label = data.get("profile", ""), data.get("label", "")
            if not isinstance(profile, str) or not PROFILE.fullmatch(profile):
                raise ValueError("账号名称使用 1～64 个英文字母、数字、下划线或短横线。")
            if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
                raise ValueError("请填写账号备注，最多 80 个字。")
            if len(c.store.accounts()) >= 50 and not c.store.account(profile):
                raise ValueError("当前最多登记 50 个账号。")
            c.store.add_account(profile, label.strip())
            return JSONResponse({"ok": True})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def remove_flow_account(request):
        if request.state.api_key:
            return JSONResponse({'error': '请从工作台管理 Flow 账号。'}, status_code=403)
        try:
            data = await payload(request)
            profile = data.get('profile')
            if (set(data) != {'profile','confirmed'} or data['confirmed'] is not True
                    or not isinstance(profile, str) or not PROFILE.fullmatch(profile)):
                raise ValueError('请确认要从 Flow 号池移除的账号。')
            if profile in c.busy or profile in c.balance_busy:
                return JSONResponse({'error': '该账号仍在检查、登录或执行任务，请结束后再移除。'}, status_code=409)
            if not c.store.remove_flow_account(profile):
                return JSONResponse({'error': '该账号仍有排队、执行中或等待重试的生成任务。请处理原任务后再移除。'}, status_code=409)
            try:
                released = c.recycle_removed_exits()
                if profile in released:
                    network_status = 'released'
                    network_message = '网络位已释放。'
                elif not (c.root / 'bin/exit-map.txt').exists() or profile not in mapped_ports(c.root / 'bin/exit-map.txt'):
                    network_status = 'unbound'
                    network_message = ''
                else:
                    network_status = 'pending'
                    network_message = '网络位待释放，旧浏览器退出后会自动回收。'
            except (OSError, ValueError, sqlite3.Error):
                network_status = 'pending'
                network_message = '网络位待释放，后台会继续检查。'
            return JSONResponse({'removed': True, 'network_status': network_status,
                                 'message': '已从 Flow 号池移除。' + network_message + '项目和任务历史已保留。'})
        except (ValueError, TypeError) as exc:
            return JSONResponse({'error': safe_error(exc)}, status_code=400)

    async def set_flow_operator_pause(request):
        from filelock import FileLock, Timeout

        if request.state.api_key:
            return JSONResponse({'error': '请从工作台管理 Flow 账号。'}, status_code=403)
        try:
            data = await payload(request)
            profile, paused = data.get('profile'), data.get('paused')
            if (set(data) != {'profile', 'paused'} or type(paused) is not bool
                    or not isinstance(profile, str) or not PROFILE.fullmatch(profile)
                    or not any(a['profile'] == profile and a.get('email') and not a.get('flow_removed')
                               for a in c.store.accounts())):
                raise ValueError('请选择已登记的 Flow 账号。')
            database = c.root / 'data/studiowright.db'
            current = account_status([{'profile': profile}], database)[0]
            if paused and current['operator_paused'] is not True:
                if profile in c.busy or profile in c.balance_busy:
                    raise Timeout(profile)
                locks = c.root / 'data/locks'
                locks.mkdir(parents=True, mode=0o700, exist_ok=True)
                with FileLock(str(locks / (profile + '.lock'))).acquire(timeout=0):
                    set_operator_pause(database, profile, True)
            elif not paused:
                set_operator_pause(database, profile, False)
            restriction = account_status([{'profile': profile}], database)[0]
            if restriction['operator_paused'] is not paused:
                raise ValueError('Flow 暂停状态未确认，请刷新后核对。')
            message = 'Flow 账号已暂停。'
            if not paused:
                risk = restriction['risk_review_required'] is True or profile in c.store.page_incidents()
                message = ('已解除手动暂停，账号仍需人工核查。' if risk
                           else '已解除手动暂停，任务会等待冷却结束。' if restriction['generation_state'] == 'cooling_down'
                           else '已解除 Flow 手动暂停。')
            return JSONResponse({'ok': True, 'paused': paused, 'message': message})
        except Timeout:
            return JSONResponse({'error': '该账号正在执行、检查或远程登录，请在窗口空闲后暂停。'}, status_code=409)
        except (ValueError, TypeError) as exc:
            return JSONResponse({'error': safe_error(exc)}, status_code=400)
        except (OSError, sqlite3.Error):
            return JSONResponse({'error': 'Flow 暂停状态未确认，请刷新后核对。'}, status_code=503)

    async def resume_flow_risk(request):
        try:
            data = await payload(request)
            profile = data.get('profile')
            if set(data) != {'profile'} or not isinstance(profile, str) or not PROFILE.fullmatch(profile) or not c.store.account(profile):
                raise ValueError('请选择已登记账号。')
            resume_after_review(c.root / 'data/studiowright.db', profile)
            status = account_status([{'profile': profile}], c.root / 'data/studiowright.db')[0]
            return JSONResponse({'ok': True, 'cooling': status['generation_state'] == 'cooling_down',
                'message': ('已记录风控核查，账号仍处于手动暂停。' if status['operator_paused']
                            else 'Flow 已恢复；如仍在冷却，任务会继续等待冷却结束。')})
        except (ValueError, TypeError) as exc:
            return JSONResponse({'error': safe_error(exc)}, status_code=400)

    async def review_flow_page_incident(request):
        try:
            data = await payload(request)
            profile, ident = data.get('profile'), data.get('incident')
            if (set(data) != {'profile', 'incident'} or not isinstance(profile, str)
                    or not PROFILE.fullmatch(profile) or not isinstance(ident, str)
                    or not re.fullmatch(r'[0-9a-f]{32}', ident) or not c.store.account(profile)):
                raise ValueError('请选择需要人工核查的账号警告。')
            if not c.store.review_page_incident(profile, ident):
                return JSONResponse({'error': '这条警告已核查或已变化，请刷新。'}, status_code=409)
            return JSONResponse({'ok': True, 'message': '已记录核查，账号仍须通过其他派发检查。'})
        except (ValueError, TypeError) as exc:
            return JSONResponse({'error': safe_error(exc)}, status_code=400)

    async def refresh_balances(request):
        try:
            data = await payload(request, allow_empty=True)
            profiles = data.get('profiles')
            if 'profiles' in data and (
                    not isinstance(profiles, list) or not 1 <= len(profiles) <= 50
                    or any(not isinstance(p, str) or not PROFILE.fullmatch(p) or not c.store.account(p)
                           for p in profiles)):
                raise ValueError('请选择已登记账号。')
            if not c.test:
                if profiles:
                    c.balance_requested.update(profiles)
                    c.balance_wake.set()
                elif not c.balance_refreshing:
                    c.balance_full_requested = True
                    c.balance_wake.set()
        except (ValueError, TypeError) as exc:
            return JSONResponse({'error': safe_error(exc)}, status_code=400)
        return JSONResponse({"queued": not c.test, "refreshing": c.balance_refreshing,
            "message": "正在读取现有账号窗口的积分，不打开新窗口、不生成。" if not c.test else "模拟环境不会读取真实账号。"})

    async def projects(request):
        try:
            data = await payload(request)
            profile, ident = data.get("profile"), data.get("id")
            if (
                not isinstance(profile, str)
                or not PROFILE.fullmatch(profile)
                or not c.store.account(profile)
            ):
                raise ValueError("请选择账号。")
            if not isinstance(ident, str) or str(uuid.UUID(ident)) != ident:
                raise ValueError("请填写完整项目 ID。")
            title = data.get("title", "已登记项目")
            if not isinstance(title, str) or len(title) > 140:
                raise ValueError("项目备注最长 140 个字。")
            c.store.project(profile, ident, title)
            return JSONResponse({"ok": True})
        except (ValueError, TypeError, AttributeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def retry_project(request):
        try:
            data = await payload(request)
            if set(data) != {'profile', 'project'}:
                raise ValueError('重试项目设置格式不正确。')
            profile, project = data['profile'], data['project']
            if not isinstance(profile, str) or not PROFILE.fullmatch(profile) or not c.store.account(profile):
                raise ValueError('请选择已登记账号。')
            if project is not None and (not isinstance(project, str) or str(uuid.UUID(project)) != project):
                raise ValueError('请选择该账号已登记的项目。')
            c.store.set_retry_project(profile, project)
            return JSONResponse({'ok': True})
        except (ValueError, TypeError, AttributeError) as exc:
            return JSONResponse({'error': safe_error(exc)}, status_code=400)

    async def submit(request):
        try:
            body = await payload(request)
            if body.keys() & {'retry_of', 'retry_task_id', 'original_task_id', 'task_id', 'root_id', 'expected_ordinal'}:
                raise ValueError('重试请使用 /api/tasks/{原任务编号}/retry。')
            with_references = request.url.path == '/api/tasks/with-references'
            if with_references and body.get('kind') not in ('image', 'video'):
                raise ValueError('参考图任务只支持图生图和 Omni 多参考视频。')
            if request.state.api_key and body.get("kind") not in {"image", "video", "quote", "media", "doctor", "sync"}:
                return JSONResponse({"error": "API Key 不允许打开登录窗口或修改登录信息。"}, status_code=403)
            args = c.validate(body, with_references=True) if with_references else c.validate(body)
            automatic = body.get('auto_retry')
            if ('auto_retry' in body and type(automatic) is not bool) or (automatic and (args[2] not in RETRY_KINDS or args[3]['count'] != 1)):
                raise ValueError('任务重试设置不正确。')
            row = c.store.create_task(*args, auto_retry=automatic)
            canonical, _ = public_task_lookup(row['id'])
            if canonical is None:
                return JSONResponse({'error': '原任务关联待核对。'}, status_code=409)
            return JSONResponse(c.public_task(canonical), status_code=202)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def cancel(request):
        ident = request.path_params["ident"]
        row = c.store.task(ident)
        if not c.store.cancel(ident):
            return JSONResponse(
                {"error": "仅能取消排队任务；已开始任务不能直接中断或重试。"}, status_code=409
            )
        if row and row["kind"] == "session_import":
            with contextlib.suppress(Exception):
                c.vault.remove(row["params"]["bundle_id"])
        return JSONResponse({"ok": True})

    def public_task_lookup(ident):
        retry = c.store.retry_detail(ident)
        root = retry['root_id'] if retry else ident
        return c.store.task(root), root

    async def retry_task(request):
        ident = request.path_params['ident']
        try:
            body = await payload(request)
            ordinal = body.get('expected_ordinal')
            if (set(body) != {'expected_ordinal', 'confirm_generation'} or body['confirm_generation'] is not True
                    or type(ordinal) is not int or not 0 <= ordinal <= MAX_GENERATION_RETRIES):
                raise ValueError('请填写已核对的尝试序号，并明确确认生成。')
        except (ValueError, TypeError):
            return JSONResponse({'error': '请填写已核对的尝试序号，并明确确认生成。'}, status_code=400)
        row, root = public_task_lookup(ident)
        if row is None:
            return JSONResponse({'error': '任务不存在。'}, status_code=404)
        detail = c.store.retry_detail(root)
        target = next((a for a in (detail or {}).get('attempts', ()) if a['ordinal'] == ordinal), None)
        if not target:
            return JSONResponse({'error': '原任务没有可重试的尝试，请核对当前状态。'}, status_code=409)
        try:
            c.store.enqueue_same_account_image_retry(root, target['id'], target['project'], expected_ordinal=ordinal)
        except ValueError as exc:
            return JSONResponse({'error': safe_error(exc)}, status_code=409)
        public, _ = c.task_view(row)
        return JSONResponse({'task_id': root, 'status': public['status']}, status_code=202)

    async def task_status(request):
        ident = request.path_params['ident']
        row, root = public_task_lookup(ident)
        if not row:
            return JSONResponse({'error':'任务不存在。'}, status_code=404)
        public = c.public_task(row)
        data = {key:public[key] for key in ('id','kind','status','created','finished','downloads','retry')}
        # The API response omits prompts, reference IDs and internal error details.
        if 'style_id' in public['params']:
            data['style_id'] = public['params']['style_id']
        data['task_id'] = data['id']
        if ident != root:
            data['requested_task_id'] = ident
        if public.get('error'): data['failure_reason'] = public['error']
        data['submission'] = public.get('submission')
        data['downloads'] = [{'path':item['url'],'name':item['name']} for item in data['downloads']]
        if data['retry']:
            data['retry'] = dict(data['retry'], attempts=[
                {key:a[key] for key in ('ordinal','reason','status','started','finished')}
                for a in data['retry']['attempts']])
        return JSONResponse(data)

    async def download(request):
        row, _ = public_task_lookup(request.path_params["ident"])
        if row:
            row, _ = c.task_view(row)
            paths = []
            for f in (row.get("result") or {}).get("files", []):
                p = Path(f).resolve()
                if p.is_relative_to((c.root / "output").resolve()) and p.is_file():
                    paths.append(p)
            index = request.path_params["index"]
            if index < len(paths):
                return FileResponse(paths[index], filename=paths[index].name)
        return JSONResponse({"error": "此任务没有可下载文件。"}, status_code=404)

    async def materials(request):
        try:
            p = request.query_params
            result = await asyncio.to_thread(
                c.library.search,
                p.get("q", ""),
                p.get("kind", ""),
                p.get("tag", ""),
                p.get("account", ""),
                int(p.get("page", "1")),
                dimensions={d: p[d] for d in SEARCH_LABELS if d in p},
                model=p.get("model", ""), project_id=p.get("project_id", ""),
                account_id=p.get("account_id", ""), created_from=p.get("created_from", ""),
                created_to=p.get("created_to", ""), index_status=p.get("index_status", ""),
                storage_status=p.get("storage_status", ""), style_id=p.get("style_id", ""),
                audio_category=p.get("audio_category", ""),
                duration_min=p.get("duration_min"), duration_max=p.get("duration_max"),
                limit=int(p.get("limit", "24")), offset=int(p["offset"]) if "offset" in p else None,
            )
            return JSONResponse(result)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def material_detail(request):
        item = await asyncio.to_thread(c.library.get, request.path_params["ident"])
        return JSONResponse(item or {"error": "素材不存在。"}, status_code=200 if item else 404)

    async def library_music_ingest(request):
        # Register Flow Music clips into the material library. Downloads are
        # restricted to the pinned Flow Music storage host with DNS-resolution
        # and redirect checks, and every clip is deduplicated inside archive.
        try:
            body = await payload(request)
            clips = body.get("clips")
            if not isinstance(clips, list) or not clips or len(clips) > 10:
                raise ValueError("clips 格式不正确。")
            import socket as _socket
            import tempfile
            import urllib.request
            from ipaddress import ip_address, ip_network
            from urllib.parse import urlsplit
            storage_host = 'storage.googleapis.com'

            class _PinnedRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    parts = urlsplit(newurl)
                    if parts.scheme != 'https' or parts.hostname != storage_host:
                        raise ValueError('音频下载重定向离开了固定存储域，已阻止。')
                    return super().redirect_request(req, fp, code, msg, headers, newurl)

            def open_pinned_storage(url, timeout=120):
                parts = urlsplit(url)
                if (parts.scheme != 'https' or parts.hostname != storage_host
                        or parts.username or parts.password):
                    raise ValueError('音频地址不正确，未导入。')
                # 本机 sing-box 透明代理使用 fake-ip DNS：固定域解析到 198.18.0.0/15
                # 保留段，连接会被代理接管并转出真实目标——该段需要显式放行。
                fake_ip = ip_network('198.18.0.0/15')
                for _, _, _, _, sockaddr in _socket.getaddrinfo(parts.hostname, 443, proto=_socket.IPPROTO_TCP):
                    address = ip_address(sockaddr[0])
                    if address in fake_ip:
                        continue
                    if (address.is_private or address.is_loopback or address.is_link_local
                            or address.is_reserved or address.is_multicast):
                        raise ValueError('存储域解析到非公开地址，已阻止。')
                return urllib.request.build_opener(_PinnedRedirect).open(url, timeout=timeout)

            results = []
            for clip in clips[:10]:
                if not isinstance(clip, dict):
                    continue
                title = str(clip.get("title") or "未命名音乐").strip()[:120]
                prompt = str(clip.get("prompt") or "")[:2000]
                email = str(clip.get("email") or "").strip()[:120]
                job_id = re.sub(r"[^0-9a-zA-Z/_-]", "", str(clip.get("job_id") or ""))[:80]
                clip_id = re.sub(r"[^0-9a-zA-Z/_-]", "", str(clip.get("clip_id") or ""))[:80]
                targets = [("audio", str(clip.get("audio_url") or ""))]
                if clip.get("wav_url"):
                    targets.append(("wav", str(clip.get("wav_url"))))
                for kind, url in targets:
                    if not url:
                        continue
                    source_id = "/".join(filter(None, (job_id or clip_id, clip_id if job_id and clip_id else "", kind)))
                    with open_pinned_storage(url) as resp:
                        data = resp.read(64 * 1024 * 1024 + 1)
                    if not data or len(data) > 64 * 1024 * 1024:
                        raise ValueError("音频文件超出 64MB 限制，未导入。")
                    tmp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as tmp:
                            tmp.write(data)
                            tmp_path = Path(tmp.name)
                        from music_import import music_metadata
                        meta = music_metadata(clip, title, prompt, source_id, clip_id, email)
                        ident = await asyncio.to_thread(c.library.archive, tmp_path, meta)
                        results.append({"source_id": source_id, "kind": kind, "title": title, "ok": True,
                                        "asset_id": ident, "path": f"/api/library/{ident}/file"})
                    finally:
                        if tmp_path is not None:
                            Path(tmp_path).unlink(missing_ok=True)
            if not results:
                raise ValueError("没有可登记的音频。")
            return JSONResponse({"success": True, "ingested": results})
        except (ValueError, TypeError, OSError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def material_file(request):
        ident = request.path_params["ident"]
        item = c.library.get(ident)
        path = c.library.file(ident)
        if not item or not path:
            return JSONResponse({"error": "素材文件不存在。"}, status_code=404)
        return FileResponse(
            path,
            media_type=item["mime"],
            filename=item.get("original_name") or path.name,
            content_disposition_type="attachment"
            if request.query_params.get("download") == "1"
            else "inline",
        )

    async def material_thumbnail(request):
        ident = request.path_params["ident"]
        item = c.library.get(ident)
        if not item or item["media_type"] != "image":
            return JSONResponse({"error": "没有图片预览。"}, status_code=404)
        try:
            path = await asyncio.to_thread(c.library.thumbnail, ident)
            return FileResponse(path, media_type="image/jpeg")
        except (ValueError, OSError):
            return JSONResponse({"error": "预览生成失败，原文件保留。"}, status_code=422)

    async def material_tags(request):
        try:
            body = await payload(request)
            ident = request.path_params["ident"]
            if "structured_tags" in body:
                from material_tags import clean_dimensions
                clean_dimensions(body["structured_tags"])
            # Validate both parts before either is saved.
            if "tags" in body and (not isinstance(body["tags"], list) or len(body["tags"]) > 30 or any(not isinstance(t, str) or not 0 < len(t.strip()) <= 80 for t in body["tags"])):
                raise ValueError("最多填写 30 个自定义标签，每个最多 80 字。")
            if "tags" in body:
                c.library.set_tags(ident, body["tags"])
            if "structured_tags" in body:
                c.library.set_dimensions(ident, body["structured_tags"])
            return JSONResponse({"ok": True, "item": c.library.get(ident)})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def material_facets(request):
        return JSONResponse(await asyncio.to_thread(c.library.facets))

    def legacy_item(item):
        if not item:
            return None
        origin = item["origins"][0] if item["origins"] else {}
        return {**item, **{k: origin.get(k, "") for k in ("account_id", "project_id", "model", "prompt", "created")},
                "custom_tags": item["tags"], "tags": item["structured_tags"],
                "file_url": item["url"], "mime_type": item["mime"], "storage_status": "ready",
                "created_at": origin.get("created", ""), "metadata": {"origins": item["origins"]}}

    async def legacy_materials(request):
        p = request.query_params
        try:
            result = await asyncio.to_thread(c.library.search, q=p.get("q", ""), kind=p.get("media_type", ""),
                dimensions={d: p[d] for d in SEARCH_LABELS if d in p}, model=p.get("model", ""),
                account_id=p.get("account_id", ""), project_id=p.get("project_id", ""),
                created_from=p.get("created_from", ""), created_to=p.get("created_to", ""),
                storage_status=p.get("storage_status", ""), style_id=p.get("style_id", ""), index_status=p.get("index_status", ""),
                limit=int(p.get("limit", "30")), offset=int(p.get("offset", "0")))
            return JSONResponse({"items": [legacy_item(i) for i in result["items"]], "total": result["total"], "limit": result["limit"], "offset": result["offset"]})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def legacy_detail(request):
        item = legacy_item(c.library.get(request.path_params["ident"]))
        return JSONResponse(item or {"error": "素材不存在。"}, status_code=200 if item else 404)

    async def index_config(request):
        try:
            result = c.library.index_config() if request.method == "GET" else c.library.configure_index(await payload(request))
            return JSONResponse(result)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def index_retry(request):
        try:
            c.library.retry_index(request.path_params["ident"])
            return JSONResponse({"queued": True, "message": "标签已排队；只有启用标签分析才会调用模型，不会生成媒体。"})
        except ValueError as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def api_keys(request):
        if request.method == "GET":
            return JSONResponse({"items": c.keys.list()})
        try:
            body = await payload(request)
            return JSONResponse(c.keys.create(body.get("name"), body.get("scope", "read")), status_code=201)
        except ValueError as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def api_key_revoke(request):
        found = c.keys.revoke(request.path_params["ident"])
        return JSONResponse({"ok": bool(found)}, status_code=200 if found else 404)

    async def bridge_pair(request):
        try:
            data = await payload(request, allow_empty=True)
            extension = data.get('extensionId', BRIDGE_LEGACY_ID)
            if set(data) - {'extensionId'} or extension not in BRIDGE_IDS:
                raise ValueError('不支持此插件，请在本机账号管理中核对安装版本。')
        except (ValueError, TypeError) as exc:
            return JSONResponse({'error': safe_error(exc)}, status_code=400)
        token = secrets.token_urlsafe(40)
        c.store.execute(
            "INSERT INTO bridge_devices VALUES(?,?)", (c.store.digest(token), time.time())
        )
        return JSONResponse(
            {
                "apiUrl": "http://127.0.0.1:18888/api/plugin/session",
                "connectionToken": token,
                "extensionId": extension,
            }
        )

    async def import_session(request):
        try:
            body = await payload(request)
            bundle = normalize_bundle(body)
            matches = [
                a
                for a in c.store.accounts()
                if a["email"] and a["email"].lower() == bundle["email"]
            ]
            profile = body.get("profile")
            if profile:
                account = c.store.account(profile)
                if not account or account["email"] and account["email"].lower() != bundle["email"]:
                    raise ValueError("登录包邮箱与所选账号不一致，未导入。")
            elif len(matches) == 1:
                profile = matches[0]["profile"]
            elif matches:
                raise ValueError("此邮箱对应多个配置，请在后台明确选择目标账号。")
            else:
                profile = "account-" + c.store.digest(bundle["email"])[:12]
                if c.store.account(profile):
                    raise ValueError("账号名称冲突，请先在后台登记目标账号。")
                c.store.add_account(profile, bundle["email"])
                c.store.update_account(profile, bundle["email"], "unknown")
            if c.store.rows('SELECT 1 FROM flow_removed_accounts WHERE profile=?', (profile,)):
                raise ValueError('此账号已从 Flow 号池移除，无法同步。')
            if c.remote.for_profile(profile):
                raise ValueError("此账号正在远程登录，请先完成远程登录再同步。")
            pending = c.store.rows(
                "SELECT id,status,params FROM tasks WHERE profile=? AND kind='session_import' AND status IN ('queued','running')",
                (profile,),
            )
            if pending:
                previous = pending[0]
                if len(pending) != 1 or json.loads(previous["params"]).get("expected_email") != bundle["email"]:
                    raise ValueError("此配置正在导入另一项登录信息，请等待核对结果。")
                return JSONResponse({
                    "id": previous["id"], "profile": profile, "email": bundle["email"],
                    "status": previous["status"], "reused": True,
                    "message": (WAITING_FOR_PROXY if previous['status'] == 'queued'
                                and c.session_import_waits_for_proxy(profile, bundle['email'])
                                else "已有导入正在核对，继续查看原任务。"),
                }, status_code=202)
            ident = c.vault.put(bundle)
            try:
                task = c.store.create_task(
                    profile,
                    None,
                    "session_import",
                    {"bundle_id": ident, "expected_email": bundle["email"]},
                    secrets.token_hex(16),
                )
            except Exception:
                c.vault.remove(ident)
                raise
            return JSONResponse(
                {
                    "id": task["id"],
                    "profile": profile,
                    "email": bundle["email"],
                    "status": "queued",
                    "message": (WAITING_FOR_PROXY if c.session_import_waits_for_proxy(profile, bundle['email'])
                                else "登录信息已安全接收，等待在线邮箱核对；接收不代表登录成功。"),
                },
                status_code=202,
            )
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": safe_error(exc)}, status_code=400)

    async def import_status(request):
        row = c.store.task(request.path_params["ident"])
        if not row or row["kind"] != "session_import":
            return JSONResponse({"error": "登录同步记录不存在。"}, status_code=404)
        result = row.get('result') or {}
        message = result.get('message')
        if row['status'] == 'queued':
            if c.session_import_waits_for_proxy(row['profile'], row['params'].get('expected_email')):
                message = WAITING_FOR_PROXY
            elif row['params'].get('restart_retries'):
                message = '后台重启前已恢复原登录资料，正在排队自动重新核对。'
        return JSONResponse(
            {
                "id": row["id"],
                "status": row["status"],
                "email": result.get("email"),
                "error": SESSION_IMPORT_INTERRUPTED if row['status'] == 'interrupted' else row.get("error"),
                "message": message,
                "project_count": len(result.get("projects", [])),
            }
        )

    async def remote_start(request):
        profile = request.path_params["profile"]
        async with c.remote_gate:
            try:
                if not c.store.account(profile):
                    raise ValueError("此账号尚未登记。")
                if c.store.rows('SELECT 1 FROM flow_removed_accounts WHERE profile=?', (profile,)):
                    raise ValueError('此账号已从 Flow 号池移除，无法打开远程登录。')
                old = c.remote.for_profile(profile)
                if old:
                    return JSONResponse(c.remote.public(old))
                if profile in c.balance_busy:
                    raise ValueError("此账号正在读取 Flow 积分，请稍后再远程登录。")
                if profile in c.busy:
                    raise ValueError("此账号正在执行任务，请结束后再远程登录。")
                c.busy.add(profile)
                try:
                    result = await asyncio.to_thread(c.remote.start, profile)
                except Exception:
                    c.busy.discard(profile)
                    raise
                return JSONResponse(result)
            except Exception as exc:
                return JSONResponse({"error": safe_error(exc)}, status_code=409)

    async def remote_finish(request):
        ident = request.path_params["ident"]
        async with c.remote_gate:
            s = await asyncio.to_thread(c.remote.close, ident)
            if not s:
                return JSONResponse({"error": "远程登录窗口已关闭。"}, status_code=404)
            c.busy.discard(s["profile"])
            task = c.store.create_task(s["profile"], None, "sync", {}, secrets.token_hex(16))
            return JSONResponse(
                {"id": task["id"], "message": "远程窗口已保存并关闭，正在核对邮箱与登录状态。"}
            )

    async def remote_status(request):
        s = c.remote.sessions.get(request.path_params["ident"])
        if not s:
            return JSONResponse(
                {"error": "远程窗口已结束，请返回账号管理重新打开。"}, status_code=404
            )
        s["expires"] = time.time() + 1200
        return JSONResponse(c.remote.public(s))

    async def remote_ws(ws):
        host, origin = ws.headers.get("host"), ws.headers.get("origin")
        s = c.remote.sessions.get(ws.path_params["ident"])
        if (
            host
            not in {
                "127.0.0.1:18888",
                "localhost:18888",
                "127.0.0.1:18889",
                "localhost:18889",
                "testserver",
            }
            or origin != "http://" + host
            or not c.store.authenticated(ws.cookies.get(cookie_name))
            or not s
            or s["expires"] < time.time()
        ):
            await ws.close(code=4403)
            return
        if s["connected"]:
            await ws.close(code=4409)
            return
        s["connected"] = True
        writer = None
        await ws.accept()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", s["port"])

            async def incoming():
                while True:
                    data = await ws.receive_bytes()
                    if len(data) > 4 * 1024 * 1024:
                        raise ValueError("帧过大")
                    writer.write(data)
                    await writer.drain()

            async def outgoing():
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await ws.send_bytes(data)

            async def alive():
                while True:
                    if (
                        not c.store.authenticated(ws.cookies.get(cookie_name))
                        or s["id"] not in c.remote.sessions
                        or s["expires"] < time.time()
                    ):
                        break
                    await asyncio.sleep(2)

            jobs = [asyncio.create_task(fn()) for fn in (incoming, outgoing, alive)]
            try:
                await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
        except Exception:
            pass
        finally:
            s["connected"] = False
            if writer:
                writer.close()
                await writer.wait_closed()
            with contextlib.suppress(Exception):
                await ws.close()

    routes = [
        Route("/", index),
        Route("/remote", remote_page),
        Route("/health", health),
        Route("/api/auth/connect", connect, methods=["POST"]),
        Route("/api/auth/status", auth_status, methods=["GET"]),
        Route("/api/auth/login", password_login, methods=["POST"]),
        Route("/api/auth/settings", password_settings, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/state", state),
        Route("/api/accounts/credits/refresh", refresh_balances, methods=["POST"]),
        Route("/api/accounts/operator-pause", set_flow_operator_pause, methods=["POST"]),
        Route("/api/accounts/risk-resume", resume_flow_risk, methods=["POST"]),
        Route("/api/accounts/page-incident-review", review_flow_page_incident, methods=["POST"]),
        Route("/api/keys", api_keys, methods=["GET", "POST"]),
        Route("/api/keys/{ident}/revoke", api_key_revoke, methods=["POST"]),
        Route("/api/bridge/pair", bridge_pair, methods=["POST"]),
        Route("/api/plugin/session", import_session, methods=["POST"]),
        Route("/api/plugin/session/{ident}", import_status),
        Route("/api/accounts/session", import_session, methods=["POST"]),
        Route("/api/accounts/{profile}/remote", remote_start, methods=["POST"]),
        Route("/api/remote/{ident}", remote_status),
        Route("/api/remote/{ident}/finish", remote_finish, methods=["POST"]),
        WebSocketRoute("/api/remote/{ident}/ws", remote_ws),
        Route("/api/library", materials),
        Route("/api/library/music", library_music_ingest, methods=["POST"]),
        Route("/api/library/facets", material_facets),
        Route("/api/library/config", index_config, methods=["GET", "POST"]),
        Route("/api/material-library/facets", material_facets),
        Route("/api/material-library/assets", legacy_materials),
        Route("/api/material-library/assets/{ident}/file", material_file),
        Route("/api/material-library/assets/{ident}", legacy_detail),
        Route("/api/library/{ident}/retry-index", index_retry, methods=["POST"]),
        Route("/api/library/{ident}/file", material_file),
        Route("/api/library/{ident}/thumbnail", material_thumbnail),
        Route("/api/library/{ident}/tags", material_tags, methods=["POST"]),
        Route("/api/library/{ident}", material_detail),
        Route("/api/accounts", accounts, methods=["POST"]),
        Route("/api/accounts/flow-remove", remove_flow_account, methods=["POST"]),
        Route("/api/projects", projects, methods=["POST"]),
        Route("/api/retry-project", retry_project, methods=["POST"]),
        Route("/api/tasks", submit, methods=["POST"]),
        Route("/api/tasks/with-references", submit, methods=["POST"]),
        Route("/api/tasks/{ident}", task_status, methods=["GET"]),
        Route("/api/tasks/{ident}/retry", retry_task, methods=["POST"]),
        Route("/api/tasks/{ident}/cancel", cancel, methods=["POST"]),
        Route("/api/tasks/{ident}/files/{index:int}", download),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ]
    vendor = c.root / "remote-tools/novnc"
    if vendor.is_dir():
        routes.append(Mount("/remote-vendor", StaticFiles(directory=vendor), name="remote-vendor"))
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.console = c

    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in {
            "127.0.0.1:18888",
            "localhost:18888",
            "127.0.0.1:18889",
            "localhost:18889",
            "testserver",
        }:
            return JSONResponse({"error": "不允许的访问地址。"}, status_code=403)
        if request.method == "GET" and request.url.path == "/health" and request.headers.get("x-studiowright-probe") == "1":
            return await call_next(request)
        plugin_path = request.url.path == "/api/plugin/session" or request.url.path.startswith(
            "/api/plugin/session/"
        )
        bearer = request.headers.get("authorization", "")
        plugin_auth = (
            plugin_path
            and bearer.startswith("Bearer ")
            and bool(
                c.store.rows(
                    "SELECT hash FROM bridge_devices WHERE hash=?", (c.store.digest(bearer[7:]),)
                )
            )
        )
        api_key = None
        if bearer and not plugin_auth:
            api_key = c.keys.verify(bearer[7:]) if bearer.startswith("Bearer ") else None
            if not api_key:
                return JSONResponse({"error": "API Key 无效或已撤销。"}, status_code=401, headers={"Cache-Control": "no-store"})
            if not c.keys.permits(api_key, request.method, request.url.path):
                return JSONResponse({"error": "此 API Key 没有该操作权限，请使用后台登录管理。"}, status_code=403)
        request.state.api_key = api_key
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if (
                origin
                and origin != "http://" + host
                and not (
                    plugin_auth and origin in BRIDGE_ORIGINS
                )
            ):
                return JSONResponse({"error": "请从后台页面发起操作。"}, status_code=403)
            if request.headers.get("x-console-request") != "1" or not request.headers.get(
                "content-type", ""
            ).startswith("application/json"):
                return JSONResponse({"error": "请求格式不正确。"}, status_code=403)
        if (
            request.url.path.startswith("/api/")
            and request.url.path not in ("/api/auth/connect", "/api/auth/status", "/api/auth/login")
            and not plugin_auth
            and not api_key
            and not local_browser(request)
            and not c.store.authenticated(request.cookies.get(cookie_name))
        ):
            return JSONResponse(
                {"error": "后台连接已到期，请重新打开本机后台入口。"}, status_code=401
            )
        # The user explicitly opted out of the local browser login gate.
        # Retain an automatic cookie for remote-login WebSocket connections.
        auto_token = None
        if local_browser(request) and not bearer and not c.store.authenticated(request.cookies.get(cookie_name)):
            auto_token, _ = web_auth.new_session(False)
        response = await call_next(request)
        if auto_token:
            response.set_cookie(cookie_name, auto_token, httponly=True, samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' http://127.0.0.1:18890; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=guard)
    return app


def configured():
    root = Path(
        os.environ.get("SW_ROOT", str(Path.home() / ".local/share/studiowright-evaluation"))
    )
    if os.environ.get("SW_CONSOLE_TEST") == "1":
        if not root.name.startswith("console-fixture-"):
            raise RuntimeError("模拟执行器只能在独立 console-fixture-* 目录中运行。")
        from adapter import FakeAdapter

        return create_app(root, FakeAdapter(root), test=True)
    from adapter import RealAdapter

    return create_app(root, RealAdapter(root))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "ticket":
        root = Path(
            os.environ.get("SW_ROOT", str(Path.home() / ".local/share/studiowright-evaluation"))
        )
        print(Store(root / "console-data").issue_ticket())
    else:
        import uvicorn

        uvicorn.run(
            configured(),
            host="127.0.0.1",
            port=18889 if os.environ.get("SW_CONSOLE_TEST") == "1" else 18888,
            access_log=False,
        )
