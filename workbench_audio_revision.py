"""Source-bound, immutable paragraph audio revisions for the private workbench.

The caller supplies a fresh trusted context and an injected renderer. This
module never discovers books, voices, or renderers on its own.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from delivery_policy import validate_override


_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _file(path, label):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} missing or redirected")
    return path.resolve(strict=True)


def _read_pinned(path, digest, label):
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise ValueError(f"{label} digest invalid")
    path = _file(path, label)
    raw = path.read_bytes()
    if _sha(raw) != digest:
        raise ValueError(f"{label} changed")
    return path, raw


def _native_receipt(manifest, manifest_path, *, source_sha, stage_sha, voice_hashes, wav_sha, label):
    if manifest.get("renderer_kind") != "native_huashuo_chapter_v1":
        raise ValueError(f"{label} renderer kind invalid")
    receipt_path = manifest.get("native_receipt_path")
    receipt_path, receipt_raw = _read_pinned(receipt_path, manifest.get("native_receipt_sha256"),
                                              f"{label} native receipt")
    if receipt_path.parent != manifest_path.parent:
        raise ValueError(f"{label} native receipt escaped render directory")
    receipt = json.loads(receipt_raw)
    if (receipt.get("kind") != "native_huashuo_chapter_v1"
            or receipt.get("human_reviewed") is not False
            or receipt.get("source_sha256") != source_sha
            or receipt.get("source_document_sha256") != stage_sha
            or receipt.get("text_sha256") != source_sha
            or receipt.get("voice_hashes") != voice_hashes
            or receipt.get("wav_sha256") != wav_sha):
        raise ValueError(f"{label} native receipt source/voice/audio binding mismatch")
    return receipt


def _context(context):
    if not isinstance(context, dict):
        raise ValueError("trusted revision context missing")
    book = context.get("book_id"); chapter = context.get("chapter_id")
    if not isinstance(book, str) or not _ID.fullmatch(book) or not isinstance(chapter, str) or not _ID.fullmatch(chapter):
        raise ValueError("revision book/chapter identity invalid")
    source = context.get("source_sha256")
    if not isinstance(source, str) or not _HASH.fullmatch(source):
        raise ValueError("revision source digest invalid")
    stage_path, stage_raw = _read_pinned(context.get("normalized_stage_path"),
                                         context.get("normalized_stage_sha256"), "normalized stage")
    manifest_path, manifest_raw = _read_pinned(context.get("base_manifest_path"),
                                               context.get("base_manifest_sha256"), "base manifest")
    wav_path, wav_raw = _read_pinned(context.get("base_wav_path"),
                                     context.get("base_wav_sha256"), "base chapter audio")
    normalized = json.loads(stage_raw)
    manifest = json.loads(manifest_raw)
    if (normalized.get("original_stage_sha256") is None
            or not isinstance(normalized.get("chapters"), list)
            or len(normalized["chapters"]) != 1
            or normalized["chapters"][0].get("id") != chapter
            or normalized.get("audit", {}).get("source_text_sha256") != source
            or _sha("".join(row.get("text", "") for row in normalized["chapters"][0].get("segments", [])).encode("utf-8")) != source):
        raise ValueError("normalized stage does not match source identity")
    if (manifest.get("book") != book or manifest.get("source_sha256") != source
            or manifest.get("status") != "complete" or len(manifest.get("chapters", [])) != 1):
        raise ValueError("base manifest identity mismatch")
    if manifest.get("normalized_stage_sha256") != _sha(stage_raw):
        raise ValueError("base manifest does not bind the supplied normalized stage")
    entry = manifest["chapters"][0]
    if (entry.get("id") != chapter or entry.get("status") != "complete"
            or entry.get("sha256") != _sha(wav_raw)
            or Path(entry.get("path", "")).resolve(strict=True) != wav_path):
        raise ValueError("base manifest audio binding mismatch")

    bindings = context.get("voice_bindings")
    if not isinstance(bindings, dict) or not bindings:
        raise ValueError("source voice bindings missing")
    segments = normalized["chapters"][0].get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("normalized paragraph list missing")
    roles = {row.get("speaker_id") for row in segments}
    if roles != set(bindings):
        raise ValueError("source voice bindings do not exactly cover normalized roles")
    voice_hashes = {}
    for role, binding in bindings.items():
        record = binding.get("record") if isinstance(binding, dict) and isinstance(binding.get("record"), dict) else binding
        if not isinstance(record, dict):
            raise ValueError("source voice record invalid")
        request = record.get("request")
        directory = record.get("directory")
        expected = request.get("vector_sha256") if isinstance(request, dict) else None
        if not isinstance(expected, str) or not _HASH.fullmatch(expected):
            raise ValueError("source voice vector digest invalid")
        vector_path = _file(Path(directory) / "speaker-vector.npy", "source voice vector")
        if _sha(vector_path.read_bytes()) != expected:
            raise ValueError("source voice vector changed")
        voice_hashes[role] = expected

    _native_receipt(manifest, manifest_path, source_sha=source,
                    stage_sha=_sha(stage_raw), voice_hashes=voice_hashes,
                    wav_sha=_sha(wav_raw), label="base")

    revision_root_value = context.get("revision_root")
    if not isinstance(revision_root_value, (str, os.PathLike)) or not str(revision_root_value):
        raise ValueError("revision root missing")
    revision_root = Path(revision_root_value)
    runtime_root_value = context.get("runtime_root")
    if not isinstance(runtime_root_value, (str, os.PathLike)) or not str(runtime_root_value):
        raise ValueError("trusted runtime root missing")
    runtime_root = Path(runtime_root_value)
    expected_revision_root = runtime_root / "audio-revisions" / book / chapter
    if revision_root.absolute() != expected_revision_root.absolute():
        raise ValueError("revision root is outside its expected book/chapter partition")
    for path in (runtime_root, runtime_root / "audio-revisions",
                 runtime_root / "audio-revisions" / book, revision_root):
        if path.exists() and path.is_symlink():
            raise ValueError("revision root redirected")
    return {"book_id": book, "chapter_id": chapter, "source_sha256": source,
            "normalized_stage_path": stage_path, "normalized_stage_sha256": _sha(stage_raw),
            "normalized_raw": stage_raw, "normalized": normalized,
            "base_manifest_path": manifest_path, "base_manifest_sha256": _sha(manifest_raw),
            "base_wav_path": wav_path, "base_wav_sha256": _sha(wav_raw),
            "voice_hashes": voice_hashes,
            "voice_fingerprint": _sha(_canonical(voice_hashes)),
            "voice_bindings": bindings,
            "revision_root": revision_root,
            "runtime_root": runtime_root}


def _paragraphs(verified):
    occurrences = {}
    rows = verified["normalized"]["chapters"][0]["segments"]
    out = []
    for index, segment in enumerate(rows):
        role, text = segment.get("speaker_id"), segment.get("text")
        if not isinstance(role, str) or not role or not isinstance(text, str) or not text:
            raise ValueError("normalized segment invalid")
        text_sha = _sha(text.encode("utf-8"))
        key = (role, text_sha)
        occurrence = occurrences.get(key, 0)
        occurrences[key] = occurrence + 1
        identity = {"normalized_stage_sha256": verified["normalized_stage_sha256"],
                    "index": index, "speaker_id": role,
                    "text_sha256": text_sha, "occurrence": occurrence}
        paragraph_id = _sha(_canonical(identity))
        out.append({"id": paragraph_id, "index": index, "speaker_id": role,
                    "text_sha256": text_sha, "occurrence": occurrence, "text": text})
    return out


def _revision_voice_bindings(verified, stage):
    chapters = stage.get("chapters") if isinstance(stage, dict) else None
    if not isinstance(chapters, list) or len(chapters) != 1:
        raise ValueError("revision stage chapter structure invalid")
    if chapters[0].get("id") != verified["chapter_id"]:
        raise ValueError("revision stage chapter identity changed")
    segments = chapters[0].get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("revision stage paragraphs missing")
    if any(not isinstance(row, dict) or not isinstance(row.get("speaker_id"), str)
           or not row["speaker_id"] for row in segments):
        raise ValueError("revision stage speaker identity invalid")
    roles = {row["speaker_id"] for row in segments}
    if not roles or not roles <= set(verified["voice_bindings"]):
        raise ValueError("revision stage uses a speaker without a verified source binding")
    bindings = {role: verified["voice_bindings"][role] for role in roles}
    hashes = {role: verified["voice_hashes"][role] for role in roles}
    return bindings, hashes


def _expected_native_stage(stage_path, verified, role_bindings=None):
    """Mirror the renderer's prepare/normalize path without rendering audio."""
    import real_book_renderer as renderer
    bindings = {}
    for role, binding in (role_bindings or verified["voice_bindings"]).items():
        record = binding.get("record") if isinstance(binding, dict) and isinstance(binding.get("record"), dict) else binding
        key = record.get("book_role_key") if isinstance(record, dict) else None
        if not isinstance(key, str):
            raise ValueError("source voice binding key missing")
        bindings[role] = {"console_key": key, "record": record}
    document, _assignments = renderer.prepare(stage_path, verified["book_id"], verified["chapter_id"],
                                               verified["source_sha256"], bindings)
    normalized = renderer._native_normalized_stage(document)
    return _sha(_canonical(normalized))


def list_paragraphs(context):
    """List source paragraphs with IDs bound to the verified normalized stage."""
    return _paragraphs(_context(context))


@contextmanager
def _book_lock(runtime_root, book_id):
    if runtime_root is None:
        raise ValueError("trusted runtime_root required for revision locking")
    import real_book_auto_voice as auto
    with auto._selection_lock(Path(runtime_root) / "audio-revision-locks", book_id):
        yield


def _revision_dir(verified, revision_id):
    if not isinstance(revision_id, str) or not _ID.fullmatch(revision_id):
        raise ValueError("revision ID invalid")
    root = verified["revision_root"]
    root.mkdir(parents=True, exist_ok=True)
    target = root / revision_id
    if target.exists() or target.is_symlink():
        raise ValueError("revision ID already exists")
    return target


def _verify_render_receipt(receipt, verified, expected_original_stage_sha256,
                           expected_normalized_stage_sha256, expected_voice_hashes=None):
    if not isinstance(receipt, dict):
        raise ValueError("renderer did not return a verified receipt")
    wav_path, wav_raw = _read_pinned(receipt.get("chapter_wav"), receipt.get("wav_sha256"), "revision WAV")
    manifest_path, manifest_raw = _read_pinned(receipt.get("manifest_path"), receipt.get("manifest_sha256"), "revision manifest")
    manifest = json.loads(manifest_raw)
    entry = manifest.get("chapters", [{}])[0]
    if (receipt.get("source_sha256") != verified["source_sha256"]
            or manifest.get("book") != verified["book_id"]
            or manifest.get("source_sha256") != verified["source_sha256"]
            or manifest.get("original_stage_sha256") != expected_original_stage_sha256
            or manifest.get("normalized_stage_sha256") != expected_normalized_stage_sha256
            or len(manifest.get("chapters", [])) != 1
            or entry.get("id") != verified["chapter_id"]
            or entry.get("source_sha256") != verified["source_sha256"]
            or entry.get("text_sha256") != verified["source_sha256"]
            or entry.get("sha256") != _sha(wav_raw)
            or Path(entry.get("path", "")).resolve(strict=True) != wav_path):
        raise ValueError("revision render receipt source/audio binding mismatch")
    _native_receipt(manifest, manifest_path, source_sha=verified["source_sha256"],
                    stage_sha=expected_normalized_stage_sha256,
                    voice_hashes=expected_voice_hashes or verified["voice_hashes"],
                    wav_sha=_sha(wav_raw), label="revision")
    return {**receipt, "chapter_wav": str(wav_path), "manifest_path": str(manifest_path),
            "wav_sha256": _sha(wav_raw), "manifest_sha256": _sha(manifest_raw)}


def generate(context, paragraph_id, instruction, render, *, revision_id=None, speaker_id=None):
    """Render one isolated paragraph with an optional instruction and/or known speaker override."""
    if not callable(render):
        raise ValueError("injected local renderer required")
    if speaker_id is not None and (not isinstance(speaker_id, str) or not speaker_id):
        raise ValueError("speaker override must name an existing source role")
    if instruction is not None and not isinstance(instruction, str):
        raise ValueError("instruction must be text or None")
    has_instruction = isinstance(instruction, str) and bool(instruction.strip())
    normalized_instruction = instruction.strip() if has_instruction else None
    if normalized_instruction is not None:
        if len(instruction) > 500 or not instruction.isprintable():
            raise ValueError("instruction must be 1–500 printable characters")
        validate_override(normalized_instruction)
    if normalized_instruction is None and speaker_id is None:
        raise ValueError("instruction must be 1–500 printable characters")
    with _book_lock(Path(context["runtime_root"]) if context.get("runtime_root") else None,
                    context.get("book_id")):
        verified = _context(context)
        paragraph = next((p for p in _paragraphs(verified) if p["id"] == paragraph_id), None)
        if paragraph is None:
            raise ValueError("paragraph ID is stale or unknown")
        if speaker_id is not None and speaker_id not in verified["voice_bindings"]:
            raise ValueError("speaker override is not present in the verified source bindings")
        if (normalized_instruction is None and speaker_id == paragraph["speaker_id"]):
            raise ValueError("speaker override and instruction produce no paragraph change")
        revision_id = revision_id or uuid.uuid4().hex
        target = _revision_dir(verified, revision_id)
        target.mkdir(parents=True, exist_ok=False)
        stage_path = target / "normalized-stage.json"
        derived = json.loads(verified["normalized_raw"])
        segment = derived["chapters"][0]["segments"][paragraph["index"]]
        if normalized_instruction is not None:
            segment["instruct"] = normalized_instruction
        if speaker_id is not None:
            segment["speaker_id"] = speaker_id
        if ("".join(row["text"] for row in derived["chapters"][0]["segments"]) !=
                "".join(row["text"] for row in verified["normalized"]["chapters"][0]["segments"])):
            raise ValueError("revision changed source text")
        stage_raw = _canonical(derived)
        with stage_path.open("xb") as stream:
            stream.write(stage_raw); stream.flush(); os.fsync(stream.fileno())
        output_dir = target / "render-output"
        output_dir.mkdir()
        revision_bindings, revision_voice_hashes = _revision_voice_bindings(verified, derived)
        expected_native_sha = _expected_native_stage(stage_path, verified, revision_bindings)
        derived_stage_sha = _sha(stage_raw)
        receipt = _verify_render_receipt(render(context, stage_path, output_dir), verified,
                                         derived_stage_sha, expected_native_sha, revision_voice_hashes)
        after_render = _context(context)
        context_pins = ("source_sha256", "normalized_stage_sha256", "base_manifest_sha256",
                        "base_wav_sha256", "voice_fingerprint")
        if any(after_render[key] != verified[key] for key in context_pins):
            raise ValueError("verified source/base/voices changed during revision rendering")
        record = {"version": 2, "status": "candidate", "revision_id": revision_id,
                  "book_id": verified["book_id"], "chapter_id": verified["chapter_id"],
                  "source_sha256": verified["source_sha256"],
                  "normalized_stage_sha256": verified["normalized_stage_sha256"],
                  "base_manifest_sha256": verified["base_manifest_sha256"],
                  "base_wav_sha256": verified["base_wav_sha256"],
                  "voice_fingerprint": verified["voice_fingerprint"],
                  "revision_voice_hashes": revision_voice_hashes,
                  "revision_voice_fingerprint": _sha(_canonical(revision_voice_hashes)),
                  "paragraph_id": paragraph_id, "paragraph_index": paragraph["index"],
                  "speaker_id": paragraph["speaker_id"], "speaker_override_id": speaker_id,
                  "paragraph_text_sha256": paragraph["text_sha256"],
                  "instruction": normalized_instruction, "derived_stage_sha256": derived_stage_sha,
                  "derived_native_stage_sha256": expected_native_sha,
                  "receipt": receipt}
        record["record_sha256"] = _sha(_canonical(record))
        raw = _canonical(record)
        with (target / "revision.json").open("xb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        return record


def _load_revision(verified, revision_id):
    if not isinstance(revision_id, str) or not _ID.fullmatch(revision_id):
        raise ValueError("revision ID invalid")
    root = verified["revision_root"]
    path = _file(root / revision_id / "revision.json", "revision record")
    raw = path.read_bytes(); record = json.loads(raw)
    expected = {"book_id": verified["book_id"], "chapter_id": verified["chapter_id"],
                "source_sha256": verified["source_sha256"],
                "normalized_stage_sha256": verified["normalized_stage_sha256"],
                "base_manifest_sha256": verified["base_manifest_sha256"],
                "base_wav_sha256": verified["base_wav_sha256"],
                "voice_fingerprint": verified["voice_fingerprint"]}
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("revision is stale for the current source/base/voices")
    if record.get("status") != "candidate" or record.get("revision_id") != revision_id:
        raise ValueError("revision record state invalid")
    unsigned = dict(record); claimed = unsigned.pop("record_sha256", None)
    if claimed != _sha(_canonical(unsigned)):
        raise ValueError("revision record digest mismatch")
    expected_voice_hashes = verified["voice_hashes"]
    record_version = record.get("version", 1)
    if type(record_version) is not int or record_version not in (1, 2):
        raise ValueError("revision record version unsupported")
    if record_version == 2:
        paragraph = next((row for row in _paragraphs(verified)
                          if row["id"] == record.get("paragraph_id")), None)
        if (paragraph is None or type(record.get("paragraph_index")) is not int
                or paragraph["index"] != record["paragraph_index"]
                or paragraph["speaker_id"] != record.get("speaker_id")
                or paragraph["text_sha256"] != record.get("paragraph_text_sha256")):
            raise ValueError("revision paragraph identity changed")
        override = record.get("speaker_override_id")
        if override is not None and (not isinstance(override, str)
                                     or override not in verified["voice_bindings"]):
            raise ValueError("revision speaker override is not source-bound")
        instruction = record.get("instruction")
        if instruction is not None:
            validate_override(instruction)
        if instruction is None and (override is None or override == paragraph["speaker_id"]):
            raise ValueError("revision has no effective paragraph change")
        stage_path, stage_raw = _read_pinned(path.parent / "normalized-stage.json",
                                             record.get("derived_stage_sha256"), "revision stage")
        expected_stage = json.loads(verified["normalized_raw"])
        segment = expected_stage["chapters"][0]["segments"][paragraph["index"]]
        if instruction is not None:
            segment["instruct"] = instruction
        if override is not None:
            segment["speaker_id"] = override
        if _canonical(expected_stage) != stage_raw:
            raise ValueError("revision stage differs from its bound paragraph override")
        revision_bindings, expected_voice_hashes = _revision_voice_bindings(verified, expected_stage)
        if (record.get("revision_voice_hashes") != expected_voice_hashes
                or record.get("revision_voice_fingerprint") != _sha(_canonical(expected_voice_hashes))):
            raise ValueError("revision output voice hashes do not match its stage")
        expected_native_sha = _expected_native_stage(stage_path, verified, revision_bindings)
        if record.get("derived_native_stage_sha256") != expected_native_sha:
            raise ValueError("revision native stage digest mismatch")
    receipt = _verify_render_receipt(record.get("receipt"), verified,
                                     record.get("derived_stage_sha256"),
                                     record.get("derived_native_stage_sha256"),
                                     expected_voice_hashes)
    return record, receipt


def verify_candidate(context, revision_id):
    """Reload and reverify an immutable candidate before audition or adoption."""
    if not isinstance(context, dict):
        raise ValueError("trusted revision context missing")
    with _book_lock(Path(context["runtime_root"]) if context.get("runtime_root") else None,
                    context.get("book_id")):
        verified = _context(context)
        record, receipt = _load_revision(verified, revision_id)
        return {**record, "receipt": receipt}


def adopt(context, revision_id):
    """Explicitly select a verified candidate without changing its history."""
    with _book_lock(Path(context["runtime_root"]) if context.get("runtime_root") else None,
                    context.get("book_id")):
        verified = _context(context)
        record, receipt = _load_revision(verified, revision_id)
        pointer = {"version": 1, "state": "revision", "book_id": verified["book_id"],
                   "chapter_id": verified["chapter_id"], "revision_id": revision_id,
                   "record_sha256": record["record_sha256"],
                   "source_sha256": verified["source_sha256"],
                   "normalized_stage_sha256": verified["normalized_stage_sha256"],
                   "base_manifest_sha256": verified["base_manifest_sha256"],
                   "base_wav_sha256": verified["base_wav_sha256"],
                   "voice_fingerprint": verified["voice_fingerprint"]}
        target = verified["revision_root"]
        target.mkdir(parents=True, exist_ok=True)
        current = target / "current-revision.json"
        temp = current.with_name(current.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("xb") as stream:
                stream.write(_canonical(pointer)); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, current)
        finally:
            if temp.exists(): temp.unlink()
        return {**pointer, **receipt}


def restore_base(context):
    """Atomically select the verified original render while retaining revisions."""
    with _book_lock(Path(context["runtime_root"]) if context.get("runtime_root") else None,
                    context.get("book_id")):
        verified = _context(context)
        pointer = {"version": 1, "state": "base", "book_id": verified["book_id"],
                   "chapter_id": verified["chapter_id"],
                   "source_sha256": verified["source_sha256"],
                   "normalized_stage_sha256": verified["normalized_stage_sha256"],
                   "base_manifest_sha256": verified["base_manifest_sha256"],
                   "base_wav_sha256": verified["base_wav_sha256"],
                   "voice_fingerprint": verified["voice_fingerprint"]}
        target = verified["revision_root"]
        target.mkdir(parents=True, exist_ok=True)
        current = target / "current-revision.json"
        temp = current.with_name(current.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("xb") as stream:
                stream.write(_canonical(pointer)); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, current)
        finally:
            if temp.exists(): temp.unlink()
        return {**pointer, "chapter_wav": str(verified["base_wav_path"]),
                "manifest_path": str(verified["base_manifest_path"]),
                "wav_sha256": verified["base_wav_sha256"],
                "manifest_sha256": verified["base_manifest_sha256"]}


def resolve_adopted(context):
    """Return a reverified adopted receipt; only absence returns None."""
    verified = _context(context)
    current = verified["revision_root"] / "current-revision.json"
    if not current.exists() and not current.is_symlink():
        return None
    if current.is_symlink() or not current.is_file():
        raise ValueError("current revision pointer redirected")
    pointer = json.loads(current.read_bytes())
    if not isinstance(pointer, dict) or type(pointer.get("version")) is not int or pointer["version"] != 1:
        raise ValueError("current revision pointer schema/version invalid")
    state = pointer.get("state", "revision")
    base_fields = {"version", "state", "book_id", "chapter_id", "source_sha256",
                   "normalized_stage_sha256", "base_manifest_sha256", "base_wav_sha256",
                   "voice_fingerprint"}
    if state == "base":
        if (set(pointer) != base_fields or pointer.get("book_id") != verified["book_id"]
                or pointer.get("chapter_id") != verified["chapter_id"]
                or any(not isinstance(pointer.get(key), str) or not _HASH.fullmatch(pointer[key])
                       for key in ("source_sha256", "normalized_stage_sha256", "base_manifest_sha256",
                                   "base_wav_sha256", "voice_fingerprint"))):
            raise ValueError("base selection pointer schema or identity invalid")
        return None
    if state != "revision":
        raise ValueError("current revision pointer state invalid")
    revision_fields = base_fields | {"revision_id", "record_sha256"}
    legacy_revision_fields = revision_fields - {"state"}
    if set(pointer) not in (revision_fields, legacy_revision_fields):
        raise ValueError("revision selection pointer schema invalid")
    expected = {"book_id": verified["book_id"], "chapter_id": verified["chapter_id"],
                "source_sha256": verified["source_sha256"],
                "normalized_stage_sha256": verified["normalized_stage_sha256"],
                "base_manifest_sha256": verified["base_manifest_sha256"],
                "base_wav_sha256": verified["base_wav_sha256"],
                "voice_fingerprint": verified["voice_fingerprint"]}
    if any(pointer.get(key) != value for key, value in expected.items()):
        raise ValueError("adopted revision is stale for the current source/base/voices")
    record, receipt = _load_revision(verified, pointer.get("revision_id"))
    if pointer.get("record_sha256") != record.get("record_sha256"):
        raise ValueError("current revision pointer does not match immutable history")
    return {**pointer, **receipt}
