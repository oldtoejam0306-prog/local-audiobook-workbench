"""Start the native desktop host and reuse the local workbench service."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

import real_book_auto_voice as auto
import workbench_web


class DesktopLaunchError(RuntimeError):
    """A user-facing failure while preparing the desktop workbench."""


def _runtime_root():
    import workbench_real_v2_runtime as runtime
    return Path(runtime.ROOT)


def _creationflags():
    if os.name != 'nt':
        return 0
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _pythonw():
    candidate = Path(sys.executable).with_name('pythonw.exe')
    return str(candidate if candidate.is_file() else Path(sys.executable))


def _start_service(source_dir, root, env, popen):
    log_path = root / 'desktop-service-startup.log'
    try:
        root.mkdir(parents=True, exist_ok=True)
        with log_path.open('a', encoding='utf-8') as log:
            return popen(
                [_pythonw(), str(source_dir / 'workbench_web.py'), '--no-browser'],
                cwd=str(source_dir), env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=_creationflags())
    except OSError as exc:
        raise DesktopLaunchError(
            f'无法启动本机工作台服务。可查看启动日志：{log_path}') from exc


def ensure_service_session(*, source_dir, root, env, popen=subprocess.Popen,
                           probe=workbench_web.existing_session,
                           sleep=time.sleep, clock=time.monotonic,
                           lock_factory=None, timeout=20.0):
    """Reuse a verified session or start the no-browser service and await it."""
    source_dir, root = Path(source_dir), Path(root)
    session_path = root / 'web-session.private.json'
    if lock_factory is None:
        lock_factory = lambda: auto._selection_lock(
            root / '.desktop-launch-locks', 'local')

    lock = lock_factory()
    with lock:
        record = probe(session_path)
        if record is not None:
            return record

        child = _start_service(source_dir, root, env, popen)
        deadline = clock() + timeout
        while True:
            # The validated endpoint is authoritative, including when a child
            # exits successfully because another launcher won the startup race.
            record = probe(session_path)
            if record is not None:
                return record
            if clock() >= deadline:
                code = child.poll()
                if code is None:
                    reason = '服务启动等待超时。'
                else:
                    reason = f'服务进程已退出（代码 {code}），且未发现有效会话。'
                raise DesktopLaunchError(
                    f'{reason} 可查看启动日志：{root / "desktop-service-startup.log"}')
            sleep(0.15)


def launch_desktop(*, root=None, source_dir=None, env=None, popen=subprocess.Popen,
                   probe=workbench_web.existing_session, sleep=time.sleep,
                   clock=time.monotonic,
                   lock_factory=None, timeout=20.0):
    """Ensure the backend, start the host without waiting for its lifetime."""
    source_dir = Path(source_dir or Path(__file__).resolve().parent)
    root = Path(root or _runtime_root())
    child_env = dict(os.environ if env is None else env)
    host_value = child_env.get('AUDIOBOOK_DESKTOP_HOST')
    host = Path(host_value) if host_value else source_dir / 'desktop' / 'bin' / 'AudiobookWorkbench.exe'
    icon_value = child_env.get('AUDIOBOOK_APP_ICON')
    icon = Path(icon_value) if icon_value else source_dir / 'assets' / 'app-icon' / 'audiobook-workbench.ico'
    if not host.is_file():
        raise DesktopLaunchError(f'找不到有声书桌面程序：{host}')
    if not icon.is_file():
        raise DesktopLaunchError(f'找不到有声书工作台图标：{icon}')

    record = ensure_service_session(
        source_dir=source_dir, root=root, env=child_env, popen=popen,
        probe=probe, sleep=sleep, clock=clock, lock_factory=lock_factory,
        timeout=timeout)
    session_path = root / 'web-session.private.json'
    profile = root / 'desktop-webview2'
    args = [str(host), '--session', str(session_path), '--profile', str(profile),
            '--icon', str(icon)]
    diagnostics = child_env.get('AUDIOBOOK_DESKTOP_DIAGNOSTICS_FILE')
    if diagnostics:
        args.extend(['--diagnostics-file', diagnostics])
    try:
        popen(args, cwd=str(source_dir), env=child_env,
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, creationflags=_creationflags())
    except OSError as exc:
        raise DesktopLaunchError(f'无法启动有声书桌面程序：{host}') from exc
    return 0


def _show_error(message):
    if os.name == 'nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, '有声书工作台', 0x10)


def main(argv=None, *, launcher=launch_desktop, show_error=_show_error):
    parser = argparse.ArgumentParser(description='启动有声书桌面工作台')
    parser.add_argument('--diagnostics-file', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.diagnostics_file:
        os.environ['AUDIOBOOK_DESKTOP_DIAGNOSTICS_FILE'] = args.diagnostics_file
    try:
        return launcher()
    except DesktopLaunchError as exc:
        show_error(str(exc))
        return 1
    except Exception:
        show_error('启动有声书工作台失败，请检查安装配置和本机启动日志。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
