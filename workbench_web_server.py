"""Loopback-only HTTP adapter for the local audiobook workbench."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from urllib.parse import parse_qs, unquote_to_bytes, urlsplit


MAX_BODY_BYTES = 256 * 1024
_MEDIA_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
_SHA256 = re.compile(r'^[0-9a-fA-F]{64}$')
_STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/workbench.css': ('workbench.css', 'text/css; charset=utf-8'),
    '/workbench.js': ('workbench.js', 'text/javascript; charset=utf-8'),
}


class WorkbenchConflict(Exception):
    """Raised by the service when an action conflicts with current state."""


MAX_BOOK_UPLOAD_BYTES = 64 * 1024 * 1024


def _response_headers(handler, content_type=None):
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('Content-Security-Policy',
                        "default-src 'self'; connect-src 'self'; media-src 'self'; "
                        "script-src 'self'; style-src 'self'; object-src 'none'; "
                        "base-uri 'none'; frame-ancestors 'none'")
    handler.send_header('Referrer-Policy', 'no-referrer')
    if content_type:
        handler.send_header('Content-Type', content_type)


def _hash_open_file(stream):
    digest = hashlib.sha256()
    while True:
        block = stream.read(1024 * 1024)
        if not block:
            break
        digest.update(block)
    return digest.hexdigest()


def _range_bounds(value, size):
    """Return inclusive (start, end), or None when no Range header is present."""
    if value is None:
        return None
    if not value.startswith('bytes=') or ',' in value:
        raise ValueError('只支持单段bytes范围。')
    spec = value[6:].strip()
    match = re.fullmatch(r'(\d*)-(\d*)', spec)
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError('Range格式无效。')
    left, right = match.groups()
    if left:
        start = int(left)
        end = int(right) if right else size - 1
        if start >= size or end < start:
            raise ValueError('Range超出媒体长度。')
        return start, min(end, size - 1)
    suffix = int(right)
    if suffix <= 0:
        raise ValueError('Range后缀长度无效。')
    return max(0, size - suffix), size - 1


def make_server(service, port=0, token=None, static_root=None):
    """Create a localhost-only server; caller owns its serve_forever lifecycle."""
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError('port必须是0至65535之间的整数。')
    session_token = token if token is not None else secrets.token_urlsafe(32)
    if not isinstance(session_token, str) or not session_token:
        raise ValueError('token必须是非空字符串。')
    root = Path(static_root) if static_root is not None else Path(__file__).resolve().parent / 'web'
    root = root.resolve()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'WorkbenchLocal/1'
        sys_version = ''

        def log_message(self, fmt, *args):
            return

        def handle(self):
            try:
                super().handle()
            except ConnectionError:
                # Browsers routinely close playback and refresh requests early.
                return

        def _check_request_origin(self):
            expected_host = f'127.0.0.1:{self.server.server_port}'
            hosts = self.headers.get_all('Host', [])
            if len(hosts) != 1 or hosts[0] != expected_host:
                self._json_error(400, 'invalid_host', 'Host必须精确匹配本机回环地址。')
                return False
            origins = self.headers.get_all('Origin', [])
            if len(origins) > 1 or (origins and origins[0] != f'http://{expected_host}'):
                self._json_error(403, 'invalid_origin', '跨源请求已拒绝。')
                return False
            return True

        def _authorized_header(self):
            values = self.headers.get_all('X-Workbench-Token', [])
            return self._authorized(values[0] if len(values) == 1 else None)

        def _authorized(self, supplied):
            if not isinstance(supplied, str) or not hmac.compare_digest(
                    supplied.encode('utf-8'), self.server.session_token.encode('utf-8')):
                self._json_error(401, 'unauthorized', '工作台会话令牌无效或缺失。')
                return False
            return True

        def _json_error(self, status, code, message):
            self._send_json(status, {'error': {'code': code, 'message': message}})

        def _send_json(self, status, value):
            body = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            self.send_response(status)
            _response_headers(self, 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path, content_type):
            try:
                data = path.read_bytes()
            except OSError:
                self._json_error(404, 'not_found', '静态资源不存在。')
                return
            self.send_response(200)
            _response_headers(self, content_type)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._check_request_origin():
                return
            parsed = urlsplit(self.path)
            if parsed.path == '/favicon.ico':
                self.send_response(204)
                _response_headers(self)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if parsed.path in _STATIC_FILES:
                name, content_type = _STATIC_FILES[parsed.path]
                self._send_file(root / name, content_type)
                return
            if parsed.path == '/api/state':
                if not self._authorized_header():
                    return
                try:
                    result = self.server.workbench_service.snapshot()
                    if not isinstance(result, dict):
                        raise TypeError('snapshot必须返回对象。')
                    self._send_json(200, result)
                except Exception:
                    self._json_error(500, 'service_error', '读取工作台状态失败。')
                return
            if parsed.path.startswith('/media/'):
                query = parse_qs(parsed.query, keep_blank_values=True)
                tokens = query.get('token', [])
                supplied = tokens[0] if len(tokens) == 1 else ''
                if not self._authorized(supplied):
                    return
                media_id = parsed.path[len('/media/'):]
                if not _MEDIA_ID.fullmatch(media_id):
                    self._json_error(404, 'not_found', '媒体不存在。')
                    return
                self._send_media(media_id)
                return
            self._json_error(404, 'not_found', '请求路径不存在。')

        def _send_media(self, media_id):
            try:
                media_path, expected_sha = self.server.workbench_service.resolve_media(media_id)
            except (FileNotFoundError, KeyError):
                self._json_error(404, 'not_found', '媒体不存在。')
                return
            except (ValueError, WorkbenchConflict):
                self._json_error(409, 'media_changed', '音频或试听链接已变化，请重新准备播放。')
                return
            except Exception:
                self._json_error(500, 'service_error', '读取媒体信息失败。')
                return
            if not isinstance(expected_sha, str) or not _SHA256.fullmatch(expected_sha):
                self._json_error(500, 'invalid_media', '媒体SHA256格式无效。')
                return
            try:
                fd = os.open(Path(media_path), os.O_RDONLY)
                stream = os.fdopen(fd, 'rb')
            except (OSError, ValueError, TypeError):
                self._json_error(404, 'not_found', '媒体不存在或无法读取。')
                return
            try:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    self._json_error(404, 'not_found', '媒体不是普通文件。')
                    return
                actual_sha = _hash_open_file(stream)
                after = os.fstat(stream.fileno())
                if (actual_sha.lower() != expected_sha.lower()
                        or (before.st_size, before.st_mtime_ns, before.st_ino)
                        != (after.st_size, after.st_mtime_ns, after.st_ino)):
                    self._json_error(409, 'media_changed', '媒体内容与已验证哈希不一致。')
                    return
                size = after.st_size
                try:
                    bounds = _range_bounds(self.headers.get('Range'), size)
                except ValueError as exc:
                    self.send_response(416)
                    _response_headers(self, 'application/json; charset=utf-8')
                    self.send_header('Content-Range', f'bytes */{size}')
                    body = json.dumps({'error': {'code': 'invalid_range', 'message': str(exc)}},
                                      ensure_ascii=False).encode('utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if bounds is None:
                    start, end, status = 0, size - 1, 200
                else:
                    start, end = bounds
                    status = 206
                length = max(0, end - start + 1)
                stream.seek(start)
                content_type = mimetypes.guess_type(str(media_path))[0] or 'application/octet-stream'
                self.send_response(status)
                _response_headers(self, content_type)
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Length', str(length))
                self.send_header('ETag', f'"{actual_sha.lower()}"')
                if status == 206:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.end_headers()
                remaining = length
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
            except (BrokenPipeError, ConnectionResetError):
                return
            except OSError:
                return
            finally:
                stream.close()

        def do_POST(self):
            if not self._check_request_origin():
                return
            parsed = urlsplit(self.path)
            if parsed.path not in ('/api/action', '/api/books/preview'):
                self._json_error(404, 'not_found', '请求路径不存在。')
                return
            if not self._authorized_header():
                return
            if parsed.path == '/api/books/preview':
                self._post_book_preview()
                return
            if self.headers.get('Transfer-Encoding'):
                self.close_connection = True
                self._json_error(400, 'invalid_body', '不支持分块请求体。')
                return
            length_headers = self.headers.get_all('Content-Length', [])
            raw_length = length_headers[0] if len(length_headers) == 1 else None
            try:
                if raw_length is None or not raw_length.isdecimal():
                    raise ValueError('Content-Length无效。')
                length = int(raw_length)
            except (TypeError, ValueError):
                self._json_error(400, 'invalid_body', 'Content-Length无效。')
                return
            if length < 0:
                self._json_error(400, 'invalid_body', 'Content-Length无效。')
                return
            if length > MAX_BODY_BYTES:
                self.close_connection = True
                self._json_error(413, 'body_too_large', '请求体超过256KB限制。')
                return
            if self.headers.get_content_type() != 'application/json':
                self._json_error(400, 'invalid_body', '请求体必须是application/json。')
                return
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError('请求体长度不完整。')
                request = json.loads(body.decode('utf-8'))
                if not isinstance(request, dict) or not isinstance(request.get('action'), str):
                    raise ValueError('请求必须包含字符串action字段。')
                name = request['action']
                payload = {key: value for key, value in request.items() if key != 'action'}
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self._json_error(400, 'invalid_body', str(exc) or 'JSON请求无效。')
                return
            try:
                result = self.server.workbench_service.action(name, payload)
                if not isinstance(result, dict):
                    raise TypeError('action必须返回对象。')
                self._send_json(200, result)
            except (WorkbenchConflict, RuntimeError) as exc:
                self._json_error(409, 'conflict', str(exc) or '操作与当前状态冲突。')
            except (ValueError, TypeError) as exc:
                self._json_error(400, 'invalid_action', str(exc) or '操作参数无效。')
            except Exception:
                self._json_error(500, 'service_error', '执行工作台操作失败。')

        def _post_book_preview(self):
            if self.headers.get('Transfer-Encoding'):
                self.close_connection = True
                self._json_error(400, 'invalid_body', '不支持分块请求体。')
                return
            filenames = self.headers.get_all('X-Book-Filename', [])
            if len(filenames) != 1:
                self.close_connection = True
                self._json_error(400, 'invalid_filename', '必须提供唯一的X-Book-Filename。')
                return
            try:
                if re.search(r'%(?![0-9a-fA-F]{2})', filenames[0]):
                    raise ValueError()
                filename = unquote_to_bytes(filenames[0]).decode('utf-8')
                if (not filename or '/' in filename or '\\' in filename or filename in ('.', '..')
                        or ':' in filename or any(ord(char) < 32 for char in filename)
                        or Path(filename).name != filename or Path(filename).suffix.lower() not in ('.txt', '.docx')):
                    raise ValueError()
            except (UnicodeDecodeError, ValueError):
                self.close_connection = True
                self._json_error(400, 'invalid_filename', '文件名必须是UTF-8编码的.txt或.docx basename。')
                return
            lengths = self.headers.get_all('Content-Length', [])
            try:
                if len(lengths) != 1 or not lengths[0].isdecimal():
                    raise ValueError()
                length = int(lengths[0])
                if not 1 <= length <= MAX_BOOK_UPLOAD_BYTES:
                    self.close_connection = True
                    self._json_error(413 if length > MAX_BOOK_UPLOAD_BYTES else 400,
                                     'body_too_large' if length > MAX_BOOK_UPLOAD_BYTES else 'invalid_body',
                                     '书籍文件需为1至64MiB。')
                    return
            except ValueError:
                self.close_connection = True
                self._json_error(400, 'invalid_body', 'Content-Length无效或重复。')
                return
            service = self.server.workbench_service
            if self.headers.get_content_type() != 'application/octet-stream':
                self.close_connection = True
                self._json_error(415, 'invalid_body', '请求体必须是原始文件字节流。')
                return
            if not service.reserve_import_upload():
                self.close_connection = True
                self._json_error(409, 'conflict', '当前操作正在进行，请等待完成。')
                return
            temporary = None
            deadline = time.monotonic() + 15.0
            received = 0
            try:
                directory = Path(service.root) / 'book-import-uploads'
                directory.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix='upload-', suffix=Path(filename).suffix.lower(), dir=directory)
                with os.fdopen(fd, 'wb') as output:
                    while received < length:
                        remaining_time = deadline - time.monotonic()
                        if remaining_time <= 0:
                            raise TimeoutError('上传超时。')
                        self.connection.settimeout(min(5.0, remaining_time))
                        block = self.rfile.read(min(256 * 1024, length - received))
                        if not block:
                            raise ValueError('请求体长度不完整。')
                        output.write(block)
                        received += len(block)
                if received != length:
                    raise ValueError('请求体长度不完整。')
                service.accept_import_upload(temporary, filename)
                self._send_json(202, service.snapshot())
            except (OSError, ValueError, TimeoutError, ConnectionError) as exc:
                if temporary:
                    Path(temporary).unlink(missing_ok=True)
                service.cancel_import_upload()
                self.close_connection = True
                self._json_error(400, 'invalid_body', str(exc) or '上传失败。')
            except Exception:
                if temporary:
                    Path(temporary).unlink(missing_ok=True)
                service.cancel_import_upload()
                self._json_error(500, 'service_error', '读取导入文件失败。')

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.workbench_service = service
    server.session_token = session_token
    return server
