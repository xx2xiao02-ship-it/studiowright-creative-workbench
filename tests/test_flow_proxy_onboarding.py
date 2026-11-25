import asyncio
from concurrent.futures import ThreadPoolExecutor
import gc
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys
from threading import Barrier
import time
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from test_flow_regressions import fixture_directory

SNAPSHOT = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot'
sys.path[:0] = [str(SNAPSHOT / name) for name in ('flow/console', 'workbench', 'flow/source/src')]
from adapter import FakeAdapter
from flow_proxy_guard import mapped_ports, reserve_confirmed_exit

spec = spec_from_file_location('flow_proxy_onboarding_console', SNAPSHOT / 'flow/console/server.py')
server = module_from_spec(spec)
spec.loader.exec_module(server)


@unittest.skipUnless(sys.platform.startswith('linux'), 'uses the Ubuntu file lock')
class WindowsProxyOnboarding(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(fixture_directory())
        self.addCleanup(gc.collect)
        (self.root / 'bin').mkdir()
        self.mapping = self.root / 'bin/exit-map.txt'
        self.original = 'existing-profile 3101\n'
        self.mapping.write_text(self.original, encoding='ascii')
        self.mapping.chmod(0o600)
        # Synthetic inventory only. These tests never open a proxy connection.
        self.exits = {
            '3101': {'public_ip': '11.22.33.1', 'confirmed': True},
            '10801': {'public_ip': '::ffff:11.22.33.1', 'confirmed': True},
            '3112': {'public_ip': '11.22.33.12', 'confirmed': False},
        }
        self.inventory = self.root / 'bin/exit-inventory.json'
        self.write_inventory()
        self.adapter = FakeAdapter(self.root)
        app = server.create_app(self.root, self.adapter, test=True)
        self.console = app.state.console
        self.store = self.console.store
        token = 'test-only-bridge'
        self.store.execute('INSERT INTO bridge_devices VALUES(?,?)', (self.store.digest(token), 0))
        self.headers = {
            'Authorization': 'Bearer ' + token,
            'Origin': 'chrome-extension://kbnibljeinmjmmpholicjkaiehmilbbk',
            'X-Console-Request': '1',
        }
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def write_inventory(self):
        self.inventory.write_text(json.dumps(self.exits), encoding='ascii')
        self.inventory.chmod(0o600)

    def add_spare(self):
        self.exits['3111'] = {'public_ip': '11.22.33.11', 'confirmed': True}
        self.write_inventory()

    def sync(self, email):
        response = self.client.post('/api/plugin/session', headers=self.headers, json={
            'email': email, 'cookies': [
                {'name': 'fixture', 'value': 'fake', 'domain': 'flow.google.com', 'path': '/',
                 'secure': True, 'hostOnly': True},
            ],
        })
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def test_current_and_legacy_plugins_can_pair_but_unknown_origins_cannot_import(self):
        with TestClient(self.client.app, client=('127.0.0.1', 32100)) as client:
            probe = client.get('/health', headers={'X-Studiowright-Probe': '1',
                                                    'Authorization': 'Bearer sw-readonly-probe'})
        self.assertEqual(probe.json().get('bridge_id'), server.BRIDGE_ID)
        self.assertIs(probe.json().get('bridge_check'), True)
        self.assertNotIn('set-cookie', probe.headers)
        session, _ = server.WebAuth(self.store).new_session(False)
        self.client.cookies.set('sw_console_fixture', session)
        pair_headers = {'X-Console-Request': '1'}
        legacy = self.client.post('/api/bridge/pair', headers=pair_headers, json={})
        self.assertEqual(legacy.json().get('extensionId'), server.BRIDGE_LEGACY_ID)
        current = self.client.post('/api/bridge/pair', headers=pair_headers, json={'extensionId': server.BRIDGE_ID})
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json().get('extensionId'), server.BRIDGE_ID)
        bad_pair = self.client.post('/api/bridge/pair', headers=pair_headers, json={'extensionId': 'unknown'})
        self.assertEqual(bad_pair.status_code, 400)
        body = {'email': 'bridge-fixture@example.test',
                'cookies': [{'name': 'fixture', 'value': 'fake', 'domain': 'flow.google.com',
                             'path': '/', 'secure': True, 'hostOnly': True}]}
        headers = {'Authorization': 'Bearer ' + current.json()['connectionToken'], **pair_headers}
        for origin, expected in [('chrome-extension://' + 'a'*32, 403),
                                 ('chrome-extension://' + server.BRIDGE_ID, 202)]:
            response = self.client.post('/api/plugin/session', headers={**headers, 'Origin': origin}, json=body)
            self.assertEqual(response.status_code, expected, response.text)

    def claim(self):
        return self.store.claim(set(), session_import_allowed=self.console.ready_session_imports(set(), None))

    def test_flow_entry_only_serves_the_redirect_and_browser_job(self):
        entry = self.client.get('/')
        self.assertEqual(entry.status_code, 200)
        self.assertIn('http://127.0.0.1:18920/', entry.text)
        self.assertIn('/static/chrome-job.js', entry.text)
        self.assertNotIn('legacyWorkspace', entry.text)
        self.assertEqual(self.client.get('/static/chrome-job.js').status_code, 200)
        for old in ('app.js', 'account-insights.js', 'library-tools.js'):
            self.assertEqual(self.client.get('/static/' + old).status_code, 404)
        self.assertEqual(self.client.get('/static/api.html').status_code, 200)

    def test_remote_login_landing_waits_for_same_origin_confirmation(self):
        profile = 'fixture-flow-profile'
        self.store.add_account(profile, 'Fixture Flow account')
        session, _ = server.WebAuth(self.store).new_session(False)
        self.client.cookies.set('sw_console_fixture', session)
        route = '/api/accounts/' + profile + '/remote'
        with patch.object(self.console.remote, 'start', return_value={'profile': profile}) as start:
            landing = self.client.get('/remote')
            self.assertEqual(landing.status_code, 200)
            self.assertIn('id="startLogin" hidden', landing.text)
            start.assert_not_called()
            foreign = self.client.post(route, json={}, headers={
                'Origin': 'https://example.test', 'X-Console-Request': '1',
            })
            self.assertEqual(foreign.status_code, 403)
            start.assert_not_called()
            confirmed = self.client.post(route, json={}, headers={
                'Origin': 'http://testserver', 'X-Console-Request': '1',
            })
            self.assertEqual(confirmed.status_code, 200)
            start.assert_called_once_with(profile)

    def test_same_cached_import_waits_then_verifies_with_a_unique_exit(self):
        email = 'new-account@example.test'
        item = self.sync(email)
        duplicate = self.sync(email)
        self.assertEqual(duplicate['id'], item['id'])
        self.assertTrue(duplicate['reused'])
        status = self.client.get('/api/plugin/session/' + item['id'], headers=self.headers).json()
        self.assertEqual(status['message'], server.WAITING_FOR_PROXY)
        self.assertIsNone(self.claim())
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.mapping.read_text(encoding='ascii'), self.original)

        self.add_spare()
        task = self.claim()
        self.assertEqual(task['id'], item['id'])
        asyncio.run(self.console.execute(task))
        self.assertEqual(self.store.task(item['id'])['status'], 'succeeded')
        self.assertEqual(self.store.account(item['profile'])['state'], 'verified')
        self.assertEqual(self.mapping.read_text(encoding='ascii'), self.original + f"{item['profile']} 3111\n")

    def test_expired_login_cannot_consume_spare_exit(self):
        item = self.sync('expired@example.test')
        bundle_id = self.store.task(item['id'])['params']['bundle_id']
        vault = self.console.vault
        payload = json.dumps(vault.get(bundle_id)).encode()
        vault.path(bundle_id).write_bytes(vault.cipher.encrypt_at_time(payload, int(time.time()) - 1900))
        self.add_spare()

        self.assertIsNone(self.claim())
        self.assertEqual(self.store.task(item['id'])['status'], 'failed')
        self.assertFalse(vault.path(bundle_id).exists())
        self.assertEqual(self.mapping.read_text(encoding='ascii'), self.original)

    def test_claim_serializes_imports_without_blocking_other_task_types(self):
        for profile, email in [('first', 'first@example.test'), ('second', 'second@example.test')]:
            self.store.add_account(profile, profile)
            self.store.update_account(profile, email, 'verified')
        first = self.sync('first@example.test')
        second = self.sync('second@example.test')
        active = self.claim()
        self.assertEqual(active['id'], first['id'])
        self.assertIsNone(self.claim())
        other = self.store.create_task('other', None, 'doctor', {}, 'fixture-doctor')
        self.assertEqual(self.claim()['id'], other['id'])
        asyncio.run(self.console.execute(active))
        self.assertEqual(self.claim()['id'], second['id'])

    def test_concurrent_profiles_cannot_share_the_last_exit(self):
        self.add_spare()
        barrier = Barrier(2)
        profiles = ('account-0123456789ab', 'account-b9876543210a')

        def reserve(profile):
            barrier.wait(timeout=5)
            return reserve_confirmed_exit(self.root, profile)

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(reserve, profiles))
        self.assertCountEqual(results, [None, 3111])
        winner = profiles[results.index(3111)]
        self.assertEqual(mapped_ports(self.mapping), {'existing-profile': 3101, winner: 3111})
        self.assertEqual(reserve_confirmed_exit(self.root, winner), 3111)

    def test_unverified_inventory_keeps_existing_binding_and_blocks_new_claims(self):
        email = 'existing@example.test'
        profile = 'account-' + self.store.digest(email)[:12]
        self.store.add_account(profile, profile)
        self.store.update_account(profile, email, 'verified')
        self.mapping.write_text(self.original + f'{profile} 3102\n', encoding='ascii')
        self.add_spare()
        item = self.sync(email)
        self.assertEqual(self.claim()['id'], item['id'])
        self.sync('new@example.test')
        self.assertIsNone(self.claim())
        self.assertEqual(mapped_ports(self.mapping), {'existing-profile': 3101, profile: 3102})
