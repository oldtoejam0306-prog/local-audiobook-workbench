import hashlib
import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from render_unit_ledger import (LedgerError, plan_units,
                                plan_units_preserving_prefix)
from render_unit_ledger_adapter import run_with_adapter


BOOK = "book-aaaaaaaaaaaaaaaaaaaa"
MONITOR = "a" * 64


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_wav(path, samples):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"".join(struct.pack("<h", x) for x in samples))
    return Path(path)


def fixture(root):
    source_segments = [{"speaker_id": "narrator", "text": "one.two."},
                       {"speaker_id": "hero", "text": "A!B!C!D!"}]
    source_text = "".join(x["text"] for x in source_segments)
    source_sha = sha(source_text.encode())
    stage = {"chapters": [{"id": "c003", "segments": source_segments}],
             "audit": {"source_text_sha256": source_sha}}
    stage_path = root / "stage.json"
    stage_path.write_text(json.dumps(stage, ensure_ascii=False), encoding="utf-8")
    role_bindings = {}
    vector_pins = {}
    for speaker in ("narrator", "hero"):
        directory = root / "voice-assets" / speaker
        directory.mkdir(parents=True)
        vector = directory / "speaker-vector.npy"
        vector.write_bytes(("vector-" + speaker).encode())
        vector_sha = sha(vector.read_bytes())
        key = BOOK + "\0" + speaker
        role_bindings[speaker] = {
            "console_key": key,
            "record": {"book_role_key": key, "directory": str(directory),
                       "request": {"stage": "self-written-test-stub",
                                   "vector_sha256": vector_sha}},
        }
        vector_pins[speaker] = vector_sha
    return stage_path, source_sha, source_segments, role_bindings, vector_pins


class AdapterIntegrationTests(unittest.TestCase):
    def test_replanned_ledger_reuses_verified_prefix_without_rendering_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_path, _, _, bindings, vector_pins = fixture(root)
            segments = [{"speaker_id": "narrator", "text": "A" * 8 + "B" * 8},
                        {"speaker_id": "hero", "text": "C" * 8}]
            source = "".join(segment["text"] for segment in segments)
            source_sha = sha(source.encode())
            stage_path.write_text(json.dumps({"chapters": [{"id": "c003", "segments": segments}],
                "audit": {"source_text_sha256": source_sha}}, ensure_ascii=False), encoding="utf-8")
            baseline = plan_units(book_id=BOOK, chapter_id="c003",
                                  source_text_sha256=source_sha, segments=segments,
                                  voice_vector_sha256=vector_pins,
                                  runtime_monitor_sha256=MONITOR, max_codepoints=8)
            prefix = baseline["units"][:1]
            short_plan = plan_units_preserving_prefix(
                book_id=BOOK, chapter_id="c003", source_text_sha256=source_sha,
                segments=segments, voice_vector_sha256=vector_pins,
                runtime_monitor_sha256=MONITOR, preserved_prefix_units=prefix,
                max_codepoints=8, max_new_unit_codepoints=4)
            old_audio = write_wav(root / "old-prefix.wav", [700, 701])
            old_audio_hash = sha(old_audio.read_bytes())
            calls = []

            def stub(unit, voice, output_dir):
                calls.append(unit["unit_id"])
                wav_path = write_wav(output_dir / "new.wav", [800 + len(calls)])
                wav_sha = sha(wav_path.read_bytes())
                return {"wav_path": str(wav_path),
                        "rendered_text_sha256": unit["text_sha256"],
                        "speaker_id": unit["speaker_id"],
                        "voice_vector_sha256": unit["voice_vector_sha256"],
                        "runtime_monitor": {"exit_code": 0, "reason": None,
                            "minimum_pressure_free_percent": 62, "monitor_sha256": MONITOR},
                        "adapter_receipt": {"local_audio": {"wav_path": str(wav_path),
                            "wav_sha256": wav_sha, "wav_bytes": wav_path.stat().st_size}}}

            prefix_wav_sha = old_audio_hash
            seed = {"unit_id": prefix[0]["unit_id"], "audio_path": str(old_audio),
                "verification_evidence": {
                    "unit_text_sha256": prefix[0]["text_sha256"],
                    "rendered_text_sha256": prefix[0]["text_sha256"],
                    "speaker_id": prefix[0]["speaker_id"],
                    "voice_vector_sha256": prefix[0]["voice_vector_sha256"],
                    "runtime_monitor": {"exit_code": 0, "reason": None,
                        "minimum_pressure_free_percent": 62, "monitor_sha256": MONITOR},
                    "adapter_receipt": {"local_audio": {"wav_path": str(old_audio),
                        "wav_sha256": prefix_wav_sha,
                        "wav_bytes": old_audio.stat().st_size}}}}
            result = run_with_adapter(
                stage_path=stage_path, book_id=BOOK, chapter_id="c003",
                source_sha256=source_sha, role_bindings=bindings,
                runtime_monitor_sha256=MONITOR, ledger_root=root / "new-ledger",
                run_segment_adapter=stub, max_codepoints=8,
                plan_override=short_plan, precompleted_units=[seed])
            self.assertEqual(result["status"], "complete")
            self.assertEqual(len(calls), len(short_plan["units"]) - 1)
            ledger_root = root / "new-ledger"
            first_unit = short_plan["units"][0]
            done = json.loads((ledger_root / "units" / first_unit["unit_id"] / "done.json").read_bytes())
            self.assertEqual(done["wav_sha256"], prefix_wav_sha)
            self.assertEqual(done["verification_evidence"]["adapter_receipt"]["local_audio"]["wav_sha256"],
                             prefix_wav_sha)

    def _preflight_call(self, root, *, output_root, stub):
        stage, source_sha, _, bindings, _ = fixture(root)
        ledger_root = root / "ledger"
        with self.assertRaises(LedgerError):
            run_with_adapter(stage_path=stage, book_id=BOOK, chapter_id="c003",
                             source_sha256=source_sha, role_bindings=bindings,
                             runtime_monitor_sha256=MONITOR,
                             ledger_root=ledger_root,
                             render_output_root=output_root,
                             run_segment_adapter=stub, max_codepoints=4)
        self.assertFalse((ledger_root / "units").exists()
                         and any((ledger_root / "units").glob("*/reserved.json")))

    def test_stubbed_units_preserve_text_voice_monitor_and_audio_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage, source_sha, source_segments, bindings, vector_pins = fixture(root)
            calls = []

            def stub(unit, voice, output_dir):
                index = len(calls)
                calls.append((unit["speaker_id"], unit["text"],
                              voice["request"]["vector_sha256"]))
                wav_path = write_wav(output_dir / "stub.wav", [100 + index, 200 + index])
                return {"wav_path": str(wav_path),
                        "rendered_text_sha256": sha(unit["text"].encode()),
                        "speaker_id": unit["speaker_id"],
                        "voice_vector_sha256": voice["request"]["vector_sha256"],
                        "runtime_monitor": {"exit_code": 0, "reason": None,
                                            "minimum_pressure_free_percent": 30,
                                            "monitor_sha256": MONITOR},
                        "adapter_receipt": {"adapter": "self-written-stub",
                                            "unit_id": unit["unit_id"],
                                            "text_sha256": sha(unit["text"].encode()),
                                            "voice_sha256": vector_pins[unit["speaker_id"]]}}

            result = run_with_adapter(stage_path=stage, book_id=BOOK, chapter_id="c003",
                                      source_sha256=source_sha, role_bindings=bindings,
                                      runtime_monitor_sha256=MONITOR,
                                      ledger_root=root / "ledger",
                                      run_segment_adapter=stub, max_codepoints=4)
            self.assertEqual(result["status"], "complete", result)
            expected_calls = [("narrator", "one.", vector_pins["narrator"]),
                              ("narrator", "two.", vector_pins["narrator"]),
                              ("hero", "A!B!", vector_pins["hero"]),
                              ("hero", "C!D!", vector_pins["hero"])]
            self.assertEqual(calls, expected_calls)
            with wave.open(result["chapter_wav"], "rb") as wav:
                samples = struct.unpack("<" + "h" * wav.getnframes(), wav.readframes(wav.getnframes()))
            self.assertEqual(list(samples), [100, 200, 101, 201, 102, 202, 103, 203])
            ledger_root = root / "ledger"
            for unit in json.loads((ledger_root / "plan.json").read_bytes())["units"]:
                done = json.loads((ledger_root / "units" / unit["unit_id"] / "done.json").read_bytes())
                self.assertEqual(done["verification_evidence"]["adapter_receipt"]["unit_id"],
                                 unit["unit_id"])

    def test_explicit_render_output_root_keeps_each_unit_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage, source_sha, _, bindings, vector_pins = fixture(root)
            calls = []

            def stub(unit, voice, output_dir):
                calls.append(unit["unit_id"])
                wav_path = write_wav(output_dir / "stub.wav", [123])
                return {"wav_path": str(wav_path),
                        "rendered_text_sha256": sha(unit["text"].encode()),
                        "speaker_id": unit["speaker_id"],
                        "voice_vector_sha256": vector_pins[unit["speaker_id"]],
                        "runtime_monitor": {"exit_code": 0, "reason": None,
                                            "minimum_pressure_free_percent": 30,
                                            "monitor_sha256": MONITOR},
                        "adapter_receipt": {"adapter": "self-written-test-stub"}}

            ledger_root = root / "ledger"
            output_root = root / "render-output"
            result = run_with_adapter(stage_path=stage, book_id=BOOK, chapter_id="c003",
                                      source_sha256=source_sha, role_bindings=bindings,
                                      runtime_monitor_sha256=MONITOR,
                                      ledger_root=ledger_root,
                                      render_output_root=output_root,
                                      run_segment_adapter=stub, max_codepoints=4)
            self.assertEqual(result["status"], "complete", result)
            self.assertEqual(len(calls), 4)
            unit_roots = sorted(output_root.glob("u*"))
            self.assertEqual(len(unit_roots), 4)
            self.assertTrue(all(list(unit_root.glob("chapter-render-*/manifest.json"))
                                for unit_root in unit_roots))
            resumed = run_with_adapter(stage_path=stage, book_id=BOOK, chapter_id="c003",
                                       source_sha256=source_sha, role_bindings=bindings,
                                       runtime_monitor_sha256=MONITOR,
                                       ledger_root=ledger_root,
                                       render_output_root=ledger_root / "invalid-output",
                                       run_segment_adapter=stub, max_codepoints=4)
            self.assertEqual(resumed["status"], "complete", resumed)
            self.assertEqual(len(calls), 4)
            self.assertFalse((ledger_root / "invalid-output").exists())

    def test_preflight_rejects_occupied_unit_output_before_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "render-output"
            output_root.mkdir()
            (output_root / "u0000").mkdir()
            self._preflight_call(root, output_root=output_root,
                                 stub=lambda *args: self.fail("adapter must not run"))

    def test_preflight_rejects_overlong_future_path_before_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # The root itself fits a Windows path component, while its fixed
            # bridge descendants exceed MAX_PATH.
            output_root = root / ("x" * 180)
            self._preflight_call(root, output_root=output_root,
                                 stub=lambda *args: self.fail("adapter must not run"))

    def test_preflight_rejects_reparse_root_before_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "render-output"
            stage, source_sha, _, bindings, _ = fixture(root)
            with patch("render_unit_ledger_adapter._is_reparse_point",
                       side_effect=lambda path: Path(path) == output_root):
                with self.assertRaisesRegex(LedgerError, "reparse point"):
                    run_with_adapter(stage_path=stage, book_id=BOOK, chapter_id="c003",
                                     source_sha256=source_sha, role_bindings=bindings,
                                     runtime_monitor_sha256=MONITOR,
                                     ledger_root=root / "ledger",
                                     render_output_root=output_root,
                                     run_segment_adapter=lambda *args: self.fail(
                                         "adapter must not run"), max_codepoints=4)
            self.assertFalse((root / "ledger" / "units").exists())

    def test_bad_monitor_is_unknown_and_never_reinvokes_stub(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage, source_sha, _, bindings, _ = fixture(root)
            calls = []

            def bad_stub(unit, voice, output_dir):
                calls.append(unit["unit_id"])
                wav_path = write_wav(output_dir / "bad.wav", [123])
                return {"wav_path": str(wav_path),
                        "rendered_text_sha256": sha(unit["text"].encode()),
                        "speaker_id": unit["speaker_id"],
                        "voice_vector_sha256": voice["request"]["vector_sha256"],
                        "runtime_monitor": {"exit_code": 0, "reason": None,
                                            "minimum_pressure_free_percent": 29,
                                            "monitor_sha256": MONITOR},
                        "adapter_receipt": {"adapter": "self-written-stub"}}

            ledger_root = root / "ledger"
            first = run_with_adapter(stage_path=stage, book_id=BOOK, chapter_id="c003",
                                     source_sha256=source_sha, role_bindings=bindings,
                                     runtime_monitor_sha256=MONITOR, ledger_root=ledger_root,
                                     run_segment_adapter=bad_stub, max_codepoints=4)
            self.assertEqual(first["status"], "unknown")
            self.assertEqual(len(calls), 1)
            second = run_with_adapter(stage_path=stage, book_id=BOOK, chapter_id="c003",
                                      source_sha256=source_sha, role_bindings=bindings,
                                      runtime_monitor_sha256=MONITOR, ledger_root=ledger_root,
                                      run_segment_adapter=bad_stub, max_codepoints=4)
            self.assertEqual(second["status"], "unknown")
            self.assertEqual(len(calls), 1)

    def test_existing_unknown_is_returned_without_new_preflight_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage, source_sha, _, bindings, _ = fixture(root)

            def bad_monitor(unit, voice, output_dir):
                wav_path = write_wav(output_dir / "bad.wav", [1])
                return {"wav_path": str(wav_path),
                        "rendered_text_sha256": sha(unit["text"].encode()),
                        "speaker_id": unit["speaker_id"],
                        "voice_vector_sha256": voice["request"]["vector_sha256"],
                        "runtime_monitor": {"exit_code": 0, "reason": None,
                                            "minimum_pressure_free_percent": 29,
                                            "monitor_sha256": MONITOR},
                        "adapter_receipt": {"adapter": "self-written-test-stub"}}

            ledger_root = root / "ledger"
            first = run_with_adapter(stage_path=stage, book_id=BOOK, chapter_id="c003",
                                     source_sha256=source_sha, role_bindings=bindings,
                                     runtime_monitor_sha256=MONITOR,
                                     ledger_root=ledger_root,
                                     run_segment_adapter=bad_monitor, max_codepoints=4)
            self.assertEqual(first["status"], "unknown")
            too_long_root = root / ("x" * 180)
            second = run_with_adapter(stage_path=stage, book_id=BOOK, chapter_id="c003",
                                      source_sha256=source_sha, role_bindings=bindings,
                                      runtime_monitor_sha256=MONITOR,
                                      ledger_root=ledger_root,
                                      render_output_root=too_long_root,
                                      run_segment_adapter=lambda *args: self.fail(
                                          "unknown reservation must not be retried"),
                                      max_codepoints=4)
            self.assertEqual(second["status"], "unknown")
            self.assertFalse(too_long_root.exists())
            third = run_with_adapter(stage_path=stage, book_id=BOOK, chapter_id="c003",
                                     source_sha256=source_sha, role_bindings=bindings,
                                     runtime_monitor_sha256=MONITOR,
                                     ledger_root=ledger_root,
                                     render_output_root=ledger_root / "invalid-output",
                                     run_segment_adapter=lambda *args: self.fail(
                                         "unknown reservation must not be retried"),
                                     max_codepoints=4)
            self.assertEqual(third["status"], "unknown")
            self.assertFalse((ledger_root / "invalid-output").exists())


if __name__ == "__main__":
    unittest.main()
