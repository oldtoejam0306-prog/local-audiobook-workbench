import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import workbench_web as launch
from workbench_web_server import make_server


class StateService:
    def snapshot(self):
        return {'books': []}


class WorkbenchWebLaunchTests(unittest.TestCase):
    def test_existing_session_probes_only_loopback_and_rejects_bad_token_or_config(self):
        server = make_server(StateService(), token='launch-test-token')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td)/'session.json'
                record = {'port':server.server_port, 'token':'launch-test-token',
                          'pid':123, 'host':'0.0.0.0'}
                path.write_text(json.dumps(record), encoding='utf-8')
                self.assertEqual(launch.existing_session(path), record)
                record['token'] = 'wrong-token'
                path.write_text(json.dumps(record), encoding='utf-8')
                self.assertIsNone(launch.existing_session(path))
                for bad in ({'port':0, 'token':'x'}, {'port':65536, 'token':'x'},
                            {'port':server.server_port, 'token':None},
                            {'port':'127.0.0.1:'+str(server.server_port), 'token':'x'},
                            {'port':server.server_port}):
                    path.write_text(json.dumps(bad), encoding='utf-8')
                    self.assertIsNone(launch.existing_session(path), bad)
                path.write_text('{broken', encoding='utf-8')
                self.assertIsNone(launch.existing_session(path))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_existing_session_does_not_follow_redirect_between_loopback_servers(self):
        target_hits = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                target_hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *_args):
                return

        target = ThreadingHTTPServer(('127.0.0.1', 0), TargetHandler)

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location',
                                 f'http://127.0.0.1:{target.server_port}/redirect-target')
                self.end_headers()

            def log_message(self, *_args):
                return

        redirect = ThreadingHTTPServer(('127.0.0.1', 0), RedirectHandler)
        servers = (target, redirect)
        threads = [threading.Thread(target=server.serve_forever, daemon=True)
                   for server in servers]
        for thread in threads:
            thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                session = Path(td)/'session.json'
                session.write_text(json.dumps({'port':redirect.server_port,
                                               'token':'redirect-probe-token'}), encoding='utf-8')
                self.assertIsNone(launch.existing_session(session))
                self.assertEqual(target_hits, [])
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=3)

    def test_session_url_places_token_in_fragment(self):
        self.assertEqual(launch.session_url({'port':43123, 'token':'abc_def-123'}),
                         'http://127.0.0.1:43123/#token=abc_def-123')

    def test_main_reuses_existing_session_without_constructing_server(self):
        record = {'port':43123, 'token':'existing-token', 'pid':42}
        with tempfile.TemporaryDirectory() as td:
            with patch('workbench_real_v2_runtime.ROOT', Path(td)), \
                 patch.object(launch.auto, '_selection_lock', return_value=nullcontext()), \
                 patch.object(launch, 'existing_session', return_value=record) as existing, \
                 patch.object(launch, 'WorkbenchService') as service, \
                 patch.object(launch, 'make_server') as server, \
                 patch.object(launch.webbrowser, 'open') as browser:
                self.assertEqual(launch.main([]), 0)
            existing.assert_called_once()
            service.assert_not_called()
            server.assert_not_called()
            browser.assert_called_once_with('http://127.0.0.1:43123/#token=existing-token')


if __name__ == '__main__':
    unittest.main()
