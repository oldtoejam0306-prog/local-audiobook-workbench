"""Apply the macOS offline write sandbox and monitor a render child process."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path(__file__).resolve().parent
profile = '(version 1)(allow default)(deny network*)(deny file-write*)(allow file-write* (subpath "' + str(root) + '"))'
python = os.environ.get("AUDIOBOOK_PYTHON", sys.executable)
sandbox = os.environ.get("AUDIOBOOK_SANDBOX_EXEC", "/usr/bin/sandbox-exec")
customvoice_hf_home = os.environ["AUDIOBOOK_CUSTOMVOICE_HF_HOME"]
import release_config
search_path = release_config.ffmpeg_search_path(os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"))
env = {
    "HOME": str(root),
    "TMPDIR": str(root),
    "HF_HOME": str(root / "hf"),
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PATH": search_path,
    "AUDIOBOOK_CUSTOMVOICE_HF_HOME": customvoice_hf_home,
    "AUDIOBOOK_PYTHON": python,
}
command = [sandbox, "-p", profile, python, "-B", str(root / "render-driver.py")]
started = time.monotonic()
peak = 0
minimum = 100
samples = 0
reason = None
with (root / "driver.stdout").open("xb") as stdout, (root / "driver.stderr").open("xb") as stderr:
    process = subprocess.Popen(command, cwd=root, env=env, stdout=stdout, stderr=stderr)
    while process.poll() is None:
        if time.monotonic() - started > 600:
            reason = "timeout"
            break
        rss = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(process.pid)], capture_output=True, text=True)
        if rss.returncode or not rss.stdout.strip().isdigit():
            if process.poll() is not None:
                break
            reason = "rss_monitor_failed"
            break
        peak = max(peak, int(rss.stdout.strip()) * 1024)
        samples += 1
        pressure = subprocess.run(["/usr/bin/memory_pressure"], capture_output=True, text=True)
        lines = [line for line in pressure.stdout.splitlines() if "System-wide memory free percentage:" in line]
        if pressure.returncode or len(lines) != 1:
            reason = "pressure_monitor_failed"
            break
        available = int(lines[0].split(":", 1)[1].strip().rstrip("%"))
        minimum = min(minimum, available)
        if available < 30:
            reason = "memory_pressure"
            break
        if peak > 6 * 1024**3:
            reason = "child_rss_exceeded"
            break
        time.sleep(2)
    if reason and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    else:
        process.wait()
record = {
    "exit_code": process.returncode,
    "seconds": round(time.monotonic() - started, 3),
    "sampled_peak_rss_bytes": peak,
    "minimum_pressure_free_percent": minimum,
    "samples": samples,
    "reason": reason,
}
(root / "monitor-result.json").write_text(json.dumps(record), encoding="utf-8")
print(json.dumps(record), flush=True)
if reason or process.returncode:
    raise SystemExit(1)
