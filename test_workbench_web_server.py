import hashlib
import http.client
from http.server import BaseHTTPRequestHandler
from io import BytesIO
import json
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from workbench_web_server import WorkbenchConflict, make_server


def write_pcm(path):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b'\x00\x00' * 64)
    return path


class FakeService:
    def __init__(self, media):
        self.media = media
        self.media_sha = hashlib.sha256(media.read_bytes()).hexdigest()
        self.actions = []

    def snapshot(self):
        return {'ready': True}

    def action(self, name, payload):
        if name == 'conflict':
            raise WorkbenchConflict('状态已变化。')
        if name == 'bad':
            raise ValueError('参数不合法。')
        if name == 'busy':
            raise RuntimeError('当前操作正在进行，请等待完成。')
        if name == 'explode':
            raise Exception('private detail')
        self.actions.append((name, payload))
        return {'ok': True}

    def resolve_media(self, media_id):
        return self.media, self.media_sha


class WorkbenchWebServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = write_pcm(self.root/'sample.wav')
        web = self.root/'web'; web.mkdir()
        (web/'index.html').write_text('<!doctype html><title>test</title>', encoding='utf-8')
        (web/'workbench.css').write_text('body{}', encoding='utf-8')
        (web/'workbench.js').write_text('void 0;', encoding='utf-8')
        self.service = FakeService(self.media)
        self.server = make_server(self.service, token='test-token', static_root=web)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, path, *, method='GET', headers=None, data=None):
        request = Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, response.headers, response.read()
        except HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_token_is_generated_and_server_binds_only_loopback(self):
        self.assertTrue(self.server.session_token)
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        server = make_server(self.service)
        try:
            self.assertTrue(server.session_token)
        finally:
            server.server_close()

    def test_handler_suppresses_connection_abort_but_propagates_other_errors(self):
        class FakeSocket:
            def makefile(self, *_args, **_kwargs):
                return BytesIO()
            def close(self):
                return
        handler_type = self.server.RequestHandlerClass
        request = FakeSocket()
        with patch.object(BaseHTTPRequestHandler, 'handle', side_effect=ConnectionAbortedError('client left')):
            handler_type(request, ('127.0.0.1', 12345), self.server)
        with patch.object(BaseHTTPRequestHandler, 'handle', side_effect=RuntimeError('handler bug')):
            with self.assertRaisesRegex(RuntimeError, 'handler bug'):
                handler_type(FakeSocket(), ('127.0.0.1', 12345), self.server)

    def test_state_requires_token_and_responses_are_hardened(self):
        status, _, _ = self.request('/api/state')
        self.assertEqual(status, 401)
        status, _, _ = self.request('/api/state', headers={'X-Workbench-Token':'é'})
        self.assertEqual(status, 401)
        status, headers, body = self.request('/api/state', headers={'X-Workbench-Token':'test-token'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'ready': True})
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("default-src 'self'", headers['Content-Security-Policy'])

    def test_action_auth_payload_flattening_and_service_errors(self):
        unauthenticated, _, _ = self.request('/api/action', method='POST',
                                             data=json.dumps({'action':'save'}).encode(),
                                             headers={'Content-Type':'application/json'})
        self.assertEqual(unauthenticated, 401)
        payload = json.dumps({'action':'save', 'chapter_id':'c01', 'value':2}).encode()
        status, _, _ = self.request('/api/action', method='POST', data=payload,
                                    headers={'X-Workbench-Token':'test-token',
                                             'Content-Type':'application/json'})
        self.assertEqual(status, 200)
        self.assertEqual(self.service.actions, [('save', {'chapter_id':'c01', 'value':2})])
        for name, expected in [('conflict', 409), ('busy', 409), ('bad', 400), ('explode', 500)]:
            body = json.dumps({'action':name}).encode()
            status, _, response = self.request('/api/action', method='POST', data=body,
                                               headers={'X-Workbench-Token':'test-token',
                                                        'Content-Type':'application/json'})
            self.assertEqual(status, expected)
            self.assertNotIn(b'private detail', response)
            if name == 'busy':
                self.assertIn('当前操作正在进行'.encode('utf-8'), response)

    def test_origin_and_host_are_validated(self):
        origin = f'http://127.0.0.1:{self.server.server_port}'
        status, _, _ = self.request('/api/state', headers={'X-Workbench-Token':'test-token',
                                                            'Origin':origin})
        self.assertEqual(status, 200)
        status, _, _ = self.request('/api/state', headers={'X-Workbench-Token':'test-token',
                                                            'Origin':'http://evil.example'})
        self.assertEqual(status, 403)
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request('GET', '/api/state', headers={'Host':'localhost',
                                                   'X-Workbench-Token':'test-token'})
        response = conn.getresponse(); response.read()
        self.assertEqual(response.status, 400)
        conn.close()

    def test_static_allowlist_and_traversal_rejection(self):
        for path, expected in [('/', 200), ('/workbench.css', 200), ('/workbench.js', 200),
                               ('/../sample.wav', 404), ('/%2e%2e/sample.wav', 404),
                               ('/media/../sample.wav?token=test-token', 404)]:
            status, _, _ = self.request(path)
            self.assertEqual(status, expected, path)

    def test_media_requires_token_supports_ranges_and_detects_tampering(self):
        media_url = '/media/chapter_01?token=test-token'
        status, headers, body = self.request('/media/chapter_01')
        self.assertEqual(status, 401)
        original = self.media.read_bytes()
        status, headers, body = self.request(media_url, headers={'Range':'bytes=0-11'})
        self.assertEqual(status, 206)
        self.assertEqual(body, original[:12])
        self.assertEqual(headers['Content-Range'], f'bytes 0-11/{len(original)}')
        status, _, body = self.request(media_url, headers={'Range':'bytes=0-1,4-5'})
        self.assertEqual(status, 416)
        self.media.write_bytes(original + b'changed')
        status, _, body = self.request(media_url)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)['error']['code'], 'media_changed')

    def test_body_limit_and_malformed_json(self):
        headers = {'X-Workbench-Token':'test-token', 'Content-Type':'application/json'}
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request('POST', '/api/action', headers={**headers, 'Content-Length':str(256*1024+1)})
        response = conn.getresponse(); response.read()
        status = response.status
        conn.close()
        self.assertEqual(status, 413)
        status, _, _ = self.request('/api/action', method='POST', data=b'{oops', headers=headers)
        self.assertEqual(status, 400)
        status, _, _ = self.request('/api/action', method='POST', data=b'{}',
                                    headers={'X-Workbench-Token':'test-token', 'Content-Type':'text/plain'})
        self.assertEqual(status, 400)


if __name__ == '__main__':
    unittest.main()
