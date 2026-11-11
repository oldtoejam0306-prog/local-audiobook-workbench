"""Launch the local browser UI with the existing Windows workbench configuration."""
import argparse
import json
import os
from pathlib import Path
import threading
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import webbrowser

import real_book_auto_voice as auto
from workbench_web_server import make_server
from workbench_web_state import WorkbenchService


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def session_url(record):
    return f"http://127.0.0.1:{record['port']}/#token={record['token']}"


def existing_session(path):
    """Probe only the exact loopback endpoint saved by this launcher."""
    try:
        record = json.loads(Path(path).read_text(encoding='utf-8'))
        port, token = record['port'], record['token']
        if type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str):
            return None
        request = Request(f'http://127.0.0.1:{port}/api/state',
                          headers={'X-Workbench-Token': token})
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=2) as response:
            state = json.load(response)
        return record if isinstance(state, dict) and 'books' in state else None
    except (OSError, ValueError, KeyError, TypeError, URLError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description='启动本机有声书工作台')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    import workbench_real_v2_runtime as runtime
    root = Path(runtime.ROOT)
    root.mkdir(parents=True, exist_ok=True)
    session_path = root / 'web-session.private.json'
    with auto._selection_lock(root / '.web-launch-locks', 'local'):
        record = existing_session(session_path)
        if record is not None:
            if not args.no_browser:
                webbrowser.open(session_url(record))
            return 0
        service = WorkbenchService(runtime=runtime)
        server = make_server(service, port=args.port)
        service.on_shutdown = lambda: threading.Thread(target=server.shutdown, daemon=True).start()
        record = {'port': server.server_port, 'token': server.session_token, 'pid': os.getpid()}
        temporary = session_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record), encoding='utf-8')
        os.replace(temporary, session_path)
    if not args.no_browser:
        webbrowser.open(session_url(record))
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        server.server_close()
        try:
            if json.loads(session_path.read_text(encoding='utf-8')) == record:
                session_path.unlink()
        except (OSError, ValueError):
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
