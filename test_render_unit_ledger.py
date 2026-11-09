import hashlib
import json
import struct
import tempfile
import threading
import unittest
import wave
from pathlib import Path

from render_unit_ledger import (LedgerError, UnitLedger, plan_units,
                                plan_units_preserving_prefix,
                                validate_plan_segments)


MONITOR = "a" * 64
VOICE = "b" * 64


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()


def make_plan(segments=None, max_chars=4):
    segments = segments or [
        {"speaker_id": "narrator", "text": "one.two."},
        {"speaker_id": "hero", "text": "A!B!"},
    ]
    source = "".join(item["text"] for item in segments)
    speakers = {item["speaker_id"] for item in segments}
    voices = {speaker: (VOICE if speaker == "narrator" else "c" * 64)
              for speaker in speakers}
    return plan_units(book_id="book-aaaaaaaaaaaaaaaaaaaa", chapter_id="c003",
                      source_text_sha256=sha(source.encode()), segments=segments,
                      voice_vector_sha256=voices, runtime_monitor_sha256=MONITOR,
                      max_codepoints=max_chars)


def write_wav(path, samples):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"".join(struct.pack("<h", value) for value in samples))
    return Path(path)


def evidence(unit, *, free=30, monitor_hash=MONITOR):
    return {"unit_text_sha256": unit["text_sha256"],
            "rendered_text_sha256": unit["text_sha256"],
            "speaker_id": unit["speaker_id"],
            "voice_vector_sha256": unit["voice_vector_sha256"],
            "runtime_monitor": {"exit_code": 0, "reason": None,
                                "minimum_pressure_free_percent": free,
                                "monitor_sha256": monitor_hash}}


class PlannerTests(unittest.TestCase):
    def test_short_replan_preserves_exact_completed_prefix_and_caps_new_units(self):
        segments = [{"speaker_id": "narrator", "text": "甲" * 48 + "乙" * 47 + "。"},
                    {"speaker_id": "hero", "text": "新声。"}]
        source = "".join(item["text"] for item in segments)
        voices = {"narrator": VOICE, "hero": "c" * 64}
        original = plan_units(book_id="book-aaaaaaaaaaaaaaaaaaaa", chapter_id="c003",
                              source_text_sha256=sha(source.encode()), segments=segments,
                              voice_vector_sha256=voices, runtime_monitor_sha256=MONITOR,
                              max_codepoints=48)
        prefix = original["units"][:1]
        replanned = plan_units_preserving_prefix(
            book_id="book-aaaaaaaaaaaaaaaaaaaa", chapter_id="c003",
            source_text_sha256=sha(source.encode()), segments=segments,
            voice_vector_sha256=voices, runtime_monitor_sha256=MONITOR,
            preserved_prefix_units=prefix, max_codepoints=48,
            max_new_unit_codepoints=24)
        self.assertEqual(replanned["units"][0], prefix[0])
        self.assertEqual(replanned["units"][0]["text"], "甲" * 48)
        self.assertTrue(all(len(unit["text"]) <= 24 for unit in replanned["units"][1:]))
        self.assertEqual("".join(unit["text"] for unit in replanned["units"]), source)
        validate_plan_segments(replanned, segments)

    def test_short_replan_rejects_nonprefix_or_inauthentic_completed_units(self):
        segments = [{"speaker_id": "narrator", "text": "甲" * 60}]
        source_sha = sha(segments[0]["text"].encode())
        original = plan_units(book_id="book-aaaaaaaaaaaaaaaaaaaa", chapter_id="c003",
                              source_text_sha256=source_sha, segments=segments,
                              voice_vector_sha256={"narrator": VOICE},
                              runtime_monitor_sha256=MONITOR, max_codepoints=48)
        forged = [dict(original["units"][0], text="乙" * 48,
                       text_sha256=sha(("乙" * 48).encode()))]
        with self.assertRaisesRegex(LedgerError, "exact prefix"):
            plan_units_preserving_prefix(
                book_id="book-aaaaaaaaaaaaaaaaaaaa", chapter_id="c003",
                source_text_sha256=source_sha, segments=segments,
                voice_vector_sha256={"narrator": VOICE}, runtime_monitor_sha256=MONITOR,
                preserved_prefix_units=forged, max_codepoints=48,
                max_new_unit_codepoints=24)

    def test_units_preserve_exact_source_speaker_order_and_offsets(self):
        segments = [{"speaker_id": "narrator", "text": "第一句。第二句继续！"},
                    {"speaker_id": "hero", "text": "甲说：你好。再见。"}]
        source = "".join(segment["text"] for segment in segments)
        plan = plan_units(book_id="book-aaaaaaaaaaaaaaaaaaaa", chapter_id="c003",
                          source_text_sha256=sha(source.encode()), segments=segments,
                          voice_vector_sha256={"narrator": VOICE, "hero": "c" * 64},
                          runtime_monitor_sha256=MONITOR, max_codepoints=6)
        units = plan["units"]
        self.assertEqual("".join(unit["text"] for unit in units), source)
        self.assertEqual([unit["ordinal"] for unit in units], list(range(len(units))))
        self.assertTrue(all(len(unit["text"]) <= 6 for unit in units))
        for segment_index, source_segment in enumerate(segments):
            group = [unit for unit in units if unit["segment_index"] == segment_index]
            self.assertTrue(group)
            self.assertTrue(all(unit["speaker_id"] == source_segment["speaker_id"] for unit in group))
            self.assertEqual("".join(unit["text"] for unit in group), source_segment["text"])
            self.assertEqual(group[0]["start_offset"], 0)
            self.assertEqual(group[-1]["end_offset"], len(source_segment["text"]))
        again = plan_units(book_id="book-aaaaaaaaaaaaaaaaaaaa", chapter_id="c003",
                           source_text_sha256=sha(source.encode()), segments=segments,
                           voice_vector_sha256={"narrator": VOICE, "hero": "c" * 64},
                           runtime_monitor_sha256=MONITOR, max_codepoints=6)
        self.assertEqual(plan, again)

    def test_planner_rejects_source_or_voice_mismatch(self):
        segment = [{"speaker_id": "narrator", "text": "hello"}]
        with self.assertRaisesRegex(LedgerError, "do not match"):
            plan_units(book_id="book-aaaaaaaaaaaaaaaaaaaa", chapter_id="c003",
                       source_text_sha256="0" * 64, segments=segment,
                       voice_vector_sha256={"narrator": VOICE},
                       runtime_monitor_sha256=MONITOR)
        with self.assertRaisesRegex(LedgerError, "do not match"):
            plan_units(book_id="book-aaaaaaaaaaaaaaaaaaaa", chapter_id="c003",
                       source_text_sha256=sha(b"hello"), segments=segment,
                       voice_vector_sha256={"narrator": VOICE, "unused": "c" * 64},
                       runtime_monitor_sha256=MONITOR)

    def test_trailing_whitespace_never_creates_an_oversized_unit(self):
        source = "hello  "
        plan = plan_units(book_id="book-aaaaaaaaaaaaaaaaaaaa", chapter_id="c003",
                          source_text_sha256=sha(source.encode()),
                          segments=[{"speaker_id": "narrator", "text": source}],
                          voice_vector_sha256={"narrator": VOICE},
                          runtime_monitor_sha256=MONITOR, max_codepoints=8)
        self.assertEqual("".join(unit["text"] for unit in plan["units"]), source)
        self.assertTrue(all(len(unit["text"]) <= 8 for unit in plan["units"]))
        too_much = "hello   "
        with self.assertRaisesRegex(LedgerError, "trailing whitespace"):
            plan_units(book_id="book-aaaaaaaaaaaaaaaaaaaa", chapter_id="c003",
                       source_text_sha256=sha(too_much.encode()),
                       segments=[{"speaker_id": "narrator", "text": too_much}],
                       voice_vector_sha256={"narrator": VOICE},
                       runtime_monitor_sha256=MONITOR, max_codepoints=5)


class LedgerTests(unittest.TestCase):
    def test_unknown_and_complete_race_has_only_one_terminal_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = make_plan(segments=[{"speaker_id": "narrator", "text": "one."}], max_chars=4)
            ledger = UnitLedger(Path(tmp) / "ledger", plan)
            ready = ledger.next_unit()
            audio = write_wav(Path(tmp) / "unit.wav", [111, 222])
            barrier = threading.Barrier(2)
            errors = []

            def complete():
                barrier.wait()
                try:
                    ledger.complete_unit(ready["unit"]["unit_id"], audio,
                                         evidence=evidence(ready["unit"]))
                    return "complete"
                except (OSError, LedgerError) as exc:
                    errors.append(type(exc).__name__)
                    return "lost"

            def mark_unknown():
                barrier.wait()
                try:
                    ledger.record_unknown(ready["unit"]["unit_id"], "transport_unknown")
                    return "unknown"
                except (OSError, LedgerError) as exc:
                    errors.append(type(exc).__name__)
                    return "lost"

            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(complete), pool.submit(mark_unknown)]
                outcomes = [f.result() for f in futures]
            self.assertEqual(sum(x == "complete" for x in outcomes)
                             + sum(x == "unknown" for x in outcomes), 1)
            terminal = json.loads((Path(tmp) / "ledger" / "units"
                                   / ready["unit"]["unit_id"] / "outcome.json").read_bytes())
            self.assertIn(terminal["state"], {"completion_claimed", "unknown"})
            done_path = terminal_path = (Path(tmp) / "ledger" / "units"
                                         / ready["unit"]["unit_id"])
            if terminal["state"] == "unknown":
                self.assertFalse((done_path / "done.json").exists())
                self.assertFalse((done_path / "audio.wav").exists())
                self.assertEqual(UnitLedger(Path(tmp) / "ledger", plan).next_unit()["status"], "unknown")
            else:
                self.assertTrue((terminal_path / "done.json").is_file())
                ledger._verify_done(ready["unit"])
            self.assertEqual(len(errors), 1)

    def test_unknown_unit_is_never_retried_or_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = make_plan(segments=[{"speaker_id": "narrator", "text": "one.two."}], max_chars=4)
            ledger = UnitLedger(Path(tmp) / "ledger", plan)
            first = ledger.next_unit()
            self.assertEqual(first["status"], "ready")
            self.assertEqual(first["unit"]["ordinal"], 0)
            self.assertEqual(ledger.next_unit()["status"], "unknown")
            ledger.record_unknown(first["unit"]["unit_id"], "transport_unknown")
            reopened = UnitLedger(Path(tmp) / "ledger", plan)
            state = reopened.next_unit()
            self.assertEqual(state["status"], "unknown")
            self.assertEqual(state["unit_id"], first["unit"]["unit_id"])
            self.assertFalse((Path(tmp) / "ledger" / "units" /
                              plan["units"][1]["unit_id"] / "reserved.json").exists())
            with self.assertRaises(LedgerError):
                reopened.complete_unit(first["unit"]["unit_id"], Path(tmp) / "missing.wav",
                                       evidence=evidence(first["unit"]))

    def test_complete_unit_hash_is_checked_before_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = make_plan(segments=[{"speaker_id": "narrator", "text": "one.two."}], max_chars=4)
            ledger = UnitLedger(Path(tmp) / "ledger", plan)
            ready = ledger.next_unit()
            source = write_wav(Path(tmp) / "unit.wav", [100, 200, 300])
            ledger.complete_unit(ready["unit"]["unit_id"], source,
                                 evidence=evidence(ready["unit"]))
            resumed = UnitLedger(Path(tmp) / "ledger", plan)
            next_work = resumed.next_unit()
            self.assertEqual(next_work["status"], "ready")
            self.assertEqual(next_work["unit"]["ordinal"], 1)
            stored = (Path(tmp) / "ledger" / "units" / ready["unit"]["unit_id"] / "audio.wav")
            stored.write_bytes(stored.read_bytes() + b"corrupt")
            with self.assertRaisesRegex(LedgerError, "hash or format mismatch"):
                resumed.next_unit()

    def test_runtime_monitor_below_30_blocks_completion_and_consumes_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = make_plan(segments=[{"speaker_id": "narrator", "text": "one.two."}], max_chars=4)
            ledger = UnitLedger(Path(tmp) / "ledger", plan)
            ready = ledger.next_unit()
            source = write_wav(Path(tmp) / "unit.wav", [100])
            with self.assertRaisesRegex(LedgerError, "30% runtime monitor"):
                ledger.complete_unit(ready["unit"]["unit_id"], source,
                                     evidence=evidence(ready["unit"], free=29))
            self.assertEqual(ledger.next_unit()["status"], "unknown")
            self.assertFalse((Path(tmp) / "ledger" / "units" /
                              ready["unit"]["unit_id"] / "audio.wav").exists())

    def test_streaming_assembly_matches_source_unit_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = make_plan(segments=[{"speaker_id": "narrator", "text": "one.two."}], max_chars=4)
            ledger = UnitLedger(Path(tmp) / "ledger", plan)
            unit_samples = [[100, 200], [300, 400, 500]]
            for expected_ordinal, samples in enumerate(unit_samples):
                ready = ledger.next_unit()
                self.assertEqual(ready["status"], "ready")
                self.assertEqual(ready["unit"]["ordinal"], expected_ordinal)
                wav_path = write_wav(Path(tmp) / f"unit-{expected_ordinal}.wav", samples)
                ledger.complete_unit(ready["unit"]["unit_id"], wav_path,
                                     evidence=evidence(ready["unit"]))
            self.assertEqual(ledger.next_unit()["status"], "all_units_complete")
            record = ledger.assemble()
            verified = ledger.verify_assembled()
            self.assertEqual(record, verified)
            with wave.open(str(Path(tmp) / "ledger" / "chapter.wav"), "rb") as wav:
                actual = struct.unpack("<" + "h" * wav.getnframes(), wav.readframes(wav.getnframes()))
            self.assertEqual(list(actual), [100, 200, 300, 400, 500])
            self.assertEqual(record["unit_ids"], [unit["unit_id"] for unit in plan["units"]])
            first_done = (Path(tmp) / "ledger" / "units" / plan["units"][0]["unit_id"]
                          / "done.json")
            done_record = json.loads(first_done.read_bytes())
            self.assertEqual(done_record["verification_evidence"], evidence(plan["units"][0]))
            assembled = json.loads((Path(tmp) / "ledger" / "assembled.json").read_bytes())
            assembled["unit_wav_sha256"][0] = "0" * 64
            (Path(tmp) / "ledger" / "assembled.json").write_bytes(canonical(assembled))
            with self.assertRaisesRegex(LedgerError, "hash list mismatch"):
                ledger.verify_assembled()

    def test_plan_cannot_be_reopened_with_different_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger"
            plan = make_plan(segments=[{"speaker_id": "narrator", "text": "one.two."}], max_chars=4)
            UnitLedger(path, plan)
            changed = make_plan(segments=[{"speaker_id": "narrator", "text": "one.three."}], max_chars=4)
            with self.assertRaisesRegex(LedgerError, "different plan"):
                UnitLedger(path, changed)


if __name__ == "__main__":
    unittest.main()
