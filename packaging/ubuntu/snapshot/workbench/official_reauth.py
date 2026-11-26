"""Operator-approved official login and one resend of a proven unsent message."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from official_runtime import desktop_authorization_proxy, official_network_env

from official_runtime import AUTH_MESSAGES, AUTH_UNSENT, auth_failure_proven

SCHEMA = """
CREATE TABLE IF NOT EXISTS official_reauth(
 request_id TEXT PRIMARY KEY REFERENCES requests(request_id), account_key TEXT NOT NULL,
 state TEXT NOT NULL, stage TEXT, token_before TEXT, block_reason TEXT NOT NULL,
 block_created REAL NOT NULL, expires REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS official_reauth_pending_account ON official_reauth(account_key)
 WHERE state IN ('waiting','queued','resending');
"""
REDO_STATES = {'expired','wrong_account','window_failed','account_changed','closed'}
TTL = 300
TOKEN = Path('home/.gemini/antigravity-cli/antigravity-oauth-token')
PROBE_AUTH='readiness_authorization_required'


def authorization_window(account_key):
    """Find the account's verified Ubuntu Chrome without creating a Flow project."""
    from flow_browser_state import _pages, registered_authorization_window
    from task_window_registry import TaskWindowRegistry, process_identity

    binding = TaskWindowRegistry().get(account_key)
    if binding:
        pid, port = binding['browser_pid'], binding['port']
        try:
            if (process_identity(pid, account_key, port) == (binding['profile'], binding['process_start'])
                    and any(page['id'] == binding['page_target'] for page in _pages(account_key, pid, port))):
                return binding
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return registered_authorization_window(account_key)


def close_authorization_tab(stage, account_key):
    """Close only this login's recorded tab, after its token was saved."""
    from urllib.parse import urlsplit
    from urllib.request import build_opener, ProxyHandler
    from task_window_registry import TaskWindowRegistry, process_identity
    try:
        target = private_bytes(stage/'browser-target', 128).decode('ascii')
        saved_window = stage/'browser-window.json'
        binding = (json.loads(private_bytes(saved_window, 2048)) if saved_window.is_file()
                   else TaskWindowRegistry().get(account_key))
        if (not isinstance(binding, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', target)
                or target == binding['page_target']):
            return False
        pid, port = binding['browser_pid'], binding['port']
        if process_identity(pid, account_key, port) != (binding['profile'], binding['process_start']):
            return False
        opener = build_opener(ProxyHandler({}))
        base = f'http://127.0.0.1:{port}'
        with opener.open(base+'/json/list', timeout=2) as response:
            raw = response.read(4*1024*1024+1)
        if len(raw) > 4*1024*1024:
            return False
        pages = [page for page in json.loads(raw) if page.get('type') == 'page']
        if not any(page['id'] == binding['page_target'] and
                   page.get('url', '').startswith('https://flow.google.com/') for page in pages):
            return False
        tab = next((page for page in pages if page['id'] == target), None)
        if tab is None:
            return True
        url = urlsplit(tab.get('url', ''))
        if url.scheme != 'https' or not (url.netloc == 'accounts.google.com'
                or (url.netloc == 'antigravity.google' and url.path == '/oauth-callback')):
            return False
        with opener.open(base+'/json/close/'+target, timeout=2):
            return True
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return False


def private_bytes(path, limit):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('授权资料不可读取。')
    return path.read_bytes()


def legacy_proof(root, request, key):
    """Recognize historical CLI audit only when it contains explicit pre-send evidence."""
    try:
        audit = root/'official-audit'/request
        if audit.is_symlink():
            return False
        receipt = json.loads(private_bytes(audit/'submission.json', 4096))
        result = json.loads(private_bytes(audit/'result.json', 16384))
        if (receipt.get('account_key') != key or result.get('status') != 'failed'
                or result.get('reason') != 'provider_error' or result.get('response')
                or result.get('first_text_ms') is not None):
            return False
        markers, output = set(), []
        for line in private_bytes(audit/'events.jsonl', 128*1024).splitlines():
            entry = json.loads(line)
            if entry['channel'] == 'stderr':
                if entry['line'].strip() in AUTH_MESSAGES:
                    markers.add(entry['line'].strip())
            elif entry['channel'] == 'stdout' and entry['line'].strip():
                output.append(json.loads(entry['line']))
        return len(output)==1 and output[0].get('event')=='result' and auth_failure_proven(markers,output[0].get('result'),1)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return False


class Reauthorization:
    def __init__(self, official):
        self.official=official
        self.pool=official.pool
        self.runtime=official.runtime
        from gemini_authorization import GeminiAuthorization
        self.activation=GeminiAuthorization(self)

    def evidence(self, row, attempts):
        if (row['status']!='failed' or row['response'] or row['first_text_ms'] is not None
                or row['attachments']!='[]' or len(attempts)>1):
            return False
        if attempts:
            a=attempts[0]
            return a['ordinal']==1 and a['status']=='failed' and a['reason']==AUTH_UNSENT and a['submitted']==0
        return (row['official_reason']=='provider_error'
                and legacy_proof(self.pool.state_dir,row['request_id'],row['account_key']))

    @staticmethod
    def probe_evidence(row,attempts):
        if (not row['readiness_probe'] or row['official_reason']!=PROBE_AUTH or row['status']!='failed'
                or row['response'] or row['first_text_ms'] is not None or row['attachments']!='[]'
                or row['finished'] is None or len(attempts)!=1):
            return False
        a=attempts[0]
        return a['ordinal']==1 and a['status']=='failed' and a['reason']==PROBE_AUTH and a['submitted']==0

    @staticmethod
    def guard(row):
        return (PROBE_AUTH,row['finished']) if row['readiness_probe'] else (row['block_reason'],row['block_created'])

    def view(self, row, attempts, state):
        eligible=(row.get('block_reason') in (AUTH_UNSENT,'provider_error')
                  and row.get('block_created')==row.get('finished') and self.evidence(row,attempts))
        if eligible and row.get('account_key') in self.pool.removed_gemini_accounts():
            eligible=False
        if state:
            return {'state':state,'can_start':state in REDO_STATES and eligible}
        return {'state':'required','can_start':True} if eligible else None

    def dashboard(self):
        with self.pool.connection() as db:
            db.execute('BEGIN')
            candidates=db.execute("""SELECT r.request_id,a.state FROM requests r
                     JOIN official_jobs j USING(request_id) JOIN conversations c ON c.id=j.conversation_id
                     LEFT JOIN official_reauth a USING(request_id)
                     LEFT JOIN official_account_blocks b ON b.account_key=r.account_key
                     WHERE c.hidden=0 AND ((r.status='failed' AND r.finished=b.created AND j.reason IN (?,?))
                           OR (j.readiness_probe=1 AND j.reason=? AND NOT EXISTS(
                              SELECT 1 FROM official_jobs p JOIN requests rr USING(request_id)
                              WHERE p.readiness_probe=1 AND rr.account_key=r.account_key AND rr.rowid>r.rowid))
                           OR (a.state IS NOT NULL AND a.expires>?))
                     ORDER BY CASE WHEN r.status='failed' AND r.finished=b.created THEN 0 ELSE 1 END,r.created DESC LIMIT 80""",
                     (AUTH_UNSENT,'provider_error',PROBE_AUTH,time.time()-86400)).fetchall()
            result=[]
            for candidate in candidates:
                rid=candidate['request_id']
                row=self.row(db,rid)
                attempts=self.attempts(db,rid)
                state=candidate['state']
                if row['readiness_probe']:
                    if not state and not self.probe_evidence(row,attempts):continue
                    flow={'state':state or 'required','can_start':(not state or state in REDO_STATES) and
                          self.probe_evidence(row,attempts) and self.check(db,row),'mode':'login_only'}
                else:
                    flow=self.view(dict(row),attempts,state)
                if flow:
                    result.append({'request_id':rid,'conversation_id':row['conversation_id'],
                                   'account_key':row['account_key'],**flow})
            return {'items':result}

    @staticmethod
    def row(db, rid):
        return db.execute("""SELECT r.*,j.reason AS official_reason,j.started,j.readiness_probe,t.attachments,
                         t.conversation_id,m.first_text_ms,m.total_ms,c.hidden,
                         b.reason AS block_reason,b.created AS block_created
                         FROM requests r JOIN official_jobs j USING(request_id)
                         JOIN conversation_turns t USING(request_id) JOIN conversations c ON c.id=t.conversation_id
                         LEFT JOIN request_metrics m USING(request_id)
                         LEFT JOIN official_account_blocks b ON b.account_key=r.account_key
                         WHERE r.request_id=?""",(rid,)).fetchone()

    @staticmethod
    def attempts(db,rid):
        return db.execute('SELECT * FROM official_attempts WHERE request_id=? ORDER BY ordinal',(rid,)).fetchall()

    def check(self,db,row):
        from official_pool import busy_official,unresolved_official
        key=row['account_key']
        pref=db.execute('SELECT gemini_enabled FROM account_preferences WHERE account_key=?',(key,)).fetchone()
        if (self.pool.maintenance() or row['hidden'] or (pref and not pref[0])
                or busy_official(db,key) or (not row['readiness_probe'] and unresolved_official(db,key))):
            return False
        if row['readiness_probe']:
            return not db.execute("""SELECT 1 FROM official_jobs p JOIN requests r USING(request_id)
                WHERE p.readiness_probe=1 AND r.account_key=? AND r.rowid>(SELECT rowid FROM requests WHERE request_id=?)""",
                (key,row['request_id'])).fetchone()
        return (not db.execute("""SELECT 1 FROM requests r JOIN conversation_turns t USING(request_id)
                          WHERE t.conversation_id=? AND r.rowid>(SELECT rowid FROM requests WHERE request_id=?)""",
                         (row['conversation_id'],row['request_id'])).fetchone()
                and row['block_reason'] in (AUTH_UNSENT,'provider_error')
                and row['block_created']==row['finished'])

    def start(self,rid,*,login_only=False):
        if not login_only and self.activation.status()['state'] in ('starting','waiting'):
            raise ValueError('Gemini 正在按顺序授权，请先完成当前账号。')
        self.pool.conversation_id(rid)
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=self.row(db,rid)
            if login_only and (not row or not row['readiness_probe']):
                raise ValueError('自动授权不处理业务请求。')
            existing=db.execute('SELECT state FROM official_reauth WHERE request_id=?',(rid,)).fetchone()
            if existing and existing[0]=='waiting':
                return {'state':'waiting'}
            check_evidence=self.probe_evidence if row and row['readiness_probe'] else self.evidence
            if (existing and existing[0] not in REDO_STATES or not row
                    or not check_evidence(row,self.attempts(db,rid)) or not self.check(db,row)):
                raise ValueError('当前不能打开授权窗口，请等待该账号空闲或重新检查状态。')
            if db.execute("""SELECT 1 FROM official_reauth WHERE account_key=?
                          AND state IN ('waiting','queued','resending')""",(row['account_key'],)).fetchone():
                raise ValueError('原账号已有授权或补发正在处理。')
            if row['readiness_probe'] and db.execute("""SELECT 1 FROM official_reauth a JOIN official_jobs j USING(request_id)
                          WHERE a.state='waiting' AND j.readiness_probe=1""").fetchone():
                raise ValueError('已有一个 Gemini 授权窗口打开，请先完成或关闭它。')
            account=self.runtime.account(row['account_key'])
            if not account:
                raise ValueError('原账号的授权资料不可读取。')
            stage=Path(tempfile.mkdtemp(prefix='workbench-reauth-',dir='/dev/shm'))
            try:
                origin=account['root']/TOKEN
                before=hashlib.sha256(private_bytes(origin,100000)).hexdigest()
                (stage/TOKEN).parent.mkdir(parents=True,mode=0o700)
                (stage/'workspace').mkdir(mode=0o700)
                for name in ('settings.json','jetski_state.pbtxt','cache/onboarding.json'):
                    src=origin.parent/name
                    if src.is_file():
                        dst=(stage/TOKEN).parent/name
                        dst.parent.mkdir(parents=True,exist_ok=True)
                        dst.write_bytes(private_bytes(src,1024*1024))
                        dst.chmod(0o600)
                db.execute('DELETE FROM official_reauth WHERE request_id=?',(rid,))
                db.execute("INSERT INTO official_reauth VALUES(?,?,'waiting',?,?,?,?,?)",
                           (rid,row['account_key'],str(stage),before,*self.guard(row),time.time()+TTL))
                db.execute('COMMIT')
            except BaseException:
                shutil.rmtree(stage,ignore_errors=True)
                raise
        try:
            self.launch(stage,account,browser_only=bool(row['readiness_probe']),account_key=row['account_key'])
        except (OSError,ValueError,subprocess.SubprocessError):
            self.close_stage(rid,stage,'window_failed')
            raise ValueError('无法打开 Ubuntu 授权窗口。请确认 Ubuntu 桌面已登录后再试。') from None
        return {'state':'waiting'}

    def launch(self,stage,account,*,browser_only=False,account_key=None):
        env=os.environ.copy()
        desktop=subprocess.check_output(['systemctl','--user','show-environment'],text=True,timeout=4)
        for line in desktop.splitlines():
            key,_,value=line.partition('=')
            if key in ('DISPLAY','WAYLAND_DISPLAY','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS','XAUTHORITY'):
                env[key]=value
        if browser_only:
            if not (env.get('DISPLAY') or env.get('WAYLAND_DISPLAY')):
                raise OSError('Ubuntu desktop is unavailable')
            # The CLI runs in a private virtual terminal; only Chrome appears
            # on the real Ubuntu desktop.
            for key in ('SSH_TTY','SSH_CONNECTION','SSH_CLIENT'):
                env.pop(key,None)
            env['GDK_BACKEND']='x11'
            subprocess.Popen(['/usr/bin/xvfb-run','-a','-s','-screen 0 12000x1080x24',
                              sys.executable,'-B',str(Path(__file__).resolve()),
                              '--browser-login',str(stage),str(account['binary']),account_key,account['email']],
                             env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,start_new_session=True)
            return
        subprocess.run(['gnome-terminal','--window','--title=Gemini 官方重新授权',
                        '--working-directory='+str(stage/'workspace'),'--',sys.executable,'-B',
                        str(Path(__file__).resolve()),str(stage),str(account['binary']),account['email']],
                       env=env,capture_output=True,check=True,timeout=8)

    def close_stage(self,rid,stage,status):
        with self.pool.connection() as db:
            db.execute("UPDATE official_reauth SET state=?,stage=NULL,token_before=NULL WHERE request_id=? AND state='waiting' AND stage=?",
                       (status,rid,str(stage)))
        self.clean(stage)

    @staticmethod
    def clean(stage):
        if stage.parent==Path('/dev/shm') and stage.name.startswith('workbench-reauth-'):
            shutil.rmtree(stage,ignore_errors=True)

    def tick(self):
        if self.pool.maintenance():
            return
        with self.pool.connection() as db:
            pending=db.execute("SELECT * FROM official_reauth WHERE state='waiting'").fetchall()
        for auth in pending:
            stage=Path(auth['stage'])
            if time.time()>=auth['expires']:
                self.close_stage(auth['request_id'],stage,'expired')
                continue
            try:
                token=private_bytes(stage/TOKEN,100000)
                identity=json.loads(token).get('id_token_email','').lower()
            except (ValueError,OSError,AttributeError):
                if (stage/'closed').is_file():
                    self.close_stage(auth['request_id'],stage,'closed' if (stage/'browser-opened').is_file()
                                     or auth['block_reason']!=PROBE_AUTH else 'window_failed')
                continue
            if not identity:
                continue
            self.accept(auth,stage,token,identity)

    def accept(self,auth,stage,token,identity):
        rid=auth['request_id']
        account=self.runtime.account(auth['account_key'])
        if not account or identity!=account['email'].lower():
            self.close_stage(rid,stage,'wrong_account')
            return
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            active=db.execute("SELECT 1 FROM official_reauth WHERE request_id=? AND stage=? AND state='waiting'",(rid,str(stage))).fetchone()
            if not active:
                return
            row=self.row(db,rid)
            attempts=self.attempts(db,rid)
            origin=account['root']/TOKEN
            check_evidence=self.probe_evidence if row and row['readiness_probe'] else self.evidence
            if (not row or not check_evidence(row,attempts) or not self.check(db,row)
                    or row['account_key']!=auth['account_key'] or self.guard(row)!=(auth['block_reason'],auth['block_created'])
                    or hashlib.sha256(private_bytes(origin,100000)).hexdigest()!=auth['token_before']):
                state='account_changed'
            else:
                fd,temp=tempfile.mkstemp(prefix='.reauth-',dir=origin.parent)
                try:
                    with os.fdopen(fd,'wb') as stream:
                        stream.write(token);stream.flush();os.fsync(stream.fileno())
                    os.replace(temp,origin)
                finally:
                    Path(temp).unlink(missing_ok=True)
                if row['readiness_probe']:
                    state='completed'
                else:
                    if not attempts:
                        db.execute("""INSERT INTO official_attempts(request_id,ordinal,status,reason,submitted,started,finished,total_ms)
                                      VALUES(?,1,'failed',?,0,?,?,?)""",
                                   (rid,AUTH_UNSENT,row['started'] or row['created'],row['finished'],row['total_ms']))
                    db.execute("UPDATE requests SET status='queued',finished=NULL,note='原账号已重新授权，原消息等待补发一次。' WHERE request_id=?",(rid,))
                    state='queued'
            if state in ('completed','queued'):
                close_authorization_tab(stage, auth['account_key'])
            db.execute("UPDATE official_reauth SET state=?,stage=NULL,token_before=NULL WHERE request_id=?",(state,rid))
            db.execute('COMMIT')
        self.clean(stage)
        if state in ('completed','queued'):
            from official_quota import reader_for
            reader_for(self.runtime).refresh_after_authorization(auth['account_key'])
        self.official.wake.set()


def authorize(stage,binary,email):
    print('请打开下方 Google 链接，选择 '+email+'。',flush=True)
    print('如果 Google 显示授权码，请粘贴回此窗口。此窗口只办理授权。',flush=True)
    home=str(stage/'home')
    env={'HOME':home,'XDG_CONFIG_HOME':home+'/.config','XDG_CACHE_HOME':home+'/.cache',
         'XDG_DATA_HOME':home+'/.local/share','PATH':'/usr/bin:/bin','TERM':'xterm-256color',
         'LANG':'C.UTF-8','SSH_TTY':'official-login','SSH_CONNECTION':'official-login'}
    env.update(official_network_env())
    stopped=False
    def stop(*_):
        nonlocal stopped
        stopped=True
    signal.signal(signal.SIGHUP,stop)
    signal.signal(signal.SIGTERM,stop)
    try:
        with subprocess.Popen([binary,'--settings','{"altScreenMode":"never","notifications":false}'],
                              cwd=stage/'workspace',env=env) as proc:
            until=time.monotonic()+TTL
            while not stopped and stage.is_dir() and time.monotonic()<until and proc.poll() is None:
                time.sleep(.5)
            if proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=2)
                except subprocess.TimeoutExpired:proc.kill()
    finally:
        try:(stage/'closed').touch(mode=0o600,exist_ok=False)
        except OSError:pass


def token_proxy(upstream):
    """Use the desktop proxy for the provider's authorization endpoints only."""
    import select
    import socket
    import socketserver
    import threading

    class Handler(socketserver.StreamRequestHandler):
        rbufsize=0

        def handle(self):
            self.request.settimeout(8)
            try:
                request=self.rfile.readline(1024)
                match=re.fullmatch(rb'CONNECT ([A-Za-z0-9.-]{1,253}):443 HTTP/1\.[01]\r\n',request)
                if not match:return
                host=match[1].decode('ascii').lower()
                for _ in range(50):
                    header=self.rfile.readline(4096)
                    if header==b'\r\n':break
                    if not header:return
                else:return
                if host not in ('oauth2.googleapis.com','www.googleapis.com',
                                'daily-cloudcode-pa.googleapis.com','aicode.googleapis.com'):
                    self.request.sendall(b'HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
                    return
                with socket.create_connection(upstream,timeout=8) as remote:
                    remote.sendall(f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n'.encode('ascii'))
                    with remote.makefile('rb',buffering=0) as handshake:
                        if not re.match(rb'^HTTP/1\.[01] 200(?: |\r)',handshake.readline(1024)):return
                        for _ in range(50):
                            header=handshake.readline(4096)
                            if header==b'\r\n':break
                            if not header:return
                        else:return
                    self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                    remote.settimeout(10)
                    while True:
                        ready,_,_=select.select([self.request,remote],[],[],45)
                        if not ready:return
                        for source in ready:
                            data=source.recv(65536)
                            if not data:return
                            (remote if source is self.request else self.request).sendall(data)
            except (OSError,ValueError):
                return

    server=socketserver.ThreadingTCPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server


def browser_authorize(stage,binary,account_key,email):
    """Keep the CLI private and complete its Google OAuth in Ubuntu Chrome."""
    import gi
    from urllib.parse import parse_qs,urlsplit
    from urllib.request import build_opener, ProxyHandler, Request
    from flow_browser_state import _pages, focus_call
    from task_window_registry import process_identity
    gi.require_version('Gtk','3.0')
    gi.require_version('Vte','2.91')
    gi.require_version('Soup','3.0')
    from gi.repository import Gtk,Vte,GLib,Soup

    binding=authorization_window(account_key)
    if not binding:return
    pid,port=binding['browser_pid'],binding['port']
    chrome_http=f'http://127.0.0.1:{port}'
    local_http=build_opener(ProxyHandler({}))
    def checked_window():
        return (process_identity(pid,account_key,port)==(binding['profile'],binding['process_start'])
                and any(p['id']==binding['page_target'] for p in _pages(account_key,pid,port)))
    if not checked_window():return
    home=str(stage/'home')
    cli_env=dict(HOME=home,XDG_CONFIG_HOME=home+'/.config',XDG_CACHE_HOME=home+'/.cache',
                 XDG_DATA_HOME=home+'/.local/share',LANG='C.UTF-8',PATH='/usr/bin:/bin',
                 DBUS_SESSION_BUS_ADDRESS='unix:path='+str(stage/'no-shared-keyring'))
    upstream=desktop_authorization_proxy()
    proxy=token_proxy(upstream) if upstream else None
    if proxy:
        cli_env.update(HTTPS_PROXY=f'http://127.0.0.1:{proxy.server_address[1]}',
                       NO_PROXY='localhost,127.0.0.1,::1')
    terminal=Vte.Terminal()
    terminal.set_size(1000,42)
    window=Gtk.Window()
    window.add(terminal)
    window.show_all()
    cli_pid=None
    login_selected=False
    trusted=False
    connecting=False
    code_sent=False
    callback_state_ok=True
    callback_poll=0.0
    link=None
    oauth_state=None
    deadline=time.monotonic()+TTL
    tab_id=None
    opened=False
    main_frame=None
    expected_window=binding.get('chrome_window')
    session=Soup.Session()
    socket=None

    def spawn_done(_term,pid,error,*_):
        nonlocal cli_pid
        if error is not None:
            Gtk.main_quit()
        else:
            cli_pid=pid

    def submit_code(code):
        nonlocal code_sent
        if not code_sent and isinstance(code,str) and re.fullmatch(r'4/[A-Za-z0-9_-]{35,512}',code):
            code_sent=True
            (stage/'callback-relayed').touch(mode=0o600,exist_ok=True)
            # The CLI uses bracketed paste. A burst of bare text can be
            # interpreted as a single keyboard event instead of field input.
            terminal.feed_child(b'\x1b[200~'+code.encode('ascii')+b'\x1b[201~')
            def enter_code():
                if stage.is_dir():terminal.feed_child(b'\r')
                return False
            GLib.timeout_add(100,enter_code)

    def accept_callback(url):
        nonlocal callback_state_ok
        if not isinstance(url,str):return
        parsed=urlsplit(url)
        if (parsed.scheme,parsed.netloc,parsed.path)!=('https','antigravity.google','/oauth-callback'):return
        vals=parse_qs(parsed.query) or parse_qs(parsed.fragment)
        if 'state' in vals and vals['state']!=[oauth_state]:
            callback_state_ok=False
            (stage/'callback-state-mismatch').touch(mode=0o600,exist_ok=True)
        if callback_state_ok and vals.get('state')==[oauth_state]:
            submit_code(vals.get('code',[''])[0])

    # Google may clear the callback URL before showing the one-time code.
    # Read only the exact official callback, in the tab created for this flow.
    # The CLI still validates PKCE, and accept() verifies the token's account.
    callback_value=r"""((email)=>{
      const path=location.pathname, origin=location.origin;
      if(origin==='https://accounts.google.com'){
        const visible=e=>e.getClientRects().length&&!e.disabled;
        const body=document.body?.innerText||'';
        let choices=[];
        if(path==='/v3/signin/accountchooser'){
          choices=[...document.querySelectorAll('[data-identifier]')].filter(e=>visible(e)&&e.getAttribute('data-identifier')?.toLowerCase()===email);
        }else if(path==='/signin/oauth/v3/firstparty/nativeapp'&&body.toLowerCase().includes(email)&&body.includes('Google Antigravity')&&!document.querySelector('input[type=password]')){
          choices=[...document.querySelectorAll('button,[role=button]')].filter(e=>visible(e)&&/^(登录|sign in)$/i.test(e.innerText.trim()));
        }
        if(choices.length===1&&!choices[0].hasAttribute('data-workbench-auth-acted')){
          choices[0].setAttribute('data-workbench-auth-acted','');
          choices[0].click();
        }
        return null;
      }
      if(origin!=='https://antigravity.google'||path!=='/oauth-callback')return null;
      const copy=[...document.querySelectorAll('button')].some(b=>/copy to clipboard|复制到剪贴板/i.test(b.innerText));
      const found=[...new Set((document.body?.innerText||'').match(/4\/[A-Za-z0-9_-]{35,512}/g)||[])];
      return copy&&found.length===1?found[0]:null;
    })("""+json.dumps(email.lower())+')'

    def send(ws,id_,method,params=None):
        ws.send_text(json.dumps({'id':id_,'method':method,'params':params or {}}))

    def browser_message(ws,kind,data):
        nonlocal main_frame,opened,expected_window
        if kind!=Soup.WebsocketDataType.TEXT:return
        msg=json.loads(data.get_data())
        if msg.get('id')==7:
            if callback_state_ok:
                submit_code(msg.get('result',{}).get('result',{}).get('value'))
        elif msg.get('id')==8:
            expected_window=msg.get('result',{}).get('windowId')
            if type(expected_window) is not int or expected_window<=0:
                Gtk.main_quit()
                return
            send(ws,5,'Browser.getWindowForTarget',{'targetId':tab_id})
        elif msg.get('id')==5:
            if msg.get('result',{}).get('windowId')!=expected_window:
                Gtk.main_quit()
                return
            send(ws,1,'Page.enable')
            send(ws,4,'Network.enable')
            send(ws,2,'Page.getFrameTree')
        elif msg.get('id')==2:
            main_frame=msg.get('result',{}).get('frameTree',{}).get('frame',{}).get('id')
            if main_frame:
                send(ws,3,'Page.navigate',{'url':link})
        elif msg.get('method')=='Page.frameNavigated':
            frame=msg.get('params',{}).get('frame',{})
            if frame.get('id')==main_frame:
                accept_callback(frame.get('url','')+frame.get('urlFragment',''))
        elif msg.get('method')=='Page.navigatedWithinDocument':
            p=msg.get('params',{})
            if p.get('frameId')==main_frame:accept_callback(p.get('url'))
        elif msg.get('method')=='Network.requestWillBeSent':
            p=msg.get('params',{})
            if p.get('frameId')==main_frame and p.get('type')=='Document':
                accept_callback(p.get('request',{}).get('url'))
        elif msg.get('id')==3 and not msg.get('error') and not msg.get('result',{}).get('errorText'):
            opened=True
            (stage/'browser-opened').touch(mode=0o600,exist_ok=True)
            send(ws,6,'Page.bringToFront')

    def browser_connected(sess,result,*_):
        nonlocal socket,connecting
        try:socket=sess.websocket_connect_finish(result)
        except GLib.Error:
            Gtk.main_quit()
            return
        socket.connect('message',browser_message)
        socket.connect('closed',lambda *_: Gtk.main_quit())
        if expected_window is None:
            send(socket,8,'Browser.getWindowForTarget',{'targetId':binding['page_target']})
        else:
            send(socket,5,'Browser.getWindowForTarget',{'targetId':tab_id})

    def tick_ui():
        nonlocal login_selected,trusted,link,oauth_state,connecting,tab_id,callback_poll
        if not stage.is_dir() or time.monotonic()>=deadline:
            Gtk.main_quit()
            return False
        output=terminal.get_text_format(Vte.Format.TEXT) or ''
        if not login_selected and 'Select login method:' in output and '> 1. Google OAuth' in output:
            terminal.feed_child(b'\r')
            login_selected=True
        if not trusted and 'Do you trust the contents of this project?' in output:
            terminal.feed_child(b'\r')
            trusted=True
        if (opened and callback_state_ok and not code_sent and socket
                and time.monotonic()-callback_poll>=1):
            callback_poll=time.monotonic()
            send(socket,7,'Runtime.evaluate',{'expression':callback_value,'returnByValue':True,'userGesture':True})
        if code_sent and re.search(r'invalid[_ ]grant|failed to authenticate|authorization failed|token exchange failed',output,re.I):
            (stage/'callback-cli-rejected').touch(mode=0o600,exist_ok=True)
            Gtk.main_quit()
            return False
        if not connecting:
            for url in re.findall(r'https://accounts\.google\.com/o/oauth2/[^\s<>"\]]+',output):
                parts=urlsplit(url)
                if parts.path not in ('/o/oauth2/auth','/o/oauth2/v2/auth'):continue
                fields=parse_qs(parts.query)
                if (fields.get('redirect_uri')!=['https://antigravity.google/oauth-callback']
                        or fields.get('response_type')!=['code'] or fields.get('code_challenge_method')!=['S256']
                        or not all(fields.get(k) for k in ('state','client_id','code_challenge'))):
                    continue
                link=url
                oauth_state=fields['state'][0]
                try:
                    if not checked_window() or not focus_call('Activate',pid,binding['window_id']).get('ok'):
                        raise ValueError('window binding')
                    with local_http.open(Request(chrome_http+'/json/new?about:blank',method='PUT'),timeout=3) as resp:
                        target=json.load(resp)
                    new_id=target['id']
                    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',new_id) or new_id==binding['page_target']:
                        raise ValueError('tab identity')
                    tab_id=new_id
                    ws=target['webSocketDebuggerUrl']
                    if ws!=f'ws://127.0.0.1:{port}/devtools/page/{tab_id}':
                        raise ValueError('tab endpoint')
                    (stage/'browser-window.json').write_text(json.dumps({
                        field: binding[field] for field in
                        ('browser_pid','port','profile','process_start','page_target')
                    }),encoding='ascii')
                    (stage/'browser-target').write_text(tab_id,encoding='ascii')
                    connecting=True
                    message=Soup.Message.new('GET',ws)
                    session.websocket_connect_async(message,None,None,GLib.PRIORITY_DEFAULT,None,browser_connected,None)
                except (OSError,ValueError,KeyError,subprocess.SubprocessError):
                    Gtk.main_quit()
                    return False
                break
        return True

    terminal.connect('child-exited',lambda *_: Gtk.main_quit())
    terminal.spawn_async(Vte.PtyFlags.DEFAULT,str(stage/'workspace'),
                         [binary,'--settings','{"altScreenMode":"never","notifications":false}'],
                         [f'{k}={v}' for k,v in cli_env.items()],GLib.SpawnFlags.DEFAULT,
                         None,None,-1,None,spawn_done,None)
    GLib.timeout_add(250,tick_ui)
    try:
        Gtk.main()
    finally:
        if proxy:
            proxy.shutdown()
            proxy.server_close()
        # Clean up only the tab we created if authorization was never opened.
        if tab_id and not opened:
            try:
                if checked_window():
                    with local_http.open(chrome_http+'/json/close/'+tab_id,timeout=2):
                        pass
            except (OSError,ValueError):pass
        if cli_pid:
            try:os.kill(cli_pid,signal.SIGTERM)
            except ProcessLookupError:pass


if __name__=='__main__':
    if sys.argv[1]=='--browser-login':
        stage=Path(sys.argv[2])
        try:
            browser_authorize(stage,sys.argv[3],sys.argv[4],sys.argv[5])
        finally:
            try:(stage/'closed').touch(mode=0o600,exist_ok=True)
            except OSError:pass
    else:
        authorize(Path(sys.argv[1]),sys.argv[2],sys.argv[3])
