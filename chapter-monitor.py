"""Run the private one-shot chapter driver under the configured Mac sandbox."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

root = Path(__file__).resolve().parent
import release_config

python = os.environ.get('AUDIOBOOK_PYTHON') or sys.executable
sandbox = os.environ.get('AUDIOBOOK_SANDBOX_EXEC') or str(release_config.sandbox_exec())
config_file = os.environ.get('AUDIOBOOK_CONFIG_FILE')
if not config_file:
    raise RuntimeError('AUDIOBOOK_CONFIG_FILE is required')
customvoice_hf_home = str(release_config.mac_customvoice_hf_home())
search_path = release_config.ffmpeg_search_path(os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin'))
env = {
    'HOME': str(root),
    'TMPDIR': str(root),
    'HF_HOME': customvoice_hf_home,
    'HF_HUB_OFFLINE': '1',
    'HF_HUB_DISABLE_TELEMETRY': '1',
    'TRANSFORMERS_OFFLINE': '1',
    'PYTHONDONTWRITEBYTECODE': '1',
    'PATH': search_path,
    'PYTHONPATH': str(root),
    'AUDIOBOOK_CONFIG_FILE': config_file,
    'AUDIOBOOK_DATA_ROOT': str(release_config.mac_data_root()),
    'AUDIOBOOK_STAGE_ROOT': str(release_config.mac_stage_root()),
    'AUDIOBOOK_VOICE_STAGE_ROOT': str(release_config.mac_voice_root()),
    'AUDIOBOOK_VOICE_ROOT': str(release_config.mac_voice_library_root()),
    'AUDIOBOOK_PRESETS_PATH': str(release_config.mac_presets_path()),
    'HUASHUO_ROOT': str(release_config.huashuo_root()),
    'AUDIOBOOK_HUASHUO_CLI': str(release_config.mac_huashuo_cli()),
    'AUDIOBOOK_CUSTOMVOICE_HF_HOME': customvoice_hf_home,
    'AUDIOBOOK_VOICE_DESIGN_MODEL_ROOT': str(release_config.mac_voice_design_model_root()),
    'AUDIOBOOK_PYTHON': python,
    'AUDIOBOOK_SANDBOX_EXEC': sandbox,
    'AUDIOBOOK_FFMPEG': str(release_config.mac_ffmpeg()),
    'AUDIOBOOK_FFPROBE': str(release_config.mac_ffprobe()),
    'AUDIOBOOK_CONTROLLED_REAL_ROOT': str(release_config.mac_controlled_root()),
}
profile = '(version 1)(allow default)(deny network*)(deny file-write*)(allow file-write* (subpath "' + str(root) + '"))'
command = [sandbox, '-p', profile, python, '-B', str(root / 'render-driver.py')]


def stop_group(process):
    try: os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(); return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        process.poll()
        try: os.killpg(process.pid, 0)
        except ProcessLookupError: break
        time.sleep(0.1)
    else:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    process.wait()


child = None


def terminate(_signum, _frame):
    if child is not None: stop_group(child)
    raise SystemExit(128 + _signum)


signal.signal(signal.SIGTERM, terminate)
started = time.monotonic()
peak = 0
minimum = 100
samples = 0
reason = None
with (root / 'driver.stdout').open('xb') as stdout, (root / 'driver.stderr').open('xb') as stderr:
    process = subprocess.Popen(command, cwd=root, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
    child = process
    try:
        while process.poll() is None:
            if time.monotonic() - started > 1800:
                reason = 'timeout'
                break
            rss = subprocess.run(['/bin/ps', '-o', 'rss=', '-p', str(process.pid)],
                                 capture_output=True, text=True, timeout=10)
            if rss.returncode or not rss.stdout.strip().isdigit():
                if process.poll() is not None: break
                reason = 'rss_monitor_failed'
                break
            peak = max(peak, int(rss.stdout.strip()) * 1024)
            samples += 1
            pressure = subprocess.run(['/usr/bin/memory_pressure'], capture_output=True,
                                      text=True, timeout=10)
            lines = [line for line in pressure.stdout.splitlines()
                     if 'System-wide memory free percentage:' in line]
            if pressure.returncode or len(lines) != 1:
                reason = 'pressure_monitor_failed'
                break
            available = int(lines[0].split(':', 1)[1].strip().rstrip('%'))
            minimum = min(minimum, available)
            if available < 30:
                reason = 'memory_pressure'
                break
            if peak > 6 * 1024**3:
                reason = 'child_rss_exceeded'
                break
            time.sleep(2)
    finally:
        # The driver is a separate process group; always kill/reap descendants,
        # including the case where the leader exits before a helper process.
        stop_group(process)
record = {'exit_code': process.returncode,
          'seconds': round(time.monotonic() - started, 3),
          'sampled_peak_rss_bytes': peak,
          'minimum_pressure_free_percent': minimum,
          'samples': samples,
          'reason': reason}
(root / 'monitor-result.json').write_text(json.dumps(record), encoding='utf-8')
print(json.dumps(record), flush=True)
if reason or process.returncode:
    raise SystemExit(1)
