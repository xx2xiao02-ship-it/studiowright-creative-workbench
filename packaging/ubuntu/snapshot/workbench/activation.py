"""One explicit activation batch, with durable, one-shot cooldown recovery."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path


class Activation:
    def __init__(self, state_dir, program_dir, sources, pool, opener=None, start_worker=True):
        self.path = Path(state_dir) / 'activation.sqlite3'
        self.program_dir = Path(program_dir)
        self.sources, self.pool = sources, pool
        self.opener = opener or self.open_browser
        self.guard = threading.RLock()
        self.next_watch = 0.0
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS items(batch_id TEXT NOT NULL, account_key TEXT NOT NULL,
                status TEXT NOT NULL, due REAL, message TEXT NOT NULL, PRIMARY KEY(batch_id,account_key));''')
            db.executescript('''CREATE TABLE IF NOT EXISTS activation_schedule(
              id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
              daily_time TEXT NOT NULL DEFAULT '08:00', next_due REAL, last_due REAL,
              last_outcome TEXT, last_note TEXT, flow_hold INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS activation_schedule_holds(
              account_key TEXT PRIMARY KEY, reason TEXT NOT NULL, created REAL NOT NULL);''')
            db.execute('INSERT OR IGNORE INTO activation_schedule(id) VALUES(1)')
            db.executescript('''CREATE TABLE IF NOT EXISTS activation_poll(
              id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
              interval_minutes INTEGER NOT NULL DEFAULT 30, next_due REAL, last_due REAL,
              last_account TEXT, last_outcome TEXT, last_note TEXT);
              INSERT OR IGNORE INTO activation_poll(id) VALUES(1);''')
            if 'origin' not in {r[1] for r in db.execute('PRAGMA table_info(batches)')}:
                db.execute("ALTER TABLE batches ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")
            if 'flow_refresh' not in {r[1] for r in db.execute('PRAGMA table_info(batches)')}:
                db.execute("ALTER TABLE batches ADD COLUMN flow_refresh TEXT NOT NULL DEFAULT 'pending'")
            if 'gemini_checked' not in {r[1] for r in db.execute('PRAGMA table_info(items)')}:
                db.execute('ALTER TABLE items ADD COLUMN gemini_checked INTEGER NOT NULL DEFAULT 0')
                db.execute("UPDATE items SET gemini_checked=1 WHERE status='done'")
            if 'service' not in {r[1] for r in db.execute('PRAGMA table_info(batches)')}:
                db.execute("ALTER TABLE batches ADD COLUMN service TEXT NOT NULL DEFAULT 'all'")
            if 'flow_task_id' not in {r[1] for r in db.execute('PRAGMA table_info(items)')}:
                db.execute('ALTER TABLE items ADD COLUMN flow_task_id TEXT')
            # Preserve the safety gate across restarts for operations already started.
            db.execute("""INSERT OR IGNORE INTO activation_schedule_holds(account_key,reason,created)
                          SELECT i.account_key,'定时检查中断，需手动核对',? FROM items i
                          JOIN batches b ON b.id=i.batch_id WHERE b.origin IN ('scheduled','poll') AND i.status='processing'""", (time.time(),))
            if db.execute("SELECT 1 FROM batches WHERE flow_refresh='issuing' LIMIT 1").fetchone():
                db.execute('UPDATE activation_schedule SET flow_hold=1 WHERE id=1')
            db.execute("UPDATE activation_schedule SET enabled=0,next_due=NULL,last_outcome='interrupted',last_note='启动时工作台中断，定时激活已暂停，请核查' WHERE last_outcome='starting'")
            db.execute("UPDATE activation_poll SET enabled=0,next_due=NULL,last_outcome='interrupted',last_note='启动时工作台中断，轮询已暂停，请核查' WHERE last_outcome='starting'")
            # An interrupted operation is never submitted again after restart.
            db.execute("UPDATE items SET status='interrupted', message='上次操作中断，结果待核对；没有自动重试' WHERE status IN ('queued','processing')")
            db.execute("UPDATE batches SET flow_refresh='unknown' WHERE flow_refresh='issuing'")
        if start_worker:
            self.thread = threading.Thread(target=self.run, name='account-activation', daemon=True)
            self.thread.start()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self):
        snapshot = self.sources.snapshot()
        accounts = snapshot.get('real_accounts', {})
        if not accounts.get('available') or not isinstance(accounts.get('items'), list):
            raise ValueError('账号状态暂不可读取')
        self.pool.annotate(accounts)
        return snapshot

    def status(self, service=None):
        if service not in (None, 'flow', 'gemini'):
            raise ValueError('请选择 Flow 或 Gemini')
        desktop = self.desktop_recovery_status()
        with self.db() as db:
            batch = db.execute('SELECT id,created,flow_refresh,service FROM batches' +
                               (' WHERE service=?' if service else '') + ' ORDER BY created DESC LIMIT 1',
                               (service,) if service else ()).fetchone()
            if not batch:
                return {'status': 'idle', 'service': service, 'items': [], 'desktop_recovery': desktop}
            items = [dict(r) for r in db.execute('SELECT account_key,status,due,message,flow_task_id FROM items WHERE batch_id=? ORDER BY rowid', (batch['id'],))]
        return {'status': 'running' if any(i['status'] in ('queued','processing','cooling','waiting_flow') for i in items) else 'finished',
                'id': batch['id'], 'service': batch['service'], 'created': batch['created'], 'flow_refresh': batch['flow_refresh'], 'items': items,
                'desktop_recovery': desktop}

    def desktop_recovery_status(self):
        path = self.path.parent / 'account-desktops.sqlite3'
        if not path.is_file() or path.is_symlink():
            return []
        try:
            db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
            try:
                rows = db.execute("SELECT account_key,reason FROM layouts WHERE status='blocked' ORDER BY changed DESC").fetchall()
                enrolled = db.execute("SELECT 1 FROM layouts WHERE status IN ('active','recovering') LIMIT 1").fetchone()
            finally:
                db.close()
            labels = {'flow_identity_unconfirmed':'Flow 登录身份未确认', 'flow_window_ambiguous':'Flow 窗口不唯一',
                      'flow_window_unmatched':'Flow 窗口归属未确认', 'gemini_window_ambiguous':'Gemini 窗口不唯一',
                      'gemini_pending':'存在未确认消息', 'gemini_busy':'账号正在处理任务',
                      'gemini_paused':'Gemini 已手动暂停', 'flow_window_unconfirmed':'Flow 窗口状态未确认',
                      'account_busy':'账号正在使用', 'flow_busy':'Flow 正在处理任务',
                      'flow_not_ready':'Flow 状态尚不可用', 'account_state_changed':'账号状态发生变化',
                      'recovery_interrupted':'上次恢复被中断', 'new_window_unconfirmed':'新窗口是否打开未确认',
                      'new_window_ambiguous':'新窗口无法唯一识别', 'no_empty_workspace':'没有可用的空桌面'}
            issues = [{'account_key':key, 'message':labels.get(reason,'窗口或账号状态无法确认') + '；已停止自动恢复，可先核查再手动一键激活'} for key,reason in rows]
            if enrolled and sys.platform.startswith('linux'):
                try:
                    daemon = subprocess.run(['systemctl','--user','is-active','--quiet','studiowright-account-desktops.service'], capture_output=True, timeout=2)
                    available = daemon.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    available = False
                if not available:
                    issues.append({'account_key':'', 'message':'Ubuntu 图形桌面或桌面恢复服务未就绪；请登录 Ubuntu 桌面后检查'})
            return issues
        except sqlite3.Error:
            return [{'account_key':'', 'message':'桌面恢复状态暂时无法读取，请检查工作台'}]

    @staticmethod
    def next_daily(daily_time, now):
        hour, minute = map(int, daily_time.split(':'))
        tz = timezone(timedelta(hours=8))
        current = datetime.fromtimestamp(now, tz)
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(days=1)
        return target.timestamp()

    def schedule_status(self):
        with self.db() as db:
            row = db.execute('SELECT * FROM activation_schedule WHERE id=1').fetchone()
            held = db.execute('SELECT count(*) FROM activation_schedule_holds').fetchone()[0]
        return {'enabled':bool(row['enabled']), 'time':row['daily_time'], 'timezone':'Asia/Shanghai',
                'next_due':row['next_due'], 'last_due':row['last_due'], 'last_outcome':row['last_outcome'],
                'last_note':row['last_note'], 'held_accounts':held, 'flow_hold':bool(row['flow_hold'])}

    def set_schedule(self, enabled, daily_time):
        if type(enabled) is not bool or not isinstance(daily_time, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', daily_time):
            raise ValueError('请选择正确的定时时间')
        with self.guard:
            if self.pool.maintenance():
                raise ValueError('工作台正在更新，请稍后操作')
            with self.db() as db:
                db.execute('UPDATE activation_schedule SET enabled=?,daily_time=?,next_due=? WHERE id=1',
                           (int(enabled), daily_time, self.next_daily(daily_time,time.time()) if enabled else None))
        self.wake_event.set()
        return self.schedule_status()

    def schedule_tick(self, now=None):
        now = time.time() if now is None else now
        with self.guard:
            if self.pool.maintenance():
                return
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM activation_schedule WHERE id=1').fetchone()
                if not row['enabled'] or row['next_due'] is None or row['next_due'] > now:
                    return
                due = row['next_due']
                next_due = self.next_daily(row['daily_time'], now)
                # Claim the date before taking action; never replay an interrupted or missed run.
                outcome = 'missed' if now - due > 120 else 'starting'
                note = '错过预定时间，本次跳过，不补跑' if outcome == 'missed' else '正在准备定时激活'
                db.execute('UPDATE activation_schedule SET next_due=?,last_due=?,last_outcome=?,last_note=? WHERE id=1',
                           (next_due,due,outcome,note))
            if outcome == 'missed':
                return
            if self.status()['status'] == 'running':
                with self.db() as db:
                    db.execute("UPDATE activation_schedule SET last_outcome='skipped',last_note='已有激活正在运行，本次跳过' WHERE id=1")
                return
            try:
                self.start(origin='scheduled')
                with self.db() as db:
                    db.execute("UPDATE activation_schedule SET last_outcome='started',last_note='定时激活已启动，查看本轮账号结果' WHERE id=1")
            except Exception:
                with self.db() as db:
                    db.execute("UPDATE activation_schedule SET enabled=0,next_due=NULL,last_outcome='failed',last_note='定时激活未能开始，已暂停且不会自动重试' WHERE id=1")

    @staticmethod
    def poll_delay(minutes):
        # Independent integer jitter in [85%, 115%], chosen and persisted per slot.
        base = minutes * 60
        spread = base * 15 // 100
        return base - spread + secrets.randbelow(2 * spread + 1)

    def poll_status(self):
        with self.db() as db:
            row = db.execute('SELECT * FROM activation_poll WHERE id=1').fetchone()
            held = db.execute('SELECT count(*) FROM activation_schedule_holds').fetchone()[0]
        return {'enabled':bool(row['enabled']), 'interval_minutes':row['interval_minutes'], 'jitter_percent':15,
                'next_due':row['next_due'], 'last_due':row['last_due'], 'last_account':row['last_account'],
                'last_outcome':row['last_outcome'], 'last_note':row['last_note'], 'held_accounts':held}

    def set_poll(self, enabled, interval_minutes):
        if type(enabled) is not bool or type(interval_minutes) is not int or not 1 <= interval_minutes <= 1440:
            raise ValueError('请输入 1 至 1440 分钟的轮询间隔')
        with self.guard:
            if self.pool.maintenance():
                raise ValueError('工作台正在更新，请稍后操作')
            with self.db() as db:
                previous = db.execute('SELECT enabled,interval_minutes,next_due FROM activation_poll WHERE id=1').fetchone()
                due = (previous['next_due'] if enabled and previous['enabled'] and previous['interval_minutes'] == interval_minutes
                       else time.time()+self.poll_delay(interval_minutes) if enabled else None)
                db.execute('UPDATE activation_poll SET enabled=?,interval_minutes=?,next_due=? WHERE id=1',
                           (int(enabled),interval_minutes,due))
        self.wake_event.set()
        return self.poll_status()

    def poll_tick(self, now=None):
        now = time.time() if now is None else now
        with self.guard:
            if self.pool.maintenance():
                return
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM activation_poll WHERE id=1').fetchone()
                if not row['enabled'] or row['next_due'] is None or row['next_due'] > now:
                    return
                missed = now - row['next_due'] > 120
                db.execute('UPDATE activation_poll SET next_due=?,last_due=?,last_outcome=?,last_note=? WHERE id=1',
                           (now+self.poll_delay(row['interval_minutes']),row['next_due'],
                            'missed' if missed else 'starting', '错过轮询时间，本次跳过，不补跑' if missed else '正在准备轮询'))
            if missed:
                return
            if self.status()['status'] == 'running':
                with self.db() as db:
                    db.execute("UPDATE activation_poll SET last_outcome='skipped',last_note='已有激活正在运行，本次跳过' WHERE id=1")
                return
            try:
                self.start(origin='poll')
                with self.db() as db:
                    db.execute("UPDATE activation_poll SET last_outcome='started',last_note='已安排一个账号，查看本轮账号结果' WHERE id=1")
            except Exception:
                with self.db() as db:
                    db.execute("UPDATE activation_poll SET enabled=0,next_due=NULL,last_outcome='failed',last_note='轮询未能开始，已暂停且不会自动重试' WHERE id=1")

    def start(self, origin='manual', service='all'):
        if origin not in ('manual','scheduled','poll'):
            raise ValueError('激活方式不正确')
        if service not in ('all', 'flow', 'gemini') or (origin != 'manual' and service != 'all'):
            raise ValueError('激活业务不正确')
        with self.guard:
            if self.pool.maintenance():
                raise ValueError('工作台正在更新，请稍后操作')
            if self.status()['status'] == 'running':
                raise ValueError('已有一轮账号检查正在进行或等待冷却')
            snapshot = self.snapshot()
            items = snapshot['real_accounts']['items']
            if service == 'flow':
                items = [a for a in items if not a.get('flow_removed')]
            keys = [a.get('key', '').removeprefix('flow-account-') for a in items]
            if not keys or len(keys) > 50 or len(set(keys)) != len(keys) or any(not re.fullmatch(r'[0-9a-f]{64}', k) for k in keys):
                raise ValueError('现有账号列表不能唯一确认')
            batch = str(uuid.uuid4())
            flow_refresh = 'skipped_service' if service == 'gemini' else 'pending'
            if origin == 'poll':
                keys.sort()
                with self.db() as db:
                    previous = db.execute('SELECT last_account FROM activation_poll WHERE id=1').fetchone()[0]
                keys = [next((key for key in keys if previous is None or key > previous), keys[0])]
                # The Flow service already refreshes balances; never trigger a pool-wide refresh per account.
                flow_refresh = 'skipped_poll'
            if origin == 'scheduled':
                if any(t.get('status') in ('queued','running') for t in snapshot.get('tasks', [])):
                    flow_refresh = 'skipped_busy'
                elif all(a.get('flow_balance_fresh') is True for a in items):
                    flow_refresh = 'skipped_fresh'
                elif any(a.get('flow_login') != 'verified' or not a.get('flow_pacing_known') or
                         not a.get('flow_risk_known') or a.get('flow_risk_paused') or a.get('flow_cooling') or
                         a.get('gemini_unresolved') or a.get('gemini_busy') for a in items):
                    flow_refresh = 'skipped_safety'
            with self.db() as db:
                db.execute('INSERT INTO batches(id,created,origin,flow_refresh,service) VALUES (?,?,?,?,?)', (batch,time.time(),origin,flow_refresh,service))
                db.executemany('INSERT INTO items(batch_id,account_key,status,due,message) VALUES (?,?,?,NULL,?)', [(batch,k,'queued','等待逐个核对') for k in keys])
                if origin == 'poll':
                    db.execute('UPDATE activation_poll SET last_account=? WHERE id=1', (keys[0],))
            self.wake_event.set()
            return self.status()

    def update(self, batch, key, status, message, due=None, checked=None):
        with self.db() as db:
            db.execute('UPDATE items SET status=?,message=?,due=?,gemini_checked=COALESCE(?,gemini_checked) WHERE batch_id=? AND account_key=?',
                       (status,message,due,checked,batch,key))
            row = db.execute('SELECT origin,service FROM batches WHERE id=?', (batch,)).fetchone()
            if row and row['origin'] in ('scheduled','poll') and status in ('failed','interrupted'):
                db.execute('INSERT OR REPLACE INTO activation_schedule_holds(account_key,reason,created) VALUES(?,?,?)',
                           (key, '上次定时检查失败或中断，需手动核对', time.time()))
            elif row and row['origin'] == 'manual' and row['service'] != 'flow' and status == 'done':
                db.execute('DELETE FROM activation_schedule_holds WHERE account_key=?', (key,))
            if row and row['origin'] == 'poll' and status in ('done','blocked','failed','interrupted'):
                db.execute('UPDATE activation_poll SET last_outcome=?,last_note=? WHERE id=1', (status,message))

    def open_browser(self, account):
        flow_python = Path.home() / '.local/share/studiowright-evaluation/venv/bin/python'
        helper = self.program_dir / 'activation_browser.py'
        if not flow_python.is_file() or not helper.is_file() or helper.is_symlink():
            return {'result': 'not_ready', 'reason': 'helper_unavailable'}
        try:
            result = subprocess.run([str(flow_python), '-B', str(helper), account], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45, check=False)
            data = json.loads(result.stdout)
            if result.returncode == 0 and data.get('result') == 'browser_ready':
                return data
            return {'result': 'not_ready', 'reason': data.get('reason', 'browser_unavailable')}
        except (OSError, ValueError, subprocess.SubprocessError):
            return {'result': 'not_ready', 'reason': 'browser_unavailable'}

    def process_one(self, batch, key, snapshot):
        account_data = snapshot['real_accounts']
        matches = [a for a in account_data['items'] if a.get('key') == 'flow-account-' + key]
        if len(matches) != 1:
            return self.update(batch,key,'blocked','账号归属无法确认，未操作')
        with self.db() as db:
            row = db.execute('SELECT service FROM batches WHERE id=?',(batch,)).fetchone()
        if row and (row['service']=='gemini' or row['service']=='all' and matches[0].get('flow_removed')):
            return self.process_gemini(batch,key)
        return self.process_flow(batch,key,matches[0],snapshot)

    @staticmethod
    def gemini_hold(db, key):
        from official_pool import unresolved_official,busy_official
        pref = db.execute('SELECT gemini_enabled FROM account_preferences WHERE account_key=?', (key,)).fetchone()
        if pref and pref[0] == 0:
            return '账号已手动暂停，本次保留'
        block = db.execute('SELECT reason FROM official_account_blocks WHERE account_key=?', (key,)).fetchone()
        if block and block[0] not in ('activation_verification_required','activation_quota_exhausted',
                                    'official_authorization_verification_required','official_quota_exhausted'):
            return '官方调用受限，需核查后恢复'
        if unresolved_official(db,key):
            return '官方通道有未确认的消息，本次保留限制'
        if busy_official(db,key):
            return '官方通道正在等待或执行任务，本次未操作'
        return None

    def process_gemini(self, batch, key):
        """Check official authorization/quota without reading or changing legacy monitor state."""
        official = getattr(self.pool, 'official', None)
        if not official or not official.runtime.account(key):
            return self.update(batch,key,'blocked','官方授权身份尚未就绪，请先检查登录')
        with self.pool.connection() as db:
            hold = self.gemini_hold(db,key)
            before = db.execute('SELECT reason FROM official_account_blocks WHERE account_key=?',(key,)).fetchone()
            before = tuple(before) if before else None
        if hold:
            return self.update(batch,key,'blocked',hold)
        from official_quota import check_account
        quota = check_account(official.runtime,key)
        if quota.get('status') != 'ok':
            messages = {
                'verification_required':'额度查询返回核验提示；已识别登录身份，额度暂未读到；未因此暂停对话',
                'authorization_required':'官方要求重新授权；请在官方 CLI 检查登录',
                'authorization_unavailable':'本地官方授权资料未就绪，额度暂未读到',
                'network_error':'连接官方服务失败，未读到额度；没有新增派发暂停',
                'eligibility_failed':'官方资格检查失败，但未确认需要额外核验；没有新增派发暂停'}
            return self.update(batch,key,'blocked',messages.get(quota.get('status'),'本轮未读到官方额度，不能据此认定账号失效；没有新增派发暂停'))
        for window in ('five_hour','weekly'):
            remaining = quota.get(window,{}).get('remaining_percent')
            if type(remaining) not in (int,float) or not 0 < remaining <= 100:
                if remaining == 0:
                    with self.pool.connection() as db:
                        db.execute('INSERT OR IGNORE INTO official_account_blocks(account_key,reason,created) VALUES(?,?,?)',
                                   (key,'official_quota_exhausted',time.time()))
                return self.update(batch,key,'blocked','官方额度已用尽，等待对应周期恢复' if remaining == 0 else '官方额度数据不完整，没有新增派发暂停')
        with self.pool.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                hold = self.gemini_hold(db,key)
                current = db.execute('SELECT reason FROM official_account_blocks WHERE account_key=?',(key,)).fetchone()
                if hold or (tuple(current) if current else None) != before:
                    db.execute('ROLLBACK')
                    return self.update(batch,key,'blocked',hold or '检查期间官方账号状态变化，请核对后再操作')
                db.execute("DELETE FROM official_account_blocks WHERE account_key=? AND reason IN ('activation_verification_required','activation_quota_exhausted','official_authorization_verification_required','official_quota_exhausted')",(key,))
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction: db.execute('ROLLBACK')
                raise
        self.update(batch,key,'done','官方授权与额度检查通过；未执行模型调用，未改变手动暂停或旧版记录')

    def process_flow(self, batch, key, account, snapshot):
        """Submit only one existing Flow login/project check; never generate or retry."""
        if account.get('flow_removed'):
            return self.update(batch,key,'blocked','账号已移出 Flow 号池，本次未检查 Flow。')
        if account.get('flow_page_incident'):
            return self.update(batch,key,'blocked','Flow 页面显示异常活动，等待人工核查')
        with self.db() as db:
            task_id = db.execute('SELECT flow_task_id FROM items WHERE batch_id=? AND account_key=?', (batch,key)).fetchone()[0]
        if task_id:
            tasks = [task for task in snapshot.get('tasks', []) if task.get('source_id') == task_id and task.get('source_profile') == account.get('profile')]
            if len(tasks) != 1:
                return self.update(batch,key,'blocked','已提交的 Flow 检查结果待核对；不会重复提交')
            task = tasks[0]
            if task.get('status') in ('queued','running'):
                return self.update(batch,key,'waiting_flow','Flow 正在检查登录与连接，请稍候')
            if task.get('status') == 'succeeded' and account.get('flow_login') == 'verified':
                return self.update(batch,key,'done','Flow 登录与后台连接已核对；积分以后台最新读取为准')
            return self.update(batch,key,'failed','Flow 检查未通过，请查看原任务详情或重新登录；不会自动重试')
        if not account.get('flow_pacing_known') or not account.get('flow_risk_known'):
            return self.update(batch,key,'blocked','Flow 风控或冷却状态无法确认，请先核查')
        if account.get('flow_risk_paused'):
            return self.update(batch,key,'blocked','Flow 风控暂停，需人工核查后恢复，本次未解除')
        if account.get('flow_cooling'):
            return self.update(batch,key,'blocked','Flow 仍在冷却，未提前解除；到期后可再次激活')
        if account.get('gemini_busy') or any(t.get('source_profile') == account.get('profile') and t.get('status') in ('queued','running') for t in snapshot.get('tasks', [])):
            return self.update(batch,key,'blocked','账号正在处理任务，本次未操作')
        if account.get('flow_health') == 'ready' and account.get('flow_login') == 'verified' and account.get('flow_balance_fresh') is True:
            return self.update(batch,key,'done','Flow 近期核对正常，无需重复恢复')
        request_key = 'activate-' + batch.replace('-', '') + '-' + key[:32]
        try:
            task_id = self.sources.flow_activation_check(account['profile'], request_key)
        except Exception:
            return self.update(batch,key,'failed','Flow 检查提交结果未确认；请查原任务，不会自动重新提交')
        with self.db() as db:
            db.execute("UPDATE items SET flow_task_id=?,status='waiting_flow',message='Flow 检查已排队，等待原任务结果' WHERE batch_id=? AND account_key=?", (task_id,batch,key))

    def watch_cooldowns(self):
        """Track new cooldown episodes without repeating Gemini checks."""
        with self.db() as db:
            batch = db.execute('SELECT id,origin,service FROM batches ORDER BY created DESC LIMIT 1').fetchone()
            if not batch or batch['origin'] == 'poll' or batch['service'] != 'all':
                return
            keys = {r[0] for r in db.execute("SELECT account_key FROM items WHERE batch_id=? AND status='done'", (batch['id'],))}
        if not keys:
            return
        snapshot = self.snapshot()
        for account in snapshot['real_accounts']['items']:
            key = account.get('key','').removeprefix('flow-account-')
            seconds = account.get('flow_cooldown_seconds')
            if (key in keys and account.get('flow_cooling') is True and account.get('flow_pacing_known') is True
                    and account.get('flow_risk_known') is True and account.get('flow_risk_paused') is False
                    and type(seconds) in (int,float) and seconds > 0):
                self.update(batch['id'],key,'cooling','Flow 冷却中，到期后核对状态并恢复',time.time()+seconds)

    def tick(self):
        if self.pool.maintenance():
            return
        self.schedule_tick()
        self.poll_tick()
        with self.guard:
            with self.db() as db:
                batch = db.execute("SELECT id,origin FROM batches WHERE flow_refresh='pending' AND EXISTS (SELECT 1 FROM items WHERE items.batch_id=batches.id AND status IN ('queued','processing','cooling','waiting_flow')) ORDER BY created LIMIT 1").fetchone()
                if batch and batch['origin'] == 'scheduled' and db.execute('SELECT flow_hold FROM activation_schedule WHERE id=1').fetchone()[0]:
                    db.execute("UPDATE batches SET flow_refresh='held' WHERE id=?", (batch['id'],))
                    batch = None
                if batch:
                    db.execute("UPDATE batches SET flow_refresh='issuing' WHERE id=?", (batch['id'],))
            if batch:
                try:
                    acknowledged = self.sources.refresh_flow_balances_once()
                except Exception:
                    acknowledged = False
                with self.db() as db:
                    db.execute('UPDATE batches SET flow_refresh=? WHERE id=?', ('accepted' if acknowledged else 'unknown',batch['id']))
                    if not acknowledged:
                        db.execute('UPDATE activation_schedule SET flow_hold=1 WHERE id=1')
                    elif batch['origin'] == 'manual' and acknowledged:
                        db.execute('UPDATE activation_schedule SET flow_hold=0 WHERE id=1')
            with self.db() as db:
                row = db.execute("SELECT batch_id,account_key,status,due FROM items WHERE status IN ('queued','waiting_flow') OR (status='cooling' AND due<=?) ORDER BY CASE status WHEN 'waiting_flow' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,rowid LIMIT 1", (time.time(),)).fetchone()
            if not row:
                if time.monotonic() >= self.next_watch:
                    self.next_watch = time.monotonic() + 30
                    self.watch_cooldowns()
                return
            batch,key = row['batch_id'],row['account_key']
            # Reserve before any action. A restart leaves this interrupted, never retried.
            self.update(batch,key,'processing','正在核对账号状态')
        try:
            self.process_one(batch,key,self.snapshot())
        except Exception:
            self.update(batch,key,'failed','状态检查未完成，已停止，不会自动重试')

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self.wake_event.wait(5)
            self.wake_event.clear()

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
