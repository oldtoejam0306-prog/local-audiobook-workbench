import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import isolated_real_multivoice_mac as multivoice
import release_config


class MultiVoiceInputTests(unittest.TestCase):
    def test_loads_only_one_synthetic_schema_chapter_under_configured_root(self):
        with tempfile.TemporaryDirectory(prefix="multi-voice-fixture-") as directory:
            root = Path(directory)
            request = {"book": "synthetic fixture", "chapters": [{"id": "c001", "segments": [
                {"speaker_id": "narrator", "text": "Synthetic heading."},
                {"speaker_id": "speaker_a", "text": "Synthetic dialogue."},
            ]}]}
            source = root / "request.json"
            raw = json.dumps(request, ensure_ascii=False).encode()
            source.write_bytes(raw)
            with mock.patch.object(release_config, "mac_data_root", return_value=root):
                path, parsed, chapter_id, source_hash = multivoice.load_input(str(source))
            self.assertEqual(path, source)
            self.assertEqual(chapter_id, "c001")
            self.assertEqual(source_hash, hashlib.sha256(raw).hexdigest())
            self.assertEqual(parsed["chapters"][0]["segments"][1]["speaker_id"], "speaker_a")

    def test_voice_map_requires_hash_matching_stage_for_every_role(self):
        with tempfile.TemporaryDirectory(prefix="multi-voice-stage-") as directory:
            root = Path(directory)
            stage_root = root / "voices"
            stage_root.mkdir()
            entries = []
            for role in ("narrator", "speaker_a"):
                stage = stage_root / ("voice-experiment-" + role)
                stage.mkdir()
                vector = stage / "speaker-vector.npy"
                vector.write_bytes(("synthetic-" + role).encode())
                entries.append({"role": role, "stage": stage, "vector_sha256": hashlib.sha256(vector.read_bytes()).hexdigest()})
            segments = [{"speaker_id": "narrator", "text": "Heading."}, {"speaker_id": "speaker_a", "text": "Line."}]
            with mock.patch.object(release_config, "mac_multivoice_voices", return_value=entries), \
                 mock.patch.object(release_config, "mac_voice_root", return_value=stage_root):
                result = multivoice.configured_voice_map(segments)
            self.assertEqual({row["role"] for row in result}, {"narrator", "speaker_a"})

    def test_rejects_input_outside_data_root(self):
        with tempfile.TemporaryDirectory(prefix="multi-voice-root-") as root_dir, \
             tempfile.TemporaryDirectory(prefix="multi-voice-outside-") as outside_dir:
            source = Path(outside_dir) / "request.json"
            source.write_text("{}", encoding="utf-8")
            with mock.patch.object(release_config, "mac_data_root", return_value=Path(root_dir)):
                with self.assertRaises(ValueError):
                    multivoice.load_input(str(source))


if __name__ == "__main__":
    unittest.main()
