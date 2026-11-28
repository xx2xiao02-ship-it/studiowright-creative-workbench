import base64
from contextlib import contextmanager
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

SNAPSHOT = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot'
sys.path[:0] = [str(SNAPSHOT / 'flow/console'), str(SNAPSHOT / 'workbench')]

from native_video_review import BASE, MODEL, VERTEX_TRANSPORT, NativeVideoReviews, checked_outcome, ffmpeg_binary
from native_video_vertex import HOST, VertexVideoReview, configured_adapter
from unified_api import allowed_route, handler_for
from unified_keys import UnifiedKeys


@contextmanager
def running(server):
    with server:
        worker = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        worker.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            worker.join()


class NativeVideoContract(unittest.TestCase):
    def test_native_routes_do_not_replace_chat_or_image_routes(self):
        review = BASE + '/reviews/' + str(uuid4())
        for method, path in [('GET', BASE + '/capabilities'), ('GET', review),
                             ('POST', review), ('POST', review + '/media')]:
            self.assertEqual(allowed_route(method, path), 'native-video')
            self.assertIsNone(allowed_route(method, path + '?extra=1'))
        self.assertIsNone(allowed_route('GET', review + '/media'))
        self.assertIsNone(allowed_route('POST', BASE + '/reviews/not-a-uuid/media'))
        for path in ('/api/real-pool/chat', '/api/real-pool/image-analysis'):
            self.assertEqual(allowed_route('POST', path), 'workbench')

    def test_completion_requires_provider_receipts(self):
        media = {'sha256': 'f' * 64, 'bytes': 200, 'mime_type': 'video/mp4', 'audio_present': True}
        good = {'status': 'completed', 'provider_model': MODEL, 'input_media': media,
                'response_id': 'remote/response-123', 'account_ref': 'google-adc/sha256-' + 'e' * 64,
                'provider_evidence': {'transport': VERTEX_TRANSPORT, 'http_status': 200, 'response_sha256': 'a' * 64},
                'review': {'audio': 'heard'}}
        self.assertEqual(checked_outcome(good, media)[0], 'completed')
        for changes in ({'provider_model': 'gemini-4-hydrogen-high'}, {'provider_model': MODEL + '-medium'},
                        {'input_media': {**media, 'audio_present': False}},
                        {'input_media': {**media, 'sha256': 'a' * 64}},
                        {'account_ref': ''}, {'response_id': ''}, {'review': None}, {'provider_evidence': None}):
            with self.subTest(changes=changes):
                self.assertEqual(checked_outcome({**good, **changes}, media)[0], 'failed')
        self.assertEqual(checked_outcome({**good, 'status': 'unknown'}, media)[0], 'unknown')
        self.assertEqual(checked_outcome({}, media)[0], 'unknown')

    def test_default_adapter_is_disabled(self):
        with patch.dict('os.environ', {'SW_NATIVE_VERTEX_ENABLED': '', 'SW_NATIVE_VERTEX_PROJECT': 'test-project'}):
            self.assertIsNone(configured_adapter())

    def test_real_gateway_preserves_inline_mp4_and_original_receipt(self):
        incoming = []
        reply = json.dumps({'modelVersion': MODEL, 'responseId': 'controlled-123', 'candidates': [
            {'finishReason': 'STOP', 'content': {'parts': [{'text': 'ignored', 'thought': True},
             {'text': '{"audio":"controlled result"}'}]}}]}).encode()

        class ControlledVertex(BaseHTTPRequestHandler):
            def do_POST(self):
                incoming.append((self.path, self.headers['Authorization'],
                                 json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200)
                self.send_header('Content-Length', str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *_args):
                pass

        class ControlledCredentials:
            service_account_email = 'isolated-test@example.invalid'

            def before_request(self, _auth_request, _method, _url, headers):
                headers['authorization'] = 'Bearer controlled-upstream-only'

        with tempfile.TemporaryDirectory() as temp, running(HTTPServer(('127.0.0.1', 0), ControlledVertex)) as provider_port:
            root = Path(temp)
            film = root/'input.mp4'
            subprocess.run([ffmpeg_binary(), '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=s=320x240:r=30', '-f', 'lavfi', '-i', 'sine=f=440',
                            '-t', '4', '-c:v', 'mpeg4', '-q:v', '2', '-c:a', 'aac', str(film)], check=True)
            original = film.read_bytes()
            digest = hashlib.sha256(original).hexdigest()
            keys = UnifiedKeys(root/'keys.sqlite3')
            key = keys.create('隔离测试')['key']
            other_key = keys.create('隔离测试其他调用方')['key']
            reviews = NativeVideoReviews(root/'reviews', VertexVideoReview('test-project', ControlledCredentials(), None))
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(keys, native_reviews=reviews))
            with running(server) as gateway_port, patch('native_video_vertex.http.client.HTTPSConnection') as https:
                https.side_effect = lambda _host, timeout: http.client.HTTPConnection('127.0.0.1', provider_port, timeout=timeout)

                def call(method, path, body=None, headers=None, token=key):
                    connection = http.client.HTTPConnection('127.0.0.1', gateway_port, timeout=10)
                    try:
                        connection.request(method, path, body=body, headers={'Authorization': 'Bearer ' + token, **(headers or {})})
                        with connection.getresponse() as response:
                            return response.status, json.load(response)
                    finally:
                        connection.close()

                operation = BASE + '/reviews/' + str(uuid4())
                upload = {'Content-Type': 'video/mp4', 'X-Content-SHA256': digest}
                self.assertEqual(call('GET', BASE + '/capabilities', token='invalid')[0], 401)
                self.assertTrue(call('GET', BASE + '/capabilities')[1]['available'])
                self.assertEqual(call('POST', operation + '/media', original, upload)[1]['status'], 'uploaded')
                payload = json.dumps({'model': MODEL, 'sha256': digest, 'prompt': '识别此视频和声音。'})
                self.assertEqual(call('POST', operation, payload, {'Content-Type': 'application/json'})[0], 202)
                self.assertEqual(call('GET', operation, token=other_key)[0], 404)
                self.assertTrue(reviews.review_slot.acquire(timeout=10))
                reviews.review_slot.release()
                status, completed = call('GET', operation)
                self.assertEqual(status, 200)
                self.assertEqual(completed['status'], 'completed')
                self.assertEqual(completed['input_media']['sha256'], digest)
                self.assertEqual(completed['provider_evidence']['response_sha256'], hashlib.sha256(reply).hexdigest())
                self.assertEqual(call('POST', operation, payload, {'Content-Type': 'application/json'}), (200, completed))

            https.assert_called_once_with(HOST, timeout=180)
            self.assertEqual(len(incoming), 1)
            endpoint, auth, request = incoming[0]
            self.assertEqual(endpoint, '/v1/projects/test-project/locations/global/publishers/google/models/' + MODEL + ':generateContent')
            self.assertEqual(auth, 'Bearer controlled-upstream-only')
            media, instruction = request['contents'][0]['parts']
            self.assertEqual(set(media), {'inlineData'})
            self.assertEqual(media['inlineData']['mimeType'], 'video/mp4')
            self.assertEqual(base64.b64decode(media['inlineData']['data'], validate=True), original)
            self.assertEqual(instruction, {'text': '识别此视频和声音。'})
            self.assertFalse((root/'reviews'/f'{completed["request_id"]}.mp4').exists())
