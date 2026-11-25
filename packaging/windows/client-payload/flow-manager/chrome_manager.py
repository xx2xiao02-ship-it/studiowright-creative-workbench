"""Windows-only, loopback-only profile metadata manager. Never reads Cookies/Login Data."""
import json
import os
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection, HTTPException
from pathlib import Path

HERE = Path(__file__).resolve().parent
USER_DATA = Path(os.environ.get('LOCALAPPDATA', ''))/'Google/Chrome/User Data'
EXTENSION = HERE.parent/'flow2api-token-updater-original-20261029'
EXTENSION_ID = 'kbnibljeinmjmmpholicjkaiehmilbbk'
ORIGIN = 'http://127.0.0.1:18890'
BACKEND = 'http://127.0.0.1:18888'
JOBS = {}
LOCK = threading.Lock()
LAUNCH_LOCK = threading.Lock()
LAST_LAUNCH = 0.0
SYNC_BLOCK_REASON = ''
PLUGIN_CHECK_SUPPORTED = False


def ubuntu_profile_matches(local_profiles):
    connection = HTTPConnection('127.0.0.1', 18920, timeout=6)
    try:
        connection.request('GET', '/api/snapshot', headers={'Accept':'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            return None
        maximum = 8 * 1024 * 1024
        body = response.read(maximum + 1)
        if len(body) > maximum:
            return None
        snapshot = json.loads(body)
        flow, pool = snapshot['flow_state'], snapshot['real_accounts']
        if flow['available'] is not True or flow['simulated'] is not False or pool['available'] is not True:
            return None
        rows = pool['items']
        if not isinstance(rows, list) or any(not isinstance(a.get('email'), str) or not a['email'].strip() for a in rows):
            return None
        emails = {account['email'].strip().casefold() for account in rows}
        return [{'profile':p['profile'], 'email':p['email']} for p in local_profiles
                if isinstance(p.get('email'), str) and p['email'].strip().casefold() in emails]
    except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
        return None
    finally:
        connection.close()


def profiles(root=USER_DATA):
    try:
        cache = json.loads((root/'Local State').read_text(encoding='utf-8'))['profile']['info_cache']
    except (OSError, ValueError, KeyError):
        return []
    result = []
    for name, info in cache.items():
        if not re.fullmatch(r'Default|Profile \d+', name) or not (root/name).is_dir():
            continue
        extension = None
        for pref in ('Secure Preferences', 'Preferences'):
            try:
                settings = json.loads((root/name/pref).read_text(encoding='utf-8')).get('extensions', {}).get('settings', {})
                extension = settings.get(EXTENSION_ID) or extension
            except (OSError, ValueError):
                pass
        plugin, note = profile_plugin_state(root/name, extension)
        jobs = [(n, job) for n, job in reversed(list(JOBS.items())) if job['profile']==name]
        result.append({'profile': name, 'name': info.get('name', name), 'email': info.get('user_name', ''),
                       'plugin': plugin, 'plugin_note': note,
                       'last_job': next((dict(j, nonce=n) for n,j in jobs if j.get('action')!='check'), None),
                       'last_plugin_check': next((dict(j, nonce=n) for n,j in jobs if j.get('action')=='check'), None)})
    return result


def profile_plugin_state(profile, entry):
    if not isinstance(entry, dict):
        return 'missing', '未找到插件登记'
    path = entry.get('path')
    if not isinstance(path, str) or not path:
        return 'broken', '已登记的插件文件找不到，请重新加载正确的插件文件夹。'
    folder = Path(path)
    if not folder.is_absolute():
        folder = profile/'Extensions'/EXTENSION_ID/folder
    if not (folder/'manifest.json').is_file():
        return 'broken', '已登记的插件文件找不到，请重新加载正确的插件文件夹。'
    if entry.get('state') == 0 or entry.get('disable_reasons'):
        return 'disabled', '请在这个 Chrome 用户的扩展页启用插件。'
    return 'registered', '已登记，运行状态待连接确认'


def install_ready():
    return not SYNC_BLOCK_REASON and (EXTENSION/'manifest.json').is_file()


def chrome_path():
    for root in (Path(os.environ.get('LOCALAPPDATA','')), Path(os.environ.get('PROGRAMFILES','')), Path(os.environ.get('PROGRAMFILES(X86)',''))):
        path = root/'Google/Chrome/Application/chrome.exe'
        if path.is_file(): return path
    raise ValueError('找不到 Chrome，请先安装 Chrome。')


def open_profile(profile, url):
    global LAST_LAUNCH
    if profile not in {p['profile'] for p in profiles()}:
        raise ValueError('Chrome 用户不存在，请刷新列表。')
    with LAUNCH_LOCK:
        time.sleep(max(0, 2-(time.monotonic()-LAST_LAUNCH)))
        process = subprocess.Popen([str(chrome_path()), '--profile-directory='+profile, url])
        LAST_LAUNCH = time.monotonic()
        try:
            code = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            code = None
        # Chrome may exit 0 after handing the request to its existing process.
        # A nonzero exit must never be presented as a successfully opened window.
        with (HERE/'chrome-launches.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps({'time':time.time(), 'profile':profile, 'pid':process.pid,
                                  'exit_code':code, 'action':'install' if url.startswith('chrome:') else 'sync' if '#chrome-job=' in url else 'open'})+'\n')
        if code not in (None, 0):
            raise ValueError('Chrome 启动异常（退出码 '+str(code)+'），已停止继续批量打开；请先关闭 Chrome 错误提示。')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def reply(self, value, status=200, mime='application/json; charset=utf-8'):
        raw = json.dumps(value, ensure_ascii=False).encode() if isinstance(value,(dict,list)) else value
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        if self.headers.get('Origin') in (ORIGIN, BACKEND):
            self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
            self.send_header('Vary', 'Origin')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers(); self.wfile.write(raw)

    def allowed(self):
        return self.client_address[0]=='127.0.0.1' and self.headers.get('Host')=='127.0.0.1:18890' and self.headers.get('Origin') in (None,ORIGIN,BACKEND)

    def do_OPTIONS(self):
        if not self.allowed(): return self.reply({'error':'来源不允许'},403)
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin',self.headers.get('Origin',ORIGIN))
        self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type, X-Console-Request')
        self.end_headers()

    def do_GET(self):
        if not self.allowed(): return self.reply({'error':'来源不允许'},403)
        if self.path in ('/', '/chrome-manager.js'):
            file=HERE/('chrome-manager.html' if self.path=='/' else 'chrome-manager.js')
            return self.reply(file.read_bytes(),mime='text/html; charset=utf-8' if self.path=='/' else 'text/javascript; charset=utf-8')
        if self.path=='/health': return self.reply({'application':'studiowright-chrome-manager'})
        if self.path=='/api/profiles':
            with LOCK: local=profiles()
            return self.reply({'profiles':local, 'extension_folder':str(EXTENSION) if install_ready() else '',
                               'extension':{'install_ready':install_ready(), 'can_check':PLUGIN_CHECK_SUPPORTED,
                                            'note':SYNC_BLOCK_REASON or ('' if install_ready() else '插件文件夹缺失，请重新安装 StudioWright 桌面版。')}})
        if self.path=='/api/registered-profiles':
            with LOCK:
                local = profiles()
            matches = ubuntu_profile_matches(local)
            return self.reply({'available': matches is not None, 'profiles': matches if matches is not None else []})
        if self.path.startswith('/api/job/'):
            nonce=self.path.rsplit('/',1)[-1]
            with LOCK: job=JOBS.get(nonce)
            if not job or job['expires']<time.time(): return self.reply({'error':'请求已过期，请从账号管理重新发起'},404)
            return self.reply(job)
        self.reply({'error':'未找到'},404)

    def do_POST(self):
        if not self.allowed() or self.headers.get('X-Console-Request')!='1' or not self.headers.get('Content-Type','').startswith('application/json'):
            return self.reply({'error':'请求来源或格式不允许'},403)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=8192: raise ValueError('请求长度不正确')
            body=json.loads(self.rfile.read(length))
            if self.path=='/api/open':
                if self.headers.get('Origin')!=ORIGIN: return self.reply({'error':'请在本机账号管理中操作'},403)
                selected=body.get('profiles',[]);action=body.get('action')
                if not isinstance(selected,list) or not 1<=len(selected)<=50 or action not in ('install','sync','open','check'):
                    raise ValueError('请勾选账号并选择操作')
                if action=='sync' and SYNC_BLOCK_REASON:
                    raise ValueError(SYNC_BLOCK_REASON)
                if action=='install' and not install_ready():
                    raise ValueError(SYNC_BLOCK_REASON or '插件文件夹缺失，请重新安装 StudioWright 桌面版。')
                if action=='check' and not PLUGIN_CHECK_SUPPORTED:
                    raise ValueError('当前 Ubuntu 后台未启用新版插件连接检查。')
                known={p['profile'] for p in profiles()}
                if any(p not in known for p in selected): raise ValueError('账号列表已变化，请刷新')
                launched=[]
                for profile in dict.fromkeys(selected):
                    if action=='install':
                        open_profile(profile,'chrome://extensions/');launched.append(profile);continue
                    if action=='open':
                        open_profile(profile,'https://flow.google.com/');launched.append(profile);continue
                    nonce=secrets.token_urlsafe(32)
                    with LOCK:
                        for old in [n for n,j in JOBS.items() if j['expires']<time.time()]: del JOBS[old]
                        JOBS[nonce]={'profile':profile,'status':'waiting','message':'等待窗口连接插件',
                                     'action':action,'extension_id':EXTENSION_ID,'expires':time.time()+1200}
                    open_profile(profile,BACKEND+'/#chrome-job='+nonce);launched.append(profile)
                return self.reply({'opened':launched})
            if self.path.startswith('/api/job/'):
                nonce=self.path.rsplit('/',1)[-1]
                with LOCK:
                    job=JOBS.get(nonce)
                    if not job or job['expires']<time.time(): raise ValueError('请求已过期')
                    state=body.get('status')
                    if state not in ('waiting','running','succeeded','failed'): raise ValueError('状态无效')
                    message=body.get('message','')
                    if not isinstance(message,str): raise ValueError('结果格式不正确')
                    job.update(status=state,message=message[:500])
                return self.reply({'ok':True})
            self.reply({'error':'未找到'},404)
        except (ValueError,TypeError,OSError) as exc:
            self.reply({'error':str(exc)},400)


if __name__=='__main__':
    ThreadingHTTPServer(('127.0.0.1',18890),Handler).serve_forever()
