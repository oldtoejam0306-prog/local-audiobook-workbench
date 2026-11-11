import hashlib
import json
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import workbench_audio_revision as revision
import real_book_renderer


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class RevisionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.book = "book-" + "a" * 20
        self.chapter = "c003"
        self.source_text = "旁白第一段。旁白第一段。角色回应。"
        self.source_sha = sha(self.source_text.encode("utf-8"))
        self.stage_path = self.root / "normalized-stage.json"
        self.normalized = {"chapters": [{"id": self.chapter, "segments": [
            {"speaker_id": "narrator", "text": "旁白第一段。"},
            {"speaker_id": "narrator", "text": "旁白第一段。"},
            {"speaker_id": "speaker-1", "text": "角色回应。", "instruct": "原有朗读提示"}]}],
            "audit": {"source_text_sha256": self.source_sha}, "original_stage_sha256": "b" * 64}
        self.stage_path.write_bytes(canonical(self.normalized))
        self.voice_dir = self.root / "voices" / "narrator"
        self.voice_dir.mkdir(parents=True)
        self.vector_path = self.voice_dir / "speaker-vector.npy"
        self.vector_path.write_bytes(b"fixed-vector")
        self.voice_dir2 = self.root / "voices" / "speaker-1"
        self.voice_dir2.mkdir(parents=True)
        self.vector_path2 = self.voice_dir2 / "speaker-vector.npy"
        self.vector_path2.write_bytes(b"fixed-vector-2")
        self.wav_path = self.root / "base.wav"
        self.wav_path.write_bytes(b"base-wave")
        self.manifest_path = self.root / "base-manifest.json"
        self.base_native_path = self.root / "base-native-receipt.json"
        base_voice_hashes = {"narrator": sha(self.vector_path.read_bytes()),
                             "speaker-1": sha(self.vector_path2.read_bytes())}
        base_native = {"kind": "native_huashuo_chapter_v1", "human_reviewed": False,
            "source_sha256": self.source_sha, "source_document_sha256": sha(self.stage_path.read_bytes()),
            "text_sha256": self.source_sha, "voice_hashes": base_voice_hashes,
            "wav_sha256": sha(self.wav_path.read_bytes())}
        self.base_native_path.write_bytes(canonical(base_native))
        self.base_manifest = {"book": self.book, "status": "complete",
            "renderer_kind": "native_huashuo_chapter_v1",
            "source_sha256": self.source_sha,
            "normalized_stage_sha256": sha(self.stage_path.read_bytes()),
            "native_receipt_path": str(self.base_native_path),
            "native_receipt_sha256": sha(self.base_native_path.read_bytes()),
            "chapters": [{"id": self.chapter, "status": "complete",
                "path": str(self.wav_path), "sha256": sha(self.wav_path.read_bytes())}]}
        self.manifest_path.write_bytes(canonical(self.base_manifest))
        self.context = {"book_id": self.book, "chapter_id": self.chapter,
            "source_sha256": self.source_sha, "normalized_stage_path": str(self.stage_path),
            "normalized_stage_sha256": sha(self.stage_path.read_bytes()),
            "base_manifest_path": str(self.manifest_path),
            "base_manifest_sha256": sha(self.manifest_path.read_bytes()),
            "base_wav_path": str(self.wav_path), "base_wav_sha256": sha(self.wav_path.read_bytes()),
            "voice_bindings": {
                "narrator": {"directory": str(self.voice_dir), "request": {"vector_sha256": sha(self.vector_path.read_bytes())}, "book_role_key": self.book + "\0narrator"},
                "speaker-1": {"directory": str(self.voice_dir2), "request": {"vector_sha256": sha(self.vector_path2.read_bytes())}, "book_role_key": self.book + "\0speaker-1"}},
            "revision_root": str(self.root / "runtime" / "audio-revisions" / self.book / self.chapter),
            "runtime_root": str(self.root / "runtime")}
        def fake_prepare(stage_path, book_id, chapter_id, source_sha256, role_bindings):
            raw = Path(stage_path).read_bytes(); stage = json.loads(raw)
            return {"book": book_id, "chapters": stage["chapters"],
                    "_source_stage_sha256": sha(raw), "_source_audit": stage["audit"]}, {}
        def fake_normalized(document):
            return {"chapters": document["chapters"], "audit": document["_source_audit"],
                    "original_stage_sha256": document["_source_stage_sha256"]}
        self.prepare_patch = patch.object(real_book_renderer, "prepare", side_effect=fake_prepare)
        self.normalize_patch = patch.object(real_book_renderer, "_native_normalized_stage", side_effect=fake_normalized)
        self.prepare_patch.start(); self.normalize_patch.start()
        self.addCleanup(self.prepare_patch.stop); self.addCleanup(self.normalize_patch.stop)

    def tearDown(self):
        self.temp.cleanup()

    def fake_render(self, context, stage_path, output_dir):
        derived = json.loads(Path(stage_path).read_bytes())
        derived_raw = Path(stage_path).read_bytes()
        normalized_sha = sha(canonical({"chapters": derived["chapters"], "audit": derived["audit"],
                                        "original_stage_sha256": sha(derived_raw)}))
        self.assertEqual("".join(x["text"] for x in derived["chapters"][0]["segments"]), self.source_text)
        output_roles = {x["speaker_id"] for x in derived["chapters"][0]["segments"]}
        self.assertTrue(output_roles <= {x["speaker_id"] for x in self.normalized["chapters"][0]["segments"]})
        wav = Path(output_dir) / "chapter.wav"
        with wave.open(str(wav), "wb") as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000); audio.writeframes(b"\x00\x00" * 16)
        native_path = Path(output_dir) / "native-render-receipt.json"
        voice_hashes = {role: context["voice_bindings"][role]["request"]["vector_sha256"]
                        for role in output_roles}
        native = {"kind": "native_huashuo_chapter_v1", "human_reviewed": False,
            "source_sha256": self.source_sha, "source_document_sha256": normalized_sha,
            "text_sha256": self.source_sha, "voice_hashes": voice_hashes,
            "wav_sha256": sha(wav.read_bytes())}
        native_path.write_bytes(canonical(native))
        manifest = {"book": self.book, "status": "complete", "source_sha256": self.source_sha,
            "renderer_kind": "native_huashuo_chapter_v1",
            "normalized_stage_sha256": normalized_sha,
            "original_stage_sha256": sha(derived_raw),
            "native_receipt_path": str(native_path), "native_receipt_sha256": sha(native_path.read_bytes()),
            "chapters": [{"id": self.chapter, "status": "complete", "source_sha256": self.source_sha,
                "text_sha256": self.source_sha, "path": str(wav), "sha256": sha(wav.read_bytes())}]}
        manifest_path = Path(output_dir) / "manifest.json"
        manifest_path.write_bytes(canonical(manifest))
        return {"chapter_wav": str(wav), "wav_sha256": sha(wav.read_bytes()),
                "manifest_path": str(manifest_path), "manifest_sha256": sha(manifest_path.read_bytes()),
                "source_sha256": self.source_sha}

    def _replace_base_with_verified_render(self):
        self.wav_path.write_bytes(b"new verified base render")
        native = json.loads(self.base_native_path.read_bytes())
        native["wav_sha256"] = sha(self.wav_path.read_bytes())
        self.base_native_path.write_bytes(canonical(native))
        manifest = json.loads(self.manifest_path.read_bytes())
        manifest["native_receipt_sha256"] = sha(self.base_native_path.read_bytes())
        manifest["chapters"][0]["sha256"] = sha(self.wav_path.read_bytes())
        self.manifest_path.write_bytes(canonical(manifest))
        current = dict(self.context)
        current["base_wav_sha256"] = sha(self.wav_path.read_bytes())
        current["base_manifest_sha256"] = sha(self.manifest_path.read_bytes())
        return current

    def test_paragraph_ids_are_stage_and_occurrence_bound_and_revision_is_immutable(self):
        rows = revision.list_paragraphs(self.context)
        self.assertEqual([0, 1, 2], [row["index"] for row in rows])
        self.assertEqual(0, rows[0]["occurrence"])
        self.assertEqual(1, rows[1]["occurrence"])
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
        with patch("real_book_auto_voice._selection_lock"):
            record = revision.generate(self.context, rows[0]["id"], "收尾完整自然", self.fake_render,
                                       revision_id="revision-one")
            raw = (Path(self.context["revision_root"]) / "revision-one" / "revision.json").read_bytes()
            self.assertEqual(record["record_sha256"], json.loads(raw)["record_sha256"])
            derived = json.loads((Path(self.context["revision_root"]) / "revision-one" / "normalized-stage.json").read_bytes())
            self.assertEqual("收尾完整自然", derived["chapters"][0]["segments"][0]["instruct"])
            self.assertNotIn("instruct", derived["chapters"][0]["segments"][1])
            selected = revision.adopt(self.context, "revision-one")
            resolved = revision.resolve_adopted(self.context)
        self.assertEqual(selected["chapter_wav"], resolved["chapter_wav"])
        self.assertEqual(record["revision_id"], resolved["revision_id"])

    def test_missing_pointer_returns_none_but_stale_or_corrupt_pointer_fails_closed(self):
        with patch("real_book_auto_voice._selection_lock"):
            self.assertIsNone(revision.resolve_adopted(self.context))
            rows = revision.list_paragraphs(self.context)
            revision.generate(self.context, rows[0]["id"], "自然收束", self.fake_render, revision_id="rev2")
            revision.adopt(self.context, "rev2")
        pointer = Path(self.context["revision_root"]) / "current-revision.json"
        pointer.write_bytes(canonical({"version": 1, "revision_id": "rev2", "record_sha256": "0" * 64,
            "book_id": self.book, "chapter_id": self.chapter, "source_sha256": self.source_sha,
            "normalized_stage_sha256": self.context["normalized_stage_sha256"],
            "base_manifest_sha256": self.context["base_manifest_sha256"],
            "base_wav_sha256": self.context["base_wav_sha256"],
            "voice_fingerprint": revision._sha(canonical({"narrator": sha(self.vector_path.read_bytes()),
                                                             "speaker-1": sha(self.vector_path2.read_bytes())}))}))
        with self.assertRaises(ValueError):
            revision.resolve_adopted(self.context)

    def test_restore_base_then_new_verified_base_render_remains_playable(self):
        with patch("real_book_auto_voice._selection_lock"):
            restored = revision.restore_base(self.context)
        self.assertEqual("base", restored["state"])
        new_context = self._replace_base_with_verified_render()
        self.assertIsNone(revision.resolve_adopted(new_context))

    def test_base_pointer_rejects_cross_book_and_bad_schema(self):
        with patch("real_book_auto_voice._selection_lock"):
            revision.restore_base(self.context)
        pointer_path = Path(self.context["revision_root"]) / "current-revision.json"
        pointer = json.loads(pointer_path.read_bytes())
        pointer["book_id"] = "book-" + "f" * 20
        pointer_path.write_bytes(canonical(pointer))
        with self.assertRaisesRegex(ValueError, "base selection pointer schema or identity"):
            revision.resolve_adopted(self.context)

        with patch("real_book_auto_voice._selection_lock"):
            revision.restore_base(self.context)
        pointer = json.loads(pointer_path.read_bytes())
        pointer["version"] = True
        pointer_path.write_bytes(canonical(pointer))
        with self.assertRaisesRegex(ValueError, "schema/version"):
            revision.resolve_adopted(self.context)

        with patch("real_book_auto_voice._selection_lock"):
            revision.restore_base(self.context)
        pointer = json.loads(pointer_path.read_bytes())
        pointer["revision_id"] = "unexpected"
        pointer_path.write_bytes(canonical(pointer))
        with self.assertRaisesRegex(ValueError, "base selection pointer schema"):
            revision.resolve_adopted(self.context)

    def test_active_revision_remains_blocked_after_base_changes(self):
        rows = revision.list_paragraphs(self.context)
        with patch("real_book_auto_voice._selection_lock"):
            record = revision.generate(self.context, rows[0]["id"], "自然收束", self.fake_render,
                                       revision_id="stale-after-base")
            revision.adopt(self.context, record["revision_id"])
        new_context = self._replace_base_with_verified_render()
        with self.assertRaisesRegex(ValueError, "stale for the current source/base/voices"):
            revision.resolve_adopted(new_context)

    def test_source_and_voice_files_are_rechecked_before_generation(self):
        rows = revision.list_paragraphs(self.context)
        self.vector_path.write_bytes(b"changed")
        with patch("real_book_auto_voice._selection_lock"):
            with self.assertRaisesRegex(ValueError, "vector changed"):
                revision.generate(self.context, rows[0]["id"], "自然收束", self.fake_render)

    def test_candidate_is_reloaded_and_receipt_rechecked_before_audition(self):
        rows = revision.list_paragraphs(self.context)
        with patch("real_book_auto_voice._selection_lock"):
            created = revision.generate(self.context, rows[0]["id"], "自然收束", self.fake_render,
                                        revision_id="audition-check")
            checked = revision.verify_candidate(self.context, created["revision_id"])
        self.assertEqual(created["record_sha256"], checked["record_sha256"])
        wav_path = Path(checked["receipt"]["chapter_wav"])
        wav_path.write_bytes(b"tampered-wave")
        with self.assertRaisesRegex(ValueError, "revision WAV changed"):
            revision.verify_candidate(self.context, created["revision_id"])

    def test_instruction_is_bounded_and_stale_paragraph_id_refused(self):
        rows = revision.list_paragraphs(self.context)
        with patch("real_book_auto_voice._selection_lock"):
            with self.assertRaises(ValueError):
                revision.generate(self.context, rows[0]["id"], "\n" + "x" * 800, self.fake_render)
            with self.assertRaisesRegex(ValueError, "stale or unknown"):
                revision.generate(self.context, "0" * 64, "自然收束", self.fake_render)

    def test_specific_speaker_override_uses_only_verified_output_voice_and_preserves_hint(self):
        rows = revision.list_paragraphs(self.context)
        with patch("real_book_auto_voice._selection_lock"):
            created = revision.generate(self.context, rows[2]["id"], "", self.fake_render,
                                        speaker_id="narrator", revision_id="speaker-override")
            verified = revision.verify_candidate(self.context, created["revision_id"])
            selected = revision.adopt(self.context, created["revision_id"])
            resolved = revision.resolve_adopted(self.context)
        stage_path = Path(self.context["revision_root"]) / created["revision_id"] / "normalized-stage.json"
        stage = json.loads(stage_path.read_bytes())
        segment = stage["chapters"][0]["segments"][2]
        self.assertEqual("narrator", segment["speaker_id"])
        self.assertEqual("原有朗读提示", segment["instruct"])
        self.assertEqual("speaker-1", created["speaker_id"])
        self.assertEqual("narrator", created["speaker_override_id"])
        self.assertIsNone(created["instruction"])
        self.assertEqual({"narrator"}, set(created["revision_voice_hashes"]))
        self.assertEqual(selected["record_sha256"], resolved["record_sha256"])
        native = json.loads(Path(verified["receipt"]["manifest_path"]).with_name("native-render-receipt.json").read_bytes())
        self.assertEqual(created["revision_voice_hashes"], native["voice_hashes"])

    def test_unknown_speaker_and_same_role_empty_override_are_rejected(self):
        rows = revision.list_paragraphs(self.context)
        with patch("real_book_auto_voice._selection_lock"):
            with self.assertRaisesRegex(ValueError, "not present in the verified source bindings"):
                revision.generate(self.context, rows[0]["id"], "", self.fake_render,
                                  speaker_id="unbound-role")
            with self.assertRaisesRegex(ValueError, "no paragraph change"):
                revision.generate(self.context, rows[0]["id"], "", self.fake_render,
                                  speaker_id="narrator")

    def test_revision_stage_and_native_voice_receipt_tampering_are_rejected(self):
        rows = revision.list_paragraphs(self.context)
        with patch("real_book_auto_voice._selection_lock"):
            created = revision.generate(self.context, rows[2]["id"], "", self.fake_render,
                                        speaker_id="narrator", revision_id="tamper-stage")
        revision_dir = Path(self.context["revision_root"]) / created["revision_id"]
        stage_path = revision_dir / "normalized-stage.json"
        stage = json.loads(stage_path.read_bytes()); stage["chapters"][0]["segments"][2]["text"] = "篡改正文"
        stage_path.write_bytes(canonical(stage))
        with patch("real_book_auto_voice._selection_lock"):
            with self.assertRaisesRegex(ValueError, "revision stage changed"):
                revision.verify_candidate(self.context, created["revision_id"])

        with patch("real_book_auto_voice._selection_lock"):
            created = revision.generate(self.context, rows[2]["id"], "", self.fake_render,
                                        speaker_id="narrator", revision_id="tamper-native")
        native_path = Path(created["receipt"]["manifest_path"]).with_name("native-render-receipt.json")
        native = json.loads(native_path.read_bytes()); native["voice_hashes"]["narrator"] = "0" * 64
        native_path.write_bytes(canonical(native))
        with patch("real_book_auto_voice._selection_lock"):
            with self.assertRaisesRegex(ValueError, "native receipt changed"):
                revision.verify_candidate(self.context, created["revision_id"])

    def test_legacy_revision_record_without_override_fields_remains_verifiable(self):
        rows = revision.list_paragraphs(self.context)
        with patch("real_book_auto_voice._selection_lock"):
            created = revision.generate(self.context, rows[0]["id"], "自然收束", self.fake_render,
                                        revision_id="legacy-record")
        revision_path = Path(self.context["revision_root"]) / created["revision_id"] / "revision.json"
        record = json.loads(revision_path.read_bytes())
        for key in ("revision_voice_hashes", "revision_voice_fingerprint", "speaker_override_id"):
            record.pop(key, None)
        record["version"] = 1
        record.pop("record_sha256")
        record["record_sha256"] = sha(canonical(record))
        revision_path.write_bytes(canonical(record))
        with patch("real_book_auto_voice._selection_lock"):
            loaded = revision.verify_candidate(self.context, created["revision_id"])
        self.assertEqual(record["record_sha256"], loaded["record_sha256"])


class RevisionUiPlaybackTests(unittest.TestCase):
    def test_revision_status_reports_full_chapter_and_only_available_cache_counts(self):
        import workbench_real_v2_ui as ui
        message = ui.audio_revision_summary({"receipt": {"cache_statistics": {"generated": 1, "cached": 89}}})
        self.assertIn("整章重排", message)
        self.assertIn("新生成 1 个单元", message)
        self.assertIn("复用缓存 89 个单元", message)
        self.assertNotIn("仅重生成所选段落", message)
        self.assertIn("缓存计数不可用", ui.audio_revision_summary({"receipt": {}}))

    def test_playback_prefers_verified_adopted_revision(self):
        import workbench_real_v2_ui as ui
        import workbench_audio_revision as store
        app = Mock()
        app.task_tree.selection.return_value = ("c003",)
        app.completed = {"c003": Path("base.wav")}
        app.book.get.return_value = next(iter(ui.existing.BOOKS))
        app._runtime_root.return_value = "runtime"
        app.play_speed.get.return_value = "1.0"
        app.player.prepare.return_value = "prepared"
        app._run.side_effect = lambda kind, label, fn: fn()
        app._revision_api.return_value = types.SimpleNamespace(get_context=lambda *_: {"context": "fresh"})
        app._set_playback_state = Mock()
        app.playback_generation = 0
        with patch.object(ui, "_sequence_root", return_value=Path("sequence")), \
             patch.object(ui, "completed_audio", return_value=Path("base.wav")), \
             patch.object(store, "resolve_adopted", return_value={"chapter_wav": "adopted.wav"}):
            ui.App.play_selected(app)
        app.player.prepare.assert_called_once_with(Path("adopted.wav"), 1.0)
        app.player.start.assert_called_once_with("prepared")

    def test_playback_uses_current_verified_base_when_restore_pointer_is_inactive(self):
        import workbench_real_v2_ui as ui
        import workbench_audio_revision as store
        app = Mock()
        app.task_tree.selection.return_value = ("c003",)
        app.completed = {"c003": Path("current-base.wav")}
        app.book.get.return_value = next(iter(ui.existing.BOOKS))
        app._runtime_root.return_value = "runtime"
        app.play_speed.get.return_value = "1.0"
        app.player.prepare.return_value = "prepared"
        app._run.side_effect = lambda kind, label, fn: fn()
        app._revision_api.return_value = types.SimpleNamespace(get_context=lambda *_: {"context": "fresh"})
        app._set_playback_state = Mock()
        app.playback_generation = 0
        with patch.object(ui, "_sequence_root", return_value=Path("sequence")), \
             patch.object(ui, "completed_audio", return_value=Path("current-base.wav")), \
             patch.object(store, "resolve_adopted", return_value=None):
            ui.App.play_selected(app)
        app.player.prepare.assert_called_once_with(Path("current-base.wav"), 1.0)

    def test_stale_adoption_error_does_not_fall_back_to_base_audio(self):
        import workbench_real_v2_ui as ui
        import workbench_audio_revision as store
        app = Mock()
        app.task_tree.selection.return_value = ("c003",)
        app.completed = {"c003": Path("base.wav")}
        app.book.get.return_value = next(iter(ui.existing.BOOKS))
        app._runtime_root.return_value = "runtime"
        app.play_speed.get.return_value = "1.0"
        app._run.side_effect = lambda kind, label, fn: fn()
        app._revision_api.return_value = types.SimpleNamespace(get_context=lambda *_: {"context": "fresh"})
        app.playback_generation = 0
        with patch.object(ui, "_sequence_root", return_value=Path("sequence")), \
             patch.object(ui, "completed_audio", return_value=Path("base.wav")), \
             patch.object(store, "resolve_adopted", side_effect=ValueError("stale adoption")):
            with self.assertRaisesRegex(ValueError, "stale adoption"):
                ui.App.play_selected(app)
        app.player.prepare.assert_not_called()

    def test_playback_without_source_matching_completion_is_refused(self):
        import workbench_real_v2_ui as ui
        app = Mock()
        app.task_tree.selection.return_value = ("c003",)
        app.completed = {"c003": Path("old-audio.wav")}
        app.book.get.return_value = next(iter(ui.existing.BOOKS))
        app._runtime_root.return_value = "runtime"
        with patch.object(ui, "_sequence_root", return_value=None):
            ui.App.play_selected(app)
        app._run.assert_not_called()
        app.status.set.assert_called_once()
        self.assertIn("没有与索引和原文一致", app.status.set.call_args.args[0])
