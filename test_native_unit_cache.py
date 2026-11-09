import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import native_unit_cache as cache


MODEL = 'a' * 64
ADAPTER = 'b' * 64
KEY = '1' * 20
ENGINE = {'name': 'synthetic-engine', 'version': 1}


def write_pcm(path, frames=8):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b'\x01\x00' * frames)
    return path


def metadata(key=KEY, *, text='hello', voice='voice-a', instruct='', engine=None, problem=None):
    return {'key': key, 'text': text, 'voice': voice, 'instruct': instruct,
            'engine': engine or ENGINE, 'problem': problem}


def expected(**overrides):
    value = {'text': 'hello', 'voice': 'voice-a', 'instruct': '', 'engine': ENGINE}
    value.update(overrides)
    return value


class NativeUnitCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache_root = self.root / 'unit-cache'
        self.source = self.root / 'source'
        self.source.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def put_source(self, *, meta=None, frames=8, truncate=0):
        write_pcm(self.source / f'{KEY}.wav', frames)
        if truncate:
            audio = self.source / f'{KEY}.wav'
            audio.write_bytes(audio.read_bytes()[:-truncate])
        (self.source / f'{KEY}.json').write_text(
            json.dumps(meta or metadata()), encoding='utf-8')

    def publish(self, *, meta=None, frames=8, truncate=0):
        self.put_source(meta=meta, frames=frames, truncate=truncate)
        return cache.publish_units(self.cache_root, self.source,
                                   model_sha256=MODEL, adapter_sha256=ADAPTER)

    def test_complete_unit_publishes_and_restores_only_matching_plan(self):
        result = self.publish()
        self.assertEqual(result, {'published': [KEY], 'skipped': []})
        destination = self.root / 'units'
        restored = cache.restore_units(self.cache_root, destination, {KEY: expected()},
                                       model_sha256=MODEL, adapter_sha256=ADAPTER)
        self.assertEqual(restored, {'restored': [KEY], 'rejected': []})
        entry = self.cache_root / MODEL / ADAPTER / KEY
        self.assertEqual((destination / f'{KEY}.wav').read_bytes(), (entry / 'audio.wav').read_bytes())
        self.assertEqual((destination / f'{KEY}.json').read_bytes(), (entry / 'metadata.json').read_bytes())

    def test_publish_skips_flagged_missing_and_truncated_units(self):
        flagged = self.publish(meta=metadata(problem='generation_failed'))
        self.assertEqual(flagged['published'], [])
        self.assertEqual(flagged['skipped'], [KEY])

        (self.source / f'{KEY}.json').unlink()
        self.assertEqual(cache.publish_units(self.cache_root, self.source,
                         model_sha256=MODEL, adapter_sha256=ADAPTER),
                         {'published': [], 'skipped': []})

        self.put_source(truncate=4)
        truncated = cache.publish_units(self.cache_root, self.source,
                                        model_sha256=MODEL, adapter_sha256=ADAPTER)
        self.assertEqual(truncated['published'], [])
        self.assertEqual(truncated['skipped'], [KEY])

    def test_restore_rejects_any_text_voice_instruct_or_engine_change(self):
        self.publish()
        variants = (
            {'text': 'changed'},
            {'voice': 'voice-b'},
            {'instruct': 'different'},
            {'engine': {'name': 'other-engine', 'version': 1}},
        )
        for change in variants:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as td:
                destination = Path(td)
                result = cache.restore_units(self.cache_root, destination, {KEY: expected(**change)},
                                             model_sha256=MODEL, adapter_sha256=ADAPTER)
                self.assertEqual(result, {'restored': [], 'rejected': [KEY]})
                self.assertFalse(list(destination.iterdir()))

    def test_model_or_adapter_namespace_change_does_not_restore(self):
        self.publish()
        for model, adapter in (('c' * 64, ADAPTER), (MODEL, 'd' * 64)):
            with self.subTest(model=model, adapter=adapter), tempfile.TemporaryDirectory() as td:
                result = cache.restore_units(self.cache_root, Path(td), {KEY: expected()},
                                             model_sha256=model, adapter_sha256=adapter)
                self.assertEqual(result, {'restored': [], 'rejected': []})

    def test_audio_or_metadata_tamper_is_rejected_without_copy(self):
        self.publish()
        entry = self.cache_root / MODEL / ADAPTER / KEY
        for name in ('audio.wav', 'metadata.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                original = (entry / name).read_bytes()
                (entry / name).write_bytes(original + b'tamper')
                result = cache.restore_units(self.cache_root, Path(td), {KEY: expected()},
                                             model_sha256=MODEL, adapter_sha256=ADAPTER)
                self.assertEqual(result, {'restored': [], 'rejected': [KEY]})
                self.assertFalse(list(Path(td).iterdir()))
                (entry / name).write_bytes(original)

    def test_corrupt_entry_manifest_is_rejected(self):
        self.publish()
        entry = self.cache_root / MODEL / ADAPTER / KEY
        manifest_path = entry / 'entry.json'
        manifest = json.loads(manifest_path.read_bytes())
        manifest['key'] = '2' * 20
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        with tempfile.TemporaryDirectory() as td:
            result = cache.restore_units(self.cache_root, Path(td), {KEY: expected()},
                                         model_sha256=MODEL, adapter_sha256=ADAPTER)
        self.assertEqual(result, {'restored': [], 'rejected': [KEY]})

    def test_entry_manifest_symlink_is_rejected(self):
        self.publish()
        entry = self.cache_root / MODEL / ADAPTER / KEY
        manifest = entry / 'entry.json'
        outside = self.root / 'external-manifest.json'
        outside.write_bytes(manifest.read_bytes())
        real_is_symlink = Path.is_symlink
        real_read_bytes = Path.read_bytes
        def is_symlink(path):
            return True if Path(path) == manifest else real_is_symlink(path)
        def read_bytes(path):
            return real_read_bytes(outside) if Path(path) == manifest else real_read_bytes(path)
        with tempfile.TemporaryDirectory() as td:
            with patch.object(Path, 'is_symlink', is_symlink), patch.object(Path, 'read_bytes', read_bytes):
                result = cache.restore_units(self.cache_root, Path(td), {KEY: expected()},
                                             model_sha256=MODEL, adapter_sha256=ADAPTER)
        self.assertEqual(result, {'restored': [], 'rejected': [KEY]})


if __name__ == '__main__':
    unittest.main()
