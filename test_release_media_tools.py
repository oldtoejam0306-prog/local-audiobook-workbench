import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import release_config


class ReleaseMediaToolTests(unittest.TestCase):
    def _config(self, root, mac):
        path = root / 'config.json'; path.write_text(json.dumps({'mac': mac}), encoding='utf-8')
        return patch.dict('os.environ', {'AUDIOBOOK_CONFIG_FILE': str(path), 'PATH': ''})

    def test_ffmpeg_path_includes_separate_configured_ffprobe_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); fdir = root / 'ffmpeg-tools'; pdir = root / 'probe-tools'
            fdir.mkdir(); pdir.mkdir(); ffmpeg = fdir / 'ffmpeg'; ffprobe = pdir / 'ffprobe'
            ffmpeg.write_bytes(b'tool'); ffprobe.write_bytes(b'tool')
            with self._config(root, {'ffmpeg': str(ffmpeg), 'ffprobe': str(ffprobe)}):
                result = release_config.ffmpeg_search_path('')
            self.assertEqual(result.split(release_config.os.pathsep), [str(fdir.resolve()), str(pdir.resolve())])

    def test_ffprobe_defaults_to_ffmpeg_sibling_or_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); tools = root / 'tools'; tools.mkdir()
            ffmpeg = tools / 'ffmpeg'; ffprobe = tools / 'ffprobe'
            ffmpeg.write_bytes(b'f'); ffprobe.write_bytes(b'p')
            with self._config(root, {'ffmpeg': str(ffmpeg)}):
                self.assertEqual(release_config.mac_ffprobe(), ffprobe.resolve())
                ffprobe.unlink()
                ffprobe.write_bytes(b'p')
                search = tools
                self.assertEqual(release_config.mac_ffprobe(str(search)), ffprobe.resolve())

    def test_missing_ffprobe_fails_before_render(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); tools = root / 'tools'; tools.mkdir()
            ffmpeg = tools / 'ffmpeg'; ffmpeg.write_bytes(b'f')
            with self._config(root, {'ffmpeg': str(ffmpeg)}):
                with self.assertRaisesRegex(RuntimeError, 'FFprobe'):
                    release_config.validate_media_tools('')


if __name__ == '__main__': unittest.main()
