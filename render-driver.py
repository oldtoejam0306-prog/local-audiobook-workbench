"""Sandbox child that renders one generated synthetic voice against caller text."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import time

root = Path(__file__).resolve().parent
probe = socket.socket()
probe.settimeout(1)
try:
    try:
        probe.connect(("127.0.0.1", 9))
    except PermissionError:
        pass
    else:
        raise RuntimeError("network probe failed")
finally:
    probe.close()
try:
    fd = os.open("/dev/null", os.O_WRONLY)
except PermissionError:
    pass
else:
    os.close(fd)
    raise RuntimeError("outside write probe failed")

import isolated_huashuo_library as wrapper
import huashuo.library as library
import release_config

published_value = json.loads((root / "publish-result.json").read_text(encoding="utf-8"))
published = published_value if isinstance(published_value, list) else [published_value]
if not published:
    raise ValueError("no published voice")
voice_refs = {}
expected_vectors = {}
for index, item in enumerate(published):
    role = item.get("role", "synthetic_character" if len(published) == 1 else "")
    if not role or role in voice_refs:
        raise ValueError("voice roles are missing or duplicated")
    voice_refs[role] = item["voice_ref"]
    expected_vectors[item["voice_ref"]] = item["metadata"]["vector_sha256"]
if len(published) == 1:
    voice_refs.setdefault("narrator", published[0]["voice_ref"])
stage = wrapper.create_staging_root(root)


def persist(name, value):
    temp = stage / (name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, stage / name)
    fd = os.open(stage, os.O_RDONLY)
    os.fsync(fd)
    os.close(fd)


try:
    shutil.copytree(published[0]["library_dir"], stage / "voices")
    voice_dir = stage / "voices" / "zh"
    voice_dir.mkdir(parents=True, exist_ok=True)
    for item in published[1:]:
        for vector_file in (Path(item["library_dir"]) / "zh").glob("*"):
            target = voice_dir / vector_file.name
            if target.exists():
                if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(vector_file.read_bytes()).digest():
                    raise ValueError("voice library filename collision")
            else:
                shutil.copyfile(vector_file, target)
    source_model = release_config.validate_customvoice_hf_home(os.environ["AUDIOBOOK_CUSTOMVOICE_HF_HOME"])

    def inventory(folder):
        result = {}
        for item in sorted(folder.rglob("*")):
            resolved = item.resolve(strict=True) if item.is_symlink() else item
            if item.is_symlink() and not resolved.is_relative_to(folder.resolve(strict=True)):
                raise RuntimeError("model link escapes the configured cache root")
            if resolved.is_file():
                digest = hashlib.sha256()
                with resolved.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                result[item.relative_to(folder).as_posix()] = digest.hexdigest()
        return result

    before = inventory(source_model)
    shutil.copytree(source_model, stage / "hf", symlinks=False)
    after = inventory(stage / "hf")
    if not before or before != after:
        raise RuntimeError("local model copy inventory mismatch")
    os.environ["HF_HOME"] = str(stage / "hf")
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(stage / "hf" / "hub")
    os.environ["TRANSFORMERS_CACHE"] = str(stage / "hf" / "hub")
    model_hash = hashlib.sha256(json.dumps(before, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    request = json.loads((root / "render-request.json").read_text(encoding="utf-8"))
    if isinstance(request.get("segments"), list):
        segments = request["segments"]
        if not segments or segments[0].get("speaker_id") != "narrator":
            raise ValueError("multi-voice chapter must begin with a narrator segment")
        for segment in segments:
            if not isinstance(segment, dict) or set(segment) != {"speaker_id", "text"}:
                raise ValueError("invalid render segment")
            if segment["speaker_id"] not in voice_refs or not isinstance(segment["text"], str) or not segment["text"].strip():
                raise ValueError("segment has no bound voice or text")
        lines = [segment["text"] for segment in segments]
        roles = [segment["speaker_id"] for segment in segments]
        text = "".join(lines)
        title = lines[0]
        multi_voice = True
    else:
        line = request.get("text")
        if not isinstance(line, str) or not line.strip() or len(line) > 500:
            raise ValueError("render text is invalid")
        title = "Synthetic chapter"
        lines = [title, line]
        roles = ["narrator", "synthetic_character"]
        text = title + "\n" + line + "\n"
        multi_voice = False
    workdir = stage / "work"
    workdir.mkdir()
    resume_file = root / "resume-stage.txt"
    if resume_file.is_file():
        raw_resume_stage = Path(resume_file.read_text(encoding="utf-8"))
        if raw_resume_stage.is_symlink():
            raise ValueError("resume stage may not be a symlink")
        resume_stage = raw_resume_stage.resolve(strict=True)
        if resume_stage.parent.parent.resolve(strict=True) != root.parent.resolve(strict=True):
            raise ValueError("resume stage is outside the prior run area")
        prior_cache = resume_stage / "work" / "cache" / "units"
        if prior_cache.is_symlink():
            raise ValueError("resume cache may not be a symlink")
        if prior_cache.is_dir():
            target_cache = workdir / "cache" / "units"
            target_cache.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(prior_cache, target_cache, symlinks=False)
    (stage / "book.txt").write_text(text, encoding="utf-8")
    (workdir / "text.txt").write_text(text, encoding="utf-8")
    header = {
        "type": "huaben", "version": 1, "title": "synthetic-voice-library",
        "language": "zh", "text": "text.txt", "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    rows = [header]
    if multi_voice:
        offset = 0
        for index, (line, role) in enumerate(zip(lines, roles)):
            stop = offset + len(line)
            if index == 0:
                row = {"type": "chapter", "id": "c001", "text": line, "level": 1, "src": [offset, stop]}
            else:
                row = {"type": "dialogue", "id": "c001.p" + str(index), "text": line, "say": line,
                       "speaker": role, "src": [offset, stop]}
            rows.append(row)
            offset = stop
    else:
        rows.extend([
            {"type": "chapter", "id": "c001", "text": title, "level": 1, "src": [0, len(title)]},
            {"type": "dialogue", "id": "c001.p1", "text": lines[1], "say": lines[1],
             "speaker": "synthetic_character", "src": [len(title) + 1, len(title) + 1 + len(lines[1])]},
        ])
    (workdir / "script.huaben.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    character_roles = {role: {"voice": voice_refs[role]} for role in set(roles) if role != "narrator"}
    (workdir / "cast.json").write_text(json.dumps({"narrator": {"voice": voice_refs["narrator"]},
                                                     "characters": character_roles}), encoding="utf-8")
    reads = []
    original_vector = library.Voice.vector

    def trace(voice):
        path = voice.vector_path
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        array = original_vector(voice)
        reads.append({"ref": voice.ref, "sha256": actual, "shape": list(array.shape)})
        persist("vector-reads.json", reads)
        if voice.ref in expected_vectors and actual != expected_vectors[voice.ref]:
            raise RuntimeError("unexpected candidate vector")
        return array

    library.Voice.vector = trace
    batch_state = {"remaining": 0}
    if multi_voice:
        import huashuo.synth as synthmod
        original_synthesize = synthmod.synthesize
        observed = []

        def controlled_synthesize(units, engine, workdir, language, *args, **kwargs):
            observed.extend({"text": unit.page_text, "spoken_text": unit.text, "voice": unit.voice} for unit in units)
            persist("plan-observed.json", observed)
            expected_chars = [(char, voice_refs[role]) for line, role in zip(lines, roles) for char in line if not char.isspace()]
            actual_chars = [(char, item["voice"]) for item in observed for char in item["text"] if not char.isspace()]
            if expected_chars != actual_chars:
                raise RuntimeError("planned text/voice sequence differs from the request")
            keys = synthmod.unit_keys(units, engine, language)
            missing = [index for index, key in enumerate(keys) if synthmod.cached_ok(workdir, key) is None]
            selected = missing[:5]
            persist("batch-before.json", {"total": len(units), "cached": len(units) - len(missing), "selected_indices": selected})
            if selected:
                result = original_synthesize([units[index] for index in selected], engine, workdir, language, *args, **kwargs)
            else:
                result = original_synthesize(units, engine, workdir, language, *args, **kwargs)
            remaining = [index for index, key in enumerate(keys) if synthmod.cached_ok(workdir, key) is None]
            batch_state["remaining"] = len(remaining)
            persist("batch-after.json", {"total": len(units), "cached": len(units) - len(remaining), "remaining": len(remaining)})
            if remaining:
                return result
            for ref in {unit.voice for unit in units}:
                engine.check_voice(ref)
            return result

        synthmod.synthesize = controlled_synthesize
    times = {}
    returns = {}
    with (stage / "huashuo.log").open("w", encoding="utf-8") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        commands = ("check", "synth", "package")
        for command in commands:
            persist("phase-state.json", {"phase": command, "status": "running"})
            started = time.monotonic()
            kwargs = {"output": stage / "synthetic-result.mp3"} if command == "package" else {}
            result = wrapper.run_isolated(stage, stage / "voices", stage / "book.txt", workdir, command, **kwargs)
            times[command] = round(time.monotonic() - started, 3)
            returns[command] = result
            if result not in (None, 0):
                persist("phase-state.json", {"phase": command, "status": "failed", "return": result})
                raise RuntimeError(command + " failed")
            if multi_voice and command == "synth" and batch_state["remaining"] > 0:
                break
            cache = {}
            if command == "synth":
                for item in sorted((workdir / "cache/units").glob("*")):
                    if item.is_file():
                        cache[item.name] = hashlib.sha256(item.read_bytes()).hexdigest()
                persist("synth-cache-hashes.json", cache)
            persist("phase-" + command + ".json", {"phase": command, "status": "complete", "return": result, "seconds": times[command]})
            persist("phase-state.json", {"phase": command, "status": "complete"})
    pending = multi_voice and batch_state["remaining"] > 0
    read_refs = {row["ref"] for row in reads}
    if (not pending and set(expected_vectors) - read_refs) or any(
        row["ref"] in expected_vectors and (row["sha256"] != expected_vectors[row["ref"]] or row["shape"] != [2048])
        for row in reads
    ):
        raise RuntimeError("candidate vector load did not match the configured inventory")
    audio = stage / "synthetic-result.mp3"
    audio_hash = hashlib.sha256(audio.read_bytes()).hexdigest() if audio.is_file() else None
    report = {
        "status": "batch_pending" if pending else "complete", "stage": str(stage),
        "voice_refs": voice_refs, "vector_sha256s": expected_vectors,
        "vector_sha256": next(iter(expected_vectors.values())) if len(expected_vectors) == 1 else None,
        "vector_reads": reads, "audio_sha256": audio_hash, "audio_bytes": audio.stat().st_size if audio_hash else 0,
        "times": times, "returns": returns, "model_inventory_sha256": model_hash,
        "network": "sandboxed-offline", "human_listening_reviewed": False,
    }
    (root / "render-result.json").write_text(json.dumps(report), encoding="utf-8")
    print(json.dumps(report), flush=True)
except BaseException as exc:
    (root / "render-unknown.json").write_text(json.dumps({"stage": str(stage), "error": str(exc), "no_retry": True}))
    raise
