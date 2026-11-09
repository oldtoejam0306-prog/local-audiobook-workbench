import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import chapter_local_index
import book_workspace_snapshot as snapshot_module


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical_sha(value):
    return chapter_local_index.object_sha256(value)


class BookWorkspaceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='workspace-snapshot-')
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name).resolve()
        self.root = self.parent / 'book-A'
        self.workdir = self.root / 'work.huashuo'
        self.workdir.mkdir(parents=True)
        self.source = b'synthetic DOCX bytes only'
        self.text = '第一章\n星⭐来了。\n“你好吗？”\n第二章\n夜色安静。\n再见。\n'
        self.text_bytes = self.text.encode('utf-8')
        self.text_sha = sha(self.text_bytes)
        self.intermediate = b'synthetic intermediate txt bytes'
        self.rows = [
            {'type': 'huaben', 'version': 1, 'title': 'Synthetic', 'language': 'zh',
             'text': 'text.txt', 'text_sha256': self.text_sha, 'author': 'Synthetic Author',
             'source': {'format': 'TXT', 'path': '../../imported/intermediate.txt',
                        'sha256': sha(self.intermediate)}},
            {'type': 'chapter', 'id': 'c001', 'text': '第一章', 'src': [0, 3]},
            {'type': 'narration', 'id': 'n001', 'text': '星⭐来了。', 'src': [4, 9]},
            {'type': 'dialogue', 'id': 'd001', 'text': '“你好吗？”', 'speaker': 'unknown', 'src': [10, 16]},
            {'type': 'chapter', 'id': 'c002', 'text': '第二章', 'src': [17, 20]},
            {'type': 'narration', 'id': 'n002', 'text': '夜色安静。', 'src': [21, 26]},
            {'type': 'dialogue', 'id': 'd002', 'text': '再见。', 'speaker': '林舟', 'src': [27, 30]},
        ]
        self.cast = {'version': 1, 'narrator': {'voice': 'preset:Vivian'},
                     'characters': {'林舟': {'voice': 'preset:Serena'}}}
        self.source_sha = sha(self.source)
        manifest = {'version': 1, 'book_id': 'book-A', 'format': '.docx',
                    'source_sha256': self.source_sha, 'input_file': 'book.txt'}
        (self.root / 'original.docx').write_bytes(self.source)
        (self.root / 'book.json').write_text(json.dumps(manifest), encoding='utf-8')
        (self.workdir / 'text.txt').write_bytes(self.text_bytes)
        (self.workdir / 'script.huaben.jsonl').write_text(
            ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in self.rows), encoding='utf-8')
        (self.workdir / 'cast.json').write_text(json.dumps(self.cast, ensure_ascii=False), encoding='utf-8')
        # Existing lock files are synthetic sentinels; the reader must not alter their bytes.
        (self.root / '.initialize.lock').write_bytes(b'book-lock-sentinel')
        (self.workdir / '.console.lock').write_bytes(b'work-lock-sentinel')
        self.expected_files = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        self.before_file_bytes = {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob('*') if path.is_file()
        }

    def read(self, **overrides):
        args = {'book_root': self.root, 'workdir': self.workdir, 'expected_book_id': 'book-A'}
        args.update(overrides)
        return snapshot_module.read_workspace_snapshot(**args)

    def test_valid_snapshot_binds_hashes_and_source_provenance_without_resolving_path(self):
        snapshot = self.read()
        self.assertEqual(snapshot.source_sha256, self.source_sha)
        self.assertEqual(snapshot.text_sha256, self.text_sha)
        self.assertEqual(snapshot.script_sha256, canonical_sha(self.rows))
        self.assertEqual(snapshot.cast_sha256, canonical_sha(self.cast))
        self.assertEqual(snapshot.persisted_index_snapshot['book_id'], 'book-A')
        provenance = snapshot.provenance
        self.assertFalse(provenance['source_header_intermediate_metadata']['sha256_matches_original_docx'])
        self.assertFalse(provenance['source_header_intermediate_metadata']['sha256_matches_text_file'])
        self.assertTrue(provenance['source_header_intermediate_metadata']['path_has_parent_component'])
        self.assertIs(provenance['source_header_intermediate_metadata']['path_resolved'], False)
        self.assertNotIn('../../imported/intermediate.txt', json.dumps(provenance))
        index = chapter_local_index.build_index(
            book_id=snapshot.book_id, source_bytes=snapshot.source_bytes,
            normalized_text=snapshot.normalized_text, script_rows=snapshot.script_rows,
            cast=snapshot.cast, trusted_snapshot=snapshot.persisted_index_snapshot)
        self.assertEqual(len(index.chapters), 2)
        self.assertEqual(sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')),
                         self.expected_files)
        for relative, before in self.before_file_bytes.items():
            self.assertEqual((self.root / relative).read_bytes(), before)

    def test_expected_book_id_and_workdir_escape_are_rejected(self):
        with self.assertRaisesRegex(snapshot_module.WorkspaceSnapshotError, 'book.json identity'):
            self.read(expected_book_id='book-B')
        outside = self.parent / 'outside-workdir'
        outside.mkdir()
        with self.assertRaisesRegex(snapshot_module.WorkspaceSnapshotError, 'work.huashuo'):
            self.read(workdir=outside)

    def test_unrelated_child_workspace_cannot_be_bound_to_this_book(self):
        unrelated = self.root / 'unrelated.huashuo'
        unrelated.mkdir()
        unrelated_text = '另一册书的内容。\n'
        (unrelated / 'text.txt').write_text(unrelated_text, encoding='utf-8')
        unrelated_cast = {'version': 1, 'narrator': {'voice': 'preset:Other'},
                          'characters': {}}
        unrelated_rows = [
            {'type': 'huaben', 'version': 1, 'title': 'Unrelated', 'language': 'zh',
             'text': 'text.txt', 'text_sha256': sha(unrelated_text.encode('utf-8')),
             'author': 'Synthetic', 'source': None},
            {'type': 'narration', 'id': 'n001', 'text': '另一册书的内容。', 'src': [0, 8]},
        ]
        (unrelated / 'script.huaben.jsonl').write_text(
            ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in unrelated_rows),
            encoding='utf-8')
        (unrelated / 'cast.json').write_text(
            json.dumps(unrelated_cast, ensure_ascii=False), encoding='utf-8')
        (unrelated / '.console.lock').write_bytes(b'unrelated-lock-sentinel')
        with self.assertRaisesRegex(snapshot_module.WorkspaceSnapshotError, 'work.huashuo'):
            self.read(workdir=unrelated)

    def test_hardlinked_input_is_rejected(self):
        source = self.root / 'original.docx'
        outside = self.parent / 'linked-outside'
        outside.write_bytes(source.read_bytes())
        source.unlink()
        try:
            os.link(outside, source)
        except OSError as exc:
            self.skipTest(f'hard links unavailable: {exc}')
        with self.assertRaisesRegex(snapshot_module.WorkspaceSnapshotError, 'single-link'):
            self.read()
        self.assertEqual(outside.read_bytes(), self.source)

    def test_symlink_input_is_rejected_when_platform_allows_creation(self):
        text = self.workdir / 'text.txt'
        outside = self.parent / 'outside-text'
        outside.write_bytes(self.text_bytes)
        text.unlink()
        try:
            os.symlink(outside, text)
        except OSError as exc:
            self.skipTest(f'symlinks unavailable: {exc}')
        with self.assertRaises(snapshot_module.WorkspaceSnapshotError):
            self.read()
        self.assertEqual(outside.read_bytes(), self.text_bytes)

    def test_change_during_read_is_detected(self):
        original_reader = snapshot_module._read_regular
        changed = {'done': False}

        def mutate_after_text_read(path, *, root, label):
            content, identity = original_reader(path, root=root, label=label)
            if label == 'text':
                changed['done'] = True
                path.write_bytes(self.text_bytes.replace('星⭐来了。'.encode('utf-8'),
                                                         '星⭐走了。'.encode('utf-8')))
            return content, identity

        with patch.object(snapshot_module, '_read_regular', side_effect=mutate_after_text_read):
            with self.assertRaisesRegex(snapshot_module.WorkspaceSnapshotChanged, 'changed during workspace snapshot'):
                self.read()
        self.assertTrue(changed['done'])

    def test_interrupted_read_releases_both_existing_locks_without_writing_them(self):
        original = snapshot_module._read_regular

        def interrupt(path, *, root, label):
            if label == 'script':
                raise OSError('synthetic interruption')
            return original(path, root=root, label=label)

        with patch.object(snapshot_module, '_read_regular', side_effect=interrupt):
            with self.assertRaises(snapshot_module.WorkspaceSnapshotError):
                self.read()
        for rel, before in self.before_file_bytes.items():
            if rel in ('.initialize.lock', 'work.huashuo/.console.lock'):
                self.assertEqual((self.root / rel).read_bytes(), before)
        recovered = self.read()
        self.assertEqual(recovered.book_id, 'book-A')

    def test_cross_process_lock_contention_blocks_snapshot(self):
        lock_path = self.workdir / '.console.lock'
        child_code = (
            'import sys,time; from pathlib import Path; import book_workspace_snapshot as s; '
            'p=Path(sys.argv[1]); '
            'ctx=s._lock_existing(p); '
            'ctx.__enter__(); print(\"locked\",flush=True); time.sleep(20); ctx.__exit__(None,None,None)'
        )
        env = os.environ.copy()
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        proc = subprocess.Popen([sys.executable, '-B', '-c', child_code, str(lock_path)],
                                cwd=Path(__file__).resolve().parent, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'locked')
            with self.assertRaises(snapshot_module.WorkspaceSnapshotBusy):
                self.read()
        finally:
            proc.terminate()
            proc.communicate(timeout=5)
        self.assertEqual((self.workdir / '.console.lock').read_bytes(), b'work-lock-sentinel')


if __name__ == '__main__':
    unittest.main()
