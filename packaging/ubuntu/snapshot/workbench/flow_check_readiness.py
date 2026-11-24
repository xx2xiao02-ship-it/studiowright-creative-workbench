"""Inspect fixed Flow pages without focusing, navigating, typing, or sending."""
import asyncio
import json
import re
import sys
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from flow_browser_state import _pages, focus_call
from task_window_registry import canonical_project_url, process_identity


def composer_expression(email):
    return r"""(() => {
      if (location.origin !== 'https://flow.google.com') return {origin:false};
      const w=window.WIZ_global_data||{};
      const valid=v=>typeof v==='string' && /^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$/i.test(v.trim());
      const emails=[...new Set((valid(w.oPEP7c)?[w.oPEP7c]:Object.values(w).filter(valid)).map(v=>v.trim().toLowerCase()))];
      const boxes=[...document.querySelectorAll("flow-prompt-box [contenteditable='true'], flow-base-prompt-box [contenteditable='true']")];
      const visible=e=>e.getClientRects().length>0 && getComputedStyle(e).visibility==='visible';
      const account=emails.length===1 && emails[0]===EXPECTED && !!(w.SNlM0e&&w.FdrFJe);
      const login=!account && [...document.querySelectorAll('a,button')].some(e=>visible(e) &&
        (/^(sign in|log in|登录|登入|继续登录|使用 Google 登录|sign in with google)$/i.test(e.innerText.trim()) ||
          (e.tagName==='A' && e.href.startsWith('https://accounts.google.com/') && /sign\s*in|log\s*in|登录|登入/i.test(e.innerText))));
      const status=performance.getEntriesByType('navigation')[0]?.responseStatus;
      const bodyText=(document.body?.innerText||'').trim();
      const loadFailed=status>=400 || (!account && !window.WIZ_global_data && boxes.length===0 &&
        document.readyState==='complete' &&
        (!bodyText || /^(failed to load (?:the )?project|项目加载失败)[.!。]?$/i.test(bodyText)));
      return {origin:true,path:location.pathname,account,login,loadFailed,
        busy:[...document.querySelectorAll('[role=progressbar],[aria-busy=true]')].some(visible),
        overlay:[...document.querySelectorAll('[role=dialog],[aria-modal=true],flow-change-log-modal')].some(visible),
        draft:boxes.some(e=>(e.innerText||'').trim()) ||
          !!document.querySelector('flow-prompt-box button.chip-container img,flow-prompt-box .mention-chip,flow-base-prompt-box button.chip-container img,flow-base-prompt-box .mention-chip') ||
          [...document.querySelectorAll('input[type=file]')].some(e=>e.files?.length),
        editor:boxes.filter(e=>visible(e) && e.getAttribute('aria-disabled')!=='true').length===1 &&
          [...document.querySelectorAll('flow-prompt-box button mat-icon,flow-base-prompt-box button mat-icon,button.generate-icon-button mat-icon')].some(i=>i.textContent.trim()==='arrow_forward'&&visible(i))};
    })()""".replace('EXPECTED', json.dumps(email.lower()))


def page_verdict(state):
    if state.get('busy'):
        return 'busy'
    if state.get('draft'):
        return 'draft'
    if state.get('login'):
        return 'login'
    if state.get('loadFailed'):
        return 'load_failed'
    if not state.get('account'):
        return 'unconfirmed'
    if state.get('overlay'):
        return 'overlay'
    return 'ok' if state.get('editor') else 'editor'


def google_login_page(url):
    location = urlsplit(url)
    if (location.scheme, location.netloc) != ('https', 'accounts.google.com'):
        return None
    path = location.path.lower().strip('/').split('/')
    if 'signin' in path:
        return 'verification_required' if 'challenge' in path else 'login'
    if path[0] in ('servicelogin', 'accountchooser'):
        return 'login'
    return None


async def inspect_page(account, binding):
    import websockets
    profile = account['profile']
    key = account['key'].removeprefix('flow-account-')
    if binding.get('binding_state') != 'verified' or binding.get('account_key') != key:
        return 'binding'
    pid, port = binding['browser_pid'], binding['port']
    if process_identity(pid, key, port) != (profile, binding['process_start']):
        return 'binding'
    pages = await asyncio.to_thread(_pages, key, pid, port, include_non_flow=True)
    pages = [page for page in pages if page.get('id') == binding['page_target']]
    if len(pages) != 1:
        return 'binding'
    page = pages[0]
    project = canonical_project_url(binding['project_url'])
    if project.rsplit('/', 1)[1] not in account['projects']:
        return 'project'
    login_page = google_login_page(page['url'])
    if not login_page:
        try:
            if canonical_project_url(page['url']) != project:
                return 'project'
        except ValueError:
            return 'project'
    windows = await asyncio.to_thread(focus_call, 'List', pid)
    if sum(w.get('id') == binding['window_id'] and w.get('workspace') == binding['workspace'] for w in windows) != 1:
        return 'binding'
    endpoint = urlsplit(page.get('webSocketDebuggerUrl', ''))
    if (endpoint.scheme, endpoint.netloc, endpoint.path, endpoint.query, endpoint.fragment) != (
            'ws', f'127.0.0.1:{port}', '/devtools/page/' + page['id'], '', ''):
        return 'binding'
    async with websockets.connect(endpoint.geturl(), open_timeout=2, close_timeout=1, max_size=65536) as ws:
        await ws.send(json.dumps({'id': 1, 'method': 'Browser.getWindowForTarget', 'params': {'targetId': page['id']}}))
        window = json.loads(await ws.recv()).get('result', {}).get('windowId')
        if window != binding['chrome_window']:
            return 'binding'
        if login_page:
            return login_page
        # Return flags only. Do not return prompt text or account credentials.
        expression = composer_expression(account['email'])
        await ws.send(json.dumps({'id': 2, 'method': 'Runtime.evaluate',
                                  'params': {'expression': expression, 'returnByValue': True}}))
        state = json.loads(await ws.recv()).get('result', {}).get('result', {}).get('value', {})
    if not state.get('origin') or 'https://flow.google.com' + state.get('path', '').rstrip('/') != project:
        return 'project'
    return page_verdict(state)


async def probe(data):
    bindings = {b['profile']: b for b in data['bindings']}
    busy = set(data['busy'])
    async def check(account):
        profile = account['profile']
        if profile in busy:
            return profile, 'busy'
        try:
            async with asyncio.timeout(6):
                return profile, await inspect_page(account, bindings.get(profile, {}))
        except Exception:
            return profile, 'unconfirmed'
    return dict(await asyncio.gather(*(check(a) for a in data['accounts'])))


if __name__ == '__main__':
    print(json.dumps(asyncio.run(probe(json.load(sys.stdin)))))
