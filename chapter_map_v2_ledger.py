"""Offline Mac-side ledger primitives for chapter-map machine-context v2.

No provider dispatch or receipt finalization is implemented here. Reservations
are unverified accounting records and cannot authorize a model request.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

import analysis_manifest as ledger

PROTOCOL = "chapter-map-machine-context-v2"
UNRESOLVED = frozenset({"reserved_not_dispatched", "in_progress", "awaiting_review", "billing_unknown"})
HASH_FIELDS = ("job_hash", "source_sha256", "script_sha256", "memory_sha256",
               "manifest_snapshot_sha256", "chapter_input_sha256", "machine_context_sha256",
               "machine_context_included_sha256", "machine_context_ledger_provenance_sha256",
               "request_body_sha256", "system_prompt_sha256", "user_prompt_sha256",
               "builder_source_sha256")
CHAIN_FIELDS = frozenset({"protocol", "ledger_entry_sha256", "chapter_id", "attempt_id", "job_hash",
                          "chapter_sha256", "receipt_sha256", "request_body_sha256",
                          "worker_source_sha256", "summary_sha256", "evidence_sha256", "status"})


class LedgerV2Error(ledger.ProgressBlocked):
    pass


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _amount(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise LedgerV2Error(f"{field} must be an exact decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise LedgerV2Error(f"{field} is invalid") from None
    if not result.is_finite() or result <= 0:
        raise LedgerV2Error(f"{field} must be positive and finite")
    return result


@contextmanager
def _book_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                f.write(b"\0"); f.flush()
            f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            try: yield
            finally:
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _attempt_id(meta: Mapping[str, Any]) -> str:
    identity = {k: meta[k] for k in ("protocol", "book_id", "chapter_id", "job_hash",
                                      "request_body_sha256", "builder_source_sha256",
                                      "manifest_snapshot_sha256", "machine_context_sha256")}
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return _digest(raw)


def reserve(manifest_path: str | Path, book_lock_path: str | Path, metadata: Mapping[str, Any], *,
            budget_snapshot_cny: str | int | Decimal, manifest_snapshot_sha256: str,
            request_verifier=None) -> dict[str, Any]:
    """Atomically reserve a builder request; both snapshots are rechecked under lock."""
    target, book_lock = Path(manifest_path), Path(book_lock_path)
    if not isinstance(metadata, Mapping) or metadata.get("protocol") != PROTOCOL:
        raise LedgerV2Error("v2 request metadata is invalid")
    if any(not isinstance(metadata.get(k), str) or len(metadata[k]) != 64
           or any(c not in "0123456789abcdef" for c in metadata[k]) for k in HASH_FIELDS):
        raise LedgerV2Error("v2 request hashes are invalid")
    for k in ("book_id", "chapter_id", "requested_model"):
        if not isinstance(metadata.get(k), str) or not metadata[k]:
            raise LedgerV2Error(f"{k} is invalid")
    if metadata["requested_model"] != "deepseek-flash":
        raise LedgerV2Error("v2 model is not allowlisted")
    estimate = _amount(metadata.get("estimated_peak_cny"), "estimated_peak_cny")
    budget = _amount(metadata.get("budget_cny"), "budget_cny")
    if estimate > budget or budget > Decimal("10"):
        raise LedgerV2Error("v2 request budget is invalid")
    caller_balance = _amount(budget_snapshot_cny, "budget snapshot")
    if manifest_snapshot_sha256 != metadata["manifest_snapshot_sha256"]:
        raise LedgerV2Error("caller manifest snapshot does not match request provenance")
    with _book_lock(book_lock):
        # Lock ordering is always book lock, then manifest sidecar lock.
        with ledger._file_lock(target.with_suffix(target.suffix + ".lock")):
            raw_manifest = target.read_bytes()
            data = ledger._read(target)
            if data.get("status") != "active" or data.get("profile_manifest_mode_enabled") is not True:
                raise LedgerV2Error("active shared manifest mode is required")
            if data.get("currency") != "CNY" or data.get("book_id") != metadata["book_id"]:
                raise LedgerV2Error("manifest book or currency does not match request")
            chapter_index, current_chapter = ledger._chapter(data, metadata["chapter_id"])
            entries = ledger._chapter_map_v2_entries(data)
            prior = entries.get(metadata["chapter_id"])
            attempt = _attempt_id(metadata)
            if prior:
                prior_provenance = prior.get("provenance", {})
                stored_metadata = {k: prior_provenance.get(k) for k in metadata}
                for money_key in ("estimated_peak_cny", "budget_cny"):
                    try:
                        stored_metadata[money_key] = Decimal(str(stored_metadata[money_key]))
                        supplied_amount = Decimal(str(metadata[money_key]))
                    except (InvalidOperation, ValueError, KeyError):
                        raise LedgerV2Error("chapter already has malformed request budget metadata") from None
                    if not stored_metadata[money_key].is_finite() or stored_metadata[money_key] != supplied_amount:
                        raise LedgerV2Error("chapter already has a different v2 attempt")
                    metadata = dict(metadata)
                    metadata[money_key] = supplied_amount
                if (prior_provenance.get("attempt_id") != attempt or stored_metadata != dict(metadata)
                        or prior_provenance.get("budget_snapshot_cny") != format(caller_balance, "f")):
                    raise LedgerV2Error("chapter already has a different v2 attempt")
                return {"protocol": PROTOCOL, "status": prior["status"], "attempt_id": attempt,
                        "reservation": prior["reservation"], "dispatch_allowed": False,
                        "idempotent_replay": True}
            if ledger._atomic_dispatch_gate_entries(data):
                raise LedgerV2Error("atomic experiment dispatch-gate attempt blocks legacy v2 reserve")
            if request_verifier is not None:
                metadata = dict(request_verifier(dict(metadata), raw_manifest))
                estimate = _amount(metadata.get("estimated_peak_cny"), "estimated_peak_cny")
                budget = _amount(metadata.get("budget_cny"), "budget_cny")
                if estimate > budget or budget > Decimal("10"):
                    raise LedgerV2Error("Mac-rebuilt v2 request budget is invalid")
                caller_balance = _amount(budget_snapshot_cny, "budget snapshot")
                if manifest_snapshot_sha256 != metadata.get("manifest_snapshot_sha256"):
                    raise LedgerV2Error("Mac-rebuilt manifest snapshot does not match reserve request")
            if _digest(raw_manifest) != manifest_snapshot_sha256:
                raise LedgerV2Error("manifest snapshot is stale")
            book_limit = _amount(data["estimated_budget_limit"], "book limit")
            reserved_before = ledger._reserved(data)
            if reserved_before > book_limit:
                raise LedgerV2Error("existing reservations exceed the book limit")
            remaining = book_limit - reserved_before
            if remaining <= 0 or remaining != caller_balance:
                raise LedgerV2Error("caller budget snapshot is stale")
            if any(e.get("status") in UNRESOLVED for e in entries.values()):
                raise LedgerV2Error("another v2 reservation is unresolved")
            if any(ch["status"] in ("in_progress", "awaiting_review", "billing_unknown") for ch in data["chapters"]):
                raise LedgerV2Error("profile request is unresolved")
            hard_v1_unresolved = [ident for ident, entry in ledger._chapter_map_entries(data).items()
                                  if entry.get("status") in {"reserved_not_dispatched", "in_progress", "billing_unknown"}]
            if hard_v1_unresolved:
                raise LedgerV2Error(
                    f"chapter-map-v1 has unresolved chapter(s): {', '.join(sorted(hard_v1_unresolved))}")
            if reserved_before + estimate > book_limit:
                raise ledger.BudgetExceeded("v2 reservation exceeds the shared book budget")
            if current_chapter.get("status") != "pending":
                raise LedgerV2Error("v2 reservation requires a pending manifest chapter")
            chain = metadata.get("machine_context_provenance")
            if not isinstance(chain, list) or any(not isinstance(row, dict) or set(row) != CHAIN_FIELDS for row in chain):
                raise LedgerV2Error("ordered machine receipt provenance is missing or malformed")
            expected_prior = [ch["chapter_id"] for ch in data["chapters"][:chapter_index]]
            if [row.get("chapter_id") for row in chain] != expected_prior:
                raise LedgerV2Error("machine receipt chain does not cover every earlier chapter in manifest order")
            if chain and request_verifier is None:
                raise LedgerV2Error("prior machine receipts require Mac-side request reconstruction")
            for row in chain:
                if (row.get("protocol") != "chapter-map-v1"
                        or row.get("status") != "machine_generated_unreviewed"
                        or any(not isinstance(row.get(key), str) or len(row[key]) != 64
                               or any(c not in "0123456789abcdef" for c in row[key])
                               for key in CHAIN_FIELDS - {"protocol", "chapter_id", "status"})):
                    raise LedgerV2Error("machine receipt chain contains an untrusted entry")
            if chain:
                v1_entries = ledger._chapter_map_entries(data)
                workdir = target.parent.parent.resolve()
                try:
                    import profile_manifest_rpc
                    worker_pins = profile_manifest_rpc.CHAPTER_MAP_WORKER_SHA256
                except Exception:
                    raise LedgerV2Error("trusted v1 worker pin is unavailable") from None
                for row in chain:
                    entry = v1_entries.get(row["chapter_id"])
                    if not isinstance(entry, dict) or entry.get("status") != "awaiting_review":
                        raise LedgerV2Error("prior machine receipt is not complete and unreviewed")
                    provenance = entry.get("provenance")
                    proof = entry.get("receipt_provenance")
                    if (not isinstance(provenance, dict) or not isinstance(proof, dict)
                            or provenance.get("protocol") != "chapter-map-v1"
                            or provenance.get("dispatch_issued") is not True
                            or provenance.get("worker_source_sha256") not in worker_pins
                            or proof.get("billing_status") != "verified"):
                        raise LedgerV2Error("prior machine receipt provenance or billing is unverified")
                    try:
                        verified_cost = ledger._money(proof.get("verified_cost_cny"), field="verified prior bill")
                    except ledger.ManifestError:
                        raise LedgerV2Error("prior machine receipt verified bill is missing") from None
                    evidence_hash = proof.get("billing_evidence_sha256")
                    if (not isinstance(evidence_hash, str) or len(evidence_hash) != 64
                            or any(c not in "0123456789abcdef" for c in evidence_hash)):
                        raise LedgerV2Error("prior machine receipt billing evidence is missing")
                    reconciled = any(
                        isinstance(event, dict)
                        and event.get("event") == "chapter_map_v1_billing_reconciled"
                        and event.get("chapter_id") == row["chapter_id"]
                        and isinstance(event.get("details"), dict)
                        and event["details"].get("attempt_id") == provenance.get("attempt_id")
                        and event["details"].get("receipt_sha256") == proof.get("receipt_sha256")
                        and event["details"].get("billing_evidence_sha256") == evidence_hash
                        and str(event["details"].get("verified_cost_cny")) == str(proof.get("verified_cost_cny"))
                        for event in data.get("events", []))
                    if not reconciled:
                        raise LedgerV2Error("prior machine receipt has no trusted billing reconciliation event")
                    for key in ("attempt_id", "job_hash", "request_body_sha256", "worker_source_sha256"):
                        if row.get(key) != provenance.get(key):
                            raise LedgerV2Error("prior machine receipt chain does not match Mac ledger")
                    if (proof.get("receipt_sha256") != row.get("receipt_sha256")
                            or ledger._canonical_hash(entry) != row.get("ledger_entry_sha256")):
                        raise LedgerV2Error("prior machine receipt or ledger entry hash mismatch")
                    if (provenance.get("source_sha256") != metadata.get("source_sha256")
                            or provenance.get("script_sha256") != metadata.get("script_sha256")):
                        raise LedgerV2Error("prior machine receipt belongs to a different source/script version")
                    for path_key, proof_key, hash_key in (("export_path", None, "job_hash"),
                                                         ("receipt_path", proof, "receipt_sha256")):
                        raw_path = provenance.get(path_key) if proof_key is None else proof.get(path_key)
                        if not isinstance(raw_path, str) or not raw_path:
                            raise LedgerV2Error("prior export or receipt path is missing")
                        try:
                            candidate = Path(raw_path).resolve()
                            if not candidate.is_relative_to(workdir):
                                raise LedgerV2Error("prior receipt path escapes Mac workdir")
                            actual_hash = _digest(candidate.read_bytes())
                        except OSError:
                            raise LedgerV2Error("prior export or receipt bytes are unavailable") from None
                        expected_hash = (provenance.get(hash_key) if proof_key is None else proof.get(hash_key))
                        if actual_hash != expected_hash:
                            raise LedgerV2Error("prior export or receipt bytes changed")
            v1_unresolved = [ident for ident, entry in ledger._chapter_map_entries(data).items()
                             if entry.get("status") in ledger._CHAPTER_MAP_UNRESOLVED
                             and not (entry.get("status") == "awaiting_review"
                                      and ident in {row["chapter_id"] for row in chain})]
            if v1_unresolved:
                raise LedgerV2Error(
                    f"chapter-map-v1 has unresolved chapter(s): {', '.join(sorted(v1_unresolved))}")
            reservation = {"kind": "estimated_peak", "currency": "CNY", "amount_cny": format(estimate, "f"),
                           "billing_status": "unverified", "scope": "single_request"}
            provenance = dict(metadata)
            provenance.update({"attempt_id": attempt, "reservation": reservation,
                               "budget_snapshot_cny": format(caller_balance, "f"),
                               "dispatch_issued": False, "status": "reserved_not_dispatched"})
            data.setdefault("chapter_map_machine_context_v2", {"protocol": PROTOCOL, "chapters": {}})
            data["chapter_map_machine_context_v2"]["chapters"][metadata["chapter_id"]] = {
                "status": "reserved_not_dispatched", "reservation": reservation,
                "provenance": provenance, "receipt_provenance": None}
            ledger._record(data, "chapter_map_v2_reserved_not_dispatched", metadata["chapter_id"], attempt_id=attempt)
            ledger._atomic_write(target, data)
            return {"protocol": PROTOCOL, "status": "reserved_not_dispatched", "attempt_id": attempt,
                    "reservation": reservation, "dispatch_allowed": False, "budget_reserved": True,
                    "idempotent_replay": False}


def record_receipt(manifest_path: str | Path, book_lock_path: str | Path, chapter_id: str,
                   receipt_path: str | Path) -> dict[str, Any]:
    """Fail closed: neither dispatch nor receipt finalization exists in v2 yet."""
    raise LedgerV2Error("chapter-map v2 dispatch is disabled; receipt finalization is unavailable")


def _reserved(data):
    return ledger._reserved(data)
