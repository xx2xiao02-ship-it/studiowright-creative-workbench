"""Flow window inventory and focus bridge. Never create or arrange windows."""
import ast
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from urllib.request import build_opener, ProxyHandler

FOCUS_BUS = 'local.studiowright.Foreground'
FOCUS_PATH = '/local/studiowright/Foreground'
# Retain the existing registry and its history; no account database migration.
REGISTRY = Path.home()/'.local/state/creative-unified-workbench/account-desktops.sqlite3'


def gui_env():
    env = os.environ.copy()
    for line in subprocess.check_output(['systemctl', '--user', 'show-environment'], text=True).splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            env[k] = v
    return env


def focus_call(method, *args):
    if method not in ('Status', 'List', 'Activate'):
        raise ValueError('unsupported_window_operation')
    result = subprocess.run(['gdbus', 'call', '--session', '--dest', FOCUS_BUS,
        '--object-path', FOCUS_PATH, '--method', FOCUS_BUS+'.'+method,
        *[str(a) for a in args]], env=gui_env(), capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise ValueError('desktop_component_unavailable')
    value = ast.literal_eval(result.stdout.strip())[0]
    return json.loads(value) if isinstance(value, str) else value


@contextmanager
def registry():
    REGISTRY.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(REGISTRY, timeout=5)
    db.execute('CREATE TABLE IF NOT EXISTS layouts(account_key TEXT PRIMARY KEY,status TEXT NOT NULL,changed REAL NOT NULL,reason TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS flow_windows(account_key TEXT PRIMARY KEY,pid INTEGER NOT NULL,window_id INTEGER NOT NULL,port INTEGER)')
    if 'port' not in {r[1] for r in db.execute('PRAGMA table_info(flow_windows)')}:
        db.execute('ALTER TABLE flow_windows ADD COLUMN port INTEGER')
    try:
        with db:
            yield db
    finally:
        db.close()


def mark(key, status, reason=None):
    with registry() as db:
        db.execute('INSERT INTO layouts VALUES(?,?,?,?) ON CONFLICT(account_key) DO UPDATE SET status=excluded.status,changed=excluded.changed,reason=excluded.reason',
                   (key, status, time.time(), reason))


def _pages(key, pid, port, *, include_non_flow=False):
    from shared_session_controller import process_args
    args = process_args(str(pid))
    profiles = [a.split('=', 1)[1] for a in args if a.startswith('--user-data-dir=')]
    ports = [a.split('=', 1)[1] for a in args if a.startswith('--remote-debugging-port=')]
    if (len(profiles) != 1 or ports != [str(port)] or
            any(a.startswith('--type=') for a in args) or
            hashlib.sha256(Path(profiles[0]).name.encode()).hexdigest() != key):
        raise ValueError('browser_identity_unconfirmed')
    with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}/json/list', timeout=3) as response:
        raw = response.read(4*1024*1024+1)
    if len(raw) > 4*1024*1024:
        raise ValueError('browser_inventory_too_large')
    return [p for p in json.loads(raw) if p.get('type') == 'page' and
            (include_non_flow or p.get('url', '').startswith('https://flow.google.com/'))]


def registered_authorization_window(key):
    """Read the login-verified Chrome registration for a single native window."""
    from contextlib import closing
    from task_window_registry import process_identity

    if not REGISTRY.is_file() or REGISTRY.is_symlink():
        return None
    try:
        with closing(sqlite3.connect(REGISTRY.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            row = db.execute('SELECT pid,port FROM flow_windows WHERE account_key=?', (key,)).fetchone()
        if not row:
            return None
        previous_pid, port = row
        if type(port) is not int or not 1 <= port <= 65535:
            return None
        pid = _live_browser(key, previous_pid, port)
        profile, process_start = process_identity(pid, key, port)
        pages = _pages(key, pid, port)
        windows = focus_call('List', pid)
        if not pages or len(windows) != 1:
            return None
        window_id = windows[0].get('id')
        target = pages[0].get('id')
        if type(window_id) is not int or window_id <= 0 or not isinstance(target, str) or not target:
            return None
        return {'browser_pid':pid, 'port':port, 'profile':profile, 'process_start':process_start,
                'page_target':target, 'window_id':window_id}
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        return None


def flow_only_ready(key):
    with registry() as db:
        row = db.execute('SELECT pid,port FROM flow_windows WHERE account_key=?', (key,)).fetchone()
    if not row or not row[1]:
        return False
    try:
        return bool(_pages(key, _live_browser(key,row[0],row[1]),row[1]))
    except (OSError, ValueError):
        return False


def _live_browser(key, previous_pid, port):
    """Resolve a restarted browser by exact profile hash and debugging port."""
    from shared_session_controller import process_args
    found=[]
    for entry in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args=process_args(entry.parent.name)
            profiles=[a.split('=',1)[1] for a in args if a.startswith('--user-data-dir=')]
            if (len(profiles)==1 and hashlib.sha256(Path(profiles[0]).name.encode()).hexdigest()==key
                    and '--remote-debugging-port='+str(port) in args
                    and not any(a.startswith('--type=') for a in args)):
                found.append(int(entry.parent.name))
        except (OSError,ValueError,UnicodeError):
            continue
    if len(found)!=1:
        raise ValueError('browser_identity_unconfirmed')
    return found[0]


def _registered_pages(key, pid, port, window_id):
    """Bind tabs to the saved Chrome window, never to a second user window."""
    pages = _pages(key, pid, port)
    async def read():
        import websockets
        with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}/json/version', timeout=3) as response:
            endpoint = json.load(response)['webSocketDebuggerUrl']
        if not endpoint.startswith(f'ws://127.0.0.1:{port}/'):
            raise ValueError('browser_identity_unconfirmed')
        selected=[];groups={}
        async with websockets.connect(endpoint, open_timeout=3, close_timeout=1, max_size=1024*1024) as ws:
            for counter, page in enumerate(pages, 1):
                await ws.send(json.dumps({'id': counter, 'method': 'Browser.getWindowForTarget', 'params': {'targetId': page['id']}}))
                async with asyncio.timeout(3):
                    while True:
                        response = json.loads(await ws.recv())
                        if response.get('id') == counter:
                            break
                actual=response.get('result', {}).get('windowId')
                if type(actual) is not int:raise ValueError('browser_window_unconfirmed')
                groups.setdefault(actual,[]).append(page)
                if actual == window_id:
                    selected.append(page)
        if selected:return selected
        if window_id is None and len(groups)==1:return next(iter(groups.values()))
        raise ValueError('flow_window_ambiguous')
    return asyncio.run(read())


def bindings(account_key=None):
    """Derive the current workspace; no persistent workspace or fixed size."""
    with registry() as db:
        query = 'SELECT account_key,pid,port,window_id FROM flow_windows'
        rows = db.execute(query+(' WHERE account_key=?' if account_key else ''),
                          (account_key,) if account_key else ()).fetchall()
    result = []
    for key, pid, port, window_id in rows:
        try:
            live_pid=_live_browser(key,pid,port)
            if live_pid!=pid:window_id=None
            pid=live_pid
            titles = {p.get('title') for p in _registered_pages(key, pid, port, window_id) if p.get('title')}
            names = {name for title in titles for name in (title, title+' - Google Chrome', title+' - Chromium')}
            windows = [w for w in focus_call('List', pid) if w.get('title') in names]
            if len(windows) != 1 or type(windows[0].get('workspace')) is not int or windows[0]['workspace'] < 0:
                raise ValueError('flow_window_ambiguous')
            result.append({'key': key, 'pid': pid, 'workspace': windows[0]['workspace'],
                           'window_id': windows[0]['id']})
        except Exception as exc:
            result.append({'key': key, 'pid': pid, 'error': str(exc)[:100]})
    return result


def call(method, *args):
    """Compatibility for the already loaded Flow package; read operations only."""
    if method == 'Status' and not args:
        return bindings()
    if method == 'List' and len(args) == 1:
        return focus_call('List', *args)
    raise ValueError('unsupported_window_operation')


async def register_flow(key, pid, port, email):
    """Verify the existing Flow identity, then register its CDP window."""
    from playwright.async_api import async_playwright
    from adapter import READ_STATE
    _pages(key, pid, port)
    recovery = os.environ.get('STUDIOWRIGHT_DESKTOP_RECOVERY') == '1'
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f'http://127.0.0.1:{port}',
            timeout=30000 if recovery else 4000, no_defaults=True)
        session = await browser.new_browser_cdp_session()
        for _ in range(360 if recovery else 32):
            pages = [p for cx in browser.contexts for p in cx.pages if p.url.startswith('https://flow.google.com/')]
            try:
                states = [await p.evaluate(READ_STATE) for p in pages]
                if any(isinstance(s, dict) and s.get('signed_in') and s.get('emails') == [email.lower()] for s in states):
                    break
            except Exception:
                pass
            await asyncio.sleep(.25)
        else:
            raise ValueError('flow_identity_unconfirmed')
        targets = (await session.send('Target.getTargets'))['targetInfos']
        groups = set()
        for target in targets:
            if target.get('type') == 'page' and target.get('url', '').startswith('https://flow.google.com/'):
                groups.add((await session.send('Browser.getWindowForTarget', {'targetId': target['targetId']}))['windowId'])
        if len(groups) != 1:
            raise ValueError('flow_window_ambiguous')
        window = next(iter(groups))
        with registry() as db:
            db.execute('INSERT INTO flow_windows VALUES(?,?,?,?) ON CONFLICT(account_key) DO UPDATE SET pid=excluded.pid,window_id=excluded.window_id,port=excluded.port', (key, pid, window, port))
        return {'layout': 'flow_only', 'pid': pid, 'window_id': window}
