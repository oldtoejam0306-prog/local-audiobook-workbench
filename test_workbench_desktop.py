from contextlib import nullcontext
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import workbench_desktop as desktop


class FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


class WorkbenchDesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        self.root = self.base / 'runtime'
        self.host = self.base / 'native' / 'AudiobookWorkbench.exe'
        self.host.parent.mkdir()
        self.host.write_bytes(b'host-fixture')
        self.icon = self.base / 'app.ico'
        self.icon.write_bytes(b'icon-fixture')

    def _launch(self, **kwargs):
        return desktop.launch_desktop(
            root=self.root, source_dir=self.source,
            env={'AUDIOBOOK_DESKTOP_HOST': str(self.host),
                 'AUDIOBOOK_APP_ICON': str(self.icon)},
            lock_factory=lambda: nullcontext(), **kwargs)

    def test_reuses_validated_session_and_starts_host_without_token_argument(self):
        record = {'port': 43123, 'token': 'secret-session-token'}
        calls = []

        def probe(path):
            self.assertEqual(path, self.root / 'web-session.private.json')
            return record

        def popen(args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess()

        self.assertEqual(self._launch(probe=probe, popen=popen), 0)
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args, [str(self.host), '--session',
                                str(self.root / 'web-session.private.json'),
                                '--profile', str(self.root / 'desktop-webview2'),
                                '--icon', str(self.icon)])
        self.assertNotIn(record['token'], args)
        self.assertEqual(kwargs['cwd'], str(self.source))
        self.assertFalse(any('workbench_web.py' in str(arg) for arg in args))

    def test_starts_no_browser_service_then_retries_until_valid_session(self):
        calls, probes, delays = [], [], []
        record = {'port': 45001, 'token': 'never-log-token'}
        process = FakeProcess(returncode=0)

        def popen(args, **kwargs):
            calls.append((args, kwargs))
            return process

        def probe(path):
            probes.append(path)
            return record if len(probes) >= 3 else None

        clock_values = iter([0.0, 0.1, 0.2, 0.3])
        self.assertEqual(self._launch(
            probe=probe, popen=popen, sleep=delays.append,
            clock=lambda: next(clock_values)), 0)
        self.assertEqual(len(calls), 2)
        service_args, service_kwargs = calls[0]
        self.assertEqual(service_args[-2:], [str(self.source / 'workbench_web.py'), '--no-browser'])
        self.assertEqual(service_kwargs['cwd'], str(self.source))
        self.assertEqual(service_kwargs['stdout'].name,
                         str(self.root / 'desktop-service-startup.log'))
        self.assertEqual(delays, [0.15])
        self.assertEqual(service_kwargs['env']['AUDIOBOOK_DESKTOP_HOST'], str(self.host))
        host_args = calls[1][0]
        self.assertEqual(host_args[0], str(self.host))
        self.assertNotIn(record['token'], host_args)

    def test_timeout_reports_startup_log_and_does_not_open_browser(self):
        calls, shown, ticks = [], [], iter([0.0, 0.5, 1.0, 1.5])

        def popen(args, **kwargs):
            calls.append(args)
            return FakeProcess(returncode=0)

        result = desktop.main(
            [], launcher=lambda: self._launch(
                probe=lambda _path: None, popen=popen, sleep=lambda _delay: None,
                clock=lambda: next(ticks), timeout=1.0),
            show_error=shown.append)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn('desktop-service-startup.log', shown[0])
        with patch('workbench_web.webbrowser.open') as browser_open:
            self.assertFalse(browser_open.called)

    def test_missing_host_fails_before_starting_service(self):
        calls, shown = [], []
        result = desktop.main(
            [], launcher=lambda: desktop.launch_desktop(
                root=self.root, source_dir=self.source,
                env={'AUDIOBOOK_DESKTOP_HOST': str(self.base / 'absent.exe')},
                popen=lambda *a, **k: calls.append(a),
                probe=lambda _path: None, lock_factory=lambda: nullcontext()),
            show_error=shown.append)
        self.assertEqual(result, 1)
        self.assertEqual(calls, [])
        self.assertIn('找不到', shown[0])

    def test_missing_icon_fails_before_starting_service(self):
        calls, shown = [], []
        result = desktop.main(
            [], launcher=lambda: desktop.launch_desktop(
                root=self.root, source_dir=self.source,
                env={'AUDIOBOOK_DESKTOP_HOST': str(self.host),
                     'AUDIOBOOK_APP_ICON': str(self.base / 'absent.ico')},
                popen=lambda *a, **k: calls.append(a),
                probe=lambda _path: None, lock_factory=lambda: nullcontext()),
            show_error=shown.append)
        self.assertEqual(result, 1)
        self.assertEqual(calls, [])
        self.assertIn('图标', shown[0])

    def test_windows_creation_flags_only_use_create_no_window(self):
        self.assertEqual(desktop._creationflags(),
                         getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def test_host_configuration_override_and_diagnostics_are_passed_as_args(self):
        custom_host = self.base / 'custom-host.exe'
        custom_host.write_bytes(b'x')
        custom_icon = self.base / 'custom-icon.ico'
        custom_icon.write_bytes(b'y')
        diagnostics = self.base / 'diag.json'
        captured = []
        desktop.launch_desktop(
            root=self.root, source_dir=self.source,
            env={'AUDIOBOOK_DESKTOP_HOST': str(custom_host),
                 'AUDIOBOOK_APP_ICON': str(custom_icon),
                 'AUDIOBOOK_DESKTOP_DIAGNOSTICS_FILE': str(diagnostics)},
            popen=lambda args, **kwargs: captured.append(args),
            probe=lambda _path: {'port': 43123, 'token': 'private'},
            lock_factory=lambda: nullcontext())
        self.assertEqual(captured[0][0], str(custom_host))
        self.assertEqual(captured[0][-2:], ['--diagnostics-file', str(diagnostics)])
        self.assertIn(str(custom_icon), captured[0])

    def test_existing_session_never_opens_browser_and_skips_service_start(self):
        calls = []
        configured_env = {'AUDIOBOOK_WORKBENCH_ROOT': str(self.root), 'KEEP': 'value'}
        with patch.object(desktop.workbench_web.webbrowser, 'open') as browser_open:
            desktop.ensure_service_session(
                source_dir=self.source, root=self.root, env=configured_env,
                popen=lambda args, **kwargs: (calls.append((args, kwargs)) or FakeProcess()),
                probe=lambda _path: {'port': 40000, 'token': 'opaque'},
                lock_factory=lambda: nullcontext())
            browser_open.assert_not_called()
        self.assertEqual(calls, [])

    def test_default_host_and_icon_paths_match_desktop_contract(self):
        host = self.source / 'desktop' / 'bin' / 'AudiobookWorkbench.exe'
        icon = self.source / 'assets' / 'app-icon' / 'audiobook-workbench.ico'
        host.parent.mkdir(parents=True)
        icon.parent.mkdir(parents=True)
        host.write_bytes(b'host')
        icon.write_bytes(b'icon')
        captured = []
        desktop.launch_desktop(
            root=self.root, source_dir=self.source, env={},
            popen=lambda args, **kwargs: captured.append(args),
            probe=lambda _path: {'port': 43123, 'token': 'hidden'},
            lock_factory=lambda: nullcontext())
        self.assertEqual(captured[0][0], str(host))
        self.assertEqual(captured[0][captured[0].index('--icon') + 1], str(icon))


if __name__ == '__main__':
    unittest.main()
