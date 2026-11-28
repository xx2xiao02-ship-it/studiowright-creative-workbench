"""Official /usage reader. No prompts, generation, original-home writes, or raw logs."""
import copy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time

from official_runtime import official_network_env

ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')
KEY = re.compile(r'[0-9a-f]{64}\Z')
TTL = 300
AUTO_REFRESH_SECONDS = 6 * 3600
ADVANCED_REFRESH_SECONDS = 300
QUOTA_WINDOWS = ('five_hour','weekly')


def verification_entry(text):
    from urllib.parse import parse_qs, urlsplit
    candidates = set()
    for link in re.findall(r'https://[^\s<>"\x1b]+', ANSI.sub('', text)):
        try:
            url = urlsplit(link)
            target = parse_qs(url.query).get('continue', [])
            if (url.scheme == 'https' and url.netloc == 'accounts.google.com'
                    and url.path == '/v3/signin/continue' and not url.fragment
                    and target == ['https://developers.google.com/gemini-code-assist/auth/auth_success_gemini']
                    and len(link) <= 4096):
                candidates.add(link)
        except ValueError:
            continue
    return next(iter(candidates)) if len(candidates) == 1 else None


def classify_failure(text, *, sent=False, identity=False):
    """Expose only typed evidence; generic eligibility failures do not prove verification."""
    plain = ANSI.sub('', text)
    status = 'unavailable'
    reason = 'usage_not_returned' if sent else 'startup_not_ready'
    if re.search(r'VALIDATION_REQUIRED|verify your account.{0,160}continue|additional (?:account )?verification required',plain,re.I|re.S):
        status,reason='verification_required','provider_validation_required'
    elif re.search(r'UNAUTHENTICATED|invalid_grant|invalid.*refresh.token|refresh.token.*expired|sign in to continue|login required',plain,re.I):
        status,reason='authorization_required','provider_authorization_required'
    elif re.search(r'ECONNREFUSED|ECONNRESET|ENOTFOUND|ETIMEDOUT|network error|fetch failed|connection timed out',plain,re.I):
        status,reason='network_error','provider_connection_failed'
    elif re.search(r'Eligibility check failed:',plain,re.I):
        status,reason='eligibility_failed','provider_eligibility_check_failed'
    codes=re.findall(r'(?:HTTP(?:Error| status| status code)?[:= ]*|status(?: code)?[:= ]+)([45]\d{2})\b',plain,re.I)
    return {'status':status,'reason':reason,'http_status':int(codes[-1]) if codes else None,
            'phase':'usage' if sent else 'startup','identity_matched':identity,'checked_at':time.time()}


def parse_usage(text, checked_at):
    """Parse only the Gemini shared model group; unrelated groups never contribute."""
    plain = ANSI.sub('', text).replace('\r', '\n')
    groups = re.findall(r'GEMINI MODELS(.*?)(?:CLAUDE AND GPT MODELS|\Z)', plain, re.S)
    if not groups:
        return None
    group = groups[-1]
    result = {}
    for name, label in [('weekly', 'Weekly'), ('five_hour', 'Five Hour')]:
        match = re.search(label + r' Limit Remaining\s*\[[^\]]*\]\s*(\d+(?:\.\d+)?)%\s*(Refreshes in [^\n]+|Quota available)', group)
        if not match:
            continue
        value = float(match[1])
        if not math.isfinite(value) or not 0 <= value <= 100:
            return None
        reset = None
        if match[2].startswith('Refreshes in '):
            duration = re.fullmatch(r'Refreshes in\s*(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s\s*)?', match[2].strip())
            if duration and any(duration.groups()):
                reset = checked_at + sum(int(n or 0)*unit for n, unit in zip(duration.groups(), (86400,3600,60,1)))
        result[name] = {'remaining_percent': value, 'reset_at': reset}
    return result or None


def parse_headless_usage(text):
    """Read Google's shared Gemini quota from the CLI's tab-delimited response."""
    windows={}
    labels={'Five Hour Limit Remaining':'five_hour','Weekly Limit Remaining':'weekly'}
    for line in text.splitlines():
        match=re.fullmatch(r'Gemini Models\t(Weekly Limit Remaining|Five Hour Limit Remaining)\t(\d+(?:\.\d+)?)%\t([^\t]*)',line)
        if not match:
            continue
        window=labels[match[1]]; percent=float(match[2])
        if window in windows or not 0<=percent<=100:
            return None
        reset=None
        if match[3]:
            try:
                date=datetime.fromisoformat(match[3].replace('Z','+00:00'))
                if date.tzinfo is not None:
                    reset=date.timestamp()
            except (ValueError,OverflowError,OSError):
                pass
        windows[window]={'remaining_percent':percent,'reset_at':reset}
    return windows or None


def quota_restriction(row, now):
    if row.get('status') in ('verification_required','authorization_required'):
        return row['status']
    if row.get('status') != 'ok':
        return None
    checked=row.get('checked_at',0)
    fresh=type(checked) in (int,float) and 0<=now-checked<=600
    for window in QUOTA_WINDOWS:
        quota=row.get(window,{})
        reset=quota.get('reset_at')
        if quota.get('remaining_percent')==0 and (reset>now if type(reset) in (int,float) else fresh):
            return 'quota_exhausted'
    return None


def read_headless_usage(account, workspace, env):
    try:
        result=subprocess.run([str(account['binary']),'--print','/usage','--output-format','json','--print-timeout','15s'],
                              cwd=workspace,env=env,capture_output=True,text=True,timeout=20,stdin=subprocess.DEVNULL)
        reply=json.loads(result.stdout)
    except (OSError,ValueError,subprocess.SubprocessError):
        return None
    now=time.time()
    if not isinstance(reply,dict):
        return None
    text=reply.get('response')
    if result.returncode==0 and reply.get('status')=='SUCCESS' and isinstance(text,str):
        windows=parse_headless_usage(text)
        if windows:
            return {'status':'ok','checked_at':now,'phase':'usage','identity_matched':True,**windows}
    evidence=str(reply.get('error') or '')+'\n'+result.stderr
    failure=classify_failure(evidence,sent=True,identity=True)
    if failure['status'] in ('verification_required','authorization_required'):
        link=verification_entry(evidence)
        if link:
            failure['_verification_url']=link
        return failure
    return None


def read_account(runtime, key, *, save_renewal=False):
    # Imports are Linux-only; the pure parser and aggregation run on Windows too.
    import fcntl
    import pty
    import select
    import struct
    import termios
    account = runtime.account(key)
    if not account:
        return {'status': 'authorization_unavailable', 'checked_at': time.time()}
    original = account['root']/'home/.gemini/antigravity-cli'
    advanced=key in runtime.text_models()
    # Credentials exist only in this private tmpfs directory, deleted on every exit.
    # The native CLI renews auth in its process; no token is copied back or logged.
    with tempfile.TemporaryDirectory(prefix='workbench-quota-', dir='/dev/shm') as temporary:
        root = Path(temporary); home = root/'home'; cfg = home/'.gemini/antigravity-cli'
        cfg.mkdir(parents=True, mode=0o700)
        token_name='antigravity-oauth-token'
        original_token=None
        for name in (token_name, 'settings.json', 'jetski_state.pbtxt', 'cache/onboarding.json'):
            source = original/name
            if advanced and not save_renewal and name in ('jetski_state.pbtxt','cache/onboarding.json') and not source.exists():
                continue
            if source.is_symlink() or not source.is_file() or source.stat().st_size > 1024*1024:
                return {'status': 'authorization_unavailable', 'checked_at': time.time()}
            target = cfg/name; target.parent.mkdir(parents=True, exist_ok=True)
            data=source.read_bytes()
            if name==token_name: original_token=data
            with target.open('xb') as stream:
                os.fchmod(stream.fileno(), 0o600); stream.write(data)
        workspace = root/'workspace'; workspace.mkdir(mode=0o700)
        env = {'HOME':str(home), 'XDG_CONFIG_HOME':str(home/'.config'), 'XDG_CACHE_HOME':str(home/'.cache'),
               'XDG_DATA_HOME':str(home/'.local/share'), 'PATH':'/usr/bin:/bin', 'LANG':'C.UTF-8',
               'TERM':'xterm-256color'}
        # Match the account's production login mode. Ordinary accounts can
        # otherwise wait for an SSH authorization code instead of reading quotas.
        if advanced:
            env.update(SSH_TTY='quota-read-only', SSH_CONNECTION='quota-read-only')
        env.update(official_network_env())
        if not save_renewal:
            headless=read_headless_usage(account,workspace,env)
            if headless:
                return headless
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH',60,4096,0,0))
        process = None
        try:
            process = subprocess.Popen([str(account['binary']), '--settings', '{"altScreenMode":"never","notifications":false}'],
                cwd=workspace, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave); slave = None
            output = b''; trusted = False; sent = False; identity = False; started = time.monotonic()
            partial = None
            while time.monotonic()-started < 25:
                if select.select([master],[],[],.2)[0]:
                    try: output += os.read(master,65536)
                    except OSError: break
                if len(output) > 1024*1024:
                    break
                plain = ANSI.sub('',output.decode('utf-8',errors='replace'))
                failure=classify_failure(plain,sent=sent,identity=identity)
                if failure['status']=='verification_required':
                    link=verification_entry(plain) if identity and sent else None
                    if link:
                        failure['_verification_url']=link
                        return failure
                    # The official link can arrive in a later terminal write.
                    if time.monotonic()-started >= 20:
                        failure['reason']='provider_validation_no_entry'
                        return failure
                # Trust is granted solely to the newly created empty tmpfs workspace.
                if not trusted and 'Do you trust the contents of this project?' in plain:
                    os.write(master,b'\r'); trusted = True
                if not sent and '? for shortcuts' in plain and account['email'].lower() in plain.lower():
                    identity = True
                    os.write(master,b'\x1b'); time.sleep(.15)
                    os.write(master,b'/usage\r'); sent = True; output = b''
                if sent:
                    parsed = parse_usage(output.decode('utf-8',errors='replace'),time.time())
                    if parsed and all(window in parsed for window in QUOTA_WINDOWS):
                        if save_renewal:
                            save_verified_token(original/token_name,cfg/token_name,original_token,account['email'])
                        return {'status':'ok', 'checked_at':time.time(),'phase':'usage','identity_matched':True, **parsed}
                    if parsed and not save_renewal:
                        partial = {'status':'ok', 'checked_at':time.time(),'phase':'usage','identity_matched':True, **parsed}
                if process.poll() is not None and not select.select([master],[],[],0)[0]:break
            plain=output.decode('utf-8',errors='replace')
            failure=classify_failure(plain,sent=sent,identity=identity)
            if failure['status']=='verification_required':
                link=verification_entry(plain) if identity and sent else None
                if link:failure['_verification_url']=link
                else:failure['reason']='provider_validation_no_entry'
            if partial and failure['status'] == 'unavailable':
                return partial
            return failure
        finally:
            if slave is not None: os.close(slave)
            if process:
                try: os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError: pass
            if process:
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try: os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError: pass
                    process.wait(timeout=3)
            os.close(master)


def save_verified_token(original, refreshed, before, email):
    if any(path.is_symlink() or not path.is_file() or path.stat().st_size>100000
           for path in (original,refreshed)):
        raise ValueError('Authorization file is not available')
    after=refreshed.read_bytes()
    if json.loads(after).get('id_token_email','').casefold()!=email.casefold():
        raise ValueError('Authorization identity changed')
    if original.read_bytes()!=before:
        raise ValueError('Authorization was updated by another operation')
    if after==before:
        return
    fd,name=tempfile.mkstemp(prefix='.workbench-renewal-',dir=original.parent)
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(after);out.flush();os.fsync(out.fileno())
        if not original.is_symlink() and original.read_bytes()==before:
            os.replace(name,original)
        else:
            raise ValueError('Authorization was updated by another operation')
    finally:
        Path(name).unlink(missing_ok=True)


def recover_saved_authorization(runtime,key):
    # Only used by a worker that owns the original account's proven-unsent turn.
    try:
        if not hasattr(runtime,'config'):
            return False
        reader=reader_for(runtime)
        with reader.lock:
            lock=reader.account_locks.setdefault(key,threading.Lock())
        if not lock.acquire(timeout=1):
            return False
        try:
            row=read_account(runtime,key,save_renewal=True)
        finally:
            lock.release()
        return row.get('status')=='ok' and row.get('identity_matched') is True and all(
            row[window]['remaining_percent']>0 for window in ('five_hour','weekly'))
    except (OSError,ValueError,TypeError,KeyError,AttributeError,subprocess.SubprocessError):
        return False


def _aggregate_rows(accounts, now):
    known = [a for a in accounts.values() if a.get('status') == 'ok' and 0 <= now-a.get('checked_at',0) <= 600
             and all(type(a.get(w,{}).get('remaining_percent')) in (int,float)
                     and math.isfinite(a[w]['remaining_percent']) and 0 <= a[w]['remaining_percent'] <= 100
                     for w in ('five_hour','weekly'))]
    summary = {'known_accounts':len(known), 'total_accounts':len(accounts), 'windows':{}}
    for window in ('five_hour','weekly'):
        values = [a[window]['remaining_percent'] for a in known]
        resets = [a[window]['reset_at'] for a in known if type(a[window].get('reset_at')) in (float,int) and a[window]['reset_at']>now]
        summary['windows'][window] = {'average_remaining_percent':round(sum(values)/len(values),2) if values else None,
                                      'total_remaining_percent':round(sum(values),2) if values else None,
                                      'next_reset_at':min(resets) if resets else None}
    return summary


def aggregate(accounts, now):
    return _aggregate_rows(accounts, now)


class QuotaReader:
    def __init__(self, runtime):
        self.runtime = runtime; self.lock = threading.Lock(); self.running = False; self.attempted = 0
        self.account_locks = {}
        self.authorization_refreshes = set()
        self.explicit_attempted = 0
        self.pending_recovery = ()
        self.verification_links = {}
        self.verification = {'state':'idle','completed':0,'total':0}
        self.rows = {}; self.cache = runtime.config.parent/'official-quota.json'
        try:
            if not self.cache.is_symlink() and self.cache.stat().st_size < 100000:
                saved = json.loads(self.cache.read_text())
                if saved.get('schema_version') == 1 and isinstance(saved.get('accounts'),dict):
                    self.rows = {k:v for k,v in saved['accounts'].items() if KEY.fullmatch(k) and isinstance(v,dict)}
                    attempted=saved.get('attempted_at',0)
                    self.attempted=attempted if type(attempted) in (float,int) and math.isfinite(attempted) and attempted<=time.time() else 0
        except (OSError,ValueError,TypeError): pass
        self.stop = threading.Event()
        threading.Thread(target=self._auto_refresh_loop, daemon=True, name='official-quota-auto').start()

    def _auto_refresh_tick(self, *, advanced_only=False):
        """One scheduled pass over the currently readable accounts.

        Failed reads (verification required, network errors) stay on the
        explicit-refresh path exactly like the manual button; the tick only
        re-reads accounts whose numbers are actually shown on the panel."""
        advanced=self.runtime.text_models() if advanced_only else None
        with self.lock:
            if self.running or not self.rows:
                return 0
            keys = tuple(k for k, r in self.rows.items() if r.get('status') in ('ok', 'not_read')
                         and (advanced is None or k in advanced))
            if not keys:
                return 0
            self.running = True
            self.attempted = time.time()
        print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} quota auto-refresh: {len(keys)} accounts', flush=True)
        self._refresh(keys)
        return len(keys)

    def _auto_refresh_loop(self):
        next_full=time.monotonic()+AUTO_REFRESH_SECONDS
        while not self.stop.wait(ADVANCED_REFRESH_SECONDS):
            try:
                full=time.monotonic()>=next_full
                self._auto_refresh_tick(advanced_only=not full)
                if full:
                    next_full=time.monotonic()+AUTO_REFRESH_SECONDS
            except Exception:
                # The daemon loop must survive anything a single pass raises.
                with self.lock:
                    self.running = False

    def snapshot(self, force=False, recover=False, refresh=True):
        try: keys = list((self.runtime.settings() or {}).get('accounts',{}))
        except (OSError,ValueError,TypeError): keys = []
        advanced=self.runtime.text_models()
        now = time.time()
        with self.lock:
            self.rows = {k:self.rows.get(k,{'status':'not_read'}) for k in keys if KEY.fullmatch(k)}
            # Failed reads await the explicit refresh button; no silent auth retry loop.
            automatic = not self.attempted or (any(r.get('status')=='ok' for r in self.rows.values()) and now-self.attempted >= TTL)
            explicit = force and now-self.explicit_attempted>=60
            if explicit:
                self.explicit_attempted=now
                if self.running:
                    self.pending_recovery=tuple(self.rows)
            plan = tuple(sorted((k for k,r in self.rows.items() if explicit or r.get('status') in ('ok','not_read')),
                                key=lambda key:key not in advanced))
            if refresh and plan and not self.running and (automatic or explicit):
                self.running=True; self.attempted=now
                threading.Thread(target=self._refresh,args=(plan,recover),daemon=True,name='official-quota-reader').start()
            rows=copy.deepcopy(self.rows)
            advanced_rows={k:{**rows.get(k,{'status':'not_read'}),'models':sorted(models)}
                           for k,models in advanced.items() if k in rows}
            advanced_summary = aggregate(advanced_rows, now)
            return {'schema_version':1,'source':'official_cli_usage','read_at':now,'attempted_at':self.attempted,
                    'auto_refresh_seconds':AUTO_REFRESH_SECONDS,
                    'advanced_pool':{'quota_scope':'gemini_models_shared','auto_refresh_seconds':ADVANCED_REFRESH_SECONDS,
                                     'accounts':advanced_rows,**advanced_summary},
                    'refreshing':self.running,'accounts':rows,**aggregate(rows,now)}

    def cached_accounts(self, keys):
        with self.lock:
            return {key:copy.deepcopy(self.rows.get(key,{})) for key in keys}

    def _refresh(self,keys,recover=False):
        try:
            for key in keys:
                self.check(key, recover=recover)
        except OSError:
            pass  # The in-memory snapshot remains usable when cache storage is unavailable.
        finally:
            with self.lock:
                pending=self.pending_recovery
                self.pending_recovery=()
                self.running=bool(pending)
                if pending:
                    self.attempted=time.time()
                    threading.Thread(target=self._refresh,args=(pending,True),daemon=True,name='official-quota-recovery').start()

    def refresh_after_authorization(self, key):
        if not KEY.fullmatch(key):
            return False
        with self.lock:
            if key in self.authorization_refreshes:
                return False
            self.authorization_refreshes.add(key)
        def refresh():
            try:
                self.check(key)
            finally:
                with self.lock:
                    self.authorization_refreshes.discard(key)
        threading.Thread(target=refresh,daemon=True,name='official-quota-after-login').start()
        return True

    def check(self,key,*,recover=False):
        if not KEY.fullmatch(key):raise ValueError('账号编号不正确')
        with self.lock: account_lock=self.account_locks.setdefault(key,threading.Lock())
        with account_lock:
            for attempt in (1, 2):
                try:
                    row=read_account(self.runtime,key)
                except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError):
                    row={'status':'unavailable','checked_at':time.time(),'reason':'local_read_failed'}
                status=row.get('status')
                startup_failed=status=='unavailable' and row.get('reason')=='startup_not_ready'
                if not (startup_failed or (recover and status in ('network_error','unavailable'))):
                    break
            row['query_attempts']=attempt
            with self.lock:
                link=row.pop('_verification_url',None)
                if link:self.verification_links[key]=(link,time.time())
                else:self.verification_links.pop(key,None)
                self.rows[key]=row
                if not self.cache.is_symlink():
                    temporary=self.cache.with_name('.official-quota-'+str(time.time_ns())+'.json')
                    try:
                        with temporary.open('x') as stream:
                            os.fchmod(stream.fileno(),0o600)
                            json.dump({'schema_version':1,'attempted_at':self.attempted,'accounts':self.rows},stream)
                            stream.flush();os.fsync(stream.fileno())
                        os.replace(temporary,self.cache)
                    except OSError:
                        temporary.unlink(missing_ok=True)
                return copy.deepcopy(row)


_readers = {}
_lock = threading.Lock()


def reader_for(runtime):
    with _lock:
        reader = _readers.setdefault(str(runtime.config),None)
        if reader is None:
            reader = QuotaReader(runtime); _readers[str(runtime.config)]=reader
    return reader


def snapshot(runtime, force=False):
    return reader_for(runtime).snapshot(force)


def check_account(runtime,key):
    return reader_for(runtime).check(key)
