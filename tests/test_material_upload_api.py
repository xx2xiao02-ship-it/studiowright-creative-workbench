import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from PIL import Image
from starlette.testclient import TestClient

SNAPSHOT = Path(__file__).resolve().parents[1] / "packaging/ubuntu/snapshot"
sys.path[:0] = [str(SNAPSHOT / 'flow/console'), str(SNAPSHOT / 'workbench')]
from unified_api import LIBRARY_IMPORT_LIMIT, handler_for
from unified_keys import UnifiedKeys

spec = spec_from_file_location('material_upload_console', SNAPSHOT / 'flow/console/server.py')
console = module_from_spec(spec)
spec.loader.exec_module(console)


@contextmanager
def serve(handler):
    with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join(timeout=5)


class MaterialUploadAPI(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        app = console.create_app(self.root / 'console', SimpleNamespace(discover=lambda: []), test=True)
        store = app.state.console.store
        self.library = app.state.console.library
        self.client = client = TestClient(app)
        self.addCleanup(client.close)
        client.cookies.set('sw_console_fixture', store.exchange(store.issue_ticket()))

        class FlowBridge(BaseHTTPRequestHandler):
            def forward(self):
                # The Flow console normally runs on a fixed loopback port.
                # Adapt the ephemeral fixture port and keep its origin check.
                incoming_host = f'127.0.0.1:{self.server.server_port}'
                if self.headers.get('Host') != incoming_host:
                    self.send_error(403)
                    return
                headers = {k: v for k, v in self.headers.items() if k.lower() != 'host'}
                if headers.get('Origin') == 'http://' + incoming_host:
                    headers['Origin'] = 'http://testserver'
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                response = client.request(self.command, self.path, headers=headers, content=body)
                self.send_response(response.status_code)
                for name in ('Content-Type', 'Content-Length', 'Content-Range'):
                    if name in response.headers:
                        self.send_header(name, response.headers[name])
                self.end_headers()
                self.wfile.write(response.content)

            do_GET = do_POST = forward

            def log_message(self, *_args):
                pass

        flow_port = self.enterContext(serve(FlowBridge))
        keys = UnifiedKeys(self.root / 'keys.sqlite3')
        self.auth = {'Authorization': 'Bearer ' + keys.create('material fixture')['key']}
        api_port = self.enterContext(serve(handler_for(keys, upstream_port=flow_port, flow_port=flow_port)))
        self.address = f'http://127.0.0.1:{api_port}'
        self.http = build_opener(ProxyHandler({}))
        image = io.BytesIO()
        Image.new('RGB', (12, 8), 'green').save(image, format='PNG')
        self.image = image.getvalue()

    def upload(self, name, content=None, *, expected=None, auth=True, length=None):
        content = self.image if content is None else content
        query = {'filename': name}
        if expected is not None:
            query['sha256'] = expected
        headers = {**(self.auth if auth else {}), 'Content-Type': 'application/octet-stream'}
        if length is not None:
            headers['Content-Length'] = str(length)
        url = self.address + '/api/library/import?' + urlencode(query)
        with self.http.open(Request(url, data=content, headers=headers), timeout=8) as response:
            return response.status, json.load(response)

    def test_concurrent_imports_reuse_one_id_and_original_names_are_searchable(self):
        expected = hashlib.sha256(self.image).hexdigest()
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = [workers.submit(self.upload, name, expected=expected.upper())
                       for name in ('片头.png', '片头副本.PNG')]
            results = dict(task.result() for task in pending)
        self.assertEqual(set(results), {200, 201})
        self.assertTrue(results[200]['deduplicated'])
        self.assertFalse(results[201]['deduplicated'])
        self.assertEqual(results[200]['asset_id'], expected)
        self.assertEqual(results[201]['asset_id'], expected)
        detail = Request(self.address + '/api/library/' + expected, headers=self.auth)
        with self.http.open(detail, timeout=8) as response:
            item = json.load(response)
        self.assertEqual({origin['details']['original_name'] for origin in item['origins']},
                         {'片头.png', '片头副本.PNG'})
        self.assertEqual(self.library.search('片头副本.PNG')['total'], 1)
        self.assertEqual(len(list((self.library.root / 'objects').rglob('*.png'))), 1)
        original = Request(self.address + results[200]['path'], headers=self.auth)
        with self.http.open(original, timeout=8) as response:
            self.assertEqual(response.read(), self.image)

    def test_import_rejects_unauthorized_invalid_or_oversized_content(self):
        checks = [
            (401, 'unauthorized.png', {'auth': False}),
            (400, '../outside.png', {}),
            (400, 'corrupt.png', {'content': b'not a PNG'}),
            (400, 'wrong-sha.png', {'expected': '0' * 64}),
            (400, 'wrong-media.wav', {}),
            (413, 'oversized.png', {'length': LIBRARY_IMPORT_LIMIT + 1}),
        ]
        for status, name, options in checks:
            with self.subTest(name=name), self.assertRaises(HTTPError) as exc:
                self.upload(name, **options)
            self.assertEqual(exc.exception.code, status)
        self.assertEqual(self.library.search()['total'], 0)
        self.assertFalse(list((self.library.root / 'staging').iterdir()))

    def test_upload_repairs_a_missing_existing_audio_file_at_its_original_location(self):
        wave_buffer = io.BytesIO()
        with wave.open(wave_buffer, 'wb') as audio:
            audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0\0' * 800)
        data = wave_buffer.getvalue()
        source = self.root / 'music.wav'
        source.write_bytes(data)
        asset = self.library.archive(source, {
            'source': 'windows_local', 'source_id': 'music-fixture', 'media_type': 'audio',
            'details': {'original_name': source.name, 'audio_category': 'music'},
        })
        previous_path = self.library.file(asset)
        previous_path.unlink()
        status, response = self.upload('恢复版.wav', data)
        self.assertEqual((status, response['asset_id'], response['media_type']), (200, asset, 'audio'))
        self.assertTrue(response['deduplicated'])
        self.assertEqual(self.library.file(asset), previous_path)
        self.assertEqual(previous_path.read_bytes(), data)
