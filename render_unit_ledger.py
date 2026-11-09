"""Offline, append-only ledger for source-preserving rendered audio units.

This module does not invoke Huashuo, SSH, subprocesses, or model APIs. Callers
provide WAV files and per-unit verification evidence after rendering. A unit
reservation without a verified completion is permanently unknown in that
ledger; it is never dispatched or retried by this module.

Verification evidence is an adapter attestation. Hashing and rechecking its
contents here proves only local consistency; only a trusted renderer/monitor
adapter can establish remote source, model, voice, or runtime provenance.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import wave
from pathlib import Path
from typing import Any


RUNTIME_MIN_FREE_PERCENT = 30
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SPEAKER = re.compile(r"[A-Za-z0-9_.:-]{1,120}\Z")
_ALLOWED_UNKNOWN_REASONS = frozenset({
    "renderer_failed", "monitor_failed", "transport_unknown",
    "audio_verification_failed", "operator_stopped",
})
_PUNCTUATION = frozenset("。！？!?；;，,、\n")


class LedgerError(ValueError):
    """A ledger, plan, output, or evidence invariant failed."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            count += len(block)
    return digest.hexdigest(), count


def _write_new(path: Path, raw: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _write_json_new(path: Path, value: Any) -> None:
    _write_new(path, _canonical(value))


def _read_json(path: Path) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise LedgerError("ledger record missing or unsafe")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise LedgerError("ledger record must be an object")
    return value


def _validate_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise LedgerError(f"invalid {label} hash")
    return value


def _cut_end(text: str, limit: int) -> int:
    """Choose a deterministic source-preserving boundary no later than limit."""
    if len(text) <= limit:
        return len(text)
    lower = max(1, limit // 2)
    punctuation = max((i + 1 for i, ch in enumerate(text[:limit])
                       if ch in _PUNCTUATION and i + 1 >= lower), default=0)
    if punctuation:
        return punctuation
    whitespace = max((i + 1 for i, ch in enumerate(text[:limit])
                      if ch.isspace() and i + 1 >= lower), default=0)
    if whitespace:
        return whitespace
    return limit


def plan_units(*, book_id: str, chapter_id: str, source_text_sha256: str,
               segments: list[dict[str, str]], voice_vector_sha256: dict[str, str],
               runtime_monitor_sha256: str, max_codepoints: int = 96) -> dict[str, Any]:
    """Split ordered speaker/text segments without changing any source text.

    Segments are never joined across speaker boundaries. Within a segment,
    pieces prefer punctuation and whitespace near the size limit; hard splits
    preserve exact code points when no useful boundary exists. This planner
    does not claim that a piece equals one Huashuo internal synthesis unit.
    """
    if (not isinstance(book_id, str) or not book_id
            or not isinstance(chapter_id, str) or not chapter_id
            or type(max_codepoints) is not int or not 1 <= max_codepoints <= 500):
        raise LedgerError("invalid book, chapter, or unit size")
    source_text_sha256 = _validate_hash(source_text_sha256, "source")
    runtime_monitor_sha256 = _validate_hash(runtime_monitor_sha256, "runtime monitor")
    if not isinstance(segments, list) or not segments:
        raise LedgerError("source segments are required")
    if not isinstance(voice_vector_sha256, dict) or not voice_vector_sha256:
        raise LedgerError("voice vector pins are required")
    for speaker, digest in voice_vector_sha256.items():
        if not isinstance(speaker, str) or not _SPEAKER.fullmatch(speaker):
            raise LedgerError("invalid speaker identity")
        _validate_hash(digest, f"voice vector for {speaker}")

    units: list[dict[str, Any]] = []
    source_parts: list[str] = []
    referenced_speakers: set[str] = set()
    for segment_index, segment in enumerate(segments):
        if (not isinstance(segment, dict) or set(segment) != {"speaker_id", "text"}
                or not isinstance(segment["speaker_id"], str)
                or not _SPEAKER.fullmatch(segment["speaker_id"])
                or not isinstance(segment["text"], str) or not segment["text"]):
            raise LedgerError("each source segment needs a speaker and nonempty text")
        speaker = segment["speaker_id"]
        if speaker not in voice_vector_sha256:
            raise LedgerError("speaker is missing a pinned voice vector")
        text = segment["text"]
        source_parts.append(text)
        referenced_speakers.add(speaker)
        content_end = len(text.rstrip())
        trailing = text[content_end:]
        if content_end == 0:
            raise LedgerError("whitespace-only segment cannot be rendered safely")
        pieces: list[tuple[int, int, str]] = []
        offset = 0
        while offset < content_end:
            end = _cut_end(text[offset:content_end], max_codepoints) + offset
            piece = text[offset:end]
            if not piece.strip():
                raise LedgerError("whitespace-only unit cannot be rendered safely")
            pieces.append((offset, end, piece))
            offset = end
        if trailing:
            start, end, piece = pieces[-1]
            if len(piece) + len(trailing) > max_codepoints:
                raise LedgerError("trailing whitespace cannot fit within the unit size")
            pieces[-1] = (start, end + len(trailing), piece + trailing)
        for offset, end, piece in pieces:
            text_sha = _sha(piece.encode("utf-8"))
            unit_identity = {"chapter_id": chapter_id, "segment_index": segment_index,
                             "start_offset": offset, "end_offset": end,
                             "speaker_id": speaker, "text_sha256": text_sha,
                             "voice_vector_sha256": voice_vector_sha256[speaker]}
            unit_id = _sha(_canonical(unit_identity))[:32]
            units.append({"unit_id": unit_id, "ordinal": len(units),
                          "segment_index": segment_index, "start_offset": offset,
                          "end_offset": end, "speaker_id": speaker, "text": piece,
                          "text_sha256": text_sha,
                          "voice_vector_sha256": voice_vector_sha256[speaker]})
            offset = end
    if _sha("".join(source_parts).encode("utf-8")) != source_text_sha256:
        raise LedgerError("source segments do not match the pinned chapter text")
    if referenced_speakers != set(voice_vector_sha256):
        # Plans pin exactly the assets that will be used, avoiding unused or
        # accidentally substituted voices in later calls.
        raise LedgerError("voice vector pins do not match source speakers")
    plan = {"version": 1, "book_id": book_id, "chapter_id": chapter_id,
            "source_text_sha256": source_text_sha256,
            "voice_vector_sha256": dict(sorted(voice_vector_sha256.items())),
            "runtime_monitor_sha256": runtime_monitor_sha256,
            "runtime_min_free_percent": RUNTIME_MIN_FREE_PERCENT,
            "max_codepoints": max_codepoints, "units": units,
            "human_reviewed": False}
    plan["plan_sha256"] = _sha(_canonical(plan))
    return plan


def plan_units_preserving_prefix(*, book_id: str, chapter_id: str,
                                 source_text_sha256: str,
                                 segments: list[dict[str, str]],
                                 voice_vector_sha256: dict[str, str],
                                 runtime_monitor_sha256: str,
                                 preserved_prefix_units: list[dict[str, Any]],
                                 max_codepoints: int = 48,
                                 max_new_unit_codepoints: int = 24) -> dict[str, Any]:
    """Replan only the unfinished suffix while preserving exact completed units.

    The regular plan limit remains the maximum accepted by the ledger. New
    suffix units use the smaller limit; the already-completed prefix remains
    byte-for-byte identical to its source plan so its verified WAV/evidence can
    be carried into a separate ledger without rendering it again.
    """
    if (not isinstance(preserved_prefix_units, list) or not preserved_prefix_units
            or type(max_new_unit_codepoints) is not int
            or not 1 <= max_new_unit_codepoints <= max_codepoints):
        raise LedgerError("a nonempty verified prefix and valid suffix unit limit are required")
    baseline = plan_units(
        book_id=book_id, chapter_id=chapter_id,
        source_text_sha256=source_text_sha256, segments=segments,
        voice_vector_sha256=voice_vector_sha256,
        runtime_monitor_sha256=runtime_monitor_sha256,
        max_codepoints=max_codepoints)
    if (len(preserved_prefix_units) > len(baseline["units"])
            or _canonical(preserved_prefix_units)
               != _canonical(baseline["units"][:len(preserved_prefix_units)])):
        raise LedgerError("completed units are not an exact prefix of the frozen source plan")

    last = preserved_prefix_units[-1]
    last_segment = last["segment_index"]
    last_end = last["end_offset"]
    units = [dict(unit) for unit in preserved_prefix_units]
    for segment_index, segment in enumerate(segments):
        text = segment["text"]
        if segment_index < last_segment:
            continue
        offset = last_end if segment_index == last_segment else 0
        content_end = len(text.rstrip())
        trailing = text[content_end:]
        if offset >= content_end:
            if segment_index == last_segment and offset != len(text):
                raise LedgerError("preserved prefix ends inside trailing whitespace")
            continue
        pieces: list[tuple[int, int, str]] = []
        while offset < content_end:
            end = _cut_end(text[offset:content_end], max_new_unit_codepoints) + offset
            piece = text[offset:end]
            if not piece.strip():
                raise LedgerError("short replan produced a whitespace-only unit")
            pieces.append((offset, end, piece))
            offset = end
        if trailing:
            if not pieces:
                raise LedgerError("trailing whitespace cannot form a short source unit")
            start, end, piece = pieces[-1]
            if len(piece) + len(trailing) <= max_new_unit_codepoints:
                pieces[-1] = (start, end + len(trailing), piece + trailing)
            else:
                tail_content = max_new_unit_codepoints - len(trailing)
                if tail_content < 1 or tail_content >= len(piece):
                    raise LedgerError("trailing whitespace cannot fit in the short replan limit")
                split_at = end - tail_content
                pieces[-1] = (start, split_at, text[start:split_at])
                pieces.append((split_at, len(text), text[split_at:content_end] + trailing))
        for start, end, piece in pieces:
            speaker = segment["speaker_id"]
            text_sha = _sha(piece.encode("utf-8"))
            identity = {"chapter_id": chapter_id, "segment_index": segment_index,
                        "start_offset": start, "end_offset": end,
                        "speaker_id": speaker, "text_sha256": text_sha,
                        "voice_vector_sha256": voice_vector_sha256[speaker]}
            units.append({"unit_id": _sha(_canonical(identity))[:32],
                          "ordinal": len(units), "segment_index": segment_index,
                          "start_offset": start, "end_offset": end,
                          "speaker_id": speaker, "text": piece,
                          "text_sha256": text_sha,
                          "voice_vector_sha256": voice_vector_sha256[speaker]})

    plan = dict(baseline, units=units)
    plan.pop("plan_sha256", None)
    plan["plan_sha256"] = _sha(_canonical(plan))
    _validate_plan(plan)
    if any(len(unit["text"]) > max_new_unit_codepoints
           for unit in plan["units"][len(preserved_prefix_units):]):
        raise LedgerError("short replan exceeded its suffix unit limit")
    validate_plan_segments(plan, segments)
    return plan


def validate_plan_segments(plan: dict[str, Any],
                           segments: list[dict[str, str]]) -> None:
    """Bind every custom plan unit to the exact original speaker segment span."""
    if not isinstance(segments, list) or not segments:
        raise LedgerError("source segments are required for plan validation")
    offsets = [0] * len(segments)
    for unit in plan.get("units", []):
        index = unit.get("segment_index")
        if type(index) is not int or not 0 <= index < len(segments):
            raise LedgerError("planned unit refers to an unknown source segment")
        segment = segments[index]
        start, end = unit["start_offset"], unit["end_offset"]
        if (unit["speaker_id"] != segment["speaker_id"]
                or start != offsets[index]
                or unit["text"] != segment["text"][start:end]):
            raise LedgerError("planned unit differs from its source speaker span")
        offsets[index] = end
    if offsets != [len(segment["text"]) for segment in segments]:
        raise LedgerError("planned units do not cover every source segment exactly")


def _validate_plan(plan: dict[str, Any]) -> None:
    fields = {"version", "book_id", "chapter_id", "source_text_sha256",
              "voice_vector_sha256", "runtime_monitor_sha256",
              "runtime_min_free_percent", "max_codepoints", "units",
              "human_reviewed", "plan_sha256"}
    if (not isinstance(plan, dict) or set(plan) != fields
            or type(plan.get("version")) is not int or plan.get("version") != 1
            or plan.get("human_reviewed") is not False
            or type(plan.get("runtime_min_free_percent")) is not int
            or plan.get("runtime_min_free_percent") != RUNTIME_MIN_FREE_PERCENT
            or type(plan.get("max_codepoints")) is not int
            or not 1 <= plan.get("max_codepoints") <= 500
            or not isinstance(plan.get("book_id"), str) or not plan["book_id"]
            or not isinstance(plan.get("chapter_id"), str) or not plan["chapter_id"]):
        raise LedgerError("invalid plan or runtime guard")
    expected = dict(plan)
    observed = expected.pop("plan_sha256", None)
    if observed != _sha(_canonical(expected)):
        raise LedgerError("plan hash mismatch")
    _validate_hash(plan.get("source_text_sha256"), "source")
    _validate_hash(plan.get("runtime_monitor_sha256"), "runtime monitor")
    voices = plan.get("voice_vector_sha256")
    if not isinstance(voices, dict) or not voices:
        raise LedgerError("plan voice vector pins are missing")
    for speaker, digest in voices.items():
        if not isinstance(speaker, str) or not _SPEAKER.fullmatch(speaker):
            raise LedgerError("plan speaker identity invalid")
        _validate_hash(digest, "voice vector")
    units = plan.get("units")
    if not isinstance(units, list) or not units:
        raise LedgerError("plan has no units")
    seen: set[str] = set()
    source: list[str] = []
    previous_segment = -1
    previous_end = 0
    previous_speaker: str | None = None
    for index, unit in enumerate(units):
        unit_fields = {"unit_id", "ordinal", "segment_index", "start_offset", "end_offset",
                       "speaker_id", "text", "text_sha256", "voice_vector_sha256"}
        if (not isinstance(unit, dict) or set(unit) != unit_fields
                or type(unit.get("ordinal")) is not int or unit.get("ordinal") != index
                or not isinstance(unit.get("unit_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", unit["unit_id"])
                or unit["unit_id"] in seen or not isinstance(unit.get("text"), str)
                or not unit["text"].strip() or len(unit["text"]) > plan["max_codepoints"]
                or type(unit.get("segment_index")) is not int
                or type(unit.get("start_offset")) is not int
                or type(unit.get("end_offset")) is not int
                or unit.get("start_offset", -1) < 0
                or unit.get("end_offset", 0) <= unit.get("start_offset", 0)
                or not isinstance(unit.get("speaker_id"), str)
                or not _SPEAKER.fullmatch(unit["speaker_id"])
                or unit.get("speaker_id") not in voices
                or unit.get("voice_vector_sha256") != voices[unit["speaker_id"]]
                or unit.get("text_sha256") != _sha(unit["text"].encode("utf-8"))
                or unit["end_offset"] - unit["start_offset"] != len(unit["text"])):
            raise LedgerError("planned unit identity or text hash mismatch")
        segment_index = unit["segment_index"]
        if segment_index == previous_segment:
            if unit["start_offset"] != previous_end or unit["speaker_id"] != previous_speaker:
                raise LedgerError("unit offsets or speaker order are discontinuous")
        elif segment_index == previous_segment + 1:
            if unit["start_offset"] != 0:
                raise LedgerError("new source segment does not start at offset zero")
        elif previous_segment >= 0 or segment_index != 0 or unit["start_offset"] != 0:
            raise LedgerError("source segment order contains a gap")
        identity = {"chapter_id": plan["chapter_id"], "segment_index": segment_index,
                    "start_offset": unit["start_offset"], "end_offset": unit["end_offset"],
                    "speaker_id": unit["speaker_id"], "text_sha256": unit["text_sha256"],
                    "voice_vector_sha256": unit["voice_vector_sha256"]}
        if unit["unit_id"] != _sha(_canonical(identity))[:32]:
            raise LedgerError("unit ID does not match its source identity")
        seen.add(unit["unit_id"])
        source.append(unit["text"])
        previous_segment, previous_end, previous_speaker = segment_index, unit["end_offset"], unit["speaker_id"]
    if _sha("".join(source).encode("utf-8")) != plan["source_text_sha256"]:
        raise LedgerError("planned units do not preserve the pinned chapter text")


def _validate_wav(path: Path) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise LedgerError("unit WAV missing or unsafe")
    try:
        with wave.open(str(path), "rb") as source:
            params = (source.getnchannels(), source.getsampwidth(),
                      source.getframerate(), source.getcomptype())
            if params != (1, 2, 24000, "NONE") or source.getnframes() <= 0:
                raise LedgerError("unit WAV format invalid")
            frames = source.getnframes()
            read_frames = 0
            digest = hashlib.sha256()
            while True:
                block = source.readframes(32768)
                if not block:
                    break
                if len(block) % 2:
                    raise LedgerError("unit WAV contains truncated samples")
                digest.update(block)
                read_frames += len(block) // 2
            if read_frames != frames:
                raise LedgerError("unit WAV frame count mismatch")
    except (wave.Error, EOFError, OSError) as exc:
        raise LedgerError("unit WAV could not be decoded") from exc
    whole_sha, byte_count = _hash_file(path)
    return {"wav_sha256": whole_sha, "wav_bytes": byte_count,
            "pcm_sha256": digest.hexdigest(), "frames": frames,
            "sample_rate": 24000, "channels": 1, "sample_width": 2}


class UnitLedger:
    """Append-only, one-attempt-per-unit ledger with verified chapter assembly."""

    def __init__(self, root: Path, plan: dict[str, Any]):
        _validate_plan(plan)
        self.plan = json.loads(_canonical(plan))
        self.root = Path(root)
        if not self.root.is_absolute():
            raise LedgerError("ledger root must be absolute")
        if self.root.exists():
            if self.root.is_symlink() or not self.root.is_dir():
                raise LedgerError("ledger root exists but is unsafe")
            plan_path = self.root / "plan.json"
            if not plan_path.is_file() or plan_path.is_symlink() or plan_path.read_bytes() != _canonical(self.plan):
                raise LedgerError("existing ledger belongs to a different plan")
        else:
            parent = self.root.parent.resolve(strict=True)
            self.root = parent / self.root.name
            self.root.mkdir(exist_ok=False)
            _write_json_new(self.root / "plan.json", self.plan)
        self.units_root = self.root / "units"
        if self.units_root.is_symlink() or (self.units_root.exists() and not self.units_root.is_dir()):
            raise LedgerError("unit ledger directory is unsafe")

    def _unit(self, unit_id: str) -> dict[str, Any]:
        for unit in self.plan["units"]:
            if unit["unit_id"] == unit_id:
                return unit
        raise LedgerError("unknown unit id")

    def _unit_dir(self, unit_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", unit_id):
            raise LedgerError("invalid unit id")
        if self.units_root.is_symlink():
            raise LedgerError("unit ledger directory is a symlink")
        path = self.units_root / unit_id
        if path.is_symlink():
            raise LedgerError("unit ledger directory is a symlink")
        return path

    def _require_prior_done(self, unit: dict[str, Any]) -> None:
        for prior in self.plan["units"][:unit["ordinal"]]:
            self._verify_done(prior)

    def _unit_identity(self, unit: dict[str, Any]) -> dict[str, Any]:
        return {"plan_sha256": self.plan["plan_sha256"],
                "book_id": self.plan["book_id"], "chapter_id": self.plan["chapter_id"],
                "unit_id": unit["unit_id"], "ordinal": unit["ordinal"],
                "speaker_id": unit["speaker_id"], "text_sha256": unit["text_sha256"],
                "voice_vector_sha256": unit["voice_vector_sha256"],
                "runtime_monitor_sha256": self.plan["runtime_monitor_sha256"],
                "runtime_min_free_percent": RUNTIME_MIN_FREE_PERCENT}

    def _verify_done(self, unit: dict[str, Any]) -> dict[str, Any]:
        directory = self._unit_dir(unit["unit_id"])
        done_path = directory / "done.json"
        if not done_path.is_file() or done_path.is_symlink():
            raise LedgerError("completed unit record missing")
        done = _read_json(done_path)
        if (done.get("status") != "complete" or done.get("human_reviewed") is not False
                or done.get("identity") != self._unit_identity(unit)
                or done.get("unit_text_sha256") != unit["text_sha256"]
                or done.get("speaker_id") != unit["speaker_id"]
                or done.get("voice_vector_sha256") != unit["voice_vector_sha256"]
                or not isinstance(done.get("verification_evidence"), dict)
                or done.get("verification_evidence_sha256") !=
                   _sha(_canonical(done["verification_evidence"]))):
            raise LedgerError("completed unit identity mismatch")
        outcome = _read_json(directory / "outcome.json")
        if (outcome.get("state") != "completion_claimed"
                or outcome.get("identity") != self._unit_identity(unit)
                or outcome.get("verification_evidence") != done["verification_evidence"]
                or outcome.get("verification_evidence_sha256") != done["verification_evidence_sha256"]
                or done.get("outcome_sha256") != _sha((directory / "outcome.json").read_bytes())):
            raise LedgerError("completed unit outcome/provenance record mismatch")
        self._validate_evidence(unit, done["verification_evidence"])
        audio_path = directory / "audio.wav"
        actual = _validate_wav(audio_path)
        for key, value in actual.items():
            if done.get(key) != value:
                raise LedgerError("completed unit audio hash or format mismatch")
        return {"unit": unit, "done": done, "audio_path": audio_path, "audio": actual}

    def next_unit(self) -> dict[str, Any]:
        """Return a verified completion, reserve exactly one new unit, or stop unknown."""
        if (self.root / "assembled.json").exists():
            return {"status": "complete", "assembled": self.verify_assembled()}
        for unit in self.plan["units"]:
            directory = self._unit_dir(unit["unit_id"])
            done_path = directory / "done.json"
            reserve_path = directory / "reserved.json"
            outcome_path = directory / "outcome.json"
            audio_path = directory / "audio.wav"
            if done_path.exists():
                if reserve_path.is_symlink() or not reserve_path.is_file():
                    raise LedgerError("completion has no reservation")
                self._verify_done(unit)
                continue
            if outcome_path.exists():
                outcome = _read_json(outcome_path)
                if outcome.get("identity") != self._unit_identity(unit):
                    raise LedgerError("unit terminal outcome identity mismatch")
                return {"status": "unknown", "unit_id": unit["unit_id"],
                        "reason": "terminal_outcome_exists_without_completion",
                        "human_reviewed": False}
            if reserve_path.exists() or audio_path.exists():
                if reserve_path.exists():
                    reserved = _read_json(reserve_path)
                    if (reserved.get("status") != "may_have_occurred"
                            or reserved.get("identity") != self._unit_identity(unit)):
                        raise LedgerError("unit reservation identity mismatch")
                return {"status": "unknown", "unit_id": unit["unit_id"],
                        "reason": "reservation_or_partial_output_exists",
                        "human_reviewed": False}
            directory.mkdir(parents=True, exist_ok=False)
            _write_json_new(reserve_path, {
                "status": "may_have_occurred", "identity": self._unit_identity(unit),
                "human_reviewed": False})
            return {"status": "ready", "unit": json.loads(_canonical(unit)),
                    "reservation": str(reserve_path), "human_reviewed": False}
        return {"status": "all_units_complete", "unit_count": len(self.plan["units"]),
                "human_reviewed": False}

    def record_unknown(self, unit_id: str, reason: str) -> None:
        unit = self._unit(unit_id)
        directory = self._unit_dir(unit_id)
        reservation = directory / "reserved.json"
        if (not reservation.is_file() or reservation.is_symlink()
                or (directory / "done.json").exists() or (directory / "audio.wav").exists()
                or (directory / "outcome.json").exists()):
            raise LedgerError("unknown outcome requires an existing reservation")
        reserved = _read_json(reservation)
        if (reserved.get("status") != "may_have_occurred"
                or reserved.get("identity") != self._unit_identity(unit)):
            raise LedgerError("unknown outcome reservation identity mismatch")
        if reason not in _ALLOWED_UNKNOWN_REASONS:
            raise LedgerError("unknown reason is not allowlisted")
        _write_json_new(directory / "outcome.json", {
            "state": "unknown", "reason": reason,
            "identity": self._unit_identity(unit), "human_reviewed": False})

    def _validate_evidence(self, unit: dict[str, Any], evidence: dict[str, Any]) -> None:
        monitor = evidence.get("runtime_monitor")
        if (evidence.get("unit_text_sha256") != unit["text_sha256"]
                or evidence.get("rendered_text_sha256") != unit["text_sha256"]
                or evidence.get("speaker_id") != unit["speaker_id"]
                or evidence.get("voice_vector_sha256") != unit["voice_vector_sha256"]
                or not isinstance(monitor, dict)
                or type(monitor.get("exit_code")) is not int or monitor.get("exit_code") != 0
                or monitor.get("reason") is not None
                or type(monitor.get("minimum_pressure_free_percent")) is not int
                or monitor["minimum_pressure_free_percent"] < RUNTIME_MIN_FREE_PERCENT
                or monitor.get("monitor_sha256") != self.plan["runtime_monitor_sha256"]):
            raise LedgerError("unit source, voice, or 30% runtime monitor evidence failed")

    def complete_unit(self, unit_id: str, audio_path: Path, *, evidence: dict[str, Any]) -> dict[str, Any]:
        unit = self._unit(unit_id)
        directory = self._unit_dir(unit_id)
        reservation_path = directory / "reserved.json"
        if not reservation_path.is_file() or reservation_path.is_symlink():
            raise LedgerError("unit must be reserved before completion")
        self._require_prior_done(unit)
        reservation = _read_json(reservation_path)
        if (reservation.get("status") != "may_have_occurred"
                or reservation.get("identity") != self._unit_identity(unit)
                or (directory / "outcome.json").exists()
                or (directory / "done.json").exists()
                or (directory / "audio.wav").exists()):
            raise LedgerError("unit reservation is unknown, complete, or mismatched")
        if not isinstance(evidence, dict):
            raise LedgerError("unit verification evidence is required")
        self._validate_evidence(unit, evidence)
        evidence_sha = _sha(_canonical(evidence))
        source = Path(audio_path)
        if source.is_symlink() or not source.is_file():
            raise LedgerError("rendered unit WAV is missing or unsafe")
        audio_info = _validate_wav(source)
        # One exclusive terminal claim arbitrates unknown vs completion. If the
        # process stops after claiming completion but before done.json, the unit
        # stays unknown and cannot be retried automatically.
        outcome = {"state": "completion_claimed", "identity": self._unit_identity(unit),
                   "verification_evidence": evidence,
                   "verification_evidence_sha256": evidence_sha,
                   "human_reviewed": False}
        _write_json_new(directory / "outcome.json", outcome)
        target = directory / "audio.wav"
        digest = hashlib.sha256()
        byte_count = 0
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            while True:
                block = incoming.read(1024 * 1024)
                if not block:
                    break
                outgoing.write(block)
                digest.update(block)
                byte_count += len(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if digest.hexdigest() != audio_info["wav_sha256"] or byte_count != audio_info["wav_bytes"]:
            raise LedgerError("copied unit WAV changed during commit")
        done = {"status": "complete", "identity": self._unit_identity(unit),
                "unit_text_sha256": unit["text_sha256"], "speaker_id": unit["speaker_id"],
                "voice_vector_sha256": unit["voice_vector_sha256"],
                **audio_info, "verification_evidence_sha256": evidence_sha,
                "verification_evidence": evidence,
                "outcome_sha256": _sha((directory / "outcome.json").read_bytes()),
                "human_reviewed": False}
        _write_json_new(directory / "done.json", done)
        return done

    def assemble(self) -> dict[str, Any]:
        """Stream verified unit PCM in source order into a new chapter WAV."""
        if (self.root / "chapter.wav").exists() or (self.root / "assembled.json").exists():
            raise LedgerError("chapter assembly already exists or is uncertain")
        verified = [self._verify_done(unit) for unit in self.plan["units"]]
        formats = {(u["audio"]["channels"], u["audio"]["sample_width"],
                    u["audio"]["sample_rate"]) for u in verified}
        if len(formats) != 1:
            raise LedgerError("unit WAV formats differ")
        channels, sample_width, sample_rate = next(iter(formats))
        temporary = self.root / ("chapter-" + uuid.uuid4().hex + ".partial.wav")
        final = self.root / "chapter.wav"
        linked = False
        try:
            frames_total = 0
            with wave.open(str(temporary), "wb") as target:
                target.setnchannels(channels)
                target.setsampwidth(sample_width)
                target.setframerate(sample_rate)
                for item in verified:
                    with wave.open(str(item["audio_path"]), "rb") as source:
                        while True:
                            block = source.readframes(32768)
                            if not block:
                                break
                            target.writeframesraw(block)
                            frames_total += len(block) // (channels * sample_width)
            with temporary.open("rb+") as stream:
                os.fsync(stream.fileno())
            os.link(temporary, final)
            linked = True
            try:
                fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
        except BaseException:
            # Preserve any partial file as evidence; never overwrite or retry it.
            raise
        finally:
            if linked and temporary.exists():
                temporary.unlink()
        wav_info = _validate_wav(final)
        assembled = {"status": "complete", "plan_sha256": self.plan["plan_sha256"],
                     "source_text_sha256": self.plan["source_text_sha256"],
                     "unit_ids": [u["unit"]["unit_id"] for u in verified],
                     "unit_wav_sha256": [u["audio"]["wav_sha256"] for u in verified],
                     "unit_done_sha256": [_sha((self._unit_dir(u["unit"]["unit_id"])
                                                 / "done.json").read_bytes()) for u in verified],
                     "unit_evidence_sha256": [u["done"]["verification_evidence_sha256"]
                                              for u in verified],
                     "frames": frames_total, "chapter_wav_sha256": wav_info["wav_sha256"],
                     "chapter_wav_bytes": wav_info["wav_bytes"],
                     "runtime_min_free_percent": RUNTIME_MIN_FREE_PERCENT,
                     "human_reviewed": False}
        _write_json_new(self.root / "assembled.json", assembled)
        return assembled

    def verify_assembled(self) -> dict[str, Any]:
        record = _read_json(self.root / "assembled.json")
        if (record.get("status") != "complete"
                or record.get("plan_sha256") != self.plan["plan_sha256"]
                or record.get("source_text_sha256") != self.plan["source_text_sha256"]
                or record.get("unit_ids") != [u["unit_id"] for u in self.plan["units"]]
                or record.get("human_reviewed") is not False):
            raise LedgerError("chapter assembly record mismatch")
        wav_info = _validate_wav(self.root / "chapter.wav")
        if (wav_info["wav_sha256"] != record.get("chapter_wav_sha256")
                or wav_info["wav_bytes"] != record.get("chapter_wav_bytes")
                or wav_info["frames"] != record.get("frames")):
            raise LedgerError("chapter assembly WAV hash or frame count mismatch")
        verified = [self._verify_done(unit) for unit in self.plan["units"]]
        if (record.get("unit_wav_sha256") != [u["audio"]["wav_sha256"] for u in verified]
                or record.get("unit_done_sha256") != [
                    _sha((self._unit_dir(u["unit"]["unit_id"]) / "done.json").read_bytes())
                    for u in verified]
                or record.get("unit_evidence_sha256") != [
                    u["done"]["verification_evidence_sha256"] for u in verified]
                or record.get("frames") != sum(u["audio"]["frames"] for u in verified)):
            raise LedgerError("assembled unit provenance or order hash list mismatch")
        return record


__all__ = ["LedgerError", "UnitLedger", "plan_units", "RUNTIME_MIN_FREE_PERCENT"]
