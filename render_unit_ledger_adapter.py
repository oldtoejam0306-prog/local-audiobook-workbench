"""Offline adapter seam between the unit ledger and the existing render bridge.

`run_segment_adapter` is supplied by the caller. This module only accepts its
WAV and attestations after `real_book_renderer.prepare` and
`isolated_chapter_voice_bridge.render_document` validate the exact text,
speaker, vector, and WAV format. The adapter attestation remains trusted input:
this wrapper cannot authenticate a remote provider or monitor by itself.

No renderer is configured by default. The tests use only a local WAV stub.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

import isolated_chapter_voice_bridge as bridge
import real_book_renderer as renderer
from render_unit_ledger import (LedgerError, UnitLedger, plan_units,
                                validate_plan_segments)


_WINDOWS_MAX_PATH = 259
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_SAFE_ADAPTER_CODES = {
    "ADAPTER_PREFLIGHT_FAILED", "REMOTE_SOURCE_FAILED",
    "REMOTE_COMMAND_RESPONSE_FAILED", "MONITOR_FAILED",
    "COPY_FAILED", "PCM_VALIDATION_FAILED",
}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return (path.is_symlink()
            or bool(getattr(info, "st_file_attributes", 0)
                    & _FILE_ATTRIBUTE_REPARSE_POINT))


def _check_no_reparse_ancestors(path: Path) -> None:
    """Reject symlinks/junctions in every existing component of a fixed path."""
    absolute = Path(os.path.abspath(path))
    for component in reversed((absolute, *absolute.parents)):
        if _is_reparse_point(component):
            raise LedgerError("render path contains a reparse point")


def _check_path_length(path: Path) -> None:
    # Windows MAX_PATH counts UTF-16 code units and includes the terminating NUL.
    absolute = Path(os.path.abspath(path))
    units = len(str(absolute).encode("utf-16-le")) // 2
    if units >= _WINDOWS_MAX_PATH:
        raise LedgerError("render path exceeds the safe Windows path length")
    if any(len(part.encode("utf-16-le")) // 2 > 255 for part in absolute.parts):
        raise LedgerError("render path component exceeds the safe Windows limit")


def _preflight_render_paths(ledger: UnitLedger,
                            render_output_base: Path | None) -> None:
    """Check every planned fixed output before next_unit can reserve work.

    Completed units may retain their outputs. A reservation, partial WAV, or
    terminal outcome is left for next_unit to classify as unknown; this check
    never clears or reinterprets that state.
    """
    if render_output_base is not None:
        _check_no_reparse_ancestors(render_output_base)
        if render_output_base.exists() and not render_output_base.is_dir():
            raise LedgerError("render output root exists but is not a directory")
    _check_no_reparse_ancestors(ledger.root)
    _check_no_reparse_ancestors(ledger.units_root)

    for unit in ledger.plan["units"]:
        unit_dir = ledger.units_root / unit["unit_id"]
        if render_output_base is None:
            output_parent = unit_dir / "render-output"
        else:
            output_parent = render_output_base / ("u%04d" % unit["ordinal"])
        bridge_parent = output_parent / ("chapter-render-" + ("f" * 32))
        fixed_targets = (
            unit_dir, unit_dir / "reserved.json", unit_dir / "outcome.json",
            unit_dir / "done.json", unit_dir / "audio.wav", output_parent,
            bridge_parent, bridge_parent / "manifest.json",
            bridge_parent / "chapter-0000" / "chapter.wav",
            bridge_parent / "chapter-0000" / "segment-0000" / "rendered.wav",
            bridge_parent / "chapter-0000" / "segment-0000" / "monitor-result.json",
        )
        for target in fixed_targets:
            _check_path_length(target)
            _check_no_reparse_ancestors(target)

        done_path = unit_dir / "done.json"
        if done_path.exists():
            # Existing completed unit directories and their render trees are
            # expected during normal resume.
            continue
        reserve_path = unit_dir / "reserved.json"
        outcome_path = unit_dir / "outcome.json"
        audio_path = unit_dir / "audio.wav"
        if reserve_path.exists() or outcome_path.exists() or audio_path.exists():
            # Preserve next_unit's existing fail-closed unknown semantics.
            continue
        if unit_dir.exists():
            raise LedgerError("unfinished unit output path is already occupied")
        if output_parent.exists():
            raise LedgerError("unit render output path is already occupied")


def _head_unit_is_already_terminal_or_reserved(ledger: UnitLedger) -> bool:
    """Let next_unit preserve its existing unknown result without new checks."""
    for unit in ledger.plan["units"]:
        unit_dir = ledger.units_root / unit["unit_id"]
        if (unit_dir / "done.json").exists():
            continue
        return any((unit_dir / name).exists()
                   for name in ("reserved.json", "outcome.json", "audio.wav"))
    return False


def _existing_terminal_result(ledger: UnitLedger, ledger_root: Path,
                              unit_count: int) -> dict[str, Any] | None:
    """Return a terminal ledger result without validating an unrelated render root."""
    all_done = all((ledger.units_root / unit["unit_id"] / "done.json").exists()
                   for unit in ledger.plan["units"])
    if (not _head_unit_is_already_terminal_or_reserved(ledger)
            and not all_done and not (ledger.root / "assembled.json").exists()):
        return None

    # Each predicate above guarantees next_unit cannot reserve new work:
    # it will report the existing unknown, all-complete, or assembled state.
    state = ledger.next_unit()
    if state["status"] == "unknown":
        return {"status": "unknown", "unit_id": state["unit_id"],
                "reason": state["reason"], "human_reviewed": False}
    if state["status"] == "complete":
        assembled = state["assembled"]
    elif state["status"] == "all_units_complete":
        assembled = ledger.assemble()
    else:
        raise LedgerError("terminal ledger probe unexpectedly reserved a unit")
    return {"status": "complete", "assembled": assembled,
            "chapter_wav": str(Path(ledger_root) / "chapter.wav"),
            "unit_count": unit_count, "human_reviewed": False}


def run_with_adapter(*, stage_path: Path, book_id: str, chapter_id: str,
                     source_sha256: str, role_bindings: dict[str, Any],
                     runtime_monitor_sha256: str, ledger_root: Path,
                     run_segment_adapter: Callable[[dict[str, Any], dict[str, Any], Path], dict[str, Any]],
                     render_output_root: Path | None = None,
                     max_codepoints: int = 96,
                     plan_override: dict[str, Any] | None = None,
                     precompleted_units: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run one externally supplied renderer call per planned unit, with no retry.

    The injected adapter contract is `(unit, voice_record, fresh_output_dir)`
    and returns `{wav_path, rendered_text_sha256, speaker_id,
    voice_vector_sha256, runtime_monitor, adapter_receipt}`. The adapter must
    authenticate the render response and monitor; this function checks and
    stores its claims but cannot independently prove remote provenance.
    """
    if not callable(run_segment_adapter):
        raise LedgerError("trusted segment adapter is required")
    document, assignments = renderer.prepare(stage_path, book_id, chapter_id,
                                             source_sha256, role_bindings)
    chapters = document["chapters"]
    if len(chapters) != 1 or chapters[0].get("id") != chapter_id:
        raise LedgerError("renderer preparation returned an unexpected chapter")
    segments = chapters[0]["segments"]
    voice_pins = {}
    for segment in segments:
        speaker = segment["speaker_id"]
        voice = assignments.get(book_id + "\0" + speaker)
        if not isinstance(voice, dict):
            raise LedgerError("prepared segment has no verified voice binding")
        voice_pins[speaker] = voice["request"]["vector_sha256"]
    expected_plan = plan_units(book_id=book_id, chapter_id=chapter_id,
                               source_text_sha256=source_sha256, segments=segments,
                               voice_vector_sha256=voice_pins,
                               runtime_monitor_sha256=runtime_monitor_sha256,
                               max_codepoints=(plan_override.get("max_codepoints", max_codepoints)
                                               if isinstance(plan_override, dict)
                                               else max_codepoints))
    plan = expected_plan if plan_override is None else json.loads(
        json.dumps(plan_override, ensure_ascii=False, sort_keys=True))
    if plan_override is not None:
        metadata_keys = set(expected_plan) - {"units", "plan_sha256"}
        if any(plan.get(key) != expected_plan.get(key) for key in metadata_keys):
            raise LedgerError("custom unit plan metadata differs from frozen inputs")
        validate_plan_segments(plan, segments)
    if precompleted_units and plan_override is None:
        raise LedgerError("completed-unit reuse requires an explicit custom plan")
    supplied_ledger_root = Path(ledger_root)
    if supplied_ledger_root.is_absolute():
        _check_no_reparse_ancestors(supplied_ledger_root)
    ledger = UnitLedger(ledger_root, plan)
    terminal_result = _existing_terminal_result(ledger, Path(ledger_root),
                                                len(plan["units"]))
    if terminal_result is not None:
        return terminal_result

    if render_output_root is None:
        render_output_base = None
    else:
        ledger_path = Path(ledger_root).resolve(strict=True)
        requested_root = Path(render_output_root)
        _check_no_reparse_ancestors(requested_root)
        if requested_root.is_symlink():
            raise LedgerError("render output root is a symlink")
        render_output_base = requested_root.resolve(strict=False)
        if (render_output_base.parent != ledger_path.parent
                or render_output_base == ledger_path):
            raise LedgerError("render output root must be an isolated ledger sibling")

    _preflight_render_paths(ledger, render_output_base)
    # Reusing an existing base supports a process resuming after completed
    # units. The per-unit child paths were checked above before reservation.
    if render_output_base is not None:
        render_output_base.mkdir(exist_ok=True)

    for seed in precompleted_units or []:
        if not isinstance(seed, dict) or set(seed) != {"unit_id", "audio_path", "verification_evidence"}:
            raise LedgerError("precompleted-unit seed fields are invalid")
        state = ledger.next_unit()
        if (state.get("status") != "ready"
                or state["unit"].get("unit_id") != seed.get("unit_id")):
            raise LedgerError("precompleted units are not the reserved plan prefix")
        unit = state["unit"]
        unit_dir = ledger._unit_dir(unit["unit_id"])
        source = Path(seed["audio_path"])
        _check_no_reparse_ancestors(source)
        if source.is_symlink() or not source.is_file():
            raise LedgerError("precompleted unit WAV is missing or unsafe")
        reused_audio = unit_dir / "preserved-source.wav"
        with source.open("rb") as incoming, reused_audio.open("xb") as outgoing:
            while True:
                block = incoming.read(1024 * 1024)
                if not block:
                    break
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        evidence = json.loads(json.dumps(seed["verification_evidence"], ensure_ascii=False))
        receipt = evidence.get("adapter_receipt")
        local_audio = receipt.get("local_audio") if isinstance(receipt, dict) else None
        if not isinstance(local_audio, dict):
            raise LedgerError("precompleted unit lacks local audio receipt evidence")
        reused_bytes = reused_audio.read_bytes()
        wav_sha, wav_bytes = _sha(reused_bytes), len(reused_bytes)
        if (local_audio.get("wav_sha256") != wav_sha
                or local_audio.get("wav_bytes") != wav_bytes):
            raise LedgerError("precompleted WAV differs from its saved receipt")
        local_audio["wav_path"] = str(reused_audio)
        evidence["render_output_root"] = None
        evidence["ledger_audio_copy"] = {
            "wav_path": str(unit_dir / "audio.wav"),
            "wav_sha256": wav_sha, "wav_bytes": wav_bytes}
        ledger.complete_unit(unit["unit_id"], reused_audio, evidence=evidence)

    while True:
        state = ledger.next_unit()
        if state["status"] == "complete":
            return {"status": "complete", "assembled": state["assembled"],
                    "chapter_wav": str(Path(ledger_root) / "chapter.wav"),
                    "unit_count": len(plan["units"]), "human_reviewed": False}
        if state["status"] == "all_units_complete":
            assembled = ledger.assemble()
            return {"status": "complete", "assembled": assembled,
                    "chapter_wav": str(Path(ledger_root) / "chapter.wav"),
                    "unit_count": len(plan["units"]), "human_reviewed": False}
        if state["status"] == "unknown":
            return {"status": "unknown", "unit_id": state["unit_id"],
                    "reason": state["reason"], "human_reviewed": False}

        unit = state["unit"]
        unit_id = unit["unit_id"]
        voice = assignments[book_id + "\0" + unit["speaker_id"]]
        unit_dir = Path(ledger_root) / "units" / unit_id
        render_parent = (unit_dir / "render-output" if render_output_base is None
                         else render_output_base / ("u%04d" % unit["ordinal"]))
        try:
            render_parent.mkdir(exist_ok=False)
            adapter_result: dict[str, Any] = {}

            def render_one_segment(bound_voice: dict[str, Any], text: str,
                                   output_dir: Path) -> Path:
                result = run_segment_adapter(unit, bound_voice, output_dir)
                if not isinstance(result, dict):
                    raise LedgerError("segment adapter returned no evidence object")
                required = {"wav_path", "rendered_text_sha256", "speaker_id",
                            "voice_vector_sha256", "runtime_monitor", "adapter_receipt"}
                if set(result) != required:
                    raise LedgerError("segment adapter evidence fields are incomplete")
                if (bound_voice != voice or text != unit["text"]
                        or result.get("rendered_text_sha256") != unit["text_sha256"]
                        or result.get("speaker_id") != unit["speaker_id"]
                        or result.get("voice_vector_sha256") != unit["voice_vector_sha256"]
                        or not isinstance(result.get("adapter_receipt"), dict)):
                    raise LedgerError("segment adapter returned mismatched text or voice")
                monitor = result.get("runtime_monitor")
                if (not isinstance(monitor, dict) or monitor.get("exit_code") != 0
                        or monitor.get("reason") is not None
                        or type(monitor.get("minimum_pressure_free_percent")) is not int
                        or monitor["minimum_pressure_free_percent"] < 30
                        or monitor.get("monitor_sha256") != runtime_monitor_sha256):
                    raise LedgerError("segment adapter did not prove the unchanged 30% monitor")
                wav_path = Path(result["wav_path"])
                if (wav_path.is_symlink() or not wav_path.is_file()
                        or not wav_path.resolve().is_relative_to(Path(output_dir).resolve())):
                    raise LedgerError("segment adapter WAV is outside its isolated unit output")
                adapter_result.update(result)
                return wav_path

            render_document = {"book": book_id,
                               "chapters": [{"id": chapter_id,
                                             "segments": [{"speaker_id": unit["speaker_id"],
                                                           "text": unit["text"]}]}]}
            bridge_render_root, manifest = bridge.render_document(
                render_document, assignments, render_parent,
                render_segment=render_one_segment)
            if (manifest.get("status") != "complete"
                    or len(manifest.get("chapters", [])) != 1
                    or manifest["chapters"][0].get("id") != chapter_id
                    or len(manifest["chapters"][0].get("segments", [])) != 1):
                raise LedgerError("existing renderer did not produce one verified segment")
            entry = manifest["chapters"][0]["segments"][0]
            segment_output = (bridge_render_root / "chapter-0000" / "segment-0000").resolve()
            unit_wav = Path(adapter_result["wav_path"]).resolve()
            if (not unit_wav.is_relative_to(segment_output)
                    or entry.get("speaker_id") != unit["speaker_id"]
                    or entry.get("text_sha256") != unit["text_sha256"]
                    or entry.get("vector_sha256") != unit["voice_vector_sha256"]
                    or entry.get("wav_sha256") != renderer.sha(unit_wav.read_bytes())):
                raise LedgerError("existing renderer manifest identity/hash mismatch")
            evidence = {"unit_text_sha256": unit["text_sha256"],
                        "rendered_text_sha256": adapter_result["rendered_text_sha256"],
                        "speaker_id": unit["speaker_id"],
                        "voice_vector_sha256": unit["voice_vector_sha256"],
                        "runtime_monitor": adapter_result["runtime_monitor"],
                        "adapter_receipt": adapter_result["adapter_receipt"],
                        # The adapter-local WAV can live in a caller-selected
                        # sibling output tree. Record the durable ledger copy
                        # separately so later verification does not confuse
                        # the two paths.
                        "render_output_root": (str(render_output_base)
                                               if render_output_base is not None else None),
                        "ledger_audio_copy": {
                            "wav_path": str(ledger.root / "units" / unit_id / "audio.wav"),
                            "wav_sha256": entry["wav_sha256"],
                            "wav_bytes": unit_wav.stat().st_size},
                        "bridge_manifest_sha256": _sha(
                            (bridge_render_root / "manifest.json").read_bytes()),
                        "bridge_segment_wav_sha256": entry["wav_sha256"]}
            ledger.complete_unit(unit_id, unit_wav, evidence=evidence)
        except Exception as exc:
            # Once reserved, any failed or ambiguous adapter call is terminal.
            try:
                ledger.record_unknown(unit_id, "renderer_failed")
            except LedgerError:
                pass
            error_type = type(exc).__name__
            if not error_type.isascii() or not error_type.isidentifier() or len(error_type) > 40:
                error_type = "Error"
            result = {"status": "unknown", "unit_id": unit_id,
                      "error_type": error_type, "human_reviewed": False}
            error_code = getattr(exc, "error_code", None)
            error_stage = getattr(exc, "error_stage", None)
            if error_code in _SAFE_ADAPTER_CODES and isinstance(error_stage, str):
                result.update(error_code=error_code, error_stage=error_stage)
            return result


__all__ = ["run_with_adapter"]
