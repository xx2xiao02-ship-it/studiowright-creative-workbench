from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from test_official_runtime_token import EMAIL, KEY, make_runtime
from live_pool import Conflict, RealPool
from server import make_handler


class GeminiPoolRemovalTest(unittest.TestCase):
    def test_confirmed_removal_rejects_busy_then_preserves_history_outside_the_pool(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime, _, cli, credential = make_runtime(root)
            other = 'b' * 64
            other_email = 'other@example.com'
            other_root = root / 'accounts' / 'b'
            other_cli = other_root / 'home/.gemini/antigravity-cli'
            other_cli.mkdir(parents=True)
            (other_cli / 'settings.json').write_bytes((cli / 'settings.json').read_bytes())
            (other_cli / 'antigravity-oauth-token').write_text(json.dumps({'id_token_email': other_email}))
            (other_root / 'workspace').mkdir()
            config = runtime.settings()
            config['accounts'][other] = {'root': str(other_root), 'email': other_email}
            runtime.config.write_text(json.dumps(config))
            original_config = runtime.config.read_bytes()
            items = [{'email': email, 'key': 'flow-account-' + key, 'flow_login': 'verified'}
                     for key, email in ((KEY, EMAIL), (other, other_email))]
            pool = RealPool(root, root, browser_inventory=lambda: None)
            try:
                official = pool.official
                official.save_account_order([KEY, other])

                def snapshot():
                    accounts = {'available': True, 'items': [dict(item) for item in items]}
                    pool.annotate(accounts)
                    return {'real_accounts': accounts}

                handler = make_handler(SimpleNamespace(snapshot=snapshot), real_pool=pool, coordinator=object())
                with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        base = f'http://127.0.0.1:{server.server_port}'
                        opener = build_opener(ProxyHandler({}))

                        def call(path, payload=None):
                            request = Request(base + path)
                            if payload is not None:
                                request = Request(base + path, method='POST', data=json.dumps(payload).encode(),
                                                  headers={'Origin': base, 'X-Workbench-Request': '1',
                                                           'Content-Type': 'application/json'})
                            try:
                                with opener.open(request, timeout=5) as response:
                                    return response.status, json.load(response)
                            except HTTPError as response:
                                with response:
                                    return response.code, json.load(response)

                        endpoint = '/api/gemini/accounts/remove'
                        body = {'account': KEY, 'confirmed': True}
                        self.assertEqual(call(endpoint, body | {'confirmed': False})[0], 400)
                        rid = str(uuid4())
                        saved, _ = official.generate(KEY, 'original message', rid, items)
                        self.assertEqual(call(endpoint, body)[0], 409)
                        self.assertEqual(pool.removed_gemini_accounts(), set())
                        job = official.claim()
                        official.finish(job, {'status': 'completed', 'submitted': True, 'response': 'saved reply',
                                              'native': str(uuid4())})

                        official.reauth.activation.update(state='waiting', email=EMAIL)
                        self.assertEqual(call(endpoint, body)[0], 409)
                        official.reauth.activation.update(state='idle', email=None)
                        self.assertEqual(call(endpoint, body)[0], 200)
                        self.assertEqual(call(endpoint, body)[0], 200)
                        self.assertEqual(official.runtime.inventory(), {other: True})
                        self.assertEqual(set(call('/api/official-quota')[1]['accounts']), {other})
                        channel = call('/api/channel-status')[1]
                        self.assertEqual([row['account_key'] for row in channel['accounts']], [other])
                        self.assertEqual(channel['account_order'], [other])
                        self.assertTrue(snapshot()['real_accounts']['items'][0]['gemini_removed'])
                        self.assertEqual(snapshot()['real_accounts']['items'][0]['flow_login'], 'verified')
                        self.assertEqual(pool.conversation(saved['conversation_id'])['turns'][0]['response'], 'saved reply')
                        self.assertEqual((cli / 'antigravity-oauth-token').read_text(), credential)
                        self.assertEqual(runtime.config.read_bytes(), original_config)
                        with self.assertRaises(Conflict):
                            pool.set_gemini_enabled(KEY, True, items)
                        with self.assertRaises(Conflict):
                            official.generate(KEY, 'new question', str(uuid4()), items)
                        sent, _ = official.generate('auto', 'another question', str(uuid4()), items)
                        self.assertEqual(sent['account_key'], other)
                    finally:
                        server.shutdown()
                        thread.join(timeout=3)
            finally:
                pool.close()
            restarted = RealPool(root, root, browser_inventory=lambda: None)
            try:
                self.assertEqual(restarted.removed_gemini_accounts(), {KEY})
                self.assertEqual(restarted.official.runtime.inventory(), {other: True})
            finally:
                restarted.close()
