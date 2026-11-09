"""Private, pitch-preserving playback speed conversion for verified WAV files.

This module never changes its source WAV and does not invoke models or network
services. Non-1x playback creates a temporary local PCM WAV with ffmpeg atempo.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
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


def resolve_ffmpeg(environ=None, which=shutil.which):
    env = os.environ if environ is None else environ
    override = env.get('AUDIOBOOK_PLAYBACK_FFMPEG')
    if override:
        path = Path(override)
        if path.is_file():
            return path
        raise FileNotFoundError('AUDIOBOOK_PLAYBACK_FFMPEG指定的文件不存在。')
    found = which('ffmpeg')
    if found:
        return Path(found)
    local = env.get('LOCALAPPDATA')
    if local:
        bundled = Path(local) / 'AudiobookTrials' / 'tools' / 'ffmpeg' / 'ffmpeg.exe'
        if bundled.is_file():
            return bundled
    return None


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


class AudioPlayback:
    def __init__(self, *, cache_dir=None, ffmpeg=None, runner=subprocess.run,
                 play_fn=None, stop_fn=None):
        if cache_dir is None:
            base = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir())
            cache_dir = base / 'AudiobookTrials' / 'playback-cache'
        self.cache_dir = Path(cache_dir)
        self.ffmpeg = Path(ffmpeg) if ffmpeg is not None else None
        self.runner = runner
        self.play_fn = play_fn or self._native_play
        self.stop_fn = stop_fn or self._native_stop
        self._generation = 0
        self._active_temporary = None
        self._lock = threading.Lock()

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
        speed = parse_speed(speed)
        generation = self._generation
        source_info, source_frames = _wav_info(source)
        source_sha = _sha(source)
        if speed == 1.0:
            return PreparedAudio(source, source, source_sha, source_sha, speed, generation, False)

        ffmpeg = self.ffmpeg or resolve_ffmpeg()
        if not ffmpeg or not Path(ffmpeg).is_file():
            raise FileNotFoundError('未找到可用ffmpeg，无法生成保音高倍速试听。')
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        output = self.cache_dir / (uuid.uuid4().hex + '.wav')
        command = [str(ffmpeg), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                   '-i', str(source), '-map', '0:a:0', '-filter:a', f'atempo={speed:.6f}',
                   '-vn', '-c:a', 'pcm_s16le', '-f', 'wav', str(output)]
        try:
            result = self.runner(command, capture_output=True, timeout=900,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode != 0 or not output.is_file():
                raise RuntimeError('ffmpeg倍速处理失败。')
            output_info, output_frames = _wav_info(output)
            if output_info != (source_info[0], 2, source_info[2], 'NONE'):
                raise ValueError('倍速副本格式不匹配。')
            source_duration = source_frames / source_info[2]
            output_duration = output_frames / output_info[2]
            if abs(output_duration - source_duration / speed) > max(0.15, source_duration * 0.015):
                raise ValueError('倍速副本时长不匹配。')
            if _sha(source) != source_sha:
                raise ValueError('原始音频在播放准备期间发生变化。')
            return PreparedAudio(source, output, source_sha, _sha(output), speed, generation, True)
        except Exception:
            output.unlink(missing_ok=True)
            raise

    def start(self, prepared):
        if not isinstance(prepared, PreparedAudio):
            raise TypeError('prepared audio required')
        if prepared.generation != self._generation:
            self.discard(prepared)
            raise PlaybackCancelled('播放准备已取消。')
        if not prepared.path.is_file():
            self.discard(prepared)
            raise FileNotFoundError('播放文件不存在。')
        if _sha(prepared.path) != prepared.sha256:
            self.discard(prepared)
            raise ValueError('播放文件哈希校验失败。')
        with self._lock:
            if prepared.generation != self._generation:
                self.discard(prepared)
                raise PlaybackCancelled('播放准备已取消。')
            self._stop_active()
            try:
                self.play_fn(prepared.path)
            except Exception:
                self.discard(prepared)
                raise
            self._active_temporary = prepared.path if prepared.temporary else None
        return prepared.path

    def discard(self, prepared):
        if isinstance(prepared, PreparedAudio) and prepared.temporary:
            prepared.path.unlink(missing_ok=True)

    def _stop_active(self):
        try:
            self.stop_fn()
        finally:
            if self._active_temporary is not None:
                self._active_temporary.unlink(missing_ok=True)
                self._active_temporary = None

    def stop(self):
        with self._lock:
            self._generation += 1
            self._stop_active()
