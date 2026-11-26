"""Ubuntu Chrome rendering compatibility; upstream files and auth stay untouched."""
import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
import sys
import time
from filelock import FileLock, Timeout

from studiowright.browser import ChromeSession
from studiowright.studio import Studio
from studiowright import paths
from studiowright.store import Store


_generation_leases = {}


@asynccontextmanager
async def generation_lease(profile):
    """One operation per account; other accounts continue concurrently."""
    gate = _generation_leases.setdefault(profile, asyncio.Lock())
    await gate.acquire()
    guard = None
    try:
        paths.locks_dir().mkdir(parents=True, exist_ok=True)
        guard = FileLock(str(paths.locks_dir() / (profile + '.lock')), thread_local=False)
        while True:
            try:
                guard.acquire(timeout=0)
                break
            except Timeout:
                await asyncio.sleep(.2)
        Store().mark_interrupted(profile)
        yield
    finally:
        if guard is not None and guard.is_locked:
            guard.release()
        gate.release()


class _ManagedLock:
    def release(self):
        pass


class RenderingSession(ChromeSession):
    def configure_task(self, task_id, profile, coordinator):
        self._task_role = 'account-flow-task'
        self._task_id, self._profile, self._coordinator = task_id, profile, coordinator
        self._task_pages = set()

    async def check_page_activity(self, page, phase='browse'):
        from page_activity_guard import stop_on_page_warning
        if hasattr(self, '_profile'):
            await stop_on_page_warning(page, paths.home().parent, self._profile, phase)

    async def _safe_editor(self, page):
        from studiowright.flow import selectors as s
        from task_coordinator import CoordinationBlocked
        await self.check_page_activity(page)
        boxes = page.locator("flow-base-prompt-box [contenteditable='true'], " + s.COMPOSER)
        for index in range(await boxes.count()):
            if (await boxes.nth(index).inner_text()).strip():
                raise CoordinationBlocked('existing_flow_draft')
        for selector in (s.BOUND_CHIP, s.MENTION_CHIP):
            if await page.locator(selector).count():
                raise CoordinationBlocked('existing_flow_attachments')

    async def register_verified_identity(self, email):
        import hashlib
        from flow_readiness_recovery import _discover_chrome
        from flow_browser_state import register_flow
        found = await asyncio.to_thread(_discover_chrome, self._profile)
        if not found or found[1] != self.port:
            raise ValueError('flow_browser_identity_unconfirmed')
        for page in self.context.pages:
            if page.url.startswith('https://flow.google.com/'):
                await self._safe_editor(page)
        return await register_flow(hashlib.sha256(self._profile.encode()).hexdigest(), found[0], found[1], email)

    async def _target_id(self, page):
        channel = await self.context.new_cdp_session(page)
        try:
            return (await channel.send('Target.getTargetInfo'))['targetInfo']['targetId']
        finally:
            await channel.detach()

    async def _fixed_page(self, url_prefix):
        from urllib.parse import urlparse
        from task_coordinator import CoordinationBlocked, record
        from flow_window_policy import ensure_unique
        page = await ensure_unique(self,self._profile,task_id=self._task_id)
        row = self._coordinator.window(self._profile)
        target = row.get('target_id') if row else None
        pages=[page]
        for page in self.context.pages:
            if page.is_closed() or not page.url.startswith('https://flow.google.com/'):
                continue
            if page not in pages and ((target and await self._target_id(page)==target) or await self.role_of(page)==self._task_role):
                pages.append(page)
        if len(pages)>1:
            raise CoordinationBlocked('multiple_account_task_windows')
        if pages:
            page=pages[0]
            origin=urlparse(page.url)
            if not (page.url=='about:blank' or (origin.scheme=='https' and origin.hostname=='flow.google.com')):
                raise CoordinationBlocked('task_window_requires_login')
            await self._safe_editor(page)
            record('fixed_window_reused')
        else:
            page=await self._new_window()
            self._coordinator.bind(self._profile,self._task_id,await self._target_id(page))
            record('fixed_window_created')
        self._task_pages.add(page)
        if page.url=='about:blank':
            # A failed navigation retains this same target; recovery never opens a second window.
            from task_coordinator import recover_preparation
            async def navigate():
                await self._safe_editor(page)
                try:
                    await page.goto(url_prefix,wait_until='domcontentloaded',timeout=30000)
                except Exception as exc:
                    if type(exc).__name__=='TimeoutError':
                        raise TimeoutError('task_page_not_ready') from exc
                    raise
            await recover_preparation(navigate,reason='initial_page_load')
        await self.check_page_activity(page)
        if urlparse(page.url).hostname!='flow.google.com':
            raise CoordinationBlocked('task_window_requires_login')
        await page.evaluate('(name) => { window.name = name; }','sw-'+self._task_role)
        self._coordinator.bind(self._profile,self._task_id,await self._target_id(page))
        # Recount after adopting/creating a blank target and finishing navigation.
        await ensure_unique(self,self._profile,task_id=self._task_id)
        return page

    def find_page(self, url_prefix):
        if hasattr(self, '_task_pages'):
            return next((p for p in self._task_pages if not p.is_closed() and p.url.startswith(url_prefix)), None)
        return super().find_page(url_prefix)

    async def _new_window(self):
        page = await super()._new_window()
        if hasattr(self, '_task_pages'):
            self._task_pages.add(page)
        return page

    async def page_for(self, url_prefix: str, role: str = ""):
        if getattr(self, '_onboarding', False):
            candidates = []
            for page in self.context.pages:
                if page.is_closed():
                    continue
                await self.check_page_activity(page, 'login')
                if page.url.startswith('https://flow.google.com/'):
                    candidates.append(page)
            for candidate in candidates:
                await self._safe_editor(candidate)
            page = candidates[0] if candidates else await self._new_window()
            await self._safe_editor(page)
            return page
        if hasattr(self, '_task_role'):
            # All operations of this task use one retained account-owned window.
            page = next((p for p in self._task_pages if not p.is_closed()), None)
            if page is None:
                page = await self._fixed_page(url_prefix)
        else:
            if url_prefix.startswith('https://flow.google.com/') and hasattr(self,'_profile'):
                from flow_window_policy import ensure_unique
                page=await ensure_unique(self,self._profile)
                if page.url=='about:blank':
                    await page.goto(url_prefix,wait_until='domcontentloaded',timeout=30000)
                    await page.evaluate('(name)=>{window.name=name}','sw-account-flow-task')
                    await ensure_unique(self,self._profile)
            else:
                page = await super().page_for(url_prefix, role)
        if sys.platform.startswith("linux"):
            # Ubuntu can stop rAF even with visible=true and a normal window. Playwright
            # then cannot perform its ordinary stable-element check. Keep this target
            # active through Chrome's scheduling override, not force-click or JS clicks.
            # Retain the CDP session: detaching it releases the override.
            if not hasattr(self, "_rendering_sessions"):
                self._rendering_sessions = {}
            if page not in self._rendering_sessions:
                channel = await self.context.new_cdp_session(page)
                try:
                    await channel.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
                except BaseException:
                    await channel.detach()
                    raise
                self._rendering_sessions[page] = channel
        return page

    async def detach(self):
        try:
            for page, channel in getattr(self, "_rendering_sessions", {}).items():
                if not page.is_closed():
                    with suppress(Exception):
                        await channel.detach()
        finally:
            await super().detach()


class MixedReferencePage:
    """Keep video attachment separate from the upstream image tile path."""
    def __init__(self, flow):
        self.flow = flow
        self.video_ids = set()

    def __getattr__(self, key):
        return getattr(self.flow, key)

    async def upload_reference_video(self, path):
        from studiowright.flow.page import ReplyWatch, UPLOAD_RPC, be, replies
        async with ReplyWatch(self.page, [UPLOAD_RPC]) as watch:
            chooser = await self.flow._open_file_chooser()
            await chooser.set_files(str(path))
            deadline = time.monotonic() + 90
            while not watch.bodies and not watch.unreadable and time.monotonic() < deadline:
                if await self.page.locator("[role='dialog']").count():
                    raise ValueError('Flow 要求确认参考视频上传，请先人工处理，未提交生成。')
                await watch.wait(1)
        if len(watch.bodies) != 1:
            raise ValueError('Flow 未确认唯一的参考视频上传结果，未提交生成。')
        result = replies.upload_result(be.parse_reply(watch.bodies[0][2], UPLOAD_RPC))
        if not result.name or result.name != path.name:
            raise ValueError('Flow 未返回匹配的参考视频名称，未提交生成。')
        self.flow.names[result.media_id] = result.name
        self.video_ids.add(result.media_id)
        return result

    async def attach_reference(self, media_id):
        if media_id not in self.video_ids:
            return await self.flow.attach_reference(media_id)
        before = await self.flow.attachments()
        if not await self.flow._pick_by_name(media_id):
            raise ValueError('未在当前项目的素材选择器找到对应参考视频，未提交生成。')
        await self.flow._wait_attachments(before + 1)


def offered_model_pattern(label):
    import re
    return re.compile(re.escape(label) + r"(?=\s*(?:$|将于|將於|Retir|Discontinu|Deprecat))",re.I)


async def select_offered_model(self, pane, label):
    from studiowright.flow import selectors as selectors
    from studiowright.flow.page import FlowError
    button=pane.locator(selectors.MODEL_MENU_BUTTON).first
    current=" ".join((await button.inner_text()).replace("arrow_drop_down", "").split())
    if current.endswith(label):return
    await button.click()
    item=self.page.locator(selectors.MENU_ITEM).filter(has_text=offered_model_pattern(label))
    try:
        await item.first.wait_for(state="visible",timeout=5000)
    except Exception as exc:
        await self.page.keyboard.press("Escape")
        raise FlowError(f"the model {label!r} is not offered on this account") from exc
    await item.first.click()
    await self.page.wait_for_timeout(200)


def reference_dialog_choice(labels):
    positive = {'我同意', '同意', 'I agree', 'Agree'}
    cancel = {'取消', 'Cancel'}
    if len(set(labels)) != len(labels):
        return None
    if len(labels) == 1 and labels[0] in positive | cancel:
        return labels[0]
    if len(labels) == 2 and len(positive.intersection(labels)) == len(cancel.intersection(labels)) == 1:
        return next(label for label in labels if label in positive)
    if len(labels) == 3 and len(cancel.intersection(labels)) == 1:
        return next(label for label in labels if label in cancel)
    return None


async def handle_reference_dialog(page, dialogs, email, project):
    from adapter import READ_STATE
    from urllib.parse import urlsplit

    if await dialogs.count() != 1:
        return False
    dialog = dialogs.first
    lines = (await dialog.inner_text(timeout=2000)).splitlines()
    if not lines or lines[0].strip() != '此图片的使用权' or not any('使用限制政策' in line for line in lines):
        return False
    buttons = dialog.get_by_role('button').filter(visible=True)
    labels = [await button.inner_text(timeout=2000) for button in await buttons.all()]
    choice = reference_dialog_choice([' '.join(text.split()) for text in labels])
    if not choice:
        return False
    url = urlsplit(page.url)
    identity = await page.evaluate(READ_STATE)
    if (url.scheme != 'https' or url.netloc != 'flow.google.com' or url.path.rstrip('/') != '/project/' + project
            or not identity.get('signed_in') or identity.get('emails') != [email.lower()]):
        return False
    button = dialog.get_by_role('button', name=choice, exact=True).filter(visible=True)
    if await button.count() != 1 or not await button.is_enabled():
        return False
    await button.click(timeout=3000)
    await dialog.wait_for(state='hidden', timeout=3000)
    if choice in {'取消', 'Cancel'}:
        raise ValueError('已按设置取消 Flow 图片使用权弹窗，未提交生成。')
    return True


class RenderingStudio(Studio):
    def configure_reference_uploads(self, project, files, *, email):
        self._scoped_references = (project, tuple(files), email)

    async def _generation_page(self, project_id):
        lease, flow = await super()._generation_page(project_id)
        from types import MethodType
        flow._model = MethodType(select_offered_model,flow)
        scope = getattr(self, '_scoped_references', None)
        if scope and any(path.endswith('.mp4') for path in scope[1]):
            flow = MixedReferencePage(flow)
        return lease, flow

    async def _media_ids(self, flow, refs):
        scoped = getattr(self, '_scoped_references', None)
        if scoped is None or not refs:
            return await super()._media_ids(flow, refs)
        from urllib.parse import urlsplit
        from flow_references import reference_ids
        project, files, email = scoped
        url = urlsplit(flow.page.url)
        if (tuple(refs) != files or flow.project_id != project or url.scheme != 'https'
                or url.netloc != 'flow.google.com' or url.path.rstrip('/') != '/project/' + project):
            raise ValueError('参考图目标项目页面不一致，已停止生成。')
        results = []
        async def handle_dialog(dialogs):
            return await handle_reference_dialog(flow.page, dialogs, email, project)

        handler = handle_dialog if email else None
        # Do not consult the upstream hash cache, which only keys on project ID.
        # Each upload is performed inside the verified account's project page.
        try:
            async with asyncio.timeout(180):
                for path in map(Path, files):
                    if path.suffix == '.mp4':
                        uploaded = [await flow.upload_reference_video(path)]
                    else:
                        uploaded = await flow.upload([path], dialog_handler=handler) if handler else await flow.upload([path])
                    if len(uploaded) != 1 or (uploaded[0].name and uploaded[0].name != path.name):
                        raise ValueError('参考素材上传结果无法对应，已停止生成。')
                    results.append(uploaded[0].media_id)
        except TimeoutError:
            raise ValueError('参考素材上传超时，未提交生成。') from None
        return reference_ids(results)

    def configure_task(self, task_id):
        self._task_id = task_id
        # The adapter holds the shared, cross-process lease for the full task.
        self._lock = _ManagedLock()

    async def _locked(self):
        if hasattr(self, '_task_id'):
            return
        await super()._locked()

    async def __aenter__(self):
        if hasattr(self, '_task_id'):
            from task_coordinator import Coordinator
            self._coordinator = Coordinator()
            self._coordinator.begin(self.profile,self._task_id)
        try:
            await super().__aenter__()
        except BaseException:
            if hasattr(self,'_coordinator'):
                self._coordinator.finish(self.profile,self._task_id,False)
            if self._playwright:
                await self._playwright.stop()
            raise
        session = self.session
        self._session = RenderingSession(session.browser, session.context, session.port)
        self._session._profile = self.profile
        if hasattr(self, '_task_id'):
            self._session.configure_task(self._task_id,self.profile,self._coordinator)
        return self

    def _progress(self, job_id, stage, media_ids):
        # Save the conservative boundary before any submit action can execute.
        if hasattr(self,'_coordinator'):
            self._coordinator.progress(self.profile,self._task_id,stage)
        return super()._progress(job_id,stage,media_ids)

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if hasattr(self,'_coordinator'):
                draft=False
                for page in getattr(self.session,'_task_pages',set()):
                    if not page.is_closed():
                        try:
                            await self.session._safe_editor(page)
                        except Exception:
                            draft=True
                self._coordinator.finish(self.profile,self._task_id,exc_type is None,draft,reason=str(exc) if exc else None)
        finally:
            from flow_window_policy import Lifecycle
            with suppress(Exception):Lifecycle().touch(self.profile)
            await super().__aexit__(exc_type,exc,tb)
