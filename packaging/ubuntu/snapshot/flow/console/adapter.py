"""Separate orchestration layer; imports, never patches, pinned upstream source."""

import asyncio
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

READ_STATE = r"""() => {
 if(location.hostname!=='flow.google.com') return {signed_in:false,emails:[],projects:[]};
 const w=window.WIZ_global_data||{};
 const valid=v=>typeof v==='string' && /^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$/i.test(v.trim());
 const candidates=valid(w.oPEP7c)?[w.oPEP7c]:Object.values(w).filter(valid);
 const emails=[...new Set(candidates.map(v=>v.trim().toLowerCase()))];
 return {signed_in:!!(w.SNlM0e&&w.FdrFJe),emails,projects:[...document.querySelectorAll('a[href]')].filter(a=>/^https:\/\/flow\.google\.com\/project\/[0-9a-f-]{36}(?:[/?#]|$)/i.test(a.href)).map(a=>({url:a.href,title:(a.innerText||'').trim().slice(0,140)}))};
}"""


class PreSubmissionFailure(RuntimeError):
    """Preparation failed before any generation operation was invoked."""


def browser_profile_matches(args, target):
    directories = []
    for i, arg in enumerate(args):
        if arg.startswith("--user-data-dir="):
            directories.append(arg.split("=",1)[1])
        elif arg == "--user-data-dir" and i+1 < len(args):
            directories.append(args[i+1])
    return len(directories)==1 and Path(directories[0]).resolve()==Path(target).resolve()


class PreSubmissionTransient(RuntimeError):
    """A temporary error occurred before the generation operation was invoked."""


class AccountWindowBusy(ValueError):
    """An account operation owns the window lock; login was not checked."""


def flow_home_projects(state, known_titles=None):
    known_titles = known_titles or {}
    projects = {}
    for item in state["projects"]:
        match = re.search(r"/project/([0-9a-f-]{36})", item["url"], re.I)
        if match:
            ident = match[1].lower()
            title = item["title"].strip() or known_titles.get(ident) or "未命名项目"
            if ident not in projects or item["title"].strip():
                projects[ident] = {"id": ident, "title": title}
    return list(projects.values())


class RealAdapter:
    def __init__(self, root):
        self.root = Path(root)
        os.environ["STUDIOWRIGHT_HOME"] = str(self.root / "data")
        os.environ["STUDIOWRIGHT_OUTPUT"] = str(self.root / "output")
        os.environ["PATH"] = str(self.root / "bin") + os.pathsep + os.environ["PATH"]
        from studiowright.flow.options import ImageModel, VideoModel
        from studiowright.store import Store
        from browser_rendering import RenderingStudio as Studio

        Store()
        self.Studio = Studio
        self.models = {
            "image": [{"id": m.value, "label": m.label} for m in ImageModel],
            "video": [{"id": m.value, "label": m.label} for m in VideoModel if m.offered],
        }

    def discover(self):
        directory = self.root / "data/profiles"
        return (
            sorted(
                p.name
                for p in directory.iterdir()
                if p.is_dir() and p.name != "music-export"
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", p.name)
            )
            if directory.exists()
            else []
        )

    def validate(self, kind, params):
        from studiowright.flow.options import ImageOptions, VideoOptions

        args = {"model": params["model"], "aspect": params["aspect"], "count": params["count"]}
        return (
            ImageOptions.build(**args)
            if kind == "image"
            else VideoOptions.build(
                **args, mode=params.get("mode", "frames"), resolution=params["resolution"], duration=params["duration"]
            )
        )

    def _browser(self, profile, login=False):
        from urllib.request import urlopen

        from studiowright import chrome, paths

        target = paths.profile_dir(profile)
        port = chrome.debug_port(profile)
        # Upstream port hashing has a bounded range: don't attach across profiles.
        other = [p for p in self.discover() if p != profile and chrome.debug_port(p) == port]
        if other:
            raise ValueError("账号浏览器端口与另一个账号冲突，请换一个账号名称。")
        args_for_profile = []
        for proc in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                args = [v.decode() for v in proc.read_bytes().split(b"\0") if v]
                if len(args) == 1:
                    args = shlex.split(args[0])
                if not args or any(a.startswith("--type=") for a in args):
                    continue
                if (
                    any(a == f"--remote-debugging-port={port}" for a in args)
                    and not browser_profile_matches(args,target)
                ):
                    raise ValueError("调试端口属于其他浏览器，已停止连接，避免串号。")
                if browser_profile_matches(args,target):
                    args_for_profile = args
            except (OSError, UnicodeError):
                continue
        unit = "studiowright-browser-" + profile
        if login:
            if args_for_profile:
                # Only stop the specifically managed profile, never all Chrome processes.
                active = subprocess.run(
                    ["systemctl", "--user", "is-active", unit], capture_output=True, text=True
                )
                if active.stdout.strip() != "active":
                    raise ValueError(
                        "此账号的登录窗口已打开；请在 Ubuntu 完成登录并关闭该窗口，然后检查账号。"
                    )
                subprocess.run(
                    ["systemctl", "--user", "stop", unit], check=True, capture_output=True
                )
        else:
            if args_for_profile and not any(
                a.startswith("--remote-debugging-port=") for a in args_for_profile
            ):
                raise ValueError(
                    "此账号的登录窗口尚未关闭。请在 Ubuntu 登录完成后关闭窗口，再检查账号。"
                )
            if chrome.endpoint_version(port):
                # Verify endpoint reachable; never return its debugger secret to the UI.
                with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
                    pass
                return
        target.mkdir(parents=True, exist_ok=True)
        env_text = subprocess.check_output(["systemctl", "--user", "show-environment"], text=True)
        gui = dict(line.split("=", 1) for line in env_text.splitlines() if "=" in line)
        flags = []
        for key in (
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "XAUTHORITY",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
        ):
            if key in gui:
                flags += ["--setenv=" + key + "=" + gui[key]]
        command = (
            chrome.sign_in_command(chrome.require_chrome(), target)
            if login
            else chrome.automation_command(chrome.require_chrome(), target, port)
        )
        subprocess.run(
            ["systemd-run", "--user", "--quiet", "--collect", "--unit=" + unit, *flags, *command],
            check=True,
            capture_output=True,
        )
        if not login:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if chrome.endpoint_version(port):
                    return
                time.sleep(0.3)
            raise ValueError("Ubuntu 浏览器未就绪。请确认已进入 Ubuntu 桌面，稍后再检查。")

    async def run(self, task, account, *, on_video_submitted=None):
        if task['kind'] in {'image', 'video'}:
            from playwright.async_api import TimeoutError as PlaywrightTimeout
            from browser_rendering import generation_lease
            phase = {'generation_invoked': False, 'on_video_submitted': on_video_submitted}
            try:
                async with generation_lease(task['profile']):
                    return await self._run(task, account, phase)
            except (PlaywrightTimeout, TimeoutError, ConnectionError) as exc:
                if not phase['generation_invoked']:
                    raise PreSubmissionTransient(str(exc)) from exc
                raise
            except Exception as exc:
                if not phase["generation_invoked"]:
                    raise PreSubmissionFailure(str(exc)) from exc
                raise
        if task["kind"] not in {"sync", "login", "session_import"}:
            return await self._run(task, account)
        # Reuse the original cross-process lock before navigating the main tab
        # or restarting a login window. A separate CLI job must not be disturbed.
        from filelock import FileLock, Timeout
        from studiowright import paths

        paths.locks_dir().mkdir(parents=True, exist_ok=True)
        guard = FileLock(str(paths.locks_dir() / f"{task['profile']}.lock"))
        try:
            guard.acquire(timeout=0)
        except Timeout:
            raise AccountWindowBusy("账号窗口暂被其他操作占用，本次未检查登录，请稍后再检查。") from None
        try:
            return await self._run(task, account)
        finally:
            guard.release()

    async def _run(self, task, account, phase=None):
        p = task["params"]
        from flow_references import REFERENCE_FIELDS, ReferenceStore, task_reference_ids
        if p.keys() & REFERENCE_FIELDS:
            ids = task_reference_ids(task['kind'], p)
            store = ReferenceStore(Path.home() / '.local/state/creative-unified-workbench')
            with store.files(ids, task['profile'], task['project'], account.get('email'),
                             allow_video=p['mode'] == 'ingredients') as files:
                return await self._run_prepared(task, account, phase, files)
        return await self._run_prepared(task, account, phase)

    async def _run_prepared(self, task, account, phase=None, reference_files=()):
        profile, project, kind, p = task["profile"], task["project"], task["kind"], task["params"]
        await asyncio.to_thread(self._browser, profile, kind == "login")
        if kind == "login":
            return {
                "message": "已在 Ubuntu 打开此账号的独立登录窗口。完成登录并关闭窗口后，点击检查登录与项目。"
            }
        studio = self.Studio(profile)
        if kind in {'image', 'video'}:
            studio.configure_task(task['id'])
            studio.console_task_id = task['id']
            studio.console_attempt_ordinal = task.get('attempt_ordinal', 0)
        if reference_files:
            studio.configure_reference_uploads(project, reference_files, email=account.get('email'))
        async with studio:
            if kind in {'sync', 'session_import'}:
                studio.session._onboarding = True
            from studiowright.flow.page import FLOW_ROOT
            from login_readiness import read_when_ready
            from page_activity_guard import stop_on_page_warning

            async def check_login_warning(current):
                await stop_on_page_warning(current, self.root, profile, 'login')

            async def read_login_state():
                await check_login_warning(page)
                return await read_when_ready(page, READ_STATE)

            page = await asyncio.wait_for(studio.session.page_for(FLOW_ROOT), timeout=35)
            await check_login_warning(page)
            if not page.url.startswith(FLOW_ROOT):
                await page.goto(FLOW_ROOT, wait_until="domcontentloaded", timeout=45000)
            if kind == "session_import":
                from login_sessions import Vault, inject_verified
                from console_store import Store
                from task_window_registry import canonical_project_url

                known_titles = {row["id"]: row["title"] for row in Store(self.root / "console-data").projects(profile)}
                try:
                    previous_project = canonical_project_url(page.url)
                except ValueError:
                    previous_project = None

                vault = Vault(self.root / "console-data")
                bundle = vault.get(p["bundle_id"])
                if (
                    bundle["email"] != p["expected_email"]
                    or account["email"]
                    and account["email"].lower() != bundle["email"]
                ):
                    raise ValueError("登录包邮箱与目标账号不一致，未写入。")
                async def finalize(state):
                    for _ in range(6):
                        if state.get("projects"):
                            break
                        await asyncio.sleep(.5)
                        state = await read_login_state()
                        if not state.get("signed_in") or state.get("emails") != [bundle["email"]]:
                            raise ValueError("Flow 首页登录状态变化。")
                    projects = flow_home_projects(state, known_titles)
                    if previous_project and (previous_project.rsplit("/", 1)[-1] in known_titles
                                             or any(p["id"] == previous_project.rsplit("/", 1)[-1] for p in projects)):
                        await page.goto(previous_project, wait_until="domcontentloaded", timeout=30000)
                        await check_login_warning(page)
                    await studio.session.register_verified_identity(bundle["email"])
                    message = f"登录及窗口已核对，已同步 {len(projects)} 个首页项目。"
                    if not projects:
                        message += "可在 Flow 创建项目后检查就绪。"
                    return {"email": bundle["email"], "projects": projects, "message": message}

                return await inject_verified(
                    studio.session.context, page, bundle, READ_STATE, vault,
                    finalize=finalize, check_warning=check_login_warning,
                )
            state = None
            for _ in range(10):
                state = await read_login_state()
                if state["signed_in"] and len(state["emails"]) == 1:
                    break
                await asyncio.sleep(1)
            if not state["signed_in"] or len(state["emails"]) != 1:
                if kind == "sync":
                    from login_readiness import resume_google_session
                    recovery = await resume_google_session(studio.session.context, account.get("email"), READ_STATE,
                                                           check_warning=check_login_warning)
                    if recovery["status"] == "reauth_required":
                        raise ValueError(recovery["reason"] + " 请在此账号的“远程登录”窗口完成登录；完成后后台会自动检查，不需要手动同步。")
                    if recovery["status"] == "mismatch":
                        raise ValueError(recovery["reason"])
                    if recovery["status"] == "resumed":
                        await page.goto(FLOW_ROOT, wait_until="domcontentloaded", timeout=45000)
                        for _ in range(10):
                            state = await read_login_state()
                            if state["signed_in"] and state["emails"] == [account["email"].lower()]:
                                break
                            await asyncio.sleep(1)
                    if not state["signed_in"] or len(state["emails"]) != 1:
                        raise ValueError(recovery["reason"])
            if not state["signed_in"] or len(state["emails"]) != 1:
                if kind in ('image','video') and hasattr(studio.session,'_safe_editor'):
                    from task_coordinator import record
                    # Refresh only this owned, empty preparation window. A login
                    # challenge is retained for the user, never clicked through.
                    await studio.session._safe_editor(page)
                    record('login_page_recheck')
                    await page.goto(FLOW_ROOT,wait_until='domcontentloaded',timeout=30000)
                    state=await read_login_state()
            if not state["signed_in"] or len(state["emails"]) != 1:
                if kind in ('image','video'):
                    raise ValueError('task_window_requires_login')
                raise ValueError(
                    "Flow 未返回唯一的已登录邮箱。请检查 Ubuntu 此账号的登录窗口；未提交生成。"
                )
            email = state["emails"][0].lower()
            if account["email"] and email != account["email"].lower():
                raise ValueError("此浏览器的邮箱与已登记账号不一致，已停止操作，避免串号。")
            if kind == "sync":
                await studio.session.register_verified_identity(email)
                await page.goto(FLOW_ROOT, wait_until="domcontentloaded", timeout=45000)
                for _ in range(12):
                    state = await read_login_state()
                    if state["signed_in"] and state["emails"] == [email] and state["projects"]:
                        break
                    await asyncio.sleep(1)
                if state["emails"] != [email] or not state["signed_in"]:
                    observed = ", ".join(state.get("emails", [])) or "未返回邮箱"
                    raise ValueError(
                        f"本地登录资料已载入；Flow 首页未确认目标邮箱（当前：{observed}，登录标识：{'有' if state.get('signed_in') else '无'}）。未保存项目列表，未提交生成。"
                    )
                return {
                    "email": email,
                    "projects": flow_home_projects(state),
                    "message": "已核对登录并读取首页当前加载的项目；不代表全部历史项目。",
                }
            if kind == "media":
                return await studio.media(project, limit=100)
            if kind == "doctor":
                return await studio.doctor(project)
            from studiowright.studio import ImageRequest, VideoRequest

            options = self.validate("video" if kind == "quote" else kind, p)
            if kind == "quote":
                return await studio.video(
                    VideoRequest(p["prompt"], options, project=project, dry_run=True)
                )
            # A UI submission always names an existing project; upstream auto-create cannot run.
            out = self.root / "output" / task["id"]
            if kind == "image":
                studio.console_task_id = task["id"]
                if phase is not None:
                    phase['generation_invoked'] = True
                return await studio.image(
                    ImageRequest(p["prompt"], options, refs=reference_files, project=project, out_dir=out)
                )
            # Source price reading is mandatory. Unknown does not mean free.
            quote = await studio.video(
                VideoRequest(p["prompt"], options, project=project, dry_run=True)
            )
            if quote.get("credits") is None:
                raise ValueError(
                    "原项目未能读取当前视频报价（中文界面可能出现此情况）；本次未提交生成。可在任务详情查看报价检查结果。"
                )
            # The credit price is informational; production runs uncapped.
            studio.console_task_id = task["id"]
            if phase is not None:
                phase['generation_invoked'] = True
            frame_fields = {}
            if p.get('mode') == 'frames' and reference_files:
                frame_fields['start'] = reference_files[0]
                if len(reference_files) == 2:
                    frame_fields['end'] = reference_files[1]
            return await studio.video(
                VideoRequest(
                    p["prompt"], options, refs=() if frame_fields else reference_files, project=project,
                    out_dir=out, max_credits=p["max_credits"], **frame_fields
                ),
                on_submitted=phase.get('on_video_submitted') if phase else None,
            )

    def source_jobs(self, profile):
        from studiowright.store import Store

        return Store().recent_jobs(profile, limit=12)

    def source_jobs_for_task(self, profile, task_id, attempt_ordinal=0):
        """Exactly correlated new-generation records; old jobs are never inferred."""
        from studiowright.store import Store

        with Store().connect() as db:
            return db.execute(
                "SELECT * FROM jobs WHERE profile=? AND kind IN ('image','video') "
                "AND json_valid(params) AND json_extract(params,'$.origin_console_task')=? "
                "AND COALESCE(json_extract(params,'$.origin_console_attempt'),0)=?",
                (profile, task_id, attempt_ordinal),
            ).fetchall()


class FakeAdapter:
    """Only explicitly selected isolated test servers use this adapter."""

    def __init__(self, root):
        self.root = Path(root)
        self.calls = []
        self.models = {
            "image": [
                {"id": "nano-banana-2.1", "label": "Nano Banana 2.1"},
                {"id": "nano-banana-pro", "label": "Nano Banana Pro"},
                {"id": "nano-banana-2-lite", "label": "Nano Banana 2 Lite"},
            ],
            "video": [
                {"id": "omni-flash", "label": "Omni 1.1 Flash"},
                {"id": "veo-lite", "label": "Veo 3.1 - Lite"},
                {"id": "veo-fast", "label": "Veo 3.1 - Fast"},
                {"id": "veo-quality", "label": "Veo 3.1 - Quality"},
            ],
        }

    def discover(self):
        return []

    def validate(self, kind, params):
        if params["model"] not in [m["id"] for m in self.models[kind]]:
            raise ValueError("未知模型")

    async def run(self, task, account, *, on_video_submitted=None):
        self.calls.append(task)
        await asyncio.sleep(0.12)
        if task["params"].get("prompt") == "模拟失败":
            raise ValueError("模拟服务拒绝任务；未生成。")
        if task["kind"] == "session_import":
            return {
                "email": task["params"]["expected_email"],
                "projects": [],
                "message": "模拟登录导入完成；未连接 Google。",
            }
        if task["kind"] == "sync":
            return {
                "email": task["profile"] + "@example.test",
                "projects": [
                    {
                        "id": "11111111-1111-4111-8111-111111111111",
                        "title": "模拟项目 · 不连接 Google",
                    }
                ],
                "message": "模拟数据",
            }
        if task["kind"] == "quote":
            return {"credits": 12, "submitted": False}
        if task["kind"] == "media":
            return {"project_id": task["project"], "total": 0, "media": []}
        if task["kind"] == "login":
            return {"message": "模拟登录窗口"}
        return {
            "job_id": "mock-" + task["id"],
            "project_id": task["project"],
            "media_ids": ["mock-media"],
            "files": [],
            "stage": "delivered",
            "message": "模拟任务完成；未连接 Google，未消耗积分。",
        }

    def source_jobs(self, profile):
        return []
