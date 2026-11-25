import sys
import io
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'))
from gemini_authorization import GeminiAuthorization
from live_pool import RealPool
from official_reauth import PROBE_AUTH, close_authorization_tab
from official_pool import AUTH_UNSENT, OfficialPool, READINESS_PROMPT, READINESS_REPLY


class GeminiAuthorizationTest(unittest.TestCase):
    def test_business_auth_block_is_recovered_by_independent_probe_without_resending(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pool = RealPool(root, root, execute=lambda *_: None)
            try:
                key = 'a' * 64
                items = [{'key': 'flow-account-' + key}]
                official = pool.official
                official.runtime = SimpleNamespace(inventory=lambda: {key: True}, text_models=lambda: {})
                official.account_quotas = lambda _: {}
                original = str(uuid.uuid4())
                official.generate(key, 'original user message', original, items)
                job = official.claim()
                self.assertEqual(job['request_id'], original)
                official.finish(job, dict(status='failed', reason=AUTH_UNSENT, submitted=False))

                [first] = official.queue_readiness_probes(items, {}, blocked_only=True)
                job = official.claim(probe_only=True)
                self.assertEqual(job['prompt'], READINESS_PROMPT)
                official.finish(job, dict(status='failed', reason=PROBE_AUTH, submitted=False))
                request = first['request_id']
                self.assertTrue(any(item['request_id'] == request and item['mode'] == 'login_only'
                                    and item['can_start'] for item in official.reauth.dashboard()['items']))
                with pool.connection() as db:
                    self.assertEqual(official.reason(db, key, {key: True}), 'official_call_blocked')
                    finished = db.execute('SELECT finished FROM requests WHERE request_id=?', (request,)).fetchone()[0]
                    db.execute('INSERT INTO official_reauth VALUES(?,?,?,?,?,?,?,?)',
                               (request, key, 'completed', None, None, PROBE_AUTH, finished, time.time() + 300))

                [second] = official.queue_readiness_probes(items, {}, blocked_only=True)
                self.assertNotEqual(second['request_id'], request)
                job = official.claim(probe_only=True)
                self.assertEqual(job['prompt'], READINESS_PROMPT)
                official.finish(job, dict(status='completed', submitted=True, response=READINESS_REPLY,
                                          native=str(uuid.uuid4())))
                with pool.connection() as db:
                    self.assertIsNone(official.reason(db, key, {key: True}))
                    self.assertEqual(db.execute('SELECT status FROM requests WHERE request_id=?',
                                                (original,)).fetchone()[0], 'failed')
                    attempts = db.execute('SELECT submitted FROM official_attempts WHERE request_id=?',
                                          (original,)).fetchall()
                    self.assertEqual([row[0] for row in attempts], [0])
            finally:
                pool.close()

    def test_unknown_business_request_does_not_lock_account(self):
        pool = OfficialPool.__new__(OfficialPool)
        with sqlite3.connect(':memory:') as db:
            db.executescript("""
                CREATE TABLE requests(request_id TEXT,account_key TEXT,status TEXT,finished REAL);
                CREATE TABLE official_jobs(request_id TEXT,reason TEXT,readiness_probe INTEGER);
                CREATE TABLE official_account_blocks(account_key TEXT,reason TEXT,created REAL);
                CREATE TABLE account_unknown_release(request_id TEXT,released REAL,reason TEXT);
                CREATE TABLE account_preferences(account_key TEXT,gemini_enabled INTEGER);
                CREATE TABLE official_reauth(request_id TEXT,account_key TEXT,state TEXT);
                CREATE TABLE conversation_turns(request_id TEXT,conversation_id TEXT);
                CREATE TABLE conversations(id TEXT,native_json TEXT);
                INSERT INTO requests VALUES('old','a','unknown',1);
                INSERT INTO official_jobs VALUES('old','timeout_unconfirmed',0);
                INSERT INTO official_account_blocks VALUES('a','timeout_unconfirmed',1);
            """)
            inventory = {'a': True}
            self.assertIsNone(pool.reason(db, 'a', inventory))
            block = pool.readiness_block(db, 'a')
            self.assertIsNone(pool.reason(db, 'a', inventory, block, readiness_probe=True))
            self.assertEqual(db.execute("SELECT status FROM requests").fetchone()[0], 'unknown')
            for quota_reason in ('activation_verification_required', 'official_authorization_verification_required'):
                db.execute('UPDATE official_account_blocks SET reason=?', (quota_reason,))
                self.assertIsNone(pool.reason(db, 'a', inventory))
            db.execute("UPDATE official_account_blocks SET reason='verification_required'")
            self.assertEqual(pool.reason(db, 'a', inventory, pool.readiness_block(db, 'a'),
                                         readiness_probe=True), 'official_call_blocked')

    def test_old_timeout_is_archived_without_replaying_or_deleting(self):
        pool = OfficialPool.__new__(OfficialPool)
        with sqlite3.connect(':memory:', isolation_level=None) as db:
            db.executescript("""
                CREATE TABLE requests(request_id TEXT, account_key TEXT, status TEXT, finished REAL, note TEXT, response TEXT);
                CREATE TABLE official_jobs(request_id TEXT, reason TEXT, readiness_probe INTEGER);
                CREATE TABLE official_account_blocks(account_key TEXT PRIMARY KEY, reason TEXT, created REAL);
                CREATE TABLE account_unknown_release(request_id TEXT PRIMARY KEY, released REAL, reason TEXT);
                INSERT INTO requests VALUES('old','a','unknown',100,'timeout','partial');
                INSERT INTO requests VALUES('new','a','unknown',500,'timeout',NULL);
                INSERT INTO official_jobs VALUES('old','timeout_unconfirmed',0);
                INSERT INTO official_jobs VALUES('new','timeout_unconfirmed',0);
            """)
            pool.pool = SimpleNamespace(maintenance=lambda: False, connection=lambda: db)
            self.assertEqual(pool.archive_old_timeouts(now=2000), 1)
            self.assertEqual(pool.archive_old_timeouts(now=2000), 0)
            self.assertEqual(db.execute("SELECT status,response FROM requests WHERE request_id='old'").fetchone(), ('unknown','partial'))
            self.assertEqual(db.execute("SELECT request_id FROM account_unknown_release").fetchall(), [('old',)])
            self.assertIsNone(db.execute("SELECT reason FROM official_account_blocks").fetchone())

    def test_unknown_finish_blocks_only_original_conversation(self):
        pool = OfficialPool.__new__(OfficialPool)
        with sqlite3.connect(':memory:', isolation_level=None) as db:
            db.executescript("""
                CREATE TABLE requests(request_id TEXT,account_key TEXT,status TEXT,finished REAL,response TEXT,note TEXT);
                CREATE TABLE official_jobs(request_id TEXT,reason TEXT,readiness_probe INTEGER);
                CREATE TABLE official_attempts(request_id TEXT,status TEXT,reason TEXT,submitted INTEGER,finished REAL,total_ms REAL,response TEXT);
                CREATE TABLE conversations(id TEXT,blocked TEXT,updated REAL);
                CREATE TABLE official_account_blocks(account_key TEXT PRIMARY KEY,reason TEXT,created REAL);
                CREATE TABLE request_metrics(request_id TEXT PRIMARY KEY,first_text_ms REAL,total_ms REAL,token_count INTEGER);
                INSERT INTO requests VALUES('old','a','dispatching',NULL,NULL,NULL);
                INSERT INTO official_jobs VALUES('old',NULL,0);
                INSERT INTO conversations VALUES('conversation',NULL,0);
            """)
            pool.pool = SimpleNamespace(connection=lambda: db)
            pool.finish(dict(request_id='old',account_key='a',conversation_id='conversation'),
                        dict(status='unknown',reason='unexpected_tool_stopped',submitted=True))
            self.assertEqual(db.execute("SELECT status FROM requests").fetchone()[0], 'unknown')
            self.assertTrue(db.execute("SELECT blocked FROM conversations").fetchone()[0])
            self.assertEqual(db.execute("SELECT reason FROM official_jobs").fetchone()[0], 'unexpected_tool_stopped')
            self.assertEqual(db.execute("SELECT count(*) FROM official_account_blocks").fetchone()[0], 0)
            db.execute("UPDATE requests SET status='dispatching'")
            pool.finish(dict(request_id='old',account_key='a',conversation_id='conversation'),
                        dict(status='failed',reason='verification_required',submitted=False))
            self.assertEqual(db.execute("SELECT reason FROM official_account_blocks").fetchone()[0], 'verification_required')

    def test_authorization_schedules_targeted_quota_read(self):
        import threading
        from official_quota import QuotaReader
        reader = QuotaReader.__new__(QuotaReader)
        reader.lock = threading.Lock()
        reader.authorization_refreshes = set()
        seen = []
        started, release = threading.Event(), threading.Event()
        def check(key):
            seen.append(key)
            started.set()
            release.wait(2)
        reader.check = check
        key = 'a' * 64
        try:
            self.assertTrue(reader.refresh_after_authorization(key))
            self.assertTrue(started.wait(1))
            self.assertFalse(reader.refresh_after_authorization(key))
            self.assertEqual(seen, [key])
        finally:
            release.set()

    def make_activation(self):
        return GeminiAuthorization(SimpleNamespace(pool=None, runtime=None))

    def test_only_safe_accounts_and_login_only_probes_are_selected(self):
        activation = self.make_activation()
        accounts = {k: {'root': str(Path.cwd() / ('fixture-' + k)), 'email': k + '@example.test'}
                    for k in ('missing', 'expired', 'unknown', 'business')}
        activation.runtime = SimpleNamespace(settings=lambda: {'accounts': accounts, 'binary': '/fixture/cli'})
        activation.reauth.dashboard = lambda: {'items': [
            {'account_key': 'expired', 'request_id': 'probe', 'mode': 'login_only', 'can_start': True},
            {'account_key': 'business', 'request_id': 'message', 'can_start': True},
        ]}
        activation.allowed = lambda key, **_: key != 'unknown'
        with patch.object(Path, 'exists', lambda p: 'fixture-missing' not in str(p) and 'fixture-unknown' not in str(p)):
            self.assertEqual([(k, rid) for k, _, _, rid in activation.candidates()],
                             [('missing', None), ('expired', 'probe')])

    def test_next_account_starts_only_after_previous_save(self):
        activation = self.make_activation()
        activation.candidates = lambda: [(key, {'email': key + '@example.test'}, '/fixture/cli', None)
                                         for key in ('first', 'second')]
        activation.allowed = lambda _, **kwargs: True
        seen = []
        def bootstrap(key, _, __):
            seen.append((key, activation.status()['completed']))
        activation.bootstrap = bootstrap
        activation.run()
        self.assertEqual(seen, [('first', 0), ('second', 1)])
        self.assertEqual(activation.status()['state'], 'completed')

    def test_wrong_account_or_closed_window_stops_sequence(self):
        activation = self.make_activation()
        activation.candidates = lambda: [(key, {'email': key + '@example.test'}, '/fixture/cli', None)
                                         for key in ('first', 'second')]
        activation.allowed = lambda _, **kwargs: True
        seen = []
        def bootstrap(key, _, __):
            seen.append(key)
            raise ValueError('login not completed')
        activation.bootstrap = bootstrap
        activation.run()
        self.assertEqual(seen, ['first'])
        self.assertEqual(activation.status()['state'], 'stopped')

    def test_only_recorded_google_tab_is_closed(self):
        binding = {'browser_pid': 42, 'port': 9300, 'profile': 'a', 'process_start': '1', 'page_target': 'FLOW'}
        registry = SimpleNamespace(TaskWindowRegistry=lambda: SimpleNamespace(get=lambda _: binding),
                                   process_identity=lambda *_: ('a', '1'))
        pages = [{'type': 'page', 'id': 'FLOW', 'url': 'https://flow.google.com/project/x'},
                 {'type': 'page', 'id': 'LOGIN', 'url': 'https://antigravity.google/oauth-callback'},
                 {'type': 'page', 'id': 'OTHER', 'url': 'https://accounts.google.com/'}]
        calls = []
        def open_url(url, timeout):
            calls.append(url)
            return io.BytesIO(json.dumps(pages).encode() if url.endswith('/json/list') else b'')
        with patch.dict(sys.modules, {'task_window_registry': registry}), \
                patch('urllib.request.build_opener', return_value=SimpleNamespace(open=open_url)), \
                patch('official_reauth.private_bytes', return_value=b'LOGIN'):
            self.assertTrue(close_authorization_tab(Path.cwd(), 'account-key'))
            self.assertEqual(calls[-1], 'http://127.0.0.1:9300/json/close/LOGIN')
            pages[1]['url'] = 'https://example.test/user-page'
            self.assertFalse(close_authorization_tab(Path.cwd(), 'account-key'))
            self.assertTrue(calls[-1].endswith('/json/list'))
