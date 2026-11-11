"""Private, pitch-preserving playback speed conversion for verified WAV files.

This module never changes its source WAV and does not invoke models or network
services. Non-1x playback creates a temporary local PCM WAV with ffmpeg atempo.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
import wave


class PlaybackCancelled(RuntimeError):
    """Raised when a stop request invalidates an unfinished playback result."""


def parse_speed(value):
    try:
        speed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('播放速度必须是0.8至1.5之间的数字。') from exc
    if not 0.8 <= speed <= 1.5:
        raise ValueError('播放速度必须在0.8至1.5之间。')
    return speed


def resolve_ffmpeg(environ=None, which=shutil.which, explicit=None):
    env = os.environ if environ is None else environ
    if explicit is not None:
        path = Path(explicit)
        if path.is_file():
            return path
        raise FileNotFoundError('构造参数指定的ffmpeg文件不存在。')
    override = env.get('AUDIOBOOK_PLAYBACK_FFMPEG')
    if override:
        path = Path(override)
        if path.is_file():
            return path
        raise FileNotFoundError('AUDIOBOOK_PLAYBACK_FFMPEG指定的文件不存在。')
    general = env.get('AUDIOBOOK_FFMPEG')
    if general:
        path = Path(general)
        if path.is_file():
            return path
        raise FileNotFoundError('AUDIOBOOK_FFMPEG指定的文件不存在。')
    found = which('ffmpeg')
    if found:
        return Path(found)
    local = env.get('LOCALAPPDATA')
    if local:
        bundled = Path(local) / 'AudiobookTrials' / 'tools' / 'ffmpeg' / 'ffmpeg.exe'
        if bundled.is_file():
            return bundled
    return None


_CACHE_LOCKS = {}
_CACHE_LOCKS_GUARD = threading.Lock()
_CACHE_PINS = {}
_CACHE_PINS_GUARD = threading.RLock()


def _cache_lock(key):
    with _CACHE_LOCKS_GUARD:
        return _CACHE_LOCKS.setdefault(key, threading.Lock())


def _pin(path, token=None):
    key = str(Path(path).resolve())
    token = token or uuid.uuid4().hex
    with _CACHE_PINS_GUARD:
        _CACHE_PINS.setdefault(key, set()).add(token)
    return token


def _unpin(path, token):
    key = str(Path(path).resolve())
    with _CACHE_PINS_GUARD:
        pins = _CACHE_PINS.get(key)
        if pins is None:
            return
        pins.discard(token)
        if not pins:
            _CACHE_PINS.pop(key, None)


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _wav_info(path):
    with wave.open(str(path), 'rb') as stream:
        info = (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype())
        frames = stream.getnframes()
        if (info[0] not in (1, 2) or info[1] not in (1, 2, 3, 4)
                or info[2] < 8000 or info[2] > 192000 or info[3] != 'NONE' or frames < 1):
            raise ValueError('WAV必须是未压缩PCM音频。')
        frame_bytes = info[0] * info[1]
        read = 0
        for block in iter(lambda: stream.readframes(32768), b''):
            read += len(block)
        if read != frames * frame_bytes:
            raise ValueError('WAV音频数据不完整。')
        return info, frames


@dataclass(frozen=True)
class PreparedAudio:
    source: Path
    path: Path
    source_sha256: str
    sha256: str
    speed: float
    generation: int
    temporary: bool
    _lease: str = None
    _source_lease: str = None


class AudioPlayback:
    def __init__(self, *, cache_dir=None, ffmpeg=None, runner=subprocess.run,
                 play_fn=None, stop_fn=None):
        if cache_dir is None:
            base = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir())
            cache_dir = base / 'AudiobookTrials' / 'playback-cache'
        self.cache_dir = Path(cache_dir)
        self.ffmpeg = resolve_ffmpeg(explicit=ffmpeg) if ffmpeg is not None else None
        self.runner = runner
        self.play_fn = play_fn or self._native_play
        self.stop_fn = stop_fn or self._native_stop
        self._generation = 0
        self._active_temporary = None
        self._active_pin = None
        self._active_source = None
        self._active_source_pin = None
        self._lock = threading.Lock()
        self._prepared_leases = {}
        self._prepared_sources = {}
        self._cache_limit_bytes = 512 * 1024 * 1024

    @staticmethod
    def _native_play(path):
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)

    @staticmethod
    def _native_stop():
        import winsound
        winsound.PlaySound(None, 0)

    def prepare(self, source, speed=1.0):
        source = Path(source)
        speed = float(f'{parse_speed(speed):.6f}')
        with self._lock:
            generation = self._generation
        source_lease = uuid.uuid4().hex
        source_registered = False
        _pin(source, source_lease)
        try:
            source_info, source_frames = _wav_info(source)
            source_sha = _sha(source)
            if speed == 1.0:
                with self._lock:
                    if generation != self._generation:
                        raise PlaybackCancelled('播放准备已取消。')
                    self._prepared_sources[source_lease] = source
                    source_registered = True
                return PreparedAudio(source, source, source_sha, source_sha, speed, generation,
                                     False, None, source_lease)

            ffmpeg = self.ffmpeg if self.ffmpeg is not None else resolve_ffmpeg()
            if not ffmpeg:
                raise FileNotFoundError('未找到可用ffmpeg，无法生成保音高倍速试听。')
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            key = hashlib.sha256(f'{source_sha}:{speed:.6f}'.encode()).hexdigest()
            output = self.cache_dir / (key + '.wav')
            metadata = self.cache_dir / (key + '.json')
            if output.resolve() == source.resolve():
                raise ValueError('原音频路径与倍速缓存目标冲突。')
            lease = uuid.uuid4().hex
            lease_registered = False
            _pin(output, lease)
            try:
                with _cache_lock(str(output.resolve())):
                    cached_sha = self._valid_cache(output, metadata, source_sha, speed,
                                                   source_info, source_frames)
                    if cached_sha is None:
                        output.unlink(missing_ok=True)
                        metadata.unlink(missing_ok=True)
                        temp_output = self.cache_dir / (key + '.' + uuid.uuid4().hex + '.tmp.wav')
                        temp_meta = None
                        command = [str(ffmpeg), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                                   '-i', str(source), '-map', '0:a:0', '-filter:a', f'atempo={speed:.6f}',
                                   '-vn', '-c:a', 'pcm_s16le', '-f', 'wav', str(temp_output)]
                        try:
                            result = self.runner(command, capture_output=True, timeout=900,
                                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                            if result.returncode != 0 or not temp_output.is_file():
                                raise RuntimeError('ffmpeg倍速处理失败。')
                            output_info, output_frames = _wav_info(temp_output)
                            if output_info != (source_info[0], 2, source_info[2], 'NONE'):
                                raise ValueError('倍速副本格式不匹配。')
                            source_duration = source_frames / source_info[2]
                            output_duration = output_frames / output_info[2]
                            if abs(output_duration - source_duration / speed) > max(0.15, source_duration * 0.015):
                                raise ValueError('倍速副本时长不匹配。')
                            if _sha(source) != source_sha:
                                raise ValueError('原始音频在播放准备期间发生变化。')
                            cached_sha = _sha(temp_output)
                            os.replace(temp_output, output)
                            payload = {'source_sha256': source_sha, 'sha256': cached_sha,
                                       'speed': speed, 'info': list(output_info), 'frames': output_frames}
                            temp_meta = metadata.with_name(metadata.name + '.' + uuid.uuid4().hex + '.tmp')
                            temp_meta.write_text(json.dumps(payload, sort_keys=True), encoding='utf-8')
                            os.replace(temp_meta, metadata)
                        except Exception:
                            temp_output.unlink(missing_ok=True)
                            if temp_meta is not None:
                                temp_meta.unlink(missing_ok=True)
                            output.unlink(missing_ok=True)
                            metadata.unlink(missing_ok=True)
                            raise
                    if _sha(source) != source_sha:
                        raise ValueError('原始音频在播放准备期间发生变化。')
                    with self._lock:
                        if generation != self._generation:
                            raise PlaybackCancelled('播放准备已取消。')
                        self._prepared_leases[lease] = output
                        self._prepared_sources[source_lease] = source
                        lease_registered = True
                        source_registered = True
                    os.utime(output, None)
                    self._cleanup_cache()
                    return PreparedAudio(source, output, source_sha, cached_sha, speed, generation,
                                         True, lease, source_lease)

            except Exception:
                if lease_registered:
                    with self._lock:
                        owned = self._prepared_leases.pop(lease, None) is not None
                    if owned:
                        _unpin(output, lease)
                else:
                    _unpin(output, lease)
                raise

        except Exception:
            if source_registered:
                with self._lock:
                    owned = self._prepared_sources.pop(source_lease, None) is not None
                if owned:
                    _unpin(source, source_lease)
            else:
                _unpin(source, source_lease)
            raise

    @staticmethod
    def _valid_cache(output, metadata, source_sha, speed, source_info, source_frames):
        try:
            payload = json.loads(metadata.read_text(encoding='utf-8'))
            if (payload['source_sha256'] != source_sha or payload['speed'] != speed
                    or not output.is_file() or _sha(output) != payload['sha256']):
                return None
            info, frames = _wav_info(output)
            if (info != (source_info[0], 2, source_info[2], 'NONE')
                    or list(info) != payload['info'] or frames != payload['frames']):
                return None
            duration = frames / info[2]
            source_duration = source_frames / source_info[2]
            if abs(duration - source_duration / speed) > max(0.15, source_duration * 0.015):
                return None
            return payload['sha256']
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, wave.Error):
            return None

    def _cleanup_cache(self):
        candidates = []
        estimated_total = 0
        for path in self.cache_dir.glob('*.wav'):
            candidate = self._cache_metadata_candidate(path)
            if candidate is None:
                continue
            payload, size, modified = candidate
            candidates.append((modified, path, size, payload))
            estimated_total += size
        if estimated_total <= self._cache_limit_bytes:
            return

        entries = []
        total = 0
        for modified, path, size, payload in candidates:
            if self._owned_cache_entry(path, payload):
                total += size
                entries.append((modified, path, size))
        if total <= self._cache_limit_bytes:
            return
        with _CACHE_PINS_GUARD:
            for _, path, size in sorted(entries):
                if total <= self._cache_limit_bytes:
                    break
                if _CACHE_PINS.get(str(path.resolve())):
                    continue
                try:
                    path.unlink(missing_ok=True)
                    path.with_suffix('.json').unlink(missing_ok=True)
                    total -= size
                except OSError:
                    continue

    @staticmethod
    def _cache_metadata_candidate(path):
        """Cheaply recognize our cache naming and sidecar before any PCM scan."""
        path = Path(path)
        if (not re.fullmatch(r'[0-9a-f]{64}', path.stem) or path.is_symlink()
                or not path.is_file()):
            return None
        try:
            info = path.stat()
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        metadata = path.with_suffix('.json')
        if metadata.is_symlink() or not metadata.is_file():
            return None
        try:
            metadata_stat = metadata.stat()
            if not stat.S_ISREG(metadata_stat.st_mode) or metadata_stat.st_size > 64 * 1024:
                return None
            payload = json.loads(metadata.read_text(encoding='utf-8'))
            source_sha = payload['source_sha256']
            output_sha = payload['sha256']
            raw_speed = payload['speed']
            if (not isinstance(source_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', source_sha)
                    or not isinstance(output_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', output_sha)
                    or isinstance(raw_speed, bool) or not isinstance(raw_speed, (int, float))):
                return None
            speed = float(raw_speed)
            if speed != float(f'{parse_speed(speed):.6f}'):
                return None
            expected_key = hashlib.sha256(f'{source_sha}:{speed:.6f}'.encode()).hexdigest()
            if expected_key != path.stem:
                return None
            if not isinstance(payload.get('info'), list) or type(payload.get('frames')) is not int:
                return None
            return payload, info.st_size, info.st_mtime
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _owned_cache_entry(path, payload=None):
        """Fully verify candidate bytes only when the cache may need eviction."""
        path = Path(path)
        if payload is None:
            candidate = AudioPlayback._cache_metadata_candidate(path)
            if candidate is None:
                return False
            payload = candidate[0]
        try:
            info, frames = _wav_info(path)
            return (list(info) == payload['info'] and frames == payload['frames']
                    and _sha(path) == payload['sha256'])
        except (OSError, ValueError, KeyError, TypeError, wave.Error):
            return False

    def start(self, prepared):
        if not isinstance(prepared, PreparedAudio):
            raise TypeError('prepared audio required')
        if prepared.generation != self._generation:
            self.discard(prepared)
            raise PlaybackCancelled('播放准备已取消。')
        if not prepared.path.is_file():
            self.discard(prepared)
            raise FileNotFoundError('播放文件不存在。')
        try:
            source_sha = _sha(prepared.source)
        except OSError:
            self.discard(prepared)
            raise
        if source_sha != prepared.source_sha256:
            self.discard(prepared)
            raise ValueError('原始音频在播放准备后发生变化。')
        try:
            playback_sha = _sha(prepared.path)
        except OSError:
            self.discard(prepared)
            raise
        if playback_sha != prepared.sha256:
            self.discard(prepared)
            raise ValueError('播放文件哈希校验失败。')
        with self._lock:
            if prepared.generation != self._generation:
                self._release_prepared(prepared)
                raise PlaybackCancelled('播放准备已取消。')
            self._stop_active()
            active_source_pin = _pin(prepared.source)
            active_output_pin = _pin(prepared.path) if prepared.temporary else None
            try:
                self.play_fn(prepared.path)
            except Exception:
                _unpin(prepared.source, active_source_pin)
                if active_output_pin is not None:
                    _unpin(prepared.path, active_output_pin)
                self._release_prepared(prepared)
                raise
            self._active_temporary = prepared.path if prepared.temporary else None
            self._active_pin = active_output_pin
            self._active_source = prepared.source
            self._active_source_pin = active_source_pin
            self._release_prepared(prepared)
        return prepared.path

    def discard(self, prepared):
        if isinstance(prepared, PreparedAudio):
            with self._lock:
                self._release_prepared(prepared)
                self._cleanup_cache()

    def _release_prepared(self, prepared):
        output = self._prepared_leases.pop(prepared._lease, None)
        if output is not None:
            _unpin(output, prepared._lease)
        source = self._prepared_sources.pop(prepared._source_lease, None)
        if source is not None:
            _unpin(source, prepared._source_lease)

    def _stop_active(self):
        try:
            self.stop_fn()
        finally:
            if self._active_temporary is not None:
                _unpin(self._active_temporary, self._active_pin)
                self._active_temporary = None
                self._active_pin = None
            if self._active_source is not None:
                _unpin(self._active_source, self._active_source_pin)
                self._active_source = None
                self._active_source_pin = None

    def stop(self):
        with self._lock:
            self._generation += 1
            for lease, path in self._prepared_leases.items():
                _unpin(path, lease)
            self._prepared_leases.clear()
            for lease, path in self._prepared_sources.items():
                _unpin(path, lease)
            self._prepared_sources.clear()
            self._stop_active()
            self._cleanup_cache()
