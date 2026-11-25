import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path


MAX_GENERATION_RETRIES = 3
SAFE_VIDEO_RETRY = frozenset(('transient_before_submit', 'provider_rejected_unusual'))
RETRY_KINDS = frozenset(('image', 'video'))
SESSION_IMPORT_INTERRUPTED = '登录核对因后台重启中断。请从 Windows 账号助手重新发起同步。'


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "console.db"
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS accounts(profile TEXT PRIMARY KEY, label TEXT, email TEXT, checked REAL, state TEXT DEFAULT 'unknown', error TEXT);
            CREATE TABLE IF NOT EXISTS flow_removed_accounts(profile TEXT PRIMARY KEY, removed REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS flow_page_incidents(
                id TEXT PRIMARY KEY, profile TEXT NOT NULL, phase TEXT NOT NULL,
                observed REAL NOT NULL, screenshot BLOB, reviewed REAL);
            CREATE UNIQUE INDEX IF NOT EXISTS active_flow_page_incident
                ON flow_page_incidents(profile) WHERE reviewed IS NULL;
            CREATE TABLE IF NOT EXISTS flow_destroyed_screenshots(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS projects(profile TEXT, id TEXT, title TEXT, checked REAL, PRIMARY KEY(profile,id));
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, profile TEXT, project TEXT, kind TEXT, params TEXT, status TEXT, result TEXT, error TEXT, created REAL, started REAL, finished REAL, request_key TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS flow_retry_roots(root_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS flow_retry_attempts(task_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, ordinal INTEGER NOT NULL, reason TEXT NOT NULL, UNIQUE(root_id, ordinal));
            CREATE TABLE IF NOT EXISTS flow_retry_manual_roots(root_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS flow_retry_executions(
                root_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>0),
                reason TEXT NOT NULL, profile TEXT NOT NULL, project TEXT NOT NULL, kind TEXT NOT NULL,
                params TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT, created REAL NOT NULL,
                started REAL, finished REAL, request_key TEXT NOT NULL UNIQUE, PRIMARY KEY(root_id,ordinal));
            CREATE VIEW IF NOT EXISTS flow_task_execution_rows AS
                SELECT t.*, COALESCE(a.root_id,t.id) AS root_id, COALESCE(a.ordinal,0) AS ordinal,
                       a.reason AS retry_reason, 0 AS attempt_ordinal
                FROM tasks t LEFT JOIN flow_retry_attempts a ON a.task_id=t.id
                UNION ALL
                SELECT root_id,profile,project,kind,params,status,result,error,created,started,finished,
                       request_key,root_id,ordinal,reason,ordinal FROM flow_retry_executions;
            CREATE TABLE IF NOT EXISTS flow_retry_projects(profile TEXT PRIMARY KEY, project TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets(hash TEXT PRIMARY KEY, expires REAL);
            CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY, expires REAL);
            """)
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def rows(self, sql, args=()):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).rowcount

    def accounts(self):
        return self.rows("""SELECT a.*, r.profile IS NOT NULL AS flow_removed
            FROM accounts a LEFT JOIN flow_removed_accounts r ON r.profile=a.profile ORDER BY a.profile""")

    def remove_flow_account(self, profile):
        """Remove pool membership while retaining the browser, projects and task history."""
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM accounts WHERE profile=? AND email IS NOT NULL', (profile,)).fetchone():
                raise ValueError('请选择已登记的 Flow 账号。')
            if db.execute("""SELECT 1 FROM flow_task_execution_rows e
                    LEFT JOIN flow_retry_roots r ON r.root_id=e.root_id
                    WHERE e.profile=? AND e.kind IN ('image','video')
                      AND (e.status IN ('queued','running') OR r.state='pending_switch')
                    LIMIT 1""", (profile,)).fetchone():
                return False
            if db.execute("""SELECT 1 FROM tasks WHERE profile=?
                    AND kind='session_import' AND status IN ('queued','running') LIMIT 1""", (profile,)).fetchone():
                return False
            db.execute('INSERT OR IGNORE INTO flow_removed_accounts VALUES(?,?)', (profile, time.time()))
            return True

    def page_incidents(self):
        return {row['profile']: row for row in self.rows(
            """SELECT p.profile,p.id,p.phase,p.observed,p.screenshot IS NOT NULL AS has_screenshot,
                d.id IS NOT NULL AS screenshot_destroyed
            FROM flow_page_incidents p LEFT JOIN flow_destroyed_screenshots d ON d.id=p.id
            WHERE p.reviewed IS NULL""")}

    def record_page_incident(self, profile, phase, screenshot):
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO flow_page_incidents(id,profile,phase,observed,screenshot)
                VALUES(?,?,?,?,?) ON CONFLICT(profile) WHERE reviewed IS NULL DO UPDATE SET
                screenshot=COALESCE(flow_page_incidents.screenshot,excluded.screenshot)
                WHERE NOT EXISTS(SELECT 1 FROM flow_destroyed_screenshots WHERE id=flow_page_incidents.id)""",
                (secrets.token_hex(16), profile, phase, time.time(), screenshot))

    def review_page_incident(self, profile, ident):
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA secure_delete=ON")
            return db.execute("""UPDATE flow_page_incidents SET reviewed=?,screenshot=NULL
                WHERE profile=? AND id=? AND reviewed IS NULL""", (time.time(), profile, ident)).rowcount == 1

    def account(self, profile):
        rows = self.rows("SELECT * FROM accounts WHERE profile=?", (profile,))
        return rows[0] if rows else None

    def add_account(self, profile, label):
        self.execute(
            "INSERT INTO accounts(profile,label) VALUES(?,?) ON CONFLICT(profile) DO UPDATE SET label=excluded.label",
            (profile, label),
        )

    def update_account(self, profile, email, state, error=None):
        self.execute(
            "UPDATE accounts SET email=?,state=?,error=?,checked=? WHERE profile=?",
            (email, state, error, time.time(), profile),
        )

    def project(self, profile, project, title="已登记项目"):
        self.execute(
            "INSERT INTO projects VALUES(?,?,?,?) ON CONFLICT(profile,id) DO UPDATE SET title=excluded.title,checked=excluded.checked",
            (profile, project, title, time.time()),
        )

    def has_project(self, profile, project):
        return bool(
            self.rows("SELECT id FROM projects WHERE profile=? AND id=?", (profile, project))
        )

    def projects(self, profile=None):
        return self.rows(
            "SELECT * FROM projects"
            + (" WHERE profile=?" if profile else "")
            + " ORDER BY checked DESC,id",
            (profile,) if profile else (),
        )

    def sync_observed_project_names(self, database=None):
        """Repair only unnamed, already-owned projects from verified name observations."""
        database = Path(database) if database else Path.home()/'.local/state/creative-unified-workbench/project-names.sqlite3'
        if database.is_symlink() or not database.is_file():
            return 0
        try:
            with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as source:
                observed = {(p,i):t for p,i,t in source.execute(
                    "SELECT profile,project,title FROM names WHERE source IN ('open_flow_tab','browser_history')")}
        except (OSError,sqlite3.Error):
            return 0
        changed = 0
        with closing(self.connect()) as db, db:
            for row in db.execute("SELECT p.profile,p.id,a.email FROM projects p JOIN accounts a ON a.profile=p.profile "
                                  "WHERE a.state='verified' AND p.title IN ('','未命名项目')").fetchall():
                email = row['email'] or ''
                if '@' not in email:
                    continue
                expected = 'FLOW+' + email.split('@',1)[0]
                title = observed.get((row['profile'],row['id']))
                if isinstance(title,str) and title.strip().casefold()==expected.casefold():
                    changed += db.execute("UPDATE projects SET title=? WHERE profile=? AND id=? AND title IN ('','未命名项目')",
                                          (expected,row['profile'],row['id'])).rowcount
        return changed

    @staticmethod
    def decode(row):
        for key in ("params", "result"):
            row[key] = json.loads(row[key]) if row[key] else None
        return row

    def task(self, ident):
        rows = self.rows("SELECT * FROM tasks WHERE id=?", (ident,))
        return self.decode(rows[0]) if rows else None

    def execution(self, ident, ordinal=0):
        if not ordinal:
            return self.task(ident)
        rows = self.rows("SELECT * FROM flow_task_execution_rows WHERE id=? AND attempt_ordinal=?", (ident, ordinal))
        return self.decode(rows[0]) if rows else None

    def latest_execution(self, ident, profile=None):
        rows = self.rows("SELECT * FROM flow_task_execution_rows WHERE id=? AND (? IS NULL OR profile=?) ORDER BY ordinal DESC LIMIT 1",
                         (ident,profile,profile))
        return self.decode(rows[0]) if rows else None

    def tasks(self):
        # The recent window is measured in logical tasks, not internal attempts.
        return [self.decode(row) for row in self.rows("""
            SELECT t.* FROM tasks t JOIN (
                SELECT root_id AS id, MAX(created) AS latest
                FROM flow_task_execution_rows GROUP BY root_id
                ORDER BY latest DESC, id DESC LIMIT 150
            ) recent ON recent.id=t.id ORDER BY recent.latest DESC,t.id DESC
        """)]

    def create_task(self, profile, project, kind, params, key, auto_retry=None):
        single_generation = kind in RETRY_KINDS and params.get('count') == 1
        if auto_retry is True and not single_generation:
            raise ValueError('换号重试只支持每次生成一张图片或一段视频。')
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM tasks WHERE request_key=?", (key,)).fetchone()
            if previous:
                row = self.decode(dict(previous))
                if (row["profile"], row["project"], row["kind"], row["params"]) != (
                    profile,
                    project,
                    kind,
                    params,
                ):
                    raise ValueError("同一提交编号不能用于不同任务，请重新提交。")
                if single_generation and auto_retry is not None:
                    recorded = db.execute("""SELECT 1 FROM flow_retry_attempts a
                        WHERE a.task_id=? AND NOT EXISTS
                        (SELECT 1 FROM flow_retry_manual_roots m WHERE m.root_id=a.root_id)""",
                        (row["id"],)).fetchone()
                    if bool(recorded) != auto_retry:
                        raise ValueError('同一提交编号不能更改自动重试设置。')
                return row
            if kind in RETRY_KINDS and db.execute(
                    'SELECT 1 FROM flow_removed_accounts WHERE profile=?', (profile,)).fetchone():
                raise ValueError('该账号已从 Flow 号池移除。')
            if kind == "sync":
                pending = db.execute("SELECT * FROM tasks WHERE profile=? AND kind='sync' AND status IN ('queued','running') ORDER BY created DESC LIMIT 1", (profile,)).fetchone()
                if pending:
                    return self.decode(dict(pending))
            ident = secrets.token_hex(8)
            db.execute(
                "INSERT INTO tasks(id,profile,project,kind,params,status,created,request_key) VALUES(?,?,?,?,?,?,?,?)",
                (
                    ident,
                    profile,
                    project,
                    kind,
                    json.dumps(params, ensure_ascii=False),
                    "queued",
                    time.time(),
                    key,
                ),
            )
            if single_generation:
                db.execute("INSERT INTO flow_retry_roots VALUES(?,'active',?)", (ident, time.time()))
                db.execute("INSERT INTO flow_retry_attempts VALUES(?,?,0,'original')", (ident, ident))
                if auto_retry is False:
                    db.execute("INSERT INTO flow_retry_manual_roots VALUES(?)", (ident,))
        return self.task(ident)

    def recover(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            db.execute("UPDATE flow_retry_roots SET state='interrupted',updated=? WHERE root_id IN (SELECT a.root_id FROM flow_retry_attempts a JOIN tasks t ON t.id=a.task_id WHERE t.status='running')", (now,))
            db.execute("UPDATE flow_retry_roots SET state='interrupted',updated=? WHERE root_id IN (SELECT root_id FROM flow_retry_executions WHERE status='running')", (now,))
            db.execute("UPDATE flow_retry_executions SET status='interrupted',finished=?,error='后台曾中断；重试可能已提交，请先核对原任务。' WHERE status='running'", (now,))
            db.execute("""UPDATE tasks SET status='interrupted',finished=?,error=CASE kind
                        WHEN 'session_import' THEN ?
                        ELSE '后台曾中断；任务可能已提交，不会自动重试。请先核对原任务和项目素材。'
                        END WHERE status='running'""", (now, SESSION_IMPORT_INTERRUPTED))

    def requeue_restored_import(self, ident):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT params FROM tasks WHERE id=? AND kind='session_import' AND status='running'",
                             (ident,)).fetchone()
            if row is None:
                return False
            params = json.loads(row['params'])
            if params.get('restart_retries', 0) != 0:
                return False
            params['restart_retries'] = 1
            db.execute("UPDATE tasks SET status='queued',started=NULL,finished=NULL,error=NULL,params=? WHERE id=?",
                       (json.dumps(params), ident))
            return True

    def claim(self, busy, generation_allowed=None, retry_switch_allowed=None, generation_shareable=None,
              active_generation_projects=None, request_keys=None, *, session_import_allowed=None):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            importing = db.execute("SELECT 1 FROM tasks WHERE kind='session_import' AND status='running' LIMIT 1").fetchone()
            rows = db.execute(
                """SELECT e.* FROM flow_task_execution_rows e WHERE e.status='queued'
                AND (e.kind NOT IN ('image','video') OR NOT EXISTS
                     (SELECT 1 FROM flow_removed_accounts r WHERE r.profile=e.profile))
                ORDER BY e.created"""
            ).fetchall()
            row = next((r for r in rows if (request_keys is None or r['request_key'] in request_keys) and r["profile"] not in busy and
                        (r['kind'] != 'session_import' or (not importing and
                         (session_import_allowed is None or r['id'] in session_import_allowed))) and
                        (r['kind'] not in ('image', 'video') or
                         (r['profile'], r['project']) not in (active_generation_projects or set())) and
                        (generation_allowed is None or r["kind"] not in ('image', 'video') or
                         r["profile"] in generation_allowed) and
                        (retry_switch_allowed is None or r['retry_reason'] != 'switch_account' or
                         r['profile'] in retry_switch_allowed)), None)
            if not row:
                return None
            started = time.time()
            if row['attempt_ordinal']:
                db.execute("UPDATE flow_retry_executions SET status='running',started=? WHERE root_id=? AND ordinal=?",
                           (started,row['id'],row['attempt_ordinal']))
            else:
                db.execute("UPDATE tasks SET status='running',started=? WHERE id=?", (started,row['id']))
            return self.decode(dict(row, status='running', started=started))

    def release_unsubmitted(self, ident, attempt_ordinal=0):
        """Return a newly claimed task before calling an adapter or provider."""
        if attempt_ordinal:
            self.execute("UPDATE flow_retry_executions SET status='queued',started=NULL WHERE root_id=? AND ordinal=? AND status='running'", (ident,attempt_ordinal))
        else:
            self.execute("UPDATE tasks SET status='queued',started=NULL WHERE id=? AND status='running'", (ident,))

    def finish(self, ident, result=None, error=None, retry_evidence=None, *, attempt_ordinal=0):
        """Record the outcome and next safe attempt together, at most once."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if attempt_ordinal:
                row = db.execute("SELECT * FROM flow_retry_executions WHERE root_id=? AND ordinal=?", (ident,attempt_ordinal)).fetchone()
                attempt = row
            else:
                row = db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
                attempt = db.execute("SELECT * FROM flow_retry_attempts WHERE task_id=?", (ident,)).fetchone()
            if not row:
                return
            # Legacy callers can finish their own tasks as before. Retry attempts
            # may transition only once from running, including across a restart.
            if attempt and row["status"] != "running":
                return
            now = time.time()
            outcome = ("failed" if error else "succeeded",
                       json.dumps(result, ensure_ascii=False) if result is not None else None, error, now)
            if attempt_ordinal:
                db.execute("UPDATE flow_retry_executions SET status=?,result=?,error=?,finished=? WHERE root_id=? AND ordinal=?",
                           (*outcome,ident,attempt_ordinal))
            else:
                db.execute("UPDATE tasks SET status=?,result=?,error=?,finished=? WHERE id=?", (*outcome,ident))
            if not attempt:
                return
            root, ordinal = attempt["root_id"], attempt["ordinal"]
            if not error:
                state = 'succeeded' if result and result.get('files') else 'needs_review'
            elif retry_evidence == 'confirmed_before_submit':
                state = 'manual_failed'
            elif attempt['reason'] == 'same_account':
                state = 'manual_failed' if retry_evidence in SAFE_VIDEO_RETRY else 'needs_review'
            elif retry_evidence not in SAFE_VIDEO_RETRY:
                state = 'needs_review'
            elif db.execute('SELECT 1 FROM flow_retry_manual_roots WHERE root_id=?', (root,)).fetchone():
                state = 'manual_failed'
            elif ordinal < MAX_GENERATION_RETRIES:
                state = 'pending_switch'
            else:
                state = 'exhausted'
            db.execute("UPDATE flow_retry_roots SET state=?,updated=? WHERE root_id=?", (state, now, root))

    def enroll_failed_video(self, ident, evidence):
        """Internal maintenance entry, after correlating proof of no output."""
        if evidence not in SAFE_VIDEO_RETRY:
            return False
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=? AND kind='video' AND status='failed' AND finished IS NOT NULL", (ident,)).fetchone()
            if (not row or json.loads(row['params']).get('count') != 1 or
                db.execute("SELECT 1 FROM flow_retry_attempts WHERE task_id=?", (ident,)).fetchone()):
                return False
            db.execute("INSERT INTO flow_retry_roots VALUES(?,'pending_switch',?)", (ident, time.time()))
            db.execute("INSERT INTO flow_retry_attempts VALUES(?,?,0,'original')", (ident, ident))
            return True

    def stop_retry_wait(self, root, state):
        if state not in ('references_unavailable', 'no_account'):
            raise ValueError('Unsupported retry stop state')
        return self.execute("UPDATE flow_retry_roots SET state=?,updated=? WHERE root_id=? AND state='pending_switch'",
                            (state,time.time(),root))

    def retry_detail(self, ident):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN')
            row = db.execute("""SELECT a.root_id,a.ordinal,a.reason,r.state,m.root_id IS NULL AS auto_retry
                FROM flow_retry_attempts a JOIN flow_retry_roots r ON r.root_id=a.root_id
                LEFT JOIN flow_retry_manual_roots m ON m.root_id=a.root_id WHERE a.task_id=?""",
                (ident,)).fetchone()
            if not row:
                return None
            info = dict(row)
            info['auto_retry'] = bool(info['auto_retry'])
            info['attempts'] = [dict(r) for r in db.execute("""
                SELECT ordinal,retry_reason AS reason,id,profile,project,status,started,finished,error,attempt_ordinal
                FROM flow_task_execution_rows WHERE root_id=? ORDER BY ordinal
            """, (info['root_id'],))]
            return info

    def pending_retry_roots(self):
        return self.rows("""SELECT r.root_id,r.updated FROM flow_retry_roots r
            WHERE r.state='pending_switch' AND NOT EXISTS
            (SELECT 1 FROM flow_retry_manual_roots m WHERE m.root_id=r.root_id) ORDER BY r.updated""")

    def expire_unstarted_switches(self, now):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT t.id,a.root_id,a.ordinal FROM tasks t
                JOIN flow_retry_attempts a ON a.task_id=t.id JOIN flow_retry_roots r ON r.root_id=a.root_id
                WHERE t.status='queued' AND t.started IS NULL AND a.reason='switch_account'
                AND r.state='active' AND t.created<?""", (now-900,)).fetchall()
            for row in rows:
                db.execute("UPDATE tasks SET status='failed',finished=?,error=? WHERE id=?",
                           (now,'分配后账号一直不可用，本次未发送。',row['id']))
                db.execute("UPDATE flow_retry_roots SET state=?,updated=? WHERE root_id=?",
                           ('exhausted' if row['ordinal'] >= MAX_GENERATION_RETRIES else 'pending_switch', now, row['root_id']))

            rows = db.execute("""SELECT e.root_id,e.ordinal FROM flow_retry_executions e
                JOIN flow_retry_roots r ON r.root_id=e.root_id
                WHERE e.status='queued' AND e.started IS NULL AND e.reason='switch_account'
                AND r.state='active' AND e.created<?""", (now-900,)).fetchall()
            for row in rows:
                db.execute("UPDATE flow_retry_executions SET status='failed',finished=?,error=? WHERE root_id=? AND ordinal=?",
                           (now,'分配后账号一直不可用，本次未发送。',row['root_id'],row['ordinal']))
                db.execute("UPDATE flow_retry_roots SET state=?,updated=? WHERE root_id=?",
                           ('exhausted' if row['ordinal'] >= MAX_GENERATION_RETRIES else 'pending_switch',now,row['root_id']))

    def set_retry_project(self, profile, project):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if project is None:
                db.execute('DELETE FROM flow_retry_projects WHERE profile=?', (profile,))
                return
            if not db.execute('SELECT 1 FROM projects WHERE profile=? AND id=?', (profile, project)).fetchone():
                raise ValueError('该项目未登记在所选账号下。')
            db.execute('INSERT INTO flow_retry_projects VALUES(?,?,?) ON CONFLICT(profile) DO UPDATE SET project=excluded.project,updated=excluded.updated', (profile, project, time.time()))

    def retry_projects(self):
        return self.rows('SELECT profile,project,updated FROM flow_retry_projects')

    def enqueue_switch(self, root, profile, project, params, expected_ordinal):
        """Atomically create one attempt. A lost response or restart cannot add duplicates."""
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute("""SELECT r.state FROM flow_retry_roots r WHERE r.root_id=?
                AND NOT EXISTS (SELECT 1 FROM flow_retry_manual_roots m WHERE m.root_id=r.root_id)""",
                (root,)).fetchone()
            if not current or current['state'] != 'pending_switch':
                return False
            if db.execute('SELECT 1 FROM flow_removed_accounts WHERE profile=?', (profile,)).fetchone():
                return False
            candidates = db.execute("SELECT p.id FROM accounts a JOIN projects p ON p.profile=a.profile WHERE a.profile=? AND a.state='verified' AND p.title='FLOW+' || substr(a.email,1,instr(a.email,'@')-1)", (profile,)).fetchall()
            used = db.execute('SELECT 1 FROM flow_task_execution_rows WHERE root_id=? AND profile=?', (root, profile)).fetchone()
            previous = db.execute('SELECT * FROM flow_task_execution_rows WHERE root_id=? ORDER BY ordinal DESC LIMIT 1', (root,)).fetchone()
            if ([r['id'] for r in candidates] != [project] or used or not previous or previous['kind'] not in RETRY_KINDS
                or previous['status'] != 'failed' or previous['ordinal']+1 != expected_ordinal
                or json.loads(previous['params']).get('count') != 1 or params.get('count') != 1
                or expected_ordinal > MAX_GENERATION_RETRIES):
                return False
            now = time.time()
            db.execute("""INSERT INTO flow_retry_executions
                (root_id,ordinal,reason,profile,project,kind,params,status,created,request_key)
                VALUES(?,?,'switch_account',?,?,?,?,'queued',?,?)""",
                (root,expected_ordinal,profile,project,previous['kind'],json.dumps(params,ensure_ascii=False),now,
                 f'retry-{root}-{expected_ordinal}'))
            db.execute("UPDATE flow_retry_roots SET state='active',updated=? WHERE root_id=?", (now,root))
            return True

    def enqueue_same_account_image_retry(self, root, expected_attempt, project, *, expected_ordinal=0):
        """Queue one operator-approved attempt after the caller verifies no output."""
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute("""SELECT e.*,r.state FROM flow_task_execution_rows e
                LEFT JOIN flow_retry_roots r ON r.root_id=e.root_id
                WHERE e.root_id=? ORDER BY e.ordinal DESC LIMIT 1""", (root,)).fetchone()
            repeated = db.execute("SELECT project,retry_reason AS reason FROM flow_task_execution_rows WHERE root_id=? AND ordinal=?",
                                  (root,expected_ordinal+1)).fetchone()
            if repeated and repeated['reason']=='same_account' and repeated['project']==project:
                return root
            if not previous or previous['id'] != expected_attempt or previous['ordinal'] != expected_ordinal:
                raise ValueError('原任务的最近尝试已经变化。')
            if (not previous or previous['kind'] != 'image' or previous['status'] != 'failed'
                    or not previous['finished'] or previous['state'] not in (None,'no_account','manual_failed')):
                raise ValueError('原任务没有可手动重试的已确认失败。')
            ordinal = (previous['ordinal'] or 0) + 1
            params = json.loads(previous['params'])
            if (ordinal > MAX_GENERATION_RETRIES or params.get('count') != 1
                    or ((params.get('reference_ids') or params.get('reference_image_ids'))
                        and project != previous['project'])):
                raise ValueError('原参考图必须复用所属项目，且每次只重试一张。')
            profile = previous['profile']
            if db.execute('SELECT 1 FROM flow_removed_accounts WHERE profile=?', (profile,)).fetchone():
                raise ValueError('原账号已从 Flow 号池移除。')
            projects = db.execute("""SELECT p.id FROM projects p JOIN accounts a ON a.profile=p.profile
                WHERE p.profile=? AND a.state='verified' AND instr(a.email,'@')>1
                AND p.title='FLOW+' || substr(a.email,1,instr(a.email,'@')-1)""", (profile,)).fetchall()
            if ([p['id'] for p in projects] != [project]
                    or db.execute("SELECT 1 FROM flow_task_execution_rows WHERE profile=? AND status IN ('queued','running')", (profile,)).fetchone()):
                raise ValueError('原账号没有唯一任务项目，或已有任务。')
            if previous['retry_reason'] is None:
                db.execute("INSERT INTO flow_retry_manual_roots VALUES(?)", (root,))
                db.execute("INSERT INTO flow_retry_roots VALUES(?,'active',?)", (root,time.time()))
                db.execute("INSERT INTO flow_retry_attempts VALUES(?,?,0,'original')", (root,root))
            now = time.time()
            db.execute("""INSERT INTO flow_retry_executions
                (root_id,ordinal,reason,profile,project,kind,params,status,created,request_key)
                VALUES(?,?,'same_account',?,?,'image',?,'queued',?,?)""",
                (root,ordinal,profile,project,previous['params'],now,f'manual-retry-{root}-{ordinal}'))
            db.execute("UPDATE flow_retry_roots SET state='active',updated=? WHERE root_id=?", (now,root))
            return root

    def cancel(self, ident):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            changed = db.execute("UPDATE tasks SET status='cancelled',finished=? WHERE id=? AND status='queued'", (now, ident)).rowcount
            changed += db.execute("UPDATE flow_retry_executions SET status='cancelled',finished=? WHERE root_id=? AND status='queued'", (now,ident)).rowcount
            if changed:
                db.execute("UPDATE flow_retry_roots SET state='cancelled',updated=? WHERE root_id IN (SELECT root_id FROM flow_retry_attempts WHERE task_id=?)", (now, ident))
            return bool(changed)

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def issue_ticket(self):
        token = secrets.token_urlsafe(32)
        self.execute("DELETE FROM tickets WHERE expires<?", (time.time(),))
        self.execute("INSERT INTO tickets VALUES(?,?)", (self.digest(token), time.time() + 300))
        return token

    def exchange(self, ticket):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "DELETE FROM tickets WHERE hash=? AND expires>? RETURNING hash",
                (self.digest(ticket), time.time()),
            ).fetchone()
            if not row:
                return None
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES(?,?)", (self.digest(token), time.time() + 7 * 86400)
            )
            return token

    def authenticated(self, token):
        return bool(
            token
            and self.rows(
                "SELECT hash FROM sessions WHERE hash=? AND expires>?",
                (self.digest(token), time.time()),
            )
        )
