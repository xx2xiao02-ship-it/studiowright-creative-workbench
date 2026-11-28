"""Send the complete, staged MP4 to Google's Vertex GenerateContent endpoint."""
import base64
from contextlib import closing
from functools import partial
import hashlib
import http.client
import json
import os
import re

from native_video_review import MAX_JSON, MODEL, VERTEX_TRANSPORT


HOST = 'aiplatform.googleapis.com'
PROJECT = re.compile(r'[a-z][a-z0-9-]{4,28}[a-z0-9]')
CHUNK_BYTES = 192 * 1024  # A multiple of three for a single base64 part.
CLOUD_SCOPE = 'https://www.googleapis.com/auth/cloud-platform'


def configured_adapter():
    """Keep dispatch off unless the operator opts in with a server-side identity."""
    project = os.environ.get('SW_NATIVE_VERTEX_PROJECT', '')
    if os.environ.get('SW_NATIVE_VERTEX_ENABLED') != '1' or not PROJECT.fullmatch(project):
        return None
    try:
        from google.auth import default
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request
    except ImportError:
        return None
    try:
        credentials, _ = default(scopes=[CLOUD_SCOPE], quota_project_id=project)
        return VertexVideoReview(project, credentials, partial(Request(), timeout=15))
    except (GoogleAuthError, OSError, ValueError):
        return None


class VertexVideoReview:
    def __init__(self, project, credentials, auth_request):
        self.credentials = credentials
        self.auth_request = auth_request
        self.endpoint = f'/v1/projects/{project}/locations/global/publishers/google/models/{MODEL}:generateContent'

    def __call__(self, _request_id, path, model, prompt):
        if model != MODEL:
            return {'status': 'failed', 'error': 'provider_model_not_supported'}
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        try:
            self.credentials.before_request(self.auth_request, 'POST', 'https://' + HOST + self.endpoint, headers)
        except Exception:
            return {'status': 'failed', 'error': 'provider_credentials_unavailable'}
        identity = (getattr(self.credentials, 'service_account_email', None)
                    or getattr(self.credentials, 'account', None))
        if not headers.get('authorization') or not isinstance(identity, str) or '@' not in identity:
            return {'status': 'failed', 'error': 'provider_identity_unverified'}
        account_ref = 'google-adc/sha256-' + hashlib.sha256(identity.encode('utf-8')).hexdigest()

        # One video part. No temporary copy, media URL, sampling options or transcript.
        prefix = b'{"contents":[{"role":"USER","parts":[{"inlineData":{"mimeType":"video/mp4","data":"'
        suffix = (b'"}},{"text":' + json.dumps(prompt, ensure_ascii=True).encode('ascii')
                  + b'}]}],"generationConfig":{"responseMimeType":"application/json"}}')
        digest = hashlib.sha256()
        size = path.stat().st_size
        headers['Content-Length'] = str(len(prefix) + 4 * ((size + 2) // 3) + len(suffix))
        sent = 0

        def body(source):
            nonlocal sent
            yield prefix
            while sent < size:
                chunk = source.read(min(CHUNK_BYTES, size - sent))
                if not chunk:
                    raise OSError('staged_video_truncated')
                sent += len(chunk)
                digest.update(chunk)
                yield base64.b64encode(chunk)
            if source.read(1):
                raise OSError('staged_video_grew')
            yield suffix

        try:
            with path.open('rb') as source, closing(http.client.HTTPSConnection(HOST, timeout=180)) as connection:
                connection.request('POST', self.endpoint, body=body(source), headers=headers)
                with connection.getresponse() as response:
                    if response.status != 200:
                        definite = response.status in (400, 401, 403, 404, 413, 422, 429)
                        return {'status': 'failed' if definite else 'unknown', 'http_status': response.status}
                    raw = response.read(MAX_JSON + 1)
        except (OSError, http.client.HTTPException):
            return {'status': 'unknown', 'error': 'provider_connection_unconfirmed'}

        try:
            result = json.loads(raw) if len(raw) <= MAX_JSON else {}
            provider_model = result['modelVersion']
            response_id = result['responseId']
            candidate, *_ = result['candidates']
            parts = candidate['content']['parts']
            if candidate.get('finishReason') != 'STOP':
                return {'status': 'failed', 'provider_model': provider_model, 'error': 'provider_review_incomplete'}
            review = json.loads(''.join(part['text'] for part in parts if 'text' in part and not part.get('thought')))
        except (KeyError, ValueError, TypeError, AttributeError, RecursionError):
            return {'status': 'failed', 'error': 'provider_response_incomplete'}

        # The media fingerprint is computed on the raw bytes fed to the HTTP encoder.
        # Google attests the model and response ID, and may report modality token counts.
        usage = result.get('usageMetadata') or {}
        modalities = {}
        if isinstance(usage, dict) and isinstance(usage.get('promptTokensDetails'), list):
            for detail in usage['promptTokensDetails']:
                if isinstance(detail, dict) and detail.get('modality') in ('VIDEO', 'AUDIO', 'IMAGE', 'TEXT'):
                    count = detail.get('tokenCount')
                    if type(count) is int and count > 0:
                        modalities[detail['modality']] = count
        return {
            'status': 'completed', 'provider_model': provider_model, 'response_id': response_id,
            'input_media': {'sha256': digest.hexdigest(), 'bytes': sent, 'mime_type': 'video/mp4', 'audio_present': True},
            'provider_evidence': {'transport': VERTEX_TRANSPORT, 'http_status': 200,
                                  'response_sha256': hashlib.sha256(raw).hexdigest(),
                                  'prompt_modalities': modalities},
            'account_ref': account_ref, 'review': review,
        }
