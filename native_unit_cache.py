"""Hash-checked, content-addressed reuse of finished local Huashuo TTS units."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import wave

HASH = re.compile(r'[0-9a-f]{64}\Z')
KEY = re.compile(r'[0-9a-f]{20}\Z')


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _namespace(root, model_sha256, adapter_sha256):
    if not all(isinstance(x, str) and HASH.fullmatch(x) for x in (model_sha256, adapter_sha256)):
        raise ValueError('invalid unit cache namespace')
    return Path(root) / model_sha256 / adapter_sha256


def _pcm(path):
    with wave.open(str(path), 'rb') as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(),
                stream.getcomptype()) != (1, 2, 24000, 'NONE') or stream.getnframes() < 1:
            raise ValueError('cached unit PCM invalid')
        frames = stream.getnframes()
        read = 0
        for block in iter(lambda: stream.readframes(32768), b''):
            read += len(block)
        if read != frames * 2:
            raise ValueError('cached unit PCM truncated')


def _entry(entry, key):
    if entry.is_symlink() or (entry / 'entry.json').is_symlink():
        raise ValueError('unit cache symlink refused')
    manifest = json.loads((entry / 'entry.json').read_bytes())
    audio, metadata = entry / 'audio.wav', entry / 'metadata.json'
    if (manifest.get('key') != key or audio.is_symlink() or metadata.is_symlink()
            or _sha(audio) != manifest.get('wav_sha256')
            or _sha(metadata) != manifest.get('metadata_sha256')):
        raise ValueError('unit cache hash mismatch')
    meta = json.loads(metadata.read_bytes())
    if meta.get('key') != key or meta.get('problem') is not None:
        raise ValueError('unit cache is not an accepted finished unit')
    _pcm(audio)
    return meta, audio, metadata


def restore_units(root, destination, expected, *, model_sha256, adapter_sha256):
    """Copy only current planned keys whose source, voice and engine still agree."""
    namespace = _namespace(root, model_sha256, adapter_sha256)
    destination = Path(destination)
    restored, rejected = [], []
    for key, wanted in expected.items():
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise ValueError('invalid planned unit key')
        entry = namespace / key
        if not entry.exists():
            continue
        try:
            meta, audio, metadata = _entry(entry, key)
            if any(meta.get(field) != wanted.get(field) for field in ('text', 'voice', 'instruct', 'engine')):
                raise ValueError('unit cache source or engine differs')
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(audio, destination / (key + '.wav'))
            shutil.copyfile(metadata, destination / (key + '.json'))
            restored.append(key)
        except (OSError, ValueError, EOFError, wave.Error):
            rejected.append(key)
    return {'restored': restored, 'rejected': rejected}


def publish_units(root, source, *, model_sha256, adapter_sha256):
    """Publish complete pairs after the generator process has stopped."""
    namespace = _namespace(root, model_sha256, adapter_sha256)
    source = Path(source)
    published, skipped = [], []
    for metadata in sorted(source.glob('*.json')):
        key = metadata.stem
        if not KEY.fullmatch(key):
            continue
        audio = source / (key + '.wav')
        try:
            meta = json.loads(metadata.read_bytes())
            if (metadata.is_symlink() or audio.is_symlink() or meta.get('key') != key
                    or meta.get('problem') is not None or not isinstance(meta.get('engine'), dict)):
                raise ValueError('unfinished or flagged unit')
            _pcm(audio)
            target = namespace / key
            if target.exists():
                _entry(target, key)
                continue
            namespace.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix='.' + key + '-', dir=namespace))
            shutil.copyfile(audio, temporary / 'audio.wav')
            shutil.copyfile(metadata, temporary / 'metadata.json')
            manifest = {'key': key, 'wav_sha256': _sha(temporary / 'audio.wav'),
                        'metadata_sha256': _sha(temporary / 'metadata.json')}
            with (temporary / 'entry.json').open('x', encoding='utf-8') as stream:
                json.dump(manifest, stream, sort_keys=True, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, target)
            published.append(key)
        except (OSError, ValueError, EOFError, wave.Error):
            skipped.append(key)
    return {'published': published, 'skipped': skipped}
