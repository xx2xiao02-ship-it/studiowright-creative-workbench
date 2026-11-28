import sys
import unittest
from pathlib import Path
from uuid import uuid4

SNAPSHOT = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot'
sys.path[:0] = [str(SNAPSHOT / 'flow/console'), str(SNAPSHOT / 'workbench')]

from native_video_review import BASE, MODEL, checked_outcome
from unified_api import allowed_route


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
        good = {'status': 'completed', 'provider_model': MODEL + '-medium', 'input_media': media,
                'response_id': 'remote/response-123', 'account_ref': 'remote/account-1',
                'review': {'audio': 'heard'}}
        self.assertEqual(checked_outcome(good, media)[0], 'completed')
        for changes in ({'provider_model': 'gemini-4-hydrogen-high'}, {'provider_model': ''},
                        {'input_media': {**media, 'audio_present': False}},
                        {'input_media': {**media, 'sha256': 'a' * 64}},
                        {'account_ref': ''}, {'response_id': ''}, {'review': None}):
            with self.subTest(changes=changes):
                self.assertEqual(checked_outcome({**good, **changes}, media)[0], 'failed')
        self.assertEqual(checked_outcome({**good, 'status': 'unknown'}, media)[0], 'unknown')
        self.assertEqual(checked_outcome({}, media)[0], 'unknown')
