import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import release_config
import analysis_manifest
import profile_manifest_rpc


class ReleaseConfigTests(unittest.TestCase):
    def test_profile_worker_defaults_use_the_pinned_allowlisted_copy(self):
        expected = (Path(__file__).resolve().parent / "approved-workers" / "deepseek_profile.mjs").resolve()
        self.assertEqual(analysis_manifest.DEFAULT_PROFILE_WORKER_COPY, expected)
        self.assertEqual(profile_manifest_rpc.DEFAULT_WORKER_COPY, expected)
        self.assertTrue(expected.is_file())
        self.assertIn("050a4b2755c7fdc90e5791978e4dfd154311d2f91fc3756b7a65a106d78d4d25",
                      profile_manifest_rpc.ALLOWED_WORKER_SHA256)

    def test_mac_environment_preserves_venv_interpreter_path(self):
        config = str(Path("config.example.mac.json").resolve())
        interpreter = str(Path(tempfile.gettempdir()) / "example-venv" / "bin" / "python")
        with tempfile.TemporaryDirectory(prefix="media-tools-") as directory:
            ffmpeg = Path(directory) / "ffmpeg"
            ffprobe = Path(directory) / "ffprobe"
            ffmpeg.write_bytes(b"synthetic executable placeholder")
            ffprobe.write_bytes(b"synthetic executable placeholder")
            with mock.patch.dict(os.environ, {"AUDIOBOOK_CONFIG_FILE": config}, clear=False):
                with mock.patch.object(release_config.sys, "executable", interpreter):
                    with mock.patch.object(release_config, "mac_ffmpeg", return_value=ffmpeg):
                        with mock.patch.object(release_config, "mac_ffprobe", return_value=ffprobe):
                            release_config.apply_mac_environment()
                    self.assertEqual(os.environ["AUDIOBOOK_PYTHON"], os.path.abspath(interpreter))

    def test_mac_example_has_orchestrator_model_roles_and_limits(self):
        config = json.loads(Path("config.example.mac.json").read_text(encoding="utf-8"))
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            json.dump(config, handle)
            config_path = Path(handle.name)
        self.addCleanup(lambda: config_path.unlink(missing_ok=True))
        with mock.patch.dict(os.environ, {"AUDIOBOOK_CONFIG_FILE": str(config_path)}, clear=False):
            with mock.patch.dict(os.environ, {
                "AUDIOBOOK_VOICE_DESIGN_MODEL_ROOT": "",
                "AUDIOBOOK_CUSTOMVOICE_HF_HOME": "",
                "AUDIOBOOK_FFMPEG": "",
            }, clear=False):
                self.assertEqual(release_config.model_config("voice_design")["role"], "voice_design")
                self.assertEqual(release_config.model_config("speaker_encoder")["role"], "speaker_encoder")
                self.assertEqual(set(release_config.resource_limits()), {
                    "min_pressure_free_percent", "min_available_bytes", "max_child_rss_bytes", "poll_seconds"
                })
                self.assertTrue(str(release_config.mac_customvoice_hf_home()).endswith("huggingface"))

    def test_customvoice_path_requires_full_hf_home_layout(self):
        with tempfile.TemporaryDirectory(prefix="hf-cache-root-") as directory:
            root = Path(directory)
            snapshot = root / "hub" / "models--synthetic" / "snapshots" / "revision-a"
            snapshot.mkdir(parents=True)
            (snapshot / "config.json").write_text("{}", encoding="utf-8")
            self.assertEqual(release_config.validate_customvoice_hf_home(root), root.resolve())
            with self.assertRaises(RuntimeError):
                release_config.validate_customvoice_hf_home(snapshot)

    def test_ffmpeg_search_path_adds_the_configured_binary_directory(self):
        with tempfile.TemporaryDirectory(prefix="ffmpeg-bin-") as directory:
            tool = Path(directory) / "ffmpeg"
            probe = Path(directory) / "ffprobe"
            tool.write_bytes(b"synthetic executable placeholder")
            probe.write_bytes(b"synthetic executable placeholder")
            with mock.patch.object(release_config, "mac_ffmpeg", return_value=tool):
                with mock.patch.object(release_config, "mac_ffprobe", return_value=probe):
                    search_path = release_config.ffmpeg_search_path("/usr/bin")
            self.assertEqual(search_path.split(os.pathsep)[0], str(Path(directory).resolve()))
            self.assertIn("/usr/bin", search_path)

    def test_ffmpeg_search_path_contains_configured_tool_directory(self):
        with tempfile.TemporaryDirectory(prefix="ffmpeg-config-") as directory:
            folder = Path(directory)
            tool = folder / "ffmpeg"
            probe = folder / "ffprobe"
            tool.write_text("synthetic placeholder", encoding="utf-8")
            probe.write_text("synthetic placeholder", encoding="utf-8")
            with mock.patch.object(release_config, "mac_ffmpeg", return_value=tool):
                with mock.patch.object(release_config, "mac_ffprobe", return_value=probe):
                    result = release_config.ffmpeg_search_path("/usr/bin")
            self.assertEqual(result.split(os.pathsep)[0], str(folder.resolve()))
            self.assertIn("/usr/bin", result)


if __name__ == "__main__":
    unittest.main()
