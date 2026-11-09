"""Mac-only, bounded multi-voice chapter renderer driven by private config/input."""
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
import experimental_isolated_voice_library as library


DRIVER = Path(__file__).resolve().parent
RUN_ROOT = release_config.mac_controlled_root() / "multivoice"


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def under(path: Path, root: Path) -> bool:
    try:
        return path.resolve(strict=True).is_relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError):
        return False


def load_input(value: str) -> tuple[Path, dict, str, str]:
    path = Path(value).expanduser()
    if path.is_symlink() or not path.is_file() or not under(path, release_config.mac_data_root()):
        raise ValueError("input must be a regular file under the configured data root")
    raw = path.read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("chapter request exceeds the size limit")
    request = json.loads(raw)
    if not isinstance(request, dict) or set(request) != {"book", "chapters"}:
        raise ValueError("input must contain one explicit book and chapter list")
    if not isinstance(request["book"], str) or not request["book"].strip() or len(request["book"]) > 120:
        raise ValueError("book label is invalid")
    chapters = request["chapters"]
    if not isinstance(chapters, list) or len(chapters) != 1 or not isinstance(chapters[0], dict):
        raise ValueError("exactly one chapter is required")
    chapter = chapters[0]
    chapter_id = chapter.get("id")
    segments = chapter.get("segments")
    if not isinstance(chapter_id, str) or not chapter_id.startswith("c") or not chapter_id[1:].isdigit():
        raise ValueError("chapter identifier is invalid")
    if not isinstance(segments, list) or not segments or len(segments) > 2000:
        raise ValueError("chapter segments are missing or too numerous")
    total = 0
    for item in segments:
        if not isinstance(item, dict) or set(item) != {"speaker_id", "text"}:
            raise ValueError("invalid segment entry")
        if not isinstance(item["speaker_id"], str) or not isinstance(item["text"], str) or not item["text"].strip():
            raise ValueError("segment needs a speaker id and text")
        total += len(item["text"])
    if total > 18000:
        raise ValueError("chapter text exceeds the configured experiment limit")
    return path.resolve(strict=True), request, chapter_id, digest(raw)


def prior_run(value: str | None, input_sha256: str, chapter_id: str) -> Path | None:
    if not value:
        return None
    path = Path(value).resolve(strict=True)
    if path.parent != RUN_ROOT.resolve(strict=True) or not path.name.startswith(chapter_id + "-run-"):
        raise ValueError("prior run must be a run created by this controller")
    if digest((path / "input.json").read_bytes()) != input_sha256:
        raise ValueError("prior run input differs from the current request")
    return path


def configured_voice_map(segments: list[dict]) -> list[dict]:
    entries = release_config.mac_multivoice_voices()
    by_role = {entry["role"]: entry for entry in entries}
    used = {segment["speaker_id"] for segment in segments}
    if "narrator" not in used or used - set(by_role):
        raise ValueError("configure one voice stage for each speaker role, including narrator")
    selected = [by_role[role] for role in sorted(used)]
    voice_root = release_config.mac_voice_root().resolve(strict=True)
    for entry in selected:
        raw_stage = entry["stage"]
        if raw_stage.is_symlink():
            raise ValueError("voice stage must not be a symlink")
        stage = raw_stage.resolve(strict=True)
        if stage.parent != voice_root:
            raise ValueError("voice stage must be a direct child of the configured voice-stage directory")
        vector = stage / "speaker-vector.npy"
        if vector.is_symlink() or digest(vector.read_bytes()) != entry["vector_sha256"]:
            raise ValueError("configured voice vector is missing or changed")
    return selected


def validate_publications(voices: list[dict], publications: list[dict], experiment_parent: Path) -> dict[str, str]:
    """Keep publication refs stable across cache batches; reject mismatched vectors."""
    if not isinstance(publications, list) or len(publications) != len(voices):
        raise ValueError("prior voice publications do not match the configured roles")
    expected = {item["role"]: item["vector_sha256"] for item in voices}
    refs = {}
    parent = experiment_parent.resolve(strict=True)
    for item in publications:
        if not isinstance(item, dict) or item.get("role") not in expected:
            raise ValueError("prior publication has an unknown role")
        role = item["role"]
        if role in refs or item.get("metadata", {}).get("vector_sha256") != expected[role]:
            raise ValueError("prior voice publication vector changed")
        voice_ref = item.get("voice_ref")
        library_dir = Path(item.get("library_dir", ""))
        if not isinstance(voice_ref, str) or not voice_ref or library_dir.is_symlink():
            raise ValueError("prior voice publication is malformed")
        library_dir = library_dir.resolve(strict=True)
        if not library_dir.is_relative_to(parent) or not library_dir.is_dir():
            raise ValueError("prior voice library escaped the experiment root")
        refs[role] = voice_ref
    if set(refs) != set(expected):
        raise ValueError("prior voice publication role set changed")
    return refs


def main() -> None:
    release_config.apply_mac_environment()
    cooperative_lock = release_config.mac_controlled_root() / "active.lock"
    if cooperative_lock.is_symlink() or not cooperative_lock.is_dir():
        raise RuntimeError("multi-voice worker must run through its cooperative batch controller")
    if len(sys.argv) not in (2, 3):
        raise ValueError("usage: isolated_real_multivoice_mac.py INPUT_JSON [PRIOR_RUN]")
    input_path, request, chapter_id, input_hash = load_input(sys.argv[1])
    chapter = request["chapters"][0]
    voices = configured_voice_map(chapter["segments"])
    prior = prior_run(sys.argv[2] if len(sys.argv) == 3 else None, input_hash, chapter_id)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=chapter_id + "-run-", dir=RUN_ROOT)).resolve()
    (run / "input.json").write_bytes(input_path.read_bytes())
    if prior:
        stages = list(prior.glob("huashuo-isolated-*"))
        if len(stages) != 1 or stages[0].is_symlink():
            raise ValueError("prior run has no unique isolated Huashuo stage")
        prior_stage = stages[0].resolve(strict=True)
        if prior_stage.parent != prior:
            raise ValueError("prior stage escaped its run directory")
        (run / "resume-stage.txt").write_text(str(prior_stage), encoding="utf-8")
    config = release_config.section("mac")
    role_map = {entry["role"]: entry["stage"] for entry in voices}
    publications = []
    snapshot = {"version": 1, "book_id": "isolated-book", "locked": {}}
    snapshot_bytes = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    presets = release_config.mac_presets_path()
    experiment_parent = release_config.mac_stage_root().resolve(strict=True)
    presets_sha256 = digest(presets.read_bytes())
    if prior is not None:
        publications = json.loads((prior / "publish-result.json").read_text(encoding="utf-8"))
        refs = validate_publications(voices, publications, experiment_parent)
        for item in publications:
            verified = library.verify_experiment_library(
                library_dir=item["library_dir"], expected_receipt_sha256=item["receipt_sha256"],
                trusted_presets_sha256=presets_sha256,
            )
            if verified["voice_ref"] != refs[item["role"]]:
                raise ValueError("prior voice library verification changed its reference")
    else:
        for entry in voices:
            published = library.publish_experiment(
                task_dir=entry["stage"], experiment_parent=experiment_parent,
                book_id="isolated-book", character_id=entry["role"], locked_snapshot=snapshot,
                locked_snapshot_sha256=digest(snapshot_bytes), trusted_presets_path=presets,
                trusted_presets_sha256=presets_sha256,
            )
            published["role"] = entry["role"]
            if published["metadata"]["vector_sha256"] != entry["vector_sha256"]:
                raise ValueError("published voice vector does not match configuration")
            publications.append(published)
        validate_publications(voices, publications, experiment_parent)
    (run / "publish-result.json").write_text(json.dumps(publications), encoding="utf-8")
    segments = chapter["segments"]
    (run / "render-request.json").write_text(json.dumps({"segments": segments}, ensure_ascii=False), encoding="utf-8")
    for filename in ("render-driver.py", "monitor.py", "isolated_huashuo_library.py", "release_config.py"):
        shutil.copyfile(DRIVER / filename, run / filename)
    env = os.environ.copy()
    env.update({
        "AUDIOBOOK_CUSTOMVOICE_HF_HOME": str(release_config.mac_customvoice_hf_home()),
        "AUDIOBOOK_FFMPEG": str(release_config.mac_ffmpeg()),
        "AUDIOBOOK_SANDBOX_EXEC": str(release_config.sandbox_exec()),
        "AUDIOBOOK_PYTHON": str(Path(os.path.abspath(sys.executable))),
        "PYTHONPATH": str(DRIVER),
    })
    monitored = subprocess.run([sys.executable, str(run / "monitor.py")], cwd=run, env=env,
                                capture_output=True, text=True, timeout=700)
    if monitored.returncode:
        raise RuntimeError("isolated multi-voice batch failed")
    result = json.loads((run / "render-result.json").read_text(encoding="utf-8"))
    if result.get("status") == "complete":
        mp3 = Path(result["stage"]) / "synthetic-result.mp3"
        wav = run / "chapter.wav"
        subprocess.run([str(release_config.mac_ffmpeg()), "-v", "error", "-xerror", "-err_detect", "explode",
                        "-i", str(mp3), "-ac", "1", "-ar", "24000", str(wav)],
                       check=True, timeout=180, capture_output=True)
        result["wav_path"] = str(wav)
        result["wav_sha256"] = digest(wav.read_bytes())
        result["wav_bytes"] = wav.stat().st_size
    result["input_sha256"] = input_hash
    result["chapter_id"] = chapter_id
    (run / "multivoice-result.json").write_text(json.dumps(result), encoding="utf-8")
    print(json.dumps({"run": str(run), "status": result["status"], "input_sha256": input_hash,
                      "model_inventory_sha256": result.get("model_inventory_sha256")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
