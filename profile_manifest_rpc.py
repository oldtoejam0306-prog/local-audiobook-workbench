"""Restricted Mac-side RPC adapter for the book-scoped profile manifest.

The Windows worker sends hashes and metadata only. This module reads the
server-side export and book files, verifies the pinned worker copy, and writes
to the existing analysis manifest. It never accepts or forwards API keys and
never makes a model request.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable
import release_config

import analysis_manifest as ledger


DEFAULT_ROOT = release_config.mac_data_root()
DEFAULT_WORKER_COPY = Path(__file__).resolve().parent / "approved-workers" / "deepseek_profile.mjs"
ALLOWED_WORKER_SHA256 = frozenset({
    "050a4b2755c7fdc90e5791978e4dfd154311d2f91fc3756b7a65a106d78d4d25",
    "7bad6466a764196b63bdd690b03c739c35c02fd98ec9c3ea9fbf0fcc1f7bf7a8",
})
DEFAULT_CHAPTER_MAP_WORKER_COPY = Path(__file__).resolve().with_name("chapter_map_worker.mjs")
CHAPTER_MAP_WORKER_SHA256 = frozenset({"ccc83ee9a48869eb5b1134e8f9ade521b1a52e714fcdbb4fd148f4de362c4539"})
CHAPTER_MAP_V2_BUILDER_SHA256 = frozenset({"ced862497a1f2d7546e84aa4a58bde74216835d90461a2203ba93c8a3fbeae19"})
_BOOK_ID_RE = re.compile(r"(?:book-[0-9a-f]{20}|deep-sea-test)\Z")
_CHAPTER_ID_RE = re.compile(r"c[0-9]+(?:\.p[0-9]+(?:\.[0-9]+)?)?\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_RESERVE_FIELDS = frozenset({
    "book_id", "chapter_id", "job_hash", "request_body_sha256",
    "system_prompt_sha256", "user_prompt_sha256", "mjs_source_sha256",
    "requested_model", "reservation_cny",
})
_FINALIZE_FIELDS = frozenset({"book_id", "chapter_id", "job_hash", "request_body_sha256", "execution_ended"})
_CHAPTER_MAP_RESERVE_FIELDS = frozenset({
    "protocol", "book_id", "chapter_id", "job_hash", "source_sha256",
    "request_body_sha256", "system_prompt_sha256", "user_prompt_sha256",
    "worker_source_sha256", "estimated_peak_cny", "budget_cny", "requested_model",
})
_CHAPTER_MAP_FINALIZE_FIELDS = frozenset({
    "protocol", "book_id", "chapter_id", "job_hash", "request_body_sha256",
    "attempt_id", "execution_ended",
})
_CHAPTER_MAP_V2_FIELDS = frozenset({
    "protocol", "book_id", "chapter_id", "job_hash", "source_sha256", "script_sha256",
    "memory_sha256", "manifest_snapshot_sha256", "chapter_input_sha256", "machine_context_sha256",
    "machine_context_included_sha256", "machine_context_ledger_provenance_sha256",
    "machine_context_provenance",
    "request_body_sha256", "system_prompt_sha256", "user_prompt_sha256", "builder_source_sha256",
    "requested_model", "estimated_peak_cny", "budget_cny", "budget_snapshot_cny",
})
_CHAPTER_MAP_V2_RECEIPT_FIELDS = frozenset({"book_id", "chapter_id"})


class RpcRejected(ValueError):
    """The request is malformed, stale, or not safe to apply."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_workdir(root: Path, book_id: str) -> Path:
    root = Path(root).resolve()
    if not isinstance(book_id, str) or not _BOOK_ID_RE.fullmatch(book_id):
        raise RpcRejected("book_id is invalid")
    if book_id == "deep-sea-test":
        workdir = root / "two-excerpts-brisk.huashuo"
    else:
        books_root = (root / "books").resolve()
        book_root = (books_root / book_id).resolve()
        if not book_root.is_relative_to(books_root):
            raise RpcRejected("book path escapes the configured root")
        workdir = book_root / "work.huashuo"
    resolved = workdir.resolve()
    if not resolved.is_relative_to(root):
        raise RpcRejected("workdir escapes the configured root")
    if not resolved.is_dir():
        raise RpcRejected("book workdir does not exist")
    return resolved


def _chapter_paths(workdir: Path, chapter_id: str) -> tuple[Path, Path, Path]:
    if not isinstance(chapter_id, str) or not _CHAPTER_ID_RE.fullmatch(chapter_id):
        raise RpcRejected("chapter_id is invalid")
    state = workdir / "state"
    export_path = state / f"profile-input-{chapter_id}.json"
    receipt_path = state / f"profile-state-{chapter_id}.json"
    manifest_path = state / "analysis-manifest.json"
    for candidate in (export_path, receipt_path, manifest_path):
        if not candidate.resolve().is_relative_to(workdir.resolve()):
            raise RpcRejected("task path escapes the book workdir")
    return manifest_path, export_path, receipt_path


def _chapter_map_paths(workdir: Path, chapter_id: str) -> tuple[Path, Path, Path]:
    if not isinstance(chapter_id, str) or not _CHAPTER_ID_RE.fullmatch(chapter_id):
        raise RpcRejected("chapter_id is invalid")
    state = workdir / "state"
    export_path = state / f"chapter-map-input-{chapter_id}.json"
    receipt_path = state / f"chapter-map-state-{chapter_id}.json"
    manifest_path = state / "analysis-manifest.json"
    for candidate in (export_path, receipt_path, manifest_path):
        if not candidate.resolve().is_relative_to(workdir.resolve()):
            raise RpcRejected("chapter-map task path escapes the book workdir")
    return manifest_path, export_path, receipt_path


@contextmanager
def _book_lock(workdir: Path):
    """Take the same nonblocking book lock used by the Mac console backend."""

    lock_path = workdir / ".console.lock"
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RpcRejected("book is busy; reservation was not written") from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RpcRejected("book is busy; reservation was not written") from None
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_payload(payload: Any, expected_fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise RpcRejected("payload fields are invalid")
    for key in expected_fields:
        value = payload[key]
        if key.endswith("sha256") or key == "job_hash":
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                raise RpcRejected(f"{key} must be a lowercase SHA-256 hash")
    return payload


def _money_string(value: Any, label: str) -> str:
    if isinstance(value, bool) or isinstance(value, float):
        raise RpcRejected(f"{label} must be an exact decimal string")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RpcRejected(f"{label} is invalid") from None
    if not amount.is_finite() or amount < 0:
        raise RpcRejected(f"{label} is invalid")
    return format(amount, "f")


def _trusted_worker_hash(worker_copy: Path, allowlist: Iterable[str]) -> str:
    try:
        if worker_copy.is_symlink():
            raise RpcRejected("pinned worker copy must not be a symlink")
        digest = _sha256(worker_copy.read_bytes())
    except OSError as exc:
        raise RpcRejected(f"pinned worker copy is unavailable: {exc}") from None
    allowed = {item.lower() for item in allowlist}
    if digest not in allowed:
        raise RpcRejected("pinned worker copy hash is not allowlisted")
    return digest


def _expected_request(export_path: Path) -> tuple[dict[str, Any], bytes, str, str, str, str]:
    try:
        export_bytes = export_path.read_bytes()
        export_data = json.loads(export_bytes.decode("utf-8-sig"))
    except FileNotFoundError:
        raise RpcRejected("profile export is missing") from None
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RpcRejected(f"profile export is invalid: {exc}") from None
    if not isinstance(export_data, dict):
        raise RpcRejected("profile export must be a JSON object")
    body, system, prompt = ledger._profile_request_for_export(export_data)
    body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    body_bytes = body_text.encode("utf-8")
    job_hash = _sha256(export_bytes)
    body_hash = _sha256(body_bytes)
    system_hash = hashlib.sha256(system.encode("utf-8")).hexdigest()
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return export_data, body_bytes, job_hash, body_hash, system_hash, prompt_hash


def _chapter_map_money(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise RpcRejected(f"{label} must be a JSON number")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RpcRejected(f"{label} is invalid") from None
    if not amount.is_finite() or amount <= 0:
        raise RpcRejected(f"{label} must be finite and greater than zero")
    return amount


def _expected_chapter_map_request(export_path: Path, budget_cny: int | float) -> tuple[dict[str, Any], str]:
    try:
        raw = export_path.read_bytes()
        exported = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=ledger._unique_json_object,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except FileNotFoundError:
        raise RpcRejected("chapter-map export is missing") from None
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RpcRejected(f"chapter-map export is invalid: {exc}") from None
    if not isinstance(exported, dict):
        raise RpcRejected("chapter-map export must be a JSON object")
    try:
        request = ledger._chapter_map_request_for_export(exported, budget_cny)
    except (ledger.ManifestError, ledger.ProgressBlocked) as exc:
        raise RpcRejected(str(exc)) from None
    request["job_hash"] = _sha256(raw)
    return request, raw.decode("utf-8-sig")


def _reserved_summary(manifest_path: Path, chapter_id: str, expected: dict[str, Any]) -> dict[str, Any] | None:
    try:
        data = ledger.load_manifest(manifest_path)
    except ledger.ManifestError:
        return None
    item = next((row for row in data["chapters"] if row["chapter_id"] == chapter_id), None)
    if not item:
        return None
    provenance = item.get("request_provenance")
    if not isinstance(provenance, dict):
        return None
    for key, value in expected.items():
        if provenance.get(key) != value:
            raise RpcRejected("manifest already contains a different attempt for this chapter")
    return {"chapter": item, "manifest_status": data["status"]}


def _gate_first_dispatch(manifest_path: Path, chapter_id: str, attempt_id: str) -> bool:
    """Persist one launch decision in the sole manifest before returning it."""

    def update(data):
        _, chapter = ledger._chapter(data, chapter_id)
        provenance = chapter.get("request_provenance")
        if (chapter.get("status") != "in_progress" or not isinstance(provenance, dict)
                or provenance.get("attempt_id") != attempt_id):
            raise RpcRejected("reserved attempt changed before dispatch permission")
        if provenance.get("rpc_dispatch_issued") is True:
            return {"dispatch_allowed": False, "chapter": chapter}
        provenance["rpc_dispatch_issued"] = True
        provenance["rpc_dispatch_issued_at"] = ledger._utc_now()
        ledger._record(data, "profile_dispatch_permission_issued", chapter_id, attempt_id=attempt_id)
        return {"dispatch_allowed": True, "chapter": chapter}

    return ledger._locked_update(manifest_path, update)["dispatch_allowed"]


def reserve(
    payload: Any,
    *,
    root: str | Path = DEFAULT_ROOT,
    worker_copy: str | Path = DEFAULT_WORKER_COPY,
    worker_allowlist: Iterable[str] = ALLOWED_WORKER_SHA256,
) -> dict[str, Any]:
    """Verify Windows hashes against Mac-held files, then reserve once."""

    payload = _validate_payload(payload, _RESERVE_FIELDS)
    book_id, chapter_id = payload["book_id"], payload["chapter_id"]
    if not isinstance(payload["requested_model"], str) or payload["requested_model"] != ledger._PROFILE_MODEL:
        raise RpcRejected("requested model is not allowlisted")
    worker_path = Path(worker_copy).expanduser()
    workdir = _resolve_workdir(Path(root), book_id)
    manifest_path, export_path, _ = _chapter_paths(workdir, chapter_id)
    with _book_lock(workdir):
        worker_hash = _trusted_worker_hash(worker_path, worker_allowlist)
        exported, body_bytes, job_hash, body_hash, system_hash, prompt_hash = _expected_request(export_path)
        expected = {
            "book_id": book_id,
            "chapter_id": chapter_id,
            "job_hash": job_hash,
            "request_body_sha256": body_hash,
            "system_prompt_sha256": system_hash,
            "user_prompt_sha256": prompt_hash,
            "mjs_source_sha256": worker_hash,
            "requested_model": ledger._PROFILE_MODEL,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise RpcRejected(f"Windows {key} does not match the Mac-verified request")
        reservation = ledger._request_reservation_cny(
            json.loads(body_bytes)["messages"][0]["content"],
            json.loads(body_bytes)["messages"][1]["content"],
        )
        if _money_string(payload["reservation_cny"], "reservation_cny") != format(reservation, "f"):
            raise RpcRejected("Windows reservation estimate does not match the request")

        existing = _reserved_summary(manifest_path, chapter_id, expected)
        if existing is not None:
            item = existing["chapter"]
            provenance = item["request_provenance"]
            if item["status"] not in ("in_progress", "awaiting_review", "billing_unknown"):
                raise RpcRejected("manifest attempt is not in a reusable state")
            if provenance.get("reservation", {}).get("amount_cny") != format(reservation, "f"):
                raise RpcRejected("manifest reservation does not match the request")
            return {
                "status": item["status"],
                "attempt_id": provenance.get("attempt_id"),
                "reservation": provenance.get("reservation"),
                "dispatch_allowed": False,
                "idempotent_replay": True,
            }

        request_text = body_bytes.decode("utf-8")
        result = ledger.reserve_profile_request(
            manifest_path,
            workdir,
            export_path,
            chapter_id,
            request_text,
            worker_path=worker_path,
        )
        reserved_provenance = result["chapter"].get("request_provenance", {})
        for key, value in expected.items():
            if reserved_provenance.get(key) != value:
                # No dispatch permission is returned. The conservative
                # reservation remains visible for manual reconciliation.
                raise RpcRejected("export or request changed during reservation; no dispatch permission issued")
        attempt_id = result["attempt_id"]
        allowed = _gate_first_dispatch(manifest_path, chapter_id, attempt_id)
        return {
            "status": "in_progress",
            "attempt_id": attempt_id,
            "reservation": result["reservation"],
            "dispatch_allowed": allowed,
            "idempotent_replay": False,
        }


def reserve_chapter_map_v1(
    payload: Any,
    *,
    root: str | Path = DEFAULT_ROOT,
    worker_copy: str | Path = DEFAULT_CHAPTER_MAP_WORKER_COPY,
    worker_allowlist: Iterable[str] = CHAPTER_MAP_WORKER_SHA256,
) -> dict[str, Any]:
    """Reserve chapter-map-v1 in its separate ledger domain; never grant fetch."""
    payload = _validate_payload(payload, _CHAPTER_MAP_RESERVE_FIELDS)
    if payload.get("protocol") != "chapter-map-v1":
        raise RpcRejected("chapter-map protocol is invalid")
    if payload.get("requested_model") != "deepseek-flash":
        raise RpcRejected("chapter-map requested model is not allowlisted")
    budget = _chapter_map_money(payload.get("budget_cny"), "budget_cny")
    estimate = _chapter_map_money(payload.get("estimated_peak_cny"), "estimated_peak_cny")
    if budget > Decimal("10") or estimate > budget:
        raise RpcRejected("chapter-map budget is invalid or below the reserved peak")
    book_id, chapter_id = payload["book_id"], payload["chapter_id"]
    workdir = _resolve_workdir(Path(root), book_id)
    manifest_path, export_path, _ = _chapter_map_paths(workdir, chapter_id)
    with _book_lock(workdir):
        worker_hash = _trusted_worker_hash(Path(worker_copy).expanduser(), worker_allowlist)
        request, _ = _expected_chapter_map_request(export_path, float(budget))
        expected = {
            "protocol": request["protocol"],
            "book_id": request["book_id"],
            "chapter_id": request["chapter_id"],
            "job_hash": request["job_hash"],
            "source_sha256": request["source_sha256"],
            "request_body_sha256": request["request_body_sha256"],
            "system_prompt_sha256": request["system_prompt_sha256"],
            "user_prompt_sha256": request["user_prompt_sha256"],
            "worker_source_sha256": worker_hash,
            "estimated_peak_cny": request["estimated_peak_cny"],
            "budget_cny": budget,
            "requested_model": request["requested_model"],
        }
        for key, actual in expected.items():
            supplied = payload.get(key)
            if key in ("estimated_peak_cny", "budget_cny"):
                if _chapter_map_money(supplied, key) != actual:
                    raise RpcRejected(f"Windows chapter-map {key} does not match Mac request reconstruction")
            elif supplied != actual:
                raise RpcRejected(f"Windows chapter-map {key} does not match Mac request reconstruction")
        result = ledger.reserve_chapter_map_request(
            manifest_path, workdir, export_path, chapter_id, payload,
            worker_path=Path(worker_copy).expanduser(),
        )
        return result


def finalize_chapter_map_v1(
    payload: Any,
    *,
    root: str | Path = DEFAULT_ROOT,
) -> dict[str, Any]:
    """Reconcile a separately bound chapter-map receipt, never a profile receipt."""
    payload = _validate_payload(payload, _CHAPTER_MAP_FINALIZE_FIELDS)
    if payload.get("protocol") != "chapter-map-v1":
        raise RpcRejected("chapter-map protocol is invalid")
    if type(payload.get("execution_ended")) is not bool:
        raise RpcRejected("execution_ended must be a boolean")
    if not isinstance(payload.get("attempt_id"), str) or not _SHA256_RE.fullmatch(payload["attempt_id"]):
        raise RpcRejected("attempt_id must be a lowercase SHA-256 hash")
    workdir = _resolve_workdir(Path(root), payload["book_id"])
    manifest_path, _, receipt_path = _chapter_map_paths(workdir, payload["chapter_id"])
    with _book_lock(workdir):
        current = ledger.load_manifest(manifest_path)
        entry = ledger._chapter_map_entries(current).get(payload["chapter_id"])
        provenance = entry.get("provenance") if isinstance(entry, dict) else None
        if not isinstance(provenance, dict) or provenance.get("protocol") != "chapter-map-v1":
            raise RpcRejected("manifest has no chapter-map-v1 attempt for this chapter")
        for key in ("book_id", "chapter_id", "job_hash", "request_body_sha256", "attempt_id"):
            if provenance.get(key) != payload.get(key):
                raise RpcRejected(f"finalize {key} does not match chapter-map reservation")
        if entry.get("status") in ("awaiting_review", "billing_unknown"):
            proof = entry.get("receipt_provenance")
            expected_hash = proof.get("receipt_sha256") if isinstance(proof, dict) else None
            try:
                actual_hash = _sha256(receipt_path.read_bytes())
            except OSError:
                actual_hash = None
            if expected_hash and actual_hash == expected_hash:
                return {
                    "protocol": "chapter-map-v1", "status": entry["status"],
                    "attempt_id": provenance["attempt_id"],
                    "receipt_sha256": expected_hash, "idempotent_replay": True,
                }
            raise RpcRejected("chapter-map manifest already reconciled with a different or missing receipt")
        # Until a separate explicit dispatch-authorization operation exists,
        # record_chapter_map_receipt rejects this path before any receipt write.
        result = ledger.record_chapter_map_receipt(
            manifest_path, payload["chapter_id"], receipt_path,
            execution_ended=payload["execution_ended"],
        )
        chapter_map = ledger._chapter_map_entries(result)[payload["chapter_id"]]
        receipt_proof = chapter_map.get("receipt_provenance") or {}
        return {
            "protocol": "chapter-map-v1",
            "status": chapter_map["status"],
            "attempt_id": provenance["attempt_id"],
            "receipt_sha256": receipt_proof.get("receipt_sha256"),
            "idempotent_replay": False,
        }


def reserve_chapter_map_v2(payload: Any, *, root: str | Path = DEFAULT_ROOT) -> dict[str, Any]:
    """Rebuild v2 from Mac-held files and create a non-dispatchable reservation."""
    payload = _validate_payload(payload, _CHAPTER_MAP_V2_FIELDS)
    if payload.get("protocol") != "chapter-map-machine-context-v2":
        raise RpcRejected("chapter-map v2 protocol is invalid")
    if payload.get("builder_source_sha256") not in CHAPTER_MAP_V2_BUILDER_SHA256:
        raise RpcRejected("chapter-map v2 builder source is not pinned")
    book_id, chapter_id = payload["book_id"], payload["chapter_id"]
    if not isinstance(chapter_id, str) or not _CHAPTER_ID_RE.fullmatch(chapter_id):
        raise RpcRejected("chapter_id is invalid")
    workdir = _resolve_workdir(Path(root), book_id)
    manifest_path = workdir / "state" / "analysis-manifest.json"

    def rebuild_and_compare(client: dict[str, Any], manifest_bytes: bytes) -> dict[str, Any]:
        import chapter_map_context_v2 as context_v2

        def local_bytes(path: Path, label: str) -> bytes:
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(workdir.resolve()):
                    raise RpcRejected(f"Mac {label} path is unsafe")
                return path.read_bytes()
            except OSError as exc:
                raise RpcRejected(f"Mac {label} is unavailable: {exc}") from None

        state = workdir / "state"
        export_bytes = local_bytes(state / f"chapter-map-input-{chapter_id}.json", "current export")
        source_bytes = local_bytes(workdir / "text.txt", "source")
        script_bytes = local_bytes(workdir / "script.huaben.jsonl", "script")
        memory_bytes = local_bytes(workdir / "book-memory.json", "book memory")
        try:
            manifest_data = json.loads(manifest_bytes.decode("utf-8-sig"),
                                       object_pairs_hook=ledger._unique_json_object)
            budget = _chapter_map_money(client.get("budget_cny"), "budget_cny")
            source_text = context_v2._source_text(source_bytes)
            script_ids, _ = context_v2._chapter_rows(script_bytes, source_text)
            if chapter_id not in script_ids:
                raise RpcRejected("v2 chapter is not in Mac script order")
            current_index = script_ids.index(chapter_id)
            prior_attempts = []
            entries = ledger._chapter_map_entries(manifest_data)
            for prior_id in script_ids[:current_index]:
                entry = entries.get(prior_id)
                if not isinstance(entry, dict) or entry.get("status") != "awaiting_review":
                    raise RpcRejected(f"trusted complete prior machine receipt is unavailable: {prior_id}")
                provenance = entry.get("provenance")
                receipt_provenance = entry.get("receipt_provenance")
                if not isinstance(provenance, dict) or not isinstance(receipt_provenance, dict):
                    raise RpcRejected(f"prior machine receipt provenance is malformed: {prior_id}")
                if (receipt_provenance.get("billing_status") != "verified"
                        or receipt_provenance.get("verified_cost_cny") is None
                        or not isinstance(receipt_provenance.get("billing_evidence_sha256"), str)
                        or not _SHA256_RE.fullmatch(receipt_provenance["billing_evidence_sha256"])):
                    raise RpcRejected(f"prior machine receipt billing is unverified: {prior_id}")
                export_path = Path(provenance.get("export_path", ""))
                receipt_path = Path(receipt_provenance.get("receipt_path", ""))
                if (not export_path.is_absolute() or not receipt_path.is_absolute()
                        or not export_path.resolve().is_relative_to(workdir.resolve())
                        or not receipt_path.resolve().is_relative_to(workdir.resolve())):
                    raise RpcRejected(f"prior machine receipt path escapes Mac workdir: {prior_id}")
                prior_attempts.append({
                    "protocol": provenance.get("protocol"),
                    "export_bytes": local_bytes(export_path, f"prior export {prior_id}"),
                    "receipt_bytes": local_bytes(receipt_path, f"prior receipt {prior_id}"),
                })
            rebuilt = context_v2.build_request(
                export_bytes, source_bytes, script_bytes, memory_bytes, manifest_bytes,
                prior_attempts, budget_cny=budget, trusted_manifest_sha256=_sha256(manifest_bytes),
            )
        except RpcRejected:
            raise
        except Exception as exc:
            raise RpcRejected(f"Mac v2 request reconstruction failed: {type(exc).__name__}") from None
        if rebuilt.get("builder_source_sha256") not in CHAPTER_MAP_V2_BUILDER_SHA256:
            raise RpcRejected("Mac v2 builder source is not pinned")
        compare_fields = _CHAPTER_MAP_V2_FIELDS - {"budget_snapshot_cny"}
        for key in compare_fields:
            expected = rebuilt.get(key)
            supplied = client.get(key)
            if key in ("budget_cny", "estimated_peak_cny"):
                if _chapter_map_money(supplied, key) != expected:
                    raise RpcRejected(f"Windows v2 {key} does not match Mac reconstruction")
            elif supplied != expected:
                raise RpcRejected(f"Windows v2 {key} does not match Mac reconstruction")
        verified = {key: rebuilt[key] for key in compare_fields}
        verified["estimated_peak_cny"] = format(rebuilt["estimated_peak_cny"], "f")
        verified["budget_cny"] = format(rebuilt["budget_cny"], "f")
        verified["budget_snapshot_cny"] = client["budget_snapshot_cny"]
        return verified

    try:
        import chapter_map_v2_ledger
        result = chapter_map_v2_ledger.reserve(
            manifest_path, workdir / ".console.lock", payload,
            budget_snapshot_cny=payload["budget_snapshot_cny"],
            manifest_snapshot_sha256=payload["manifest_snapshot_sha256"],
            request_verifier=rebuild_and_compare,
        )
    except (ledger.ManifestError, chapter_map_v2_ledger.LedgerV2Error) as exc:
        raise RpcRejected(str(exc)) from None
    return result


def finalize_chapter_map_v2(payload: Any, *, root: str | Path = DEFAULT_ROOT) -> dict[str, Any]:
    """Receipt path is present for protocol completeness; dispatch is not implemented."""
    payload = _validate_payload(payload, _CHAPTER_MAP_V2_RECEIPT_FIELDS)
    book_id, chapter_id = payload["book_id"], payload["chapter_id"]
    if not isinstance(chapter_id, str) or not _CHAPTER_ID_RE.fullmatch(chapter_id):
        raise RpcRejected("chapter_id is invalid")
    workdir = _resolve_workdir(Path(root), book_id)
    manifest_path = workdir / "state" / "analysis-manifest.json"
    receipt_path = workdir / "state" / f"chapter-map-v2-state-{chapter_id}.json"
    try:
        import chapter_map_v2_ledger
        result = chapter_map_v2_ledger.record_receipt(
            manifest_path, workdir / ".console.lock", chapter_id, receipt_path)
    except (ledger.ManifestError, chapter_map_v2_ledger.LedgerV2Error) as exc:
        raise RpcRejected(str(exc)) from None
    entry = ledger._chapter_map_v2_entries(result)[chapter_id]
    return {"protocol": "chapter-map-machine-context-v2", "status": entry["status"],
            "attempt_id": entry["provenance"]["attempt_id"],
            "receipt_sha256": entry["receipt_provenance"]["receipt_sha256"]}


def finalize(
    payload: Any,
    *,
    root: str | Path = DEFAULT_ROOT,
) -> dict[str, Any]:
    """Reconcile only the server-side receipt matching the reserved hashes."""

    payload = _validate_payload(payload, _FINALIZE_FIELDS)
    book_id, chapter_id = payload["book_id"], payload["chapter_id"]
    if type(payload["execution_ended"]) is not bool:
        raise RpcRejected("execution_ended must be a boolean")
    workdir = _resolve_workdir(Path(root), book_id)
    manifest_path, _, receipt_path = _chapter_paths(workdir, chapter_id)
    with _book_lock(workdir):
        current = ledger.load_manifest(manifest_path)
        _, chapter = ledger._chapter(current, chapter_id)
        provenance = chapter.get("request_provenance")
        if not isinstance(provenance, dict):
            raise RpcRejected("manifest has no reserved request for this chapter")
        if (provenance.get("book_id") != book_id
                or provenance.get("job_hash") != payload["job_hash"]
                or provenance.get("request_body_sha256") != payload["request_body_sha256"]):
            raise RpcRejected("finalize hashes do not match the reserved request")
        if chapter["status"] in ("awaiting_review", "billing_unknown"):
            expected_receipt_hash = (
                chapter.get("receipt_provenance", {}).get("receipt_sha256")
                if isinstance(chapter.get("receipt_provenance"), dict)
                else chapter.get("billing_unknown", {}).get("receipt_sha256")
                if isinstance(chapter.get("billing_unknown"), dict)
                else None
            )
            try:
                actual_receipt_hash = _sha256(receipt_path.read_bytes())
            except OSError:
                actual_receipt_hash = None
            if expected_receipt_hash and actual_receipt_hash == expected_receipt_hash:
                return {
                    "status": chapter["status"],
                    "attempt_id": provenance.get("attempt_id"),
                    "receipt_sha256": expected_receipt_hash,
                    "idempotent_replay": True,
                }
            raise RpcRejected("manifest already finalized with a different or missing receipt")
        if chapter["status"] != "in_progress":
            raise RpcRejected("chapter is not awaiting a terminal receipt")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RpcRejected(f"server-side worker receipt is unavailable or invalid: {exc}") from None
        if (receipt.get("job_hash") != payload["job_hash"]
                or receipt.get("request_body_sha256") != payload["request_body_sha256"]):
            raise RpcRejected("server-side receipt hashes do not match the reserved request")
        result = ledger.record_profile_receipt(
            manifest_path, chapter_id, receipt_path, execution_ended=payload["execution_ended"]
        )
        chapter_after = result["chapters"][next(i for i, row in enumerate(result["chapters"])
                                                   if row["chapter_id"] == chapter_id)]
        receipt_proof = chapter_after.get("receipt_provenance") or chapter_after.get("billing_unknown") or {}
        return {
            "status": chapter_after["status"],
            "attempt_id": provenance.get("attempt_id"),
            "receipt_sha256": receipt_proof.get("receipt_sha256"),
            "idempotent_replay": False,
        }


def dispatch(action: str, payload: Any, **config: Any) -> dict[str, Any]:
    """Small RPC surface; unknown actions and payload fields are rejected."""

    if action == "reserve_profile_request":
        return reserve(payload, **config)
    if action == "finalize_profile_request":
        return finalize(payload, **config)
    if action == "reserve_chapter_map_v1":
        return reserve_chapter_map_v1(payload, **config)
    if action == "finalize_chapter_map_v1":
        return finalize_chapter_map_v1(payload, **config)
    if action == "reserve_chapter_map_v2":
        return reserve_chapter_map_v2(payload, **config)
    if action == "finalize_chapter_map_v2":
        return finalize_chapter_map_v2(payload, **config)
    raise RpcRejected("action is not allowlisted")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: profile_manifest_rpc.py ACTION")
    try:
        # JSON decimal fields are parsed without binary-float rounding. The v2
        # reserve contract sends budget_cny and estimated_peak_cny as JSON
        # numbers; the ledger receives Decimal values from this entry point.
        payload = json.loads(
            sys.stdin.read() or "{}",
            parse_float=Decimal,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON number: {value}")),
        )
        data = dispatch(sys.argv[1], payload)
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, separators=(",", ":")))
    except Exception as exc:
        # Never echo the RPC payload; it must not contain credentials.
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
