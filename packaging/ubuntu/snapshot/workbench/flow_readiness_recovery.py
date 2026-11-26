"""Recover idle Flow task pages without typing or generating."""
import asyncio
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from flow_browser_state import _pages, focus_call
from flow_check_readiness import FLOW_HOME_URLS, composer_expression, flow_project_redirect, google_login_page, inspect_page, page_verdict
from flow_task_window import registration
from task_window_registry import TaskWindowRegistry, canonical_project_url, process_identity

STATE = Path.home()/'.local/state/creative-unified-workbench'
CONFLICTS = {'preparing','submitting','submitted','generated','downloading','result_unknown'}
ACTIVE = {'running','dispatching'}
DISMISS_UPDATE = r"""(() => {
  if (location.origin!=='https://flow.google.com') return false;
  const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility==='visible';
  const dialogs=[...document.querySelectorAll('[role=dialog],[aria-modal=true]')].filter(visible);
  if(dialogs.length!==1) return false;
  const frames=dialogs[0].querySelectorAll('flow-change-log-modal iframe');
  if(frames.length!==1) return false;
  const u=new URL(frames[0].src);
  if(u.origin!=='https://www.gstatic.com'||!u.pathname.startsWith('/aitestkitchen/website/flow/changelogs/')) return false;
  const buttons=[...dialogs[0].querySelectorAll('button')].filter(b=>visible(b)&&!b.disabled&&/^(?:开始使用|Get started)$/i.test(b.innerText.trim()));
  if(buttons.length!==1) return false;
  buttons[0].click();
  return true;
})()"""
DISMISS_ACCOUNT_PANEL = r"""((state, path) => {
  if (!state.origin || !state.account || !state.overlay || state.busy || state.draft ||
      location.origin !== 'https://flow.google.com' || location.pathname.replace(/\/$/, '') !== path) return false;
  const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility === 'visible';
  const dialogs = [...document.querySelectorAll('[role=dialog],[aria-modal=true]')].filter(visible);
  if (dialogs.length !== 1) return false;
  const controls = [...dialogs[0].querySelectorAll('button[aria-label]')].filter(button =>
    visible(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true' &&
    /^(?:关闭账号面板|Close account panel)$/i.test(button.getAttribute('aria-label').trim()));
  if (controls.length !== 1) return false;
  controls[0].click();
  return true;
})"""


def generation_evidence(tasks, windows, now):
    """Running locally (or queued) is not evidence of provider acceptance."""
    evidence = {}
    for task in tasks:
        p = task.get('effective_profile') or task.get('profile')
        if not p or task.get('kind') not in ('image','video'):
            continue
        w = windows.get(p,{})
        attempt = ((task.get('retry') or {}).get('attempts') or [{}])[-1]
        if (task.get('status') == 'running' and w.get('task_id') and
                w['task_id'] in (task.get('id'),attempt.get('id')) and w.get('state') in ('submitted','generated','downloading')):
            evidence[p] = 'generating'
        elif evidence.get(p) != 'generating' and successful_output(task):
            finished = task.get('finished')
            if type(finished) in (int,float) and 0 <= now-finished <= 600:
                evidence[p] = 'recent_success'
    return evidence


def successful_output(task):
    if task.get('status') != 'succeeded' or task.get('kind') not in ('image','video'):
        return False
    result = task.get('result') or {}
    return bool(task.get('downloads') or any(j.get('stage') == 'generated' and j.get('media_ids')
                for j in result.get('source_jobs',[]) if isinstance(j,dict)))


def failed_without_result(task):
    """A bounded reply wait AND project lookup found no image; never authorizes replay."""
    if not task or task.get('status') != 'failed':
        return False
    if not re.search(r'no image reply within \d+ s, and no new image is in the project',str(task.get('error') or '')):
        return False
    result = task.get('result') or {}
    jobs = result.get('source_jobs') or []
    return bool(jobs) and not result.get('files') and all(
        j.get('status') == 'failed' and not j.get('media_ids') and not j.get('files') for j in jobs)


def review_hold(account, task_id, manual=False):
    import time
    from task_coordinator import Coordinator
    from shared_session_controller import state
    p = account['profile']
    s = state()
    if (any((t.get('effective_profile') or t.get('profile')) == p and t.get('status') in ACTIVE for t in s['tasks'])
            or any(t.get('profile') == p for t in s.get('remote_logins', []))):
        return False
    task = next((t for t in s['tasks'] if t.get('id') == task_id),None)
    if task and (task.get('effective_profile') or task.get('profile')) != p:
        return False
    closed_without_result = failed_without_result(task)
    if not closed_without_result and task:
        database = Path.home()/'.local/share/studiowright-evaluation/console-data/console.db'
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=2) as source:
            source.row_factory = sqlite3.Row
            row = source.execute('SELECT profile,status,error,result FROM flow_task_execution_rows WHERE root_id=? ORDER BY ordinal DESC LIMIT 1',
                                 (task_id,)).fetchone()
        if row and row['profile'] == p:
            raw = dict(row)
            raw['result'] = json.loads(raw['result'] or '{}')
            closed_without_result = failed_without_result(raw)
    if not manual and not (task and (successful_output(task) or closed_without_result)):
        return False
    if (STATE/'maintenance').exists():
        return False
    now = time.time()
    with Coordinator().connect() as db:
        db.execute('BEGIN IMMEDIATE')
        changed = db.execute("""UPDATE windows SET state='idle',stage='reviewed',updated=?
            WHERE profile=? AND task_id=? AND state='result_unknown'""", (now,p,task_id)).rowcount
        if changed:
            db.execute('INSERT INTO events(profile,task_id,action,detail,created) VALUES(?,?,?,?,?)',
                       (p,task_id,'readiness_hold_reviewed_by_operator' if manual else
                        'readiness_no_result_closed' if closed_without_result else 'readiness_output_verified','{}',now))
    return changed == 1


def operator_dispatch_paused():
    marker = STATE / 'maintenance'
    try:
        return (not marker.is_symlink() and marker.is_file()
                and marker.read_bytes() == b'operator-dispatch-pause\n')
    except OSError:
        return False


def guard_live(account):
    from shared_session_controller import state
    profile = account['profile']
    marker = STATE / 'maintenance'
    if account.get('operator_dispatch_paused'):
        if not operator_dispatch_paused():
            raise ValueError('maintenance')
    elif marker.exists() or marker.is_symlink():
        raise ValueError('maintenance')
    s = state()
    rows = [a for a in s['accounts'] if a.get('profile') == profile]
    if (len(rows) != 1 or rows[0].get('email','').lower() != account['email'].lower()
            or rows[0].get('state') != 'verified' or rows[0].get('risk_paused') is not False
            or rows[0].get('generation_state') != 'not_cooling_down'):
        raise ValueError('account_controls_changed')
    if (any((t.get('effective_profile') or t.get('profile')) == profile and t.get('status') in ACTIVE
            for t in s['tasks']) or any(x.get('profile') == profile for x in s.get('remote_logins', []))):
        raise ValueError('account_busy')
    coord = s.get('coordination') or {}
    if any(w.get('profile') == profile and w.get('state') in CONFLICTS for w in coord['windows']):
        raise ValueError('account_busy_or_review')
    with sqlite3.connect((STATE/'real-pool.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=2) as db:
        if (db.execute("SELECT 1 FROM flow_requests WHERE profile=? AND status IN ('dispatching','unknown')",(profile,)).fetchone()
                or db.execute("SELECT 1 FROM flow_auto_projects WHERE profile=? AND status IN ('creating','unknown')",(profile,)).fetchone()):
            raise ValueError('account_busy_or_review')
    expected = 'FLOW+'+account['email'].split('@',1)[0].lower()
    rows = {x['id']:x.get('title','') for x in s['projects'] if x.get('profile') == profile}
    projects = set(rows)
    standard = {ident for ident,title in rows.items() if title.strip().lower() == expected.lower()}
    # Flow's legacy registry may still call a valid project “untitled”. The
    # workbench already keeps observed names, keyed by exact account and UUID.
    # Names never give access to an unregistered project or another account.
    names = STATE/'project-names.sqlite3'
    if names.is_file() and not names.is_symlink():
        with sqlite3.connect(names.as_uri()+'?mode=ro',uri=True,timeout=2) as db:
            for ident,title in db.execute("SELECT project,title FROM names WHERE profile=? AND source IN ('open_flow_tab','browser_history')",(profile,)):
                if rows.get(ident) in ('','未命名项目') and title.strip().lower()==expected.lower():
                    standard.add(ident)
    return projects, standard


def _discover_chrome(profile):
    """Dynamic window discovery: scan /proc for this profile's main chrome with a
    debugging port, replacing a stale or missing registry binding."""
    import shlex
    best = None
    for proc in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = [a.decode(errors='replace') for a in proc.read_bytes().split(b'\0') if a]
        except OSError:
            continue
        if len(args) == 1:
            try: args = shlex.split(args[0])
            except ValueError: continue
        if any(x.startswith('--type=') for x in args):
            continue
        profiles = [x.split('=', 1)[1] for x in args if x.startswith('--user-data-dir=')]
        ports = [x.split('=', 1)[1] for x in args if x.startswith('--remote-debugging-port=')]
        if len(profiles) != 1 or len(ports) != 1 or Path(profiles[0]).name != profile:
            continue
        best = (int(proc.parts[-2]), int(ports[0]))
    return best


async def candidate(account):
    key = account['key'].removeprefix('flow-account-')
    profile = account['profile']
    r = await asyncio.to_thread(registration,key)
    pid = port = stamp = None
    if r:
        pid,port = r['pid'],r['port']
        try:
            profile_check,stamp = process_identity(pid,key,port)
        except ValueError:
            profile_check = None
        if profile_check != profile:
            r = None
    if not r:
        # Stale or missing registry binding (window stopped/reopened): discover
        # the account's current window dynamically. The user-data-dir basename
        # is the account identity, so no registry entry is needed to re-attach.
        found = await asyncio.to_thread(_discover_chrome, profile)
        if not found:
            raise ValueError('binding')
        pid,port = found
        profile_check,stamp = process_identity(pid,key,port)
        if profile_check != profile:
            raise ValueError('binding')
    pages = await asyncio.to_thread(_pages,key,pid,port)
    windows = await asyncio.to_thread(focus_call,'List',pid)
    if len(pages) != 1 or len(windows) != 1:
        known = TaskWindowRegistry().get(key)
        if (not known or known.get('browser_pid')!=pid or known.get('process_start')!=stamp or known.get('port')!=port):
            raise ValueError('unique')
        pages = [p for p in pages if p.get('id')==known.get('page_target')]
        windows = [w for w in windows if w.get('id')==known.get('window_id') and w.get('workspace')==known.get('workspace')]
        if len(pages)!=1 or len(windows)!=1:
            raise ValueError('unique')
    page,win = pages[0],windows[0]
    endpoint = urlsplit(page.get('webSocketDebuggerUrl',''))
    if (endpoint.scheme,endpoint.netloc,endpoint.path,endpoint.query,endpoint.fragment) != (
            'ws',f'127.0.0.1:{port}','/devtools/page/'+page['id'],'',''):
        raise ValueError('binding')
    return {'account_key':key,'profile':profile,'browser_pid':pid,'process_start':stamp,'port':port,
            'page_target':page['id'],'window_id':win['id'],'workspace':win['workspace']},page,endpoint.geturl()


async def command(ws, method, params=None):
    await ws.send(json.dumps({'id':1,'method':method,'params':params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get('id') == 1:
            if 'error' in msg or 'result' not in msg:
                raise ValueError('browser_command_failed')
            return msg['result']


async def evaluate(ws, expression):
    raw = await command(ws,'Runtime.evaluate',{'expression':expression,'returnByValue':True})
    return raw.get('result',{}).get('value',{})


async def check_duplicate(account, binding, page, remaining=None):
    """Close only a proven empty copy or unused Flow home, after fresh guards."""
    import websockets

    port = binding['port']
    target = page['id']
    endpoint = f'ws://127.0.0.1:{port}/devtools/page/{target}'
    if page.get('webSocketDebuggerUrl') != endpoint or target == binding['page_target']:
        return False
    async with websockets.connect(endpoint, open_timeout=2, close_timeout=1, max_size=65536) as ws:
        if remaining is not None:
            await asyncio.to_thread(guard_live, account)
            if TaskWindowRegistry().get(binding['account_key']) != binding:
                return False
            tabs = await asyncio.to_thread(_pages, binding['account_key'], binding['browser_pid'], port)
            if {p['id']: p['url'] for p in tabs} != remaining:
                return False
            if await inspect_page(account, binding) != 'unique':
                return False
        state = await evaluate(ws, composer_expression(account['email']))
        if not state or not state.get('origin'):
            return False
        verdict = page_verdict(state)
        if page['url'] in FLOW_HOME_URLS:
            empty = (verdict == 'editor' and state.get('account') is True
                     and state.get('emptyHome') is True and state.get('path') == '/')
        else:
            empty = verdict == 'ok' and state.get('path', '').rstrip('/') == urlsplit(binding['project_url']).path
        if not empty:
            return False
        if remaining is not None:
            await command(ws, 'Page.close')
    return True


async def reconcile_fixed_window(account, binding, actions):
    """Keep the verified binding, remove empty copies, and record a fresh count."""
    from flow_window_policy import Lifecycle
    from task_coordinator import Coordinator

    await asyncio.to_thread(guard_live, account)
    if TaskWindowRegistry().get(binding['account_key']) != binding:
        return 'binding'
    verdict = await inspect_page(account, binding)
    if verdict not in ('ok', 'unique'):
        return verdict
    key, pid, port = binding['account_key'], binding['browser_pid'], binding['port']
    tabs = await asyncio.to_thread(_pages, key, pid, port)
    fixed = binding['page_target']
    if fixed not in {p['id'] for p in tabs}:
        return 'binding'
    try:
        if any(p['url'] not in FLOW_HOME_URLS and
               canonical_project_url(p['url']) != binding['project_url'] for p in tabs):
            return 'unique'
    except ValueError:
        return 'unique'
    duplicates = [page for page in tabs if page['id'] != fixed]
    for page in duplicates:
        if not await check_duplicate(account, binding, page):
            return 'unique'

    remaining = {page['id']: page['url'] for page in tabs}
    for page in duplicates:
        if not await check_duplicate(account, binding, page, remaining):
            return 'unique'
        remaining.pop(page['id'])
        for _ in range(15):
            current = await asyncio.to_thread(_pages, key, pid, port)
            if {p['id']: p['url'] for p in current} == remaining:
                break
            await asyncio.sleep(.1)
        else:
            return 'unique'
        if 'close_duplicate' not in actions:
            actions.append('close_duplicate')

    verdict = await inspect_page(account, binding)
    if verdict != 'ok':
        return verdict
    await asyncio.to_thread(guard_live, account)
    if TaskWindowRegistry().get(key) != binding:
        return 'binding'
    Lifecycle().observe(account['profile'], 1, 1)
    if not duplicates:
        actions.append('recheck_window')
    Coordinator().event(account['profile'], None, 'readiness_recovered', actions=actions)
    return 'ok'


async def close_account_panel(ws, account, expected, project, before, actions):
    await asyncio.to_thread(guard_live, account)
    project_path = json.dumps(urlsplit(project).path)
    if await evaluate(ws, f'{DISMISS_ACCOUNT_PANEL}({expected}, {project_path})') is not True:
        return before
    actions.append('dismiss_account_panel')
    await asyncio.sleep(.3)
    return await evaluate(ws, expected)


async def reload_overlay(ws, account, expected, project, actions):
    """Refresh the same idle project once before reporting its overlay."""
    await asyncio.to_thread(guard_live, account)
    frame = (await command(ws, 'Page.getFrameTree'))['frameTree']['frame']
    before = await evaluate(ws, expected)
    if (canonical_project_url(frame['url']) != project
            or 'https://flow.google.com' + before.get('path', '').rstrip('/') != project
            or not before.get('origin') or not before.get('account')
            or before.get('busy') or before.get('draft') or not before.get('overlay')):
        return before
    await asyncio.wait_for(command(ws, 'Page.reload', {'loaderId': frame['loaderId']}), timeout=3)
    actions.append('reload')
    for _ in range(16):
        await asyncio.sleep(.35)
        loaded = (await command(ws, 'Page.getFrameTree'))['frameTree']['frame']
        if loaded['loaderId'] == frame['loaderId']:
            continue
        if canonical_project_url(loaded['url']) != project:
            return {}
        status = await evaluate(ws, expected)
        if status.get('account') and (status.get('editor') or status.get('overlay')):
            return status
    return {}


def restart_managed(binding, allow_responsive=False):
    """Never stop a process just because its port stopped answering."""
    p = binding['profile']
    unit = 'studiowright-browser-'+p+'.service'
    systemctl = ['systemctl','--user']
    pid = subprocess.check_output(systemctl+['show',unit,'-p','MainPID','--value'],text=True,timeout=3).strip()
    if pid != str(binding['browser_pid']) or process_identity(int(pid),binding['account_key'],binding['port']) != (p,binding['process_start']):
        raise ValueError('restart_not_safe')
    try:
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{binding['port']}/json/version",timeout=2):
            pass
    except (OSError,TimeoutError):
        pass
    else:
        if not allow_responsive:
            raise ValueError('browser_still_responding')
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{binding['port']}/json/list",timeout=2) as response:
            tabs = [x for x in json.load(response) if x.get('type') == 'page']
        windows = focus_call('List',binding['browser_pid'])
        if (len(tabs)!=1 or tabs[0].get('id')!=binding['page_target'] or len(windows)!=1
                or windows[0].get('id')!=binding['window_id']):
            raise ValueError('restart_not_safe')
    subprocess.run(systemctl+['restart',unit],check=True,capture_output=True,timeout=12)


async def reload_fixed_project(ws, account, expected, project, actions, issue, *, preserve_editing=False):
    if issue == 'busy':
        # A progress indicator may belong to a generation started directly in Flow.
        for _ in range(4):
            if page_verdict(await evaluate(ws, expected)) != 'busy':
                break
            await asyncio.sleep(.5)
    await asyncio.to_thread(guard_live, account)
    frame = (await command(ws, 'Page.getFrameTree'))['frameTree']['frame']
    state = await evaluate(ws, expected)
    project_path = urlsplit(project).path
    if (canonical_project_url(frame['url']) != project or not state.get('origin')
            or state.get('path', '').rstrip('/') != project_path):
        return {}
    if (state.get('overlay') or page_verdict(state) != ('ok' if issue == 'busy' else issue)
            or (issue == 'draft' and (not state.get('account') or (preserve_editing and state.get('editing'))))):
        return state
    await asyncio.wait_for(command(ws, 'Page.reload', {'loaderId': frame['loaderId']}), timeout=3)
    actions.append('reset_draft' if issue == 'draft' else 'reload')
    for _ in range(20):
        await asyncio.sleep(.4)
        loaded = (await command(ws, 'Page.getFrameTree'))['frameTree']['frame']
        login = google_login_page(loaded['url'])
        if login:
            return {'login': True, 'verification': login == 'verification_required'}
        if flow_project_redirect(loaded['url'], project):
            return {'flow_landing': True}
        if canonical_project_url(loaded['url']) != project:
            return {}
        if loaded['loaderId'] == frame['loaderId']:
            continue
        state = await evaluate(ws, expected)
        if not state.get('origin') or state.get('path', '').rstrip('/') != project_path:
            return {}
        if page_verdict(state) not in ('load_failed', 'unconfirmed', 'editor'):
            return state
    return state


async def reopen_redirected_project(ws, account, expected, project, actions):
    await asyncio.to_thread(guard_live, account)
    frame = (await command(ws, 'Page.getFrameTree'))['frameTree']['frame']
    if not flow_project_redirect(frame['url'], project):
        return {}
    state = await evaluate(ws, expected)
    if not state.get('origin') or state.get('busy') or state.get('draft'):
        return state
    current = (await command(ws, 'Page.getFrameTree'))['frameTree']['frame']
    if current.get('loaderId') != frame.get('loaderId') or current['url'] != frame['url']:
        return {}
    result = await command(ws, 'Page.navigate', {'url': project, 'frameId': frame['id']})
    actions.append('return_to_project')
    if result.get('errorText'):
        return {'loadFailed': True}
    for _ in range(16):
        await asyncio.sleep(.5)
        loaded = (await command(ws, 'Page.getFrameTree'))['frameTree']['frame']
        if loaded.get('loaderId') == frame.get('loaderId'):
            continue
        login = google_login_page(loaded['url'])
        if login:
            return {'login': True, 'verification': login == 'verification_required'}
        if flow_project_redirect(loaded['url'], project):
            return {'flow_landing': True}
        try:
            if canonical_project_url(loaded['url']) != project:
                return {}
        except ValueError:
            return {}
        state = await evaluate(ws, expected)
        if state.get('origin') and state.get('path', '').rstrip('/') == urlsplit(project).path:
            if page_verdict(state) not in ('unconfirmed', 'editor'):
                return state
    return state


async def repair(account, initial, actions, safe_only=False, reset_draft=False, auto_reset_draft=False):
    import websockets
    from flow_window_policy import Lifecycle
    from task_coordinator import Coordinator
    projects, standard = await asyncio.to_thread(guard_live,account)
    current, page, endpoint = await candidate(account)
    expected = composer_expression(account['email'])
    try:
        project = canonical_project_url(page['url'])
    except ValueError:
        project = None
    old = TaskWindowRegistry().get(current['account_key'])
    redirected = bool(old and old.get('binding_state') == 'verified'
                      and old.get('project_url', '').rsplit('/', 1)[-1] in projects
                      and flow_project_redirect(page['url'], old['project_url']))
    if redirected:
        project = old['project_url']
    same = project and project.rsplit('/',1)[1] in projects
    if not same and len(standard) != 1:
        return 'project',None
    desired = project if same else canonical_project_url('https://flow.google.com/project/'+next(iter(standard)))
    restart_safe = False
    navigated_once = False
    try:
        async with websockets.connect(endpoint,open_timeout=2,close_timeout=1,max_size=65536) as ws:
            current['chrome_window'] = (await command(ws,'Browser.getWindowForTarget',{'targetId':current['page_target']}))['windowId']
            if redirected:
                if any(old.get(key) != value for key, value in current.items()):
                    return 'binding', None
                before = await reopen_redirected_project(ws, account, expected, desired, actions)
                navigated_once = 'return_to_project' in actions
            else:
                before = await evaluate(ws,expected)
            verdict = page_verdict(before)
            if verdict in ('login', 'verification_required', 'flow_landing') or (redirected and verdict != 'ok'):
                return verdict,None
            auto_draft = auto_reset_draft and initial == 'draft'
            if verdict == 'load_failed' or (verdict == 'draft' and (reset_draft or auto_draft)) or (
                    initial == 'busy' and verdict in ('busy', 'ok')):
                if (not same or not old or old.get('binding_state') != 'verified'
                        or old.get('project_url') != project or any(old.get(k)!=v for k,v in current.items())):
                    return 'binding',None
                issue = 'busy' if initial == 'busy' and verdict in ('busy','ok') else verdict
                before = await reload_fixed_project(ws, account, expected, project, actions, issue,
                                                    preserve_editing=auto_draft)
                verdict = page_verdict(before)
                if verdict != 'ok':
                    return verdict,None
                navigated_once = True
            if safe_only and (not before.get('origin') or not before.get('account') or before.get('busy')):
                return page_verdict(before),None
            if before.get('draft'):
                return 'draft',None
            if (not before.get('origin') or not before.get('account')) and not navigated_once:
                # Auto-navigation: a stray interstitial (Google bar, account picker)
                # is brought back to the canonical project once per pass.
                navigated_once = True
                await asyncio.to_thread(guard_live,account)
                frame = (await command(ws,'Page.getFrameTree'))['frameTree']['frame']
                await command(ws,'Page.navigate',{'url':desired,'frameId':frame['id']})
                await asyncio.sleep(3)
                for _ in range(10):
                    await asyncio.sleep(.5)
                    frame = (await command(ws,'Page.getFrameTree'))['frameTree']['frame']
                    if urlsplit(frame['url']).path == urlsplit(desired).path:
                        break
                before = await evaluate(ws,expected)
            if not before.get('origin') or not before.get('account'):
                return page_verdict(before),None
            if before.get('draft'):
                return 'draft',None
            if (before.get('overlay') and not before.get('busy')
                    and same and old and old.get('binding_state') == 'verified'
                    and old.get('project_url') == project and all(old.get(k) == v for k,v in current.items())):
                before = await close_account_panel(ws, account, expected, project, before, actions)
            if same and before.get('overlay') and not before.get('busy') and 'reload' not in actions:
                before = await reload_overlay(ws, account, expected, project, actions)
                navigated_once = navigated_once or 'reload' in actions
                if not before:
                    return 'unconfirmed',None
            if before.get('overlay') and not before.get('busy'):
                await asyncio.to_thread(guard_live,account)
                latest = await evaluate(ws,expected)
                if (latest.get('account') and latest.get('path')==before.get('path') and not latest.get('draft')
                        and not latest.get('busy') and await evaluate(ws,DISMISS_UPDATE) is True):
                    actions.append('dismiss_update')
                    await asyncio.sleep(.3)
                    before = await evaluate(ws,expected)
            if before.get('draft'):
                return 'draft',None
            if before.get('busy'):
                return 'busy',None
            if before.get('overlay'):
                if not navigated_once:
                    navigated_once = True
                    await asyncio.to_thread(guard_live,account)
                    frame = (await command(ws,'Page.getFrameTree'))['frameTree']['frame']
                    await command(ws,'Page.navigate',{'url':desired,'frameId':frame['id']})
                    await asyncio.sleep(3)
                    before = await evaluate(ws,expected)
                if before.get('overlay') or before.get('busy'):
                    return ('overlay' if before.get('overlay') else 'busy'),None
            if not before.get('account') or before.get('draft') or before.get('busy'):
                return page_verdict(before),None
            if not same or not before.get('editor'):
                if same and (initial == 'after_restart' or 'reload' in actions):
                    return 'editor',None
                await asyncio.to_thread(guard_live,account)
                frame = (await command(ws,'Page.getFrameTree'))['frameTree']['frame']
                # Match the same document before navigating or refreshing it.
                if urlsplit(frame['url']).path != before['path'] or urlsplit(frame['url']).netloc != 'flow.google.com':
                    return 'binding',None
                before = await evaluate(ws,expected)
                if (not before.get('account') or before.get('draft') or before.get('overlay') or before.get('busy')
                        or before.get('path') != urlsplit(frame['url']).path):
                    return 'unconfirmed',None
                if same:
                    port = current['port']
                    with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}/json/list',timeout=2) as response:
                        tabs = [x for x in json.load(response) if x.get('type') == 'page']
                    restart_safe = (len(tabs)==1 and tabs[0].get('id')==current['page_target'])
                    actions.append('reload')
                    await asyncio.wait_for(command(ws,'Page.reload',{'loaderId':frame['loaderId']}),timeout=3)
                    restart_safe = False  # Acknowledged navigation must finish; do not restart it.
                else:
                    actions.append('open_project')
                    result = await command(ws,'Page.navigate',{'url':desired,'frameId':frame['id']})
                    if result.get('errorText'):
                        return 'project',None
    except (OSError,TimeoutError,websockets.WebSocketException):
        if not restart_safe:
            return 'unconfirmed',None
        await asyncio.to_thread(guard_live,account)
        await asyncio.to_thread(restart_managed,current)
        actions.append('restart')
        for _ in range(12):
            await asyncio.sleep(.5)
            try:
                current,page,_ = await candidate(account)
                break
            except (OSError,ValueError):
                continue
        else:
            return 'binding',None
        # The restarted browser may reopen the homepage. Reuse the same safety
        # checks in a second round; never perform a second reload or restart.
        return await repair(account,'after_restart',actions,safe_only=safe_only)
    provisional = dict(current,project_url=desired,binding_state='verified')
    account = dict(account,projects=projects)
    verdict = 'unconfirmed'
    checked_overlay = 'dismiss_update' in actions
    probes = 12 if actions else 1
    if account.get('project_prepared') and 'open_project' in actions:
        probes = 32
    for _ in range(probes):
        if actions:
            await asyncio.sleep(.35)
        try:
            verdict = await inspect_page(account,provisional)
        except (OSError,ValueError,websockets.WebSocketException):
            continue
        if verdict == 'ok':
            break
        if verdict == 'overlay' and not checked_overlay:
            checked_overlay = True
            await asyncio.to_thread(guard_live,account)
            async with websockets.connect(endpoint,open_timeout=2,close_timeout=1,max_size=65536) as ws:
                now = await evaluate(ws,expected)
                if (now.get('account') and 'https://flow.google.com'+now.get('path','').rstrip('/')==desired
                        and not now.get('draft') and not now.get('busy') and await evaluate(ws,DISMISS_UPDATE) is True):
                    actions.append('dismiss_update')
    if (verdict in {'editor','project','binding','unconfirmed'} and 'restart' not in actions
            and any(action in actions for action in ('reload','open_project'))):
        # The operator authorized refresh/restart of this exact idle task window.
        # Recheck the live account and page before escalating beyond a refresh.
        await asyncio.to_thread(guard_live,account)
        async with websockets.connect(endpoint,open_timeout=2,close_timeout=1,max_size=65536) as ws:
            last = await evaluate(ws,expected)
        if not last.get('account') or not last.get('origin') or last.get('busy') or last.get('draft'):
            return verdict,None
        await asyncio.to_thread(restart_managed,current,allow_responsive=True)
        actions.append('restart')
        for _ in range(12):
            await asyncio.sleep(.5)
            try:
                await candidate(account)
                return await repair(account,'after_restart',actions,safe_only=safe_only)
            except (OSError,ValueError):
                continue
        return 'binding',None
    if verdict != 'ok':
        return verdict,None
    await asyncio.to_thread(guard_live,account)
    TaskWindowRegistry().bind(**current,project_url=desired)
    if old is None or any(old.get(k)!=v for k,v in provisional.items()):
        actions.append('rebind')
    life = Lifecycle()
    life.observe(account['profile'],1,1)
    coordinator = Coordinator()
    coordinator.release_verified(account['profile'])
    # Clear only the stale draft hold for the exact page that passed inspection.
    # Keep its previous task identifier and never release an unknown result.
    import time
    from contextlib import closing
    with closing(coordinator.connect()) as db, db:
        db.execute("""UPDATE windows SET state='idle',updated=?
            WHERE profile=? AND target_id=? AND state='blocked_draft' AND stage='preparing'""",
                   (time.time(),account['profile'],current['page_target']))
    coordinator.event(account['profile'],None,'readiness_recovered',actions=actions)
    return 'ok',provisional


async def recover(payload):
    from filelock import FileLock, Timeout
    from shared_session_controller import flow_runtime
    from flow_window_policy import Lifecycle
    from task_coordinator import Coordinator
    _, paths = flow_runtime()
    busy = set(payload['busy'])
    reset_draft = payload.get('reset_draft') is True
    # Background recovery explicitly disables the manual reset. Maintenance
    # uses the same flag for a balance-only check.
    auto_reset_draft = (payload.get('safe_only') is True and payload.get('reset_draft') is False
                        and not payload.get('operator_dispatch_paused'))
    bindings = {b['profile']:b for b in payload['bindings']}
    lifecycle = {w['profile']:w for w in Lifecycle().status()}
    out = {}
    deadline = asyncio.get_running_loop().time() + 140
    for account in payload['accounts']:
        if payload.get('operator_dispatch_paused'):
            account = dict(account, operator_dispatch_paused=True)
        if asyncio.get_running_loop().time() >= deadline - 2:
            break
        p = account['profile']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',p):
            continue
        if p in busy:
            continue
        if payload.get('safe_only') and not account.get('repairable'):
            continue
        actions = []
        try:
            if account['repairable'] and not account.get('review_task') and not payload.get('manual_review'):
                unit = 'studiowright-browser-'+p+'.service'
                status = subprocess.check_output(['systemctl','--user','show',unit,'-p','ActiveState','--value'],
                                                 text=True,timeout=3).strip()
                if status in {'inactive','failed'} and await asyncio.to_thread(_discover_chrome, p) is None:
                    await asyncio.to_thread(guard_live,account)
                    from activation_browser import main as open_flow
                    try:
                        await asyncio.to_thread(open_flow,account['key'].removeprefix('flow-account-'),flow_only_start=True)
                        actions.append('open_browser')
                    except ValueError as exc:
                        if str(exc)!='flow_browser_already_running':
                            raise
            with FileLock(str(paths.locks_dir()/(p+'.lock')),thread_local=False).acquire(timeout=0):
                if account.get('review_task'):
                    released = review_hold(account,account['review_task'],manual=payload.get('manual_review') is True)
                    if released:
                        actions.append('release_review')
                    if payload.get('manual_review') or not released:
                        out[p] = {'page':'unconfirmed','actions':actions}
                        continue
                if not account['repairable']:
                    continue
                binding = bindings.get(p, {})
                try:
                    before = await asyncio.wait_for(inspect_page(account,binding),timeout=5)
                except (OSError,ValueError,TimeoutError):
                    before = 'binding'
                old_draft = before == 'ok' and (Coordinator().window(p) or {}).get('state') == 'blocked_draft'
                if before == 'busy' or (before == 'ok' and account.get('refresh_after_busy')):
                    verdict,_ = await asyncio.wait_for(repair(account,'busy',actions,safe_only=True),
                        timeout=min(45,max(1,deadline-asyncio.get_running_loop().time())))
                elif before == 'unique' or (before == 'ok' and not old_draft and lifecycle.get(p,{}).get('unique_ok') != 1):
                    verdict = await asyncio.wait_for(reconcile_fixed_window(account,binding,actions),
                        timeout=min(45,max(1,deadline-asyncio.get_running_loop().time())))
                elif (before in {'binding','project','editor','overlay','load_failed','flow_landing'} or old_draft
                      or (before == 'draft' and (reset_draft or auto_reset_draft))):
                    verdict,_ = await asyncio.wait_for(repair(account,before,actions,
                        safe_only=payload.get('safe_only') is True,reset_draft=reset_draft,auto_reset_draft=auto_reset_draft),
                        timeout=min(45,max(1,deadline-asyncio.get_running_loop().time())))
                else:
                    verdict = before
        except Timeout:
            verdict = 'busy'
        except (OSError,ValueError,TypeError,KeyError,TimeoutError,sqlite3.Error,subprocess.SubprocessError):
            verdict = 'unconfirmed'
        if actions and verdict != 'ok':
            Coordinator().event(p,None,'readiness_recovery_needs_review',actions=actions,verdict=verdict)
        out[p] = {'page':verdict,'actions':actions}
    if payload.get('operator_dispatch_paused') and operator_dispatch_paused():
        from account_insights import Insights
        from balance_reader import read_balance
        from console_store import Store
        from playwright.async_api import async_playwright
        root = Path.home() / '.local/share/studiowright-evaluation'
        store = Store(root / 'console-data')
        insights = Insights(store, root / 'data/studiowright.db')
        async with async_playwright() as pw:
            async def refresh(account):
                profile = account['profile']
                if out.get(profile, {}).get('page') != 'ok' or not operator_dispatch_paused():
                    return
                try:
                    await asyncio.to_thread(guard_live, account)
                    balance = await asyncio.wait_for(read_balance(store.account(profile), pw, include_details=True), timeout=20)
                    insights.record(profile, balance['balance'], next_credit_date=balance.get('next_credit_date'))
                except (OSError, ValueError, TimeoutError, sqlite3.Error, subprocess.SubprocessError):
                    pass
            await asyncio.gather(*(refresh(dict(a, operator_dispatch_paused=True)) for a in payload['accounts']))
    return out


if __name__ == '__main__':
    print(json.dumps(asyncio.run(recover(json.load(sys.stdin)))))
