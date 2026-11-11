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
            generic = root/'generic-ffmpeg'; generic.write_bytes(b'binary fixture')
            env = {'AUDIOBOOK_PLAYBACK_FFMPEG':str(override), 'AUDIOBOOK_FFMPEG':str(generic), 'LOCALAPPDATA':td}
            self.assertEqual(resolve_ffmpeg(env, which=lambda name: str(path_binary)), override)
            self.assertEqual(resolve_ffmpeg({'AUDIOBOOK_FFMPEG':str(generic)}, which=lambda name: str(path_binary)), generic)
            self.assertEqual(resolve_ffmpeg({}, which=lambda name: str(path_binary), explicit=override), override)
            with self.assertRaises(FileNotFoundError):
                resolve_ffmpeg({'AUDIOBOOK_PLAYBACK_FFMPEG':str(root/'missing')}, which=lambda name: str(path_binary))
            with self.assertRaises(FileNotFoundError):
                resolve_ffmpeg({'AUDIOBOOK_FFMPEG':str(root/'missing')}, which=lambda name: str(path_binary))
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
            self.assertTrue(prepared.path.exists())

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
            self.assertTrue(prepared.path.exists())
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
                with self.assertRaises(PlaybackCancelled):
                    player.prepare(source, 1.08)
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
            self.assertTrue(prepared.path.exists())
            self.assertEqual(played, [])

    def test_cache_reused_after_stop_and_source_change_rebuilds(self):
        with tempfile.TemporaryDirectory() as td:
            source = write_pcm(Path(td)/'source.wav')
            binary = Path(td)/'ffmpeg'; binary.write_bytes(b'fixture')
            calls = []
            def runner(command, **kwargs):
                calls.append(command)
                write_pcm(command[-1], frames=22000)
                return type('Result', (), {'returncode': 0})()
            player = AudioPlayback(cache_dir=Path(td)/'cache', ffmpeg=binary, runner=runner,
                                   play_fn=lambda path: None, stop_fn=lambda: None)
            first = player.prepare(source, 1.08)
            player.start(first); player.stop()
            second = player.prepare(source, 1.08)
            self.assertEqual(len(calls), 1)
            self.assertEqual(first.path, second.path)
            source.write_bytes(source.read_bytes() + b'changed')
            with self.assertRaises((ValueError, wave.Error)):
                player.start(second)

    def test_tampered_cache_is_rebuilt_and_failed_output_is_not_reusable(self):
        with tempfile.TemporaryDirectory() as td:
            source = write_pcm(Path(td)/'source.wav')
            binary = Path(td)/'ffmpeg'; binary.write_bytes(b'fixture')
            calls = []
            def runner(command, **kwargs):
                calls.append(command)
                write_pcm(command[-1], frames=22000)
                return type('Result', (), {'returncode': 0})()
            player = AudioPlayback(cache_dir=Path(td)/'cache', ffmpeg=binary, runner=runner,
                                   play_fn=lambda path: None, stop_fn=lambda: None)
            first = player.prepare(source, 1.08)
            first.path.write_bytes(b'truncated')
            second = player.prepare(source, 1.08)
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(sha(second.path), hashlib.sha256(b'truncated').hexdigest())

    def test_same_key_concurrent_prepare_runs_ffmpeg_once(self):
        import threading
        with tempfile.TemporaryDirectory() as td:
            source = write_pcm(Path(td)/'source.wav')
            binary = Path(td)/'ffmpeg'; binary.write_bytes(b'fixture')
            calls = []
            gate = threading.Barrier(3)
            def runner(command, **kwargs):
                calls.append(command)
                write_pcm(command[-1], frames=22000)
                return type('Result', (), {'returncode': 0})()
            player = AudioPlayback(cache_dir=Path(td)/'cache', ffmpeg=binary, runner=runner,
                                   play_fn=lambda path: None, stop_fn=lambda: None)
            results = []
            def prepare():
                gate.wait()
                results.append(player.prepare(source, 1.08))
            threads = [threading.Thread(target=prepare) for _ in range(2)]
            for thread in threads: thread.start()
            gate.wait()
            for thread in threads: thread.join()
            self.assertEqual(len(calls), 1)
            self.assertEqual(results[0].path, results[1].path)

    def test_lru_keeps_prepared_cache_pinned_until_released(self):
        with tempfile.TemporaryDirectory() as td:
            source = write_pcm(Path(td)/'source.wav')
            binary = Path(td)/'ffmpeg'; binary.write_bytes(b'fixture')
            def runner(command, **kwargs):
                write_pcm(command[-1], frames=22000)
                return type('Result', (), {'returncode': 0})()
            player = AudioPlayback(cache_dir=Path(td)/'cache', ffmpeg=binary, runner=runner,
                                   play_fn=lambda path: None, stop_fn=lambda: None)
            player._cache_limit_bytes = 1
            prepared = player.prepare(source, 1.08)
            self.assertTrue(prepared.path.exists())
            player.discard(prepared)
            player._cleanup_cache()
            self.assertFalse(prepared.path.exists())

    def test_invalid_partial_output_is_removed_and_not_cached(self):
        with tempfile.TemporaryDirectory() as td:
            source = write_pcm(Path(td)/'source.wav')
            binary = Path(td)/'ffmpeg'; binary.write_bytes(b'fixture')
            cache = Path(td)/'cache'
            def runner(command, **kwargs):
                Path(command[-1]).write_bytes(b'truncated')
                return type('Result', (), {'returncode': 0})()
            player = AudioPlayback(cache_dir=cache, ffmpeg=binary, runner=runner,
                                   play_fn=lambda path: None, stop_fn=lambda: None)
            with self.assertRaises((wave.Error, ValueError)):
                player.prepare(source, 1.08)
            self.assertEqual(list(cache.glob('*.wav')), [])
            self.assertEqual(list(cache.glob('*.json')), [])
            self.assertEqual(list(cache.glob('*.tmp*')), [])

    def test_stale_prepared_discard_does_not_release_new_generation_lease(self):
        with tempfile.TemporaryDirectory() as td:
            source = write_pcm(Path(td)/'source.wav')
            binary = Path(td)/'ffmpeg'; binary.write_bytes(b'fixture')
            def runner(command, **kwargs):
                write_pcm(command[-1], frames=22000)
                return type('Result', (), {'returncode': 0})()
            player = AudioPlayback(cache_dir=Path(td)/'cache', ffmpeg=binary, runner=runner,
                                   play_fn=lambda path: None, stop_fn=lambda: None)
            stale = player.prepare(source, 1.08)
            player.stop()
            current = player.prepare(source, 1.08)
            player._cache_limit_bytes = 1
            with self.assertRaises(PlaybackCancelled):
                player.start(stale)
            player._cleanup_cache()
            self.assertTrue(current.path.exists())
            player.start(current)

    def test_removed_source_on_start_releases_only_that_prepared_lease(self):
        with tempfile.TemporaryDirectory() as td:
            source = write_pcm(Path(td)/'source.wav')
            binary = Path(td)/'ffmpeg'; binary.write_bytes(b'fixture')
            def runner(command, **kwargs):
                write_pcm(command[-1], frames=22000)
                return type('Result', (), {'returncode': 0})()
            player = AudioPlayback(cache_dir=Path(td)/'cache', ffmpeg=binary, runner=runner,
                                   play_fn=lambda path: None, stop_fn=lambda: None)
            prepared = player.prepare(source, 1.08)
            token = prepared._lease
            source.unlink()
            with self.assertRaises(FileNotFoundError):
                player.start(prepared)
            self.assertNotIn(token, audio_playback._CACHE_PINS.get(str(prepared.path.resolve()), set()))

    def test_cleanup_failure_after_stop_does_not_unpin_next_preparation(self):
        import threading
        with tempfile.TemporaryDirectory() as td:
            source = write_pcm(Path(td)/'source.wav')
            binary = Path(td)/'ffmpeg'; binary.write_bytes(b'fixture')
            def runner(command, **kwargs):
                write_pcm(command[-1], frames=22000)
                return type('Result', (), {'returncode': 0})()
            player = AudioPlayback(cache_dir=Path(td)/'cache', ffmpeg=binary, runner=runner,
                                   play_fn=lambda path: None, stop_fn=lambda: None)
            entered, release = threading.Event(), threading.Event()
            calls = []
            def cleanup():
                calls.append(True)
                if len(calls) == 1:
                    entered.set()
                    release.wait(5)
                    raise OSError('synthetic cleanup failure')
            player._cleanup_cache = cleanup
            errors = []
            thread = threading.Thread(target=lambda: self._capture_prepare_error(player, source, errors))
            thread.start()
            self.assertTrue(entered.wait(5))
            player.stop()
            current = player.prepare(source, 1.08)
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIn(current._lease,
                          audio_playback._CACHE_PINS.get(str(current.path.resolve()), set()))
            player.start(current)

    @staticmethod
    def _capture_prepare_error(player, source, errors):
        try:
            player.prepare(source, 1.08)
        except Exception as exc:
            errors.append(exc)

    def test_cleanup_never_deletes_cache_directory_source_or_legacy_uuid_audio(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)/'cache'; cache.mkdir()
            source = write_pcm(cache/'source.wav')
            source_sha = sha(source)
            legacy = cache/(audio_playback.uuid.uuid4().hex + '.wav')
            legacy.write_bytes(b'legacy file owned by caller')
            binary = Path(td)/'ffmpeg'; binary.write_bytes(b'fixture')
            def runner(command, **kwargs):
                input_path = Path(command[command.index('-i') + 1])
                speed = float(command[command.index('-filter:a') + 1].split('=')[1])
                with wave.open(str(input_path), 'rb') as stream:
                    frames, rate = stream.getnframes(), stream.getframerate()
                write_pcm(command[-1], frames=round(frames/speed), rate=rate)
                return type('Result', (), {'returncode':0})()
            player = AudioPlayback(cache_dir=cache, ffmpeg=binary, runner=runner,
                                   play_fn=lambda _path: None, stop_fn=lambda: None)
            player._cache_limit_bytes = 1
            prepared = player.prepare(source, 1.08)
            player.start(prepared)
            player.stop()
            self.assertTrue(source.exists())
            self.assertEqual(sha(source), source_sha)
            self.assertTrue(legacy.exists())
            self.assertFalse(prepared.path.exists())

    def test_source_that_is_module_cache_entry_is_pinned_across_prepare_start_and_other_cleanup(self):
        for playback_speed in (1.0, 1.1):
            with self.subTest(playback_speed=playback_speed), tempfile.TemporaryDirectory() as td:
                cache = Path(td)/'cache'
                source = write_pcm(Path(td)/'original.wav')
                binary = Path(td)/'ffmpeg'; binary.write_bytes(b'fixture')
                def runner(command, **kwargs):
                    input_path = Path(command[command.index('-i') + 1])
                    speed = float(command[command.index('-filter:a') + 1].split('=')[1])
                    with wave.open(str(input_path), 'rb') as stream:
                        frames, rate = stream.getnframes(), stream.getframerate()
                    write_pcm(command[-1], frames=round(frames/speed), rate=rate)
                    return type('Result', (), {'returncode':0})()
                first = AudioPlayback(cache_dir=cache, ffmpeg=binary, runner=runner,
                                      play_fn=lambda _path: None, stop_fn=lambda: None)
                cached_source = first.prepare(source, 1.08)
                source_path = cached_source.path
                self.assertRegex(source_path.stem, r'^[0-9a-f]{64}$')
                first.discard(cached_source)
                self.assertTrue(source_path.exists())

                player = AudioPlayback(cache_dir=cache, ffmpeg=binary, runner=runner,
                                       play_fn=lambda _path: None, stop_fn=lambda: None)
                prepared = player.prepare(source_path, playback_speed)
                cleaner = AudioPlayback(cache_dir=cache, ffmpeg=binary, runner=runner,
                                        play_fn=lambda _path: None, stop_fn=lambda: None)
                cleaner._cache_limit_bytes = 1
                cleaner._cleanup_cache()
                self.assertTrue(source_path.exists())
                player.start(prepared)
                cleaner._cleanup_cache()
                self.assertTrue(source_path.exists())
                player.stop()

    def test_warm_cache_stop_under_budget_skips_pcm_and_sha_scans(self):
        with tempfile.TemporaryDirectory() as td:
            source = write_pcm(Path(td)/'source.wav')
            binary = Path(td)/'ffmpeg'; binary.write_bytes(b'fixture')
            def runner(command, **kwargs):
                write_pcm(command[-1], frames=22222)
                return type('Result', (), {'returncode':0})()
            player = AudioPlayback(cache_dir=Path(td)/'cache', ffmpeg=binary, runner=runner,
                                   play_fn=lambda _path: None, stop_fn=lambda: None)
            prepared = player.prepare(source, 1.08)
            player.start(prepared)
            with patch.object(audio_playback, '_wav_info', side_effect=AssertionError('unexpected PCM scan')), \
                 patch.object(audio_playback, '_sha', side_effect=AssertionError('unexpected hash scan')):
                player.stop()


if __name__ == '__main__':
    unittest.main()
