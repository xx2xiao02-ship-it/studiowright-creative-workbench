"""Authenticated native MP4 receipts. Enable dispatch only with a verified adapter."""
from contextlib import closing
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from uuid import uuid4


BASE = '/api/native-video'
CAPABILITIES = BASE + '/capabilities'
REVIEW = re.compile(BASE + r'/reviews/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(/media)?')
MODEL = 'gemini-3.8-flash'
PROVIDER_MODEL = re.compile(r'gemini-3\.8-flash(?:-(?:low|medium|high))?')
SHA256 = re.compile(r'[0-9a-f]{64}')
MAX_VIDEO = 100 * 1024 * 1024
MAX_JSON = 256 * 1024
MAX_STAGED_BYTES = 10 * MAX_VIDEO
UNAVAILABLE = 'native_video_provider_unverified'


def matches(method, path):
    if path == CAPABILITIES:
        return method == 'GET'
    match = REVIEW.fullmatch(path)
    return bool(match and (method == 'POST' or method == 'GET' and not match[2]))


class RequestError(ValueError):
    def __init__(self, status, code):
        self.status, self.code = status, code


@lru_cache(maxsize=1)
def ffmpeg_binary():
    if binary := shutil.which('ffmpeg'):
        return binary
    python = Path.home()/'.local/share/studiowright-evaluation/venv/bin/python'
    try:
        binary = subprocess.check_output([str(python), '-I', '-B', '-c',
            'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'],
            text=True, timeout=8).strip()
    except (OSError, subprocess.SubprocessError):
        raise RequestError(503, 'media_probe_unavailable') from None
    if not Path(binary).is_absolute() or not Path(binary).is_file():
        raise RequestError(503, 'media_probe_unavailable')
    return binary


def check_tracks(path):
    with path.open('rb') as source:
        if source.read(8)[4:] != b'ftyp':
            raise RequestError(422, 'mp4_required')
    try:
        probe = subprocess.run([ffmpeg_binary(), '-nostdin', '-v', 'error', '-xerror',
            '-protocol_whitelist', 'file', '-f', 'mov', '-enable_drefs', '0', '-i', str(path),
            '-map', '0:V:0', '-map', '0:a:0', '-t', '0.25', '-f', 'null', '-'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.SubprocessError):
        raise RequestError(503, 'media_probe_unavailable') from None
    if probe.returncode:
        raise RequestError(422, 'readable_video_and_audio_required')


class NativeVideoReviews:
    def __init__(self, folder, adapter=None):
        self.folder = Path(folder)
        self.adapter = adapter
        self.instance = uuid4().hex
        self.upload_slots = threading.BoundedSemaphore(2)
        self.review_slot = threading.BoundedSemaphore(1)

    def connect(self):
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.folder/'reviews.sqlite3', timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('''CREATE TABLE IF NOT EXISTS native_reviews (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                status TEXT NOT NULL, prompt_sha256 TEXT, worker TEXT, outcome TEXT)''')
        except BaseException:
            db.close()
            raise
        return db

    @staticmethod
    def row(db, request_id, owner):
        row = db.execute('SELECT * FROM native_reviews WHERE id=?', (request_id,)).fetchone()
        if row is not None and row['owner'] != owner:
            raise RequestError(404, 'review_not_found')
        return row

    def receipt(self, row):
        status = row['status']
        outcome = json.loads(row['outcome']) if row['outcome'] else {}
        if status == 'running' and row['worker'] != self.instance:
            status, outcome = 'unknown', {'error': 'worker_interrupted'}
        return {'request_id': row['id'], 'status': status, 'requested_model': MODEL,
                'actual_model': None, 'media': {'sha256': row['sha256'], 'bytes': row['size'],
                'mime_type': 'video/mp4', 'audio_present': True}, **outcome}

    def get(self, request_id, owner):
        with closing(self.connect()) as db:
            row = self.row(db, request_id, owner)
            if row is None:
                raise RequestError(404, 'review_not_found')
            return self.receipt(row)

    def upload(self, request_id, owner, stream, size, sha256):
        if not self.upload_slots.acquire(blocking=False):
            raise RequestError(429, 'uploads_busy')
        scratch = None
        try:
            with closing(self.connect()) as db:
                row = self.row(db, request_id, owner)
                if row and (row['sha256'], row['size']) != (sha256, size):
                    raise RequestError(409, 'file_fingerprint_conflict')
                with tempfile.NamedTemporaryFile(dir=self.folder, suffix='.mp4', delete=False) as temp:
                    scratch = Path(temp.name)
                    remaining = size
                    digest = hashlib.sha256()
                    while remaining:
                        chunk = stream.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise RequestError(400, 'upload_incomplete')
                        temp.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                if digest.hexdigest() != sha256:
                    raise RequestError(422, 'file_fingerprint_mismatch')
                if row is None:
                    check_tracks(scratch)
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    row = self.row(db, request_id, owner)
                    if row and (row['sha256'], row['size']) != (sha256, size):
                        raise RequestError(409, 'file_fingerprint_conflict')
                    if row is None:
                        staged = db.execute("SELECT COALESCE(SUM(size), 0) FROM native_reviews "
                                            "WHERE status IN ('uploaded', 'running')").fetchone()[0]
                        if staged + size > MAX_STAGED_BYTES:
                            raise RequestError(429, 'media_staging_full')
                        scratch.replace(self.folder/f'{request_id}.mp4')
                        db.execute('INSERT INTO native_reviews (id,owner,sha256,size,status) VALUES (?,?,?,?,?)',
                                   (request_id, owner, sha256, size, 'uploaded'))
                    return self.receipt(self.row(db, request_id, owner))
        finally:
            self.upload_slots.release()
            if scratch is not None:
                scratch.unlink(missing_ok=True)

    def submit(self, request_id, owner, payload):
        if (not isinstance(payload, dict) or set(payload) != {'model', 'sha256', 'prompt'}
                or payload['model'] != MODEL or not isinstance(payload['prompt'], str)
                or not payload['prompt'].strip() or not isinstance(payload['sha256'], str)
                or not SHA256.fullmatch(payload['sha256'])):
            raise RequestError(422, 'invalid_review_input_or_model')
        prompt = payload['prompt']
        try:
            prompt_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        except UnicodeError:
            raise RequestError(422, 'invalid_review_prompt') from None
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = self.row(db, request_id, owner)
            if row is None:
                raise RequestError(404, 'review_not_found')
            if row['sha256'] != payload['sha256'] or row['prompt_sha256'] not in (None, prompt_hash):
                raise RequestError(409, 'review_input_conflict')
            if row['status'] != 'uploaded':
                return self.receipt(row)
            if not self.review_slot.acquire(blocking=False):
                raise RequestError(429, 'reviews_busy')
            try:
                db.execute("UPDATE native_reviews SET status='running', prompt_sha256=?, worker=? WHERE id=?",
                           (prompt_hash, self.instance, request_id))
                receipt = self.receipt(self.row(db, request_id, owner))
                db.commit()
                threading.Thread(target=self.run, args=(request_id, receipt['media'], prompt), daemon=True).start()
            except Exception:
                self.review_slot.release()
                db.rollback()
                db.execute("UPDATE native_reviews SET status='unknown', outcome=? "
                           "WHERE id=? AND worker=? AND status='running'",
                           (json.dumps({'error': 'worker_interrupted'}), request_id, self.instance))
                db.commit()
                raise
        return receipt

    def run(self, request_id, media, prompt):
        path = self.folder/f'{request_id}.mp4'
        try:
            try:
                with path.open('rb') as source:
                    actual = hashlib.file_digest(source, 'sha256').hexdigest()
                if actual != media['sha256'] or path.stat().st_size != media['bytes']:
                    status, outcome = 'failed', {'error': 'stored_file_fingerprint_mismatch'}
                else:
                    result = self.adapter(request_id, path, MODEL, prompt)
                    status, outcome = checked_outcome(result, media)
            except Exception:
                status, outcome = 'unknown', {'error': 'provider_result_unconfirmed'}
            with closing(self.connect()) as db, db:
                db.execute('UPDATE native_reviews SET status=?, outcome=? WHERE id=?',
                           (status, json.dumps(outcome, ensure_ascii=True), request_id))
            path.unlink(missing_ok=True)
        finally:
            self.review_slot.release()

    def handle(self, handler, owner):
        if handler.path == CAPABILITIES:
            return 200, {'protocol': 'native-mp4-review-v1', 'available': self.adapter is not None,
                         'requested_model': MODEL, 'max_bytes': MAX_VIDEO,
                         'reason': UNAVAILABLE if self.adapter is None else None}
        match = REVIEW.fullmatch(handler.path)
        request_id = match[1]
        if handler.command == 'GET':
            return 200, self.get(request_id, owner)
        if self.adapter is None:
            raise RequestError(503, UNAVAILABLE)
        upload = bool(match[2])
        length = request_length(handler, 'video/mp4' if upload else 'application/json',
                                MAX_VIDEO if upload else MAX_JSON)
        if upload:
            values = handler.headers.get_all('X-Content-SHA256', [])
            if len(values) != 1 or not SHA256.fullmatch(values[0]):
                raise RequestError(400, 'file_fingerprint_required')
            return 200, self.upload(request_id, owner, handler.rfile, length, values[0])
        try:
            raw = handler.rfile.read(length)
            if len(raw) != length:
                raise RequestError(400, 'request_incomplete')
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise RequestError(400, 'invalid_json') from None
        receipt = self.submit(request_id, owner, payload)
        return (202 if receipt['status'] == 'running' else 200), receipt


def request_length(handler, mime_type, maximum):
    headers = handler.headers
    lengths = headers.get_all('Content-Length', [])
    if (headers.get_all('Transfer-Encoding') or len(lengths) != 1
            or not re.fullmatch(r'[1-9][0-9]{0,9}', lengths[0])
            or headers.get_all('Content-Type', []) != [mime_type]):
        raise RequestError(400, 'invalid_content_headers')
    length = int(lengths[0])
    if length > maximum:
        raise RequestError(413, 'content_too_large')
    return length


def checked_outcome(result, media):
    if not isinstance(result, dict) or result.get('status') not in ('completed', 'failed', 'unknown'):
        return 'unknown', {'error': 'provider_result_unconfirmed'}
    status = result['status']
    provider_model = result.get('provider_model')
    info = {'provider_model': provider_model} if isinstance(provider_model, str) and re.fullmatch(
        r'[A-Za-z0-9_./-]{1,120}', provider_model) else {}
    if status != 'completed':
        return status, {**info, 'error': 'provider_' + status}
    if not PROVIDER_MODEL.fullmatch(info.get('provider_model', '')):
        return 'failed', {**info, 'error': 'actual_model_unverified_or_mismatched'}
    input_media = result.get('input_media')
    if (input_media != media or input_media.get('audio_present') is not True
            or type(input_media.get('bytes')) is not int or any(not isinstance(result.get(field), str)
            or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', result[field])
            for field in ('response_id', 'account_ref')) or not isinstance(result.get('review'), dict) or not result['review']):
        return 'failed', {**info, 'error': 'provider_evidence_incomplete'}
    outcome = {**info, 'actual_model': MODEL, **{field: result[field]
        for field in ('input_media', 'response_id', 'account_ref', 'review')}}
    try:
        if len(json.dumps(outcome, allow_nan=False).encode()) <= MAX_JSON:
            return 'completed', outcome
    except (ValueError, TypeError, RecursionError):
        pass
    return 'failed', {**info, 'error': 'provider_evidence_invalid_or_oversized'}
