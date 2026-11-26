"""Account-pool health and credit reporting, independent of generation policy.

Balances are observations, job prices are recorded usage, never a reconstructed
Google billing ledger. Missing history and stale observations stay explicit.
"""
import json
import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
FRESH_SECONDS = 900


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


class Insights:
    def __init__(self, store, source):
        self.store, self.source = store, Path(source)
        store.execute("""CREATE TABLE IF NOT EXISTS account_balances(
            profile TEXT PRIMARY KEY,balance REAL,observed REAL,checked REAL,
            status TEXT,error TEXT)""")
        store.execute("CREATE TABLE IF NOT EXISTS account_credit_renewals(profile TEXT PRIMARY KEY,next_date TEXT,observed REAL)")

    def record(self, profile, balance=None, error=None, now=None, next_credit_date=None, *, transient=False):
        now = time.time() if now is None else now
        if error is None and number(balance) is None:
            raise ValueError('余额不正确。')
        if error is None:
            self.store.execute("INSERT INTO account_balances VALUES(?,?,?,?,'ready',NULL) ON CONFLICT(profile) DO UPDATE SET balance=excluded.balance,observed=excluded.observed,checked=excluded.checked,status='ready',error=NULL", (profile, balance, now, now))
            self.store.execute("INSERT INTO account_credit_renewals VALUES(?,?,?) ON CONFLICT(profile) DO UPDATE SET next_date=excluded.next_date,observed=excluded.observed", (profile,next_credit_date,now))
        else:
            self.store.execute("""INSERT INTO account_balances VALUES(?,NULL,NULL,?,'unknown',?)
                ON CONFLICT(profile) DO UPDATE SET checked=excluded.checked,error=excluded.error,
                status=CASE WHEN ? AND account_balances.status IN ('ready','retrying')
                            THEN 'retrying' ELSE 'unknown' END""", (profile, now, error, transient))

    def source_data(self):
        try:
            with sqlite3.connect(self.source.resolve().as_uri()+'?mode=ro',uri=True,timeout=1) as c:
                c.row_factory=sqlite3.Row
                jobs=[dict(r) for r in c.execute("SELECT id,profile,kind,status,result,created,finished FROM jobs WHERE kind IN ('image','video')")]
                balances={}
                for k,v in c.execute("SELECT key,value FROM settings WHERE key LIKE 'flow.credits.%'"):
                    try:
                        obj=json.loads(v)
                        if number(obj.get('credits')) is not None and number(obj.get('at')) is not None:
                            balances[k[len('flow.credits.'):]]={'balance':obj['credits'],'observed':obj['at'],'checked':None,'status':'cached','error':None}
                    except (ValueError,TypeError,AttributeError):pass
                return jobs,balances,True
        except (OSError,sqlite3.Error):return [],{},False

    def report(self, accounts, now=None, refreshing=False):
        now=time.time() if now is None else now
        jobs,cached,source_readable=self.source_data()
        balances={r['profile']:r for r in self.store.rows('SELECT * FROM account_balances')}
        renewals={r['profile']:r for r in self.store.rows('SELECT * FROM account_credit_renewals')}
        rows=[]
        for a in accounts:
            b=balances.get(a['profile']) or cached.get(a['profile']) or {}
            if number(b.get('balance')) is None and a['profile'] in cached:
                b={**cached[a['profile']], 'status':b.get('status','cached'),'error':b.get('error'),'checked':b.get('checked')}
            value=number(b.get('balance'));observed=number(b.get('observed'))
            fresh=value is not None and observed is not None and 0<=now-observed<=FRESH_SECONDS and b.get('status') in ('ready','retrying')
            if a.get('flow_page_incident'):health,reason='paused','页面显示异常活动，等待人工核查'
            elif a.get('operator_paused') is True:health,reason='paused','已手动暂停 Flow 派发'
            elif a.get('risk_paused') is True:health,reason='paused','多次异常，等待人工核查后恢复'
            elif a.get('state')!='verified':health,reason='attention','登录尚未核对或需要检查'
            elif a.get('generation_state')=='cooling_down':health,reason='cooling','异常活动冷却中'
            elif a.get('risk_paused') is not False:health,reason='attention','账号限制状态未读到'
            elif a.get('generation_state')!='not_cooling_down':health,reason='attention','冷却状态未读到'
            elif a.get('dispatch_preflight_reasons'):health,reason='attention','账号派发自检未通过'
            elif not fresh:health,reason='attention',b.get('error') or ('余额待更新' if value is not None else '余额尚未读到')
            elif value<=0:health,reason='empty','积分已用尽'
            else:health,reason='ready','最近登录已核对、未冷却且有余额'
            rows.append({'profile':a['profile'],'email':a.get('email'), 'health':health,'reason':reason,
                         'balance':value,'balance_observed':observed,'balance_checked':b.get('checked'),
                         'next_credit_date':renewals.get(a['profile'],{}).get('next_date'),
                         'balance_fresh':fresh,'balance_error':b.get('error')})
        profiles={a['profile'] for a in accounts}
        jobs=[j for j in jobs if j['profile'] in profiles]
        records=self.store.rows("SELECT id,profile,kind,result,created,finished,status FROM tasks WHERE kind IN ('image','video')")
        records=[r for r in records if r['profile'] in profiles]
        # Prefer source jobs. Include console-only jobs only when there is no linked
        # source ID; a batch and its console wrapper must never count twice.
        source_ids={j['id'] for j in jobs}
        unmatched_completed=0
        for r in records:
            try:result=json.loads(r['result'] or '{}')
            except (ValueError,TypeError):result={}
            linked=[result.get('job_id')]+[j.get('job_id') for j in result.get('source_jobs',[]) if isinstance(j,dict)]
            if any(ident in source_ids for ident in linked):continue
            # Unlinked console rows are not guessed into a billing record.
            if r['status']=='succeeded':unmatched_completed+=1
        start=datetime.fromtimestamp(now,CST).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        # Observation coverage starts when this console first recorded a task.
        first=self.store.rows('SELECT min(created) AS first FROM tasks')[0]['first']
        if jobs:first=min([j['created'] for j in jobs]+([first] if first is not None else []))
        def usage(begin,end):
            completed=[];pending=[]
            for j in jobs:
                stamp=j.get('finished') or j['created']
                if not begin<=stamp<end or j['status']=='dry-run':continue
                if j['status']=='done':completed.append(j)
                elif j['status'] in ('failed','interrupted','running','queued'):pending.append(j)
            known=0;unpriced=0
            for j in completed:
                try:result=json.loads(j.get('result') or '{}')
                except (ValueError,TypeError):result={}
                price=number(result.get('credits'))
                if price is None:unpriced+=1
                else:known+=price
            missing=first is None or first>=end or not source_readable
            return {'recorded_usage':None if missing else known,'completed':len(completed),'unpriced':unpriced,
                    'unresolved':len(pending),'coverage':'not_recorded' if missing else 'partial' if first>begin or unpriced or pending else 'recorded',
                    'basis':'completed_job_price','billing_verified':False}
        known_rows=[r for r in rows if r['balance'] is not None]
        ready=[r for r in rows if r['health']=='ready']
        return {'accounts':rows,'total_accounts':len(rows),'healthy_accounts':len(ready),
                'health_percent':round(len(ready)/len(rows)*100) if rows else None,
                'health_counts':{k:sum(r['health']==k for r in rows) for k in ('ready','cooling','attention','empty','paused')},
                'pool_balance':sum(r['balance'] for r in known_rows) if known_rows else None,
                'balance_known_accounts':len(known_rows),'balance_fresh_accounts':sum(r['balance_fresh'] for r in rows),
                'available_today':sum(r['balance'] for r in ready) if known_rows else None,
                'balance_oldest_observed':min((r['balance_observed'] for r in known_rows if r['balance_observed'] is not None),default=None),
                'yesterday':usage(start-86400,start),'today':usage(start,start+86400),
                'today_date':datetime.fromtimestamp(now,CST).strftime('%Y-%m-%d'),
                'timezone':'Asia/Shanghai','refreshing':refreshing,'updated':now,
                'unlinked_completed_tasks':unmatched_completed,
                'note':'余额为最近读取值；当前可用只计登录已核对、未冷却且余额在15分钟内更新的账号。用量按完成任务记录的价格汇总，不等于Google实扣账单，报价及未完成任务不计入。'}
