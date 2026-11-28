"""Authenticated loopback-only API for the existing creative workbench."""
import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from unified_keys import UnifiedKeys
from flow_references import MAX_BYTES as FLOW_REFERENCE_LIMIT, UPLOAD_ROUTE as FLOW_REFERENCE_UPLOAD
from native_video_review import NativeVideoReviews, RequestError, matches as native_video_route
from native_video_vertex import configured_adapter


LIBRARY_IMPORT_PATH = '/api/library/import'
LIBRARY_IMPORT_LIMIT = 1024**3


def allowed_route(method, target):
    parts = urlsplit(target)
    path = parts.path
    if parts.scheme or parts.netloc or parts.fragment or not path.startswith('/') or path.startswith('//'):
        return None
    if parts.query and not path.startswith('/api/library'):
        return None
    if len(target) > 3500:
        return None
    if native_video_route(method, path):
        return 'native-video'
    if method == 'GET':
        if re.fullmatch(r'/api/music/jobs/[0-9a-f-]{36}', path):
            return 'workbench'
        if path in ('/api/health', '/api/snapshot', '/api/conversations'):
            return 'workbench'
        if re.fullmatch(r'/api/(?:conversations|real-pool/requests|real-pool/flow-requests)/[0-9a-f-]{36}', path):
            return 'workbench'
        if re.fullmatch(r'/api/gemini-images/[0-9a-f-]{36}/[0-3]', path):
            return 'workbench'
        if re.fullmatch(r'/api/library(?:/facets|/[0-9a-f]{64}(?:/(?:file|thumbnail))?)?', path):
            return 'workbench'
        if re.fullmatch(r'/api/tasks/[A-Za-z0-9_-]{1,128}(?:/files/[0-9]{1,4})?', path):
            return 'flow'
    if method == 'POST' and path == LIBRARY_IMPORT_PATH:
        return 'flow'
    if method == 'POST' and not parts.query:
        if re.fullmatch(r'/api/tasks/[A-Za-z0-9_-]{1,128}/retry', path):
            return 'flow'
        if path in ('/api/music/jobs', '/api/conversations', '/api/real-pool/chat', '/api/real-pool/image-analysis',
                    '/api/real-pool/image-analysis-validate', '/api/real-pool/flow', '/api/real-pool/flow-validate'):
            return 'workbench'
        if FLOW_REFERENCE_UPLOAD.fullmatch(path):
            return 'workbench'
        if re.fullmatch(r'/api/real-pool/uploads/[0-9a-f-]{36}/[0-3]/(?:jpg|jpeg|png|webp|mp4|mov|webm|pdf)', path):
            return 'workbench'
    return None


def handler_for(store, upstream_port=18920, flow_port=18888, *, native_reviews=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'StudioWrightAPI/1'

        def log_message(self, *_args):
            pass  # Never log keys, prompts, URLs, or request data.

        def error_json(self, status, message):
            self.send_json(status, {'error': message})

        def send_json(self, status, data):
            body = json.dumps(data, ensure_ascii=True).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def run_request(self):
            if (len(self.headers.get_all('Host', [])) != 1 or self.headers.get('Host', '').lower() not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}') or self.headers.get('Origin')):
                return self.error_json(403, '请从可信本机程序调用。')
            if self.command == 'GET' and self.path == '/health':
                data = json.dumps({'application': 'studiowright-unified-api', 'ok': True,
                                   'source_version': os.getenv('SW_SOURCE_VERSION'),
                                   'source_commit': os.getenv('SW_SOURCE_COMMIT')}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)
                return
            header = self.headers.get('Authorization', '')
            try:
                valid = len(self.headers.get_all('Authorization', [])) == 1 and header.startswith('Bearer ') and store.allows(header[7:])
            except (OSError, ValueError, sqlite3.Error):
                return self.error_json(503, '密钥服务暂不可用。')
            if not valid:
                return self.error_json(401, '密钥无效或已撤销。')
            route = allowed_route(self.command, self.path)
            if not route:
                return self.error_json(404, '接口不存在。')
            if route == 'native-video':
                self.close_connection = True
                if native_reviews is None:
                    return self.error_json(404, '接口不存在。')
                self.connection.settimeout(90)
                try:
                    owner = hashlib.sha256(header[7:].encode('ascii')).hexdigest()
                    status, data = native_reviews.handle(self, owner)
                except RequestError as exc:
                    status, data = exc.status, {'error': exc.code}
                except (OSError, sqlite3.Error):
                    status, data = 503, {'error': 'review_state_unavailable'}
                try:
                    self.send_json(status, data)
                except OSError:
                    pass  # The original receipt remains queryable after a dropped connection.
                return
            library_import = self.command == 'POST' and urlsplit(self.path).path == LIBRARY_IMPORT_PATH
            flow_reference = FLOW_REFERENCE_UPLOAD.fullmatch(self.path) is not None
            is_upload = library_import or flow_reference or self.path.startswith('/api/real-pool/uploads/')
            size = 0
            if self.command == 'POST':
                length = self.headers.get('Content-Length', '')
                maximum = LIBRARY_IMPORT_LIMIT if library_import else (
                    FLOW_REFERENCE_LIMIT if flow_reference else (100 * 1024 * 1024 if is_upload else 100000))
                ctype = 'application/octet-stream' if is_upload else 'application/json'
                if (self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1
                        or not re.fullmatch(r'[0-9]{1,10}', length) or int(length) == 0
                        or self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != ctype):
                    return self.error_json(400, '请求内容格式或大小不正确。')
                size = int(length)
                if size > maximum:
                    return self.error_json(413 if library_import else 400, '上传内容超出大小限制。')
                if library_import:
                    self.connection.settimeout(190)
            target_port = upstream_port if route == 'workbench' else flow_port
            headers = {'Host': f'127.0.0.1:{target_port}', 'Accept': self.headers.get('Accept', '*/*')}
            if self.command == 'POST':
                request_header = 'X-Console-Request' if route == 'flow' else 'X-Workbench-Request'
                headers.update({'Content-Type': ctype, 'Content-Length': str(size),
                                'Origin': f'http://127.0.0.1:{target_port}', request_header: '1'})
                request_id = self.headers.get('Idempotency-Key')
                if request_id:
                    headers['Idempotency-Key'] = request_id
            requested_range = self.headers.get('Range')
            if requested_range:
                if not re.fullmatch(r'bytes=\d*-\d*', requested_range):
                    return self.error_json(400, '文件范围不正确。')
                headers['Range'] = requested_range
            connection = http.client.HTTPConnection('127.0.0.1', target_port, timeout=190)
            started = False
            try:
                connection.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                for name, value in headers.items():
                    connection.putheader(name, value)
                connection.endheaders()
                while size:
                    chunk = self.rfile.read(min(size, 256 * 1024))
                    if not chunk:
                        raise OSError('incomplete request')
                    connection.send(chunk)
                    size -= len(chunk)
                response = connection.getresponse()
                length = response.getheader('Content-Length', '')
                if not length.isdigit() or int(length) > 1024**3:
                    return self.error_json(502, '服务返回的数据无法读取。')
                self.send_response(response.status)
                for name in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges', 'Content-Disposition'):
                    value = response.getheader(name)
                    if value and '\r' not in value and '\n' not in value:
                        self.send_header(name, value)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                started = True
                remaining = int(length)
                while remaining:
                    chunk = response.read(min(remaining, 256 * 1024))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (OSError, http.client.HTTPException):
                if not started:
                    self.error_json(503, '连接工作台中断；如已提交，请先查询原请求记录。')
            finally:
                connection.close()

        do_GET = run_request
        do_POST = run_request
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18922)
    parser.add_argument('--upstream-port', type=int, default=18920)
    parser.add_argument('--flow-port', type=int, default=18888)
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.local/state/creative-unified-workbench')
    args = parser.parse_args()
    os.umask(0o077)
    native_reviews = NativeVideoReviews(args.state_dir/'native-video-reviews', configured_adapter())
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(
        UnifiedKeys(args.state_dir/'api-keys.sqlite3'), args.upstream_port, args.flow_port,
        native_reviews=native_reviews))
    server.serve_forever()


if __name__ == '__main__':
    main()
