"""Real Gemini use through the same account profiles owned by the existing Flow pool."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import threading
from dispatch_limit import DispatchLimit
import time
import uuid
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ACCOUNT = re.compile(r'[0-9a-f]{64}\Z')
UPLOAD_LIMIT = 100 * 1024 * 1024
UPLOAD_TYPES = {'jpg', 'jpeg', 'png', 'webp', 'mp4', 'mov', 'webm', 'pdf'}
NETWORK_ERRORS = frozenset(('SSLError', 'Timeout', 'TimeoutError', 'ConnectTimeout', 'ReadTimeout',
                            'ConnectionError', 'ConnectError', 'ProxyError', 'OSError'))


def valid_upload_prefix(ext, data):
    if ext in ('jpg', 'jpeg'):
        return data.startswith(b'\xff\xd8\xff')
    if ext == 'png':
        return data.startswith(b'\x89PNG\r\n\x1a\n')
    if ext == 'webp':
        return data.startswith(b'RIFF') and data[8:12] == b'WEBP'
    if ext in ('mp4', 'mov'):
        return len(data) >= 12 and data[4:8] == b'ftyp'
    if ext == 'webm':
        return data.startswith(b'\x1aE\xdf\xa3')
    if ext == 'pdf':
        return data.startswith(b'%PDF-')
    return False


def clean_usage(value):
    if not isinstance(value, dict):
        return None
    cleaned = {}
    for name in ('current_5h', 'weekly'):
        metric = value.get(name)
        if not isinstance(metric, dict):
            continue
        percent, remaining, reset = metric.get('usage_percentage'), metric.get('remaining_credits'), metric.get('reset_at')
        if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100:
            percent = None
        if type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining < 0:
            remaining = None
        if not isinstance(reset, str) or len(reset) > 64 or any(ord(c) < 32 for c in reset):
            reset = None
        if percent is not None or remaining is not None:
            cleaned[name] = {'usage_percentage': percent, 'remaining_credits': remaining, 'reset_at': reset}
    credits = value.get('ai_credits_remaining')
    if type(credits) in (int, float) and math.isfinite(credits) and credits >= 0:
        cleaned['ai_credits_remaining'] = credits
    return cleaned or None


def clean_model_choices(value):
    if not isinstance(value, list):
        return []
    result, seen = [], set()
    for item in value[:100]:
        if not isinstance(item, dict) or set(item) != {'id', 'label'}:
            continue
        ident, label = item['id'], item['label']
        if (not isinstance(ident, str) or not ident.strip() or len(ident) > 200
                or not isinstance(label, str) or not label.strip() or len(label) > 200
                or any(ord(c) < 32 or ord(c) == 127 for c in ident + label) or ident in seen):
            continue
        seen.add(ident)
        result.append({'id': ident, 'label': label})
    return result


class Conflict(ValueError):
    pass


class ProjectNotStarted(Exception):
    pass


class RealPool:
    def __init__(self, state_dir, program_dir, browser_inventory=None):
        self.monitor_heartbeat = {'last_completed': None, 'last_error': None}
        self.state_dir = Path(state_dir)
        self.program_dir = Path(program_dir)
        if self.state_dir.is_symlink():
            raise ValueError('账号状态目录不安全。')
        self.state_dir.mkdir(mode=0o700 if os.name == 'posix' else 0o777, parents=True, exist_ok=True)
        self.path = self.state_dir / 'real-pool.sqlite3'
        lock_path = self.state_dir / 'real-pool.lock'
        if self.path.is_symlink() or lock_path.is_symlink():
            raise ValueError('账号状态文件不安全。')
        self.lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        if os.name == 'posix':
            import fcntl
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(self.lock_fd)
                raise RuntimeError('已有工作台使用真实账号状态。') from None
        self.dispatch_limit = DispatchLimit(self.state_dir / 'dispatch')
        self.browser_inventory = self._browser_inventory if browser_inventory is None else browser_inventory
        self.upload_lock = threading.Lock()
        self.flow_reference_locks = tuple(threading.Lock() for _ in range(32))
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS account_status(
                    account_key TEXT PRIMARY KEY, gemini_status TEXT NOT NULL,
                    checked REAL, available_models INTEGER, last_note TEXT,
                    quota_json TEXT, quota_checked REAL);
                CREATE TABLE IF NOT EXISTS requests(
                    request_id TEXT PRIMARY KEY, account_key TEXT NOT NULL,
                    prompt_digest TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                    finished REAL, response TEXT, note TEXT, selection TEXT NOT NULL DEFAULT 'manual');
                CREATE TABLE IF NOT EXISTS account_unknown_release(
                    request_id TEXT PRIMARY KEY REFERENCES requests(request_id),
                    released REAL NOT NULL, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pooldesk_attempts(
                    request_id TEXT PRIMARY KEY REFERENCES requests(request_id),
                    account_key TEXT NOT NULL, stage TEXT NOT NULL, outcome TEXT,
                    engine_version TEXT NOT NULL DEFAULT 'adapter-v1');
                CREATE TABLE IF NOT EXISTS request_metrics(
                    request_id TEXT PRIMARY KEY REFERENCES requests(request_id),
                    first_text_ms REAL, total_ms REAL, token_count INTEGER);
                CREATE TABLE IF NOT EXISTS request_diagnostics(
                    request_id TEXT PRIMARY KEY REFERENCES requests(request_id),
                    phase TEXT, failure_stage TEXT, error_type TEXT, http_status INTEGER, first_text_seen INTEGER);
                CREATE TABLE IF NOT EXISTS gemini_images(
                    request_id TEXT NOT NULL REFERENCES requests(request_id), position INTEGER NOT NULL,
                    image_id TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    PRIMARY KEY(request_id,position));
                CREATE TABLE IF NOT EXISTS gemini_image_status(
                    request_id TEXT PRIMARY KEY REFERENCES requests(request_id), expected INTEGER NOT NULL, saved INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS flow_requests(
                    request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, project_key TEXT NOT NULL,
                    profile TEXT NOT NULL, project TEXT NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, finished REAL, task_id TEXT, note TEXT);
                CREATE TABLE IF NOT EXISTS flow_auto_projects(
                    profile TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, intent TEXT NOT NULL,
                    status TEXT NOT NULL, project TEXT, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS flow_recoveries(
                    task_id TEXT PRIMARY KEY, status TEXT NOT NULL, filename TEXT,
                    sha256 TEXT, size INTEGER, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS account_monitor(
                    account_key TEXT PRIMARY KEY, status TEXT NOT NULL, next_due REAL,
                    last_attempt REAL, last_note TEXT);
                CREATE TABLE IF NOT EXISTS account_checks(
                    id TEXT PRIMARY KEY, account_key TEXT NOT NULL, operation TEXT NOT NULL,
                    outcome TEXT NOT NULL, reason TEXT NOT NULL, phase TEXT, error_type TEXT,
                    quota_state TEXT NOT NULL, checked REAL NOT NULL, note TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS account_checks_latest ON account_checks(account_key,checked DESC);
                CREATE TABLE IF NOT EXISTS keepalive_evidence(
                    id TEXT PRIMARY KEY, account_key TEXT NOT NULL, checked REAL NOT NULL, status TEXT,
                    cookie_returned INTEGER, expiry_before REAL, expiry_after REAL, value_changed INTEGER);
                CREATE TABLE IF NOT EXISTS account_preferences(
                    account_key TEXT PRIMARY KEY, gemini_enabled INTEGER NOT NULL DEFAULT 1
                    CHECK(gemini_enabled IN (0,1)), keepalive_enabled INTEGER NOT NULL DEFAULT 1
                    CHECK(keepalive_enabled IN (0,1)));
                CREATE TABLE IF NOT EXISTS gemini_pool_removals(
                    account_key TEXT PRIMARY KEY, removed_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS gemini_dispatch_policy(
                    id INTEGER PRIMARY KEY CHECK(id=1), five_hour_limit INTEGER NOT NULL
                    CHECK(five_hour_limit BETWEEN 1 AND 100), weekly_limit INTEGER NOT NULL
                    CHECK(weekly_limit BETWEEN 1 AND 100));
                CREATE TABLE IF NOT EXISTS conversations(
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL,
                    updated REAL NOT NULL, account_key TEXT, native_json TEXT, blocked TEXT,
                    hidden INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS conversation_turns(
                    request_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    prompt TEXT NOT NULL, attachments TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversation_attempt_models(
                    request_id TEXT PRIMARY KEY, model TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS material_extra(
                    asset_id TEXT PRIMARY KEY, manual_json TEXT NOT NULL DEFAULT '{}',
                    suggested_json TEXT NOT NULL DEFAULT '{}', updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS material_analyses(
                    request_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, finished REAL, note TEXT);
                CREATE INDEX IF NOT EXISTS material_analyses_asset_recent ON material_analyses(asset_id,created DESC);
                CREATE TABLE IF NOT EXISTS material_shot_notes(
                    asset_id TEXT PRIMARY KEY, notes_json TEXT NOT NULL, source TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS material_shot_jobs(
                    asset_id TEXT PRIMARY KEY, queue_id TEXT UNIQUE NOT NULL, request_id TEXT UNIQUE,
                    status TEXT NOT NULL, created REAL NOT NULL, note TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS auto_material_settings(
                    id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 1,
                    since REAL, paused_reason TEXT);
                CREATE TABLE IF NOT EXISTS auto_material_jobs(
                    asset_id TEXT PRIMARY KEY, request_id TEXT UNIQUE, status TEXT NOT NULL,
                    added REAL NOT NULL, updated REAL NOT NULL, note TEXT);
                CREATE TABLE IF NOT EXISTS library_storage_plan(
                    id INTEGER PRIMARY KEY CHECK(id=1), current TEXT NOT NULL,
                    target TEXT NOT NULL, checked REAL NOT NULL);
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(account_status)')}
            preference_columns = {row[1] for row in db.execute('PRAGMA table_info(account_preferences)')}
            if 'keepalive_enabled' not in preference_columns:
                db.execute('ALTER TABLE account_preferences ADD COLUMN keepalive_enabled INTEGER NOT NULL DEFAULT 1 CHECK(keepalive_enabled IN (0,1))')
            if 'quota_json' not in columns:
                db.execute('ALTER TABLE account_status ADD COLUMN quota_json TEXT')
            if 'quota_checked' not in columns:
                db.execute('ALTER TABLE account_status ADD COLUMN quota_checked REAL')
            if 'models_json' not in columns:
                db.execute('ALTER TABLE account_status ADD COLUMN models_json TEXT')
            request_columns = {row[1] for row in db.execute('PRAGMA table_info(requests)')}
            if 'selection' not in request_columns:
                db.execute("ALTER TABLE requests ADD COLUMN selection TEXT NOT NULL DEFAULT 'manual'")
            pooldesk_columns = {row[1] for row in db.execute('PRAGMA table_info(pooldesk_attempts)')}
            if 'engine_version' not in pooldesk_columns:
                db.execute("ALTER TABLE pooldesk_attempts ADD COLUMN engine_version TEXT NOT NULL DEFAULT 'adapter-v1'")
            conversation_columns = {row[1] for row in db.execute('PRAGMA table_info(conversations)')}
            if 'hidden' not in conversation_columns:
                db.execute('ALTER TABLE conversations ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0')
            # An interrupted request may have reached Google. Never resume it.
            db.execute("UPDATE requests SET status='unknown',finished=?,note=? WHERE status='dispatching'",
                       (time.time(), '请求中断，结果需要核对'))
            db.execute("""UPDATE pooldesk_attempts SET stage='uncertain',outcome='unknown'
                          WHERE outcome IS NULL AND request_id IN
                          (SELECT request_id FROM requests WHERE status='unknown')""")
            db.execute("UPDATE flow_requests SET status='unknown',finished=?,note=? WHERE status='dispatching'",
                       (time.time(), '提交中断，请到原后台核对任务'))
            db.execute("UPDATE flow_recoveries SET status='unknown',updated=? WHERE status='recovering'", (time.time(),))
            db.execute("UPDATE account_monitor SET status='paused',next_due=NULL,last_note='上次检查中断，请手动核对' WHERE status='dispatching'")
            db.execute("""UPDATE material_analyses SET status=CASE
                       WHEN request_id IN (SELECT request_id FROM requests WHERE status!='failed') THEN 'unknown'
                       ELSE 'failed' END, finished=?, note='上次分析中断，请核对后再操作' WHERE status IN ('preparing','saving')""", (time.time(),))
            # Fresh installations start enabled; retain every existing user's setting.
            db.execute('INSERT OR IGNORE INTO auto_material_settings(id,enabled,since) VALUES(1,1,?)', (time.time(),))
            if db.execute("SELECT 1 FROM auto_material_jobs WHERE status='running' LIMIT 1").fetchone():
                db.execute("UPDATE auto_material_jobs SET status='unknown',updated=?,note='分析中断，结果需核对，不会重复提交' WHERE status='running'", (time.time(),))
                db.execute("UPDATE auto_material_settings SET paused_reason='有分析结果未确认，请核对后再继续' WHERE id=1")
        if os.name == 'posix':
            self.state_dir.chmod(0o700)
            self.path.chmod(0o600)
        from official_pool import OfficialPool
        self.official = OfficialPool(self)

    def connection(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        return closing(db)

    def close(self):
        if hasattr(self, 'official'):
            self.official.close()
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def maintenance(self):
        marker = self.state_dir / 'maintenance'
        return marker.exists() or marker.is_symlink()

    def library_storage_plan(self):
        with self.connection() as db:
            row = db.execute('SELECT current,target,checked FROM library_storage_plan WHERE id=1').fetchone()
            return dict(row) if row else None

    def save_library_storage_plan(self, current, target):
        if self.maintenance():
            raise ValueError('工作台正在更新，请稍后再试。')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if self.maintenance():
                    raise ValueError('工作台正在更新，请稍后再试。')
                db.execute('''INSERT INTO library_storage_plan(id,current,target,checked) VALUES(1,?,?,?)
                              ON CONFLICT(id) DO UPDATE SET current=excluded.current,
                              target=excluded.target,checked=excluded.checked''',
                           (current, target, time.time()))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return self.library_storage_plan()

    @staticmethod
    def allowed(account, items):
        if not isinstance(account, str) or not ACCOUNT.fullmatch(account):
            raise ValueError('账号标识不正确。')
        if sum(1 for item in items if item.get('key') == 'flow-account-' + account) != 1:
            raise Conflict('现有账号池中没有唯一匹配的账号。')

    @staticmethod
    def dispatch_policy(db):
        row = db.execute('SELECT five_hour_limit,weekly_limit FROM gemini_dispatch_policy WHERE id=1').fetchone()
        return dict(row) if row else {'five_hour_limit': 100, 'weekly_limit': 100}

    def set_dispatch_policy(self, five_hour_limit, weekly_limit):
        if (type(five_hour_limit) is not int or not 1 <= five_hour_limit <= 100
                or type(weekly_limit) is not int or not 1 <= weekly_limit <= 100):
            raise ValueError('用量上限请输入 1 到 100 的整数。')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.maintenance():
                db.execute('ROLLBACK')
                raise Conflict('工作台正在更新，请稍后操作。')
            db.execute('''INSERT INTO gemini_dispatch_policy(id,five_hour_limit,weekly_limit) VALUES(1,?,?)
                          ON CONFLICT(id) DO UPDATE SET five_hour_limit=excluded.five_hour_limit,
                          weekly_limit=excluded.weekly_limit''', (five_hour_limit, weekly_limit))
            db.execute('COMMIT')
        return {'five_hour_limit': five_hour_limit, 'weekly_limit': weekly_limit}

    @staticmethod
    def usage_dispatch_state(usage, checked, now, policy):
        if type(checked) not in (int, float) or not math.isfinite(checked) or now - checked < 0:
            return 'usage_unknown'
        current = usage.get('current_5h', {}).get('usage_percentage') if usage else None
        weekly = usage.get('weekly', {}).get('usage_percentage') if usage else None
        if current is None:
            return 'usage_unknown'
        if current >= policy['five_hour_limit'] or (weekly is not None and weekly >= policy['weekly_limit']):
            return 'limit_reached'
        return 'refresh_needed' if now - checked > 900 else 'eligible'

    def annotate(self, real_accounts):
        if not real_accounts.get('available'):
            return
        removed = self.removed_gemini_accounts()
        try:
            registered = (self.official.runtime.settings() or {}).get('accounts', {})
        except (OSError, ValueError, TypeError):
            registered = {}
        for item in real_accounts.get('items', []):
            account = item.get('key', '').removeprefix('flow-account-')
            item['gemini_removed'] = account in removed
            item['gemini_registered'] = account in registered
        if self.official.ready:
            from official_pool import OFFICIAL_REQUEST_SQL,busy_official
            from gemini_effort import text_model_options
            status = self.official.public_status()
            for item in real_accounts.get('items', []):
                account = item.get('key', '').removeprefix('flow-account-')
                official = status['accounts'].get(account, {})
                item.update(gemini='verified' if official.get('authorization') == 'saved_verified_identity' else 'unlinked',
                            gemini_channel=status['channel'], gemini_auto_state='eligible' if official.get('dispatch_eligible') else official.get('restriction', 'authorization_not_ready'),
                            gemini_model_options=text_model_options(official.get('advanced_text_models',())),
                            keepalive_state='not_required', keepalive_note='官方调用不使用网页保活；手动暂停和官方通道未确认限制保留。')
                with self.connection() as db:
                    pref=db.execute('SELECT gemini_enabled FROM account_preferences WHERE account_key=?',(account,)).fetchone()
                    item['gemini_enabled']=not pref or pref[0] != 0
                    item['gemini_unresolved']=db.execute("SELECT count(*) FROM requests r WHERE r.account_key=? AND r.status='unknown' AND "+OFFICIAL_REQUEST_SQL+" AND NOT EXISTS(SELECT 1 FROM account_unknown_release x WHERE x.request_id=r.request_id)",(account,)).fetchone()[0]
                    item['gemini_busy']=busy_official(db,account)
            real_accounts['official_dispatch']={k:v for k,v in status.items() if k!='accounts'}
            return
        browser_states = self._read_browser_inventory()
        with self.connection() as db:
            rows = {row['account_key']: dict(row) for row in db.execute('SELECT * FROM account_status')}
            monitors = {row['account_key']: dict(row) for row in db.execute('SELECT * FROM account_monitor')}
            diagnostics = {row['account_key']: dict(row) for row in db.execute('''SELECT * FROM account_checks e
                WHERE e.id=(SELECT id FROM account_checks q WHERE q.account_key=e.account_key ORDER BY checked DESC,id DESC LIMIT 1)''')}
            preferences = {row['account_key']: dict(row) for row in db.execute('SELECT * FROM account_preferences')}
            active = {row['account_key'] for row in db.execute("SELECT DISTINCT account_key FROM requests WHERE status='dispatching'")}
            unresolved_history = {row['account_key']: row['n'] for row in db.execute("SELECT account_key,count(*) AS n FROM requests WHERE status='unknown' GROUP BY account_key")}
            unresolved = {row['account_key']: row['n'] for row in db.execute("""SELECT r.account_key,count(*) AS n FROM requests r WHERE r.status='unknown'
                AND NOT EXISTS(SELECT 1 FROM account_unknown_release x WHERE x.request_id=r.request_id) GROUP BY r.account_key""")}
            policy = self.dispatch_policy(db)
        real_accounts['dispatch_policy'] = policy
        now = time.time()
        for item in real_accounts.get('items', []):
            account = item.get('key', '').removeprefix('flow-account-')
            row = rows.get(account)
            item['gemini'] = row['gemini_status'] if row else 'unlinked'
            item['gemini_checked'] = row['checked'] if row else None
            item['gemini_models'] = row['available_models'] if row else None
            try:
                item['gemini_model_options'] = clean_model_choices(json.loads(row['models_json'])) if row and row['models_json'] else []
            except (ValueError, TypeError):
                item['gemini_model_options'] = []
            try:
                item['gemini_usage'] = clean_usage(json.loads(row['quota_json'])) if row and row['quota_json'] else None
            except (ValueError, TypeError):
                item['gemini_usage'] = None
            item['gemini_usage_checked'] = row['quota_checked'] if row else None
            monitor = monitors.get(account)
            preference = preferences.get(account, {})
            item['gemini_enabled'] = bool(preference.get('gemini_enabled', 1))
            item['keepalive_enabled'] = bool(preference.get('keepalive_enabled', 1))
            item['keepalive_next_due'] = monitor['next_due'] if monitor and monitor['status'] == 'ready' else None
            item['gemini_busy'] = account in active or bool(monitor and monitor['status'] == 'dispatching')
            item['gemini_unresolved'] = unresolved.get(account, 0)
            item['gemini_history_unconfirmed'] = unresolved_history.get(account, 0)
            if monitor and monitor['status'] in ('paused', 'network_wait'):
                item['gemini'] = 'paused'
            if not item['gemini_enabled']:
                item['gemini_auto_state'] = 'disabled'
            elif item['gemini_busy']:
                item['gemini_auto_state'] = 'busy'
            elif item['gemini_unresolved']:
                item['gemini_auto_state'] = 'unresolved'
            elif self.browser_inventory is not None and browser_states is None:
                item['gemini_auto_state'] = 'browser_unknown'
            elif self.browser_inventory is not None and browser_states.get(account) is not True:
                item['gemini_auto_state'] = 'browser_unavailable'
            elif item['gemini'] != 'verified':
                item['gemini_auto_state'] = 'login_unconfirmed'
            else:
                item['gemini_auto_state'] = self.usage_dispatch_state(item['gemini_usage'], item['gemini_usage_checked'], now, policy)
            item['keepalive_note'] = ('网络检查未完成，请手动核对；不会自动重试' if monitor and monitor['status'] == 'network_wait'
                                      else monitor['last_note'] if monitor else None)
            item['keepalive_checked'] = monitor['last_attempt'] if monitor else None
            item['keepalive_state'] = ('disabled' if not item['keepalive_enabled'] or not item['gemini_enabled'] else
                                      'blocked_result' if item['gemini_unresolved'] else
                                      monitor['status'] if monitor else 'awaiting_check')
            detail = diagnostics.get(account)
            item['gemini_check_note'] = detail['note'] if detail else None
            item['gemini_check_reason'] = detail['reason'] if detail else None
            item['gemini_quota_state'] = detail['quota_state'] if detail else 'historical' if item['gemini_usage'] else 'unchecked'

    def removed_gemini_accounts(self):
        with self.connection() as db:
            return {row[0] for row in db.execute('SELECT account_key FROM gemini_pool_removals')}

    def remove_gemini_account(self, account, items):
        from gemini_quota_verification import status as quota_verification_status
        from official_quota import reader_for
        self.allowed(account, items)
        message = '账号已从 Gemini 号池删除。历史对话已保留，Flow 和 Music 不受影响。'
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.maintenance():
                raise Conflict('工作台正在更新，请稍后操作。')
            if db.execute('SELECT 1 FROM gemini_pool_removals WHERE account_key=?', (account,)).fetchone():
                return {'removed': True, 'message': message}
            registered = (self.official.runtime.settings() or {}).get('accounts', {})
            if account not in registered:
                raise Conflict('该账号当前不在 Gemini 号池。')
            active = db.execute("SELECT 1 FROM requests WHERE account_key=? AND status IN ('queued','dispatching','running') LIMIT 1",
                                (account,)).fetchone()
            monitor = db.execute("SELECT 1 FROM account_monitor WHERE account_key=? AND status='dispatching'",
                                 (account,)).fetchone()
            authorizing = db.execute("SELECT 1 FROM official_reauth WHERE account_key=? AND state IN ('waiting','queued','resending')",
                                     (account,)).fetchone()
            batch = self.official.reauth.activation.status()
            verifying = quota_verification_status(reader_for(self.official.runtime))
            email = registered[account].get('email')
            authorization_busy = any(stage['state'] == 'starting' or
                                     (stage['state'] in ('waiting','rechecking') and stage.get('email') == email)
                                     for stage in (batch, verifying))
            if active or monitor or authorizing or authorization_busy:
                raise Conflict('该账号有排队、执行或授权中的 Gemini 操作，请结束后再删除。')
            db.execute('INSERT INTO gemini_pool_removals(account_key,removed_at) VALUES(?,?)', (account,time.time()))
            # A worker may already have read the runtime inventory before this transaction.
            db.execute('''INSERT INTO account_preferences(account_key,gemini_enabled) VALUES(?,0)
                          ON CONFLICT(account_key) DO UPDATE SET gemini_enabled=0''', (account,))
            db.execute('COMMIT')
        return {'removed': True, 'message': message}

    def set_gemini_enabled(self, account, enabled, items):
        """Change workbench Gemini eligibility without altering Flow's account or credentials."""
        self.allowed(account, items)
        if type(enabled) is not bool:
            raise ValueError('账号开关格式不正确。')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if self.maintenance():
                    raise Conflict('工作台正在更新，请稍后操作。')
                if enabled and db.execute('SELECT 1 FROM gemini_pool_removals WHERE account_key=?', (account,)).fetchone():
                    raise Conflict('该账号已从 Gemini 号池删除。')
                active = db.execute("SELECT 1 FROM requests WHERE account_key=? AND status='dispatching' LIMIT 1", (account,)).fetchone()
                monitor = db.execute('SELECT status FROM account_monitor WHERE account_key=?', (account,)).fetchone()
                if active or (monitor and monitor['status'] == 'dispatching'):
                    raise Conflict('该账号正在执行或检查，请结束后再操作。')
                db.execute('''INSERT INTO account_preferences(account_key,gemini_enabled) VALUES(?,?)
                              ON CONFLICT(account_key) DO UPDATE SET gemini_enabled=excluded.gemini_enabled''',
                           (account, int(enabled)))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return {'enabled': enabled, 'message': 'Gemini 使用已启用；账号状态及未确认结果不会自动改变。' if enabled
                else 'Gemini 使用及自动保活已暂停；Flow 不受影响。'}

    def set_keepalive_enabled(self, account, enabled, items):
        """Control only the existing Gemini keepalive; never log in or generate here."""
        self.allowed(account, items)
        if type(enabled) is not bool:
            raise ValueError('保活开关格式不正确。')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if self.maintenance():
                    raise Conflict('工作台正在更新，请稍后操作。')
                monitor = db.execute('SELECT status FROM account_monitor WHERE account_key=?', (account,)).fetchone()
                if monitor and monitor['status'] == 'dispatching':
                    raise Conflict('该账号正在检查，请结束后再操作。')
                db.execute('''INSERT INTO account_preferences(account_key,keepalive_enabled) VALUES(?,?)
                              ON CONFLICT(account_key) DO UPDATE SET keepalive_enabled=excluded.keepalive_enabled''',
                           (account, int(enabled)))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return {'enabled': enabled, 'message': '自动保活已启用；账号异常和未确认结果不会自动清除。' if enabled
                else '自动保活已暂停；Gemini 使用和 Flow 不受此开关影响。'}

    def request_history(self, include_checks=True):
        """Only workbench requests, without message bodies or upstream credentials."""
        with self.connection() as db:
            rows = db.execute('''SELECT r.request_id,r.account_key,r.selection,r.status,r.created,r.finished,r.note,
                                 CASE WHEN j.request_id IS NOT NULL THEN 'official_antigravity_cli' WHEN p.request_id IS NULL THEN 'legacy' ELSE 'pooldesk' END AS engine,
                                 p.stage AS engine_stage,p.engine_version AS engine_version,j.model,
                                 COALESCE(j.readiness_probe,0) AS readiness_probe
                                 FROM requests r LEFT JOIN pooldesk_attempts p ON p.request_id=r.request_id
                                 LEFT JOIN official_jobs j ON j.request_id=r.request_id
                                 WHERE ? OR COALESCE(j.readiness_probe,0)=0
                                 ORDER BY r.created DESC LIMIT 100''', (int(include_checks),)).fetchall()
        return {'source': 'workbench', 'items': [dict(row) for row in rows]}

    def dialogue_statistics(self):
        """Aggregate this workbench's dialogue records, excluding material analysis.

        Rolling 7-day window: stale failures age out instead of polluting the UI.
        """
        cutoff = time.time() - 7 * 86400
        with self.connection() as db:
            totals = dict(db.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN r.status='completed' THEN 1 ELSE 0 END) AS completed,"
                " SUM(CASE WHEN r.status='failed' THEN 1 ELSE 0 END) AS failed,"
                " SUM(CASE WHEN r.status='unknown' THEN 1 ELSE 0 END) AS unresolved,"
                " SUM(CASE WHEN r.status='dispatching' THEN 1 ELSE 0 END) AS processing,"
                " AVG(CASE WHEN r.status='completed' THEN m.first_text_ms END) AS avg_first_ms,"
                " COUNT(CASE WHEN r.status='completed' THEN m.first_text_ms END) AS first_samples,"
                " AVG(CASE WHEN r.status='completed' AND r.finished>=r.created"
                " THEN (r.finished-r.created)*1000 END) AS avg_total_ms,"
                " COUNT(CASE WHEN r.status='completed' AND r.finished>=r.created THEN 1 END)"
                " AS total_samples"
                " FROM requests r LEFT JOIN request_metrics m USING(request_id)"
                " WHERE NOT EXISTS(SELECT 1 FROM material_analyses a WHERE a.request_id=r.request_id)"
                " AND r.created >= ?", (cutoff,)).fetchone())
            accounts = {}
            for row in db.execute(
                "SELECT r.account_key,"
                " COUNT(*) AS total,"
                " SUM(CASE WHEN r.status='completed' THEN 1 ELSE 0 END) AS completed,"
                " SUM(CASE WHEN r.status='failed' THEN 1 ELSE 0 END) AS failed,"
                " SUM(CASE WHEN r.status='unknown' THEN 1 ELSE 0 END) AS unresolved,"
                " SUM(CASE WHEN r.status='dispatching' THEN 1 ELSE 0 END) AS processing,"
                " AVG(CASE WHEN r.status='completed' THEN m.first_text_ms END) AS avg_first_ms,"
                " COUNT(CASE WHEN r.status='completed' THEN m.first_text_ms END) AS first_samples,"
                " AVG(CASE WHEN r.status='completed' AND r.finished>=r.created"
                " THEN (r.finished-r.created)*1000 END) AS avg_total_ms,"
                " COUNT(CASE WHEN r.status='completed' AND r.finished>=r.created THEN 1 END)"
                " AS total_samples"
                " FROM requests r LEFT JOIN request_metrics m USING(request_id)"
                " WHERE NOT EXISTS(SELECT 1 FROM material_analyses a WHERE a.request_id=r.request_id)"
                " AND r.created >= ? GROUP BY r.account_key", (cutoff,)):
                accounts[row['account_key']] = dict(row)
        for record in [totals,*accounts.values()]:
            for field in ('total','completed','failed','unresolved','processing','first_samples','total_samples'):
                record[field]=record[field] or 0
        return {'total':totals,'accounts':accounts,'token_count':None,'token_source':'unavailable'}

    def _read_browser_inventory(self):
        if self.browser_inventory is None:
            return None
        try:
            states = self.browser_inventory()
            if (not isinstance(states, dict) or len(states) > 50
                    or any(not isinstance(k, str) or not ACCOUNT.fullmatch(k) or type(v) is not bool for k, v in states.items())):
                return None
            return states
        except Exception:
            return None

    def _browser_inventory(self):
        flow_python = Path.home() / '.local/share/studiowright-evaluation/venv/bin/python'
        controller = self.program_dir / 'shared_session_controller.py'
        if not flow_python.is_file() or not controller.is_file() or controller.is_symlink():
            return None
        try:
            result = subprocess.run([str(flow_python), '-B', str(controller), '--operation', 'inventory'],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8, check=True)
            if len(result.stdout) > 65536:
                return None
            data = json.loads(result.stdout)
            return data.get('accounts') if isinstance(data, dict) and data.get('result') == 'browser_inventory' else None
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
            return None

    def get(self, request_id):
        with self.connection() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT request_id,account_key,selection,status,created,finished,response,note FROM requests WHERE request_id=?', (request_id,)).fetchone()
            diagnostic = db.execute('SELECT phase,failure_stage,error_type,http_status,first_text_seen FROM request_diagnostics WHERE request_id=?', (request_id,)).fetchone()
            images = db.execute('SELECT position FROM gemini_images WHERE request_id=? ORDER BY position', (request_id,)).fetchall()
            image_status = db.execute('SELECT expected,saved FROM gemini_image_status WHERE request_id=?', (request_id,)).fetchone()
            if not row:
                return None
            official=db.execute("""SELECT j.conversation_id,j.queue_wait_ms,j.model,j.reason,t.attachments,a.state AS reauth_state,
                                  f.requested_model,f.fallback_model,f.reason AS fallback_reason,
                                  b.reason AS block_reason,b.created AS block_created,
                                  CASE WHEN json_valid(c.native_json) THEN json_extract(c.native_json,'$.channel') END AS channel
                                  FROM official_jobs j LEFT JOIN conversations c ON c.id=j.conversation_id
                                  JOIN conversation_turns t USING(request_id)
                                  LEFT JOIN official_model_fallbacks f USING(request_id)
                                  LEFT JOIN official_reauth a USING(request_id)
                                  LEFT JOIN official_account_blocks b ON b.account_key=(SELECT account_key FROM requests WHERE request_id=j.request_id)
                                  WHERE j.request_id=?""",(request_id,)).fetchone()
            metric=db.execute('SELECT first_text_ms,total_ms,token_count FROM request_metrics WHERE request_id=?',(request_id,)).fetchone()
            attempts=[dict(a) for a in db.execute("""SELECT ordinal,status,reason,submitted,started,finished,total_ms,
                         CASE WHEN status!='completed' THEN response END AS response
                         FROM official_attempts WHERE request_id=? ORDER BY ordinal""",(request_id,))]
        result = dict(row)
        result['diagnostic'] = dict(diagnostic) if diagnostic else None
        if official:
            from gemini_image_input import CHANNEL as image_channel
            from gemini_effort import ADVANCED_MODELS
            result.update({name:official[name] for name in ('conversation_id','queue_wait_ms','model')})
            result['channel']=image_channel if official['channel']==image_channel else 'official_antigravity_cli'
            if official['model'] in ADVANCED_MODELS:
                result['reason']=official['reason']
            if official['requested_model']:
                result.update(requested_model=official['requested_model'], reason=official['reason'],
                              fallback={'model':official['fallback_model'],
                                        'used':official['fallback_reason'] is not None,
                                        'reason':official['fallback_reason']})
            if result['channel']==image_channel:result['reason']=official['reason']
            else:
                result['attempts']=attempts
                result['reauthorization']=self.official.reauth.view(
                    {**result,'official_reason':official['reason'],'attachments':official['attachments'],
                     'block_reason':official['block_reason'],'block_created':official['block_created'],
                     'first_text_ms':metric['first_text_ms'] if metric else None},attempts,official['reauth_state'])
        if metric:result.update(dict(metric))
        result['images'] = ['/api/gemini-images/'+request_id+'/'+str(image['position']) for image in images]
        result['image_status'] = dict(image_status) if image_status else None
        return result

    def gemini_image(self, request_id, position):
        if not re.fullmatch(r'[0-9a-f-]{36}', request_id) or type(position) is not int or not 0 <= position < 4:
            return None
        with self.connection() as db:
            row = db.execute('SELECT image_id,mime,size,sha256 FROM gemini_images WHERE request_id=? AND position=?',
                             (request_id,position)).fetchone()
        if not row or not re.fullmatch('[0-9a-f]{32}',row['image_id']):
            return None
        directory = self.state_dir/'gemini-generated-images'
        path = directory/(row['image_id']+'.image')
        try:
            if directory.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size != row['size']:
                return None
            data = path.read_bytes()
            if len(data)>20*1024*1024 or hashlib.sha256(data).hexdigest()!=row['sha256']:
                return None
            return data,row['mime']
        except OSError:
            return None

    @staticmethod
    def conversation_id(value):
        try:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError('对话编号不正确。') from None
        return value

    def conversations(self, hidden=False):
        with self.connection() as db:
            rows = db.execute('SELECT id,title,created,updated,account_key,blocked,hidden FROM conversations WHERE hidden=? ORDER BY updated DESC LIMIT 500', (int(hidden),)).fetchall()
        return {'items': [dict(row) for row in rows]}

    def conversation(self, ident):
        ident = self.conversation_id(ident)
        with self.connection() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT id,title,created,updated,account_key,blocked,hidden,native_json FROM conversations WHERE id=?', (ident,)).fetchone()
            if not row:
                return None
            turns = db.execute('''SELECT t.request_id,t.prompt,t.attachments,r.account_key,r.status,r.response,r.note,r.created,m.first_text_ms,m.token_count,j.queue_wait_ms,am.model AS selected_model,j.reason AS official_reason,a.state AS reauth_state,
                                  r.finished,b.reason AS block_reason,b.created AS block_created,
                                  COALESCE(m.total_ms,CASE WHEN r.status='completed' AND r.finished>=r.created THEN (r.finished-r.created)*1000 END) AS total_ms
                                  FROM conversation_turns t JOIN requests r ON r.request_id=t.request_id
                                  LEFT JOIN request_metrics m ON m.request_id=r.request_id
                                  LEFT JOIN official_jobs j ON j.request_id=r.request_id
                                  LEFT JOIN official_reauth a ON a.request_id=r.request_id
                                  LEFT JOIN official_account_blocks b ON b.account_key=r.account_key
                                  LEFT JOIN conversation_attempt_models am ON am.request_id=t.request_id
                                  WHERE t.conversation_id=? ORDER BY r.created,r.request_id''', (ident,)).fetchall()
            attempted = db.execute('''SELECT m.model FROM conversation_turns t
                                      JOIN requests r ON r.request_id=t.request_id
                                      JOIN conversation_attempt_models m ON m.request_id=t.request_id
                                      WHERE t.conversation_id=? AND r.status='unknown'
                                      ORDER BY r.created DESC,r.request_id DESC LIMIT 1''', (ident,)).fetchone()
            images = db.execute('''SELECT i.request_id,i.position FROM gemini_images i
                                   JOIN conversation_turns t USING(request_id) WHERE t.conversation_id=? ORDER BY i.position''', (ident,)).fetchall()
            image_status = {r['request_id']:dict(r) for r in db.execute('''SELECT s.request_id,s.expected,s.saved FROM gemini_image_status s
                            JOIN conversation_turns t USING(request_id) WHERE t.conversation_id=?''', (ident,))}
            attempts = {}
            for entry in db.execute('''SELECT a.request_id,a.ordinal,a.status,a.reason,a.submitted,a.started,a.finished,a.total_ms,
                       CASE WHEN a.status!='completed' THEN a.response END AS response
                       FROM official_attempts a JOIN conversation_turns t USING(request_id)
                       WHERE t.conversation_id=? ORDER BY a.ordinal''', (ident,)):
                attempt = dict(entry)
                attempts.setdefault(attempt.pop('request_id'), []).append(attempt)
        result = dict(row)
        native = result.pop('native_json')
        try:
            native = json.loads(native) if native else {}
            result['channel'] = native.get('channel') if isinstance(native, dict) else None
            result['model'] = native.get('model') if isinstance(native, dict) and isinstance(native.get('model'), str) else None
        except (TypeError, ValueError):
            result['model'] = None
        result['model_unconfirmed'] = bool(result['blocked'] and not result['model'] and attempted)
        if result['model_unconfirmed']:
            result['model'] = attempted['model']
        result['turns'] = []
        for row in turns:
            turn=dict(row)
            saved=attempts.get(turn['request_id'], [])
            turn['reauthorization']=None if result['hidden'] else self.official.reauth.view(turn,saved,turn['reauth_state'])
            for field in ('official_reason','reauth_state','finished','block_reason','block_created'):
                del turn[field]
            turn['attachments']=json.loads(turn['attachments'])
            result['turns'].append(turn)
        for turn in result['turns']:
            turn['attempts'] = attempts.get(turn['request_id'], [])
            turn['images'] = ['/api/gemini-images/'+turn['request_id']+'/'+str(r['position']) for r in images if r['request_id']==turn['request_id']]
            turn['image_status'] = image_status.get(turn['request_id'])
            if turn['status'] != 'unknown':
                continue
            try:
                request_id = str(uuid.UUID(turn['request_id']))
                path = self.state_dir/'reconciliation-audit'/(request_id+'.json')
                if (path.is_symlink() or path.parent.is_symlink() or not path.is_file()
                        or path.stat().st_size > 1024*1024):
                    continue
                audit = json.loads(path.read_text(encoding='utf-8'))
                if (audit.get('request_id') == request_id and audit.get('account_key') == result['account_key']
                        and audit.get('state') == 'audited' and audit.get('result',{}).get('recovery') == 'no_matching_history'):
                    turn['reconciliation_note'] = '已核对原账号的近期历史，未找到可对应的回复；仍保留未确认，不会重新发送。'
            except (ValueError, TypeError, AttributeError, OSError):
                pass
        return result

    def create_conversation(self, title):
        if not isinstance(title, str) or len(title.strip()) > 120:
            raise ValueError('对话名称不能超过 120 字。')
        now, ident = time.time(), str(uuid.uuid4())
        with self.connection() as db:
            db.execute('INSERT INTO conversations(id,title,created,updated) VALUES(?,?,?,?)', (ident, title.strip() or '新对话', now, now))
        return self.conversation(ident)

    def rename_conversation(self, ident, title):
        ident = self.conversation_id(ident)
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
            raise ValueError('请输入不超过 120 字的对话名称。')
        with self.connection() as db:
            if db.execute('UPDATE conversations SET title=?,updated=? WHERE id=?', (title.strip(), time.time(), ident)).rowcount != 1:
                raise ValueError('对话不存在。')
        return self.conversation(ident)

    def hide_conversation(self, ident, hidden):
        ident = self.conversation_id(ident)
        if type(hidden) is not bool:
            raise ValueError('对话状态不正确。')
        with self.connection() as db:
            if db.execute('UPDATE conversations SET hidden=?,updated=? WHERE id=?', (int(hidden), time.time(), ident)).rowcount != 1:
                raise ValueError('对话不存在。')
        return self.conversation(ident)

    @staticmethod
    def material_id(ident):
        if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{64}', ident):
            raise ValueError('素材编号不正确。')
        return ident

    def material_extra(self, ident):
        self.material_id(ident)
        with self.connection() as db:
            row = db.execute('SELECT manual_json,suggested_json FROM material_extra WHERE asset_id=?', (ident,)).fetchone()
            latest = db.execute('SELECT status,note,created FROM material_analyses WHERE asset_id=? ORDER BY created DESC LIMIT 1', (ident,)).fetchone()
            shot = db.execute('SELECT notes_json,source FROM material_shot_notes WHERE asset_id=?', (ident,)).fetchone()
            shot_job = db.execute('SELECT request_id,status,note FROM material_shot_jobs WHERE asset_id=?', (ident,)).fetchone()
            automatic = db.execute('SELECT status,note,updated FROM auto_material_jobs WHERE asset_id=?', (ident,)).fetchone()
            if not automatic and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='material_backfill_jobs'").fetchone():
                automatic = db.execute('SELECT status,note,updated FROM material_backfill_jobs WHERE asset_id=?', (ident,)).fetchone()
        return {'manual': json.loads(row['manual_json']) if row else {},
                'suggested': json.loads(row['suggested_json']) if row else {},
                'shot_notes': json.loads(shot['notes_json']) if shot else {},
                'shot_notes_source': shot['source'] if shot else None,
                'shot_job': dict(shot_job) if shot_job else None,
                'analysis': dict(latest) if latest else None,
                'automatic': dict(automatic) if automatic else None}

    def material_card_fields(self, ids):
        ids = list(dict.fromkeys(self.material_id(ident) for ident in ids))
        if not ids or len(ids) > 100:
            return {}
        fields = ('viewpoint', 'composition', 'movement', 'shot_size')
        with self.connection() as db:
            rows = db.execute('''SELECT e.asset_id,e.manual_json,e.suggested_json,
                (SELECT a.status FROM material_analyses a WHERE a.asset_id=e.asset_id ORDER BY a.created DESC LIMIT 1) AS status
                FROM material_extra e WHERE e.asset_id IN (''' + ','.join('?' for _ in ids) + ')', ids).fetchall()
        result = {}
        for row in rows:
            try:
                manual = json.loads(row['manual_json']); suggested = json.loads(row['suggested_json']) if row['status']=='completed' else {}
                result[row['asset_id']] = {k: manual.get(k) or suggested.get(k) for k in fields if manual.get(k) or suggested.get(k)}
            except (ValueError, TypeError, AttributeError):
                continue
        return result

    def material_analysis_history(self, page=1, limit=20, ident=None):
        """Read saved analysis attempts and their original request identities."""
        if type(page) is not int or page < 1 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('分析记录页码不正确。')
        if ident is not None:
            self.material_id(ident)
        where = 'WHERE a.asset_id=?' if ident else ''
        params = (ident,) if ident else ()
        with self.connection() as db:
            total = db.execute('SELECT count(*) FROM material_analyses a ' + where, params).fetchone()[0]
            rows = db.execute('''SELECT a.request_id,a.asset_id,a.status,a.note,a.created,a.finished,
                                r.account_key,r.status AS request_status,r.selection,r.response,
                                CASE WHEN b.request_id IS NOT NULL THEN 'backfill'
                                     WHEN j.request_id IS NOT NULL THEN 'automatic' ELSE 'manual' END AS origin
                                FROM material_analyses a
                                LEFT JOIN requests r ON r.request_id=a.request_id
                                LEFT JOIN material_backfill_jobs b ON b.request_id=a.request_id
                                LEFT JOIN auto_material_jobs j ON j.request_id=a.request_id ''' + where +
                              ' ORDER BY a.created DESC,a.request_id DESC LIMIT ? OFFSET ?',
                              params + (limit, (page-1)*limit)).fetchall()
        allowed = {'subject','scene','action','style','camera','color','keywords','shot_size','viewpoint','composition','movement'}
        items = []
        for row in rows:
            entry = dict(row)
            text = entry.pop('response') or ''
            entry['result_tags'] = None
            if entry['request_status'] == 'completed' and isinstance(text, str):
                text = text.strip()
                blocks = re.findall(r'```(?:json)?\s*([\s\S]*?)```', text, flags=re.I)
                if len(blocks) == 1:
                    text = blocks[0].strip()
                try:
                    value = json.loads(text)
                    if (isinstance(value, dict) and value and not (set(value)-allowed)
                            and all(isinstance(v, list) and len(v) <= 30 and all(isinstance(x, str) and x.strip() and len(x.strip()) <= 100 for x in v) for v in value.values())):
                        entry['result_tags'] = {k:list(dict.fromkeys(x.strip() for x in v)) for k,v in value.items()}
                except (ValueError, TypeError):
                    pass
            items.append(entry)
        return {'items': items, 'page': page, 'pages': max(1, (total+limit-1)//limit), 'total': total}

    def auto_material_state(self):
        with self.connection() as db:
            settings = db.execute('SELECT enabled,since,paused_reason FROM auto_material_settings WHERE id=1').fetchone()
            counts = {row['status']: row['n'] for row in db.execute('SELECT status,count(*) AS n FROM auto_material_jobs GROUP BY status')}
        return {'enabled': bool(settings['enabled']), 'since': settings['since'],
                'paused_reason': settings['paused_reason'] or ('官方分析账号未就绪；已入库素材保留。' if not self.official.ready else None),
                'counts': counts}

    def set_auto_material_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('自动分析设置不正确。')
        now = time.time()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT enabled FROM auto_material_settings WHERE id=1').fetchone()
            if enabled and not row['enabled']:
                db.execute('UPDATE auto_material_settings SET enabled=1,since=?,paused_reason=NULL WHERE id=1', (now,))
            elif enabled:
                db.execute('UPDATE auto_material_settings SET paused_reason=NULL WHERE id=1')
            else:
                db.execute('UPDATE auto_material_settings SET enabled=0,paused_reason=NULL WHERE id=1')
                db.execute("UPDATE auto_material_jobs SET status='skipped',updated=?,note='自动分析已关闭，未提交' WHERE status='waiting'", (now,))
            db.execute('COMMIT')
        return self.auto_material_state()

    def discover_auto_materials(self, items):
        from material_backfill import has_labels
        if not isinstance(items, list):
            raise ValueError('素材列表格式不正确。')
        supported = {'image/jpeg','image/png','image/webp','video/mp4','video/webm'}
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            settings = db.execute('SELECT enabled,since FROM auto_material_settings WHERE id=1').fetchone()
            if not settings['enabled'] or settings['since'] is None:
                db.execute('COMMIT')
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                ident, added = item.get('id'), item.get('added')
                if (not isinstance(ident, str) or not ACCOUNT.fullmatch(ident)
                        or type(added) not in (int, float) or not math.isfinite(added) or added < settings['since']):
                    continue
                size, mime, kind = item.get('size'), item.get('mime'), item.get('media_type')
                valid = (type(size) is int and 0 < size <= UPLOAD_LIMIT and mime in supported
                         and ((kind == 'image' and mime.startswith('image/')) or (kind == 'video' and mime.startswith('video/'))))
                previous = db.execute('SELECT 1 FROM material_analyses WHERE asset_id=? LIMIT 1', (ident,)).fetchone()
                backfilled = db.execute('SELECT 1 FROM material_backfill_jobs WHERE asset_id=? LIMIT 1', (ident,)).fetchone()
                tagged = has_labels(item.get('tags')) or has_labels(item.get('structured_tags'))
                state = 'waiting' if valid and not previous and not backfilled and not tagged else 'skipped'
                note = '等待自动分析' if state == 'waiting' else '已有标签或分析记录，或素材格式、大小不支持'
                db.execute('INSERT OR IGNORE INTO auto_material_jobs(asset_id,status,added,updated,note) VALUES(?,?,?,?,?)',
                           (ident, state, added, time.time(), note))
            db.execute('COMMIT')

    def finish_auto_material(self, request):
        self.conversation_id(request)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,note FROM material_analyses WHERE request_id=?', (request,)).fetchone()
            analysis_status = row['status'] if row else 'unknown'
            if analysis_status in ('unknown', 'preparing', 'saving'):
                status = 'unknown'
            else:
                status = 'completed' if analysis_status == 'completed' else 'failed'
            note = row['note'] if row and row['note'] else ('已分析' if status == 'completed' else '分析结果需核对')
            changed = db.execute(
                "UPDATE auto_material_jobs SET status=?,updated=?,note=? WHERE request_id=? AND status='running'",
                (status, time.time(), note, request),
            ).rowcount
            # A received reply with invalid labels needs review for this asset only.
            if changed and status != 'completed' and analysis_status != 'invalid':
                db.execute("UPDATE auto_material_settings SET paused_reason='有素材分析未完成，请核对后再继续；不会自动重试' WHERE id=1")
            db.execute('COMMIT')
        return status

    def save_material_extra(self, ident, values):
        self.material_id(ident)
        allowed = {'shot_size', 'viewpoint', 'composition', 'movement'}
        if not isinstance(values, dict) or set(values) - allowed:
            raise ValueError('素材分析维度不正确。')
        cleaned = {}
        for key, labels in values.items():
            if not isinstance(labels, list) or len(labels) > 30 or any(not isinstance(x, str) or not x.strip() or len(x.strip()) > 100 for x in labels):
                raise ValueError('每个维度最多 30 个标签，每个最多 100 字。')
            cleaned[key] = list(dict.fromkeys(x.strip() for x in labels))
        with self.connection() as db:
            db.execute("""INSERT INTO material_extra(asset_id,manual_json,updated) VALUES(?,?,?)
                       ON CONFLICT(asset_id) DO UPDATE SET manual_json=excluded.manual_json,updated=excluded.updated""",
                       (ident, json.dumps(cleaned, ensure_ascii=False), time.time()))
        return self.material_extra(ident)

    def search_material_extra(self, query, page):
        if not isinstance(query, str) or len(query) > 200 or type(page) is not int or not 1 <= page <= 100000:
            raise ValueError('搜索条件不正确。')
        query, limit = query.strip(), 12
        with self.connection() as db:
            where = 'WHERE instr(lower(manual_json),lower(?))>0 OR instr(lower(suggested_json),lower(?))>0'
            total = db.execute('SELECT count(*) FROM material_extra ' + where, (query, query)).fetchone()[0]
            rows = db.execute('SELECT asset_id FROM material_extra ' + where + ' ORDER BY updated DESC,asset_id LIMIT ? OFFSET ?',
                              (query, query, limit, (page - 1) * limit)).fetchall()
        return {'ids': [row['asset_id'] for row in rows], 'total': total, 'page': page, 'pages': max(1, (total + limit - 1)//limit)}

    def finish_material_analysis(self, ident, request_id):
        from material_vision import clean_shot_notes
        self.material_id(ident)
        record = self.get(request_id)
        status, note, suggested, shot_notes = 'failed', '分析未提交，请检查附件和账号状态。', None, {}
        if record and record['status'] == 'completed':
            note, status = '已收到 Gemini 分析。', 'completed'
            text = record.get('response') or ''
            text = text.strip()
            # Gemini sometimes adds an explanation after a fenced JSON answer.
            # Accept one complete fenced block; do not guess between multiple blocks.
            blocks = re.findall(r'```(?:json)?\s*([\s\S]*?)```', text, flags=re.I)
            if len(blocks) == 1:
                text = blocks[0].strip()
            try:
                value = json.loads(text)
                notes = value.pop('shot_notes', None) if isinstance(value, dict) else None
                shot_notes = clean_shot_notes(notes)
                allowed = {'subject','scene','action','style','camera','color','keywords','shot_size','viewpoint','composition','movement'}
                if (not isinstance(value, dict) or set(value) - allowed or not value
                        or any(not isinstance(v, list) or len(v) > 30 or any(not isinstance(x, str) or not x.strip() or len(x.strip()) > 100 for x in v) for v in value.values())):
                    raise ValueError()
                suggested = {k:list(dict.fromkeys(x.strip() for x in v)) for k,v in value.items()}
                status, note = 'saving', 'Gemini 分析已完成，正在登记标签。'
            except (ValueError, TypeError):
                status, note = 'invalid', '已收到回复，但格式无法识别为标签；未覆盖已有标签。'
        elif record and record['status'] in ('unknown', 'dispatching'):
            status, note = 'unknown', '分析结果未确认，系统不会自动重复提交。'
        elif record and record['note']:
            note = record['note']
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('UPDATE material_analyses SET status=?,finished=?,note=? WHERE request_id=? AND asset_id=? AND status=?',
                                 (status, time.time(), note, request_id, ident, 'preparing')).rowcount
            if changed and suggested is not None:
                db.execute("""INSERT INTO material_extra(asset_id,suggested_json,updated) VALUES(?,?,?)
                           ON CONFLICT(asset_id) DO UPDATE SET suggested_json=excluded.suggested_json,updated=excluded.updated""",
                           (ident, json.dumps(suggested, ensure_ascii=False), time.time()))
                if shot_notes:
                    db.execute("INSERT OR IGNORE INTO material_shot_notes(asset_id,notes_json,source) VALUES(?,?,'gemini')",
                               (ident, json.dumps(shot_notes, ensure_ascii=False)))
            db.execute('COMMIT')
        return self.material_extra(ident)

    def material_analysis_note(self, request_id, note, status='completed'):
        self.conversation_id(request_id)
        if not isinstance(note, str) or len(note) > 300 or status not in ('completed', 'unknown'):
            raise ValueError('分析记录格式不正确。')
        with self.connection() as db:
            db.execute("UPDATE material_analyses SET status=?,note=?,finished=? WHERE request_id=? AND status='saving'", (status, note, time.time(), request_id))

    def get_flow(self, request_id):
        with self.connection() as db:
            row = db.execute('SELECT request_id,project_key,profile,project,kind,status,created,finished,task_id,note FROM flow_requests WHERE request_id=?', (request_id,)).fetchone()
        return dict(row) if row else None

    def upload(self, request_id, index, extension, stream, length):
        with self.upload_lock:
            return self._upload(request_id, index, extension, stream, length)

    def _upload(self, request_id, index, extension, stream, length):
        try:
            if str(uuid.UUID(request_id)) != request_id or type(index) is not int or not 0 <= index < 4:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError('附件编号不正确。') from None
        if extension not in UPLOAD_TYPES or type(length) is not int or not 0 < length <= UPLOAD_LIMIT:
            raise ValueError('附件格式或大小不符合要求。')
        if self.maintenance():
            raise Conflict('工作台正在更新。')
        with self.connection() as db:
            if db.execute('SELECT 1 FROM requests WHERE request_id=?', (request_id,)).fetchone():
                raise Conflict('该请求已经发送，不能再添加附件。')
        root = self.state_dir / 'uploads'
        directory = root / request_id
        for path in (root, directory):
            if path.is_symlink():
                raise ValueError('附件暂存目录不安全。')
            path.mkdir(mode=0o700 if os.name == 'posix' else 0o777, exist_ok=True)
        files = list(directory.iterdir())
        if any(p.is_symlink() or not p.is_file() or not re.fullmatch(r'[0-3]\.(jpg|jpeg|png|webp|mp4|mov|webm|pdf)', p.name) for p in files):
            raise ValueError('附件暂存目录不安全。')
        if any(p.name.split('.')[0] == str(index) for p in files) or sum(p.stat().st_size for p in files) + length > UPLOAD_LIMIT:
            raise ValueError('附件数量重复或总大小超过 100 MB。')
        path = directory / (str(index) + '.' + extension)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        digest, first, remaining = hashlib.sha256(), b'', length
        try:
            with os.fdopen(fd, 'wb') as output:
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError('附件传输不完整。')
                    if len(first) < 32:
                        first = (first + chunk)[:32]
                    output.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                output.flush()
            if not valid_upload_prefix(extension, first):
                raise ValueError('文件内容与所选格式不符。')
            return {'index': index, 'extension': extension, 'sha256': digest.hexdigest()}
        except BaseException:
            if path.is_file() and not path.is_symlink():
                path.unlink()
            raise

    def upload_files(self, request_id, attachments):
        directory = self.state_dir / 'uploads' / request_id
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('附件尚未上传完成。（诊断：目录 ' + str(directory) + ' 不存在）')
        result, total = [], 0
        for item in attachments:
            path = directory / (str(item['index']) + '.' + item['extension'])
            if path.is_symlink() or not path.is_file():
                raise ValueError('附件尚未上传完成。（诊断：文件 ' + str(path.name) + ' 缺失）')
            total += path.stat().st_size
            if total > UPLOAD_LIMIT:
                raise ValueError('附件总大小超过 100 MB。')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                first = stream.read(32)
                if not valid_upload_prefix(item['extension'], first):
                    raise ValueError('附件内容不符合要求。')
                digest.update(first)
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != item['sha256']:
                raise ValueError('附件传输校验失败。')
            result.append(path)
        return result

    def purge_stale_uploads(self):
        root = self.state_dir / 'uploads'
        if root.is_symlink() or not root.is_dir():
            return
        with self.upload_lock:
            for directory in root.iterdir():
                if (directory.is_symlink() or not directory.is_dir()
                        or not re.fullmatch(r'[0-9a-f-]{36}', directory.name)
                        or time.time() - directory.stat().st_mtime < 24 * 3600):
                    continue
                with self.connection() as db:
                    active = db.execute("SELECT 1 FROM requests WHERE request_id=? AND status='dispatching'", (directory.name,)).fetchone()
                files = list(directory.iterdir())
                if active or any(path.is_symlink() or not path.is_file() or
                                 not re.fullmatch(r'[0-3]\.(jpg|jpeg|png|webp|mp4|mov|webm|pdf)', path.name) for path in files):
                    continue
                for path in files:
                    path.unlink()
                directory.rmdir()

    def annotate_tasks(self, tasks):
        with self.connection() as db:
            recoveries = {row['task_id']: dict(row) for row in db.execute('SELECT * FROM flow_recoveries')}
        for task in tasks:
            row = recoveries.get(task.get('source_id'))
            if not row:
                continue
            if row['status'] == 'recovered' and self.recovered_file(row['task_id']):
                task['note'] = '原任务下载失败；图片已找回，不需要重新生成。'
                task['recovered_path'] = '/api/real-pool/recoveries/' + row['task_id'] + '/file'
                task['recoverable'] = False
            elif row['status'] == 'recovering':
                task['note'] = '正在找回已生成的图片。'
                task['recoverable'] = False
            elif row['status'] in ('failed', 'unknown'):
                task['note'] = '图片未能找回；可以再次尝试下载已有结果，不会重新生成。'

    def auto_recover_tasks(self, tasks):
        """Make one download-only recovery attempt for this workbench's new tasks."""
        with self.connection() as db:
            pending = {row['task_id'] for row in db.execute('''SELECT f.task_id FROM flow_requests f
                WHERE f.status='submitted' AND f.task_id IS NOT NULL
                  AND NOT EXISTS(SELECT 1 FROM flow_recoveries r WHERE r.task_id=f.task_id)''')}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            ident = task.get('source_id')
            if task.get('recoverable') is True and ident in pending:
                try:
                    return self.recover_image(ident)
                except ValueError:
                    with self.connection() as db:
                        db.execute("INSERT OR IGNORE INTO flow_recoveries(task_id,status,updated) VALUES(?,'failed',?)", (ident, time.time()))
                    return {'status': 'failed'}
        return None

    def recovered_file(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task_id):
            return None
        with self.connection() as db:
            row = db.execute("SELECT filename,sha256,size FROM flow_recoveries WHERE task_id=? AND status='recovered'", (task_id,)).fetchone()
        if not row or not re.fullmatch(re.escape(task_id) + r'\.(jpg|png|webp)', row['filename'] or ''):
            return None
        directory = self.state_dir / 'recovered-images'
        path = directory / row['filename']
        if directory.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size != row['size']:
            return None
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            return None
        return data, {'jpg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp'}[path.suffix[1:]]

    def recover_image(self, task_id, source_root=None, fetch=None):
        """Retrieve one already-generated image only; never call a generation endpoint."""
        if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task_id):
            raise ValueError('任务编号不正确。')
        if self.recovered_file(task_id):
            return {'status': 'recovered'}
        root = Path(source_root) if source_root else Path.home() / '.local/share/studiowright-evaluation'
        def read_db(path):
            if path.is_symlink() or not path.is_file():
                raise ValueError('无法读取原任务记录。')
            db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            return closing(db)
        with read_db(root / 'console-data/console.db') as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='flow_task_execution_rows' AND type='view'").fetchone():
                task = db.execute('SELECT * FROM flow_task_execution_rows WHERE root_id=? ORDER BY ordinal DESC LIMIT 1', (task_id,)).fetchone()
            else:
                task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not task or task['status'] != 'failed' or task['kind'] != 'image':
                raise ValueError('该任务不符合图片找回条件。')
            try:
                jobs = json.loads(task['result'] or '{}').get('source_jobs', [])
                links = [j for j in jobs if isinstance(j, dict) and j.get('stage') == 'generated'
                         and isinstance(j.get('media_ids'), list) and len(j['media_ids']) == 1 and not j.get('files')]
                if len(links) != 1 or not isinstance(links[0].get('job_id'), str):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError('原任务没有唯一可找回的图片。') from None
        with read_db(root / 'data/studiowright.db') as db:
            job = db.execute('SELECT * FROM jobs WHERE id=?', (links[0]['job_id'],)).fetchone()
            if not job or job['profile'] != task['profile'] or job['project_id'] != task['project']:
                raise ValueError('原任务与项目无法匹配。')
            urls = re.findall(r'https://[^\s<>"\)]+', job['error'] or '')
        if len(urls) != 1:
            raise ValueError('原任务没有唯一下载地址。')
        parts = urlparse(urls[0])
        if parts.scheme != 'https' or parts.hostname != 'flow-content.google' or parts.port not in (None, 443) or parts.username or parts.password:
            raise ValueError('原任务下载地址不符合要求。')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM flow_recoveries WHERE task_id=?', (task_id,)).fetchone()
            if self.maintenance() or (row and row['status'] == 'recovering'):
                db.execute('ROLLBACK')
                return {'status': 'recovering'}
            db.execute("INSERT INTO flow_recoveries(task_id,status,updated) VALUES(?,'recovering',?) ON CONFLICT(task_id) DO UPDATE SET status='recovering',updated=excluded.updated", (task_id, time.time()))
            db.execute('COMMIT')
        try:
            if fetch:
                data = fetch(urls[0])
            else:
                class NoRedirect(HTTPRedirectHandler):
                    def redirect_request(self, req, fp, code, msg, headers, newurl):
                        return None
                request = Request(urls[0], headers={'Accept': '*/*'}, method='GET')
                with build_opener(NoRedirect()).open(request, timeout=30) as response:
                    data = response.read(30 * 1024 * 1024 + 1)
            if not isinstance(data, bytes) or len(data) > 30 * 1024 * 1024:
                raise ValueError()
            if data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9'):
                suffix = 'jpg'
            elif data.startswith(b'\x89PNG\r\n\x1a\n') and data.endswith(b'IEND\xaeB`\x82'):
                suffix = 'png'
            elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
                suffix = 'webp'
            else:
                raise ValueError()
            directory = self.state_dir / 'recovered-images'
            if directory.is_symlink():
                raise ValueError()
            directory.mkdir(mode=0o700 if os.name == 'posix' else 0o777, exist_ok=True)
            filename = task_id + '.' + suffix
            path = directory / filename
            digest = hashlib.sha256(data).hexdigest()
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError()
            else:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                with os.fdopen(fd, 'wb') as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
            with self.connection() as db:
                db.execute("UPDATE flow_recoveries SET status='recovered',filename=?,sha256=?,size=?,updated=? WHERE task_id=? AND status='recovering'", (filename, digest, len(data), time.time(), task_id))
            return {'status': 'recovered'}
        except Exception:
            with self.connection() as db:
                db.execute("UPDATE flow_recoveries SET status='failed',updated=? WHERE task_id=? AND status='recovering'", (time.time(), task_id))
            return {'status': 'failed'}

    @staticmethod
    def _send_flow(body):
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        encoded = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        path = '/api/tasks/with-references' if 'mode' in body['params'] else '/api/tasks'
        request = Request('http://127.0.0.1:18888' + path, data=encoded, method='POST',
                          headers={'Content-Type': 'application/json', 'X-Console-Request': '1'})
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as response:
            if response.status != 202:
                raise ValueError('unexpected status')
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('response too large')
        return json.loads(raw)

    @staticmethod
    def flow_project_key(profile, project):
        encoded = json.dumps(['flow', profile, project], ensure_ascii=False, separators=(',', ':')).encode()
        return 'flow-project-' + hashlib.sha256(encoded).hexdigest()

    def _create_flow_project(self, profile, *, readiness=False):
        pid = subprocess.check_output(['systemctl', '--user', 'show', 'studiowright-console.service',
                                       '--property=MainPID', '--value'], text=True, timeout=5).strip()
        if not pid.isdigit() or pid == '0':
            raise RuntimeError('Flow 后台不可用。')
        python = os.fsdecode(Path('/proc', pid, 'cmdline').read_bytes().split(b'\0')[0])
        process = subprocess.run([python, '-B', str(self.program_dir / 'create_flow_project.py')],
                                 input=json.dumps({'profile': profile, 'readiness': readiness}).encode(),
                                 capture_output=True, timeout=100)
        if process.returncode or len(process.stdout) > 4096:
            raise RuntimeError('创建项目的结果未确认。')
        result = json.loads(process.stdout)
        if isinstance(result, dict) and result.get('result') == 'not_started':
            raise ProjectNotStarted()
        project = result.get('project') if isinstance(result, dict) else None
        if not isinstance(project, str) or str(uuid.UUID(project)) != project:
            raise RuntimeError('创建项目的结果未确认。')
        return project

    def _register_flow_project(self, profile, project, email):
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        if not isinstance(email, str) or email.count('@') != 1 or not all(email.strip().split('@')):
            raise RuntimeError('项目所属账号的邮箱未确认。')
        title = 'FLOW+' + email.strip().lower().split('@', 1)[0]
        body = json.dumps({'profile': profile, 'id': project, 'title': title}).encode()
        request = Request('http://127.0.0.1:18888/api/projects', data=body, method='POST',
                          headers={'Content-Type': 'application/json', 'X-Console-Request': '1',
                                   'Origin': 'http://127.0.0.1:18888'})
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as response:
            result = json.loads(response.read(8193))
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise RuntimeError('项目登记未确认。')

    @staticmethod
    def _flow_window_blocked(snapshot):
        coordination = snapshot.get('flow_coordination')
        windows = (coordination.get('windows') or []) if isinstance(coordination, dict) else []
        return {w['profile'] for w in windows if isinstance(w, dict) and isinstance(w.get('profile'), str)
                and w.get('state') in ('result_unknown', 'needs_verification')}

    @staticmethod
    def _flow_dispatch_accounts(snapshot):
        blocked = RealPool._flow_window_blocked(snapshot)
        return {a['profile']: a for a in snapshot.get('real_accounts', {}).get('items', [])
                if isinstance(a, dict) and isinstance(a.get('profile'), str) and a.get('flow_login') == 'verified'
                and not a.get('flow_removed')
                and not a.get('flow_page_incident') and not a.get('flow_preflight_reasons')
                and a.get('flow_health') == 'ready' and a.get('flow_risk_known') is True
                and a.get('flow_risk_paused') is False and a.get('flow_pacing_known') is True
                and a.get('flow_cooling') is False and a['profile'] not in blocked}

    def _auto_flow_project(self, snapshot, request_id, intent, *, named_only=False):
        if not snapshot.get('flow_state', {}).get('available') or not snapshot.get('real_accounts', {}).get('available'):
            raise Conflict('Flow 账号或项目列表暂不可用，无法自动安排。')
        unique = self._flow_dispatch_accounts(snapshot)
        if not unique:
            raise Conflict('当前没有可自动派发的 Flow 账号。')
        with self.connection() as db:
            reservation = db.execute('SELECT * FROM flow_auto_projects WHERE request_id=?', (request_id,)).fetchone()
            last = {r['profile']: r['used'] for r in db.execute('SELECT profile,MAX(created) AS used FROM flow_requests GROUP BY profile')}
        if reservation:
            if reservation['intent'] != intent:
                raise Conflict('该请求编号已用于其他任务。')
            profile = reservation['profile']
            if reservation['status'] != 'created' or profile not in unique:
                raise Conflict('项目创建结果待核对，未再次创建或提交任务。')
            project_id = reservation['project']
            try:
                self._register_flow_project(profile, project_id, unique[profile].get('email'))
            except Exception:
                raise Conflict('项目已创建，但登记结果待核对；不会重复创建。') from None
            return {'key': self.flow_project_key(profile, project_id), 'source_profile': profile, 'source_id': project_id}
        pending = {profile: sum(1 for t in snapshot.get('tasks', []) if t.get('source_profile') == profile
                                and t.get('status') in ('queued', 'running')) for profile in unique}

        def account_priority(profile):
            account = unique[profile]
            remaining = account.get('flow_daily_credits_remaining')
            has_daily_credits = (account.get('flow_balance_fresh') is True
                                 and type(remaining) is int and remaining > 0)
            return pending[profile], not has_daily_credits, last.get(profile) or 0, profile

        projects = snapshot.get('projects', [])
        # Choose the account first; lack of a project must not skip its turn.
        profile = min(unique, key=account_priority)
        owned = [p for p in projects if p.get('source_profile') == profile]
        if named_only:
            from flow_references import normalized_owner
            title = 'FLOW+' + normalized_owner(unique[profile].get('email')).split('@', 1)[0]
            owned = [p for p in owned if p.get('title') == title]
        rows = [p for p in owned if isinstance(p.get('key'), str)
                and isinstance(p.get('source_id'), str)
                and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', p['source_id'])
                and sum(q.get('key') == p['key'] for q in projects) == 1]
        if rows:
            preferred = snapshot.get('retry_projects', {}).get(profile)
            project_load = {p['key']: sum(1 for t in snapshot.get('tasks', [])
                              if t.get('project_key') == p['key'] and t.get('status') in ('queued', 'running'))
                            for p in rows}
            rows.sort(key=lambda p: (project_load[p['key']], p['key'] != preferred,
                                     -(p.get('checked') or 0), p['source_id']))
            return rows[0]
        if owned:
            raise Conflict('此账号现有项目的归属或编号无法确认，暂不新建或改用其他账号。')
        return self._provision_flow_project(profile, unique[profile].get('email'), request_id, intent)

    def prepare_readiness_project(self, account):
        profile, email = account['profile'], account['email']
        with self.connection() as db:
            previous = db.execute('SELECT status,project FROM flow_auto_projects WHERE profile=?', (profile,)).fetchone()
        if previous:
            if previous['status'] != 'created':
                raise Conflict('项目创建结果待核对。')
            project_id = previous['project']
            try:
                self._register_flow_project(profile, project_id, email)
            except (OSError, ValueError, RuntimeError):
                raise Conflict('项目已创建，登记结果待核对。') from None
            return project_id
        request_id = 'readiness-' + hashlib.sha256(profile.encode()).hexdigest()
        return self._provision_flow_project(profile, email, request_id, request_id, readiness=True)['source_id']

    def _provision_flow_project(self, profile, email, request_id, intent, *, readiness=False):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.maintenance():
                db.execute('ROLLBACK')
                raise Conflict('工作台正在更新。')
            existing = db.execute('SELECT * FROM flow_auto_projects WHERE profile=? OR request_id=?', (profile, request_id)).fetchone()
            if existing:
                db.execute('ROLLBACK')
                raise Conflict('该账号已有项目创建记录待核对，未重复创建。')
            db.execute("INSERT INTO flow_auto_projects(profile,request_id,intent,status,updated) VALUES(?,?,?,'creating',?)",
                       (profile, request_id, intent, time.time()))
            db.execute('COMMIT')
        try:
            project_id = self._create_flow_project(profile, readiness=True) if readiness else self._create_flow_project(profile)
        except ProjectNotStarted:
            with self.connection() as db:
                db.execute("DELETE FROM flow_auto_projects WHERE request_id=? AND status='creating'", (request_id,))
            raise Conflict('账号窗口暂不可用，未创建项目；可以稍后再提交。') from None
        except Exception:
            with self.connection() as db:
                db.execute("UPDATE flow_auto_projects SET status='unknown',updated=? WHERE request_id=?", (time.time(), request_id))
            raise Conflict('项目创建结果待核对，未再次创建或提交任务。') from None
        with self.connection() as db:
            db.execute("UPDATE flow_auto_projects SET status='created',project=?,updated=? WHERE request_id=?",
                       (project_id, time.time(), request_id))
        try:
            self._register_flow_project(profile, project_id, email)
        except Exception:
            raise Conflict('项目已创建，但登记结果待核对；不会重复创建。') from None
        return {'key': self.flow_project_key(profile, project_id), 'source_profile': profile, 'source_id': project_id}

    @staticmethod
    def reference_scope(snapshot, project_key):
        from flow_references import normalized_owner
        projects = [p for p in snapshot.get('projects', []) if p.get('key') == project_key]
        if len(projects) != 1:
            raise Conflict('参考图需要指定已登记的完整项目编号。')
        profile, project = projects[0]['source_profile'], projects[0]['source_id']
        accounts = [a for a in snapshot.get('real_accounts', {}).get('items', []) if a.get('profile') == profile]
        if len(accounts) != 1 or accounts[0].get('flow_login') != 'verified':
            raise Conflict('该项目所属账号的 Flow 登录未确认。')
        return profile, project, normalized_owner(accounts[0].get('email'))

    def upload_flow_reference(self, snapshot, project_key, ident, ext, data, *, request_id=None):
        from flow_references import ReferenceStore
        if self.maintenance():
            raise Conflict('工作台正在更新。')
        if project_key == 'auto':
            with self.upload_lock:
                expires, created = ReferenceStore(self.state_dir).put_auto(request_id, ident, ext, data)
            return {'reference_id': ident, 'request_id': request_id, 'expires_at': expires}, created
        profile, project, owner = self.reference_scope(snapshot, project_key)
        with self.upload_lock:
            expires, created = ReferenceStore(self.state_dir).put(ident, profile, project, owner, ext, data)
        return {'reference_id': ident, 'project_key': project_key, 'expires_at': expires}, created

    def _bind_auto_flow_references(self, store, snapshot, request_id, ids, allow_video, params, kind):
        from flow_references import normalized_owner
        lock = self.flow_reference_locks[int(uuid.UUID(request_id)) % len(self.flow_reference_locks)]
        with lock:
            if self.maintenance():
                raise Conflict('工作台正在更新。')
            scope = store.auto_scope(ids, request_id, allow_video=allow_video)
            if scope:
                profile, project_id, owner = scope
                account = self._flow_dispatch_accounts(snapshot).get(profile)
                key = self.flow_project_key(profile, project_id)
                if (not snapshot.get('flow_state', {}).get('available') or not account
                        or normalized_owner(account.get('email')) != owner
                        or self.reference_scope(snapshot, key) != scope):
                    raise Conflict('本次请求已绑定的 Flow 账号或项目暂不可用。')
                return profile, project_id, key
            intent = hashlib.sha256(json.dumps({'kind': kind, 'params': params}, ensure_ascii=False,
                                               sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            selected = self._auto_flow_project(snapshot, request_id, intent, named_only=True)
            profile, project_id = selected['source_profile'], selected['source_id']
            owner = normalized_owner(self._flow_dispatch_accounts(snapshot)[profile].get('email'))
            store.auto_scope(ids, request_id, allow_video=allow_video, bind=(profile, project_id, owner))
            return profile, project_id, selected['key']

    def submit_flow(self, data, request_id, snapshot, send=None, validate_only=False):
        try:
            if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError('请求标识不正确。') from None
        if (not isinstance(data, dict) or set(data) - {'project_key', 'kind', 'params', 'confirm', 'auto_retry'}
                or not {'kind', 'params', 'confirm'} <= set(data) or data['confirm'] is not (not validate_only)):
            raise ValueError('验证请设置 confirm: false；真实提交须设置 confirm: true。')
        kind, params = data['kind'], data['params']
        if kind not in ('image', 'video') or not isinstance(params, dict):
            raise ValueError('任务类型不正确。')
        if isinstance(params.get("model"), str):
            aliases = {"omni-flash-1.1": "omni-flash", "omni-flash-1.2": "omni-flash-2"}
            if params["model"] in aliases:
                params = dict(params, model=aliases[params["model"]])
        retry_omitted = 'auto_retry' not in data
        auto_retry = data.get('auto_retry', params.get('count') == 1)
        if type(auto_retry) is not bool:
            raise ValueError('auto_retry 必须是布尔值。')
        if auto_retry and params.get('count') != 1:
            raise ValueError('换号重试只支持每次生成一张图片或一段视频。')
        required = {'prompt', 'model', 'aspect', 'count', 'resolution', 'duration'}
        from flow_references import REFERENCE_FIELDS, ReferenceStore, task_reference_ids
        if not required <= params.keys() or params.keys() - required - REFERENCE_FIELDS - {'max_credits', 'style_id'}:
            raise ValueError('任务参数不正确。')
        if 'style_id' in params:
            style_id = params['style_id']
            if not isinstance(style_id, str) or not style_id.strip() or len(style_id) > 256 or any(0xD800 <= ord(c) <= 0xDFFF for c in style_id):
                raise ValueError('画风 ID 须为 1～256 个字符的非空字符串。')
        with_references = params.keys() - {'max_credits', 'style_id'} != required
        ids = []
        if with_references:
            ids = task_reference_ids(kind, params)
        elif validate_only:
            raise ValueError('此验证入口用于图生图、Omni 多参考图或首尾帧任务。')
        prompt, model, aspect = params['prompt'], params['model'], params['aspect']
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 10000:
            raise ValueError('请输入不超过 10000 字的提示词。')
        models = snapshot.get('flow_models', {}).get(kind, [])
        if not isinstance(model, str) or sum(1 for m in models if m.get('id') == model) != 1:
            raise ValueError('模型不可用，请刷新后选择。')
        if aspect not in (('16:9', '4:3', '1:1', '3:4', '9:16') if kind == 'image' else ('16:9', '9:16')):
            raise ValueError('画面比例不正确。')
        if (type(params['count']) is not int or params['count'] not in (1, 2, 3, 4)
                or type(params['duration']) is not int or params['duration'] not in (tuple(range(3, 16)) if params.get('model') == 'omni-flash-2' else (4, 6, 8, 10))
                or params['resolution'] not in ('360p', '720p')):
            raise ValueError('数量、清晰度或时长不正确。')
        flow_params = dict(params)
        if kind == 'image':
            flow_params.pop('max_credits', None)
        else:
            # Production runs without a per-task credit cap; the console internals
            # still expect the field, so pin the uncapped default.
            flow_params['max_credits'] = 10000
        # Reject an oversized payload before the optional project-creation side effect.
        estimate = {'profile': 'x' * 64, 'project': '0' * 36, 'kind': kind, 'params': flow_params,
                    'confirm_generation': True, 'request_key': request_id, 'auto_retry': auto_retry}
        if len(json.dumps(estimate, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > 32768:
            raise ValueError('提示词过长，请缩短后提交。')
        project_key = data.get('project_key')
        if project_key is not None and not isinstance(project_key, str):
            raise ValueError('项目标识不正确。')
        auto = project_key in (None, '', 'auto')
        store = ReferenceStore(self.state_dir)
        allow_video = params.get('mode') == 'ingredients'
        if validate_only:
            if auto:
                store.auto_scope(ids, request_id, allow_video=allow_video)
            else:
                profile, project_id, owner = self.reference_scope(snapshot, project_key)
                store.check(ids, profile, project_id, owner, allow_video=allow_video, explicit_only=True)
            return {'valid': True, 'submitted': False, 'project_key': None if auto else project_key, 'kind': kind,
                    'mode': params['mode'], 'reference_count': len(ids)}, False
        with self.connection() as db:
            if db.execute('SELECT 1 FROM requests WHERE request_id=?', (request_id,)).fetchone():
                raise Conflict('该请求编号属于 Gemini，不能在 Flow 提交或重试。')
            prior = db.execute('SELECT * FROM flow_requests WHERE request_id=?', (request_id,)).fetchone()
        if prior:
            if not auto and project_key != prior['project_key']:
                raise Conflict('该请求编号已用于其他任务。')
            profile, project_id, project_key = prior['profile'], prior['project'], prior['project_key']
        elif auto and with_references:
            profile, project_id, project_key = self._bind_auto_flow_references(
                store, snapshot, request_id, ids, allow_video, params, kind)
        elif auto:
            if self.maintenance():
                raise Conflict('工作台正在更新。')
            intent = hashlib.sha256(json.dumps({'kind': kind, 'params': params}, ensure_ascii=False,
                                               sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            project = self._auto_flow_project(snapshot, request_id, intent)
            profile, project_id, project_key = project['source_profile'], project['source_id'], project['key']
        elif with_references:
            profile, project_id, owner = self.reference_scope(snapshot, project_key)
            store.check(ids, profile, project_id, owner, allow_video=allow_video, explicit_only=True)
        else:
            projects = [p for p in snapshot.get('projects', []) if p.get('key') == project_key]
            if len(projects) != 1:
                raise Conflict('项目无法唯一匹配，请刷新项目列表。')
            project = projects[0]
            profile, project_id = project['source_profile'], project['source_id']
            accounts = [a for a in snapshot.get('real_accounts', {}).get('items', []) if a.get('profile') == profile]
            if len(accounts) != 1 or accounts[0].get('flow_login') != 'verified':
                raise Conflict('该项目所属账号的 Flow 登录未确认。')
        if not prior and any(a.get('profile') == profile and a.get('flow_removed')
                             for a in snapshot.get('real_accounts', {}).get('items', [])):
            raise Conflict('这个项目所属账号已从 Flow 号池移除。')
        if not prior and profile in self._flow_window_blocked(snapshot):
            raise Conflict('该账号的 Flow 窗口有旧任务或验证待核对，本次未入队。')
        body = {'profile': profile, 'project': project_id, 'kind': kind, 'params': flow_params,
                'confirm_generation': True, 'request_key': request_id, 'auto_retry': auto_retry}
        if len(json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > 32768:
            raise ValueError('提示词过长，请缩短后提交。')
        def request_digest(payload):
            return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        digest = request_digest(body)
        # A replay of an older request with an omitted flag keeps its stored policy.
        legacy_digest = request_digest({**body, 'auto_retry': False}) if retry_omitted and auto_retry else digest
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM requests WHERE request_id=?', (request_id,)).fetchone():
                db.execute('ROLLBACK')
                raise Conflict('该请求编号属于 Gemini，不能在 Flow 提交或重试。')
            old = db.execute('SELECT digest FROM flow_requests WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old['digest'] not in (digest, legacy_digest):
                    db.execute('ROLLBACK')
                    raise Conflict('该请求编号已用于其他任务。')
                db.execute('COMMIT')
                return self.get_flow(request_id), False
            if self.maintenance():
                db.execute('ROLLBACK')
                raise Conflict('工作台正在更新。')
            db.execute("INSERT INTO flow_requests(request_id,digest,project_key,profile,project,kind,status,created) VALUES(?,?,?,?,?,?,'dispatching',?)",
                       (request_id, digest, project_key, profile, project_id, kind, time.time()))
            db.execute('COMMIT')
        status, task_id, note = 'unknown', None, '提交结果未确认，请先在原后台核对任务。'
        try:
            result = (send or self._send_flow)(body)
            ident = result.get('id') if isinstance(result, dict) else None
            if (isinstance(ident, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', ident)
                    and result.get('profile') == profile and result.get('project') == project_id and result.get('kind') == kind):
                status, task_id, note = 'submitted', ident, '已交给原 Flow 后台排队。'
        except Exception:
            pass
        with self.connection() as db:
            db.execute("UPDATE flow_requests SET status=?,finished=?,task_id=?,note=? WHERE request_id=? AND status='dispatching'",
                       (status, time.time(), task_id, note, request_id))
        return self.get_flow(request_id), True

    def choose_account(self, db, items, now, stale=False, model=None, policy=None, browser_states=None):
        policy = policy if policy is not None else self.dispatch_policy(db)
        permitted = {item.get('key', '').removeprefix('flow-account-') for item in items if isinstance(item, dict)
                     and isinstance(item.get('key'), str) and item['key'].startswith('flow-account-')}
        candidates = []
        rows = db.execute('''SELECT a.*, (SELECT MAX(r.created) FROM requests r WHERE r.account_key=a.account_key) AS last_used,
                                    EXISTS(SELECT 1 FROM requests r WHERE r.account_key=a.account_key
                                           AND (r.status='dispatching' OR (r.status='unknown' AND NOT EXISTS
                                             (SELECT 1 FROM account_unknown_release x WHERE x.request_id=r.request_id)))) AS unresolved
                             FROM account_status a WHERE a.gemini_status='verified'
                               AND NOT EXISTS(SELECT 1 FROM account_preferences p WHERE p.account_key=a.account_key AND p.gemini_enabled=0)
                               AND NOT EXISTS(SELECT 1 FROM account_monitor m WHERE m.account_key=a.account_key AND m.status IN ('paused','network_wait','dispatching')) ''').fetchall()
        for row in rows:
            account = row['account_key']
            if (account not in permitted or not ACCOUNT.fullmatch(account) or row['unresolved']
                    or (browser_states is not None and browser_states.get(account) is not True)):
                continue
            if model:
                try:
                    choices = clean_model_choices(json.loads(row['models_json'])) if row['models_json'] else []
                except (TypeError, ValueError):
                    choices = []
                if not any(choice['id'] == model for choice in choices):
                    continue
            try:
                usage = clean_usage(json.loads(row['quota_json'])) if row['quota_json'] else None
            except (ValueError, TypeError):
                continue
            usage_state = self.usage_dispatch_state(usage, row['quota_checked'], now, policy)
            if usage_state != ('refresh_needed' if stale else 'eligible'):
                continue
            percent = usage['current_5h']['usage_percentage']
            candidates.append((percent, row['last_used'] or 0, account))
        if not candidates:
            raise Conflict('没有同时满足登录、浏览器和用量条件的 Gemini 账号，请先核对账号。')
        return min(candidates)[2]

    def generate(self, account, prompt, request_id, items, attachments=None, conversation_id=None, model=None, *, retry_policy=None, effort=None, fallback_model=None):
        return self.official.generate(account,prompt,request_id,items,attachments,conversation_id,model,
                                      retry_policy=retry_policy,effort=effort,fallback_model=fallback_model)
