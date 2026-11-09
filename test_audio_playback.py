import hashlib
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import audio_playback
from audio_playback import AudioPlayback, PlaybackCancelled, parse_speed, resolve_ffmpeg


def write_pcm(path, frames=24000, rate=24000):
    path = Path(path)
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b'\x00\x00' * frames)
    return path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class AudioPlaybackTests(unittest.TestCase):
    def test_speed_bounds_and_common_values(self):
        for value in ('0.8', '1.0', '1.08', '1.1', '1.5'):
            self.assertEqual(parse_speed(value), float(value))
        for value in ('0.79', '1.51', 'fast', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_speed(value)

    def test_ffmpeg_resolution_precedence_without_installing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); override = root/'override.exe'; path_binary = root/'path-ffmpeg'; bundled = root/'default.exe'
            for item in (override, path_binary, bundled): item.write_bytes(b'binary fixture')
            env = {'AUDIOBOOK_PLAYBACK_FFMPEG':str(override), 'LOCALAPPDATA':td}
            self.assertEqual(resolve_ffmpeg(env, which=lambda name: str(path_binary)), override)
            self.assertEqual(resolve_ffmpeg({'LOCALAPPDATA':td}, which=lambda name: str(path_binary)), path_binary)
            fallback_env = {'LOCALAPPDATA':td}
            default = Path(td)/'AudiobookTrials'/'tools'/'ffmpeg'/'ffmpeg.exe'
            default.parent.mkdir(parents=True)
            default.write_bytes(b'binary fixture')
            self.assertEqual(resolve_ffmpeg(fallback_env, which=lambda name: None), default)

    def test_1x_uses_verified_source_without_ffmpeg(self):
        with tempfile.TemporaryDirectory() as td:
            source = write_pcm(Path(td)/'input.wav')
            player = AudioPlayback(cache_dir=Path(td)/'cache', ffmpeg=None,
                                   runner=Mock(side_effect=AssertionError('must not run')),
                                   play_fn=Mock(), stop_fn=Mock())
            prepared = player.prepare(source, 1.0)
            self.assertEqual(prepared.path, source)
            self.assertFalse(prepared.temporary)
            self.assertEqual(prepared.source_sha256, sha(source))
            player.start(prepared)
            self.assertEqual(player.play_fn.call_args.args[0], source)
            player.stop()

    def test_atempo_copy_preserves_pitch_format_and_source_bytes(self):
        ffmpeg = resolve_ffmpeg()
        if ffmpeg is None:
            self.skipTest('no local ffmpeg; no download attempted')
        with tempfile.TemporaryDirectory() as td:
            source = write_pcm(Path(td)/'source.wav')
            original_sha = sha(source)
            played, stopped = [], []
            player = AudioPlayback(cache_dir=Path(td)/'cache', ffmpeg=ffmpeg,
                                   play_fn=played.append, stop_fn=lambda: stopped.append(True))
            prepared = player.prepare(source, 1.08)
            self.assertTrue(prepared.temporary)
            self.assertEqual(prepared.source_sha256, original_sha)
            self.assertEqual(sha(source), original_sha)
            self.assertEqual(prepared.sha256, sha(prepared.path))
            with wave.open(str(prepared.path), 'rb') as stream:
                self.assertEqual((stream.getnchannels(), stream.getsampwidth(), stream.getframerate()), (1, 2, 24000))
                duration = stream.getnframes() / stream.getframerate()
            self.assertAlmostEqual(duration, 1 / 1.08, delta=0.08)
            player.start(prepared)
            self.assertEqual(played, [prepared.path])
            player.stop()
            self.assertTrue(stopped)
            self.assertFalse(prepared.path.exists())

    def test_stop_invalidates_pending_conversion_before_play(self):
        with tempfile.TemporaryDirectory() as td:
            source = write_pcm(Path(td)/'source.wav')
            played = []
            def fake_ffmpeg(command, **kwargs):
                write_pcm(command[-1], frames=22000)
                return type('Result', (), {'returncode':0})()
            fake_binary = Path(td)/'fake-ffmpeg.exe'; fake_binary.write_bytes(b'test fixture')
            player = AudioPlayback(cache_dir=Path(td)/'cache', ffmpeg=fake_binary,
                                   runner=fake_ffmpeg, play_fn=played.append, stop_fn=lambda: None)
            prepared = player.prepare(source, 1.08)
            player.stop()
            with self.assertRaises(PlaybackCancelled):
                player.start(prepared)
            self.assertFalse(prepared.path.exists())
            self.assertEqual(played, [])

    def test_stop_during_source_hash_keeps_pre_stop_generation(self):
        with tempfile.TemporaryDirectory() as td:
            source = write_pcm(Path(td)/'source.wav')
            played = []
            fake_binary = Path(td)/'fake-ffmpeg.exe'; fake_binary.write_bytes(b'test fixture')
            def fake_ffmpeg(command, **kwargs):
                write_pcm(command[-1], frames=22000)
                return type('Result', (), {'returncode':0})()
            player = AudioPlayback(cache_dir=Path(td)/'cache', ffmpeg=fake_binary,
                                   runner=fake_ffmpeg, play_fn=played.append, stop_fn=lambda: None)
            read_info = audio_playback._wav_info
            def stop_during_read(path):
                info = read_info(path)
                if Path(path) == source:
                    player.stop()
                return info
            with patch.object(audio_playback, '_wav_info', side_effect=stop_during_read):
                prepared = player.prepare(source, 1.08)
            with self.assertRaises(PlaybackCancelled):
                player.start(prepared)
            self.assertFalse(prepared.path.exists())
            self.assertEqual(played, [])

    def test_tampered_temporary_wav_fails_hash_before_playback(self):
        ffmpeg = resolve_ffmpeg()
        if ffmpeg is None:
            self.skipTest('no local ffmpeg; no download attempted')
        with tempfile.TemporaryDirectory() as td:
            source = write_pcm(Path(td)/'source.wav')
            played = []
            player = AudioPlayback(cache_dir=Path(td)/'cache', ffmpeg=ffmpeg,
                                   play_fn=played.append, stop_fn=lambda: None)
            prepared = player.prepare(source, 1.1)
            prepared.path.write_bytes(prepared.path.read_bytes() + b'tamper')
            with self.assertRaisesRegex(ValueError, '哈希校验失败'):
                player.start(prepared)
            self.assertFalse(prepared.path.exists())
            self.assertEqual(played, [])


if __name__ == '__main__':
    unittest.main()
