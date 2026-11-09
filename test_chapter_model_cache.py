import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import chapter_model_cache as cache


class ChapterModelCacheTests(unittest.TestCase):
    def test_cow_only_and_confined_hf_file_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); hf = root / 'hf'; snapshot = hf / 'hub' / 'models' / 'snapshots' / 'rev'
            blob = hf / 'hub' / 'models' / 'blobs' / 'weight'; snapshot.mkdir(parents=True); blob.parent.mkdir(parents=True)
            blob.write_bytes(b'model bytes')
            try: (snapshot / 'weights.bin').symlink_to(blob)
            except OSError: self.skipTest('file symlink creation is unavailable')
            calls = []
            def clonefile(src, dst, flags):
                calls.append((Path(os.fsdecode(src)), Path(os.fsdecode(dst)), flags))
                Path(os.fsdecode(dst)).write_bytes(Path(os.fsdecode(src)).read_bytes())
                return 0
            fake = types.SimpleNamespace(clonefile=clonefile)
            with mock.patch.object(cache.sys, 'platform', 'darwin'), mock.patch.object(cache.ctypes, 'CDLL', return_value=fake):
                before = cache.model_tree_inventory(snapshot, allowed_file_link_root=hf)
                clone = cache.clone_model_tree(snapshot, root / 'clone', allowed_file_link_root=hf)
                after = cache.model_tree_inventory(clone)
            self.assertEqual(before, after)
            self.assertEqual((clone / 'weights.bin').read_bytes(), b'model bytes')
            self.assertFalse((clone / 'weights.bin').is_symlink())
            self.assertEqual(calls[0][2], 0)

    def test_file_link_outside_cache_and_directory_link_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); hf = root / 'hf'; snapshot = hf / 'snapshot'; snapshot.mkdir(parents=True)
            outside = root / 'outside.bin'; outside.write_bytes(b'outside')
            try: (snapshot / 'escape.bin').symlink_to(outside)
            except OSError: self.skipTest('file symlink creation is unavailable')
            with self.assertRaises(ValueError): cache.model_tree_inventory(snapshot, allowed_file_link_root=hf)
            (snapshot / 'escape.bin').unlink()
            outside_dir = root / 'outside-dir'; outside_dir.mkdir()
            try: (snapshot / 'linked-dir').symlink_to(outside_dir, target_is_directory=True)
            except OSError: self.skipTest('directory symlink creation is unavailable')
            with self.assertRaises(ValueError): cache.model_tree_inventory(snapshot, allowed_file_link_root=hf)

    def test_clonefile_failure_does_not_copy_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); src = root / 'src'; src.mkdir(); (src / 'weights').write_bytes(b'x')
            fake = types.SimpleNamespace(clonefile=lambda *_: -1)
            with mock.patch.object(cache.sys, 'platform', 'darwin'), \
                    mock.patch.object(cache.ctypes, 'CDLL', return_value=fake), \
                    mock.patch.object(cache.ctypes, 'get_errno', return_value=5), \
                    mock.patch.object(cache.shutil, 'copyfile', side_effect=AssertionError('copy fallback')):
                with self.assertRaises(OSError): cache.clone_model_tree(src, root / 'out')


if __name__ == '__main__': unittest.main()
