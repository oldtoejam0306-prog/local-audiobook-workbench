import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import chapter_map_context_v2 as context_v2
import analysis_manifest as ledger


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class ChapterMapMachineContextV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chapter-map-context-v2-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        first = "第1章\n林舟出发。\n"
        second = "第2章\n顾晴见到林舟。\n"
        self.source = (first + second).encode("utf-8")
        self.first_text, self.current_text = first, second
        self.script_rows = [
            {"type": "chapter", "id": "c000", "src": [0, len(first)]},
            {"type": "chapter", "id": "c001", "src": [len(first), len(first + second)]},
        ]
        self.script = b"".join(compact(row) + b"\n" for row in self.script_rows)
        self.memory = {
            "book_id": "book-v2-test",
            "characters": {"林舟": {"aliases": ["阿舟"], "description": "青年船员"}},
            "chapter_summaries": [],
        }
        self.memory_bytes = compact(self.memory)
        self.source_hash = hashlib.sha256(self.source).hexdigest()
        self.script_hash = hashlib.sha256(self.script).hexdigest()
        self.memory_hash = hashlib.sha256(self.memory_bytes).hexdigest()
        self.prior_export = {
            "version": 1, "book_id": "book-v2-test", "chapter_id": "c000",
            "chapter_text": self.first_text, "source_sha256": self.source_hash,
            "script_sha256": self.script_hash, "memory_sha256": self.memory_hash,
            "characters": self.memory["characters"], "approved_summaries": [],
        }
        self.prior_export_bytes = compact(self.prior_export)
        prior_request = ledger._chapter_map_request_for_export(self.prior_export, 0.5)
        self.prior_request_hash = prior_request["request_body_sha256"]
        self.prior_worker_hash = "ccc83ee9a48869eb5b1134e8f9ade521b1a52e714fcdbb4fd148f4de362c4539"
        self.prior_attempt_id = context_v2._canonical_hash({
            "protocol": "chapter-map-v1", "book_id": "book-v2-test", "chapter_id": "c000",
            "job_hash": hashlib.sha256(self.prior_export_bytes).hexdigest(),
            "request_body_sha256": self.prior_request_hash,
            "worker_source_sha256": self.prior_worker_hash,
        })
        self.prior_result = {
            "schema_version": "chapter-map-v1",
            "characters": [{
                "name": "林舟", "evidence": {"quote": "林舟出发"}, "aliases": [], "voice": [],
            }],
            "relations": [],
            "summary": {"text": "机器候选：林舟在第一章出发。", "evidence": {"quote": "林舟出发"}},
        }
        estimate = format(prior_request["estimated_peak_cny"], "f")
        self.prior_receipt = {
            "protocol": "chapter-map-v1",
            "attempt_id": self.prior_attempt_id,
            "job_hash": hashlib.sha256(self.prior_export_bytes).hexdigest(),
            "source_sha256": self.source_hash,
            "worker_source_sha256": self.prior_worker_hash,
            "request_body_sha256": self.prior_request_hash,
            "book_id": "book-v2-test", "chapter_id": "c000",
            "status": "complete_needs_review",
            "response_id": "v1-prior-response-c000",
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            "budget_reservation": {"kind": "estimated_peak", "currency": "CNY", "amount_cny": estimate,
                                   "billing_status": "unverified", "scope": "single_request"},
            "budget_cny": 0.5,
            "estimated_peak_cost_cny": 0.00012,
            "model": "deepseek-flash", "requested_model": "deepseek-flash",
            "actual_model": "deepseek-flash", "finish_reason": "stop",
            "request_body_sha256": self.prior_request_hash,
            "system_prompt_sha256": prior_request["system_prompt_sha256"],
            "user_prompt_sha256": prior_request["user_prompt_sha256"],
            "raw_response": json.dumps(self.prior_result, ensure_ascii=False, separators=(",", ":")),
            "result": self.prior_result,
        }
        self.prior_receipt_bytes = compact(self.prior_receipt)
        self.manifest = {
            "schema_version": 1,
            "book_id": "book-v2-test",
            "status": "active",
            "profile_manifest_mode_enabled": True,
            "currency": "CNY",
            "estimated_budget_limit": "1.00",
            "chapters": [
                {"chapter_id": "c000", "status": "pending", "approved_summary": None},
                {"chapter_id": "c001", "status": "pending", "approved_summary": None},
            ],
            "chapter_map_v1": {
                "protocol": "chapter-map-v1",
                "chapters": {"c000": {
                    "status": "awaiting_review",
                    "provenance": {
                        "protocol": "chapter-map-v1", "attempt_id": self.prior_attempt_id,
                        "book_id": "book-v2-test", "chapter_id": "c000",
                        "job_hash": hashlib.sha256(self.prior_export_bytes).hexdigest(),
                        "export_sha256": hashlib.sha256(self.prior_export_bytes).hexdigest(),
                        "source_sha256": self.source_hash, "script_sha256": self.script_hash,
                        "request_body_sha256": self.prior_request_hash,
                        "system_prompt_sha256": prior_request["system_prompt_sha256"],
                        "user_prompt_sha256": prior_request["user_prompt_sha256"],
                        "worker_source_sha256": self.prior_worker_hash, "dispatch_issued": True,
                        "memory_sha256": self.memory_hash,
                        "approved_summaries_sha256": ledger._canonical_hash([]),
                        "estimated_peak_cny": estimate,
                    },
                    "receipt_provenance": {
                        "protocol": "chapter-map-v1", "attempt_id": self.prior_attempt_id,
                        "receipt_sha256": hashlib.sha256(self.prior_receipt_bytes).hexdigest(),
                        "job_hash": hashlib.sha256(self.prior_export_bytes).hexdigest(),
                        "request_body_sha256": self.prior_request_hash,
                        "provider_response_id": "v1-prior-response-c000",
                        "billing_status": "verified", "verified_cost_cny": "0.001",
                        "billing_evidence_sha256": "e" * 64,
                    },
                }},
            },
            "events": [
                {"event": "chapter_map_v1_reserved_not_dispatched", "chapter_id": "c000",
                 "details": {"attempt_id": self.prior_attempt_id}},
                {"event": "chapter_map_v1_billing_reconciled", "chapter_id": "c000",
                 "details": {"attempt_id": self.prior_attempt_id,
                             "receipt_sha256": hashlib.sha256(self.prior_receipt_bytes).hexdigest(),
                             "billing_evidence_sha256": "e" * 64, "verified_cost_cny": "0.001"}},
            ],
        }
        self.manifest_bytes = compact(self.manifest)
        self.current_export = {
            "version": 1, "book_id": "book-v2-test", "chapter_id": "c001",
            "chapter_text": self.current_text, "source_sha256": self.source_hash,
            "script_sha256": self.script_hash, "memory_sha256": self.memory_hash,
            "characters": self.memory["characters"], "approved_summaries": [],
        }
        self.current_export_bytes = compact(self.current_export)

    def build(self, **overrides):
        values = {
            "current_export_bytes": self.current_export_bytes,
            "source_bytes": self.source,
            "script_bytes": self.script,
            "book_memory_bytes": self.memory_bytes,
            "manifest_snapshot_bytes": self.manifest_bytes,
            "prior_attempts": [{"protocol": "chapter-map-v1", "export_bytes": self.prior_export_bytes,
                                "receipt_bytes": self.prior_receipt_bytes}],
            "budget_cny": 0.5,
        }
        values.update(overrides)
        values.setdefault("trusted_manifest_sha256",
                          hashlib.sha256(values["manifest_snapshot_bytes"]).hexdigest())
        return context_v2.build_request(**values)

    def test_builds_machine_context_bound_to_unreviewed_receipt_and_hashes(self):
        result = self.build()
        self.assertEqual(result["protocol"], context_v2.PROTOCOL)
        self.assertEqual(result["machine_context"], [{
            "chapter_id": "c000",
            "summary": self.prior_result["summary"]["text"],
            "evidence": {"quote": "林舟出发"},
            "status": "machine_generated_unreviewed",
        }])
        proof = result["machine_context_provenance"][0]
        self.assertEqual(proof["receipt_sha256"], hashlib.sha256(self.prior_receipt_bytes).hexdigest())
        self.assertEqual(proof["attempt_id"], self.prior_attempt_id)
        self.assertEqual(proof["status"], "machine_generated_unreviewed")
        self.assertRegex(result["machine_context_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("未经人工审核", result["body_text"])
        self.assertIn("绝非事实", result["body_text"])
        self.assertEqual(result["dispatch_status"], "blocked_offline_machine_context_v2_no_dispatch")
        self.assertFalse(result["budget_reserved"])
        with self.assertRaisesRegex(context_v2.ContextV2Error, "dispatch is disabled"):
            context_v2.dispatch(result)

    def test_rejects_missing_skipped_future_or_reordered_prior_receipts(self):
        with self.assertRaisesRegex(context_v2.ContextV2Error, "each non-approved previous chapter"):
            self.build(prior_attempts=[])
        future = {"protocol": "chapter-map-v1", "export_bytes": self.current_export_bytes,
                  "receipt_bytes": self.prior_receipt_bytes}
        with self.assertRaises(context_v2.ContextV2Error):
            self.build(prior_attempts=[future])
        wrong = dict(self.manifest)
        wrong["chapters"] = list(reversed(wrong["chapters"]))
        with self.assertRaisesRegex(context_v2.ContextV2Error, "chapter order"):
            self.build(manifest_snapshot_bytes=compact(wrong))

    def test_rejects_changed_receipt_manifest_binding_or_source_hash(self):
        tampered = dict(self.prior_receipt, response_id="different-provider-response")
        with self.assertRaisesRegex(context_v2.ContextV2Error, "receipt_sha256"):
            self.build(prior_attempts=[{"protocol": "chapter-map-v1", "export_bytes": self.prior_export_bytes,
                                        "receipt_bytes": compact(tampered)}])
        wrong_status = json.loads(self.manifest_bytes)
        wrong_status["chapter_map_v1"]["chapters"]["c000"]["status"] = "billing_unknown"
        with self.assertRaisesRegex(context_v2.ContextV2Error, "complete and unreviewed"):
            self.build(manifest_snapshot_bytes=compact(wrong_status))
        with self.assertRaisesRegex(context_v2.ContextV2Error, "source/script/memory hash"):
            self.build(source_bytes=self.source + b"changed")

    def test_untrusted_manifest_digest_and_arbitrary_worker_hash_fail_closed(self):
        with self.assertRaisesRegex(context_v2.ContextV2Error, "separately trusted Mac hash"):
            self.build(trusted_manifest_sha256="0" * 64)
        manifest = json.loads(self.manifest_bytes)
        manifest["chapter_map_v1"]["chapters"]["c000"]["provenance"]["worker_source_sha256"] = "f" * 64
        with self.assertRaisesRegex(context_v2.ContextV2Error, "worker hash is not allowlisted"):
            self.build(manifest_snapshot_bytes=compact(manifest))

    def test_prior_export_cannot_smuggle_future_approved_summary(self):
        prior_export = dict(self.prior_export)
        prior_export["approved_summaries"] = [{
            "chapter_id": "c001", "summary": "未来章节的内容。", "source": "manual", "status": "approved"
        }]
        export_bytes = compact(prior_export)
        job_hash = hashlib.sha256(export_bytes).hexdigest()
        prior_request = ledger._chapter_map_request_for_export(prior_export, 0.5)
        new_attempt = context_v2._canonical_hash({
            "protocol": "chapter-map-v1", "book_id": "book-v2-test", "chapter_id": "c000",
            "job_hash": job_hash, "request_body_sha256": prior_request["request_body_sha256"],
            "worker_source_sha256": self.prior_worker_hash,
        })
        receipt = dict(self.prior_receipt)
        receipt.update({
            "job_hash": job_hash, "attempt_id": new_attempt,
            "request_body_sha256": prior_request["request_body_sha256"],
            "system_prompt_sha256": prior_request["system_prompt_sha256"],
            "user_prompt_sha256": prior_request["user_prompt_sha256"],
        })
        new_receipt_bytes = compact(receipt)
        manifest = json.loads(self.manifest_bytes)
        entry = manifest["chapter_map_v1"]["chapters"]["c000"]
        provenance = entry["provenance"]
        provenance.update({
            "job_hash": job_hash, "export_sha256": job_hash, "attempt_id": new_attempt,
            "request_body_sha256": prior_request["request_body_sha256"],
            "system_prompt_sha256": prior_request["system_prompt_sha256"],
            "user_prompt_sha256": prior_request["user_prompt_sha256"],
            "approved_summaries_sha256": ledger._canonical_hash(prior_export["approved_summaries"]),
            "estimated_peak_cny": format(prior_request["estimated_peak_cny"], "f"),
        })
        entry["receipt_provenance"].update({
            "attempt_id": new_attempt,
            "receipt_sha256": hashlib.sha256(new_receipt_bytes).hexdigest(),
            "job_hash": job_hash,
            "request_body_sha256": prior_request["request_body_sha256"],
        })
        manifest["events"][0]["details"]["attempt_id"] = new_attempt
        with self.assertRaisesRegex(context_v2.ContextV2Error, "malformed or future context"):
            self.build(
                manifest_snapshot_bytes=compact(manifest),
                prior_attempts=[{"protocol": "chapter-map-v1", "export_bytes": export_bytes,
                                "receipt_bytes": new_receipt_bytes}],
            )

    def test_machine_receipt_never_becomes_human_approved_context(self):
        manifest = json.loads(self.manifest_bytes)
        manifest["chapter_map_v1"]["chapters"]["c000"]["status"] = "approved"
        with self.assertRaisesRegex(context_v2.ContextV2Error, "complete and unreviewed"):
            self.build(manifest_snapshot_bytes=compact(manifest))
        self.assertEqual(manifest["chapters"][0]["status"], "pending")
        self.assertIsNone(manifest["chapters"][0]["approved_summary"])

    def test_profile_memory_and_approved_summary_are_not_prompt_context(self):
        approved = {"chapter_id": "c000", "summary": "用户审核通过的摘要。",
                    "source": "manual", "status": "approved"}
        current = dict(self.current_export)
        current["characters"] = {"伪造确认人物": {"aliases": [], "description": "not approved"}}
        current["approved_summaries"] = [approved]
        manifest = json.loads(self.manifest_bytes)
        manifest["chapters"][0].update({"status": "approved", "approved_summary": approved["summary"]})
        result = self.build(
            current_export_bytes=compact(current),
            manifest_snapshot_bytes=compact(manifest),
        )
        self.assertEqual(len(result["machine_context"]), 1)
        self.assertNotIn("用户审核通过的摘要。", result["body_text"])
        self.assertNotIn("伪造确认人物", result["body_text"])
        self.assertIn("本版本不读取可编辑 memory 字段", result["body_text"])
        self.assertFalse(result["profile_memory_context_used"])
        self.assertEqual(result["profile_memory_context_used"], False)

    def test_prior_receipts_do_not_require_current_profile_approval_or_memory_snapshot(self):
        changed_memory = dict(self.memory)
        changed_memory["characters"] = {"later profile edit": {"aliases": [], "description": "changed"}}
        changed_memory_bytes = compact(changed_memory)
        current = dict(self.current_export,
                       memory_sha256=hashlib.sha256(changed_memory_bytes).hexdigest())
        result = self.build(current_export_bytes=compact(current), book_memory_bytes=changed_memory_bytes)
        self.assertEqual(result["machine_context"][0]["chapter_id"], "c000")
        self.assertEqual(self.manifest["chapters"][0]["status"], "pending")

    def test_token_usage_without_verified_currency_billing_cannot_extend_chain(self):
        manifest = json.loads(self.manifest_bytes)
        proof = manifest["chapter_map_v1"]["chapters"]["c000"]["receipt_provenance"]
        proof.update({"billing_status": "unverified"})
        with self.assertRaisesRegex(context_v2.ContextV2Error, "billing is unknown or unverified"):
            self.build(manifest_snapshot_bytes=compact(manifest))

    def test_prior_receipts_do_not_require_current_profile_approval_or_memory_snapshot(self):
        changed_memory = dict(self.memory)
        changed_memory["characters"] = {"later profile edit": {"aliases": [], "description": "changed"}}
        changed_memory_bytes = compact(changed_memory)
        current = dict(self.current_export,
                       memory_sha256=hashlib.sha256(changed_memory_bytes).hexdigest())
        result = self.build(current_export_bytes=compact(current), book_memory_bytes=changed_memory_bytes)
        self.assertEqual(result["machine_context"][0]["chapter_id"], "c000")
        self.assertEqual(self.manifest["chapters"][0]["status"], "pending")

    def test_token_usage_without_verified_currency_billing_cannot_extend_chain(self):
        manifest = json.loads(self.manifest_bytes)
        proof = manifest["chapter_map_v1"]["chapters"]["c000"]["receipt_provenance"]
        proof.update({"billing_status": "unverified"})
        with self.assertRaisesRegex(context_v2.ContextV2Error, "billing is unknown or unverified"):
            self.build(manifest_snapshot_bytes=compact(manifest))

    def test_manifest_snapshot_requires_out_of_band_mac_digest(self):
        with self.assertRaisesRegex(context_v2.ContextV2Error, "separately trusted Mac hash"):
            self.build(trusted_manifest_sha256="0" * 64)

    def test_prior_export_future_summary_is_rejected_even_when_receipt_and_hashes_rebound(self):
        prior_export = dict(self.prior_export)
        prior_export["approved_summaries"] = [{
            "chapter_id": "c001", "summary": "未来章节的内容。", "source": "manual", "status": "approved"
        }]
        export_bytes = compact(prior_export)
        job_hash = hashlib.sha256(export_bytes).hexdigest()
        request = ledger._chapter_map_request_for_export(prior_export, 0.5)
        attempt_id = context_v2._canonical_hash({
            "protocol": "chapter-map-v1", "book_id": "book-v2-test", "chapter_id": "c000",
            "job_hash": job_hash, "request_body_sha256": request["request_body_sha256"],
            "worker_source_sha256": self.prior_worker_hash,
        })
        receipt = dict(self.prior_receipt)
        receipt.update({
            "job_hash": job_hash, "attempt_id": attempt_id,
            "request_body_sha256": request["request_body_sha256"],
            "system_prompt_sha256": request["system_prompt_sha256"],
            "user_prompt_sha256": request["user_prompt_sha256"],
            "budget_reservation": dict(self.prior_receipt["budget_reservation"],
                                        amount_cny=format(request["estimated_peak_cny"], "f")),
        })
        receipt_bytes = compact(receipt)
        manifest = json.loads(self.manifest_bytes)
        entry = manifest["chapter_map_v1"]["chapters"]["c000"]
        provenance = entry["provenance"]
        provenance.update({
            "job_hash": job_hash, "export_sha256": job_hash, "attempt_id": attempt_id,
            "request_body_sha256": request["request_body_sha256"],
            "system_prompt_sha256": request["system_prompt_sha256"],
            "user_prompt_sha256": request["user_prompt_sha256"],
            "approved_summaries_sha256": ledger._canonical_hash(prior_export["approved_summaries"]),
            "estimated_peak_cny": format(request["estimated_peak_cny"], "f"),
        })
        receipt_provenance = entry["receipt_provenance"]
        receipt_provenance.update({
            "attempt_id": attempt_id,
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "job_hash": job_hash,
            "request_body_sha256": request["request_body_sha256"],
        })
        manifest["events"][0]["details"]["attempt_id"] = attempt_id
        with self.assertRaisesRegex(context_v2.ContextV2Error, "malformed or future context"):
            self.build(
                manifest_snapshot_bytes=compact(manifest),
                prior_attempts=[{"protocol": "chapter-map-v1", "export_bytes": export_bytes,
                                 "receipt_bytes": receipt_bytes}],
            )

    def test_large_context_uses_deterministic_whole_entry_suffix_with_full_hashes(self):
        items = [{
            "chapter_id": f"c{i:04d}", "summary": "摘要" * 120,
            "evidence": {"quote": "原文" * 240}, "status": "machine_generated_unreviewed",
        } for i in range(800)]
        full_hash = context_v2._canonical_hash(items)
        kept, omitted = context_v2._bounded_suffix(items, context_v2.MAX_MACHINE_CONTEXT_BYTES)
        window = context_v2._context_window(items, kept, omitted, full_hash=full_hash)
        self.assertTrue(kept)
        self.assertGreater(len(omitted), 0)
        self.assertEqual(kept, items[-len(kept):])
        self.assertEqual(omitted, items[:-len(kept)])
        self.assertEqual(window["omitted_count"], len(omitted))
        self.assertEqual(window["omitted_first_chapter_id"], omitted[0]["chapter_id"])
        self.assertEqual(window["omitted_last_chapter_id"], omitted[-1]["chapter_id"])
        self.assertEqual(window["full_context_sha256"], full_hash)
        self.assertLessEqual(
            len(context_v2._compact_json(kept, "test").encode("utf-8")),
            context_v2.MAX_MACHINE_CONTEXT_BYTES,
        )

    def test_v2_attempt_id_binds_machine_context_hash(self):
        provenance = {
            "protocol": context_v2.PROTOCOL, "book_id": "book-v2-test", "chapter_id": "c001",
            "job_hash": "a" * 64, "request_body_sha256": "b" * 64,
            "builder_source_sha256": "c" * 64, "manifest_snapshot_sha256": "d" * 64,
            "machine_context_sha256": "e" * 64,
        }
        attempt = context_v2._attempt_id_for_provenance(provenance)
        changed = dict(provenance, machine_context_sha256="f" * 64)
        self.assertNotEqual(attempt, context_v2._attempt_id_for_provenance(changed))
        with self.assertRaisesRegex(context_v2.ContextV2Error, "machine_context_sha256"):
            context_v2._attempt_id_for_provenance({k: v for k, v in provenance.items()
                                                    if k != "machine_context_sha256"})


if __name__ == "__main__":
    unittest.main()
