"""Verify that a manually queued retry uses the original task in receipts and downloads."""
import gc
import io
import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import Mock, patch

from starlette.testclient import TestClient
from test_flow_regressions import fixture_directory


SNAPSHOT = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot'
sys.path[:0] = [str(SNAPSHOT / name) for name in ('flow/console', 'workbench', 'flow/source/src')]
from unified_api import allowed_route
from studiowright.pacing import CoolingDown, Governor, PaceSettings, RiskPaused
from studiowright.store import Store as FlowStore

spec = spec_from_file_location('flow_retry_identity_console', SNAPSHOT / 'flow/console/server.py')
console = module_from_spec(spec)
spec.loader.exec_module(console)

spec = spec_from_file_location('flow_retry_identity_workbench', SNAPSHOT / 'workbench/server.py')
workbench = module_from_spec(spec)
spec.loader.exec_module(workbench)


class FixtureAdapter:
    def discover(self):
        return ['fixture']

    def validate(self, kind, params):
        pass


class RetryIdentity(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(fixture_directory())
        self.addCleanup(gc.collect)
        self.project = str(uuid.uuid4())
        with patch.object(console, 'Vault'):
            app = console.create_app(root, FixtureAdapter(), test=True)
        self.store = app.state.console.store
        self.console = app.state.console
        self.store.update_account('fixture', 'fixture@example.test', 'verified')
        self.store.project('fixture', self.project, 'FLOW+fixture')
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.key = app.state.console.keys.create('retry fixture', 'generate')['key']
        self.read_key = app.state.console.keys.create('read fixture', 'read')['key']
        self.output = root / 'output' / 'fixture.jpg'
        self.output.parent.mkdir(exist_ok=True)
        self.output.write_bytes(b'fixture image bytes')

    def headers(self, key=None):
        return {'Authorization': 'Bearer ' + (key or self.key), 'Origin': 'http://testserver', 'X-Console-Request': '1'}

    def submit(self, request_key, **extra):
        return self.client.post('/api/tasks', headers=self.headers(), json={
            'profile': 'fixture', 'project': self.project, 'kind': 'image',
            'request_key': request_key, 'confirm_generation': True,
            'params': {'model': 'nano-banana-2.1', 'prompt': 'fixture', 'aspect': '16:9', 'count': 1}, **extra,
        })

    def test_flow_pool_removal_preserves_history_and_refuses_active_or_new_generation(self):
        path = '/api/accounts/flow-remove'
        body = {'profile':'fixture', 'confirmed':True}
        headers = {'Origin':'http://testserver', 'X-Console-Request':'1'}
        self.assertEqual(self.client.post(path, headers=self.headers(), json=body).status_code, 403)
        submitted = self.submit('before-remove')
        self.assertEqual(submitted.status_code, 202)
        ident = submitted.json()['id']
        with patch.object(self.store, 'authenticated', return_value=True):
            self.assertEqual(self.client.post(path, headers=headers, json={**body,'confirmed':False}).status_code, 400)
            self.assertEqual(self.client.post(path, headers=headers, json=body).status_code, 409)
            self.assertEqual(self.store.claim(set())['id'], ident)
            self.assertEqual(self.client.post(path, headers=headers, json=body).status_code, 409)
            self.store.finish(ident, result={'files':[]})
            self.assertTrue(self.client.post(path, headers=headers, json=body).json()['removed'])
            self.assertTrue(self.client.post(path, headers=headers, json=body).json()['removed'])
        self.assertEqual(self.store.account('fixture')['email'], 'fixture@example.test')
        self.assertEqual(self.store.accounts()[0]['flow_removed'], 1)
        self.assertTrue(self.store.has_project('fixture', self.project))
        self.assertEqual(self.store.task(ident)['status'], 'succeeded')
        self.assertTrue(self.output.is_file())
        self.assertEqual(self.submit('before-remove').json()['id'], ident)
        blocked = self.submit('after-remove')
        self.assertEqual(blocked.status_code, 400)
        self.assertIn('已从 Flow 号池移除', blocked.json()['error'])

    def test_operator_pause_stops_dispatch_without_clearing_risk_or_cooldown(self):
        from account_pacing import account_status
        from filelock import FileLock
        from time import time

        database = self.console.root / 'data/studiowright.db'
        store = FlowStore(database)
        headers = {'Origin':'http://testserver', 'X-Console-Request':'1'}
        route = '/api/accounts/operator-pause'
        body = {'profile':'fixture', 'paused':True}
        self.assertEqual(self.client.post(route, headers=self.headers(), json=body).status_code, 403)
        with patch.object(self.store, 'authenticated', return_value=True):
            lock_path = database.parent / 'locks' / 'fixture.lock'
            lock_path.parent.mkdir()
            with FileLock(str(lock_path)):
                self.assertEqual(self.client.post(route, headers=headers, json=body).status_code, 409)
            for _ in range(2):
                response = self.client.post(route, headers=headers, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()['paused'])
            status = account_status(self.store.accounts(), database)[0]
            self.assertTrue(status['risk_paused'])
            self.assertTrue(status['operator_paused'])
            self.assertFalse(status['risk_review_required'])
            governor = Governor(FlowStore(database), 'fixture', PaceSettings())
            with self.assertRaises(RiskPaused):
                governor.reserve()
            now = time()
            with store.transaction() as db:
                db.execute("INSERT INTO flow_risk(profile,paused,consecutive) VALUES('fixture',1,3)")
                db.execute("INSERT INTO pacing(profile,cooldown_until) VALUES('fixture',?)", (now + 600,))
            resumed = self.client.post(route, headers=headers, json={**body,'paused':False})
            self.assertEqual(resumed.status_code, 200)
            status = account_status(self.store.accounts(), database, now)[0]
            self.assertFalse(status['operator_paused'])
            self.assertTrue(status['risk_review_required'])
            self.assertEqual(status['risk_consecutive'], 3)
            with self.assertRaises(RiskPaused):
                governor.reserve(now)
            self.assertTrue(self.client.post('/api/accounts/risk-resume', headers=headers,
                                              json={'profile':'fixture'}).json()['ok'])
            with self.assertRaises(CoolingDown):
                governor.reserve(now)
        with store.connect() as db:
            actions = [row['action'] for row in db.execute('SELECT action FROM flow_risk_events ORDER BY id')]
        self.assertEqual(actions, ['operator_pause','operator_resume','manual_resume'])

    def fail_original(self, *, evidence='confirmed_before_submit'):
        response = self.submit('fixture-original')
        self.assertEqual(response.status_code, 202, response.text)
        ident = response.json()['id']
        self.assertEqual(self.store.claim(set())['id'], ident)
        self.store.finish(ident, error='prompt_input_timeout', retry_evidence=evidence)
        return ident

    def retry(self, ident, *, ordinal=0, confirm=True, key=None):
        return self.client.post('/api/tasks/' + ident + '/retry', headers=self.headers(key),
                                json={'expected_ordinal': ordinal, 'confirm_generation': confirm})

    def assert_original_download(self, ident):
        response = self.client.get('/api/tasks/' + ident, headers=self.headers(self.read_key))
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()
        self.assertEqual((task['task_id'], task['status']), (ident, 'succeeded'))
        self.assertEqual([a['ordinal'] for a in task['retry']['attempts']], [0, 1])
        url = '/api/tasks/' + ident + '/files/0'
        self.assertEqual(task['downloads'][0]['path'], url)
        downloaded = self.client.get(url, headers=self.headers(self.read_key))
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, self.output.read_bytes())

    def test_retry_and_retransmission_keep_root_id(self):
        ident = self.fail_original()
        self.assertEqual(self.submit('fixture-wrong-route', retry_of=ident).status_code, 400)
        self.assertEqual(self.retry(ident, confirm=False).status_code, 400)
        self.assertEqual(self.retry(ident, key=self.read_key).status_code, 403)
        for _ in range(2):
            response = self.retry(ident)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json(), {'task_id': ident, 'status': 'queued'})
        attempt = self.store.claim(set())
        self.assertEqual((attempt['id'], attempt['attempt_ordinal']), (ident, 1))
        self.assertEqual(attempt['params'], self.store.task(ident)['params'])
        self.store.finish(ident, {'files': [str(self.output)]}, attempt_ordinal=1)
        self.assertEqual(self.retry(ident).json(), {'task_id': ident, 'status': 'succeeded'})
        self.assertEqual([r['id'] for r in self.store.tasks()], [ident])
        self.assertIsNone(self.store.claim(set()))
        self.assert_original_download(ident)

    def test_unconfirmed_failure_does_not_queue_another_attempt(self):
        ident = self.fail_original(evidence=None)
        self.assertEqual(self.retry(ident).status_code, 409)
        self.assertIsNone(self.store.claim(set()))

    def test_workbench_retry_forwards_only_the_original_task_and_attempt(self):
        ident = self.fail_original()
        snapshot = {'tasks': [self.client.get('/api/tasks/' + ident, headers=self.headers()).json()]}
        public = self.store.task(ident)
        public['retry'] = snapshot['tasks'][0]['retry']
        self.assertEqual(workbench.task_rows({'tasks': [public]}, [])[0]['manual_retry_ordinal'], 0)

        def forward(request, timeout):
            self.assertEqual(request.full_url, f'http://127.0.0.1:18888/api/tasks/{ident}/retry')
            self.assertEqual(json.loads(request.data), {'expected_ordinal': 0, 'confirm_generation': True})
            response = self.retry(ident)
            stream = io.BytesIO(response.content)
            stream.status = response.status_code
            return stream

        sources = workbench.Sources()
        sources.opener = Mock(open=forward)
        for _ in range(2):
            self.assertEqual(sources.flow_retry(ident, 0), (202, {'task_id': ident, 'status': 'queued'}))
        self.assertEqual([item['id'] for item in self.store.tasks()], [ident])
        self.assertEqual(self.store.claim(set())['id'], ident)
        self.assertIsNone(self.store.claim(set()))

    def test_explicit_single_attempt_never_enters_the_switch_queue(self):
        for evidence in ('transient_before_submit', 'provider_rejected_unusual', None):
            with self.subTest(evidence=evidence):
                key = 'single-attempt-' + str(uuid.uuid4())
                response = self.submit(key, auto_retry=False)
                self.assertEqual(response.status_code, 202, response.text)
                ident = response.json()['id']
                self.assertIs(response.json()['retry']['auto_retry'], False)
                self.assertEqual(self.submit(key, auto_retry=False).json()['id'], ident)
                self.assertEqual(self.submit(key, auto_retry=True).status_code, 400)
                self.assertEqual(self.store.claim(set())['id'], ident)
                self.store.finish(ident, error='fixture failure', retry_evidence=evidence)
                job = self.client.get('/api/tasks/' + ident, headers=self.headers()).json()
                self.assertIn(job['status'], ('failed', 'interrupted'))
                self.assertIs(job['retry']['auto_retry'], False)
                self.assertEqual([attempt['ordinal'] for attempt in job['retry']['attempts']], [0])
                self.assertEqual(self.store.pending_retry_roots(), [])
                self.assertIsNone(self.store.claim(set()))

    def test_omitted_retry_setting_preserves_the_default(self):
        ident = self.fail_original(evidence='provider_rejected_unusual')
        self.assertIs(self.store.retry_detail(ident)['auto_retry'], True)
        self.assertEqual([item['root_id'] for item in self.store.pending_retry_roots()], [ident])

    def test_older_separate_attempt_resolves_to_its_linked_root(self):
        ident = self.fail_original()
        alias = self.submit('fixture-older-attempt').json()['id']
        self.assertEqual(self.store.claim(set())['id'], alias)
        self.store.finish(alias, {'files': [str(self.output)]})
        self.store.execute("UPDATE flow_retry_attempts SET root_id=?,ordinal=1,reason='same_account' WHERE task_id=?",
                           (ident, alias))
        self.store.execute("UPDATE flow_retry_roots SET state='succeeded' WHERE root_id=?", (ident,))
        self.store.execute('DELETE FROM flow_retry_roots WHERE root_id=?', (alias,))
        self.assertEqual(self.submit('fixture-older-attempt').json()['id'], ident)
        self.assertEqual(self.retry(alias).json(), {'task_id': ident, 'status': 'succeeded'})
        legacy = self.client.get('/api/tasks/' + alias, headers=self.headers(self.read_key)).json()
        self.assertEqual((legacy['task_id'], legacy['requested_task_id']), (ident, alias))
        self.assert_original_download(ident)

    def test_gateway_only_exposes_the_retry_route_for_posts(self):
        self.assertEqual(allowed_route('POST', '/api/tasks/root-id/retry'), 'flow')
        self.assertIsNone(allowed_route('GET', '/api/tasks/root-id/retry'))
        self.assertIsNone(allowed_route('POST', '/api/tasks/root-id/retry?other=1'))


if __name__ == '__main__':
    unittest.main()
