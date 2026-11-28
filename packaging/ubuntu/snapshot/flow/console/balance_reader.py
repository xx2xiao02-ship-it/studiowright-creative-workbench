"""Read credit balances from the verified account's Flow and Google One pages.

Keep the Flow project tab in place. The optional Google One read uses a
temporary background tab. Uses the same per-account lock as upstream tasks.
"""
import asyncio
from contextlib import AsyncExitStack, suppress
from pathlib import Path
import hashlib
import re

from balance_browser import FixedBalancePage

ACCOUNT_PANEL = re.compile(r'^(账号详情|帐号详情|帳戶詳細資料|Account details|アカウントの詳細)$', re.I)
CLOSE_PANEL = re.compile(r'^(关闭账号面板|Close account panel)$', re.I)
CREDIT_ACTIVITY_URL = 'https://one.google.com/ai/activity?g1_landing_page=0&utm_source=flow'
CREDIT_ACTIVITY_PAGE = re.compile(r'^https://one\.google\.com/(?:u/\d+/)?ai/activity(?:[?#].*)?$')
DAILY_CREDITS_TEXT = re.compile(
    r'(?:今天[还還]剩|You have)\s*(\d+)\s*'
    r'(?:[个個]\s*Flow\s*(?:点数|點數|积分|積分)|Flow credits?\s*(?:remaining|left)\s*today)[。.]?', re.I)
REVEAL_HEADER = """() => {
    const header = document.querySelectorAll('flow-tile-view-header.header-hidden');
    const views = document.querySelectorAll('.cdk-virtual-scrollable.page-container');
    if (header.length !== 1 || views.length !== 1 || views[0].scrollTop <= 0) return null;
    const top = views[0].scrollTop;
    views[0].scrollTo({top:0,behavior:'instant'});
    return top;
}"""
RESTORE_SCROLL = """top => {
    const views = document.querySelectorAll('.cdk-virtual-scrollable.page-container');
    if (views.length === 1 && views[0].scrollTop === 0) views[0].scrollTo({top,behavior:'instant'});
}"""

class BalanceReadDeferred(ValueError):
    """Foreground input has priority; retain the previous balance observation."""


class BalancePanelUnavailable(ValueError):
    """The verified Flow page did not expose the panel or its balance yet."""


async def page_url(page):
    return await page.current_url() if isinstance(page, FixedBalancePage) else page.url


async def open_account_panel(page, panel, close, email, checked_url, foreground, restoration=None):
    from adapter import READ_STATE
    from playwright.async_api import TimeoutError as BrowserTimeout
    from foreground_queue import ForegroundQueue
    scroll_top = None
    restoration = {} if restoration is None else restoration
    # Retry the exact registered tab once before reporting a missing entry.
    for recovery in (False, True):
        if recovery:
            queue = ForegroundQueue(Path.home()/'.local/state/creative-unified-workbench/foreground-input')
            if not await foreground.enter_async_context(queue.try_acquire_async()):
                raise BalanceReadDeferred('前台正在执行任务，稍后再读余额。')
            if await page_url(page) != checked_url:
                raise ValueError('Flow 页面在读取时发生切换，保留上次余额。')
            await page.bring_to_front()
            try:
                await page.wait_for_load_state('domcontentloaded', timeout=1500)
            except BrowserTimeout:
                pass
        verified = False
        count = 0
        for attempt in range(10):
            if await page_url(page) != checked_url:
                raise ValueError('Flow 页面在读取时发生切换，保留上次余额。')
            verified = verify_identity(await page.evaluate(READ_STATE), email)
            if verified:
                if await close.count() == 1:
                    return restoration.get('opened', False), scroll_top
                top = await page.evaluate(REVEAL_HEADER)
                if top is not None:
                    if scroll_top is None:
                        scroll_top = top
                        restoration['scroll_top'] = top
                    await asyncio.sleep(.35)
                count = await panel.count()
                if count > 1:
                    raise ValueError('Flow 页面有多个账号详情入口，未读取余额。')
                if count == 1:
                    restoration['opened'] = True
                    try:
                        await panel.click(timeout=1600)
                    except BrowserTimeout:
                        break
                    return True, scroll_top
            await asyncio.sleep(.2)
        if not verified:
            raise ValueError('Flow 登录信息尚未就绪，保留上次余额。')
    raise BalancePanelUnavailable('Flow 账号详情暂未打开，保留上次余额并稍后重读。')


def parse_balance(text, branded_only=False):
    patterns=(r'^([\d,\u00a0 ]+)\s*个?\s*Google\s*Flow\s*(?:点数|积分|點數|credits?|クレジット|ポイント)$',
              r'^([\d,\u00a0 ]+)\s*(?:AI\s*)?(?:credits?|クレジット|ポイント)$',
              r'^(?:Google\s*Flow\s*)?(?:credits?|点数|积分)\s*[:：]\s*([\d,\u00a0 ]+)$')
    if branded_only:patterns=patterns[:1]
    for pattern in patterns:
        values={int(re.sub(r'[,\s\u00a0]','',m.group(1))) for line in text.splitlines() for m in re.finditer(pattern,line.strip(),re.I) if re.sub(r'[,\s\u00a0]','',m.group(1)).isdigit()}
        if len(values)==1:return values.pop()
    return None

async def fixed_flow_page(browser, profile, email='', sessions=None):
    from task_window_registry import TaskWindowRegistry, canonical_project_url, process_identity

    attached = [p for cx in browser.contexts for p in cx.pages]
    pages = [p for p in attached if p.url.startswith('https://flow.google.com/')]
    key = hashlib.sha256(profile.encode('utf-8')).hexdigest()
    binding = TaskWindowRegistry().get(key)
    if not pages:
        if (sessions is not None and binding and binding['profile'] == profile
                and binding['binding_state'] == 'verified'
                and process_identity(binding['browser_pid'], key, binding['port']) == (profile, binding['process_start'])):
            for candidate in (page for page in attached if not page.url):
                session = await candidate.context.new_cdp_session(candidate)
                sessions.push_async_callback(session.detach)
                info = (await session.send('Target.getTargetInfo'))['targetInfo']
                if info.get('type') != 'page' or info.get('targetId') != binding['page_target']:
                    continue
                direct = FixedBalancePage(candidate, session, info['url'])
                url = await direct.current_url()
                if url == info['url'] and canonical_project_url(url) == binding['project_url']:
                    direct.expected_url, direct.email = url, email
                    return direct
        raise ValueError('现有浏览器没有 Flow 页面，保留上次余额。')
    if not binding:
        if len(pages) == 1:
            return pages[0]
        raise ValueError('有多个 Flow 页面，固定任务页尚未登记，保留上次余额。')
    if binding['profile'] == profile and binding['binding_state'] == 'verified':
        for page in pages:
            try:
                if canonical_project_url(page.url) != binding['project_url']:
                    continue
            except ValueError:
                continue
            session = await page.context.new_cdp_session(page)
            try:
                info = (await session.send('Target.getTargetInfo'))['targetInfo']
            finally:
                await session.detach()
            if info['targetId'] == binding['page_target']:
                return page
    raise ValueError('未找到该账号登记的 Flow 固定任务页，保留上次余额。')


def verify_identity(state, email):
    emails = state.get('emails') or []
    if emails and emails != [email]:
        raise ValueError('Flow 页面邮箱与登记账号不符，未读取余额。')
    return bool(email and state.get('signed_in') and emails == [email])


def parse_credit_renewal(text, now=None):
    """Read explicit credit reset text, including Google's yearless Chinese format."""
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone(timedelta(hours=8)))
    lines = str(text).splitlines()
    values = set()
    for i, line in enumerate(lines):
        context = ' '.join(lines[max(0,i-1):i+2])
        if not re.search(r'积分|点数|點數|credits?', context, re.I):
            continue
        if not re.search(r'重置时间|重置時間|补充|到账|refresh|reset|refill|replenish', line, re.I):
            continue
        match = re.search(r'(?:(20\d{2})[年/.-]\s*)?(\d{1,2})[月/.-]\s*(\d{1,2})日?(?:\s*(\d{1,2}):(\d{2}))?', line)
        try:
            if match:
                y,mo,day,hour,minute=match.groups()
                year=int(y) if y else now.year
                value=datetime(year,int(mo),int(day),int(hour or 0),int(minute or 0),tzinfo=now.tzinfo)
                if not y and value.date()<now.date():
                    value=value.replace(year=year+1)
                values.add(value.strftime('%Y-%m-%d %H:%M') if hour else value.strftime('%Y-%m-%d'))
            else:
                match=re.search(r'([A-Za-z]+ \d{1,2},? 20\d{2}|\d{1,2} [A-Za-z]+ 20\d{2})',line)
                if not match:continue
                for fmt in ('%B %d %Y','%b %d %Y','%d %B %Y','%d %b %Y'):
                    try:values.add(datetime.strptime(match[0].replace(',',''),fmt).strftime('%Y-%m-%d'));break
                    except ValueError:pass
        except ValueError:
            continue
    return next(iter(values)) if len(values)==1 else None


async def read_daily_credits(browser, email):
    from playwright.async_api import Error as BrowserError

    with suppress(BrowserError):
        async with AsyncExitStack() as cleanup:
            context = browser.contexts[0]
            session = await browser.new_browser_cdp_session()
            cleanup.push_async_callback(session.detach)
            async with context.expect_page(timeout=2500) as opened:
                target = (await session.send('Target.createTarget',
                    {'url': CREDIT_ACTIVITY_URL, 'background': True}))['targetId']
                cleanup.push_async_callback(session.send, 'Target.closeTarget', {'targetId': target})
            page = await opened.value
            page_session = await context.new_cdp_session(page)
            try:
                if (await page_session.send('Target.getTargetInfo'))['targetInfo']['targetId'] != target:
                    return None
            finally:
                await page_session.detach()
            await page.wait_for_url(CREDIT_ACTIVITY_PAGE, wait_until='domcontentloaded', timeout=2500)
            summary = page.get_by_role('main').filter(has_text=DAILY_CREDITS_TEXT)
            lines = (await summary.inner_text(timeout=2500)).splitlines()
            values = [int(match[1]) for line in lines if (match := DAILY_CREDITS_TEXT.fullmatch(line.strip()))]
            identity = page.get_by_role('button', name=re.compile(r'\(' + re.escape(email) + r'\)', re.I))
            if len(values) == 1 and (await identity.count()) == 1 and CREDIT_ACTIVITY_PAGE.fullmatch(page.url):
                return values[0]
    return None


async def read_balance(account, playwright, include_details=False, *, incident_root=None):
    from filelock import FileLock,Timeout
    from playwright.async_api import TimeoutError as BrowserTimeout
    from studiowright import chrome,paths
    from adapter import READ_STATE
    loop=asyncio.get_running_loop()
    details_deadline=loop.time()+18
    lock=FileLock(str(paths.locks_dir()/f"{account['profile']}.lock"))
    try:lock.acquire(timeout=0)
    except Timeout:raise BalanceReadDeferred('账号正在执行任务，余额稍后更新。') from None
    opened=False;page=None;checked_url=None;scroll_top=None
    foreground=AsyncExitStack()
    restoration={}
    try:
        # Do not call upstream attach/ensure_running: that could open a new window.
        browser=await playwright.chromium.connect_over_cdp('http://127.0.0.1:'+str(chrome.debug_port(account['profile'])),timeout=4000,no_defaults=True)
        email=(account.get('email') or '').lower()
        page=await fixed_flow_page(browser,account['profile'],email,foreground)
        checked_url=await page_url(page)
        if incident_root is not None:
            from page_activity_guard import stop_on_page_warning
            await stop_on_page_warning(page, incident_root, account['profile'], 'browse')
        panel=page.get_by_role('button',name=ACCOUNT_PANEL).filter(visible=True)
        close=page.get_by_role('button',name=CLOSE_PANEL).filter(visible=True)
        body=page.locator('body')
        opened,scroll_top=await open_account_panel(
            page,panel,close,email,checked_url,foreground,restoration)
        for _ in range(12):
            if await page_url(page)!=checked_url:
                raise ValueError('Flow 页面在读取时发生切换，保留上次余额。')
            if incident_root is not None:
                await stop_on_page_warning(page, incident_root, account['profile'], 'browse')
            panel_text=await body.inner_text(timeout=750)
            value=parse_balance(panel_text,branded_only=True)
            if value is not None:
                if include_details and parse_credit_renewal(panel_text) is None:
                    credit_label=page.get_by_text(re.compile(r'^[\d,\s]+个?\s*Google\s*Flow\s*(?:点数|积分|點數|credits?)$',re.I)).filter(visible=True)
                    if await credit_label.count()==1:
                        try:
                            await credit_label.hover(timeout=750)
                            for _ in range(5):
                                panel_text=await body.inner_text(timeout=750)
                                if parse_credit_renewal(panel_text):break
                                await asyncio.sleep(.15)
                        except BrowserTimeout:
                            pass
                if not verify_identity(await page.evaluate(READ_STATE),email) or await page_url(page)!=checked_url:
                    raise ValueError('Flow 页面或账号发生变化，未保存余额。')
                if not include_details:
                    return value
                details = {'balance': value, 'next_credit_date': parse_credit_renewal(panel_text),
                           'daily_credits_remaining': None}
                # Give the optional read a short window inside the caller's 20 second limit.
                budget = min(4, details_deadline - loop.time())
                if budget > 0:
                    with suppress(TimeoutError):
                        details['daily_credits_remaining'] = await asyncio.wait_for(
                            read_daily_credits(browser, email), timeout=budget)
                return details
            await asyncio.sleep(.25)
        if not verify_identity(await page.evaluate(READ_STATE), email) or await page_url(page) != checked_url:
            raise ValueError('Flow 页面或账号发生变化，未保存余额。')
        raise BalancePanelUnavailable('Flow 积分暂未显示，保留上次余额并稍后重读。')
    finally:
        opened = opened or restoration.get('opened', False)
        scroll_top = restoration.get('scroll_top', scroll_top)
        try:
            if opened and page and not page.is_closed() and await page_url(page)==checked_url:
                if await close.count()==1:await close.click(timeout=750)
        except Exception:pass
        finally:
            try:
                if scroll_top is not None and not page.is_closed() and await page_url(page)==checked_url:
                    await page.evaluate(RESTORE_SCROLL,scroll_top)
            except Exception:pass
            finally:
                try:await foreground.aclose()
                finally:lock.release()
