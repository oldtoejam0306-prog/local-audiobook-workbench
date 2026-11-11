import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import chapter_revision_runtime as runtime


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


class ContextRestorationTests(unittest.TestCase):
    def test_get_context_restores_trusted_base_and_frozen_role_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            book, chapter = "book-" + "a" * 20, "c003"
            sequence_root = base / "runs" / book / "job-one" / "sequence"
            job_root = sequence_root.parent
            job_root.mkdir(parents=True)
            render_done = sequence_root / book / chapter / "render.done.json"
            render_done.parent.mkdir(parents=True)
            render_done.write_bytes(b"render-done")
            stage_path = job_root / "staging.json"; stage_path.write_bytes(b"stage")
            render_artifact_path = job_root / "render.json"; render_artifact_path.write_bytes(b"render")
            manifest_path = job_root / "manifest.json"; manifest_path.write_bytes(b"manifest")
            normalized_path = job_root / "normalized-stage.json"; normalized_path.write_bytes(b"normalized")
            voice_record = {"directory": str(job_root / "voice"), "request": {"vector_sha256": "c" * 64},
                            "book_role_key": book + "\0narrator"}
            pointer = {"path": str(job_root / "auto-voices.json"), "sha256": "a" * 64}
            source_sha = "b" * 64
            memory = {"artifact": {"auto": pointer}}
            stage_done = {"artifact": {"auto_sha256": pointer["sha256"], "path": str(stage_path)}}
            render_done_doc = {"artifact": {"path": str(render_artifact_path)}}
            voices = {"book_id": book, "chapter_id": chapter, "blocked": False,
                      "bindings": {"narrator": {"record": voice_record, "console_key": voice_record["book_role_key"]}}}
            stage = {"audit": {"source_text_sha256": source_sha}}
            saved_render = {"receipt": {"manifest_path": str(manifest_path), "manifest_sha256": "d" * 64,
                                         "wav_sha256": "e" * 64, "chapter_wav": str(job_root / "chapter.wav"),
                                         "source_sha256": source_sha}}
            manifest = {"normalized_stage_sha256": "f" * 64}
            normalized = {"chapters": [{"id": chapter, "segments": [{"speaker_id": "narrator", "text": "句子。"}]}]}
            sequence_ui = __import__("workbench_real_v2_ui")
            reads = {
                "frozen chapter voices": (voices, job_root / "auto-voices.json"),
                "chapter stage": (stage, stage_path),
                "chapter render receipt": (saved_render, render_artifact_path),
                "chapter manifest": (manifest, manifest_path),
                "normalized chapter stage": (normalized, normalized_path),
            }
            renderer_receipt = {"manifest_sha256": "d" * 64, "wav_sha256": "e" * 64}
            with patch.object(sequence_ui, "_sequence_root", return_value=sequence_root) as locate, \
                 patch.object(sequence_ui, "_read_sequence_plan", return_value={"book_id": book}), \
                 patch.object(sequence_ui, "_verified_stage_done", side_effect=[memory, stage_done, render_done_doc]), \
                 patch.object(sequence_ui, "completed_audio", return_value=Path(saved_render["receipt"]["chapter_wav"])), \
                 patch.object(sequence_ui, "_read_hash_pinned_json", side_effect=lambda _p, _r, label: reads[label]), \
                 patch.object(runtime.auto, "_validate_record") as validate_record, \
                 patch.object(runtime.renderer, "prepare", return_value=(object(), {"narrator": {}})) as prepare, \
                 patch.object(runtime.renderer, "verify", return_value=renderer_receipt):
                context = runtime.get_context(base, book, chapter)
            locate.assert_called_once_with(base, book, chapter, require_completed=True)
            self.assertEqual(source_sha, context["source_sha256"])
            self.assertEqual("f" * 64, context["normalized_stage_sha256"])
            self.assertEqual({"narrator"}, set(context["voice_bindings"]))
            self.assertEqual(str(base / "audio-revisions" / book / chapter), context["revision_root"])
            self.assertEqual(1, validate_record.call_count)
            prepare.assert_called_once()
            with patch.object(sequence_ui, "_sequence_root", return_value=None) as locate_missing:
                with self.assertRaisesRegex(ValueError, "no source-matching verified completed render"):
                    runtime.get_context(base, book, chapter)
            locate_missing.assert_called_once_with(base, book, chapter, require_completed=True)


class RenderOnlyRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.book, self.chapter = "book-" + "a" * 20, "c003"
        self.revision_root = self.root / "audio-revisions" / self.book / self.chapter
        self.revision_root.mkdir(parents=True)
        self.source_sha = "b" * 64
        self.original = {"chapters": [{"id": self.chapter, "segments": [
            {"speaker_id": "narrator", "text": "收尾句。", "delivery": "calm"},
            {"speaker_id": "speaker-1", "text": "回应。", "delivery": "neutral"}]}],
            "audit": {"source_text_sha256": self.source_sha}, "original_stage_sha256": "c" * 64}
        self.original_path = self.revision_root / "normalized-stage.json"
        self.original_path.write_bytes(canonical(self.original))
        self.context = {"runtime_root": str(self.root), "book_id": self.book,
            "chapter_id": self.chapter, "source_sha256": self.source_sha,
            "normalized_stage_path": str(self.original_path),
            "normalized_stage_sha256": sha(self.original_path.read_bytes()),
            "base_manifest_sha256": "d" * 64, "base_wav_sha256": "e" * 64,
            "base_render_done_sha256": "f" * 64,
            "voice_bindings": {"narrator": {"record": {"request": {"vector_sha256": "1" * 64}}},
                               "speaker-1": {"record": {"request": {"vector_sha256": "2" * 64}}}},
            "revision_root": str(self.revision_root)}
        self.stage_path = self.revision_root / "candidate.json"
        self.output_dir = self.revision_root / "output"
        self.output_dir.mkdir()
        self.candidate = json.loads(canonical(self.original))
        self.candidate["chapters"][0]["segments"][0]["instruct"] = "自然收束"
        self.stage_path.write_bytes(canonical(self.candidate))

    def tearDown(self):
        self.temp.cleanup()

    def _render_mocks(self, after=None, external=False, verified_result=None):
        output = self.root / "outside-render" if external else self.output_dir
        output.mkdir(exist_ok=True)
        manifest = output / "manifest.json"; manifest.write_bytes(b"manifest")
        wav = output / "chapter.wav"; wav.write_bytes(b"wav")
        receipt = {"manifest_path": str(manifest), "chapter_wav": str(wav)}
        pins = {"source_sha256", "normalized_stage_sha256", "base_manifest_sha256",
                "base_wav_sha256", "base_render_done_sha256", "voice_bindings"}
        fresh = self.context if after is None else after
        document, assignments = object(), {"narrator": {}}
        return (patch.object(runtime, "get_context", side_effect=[self.context, fresh]),
                patch.object(runtime.renderer, "render", return_value=receipt),
                patch.object(runtime.renderer, "prepare", return_value=(document, assignments)),
                patch.object(runtime.renderer, "verify", return_value=(verified_result or {"chapter_wav": str(wav)})),
                manifest, wav)

    def test_render_only_callback_calls_renderer_without_analysis_dispatch(self):
        import analysis_dispatch_cache
        get_context, render, prepare, verify, _manifest, _wav = self._render_mocks()
        dispatcher = patch.object(analysis_dispatch_cache, "CachedAnalysisDispatcher")
        with get_context, render as render_mock, prepare as prepare_mock, verify as verify_mock, dispatcher as dispatch:
            result = runtime.render_revision(self.context, self.stage_path, self.output_dir)
        self.assertIn("chapter_wav", result)
        render_mock.assert_called_once()
        prepare_mock.assert_called_once()
        verify_mock.assert_called_once()
        dispatch.assert_not_called()

    def test_maps_native_synthesis_statistics_to_cache_statistics(self):
        native_path = self.output_dir / "native-receipt.json"
        native_path.write_bytes(canonical({"synthesis_statistics":
                                           {"generated": 2, "cached": 7, "retried": 1}}))
        get_context, render, prepare, verify, _manifest, _wav = self._render_mocks(
            verified_result={"chapter_wav": str(self.output_dir / "chapter.wav"),
                             "native_receipt_path": str(native_path)})
        with get_context, render, prepare, verify:
            result = runtime.render_revision(self.context, self.stage_path, self.output_dir)
        self.assertEqual({"generated": 2, "cached": 7, "retried": 1},
                         result["cache_statistics"])

    def test_missing_native_synthesis_statistics_remain_unavailable(self):
        native_path = self.output_dir / "native-receipt.json"
        native_path.write_bytes(canonical({"other": "receipt fields"}))
        get_context, render, prepare, verify, _manifest, _wav = self._render_mocks(
            verified_result={"chapter_wav": str(self.output_dir / "chapter.wav"),
                             "native_receipt_path": str(native_path)})
        with get_context, render, prepare, verify:
            result = runtime.render_revision(self.context, self.stage_path, self.output_dir)
        self.assertNotIn("cache_statistics", result)

    def test_rejects_base_changed_before_or_during_render(self):
        changed = dict(self.context, base_wav_sha256="0" * 64)
        with patch.object(runtime, "get_context", return_value=changed):
            with self.assertRaisesRegex(ValueError, "completed chapter changed"):
                runtime.render_revision(self.context, self.stage_path, self.output_dir)
        changed_during = dict(self.context, base_render_done_sha256="0" * 64)
        get_context, render, prepare, verify, *_ = self._render_mocks(after=changed_during)
        with get_context, render, prepare, verify:
            with self.assertRaisesRegex(ValueError, "changed during"):
                runtime.render_revision(self.context, self.stage_path, self.output_dir)

    def test_rejects_changed_words_voice_or_delivery(self):
        for index, field, value in ((0, "text", "不同句子。"), (0, "speaker_id", "speaker-1"),
                                    (0, "delivery", "angry")):
            with self.subTest(field=field):
                changed = json.loads(self.stage_path.read_text(encoding="utf-8"))
                changed["chapters"][0]["segments"][index][field] = value
                self.stage_path.write_bytes(canonical(changed))
                with patch.object(runtime, "get_context", return_value=self.context):
                    with self.assertRaisesRegex(ValueError, "words, voice, or delivery"):
                        runtime.render_revision(self.context, self.stage_path, self.output_dir)
                self.stage_path.write_bytes(canonical(self.candidate))

    def test_rejects_more_than_one_instruction_and_escaped_inputs(self):
        changed = json.loads(canonical(self.candidate))
        changed["chapters"][0]["segments"][1]["instruct"] = "第二个指令"
        self.stage_path.write_bytes(canonical(changed))
        with patch.object(runtime, "get_context", return_value=self.context):
            with self.assertRaisesRegex(ValueError, "change one explicit instruction"):
                runtime.render_revision(self.context, self.stage_path, self.output_dir)
        outside = self.root.parent / "outside-revision-stage.json"
        outside.write_bytes(canonical(self.candidate))
        try:
            with patch.object(runtime, "get_context", return_value=self.context):
                with self.assertRaisesRegex(ValueError, "escape their chapter namespace"):
                    runtime.render_revision(self.context, outside, self.output_dir)
        finally:
            outside.unlink(missing_ok=True)

    def test_rejects_renderer_receipt_escaping_output_directory(self):
        get_context, render, prepare, verify, *_ = self._render_mocks(external=True)
        with get_context, render, prepare, verify:
            with self.assertRaisesRegex(ValueError, "renderer output escaped"):
                runtime.render_revision(self.context, self.stage_path, self.output_dir)

