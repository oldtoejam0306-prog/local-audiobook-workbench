"""Windows client for the isolated synthetic/voice protocol workers."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import wave
from pathlib import Path, PurePosixPath

import release_config


def identity(text: str, mode: str = "synthetic") -> str:
    value = {"mode": mode, "text": text}
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def call(payload: dict, worker: str) -> dict:
    command = ["ssh", *release_config.ssh_options(), release_config.ssh_target(), release_config.remote_command(worker)]
    result = subprocess.run(
        command, input=json.dumps(payload, ensure_ascii=False), text=True, encoding="utf-8",
        capture_output=True, timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError("remote experiment request failed")
    value = json.loads(result.stdout.strip().splitlines()[-1])
    if not isinstance(value, dict):
        raise ValueError("remote worker returned an invalid response")
    return value


def start(text: str, mode: str = "synthetic") -> dict:
    if mode not in ("synthetic", "voice") or not isinstance(text, str) or not text.strip() or len(text) > 80:
        raise ValueError("invalid controlled experiment request")
    job = identity(text, mode)
    worker = "controlled_real_voice.py" if mode == "voice" else "controlled_real_synthetic.py"
    result = call({"action": "start", "input": {"mode": mode, "text": text}}, worker)
    if result.get("job") != job or result.get("status") not in ("reserved", "already_reserved", "busy_or_uncertain"):
        raise ValueError("remote reservation did not match the request")
    return result


def status(job: str) -> dict:
    if len(job) != 64 or any(char not in "0123456789abcdef" for char in job):
        raise ValueError("invalid job identifier")
    result = call({"action": "status", "job": job}, "controlled_real_synthetic.py")
    if result.get("job") != job or result.get("input_sha256") != job or result.get("status") not in (
        "reserved", "running", "complete", "interrupted", "failed"
    ):
        raise ValueError("remote job state did not match the request")
    return result


def stop(job: str) -> dict:
    if len(job) != 64 or any(char not in "0123456789abcdef" for char in job):
        raise ValueError("invalid job identifier")
    return call({"action": "stop", "job": job}, "controlled_real_synthetic.py")


def fetch(job: str) -> Path:
    state = status(job)
    if state["status"] != "complete":
        raise ValueError("audio is not complete")
    expected = release_config.remote_controlled_root() / "jobs" / job / "audio.wav"
    if state.get("audio_path") != str(expected):
        raise ValueError("remote audio path did not match the configured job directory")
    local_root = release_config.local_workbench_root() / "playback"
    local_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="controlled-audio-", dir=local_root) as directory:
        path = Path(directory) / "audio.wav"
        command = ["scp", *release_config.ssh_options(), release_config.scp_source(str(expected)), str(path)]
        result = subprocess.run(command, capture_output=True, timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or not path.is_file() or path.stat().st_size != state.get("audio_bytes"):
            raise ValueError("audio transfer failed")
        if hashlib.sha256(path.read_bytes()).hexdigest() != state.get("audio_sha256"):
            raise ValueError("audio checksum mismatch")
        with wave.open(str(path), "rb") as wav:
            if not wav.getnframes() or wav.getcomptype() != "NONE":
                raise ValueError("audio format is invalid")
        final = local_root / (job + ".wav")
        path.replace(final)
    return final
