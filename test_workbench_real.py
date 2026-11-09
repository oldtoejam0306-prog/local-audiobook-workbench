import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import workbench_real


class WorkbenchRegistryTests(unittest.TestCase):
    def test_empty_registry_has_no_built_in_books(self):
        with mock.patch.object(workbench_real.release_config, "section", return_value={"books": []}):
            self.assertEqual(workbench_real.registry(), [])
            self.assertEqual(workbench_real.artifacts(), [])

    def test_generic_manifest_is_hash_checked_before_playback(self):
        with tempfile.TemporaryDirectory(prefix="workbench-fixture-") as directory:
            root = Path(directory)
            audio = root / "sample.wav"
            with wave.open(str(audio), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(bytes(32))
            content_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "status": "complete",
                "chapters": [{"id": "c001", "status": "complete", "segments": [{"id": "p1"}],
                              "path": "sample.wav", "bytes": audio.stat().st_size, "sha256": content_hash}],
            }), encoding="utf-8")
            book = {"book_id": "book-11111111111111111111", "display_name": "synthetic fixture",
                    "chapters": [{"chapter_id": "c001", "manifest_path": "manifest.json", "audio_path": "sample.wav"}]}
            with mock.patch.object(workbench_real, "BASE", root), \
                 mock.patch.object(workbench_real.release_config, "section", return_value={"books": [book]}):
                rows = workbench_real.artifacts()
                self.assertEqual(rows[0]["status"], "complete")
                checked, _ = workbench_real.verified_audio(rows[0])
                self.assertEqual(checked, audio)

    def test_artifact_path_cannot_escape_configured_root(self):
        with tempfile.TemporaryDirectory(prefix="workbench-root-") as directory, \
             tempfile.TemporaryDirectory(prefix="workbench-outside-") as outside:
            with mock.patch.object(workbench_real, "BASE", Path(directory)):
                with self.assertRaises(ValueError):
                    workbench_real._data_path(str(Path(outside) / "receipt.json"))


if __name__ == "__main__":
    unittest.main()
