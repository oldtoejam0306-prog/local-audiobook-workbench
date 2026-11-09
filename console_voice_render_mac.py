"""Render bounded local text with a previously generated synthetic voice."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import release_config

BASE = release_config.mac_stage_root()
ROOT = release_config.mac_voice_root()
TEMPLATE = Path(__file__).resolve().parent
sys.path.insert(0, str(TEMPLATE))
import experimental_isolated_voice_library as library


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def main() -> None:
    release_config.apply_mac_environment()
    request = json.load(sys.stdin)
    if request.get("action") == "echo":
        print(json.dumps({"ok": True, "text": request["text"]}, ensure_ascii=False))
        return
    if set(request) != {"action", "stage", "vector_sha256", "text"} or request["action"] != "render":
        raise ValueError("invalid request")
    text = request["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 500 or any(
        not c.isprintable() and c not in "\n\t" for c in text
    ):
        raise ValueError("text must contain 1 to 500 printable characters")
    stage = Path(request["stage"])
    if stage.parent != ROOT or not stage.name.startswith("voice-experiment-") or stage.is_symlink():
        raise ValueError("invalid voice stage")
    if sha((stage / "speaker-vector.npy").read_bytes()) != request["vector_sha256"]:
        raise ValueError("fixed voice vector changed")
    run = Path(tempfile.mkdtemp(prefix="render-", dir=ROOT))
    presets = release_config.mac_presets_path()
    snapshot = {"version": 1, "book_id": "isolated-book", "locked": {}}
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    published = library.publish_experiment(
        task_dir=stage,
        experiment_parent=BASE,
        book_id="isolated-book",
        character_id="isolated-character",
        locked_snapshot=snapshot,
        locked_snapshot_sha256=sha(canonical),
        trusted_presets_path=presets,
        trusted_presets_sha256=sha(presets.read_bytes()),
    )
    if published["metadata"]["vector_sha256"] != request["vector_sha256"]:
        raise ValueError("published vector mismatch")
    (run / "publish-result.json").write_text(json.dumps(published), encoding="utf-8")
    (run / "render-request.json").write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
    for name in ("render-driver.py", "monitor.py", "isolated_huashuo_library.py", "release_config.py"):
        shutil.copyfile(TEMPLATE / name, run / name)
    env = os.environ.copy()
    env.update({
        "AUDIOBOOK_CUSTOMVOICE_HF_HOME": str(release_config.mac_customvoice_hf_home()),
        "AUDIOBOOK_FFMPEG": str(release_config.mac_ffmpeg()),
        "AUDIOBOOK_SANDBOX_EXEC": str(release_config.sandbox_exec()),
        "AUDIOBOOK_PYTHON": str(Path(os.path.abspath(sys.executable))),
        "PYTHONPATH": str(TEMPLATE),
    })
    result = subprocess.run(
        [sys.executable, str(run / "monitor.py")], cwd=run, env=env,
        capture_output=True, text=True, timeout=650,
    )
    if result.returncode:
        raise RuntimeError("isolated render failed")
    report = json.loads((run / "render-result.json").read_text(encoding="utf-8"))
    mp3 = Path(report["stage"]) / "synthetic-result.mp3"
    if report["vector_sha256"] != request["vector_sha256"] or sha(mp3.read_bytes()) != report["audio_sha256"]:
        raise RuntimeError("render evidence mismatch")
    wav = run / "rendered.wav"
    subprocess.run([
        str(release_config.mac_ffmpeg()), "-v", "error", "-xerror", "-err_detect", "explode",
        "-i", str(mp3), "-ac", "1", "-ar", "24000", str(wav),
    ], check=True, timeout=120, capture_output=True)
    print(json.dumps({
        "ok": True, "wav": str(wav), "wav_sha256": sha(wav.read_bytes()),
        "text_sha256": sha(text.encode()), "vector_sha256": report["vector_sha256"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"ok": False, "error": "isolated render failed"}, ensure_ascii=False))
        sys.exit(1)
