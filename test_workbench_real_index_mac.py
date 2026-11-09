import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import book_workspace_snapshot as workspace
import chapter_local_index as chapters
import persisted_chapter_index as persisted
import test_book_workspace_snapshot as fixture
import workbench_real_index_mac as bridge


class SyntheticIndexTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.BookWorkspaceSnapshotTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        book = json.loads((self.root / 'book.json').read_text(encoding='utf8'))
        book['book_id'] = 'book-' + 'a' * 20
        (self.root / 'book.json').write_text(json.dumps(book), encoding='utf8')
        snap = workspace.read_workspace_snapshot(book_root=self.root, workdir=self.case.workdir, expected_book_id='book-' + 'a' * 20)
        index = chapters.build_index(book_id=snap.book_id, source_bytes=snap.source_bytes, normalized_text=snap.normalized_text, script_rows=snap.script_rows, cast=snap.cast, trusted_snapshot=snap.persisted_index_snapshot)
        dest = self.case.parent / 'index'
        persisted.store_chapter_index(index, dest, trusted_snapshot=snap.persisted_index_snapshot, expected_index_sha256=index.index_sha256)
        registry = {'version': 1, 'books': {'book-' + 'a' * 20: {'book_root': str(self.root), 'workdir': str(self.case.workdir), 'index_workspace': str(dest), 'snapshot': snap.persisted_index_snapshot, 'index_sha256': index.index_sha256}}}
        self.path = self.case.parent / 'registry.json'
        self.path.write_text(json.dumps(registry), encoding='utf8')
        self.config = self.case.parent / 'config.json'
        self.config.write_text(json.dumps({'mac': {'index_registry': str(self.path)}}), encoding='utf8')
        self.env = patch.dict(os.environ, {'AUDIOBOOK_CONFIG_FILE': str(self.config)})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_list_and_only_selected_chapter(self):
        listing = bridge.query({'action': 'list', 'book_id': 'book-' + 'a' * 20})
        self.assertEqual(listing['chapters'], ['c001', 'c002'])
        output = bridge.query({'action': 'prepare', 'book_id': 'book-' + 'a' * 20, 'chapter_id': 'c002'})
        self.assertEqual(output['payload']['chapter_id'], 'c002')
        self.assertIn('夜色安静', output['payload']['text'])
        self.assertNotIn('星⭐来了', output['payload']['text'])

    def test_unregistered_and_injected_path_rejected(self):
        for request in ({'action': 'list', 'book_id': 'other'}, {'action': 'prepare', 'book_id': 'book-' + 'a' * 20, 'chapter_id': '../c001'}, {'action': 'list', 'book_id': 'book-' + 'a' * 20, 'path': str(self.root)}):
            with self.subTest(request=request), self.assertRaises(Exception): bridge.query(request)

    def test_one_shot_process_uses_configured_registry(self):
        result = subprocess.run([sys.executable, '-B', 'workbench_real_index_mac.py'], input=json.dumps({'action': 'prepare', 'book_id': 'book-' + 'a' * 20, 'chapter_id': 'c001'}), text=True, encoding='utf8', capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output['ok'])
        self.assertEqual(output['data']['payload']['chapter_id'], 'c001')

    def test_changed_source_fails(self):
        (self.root / 'original.docx').write_bytes(b'changed')
        with self.assertRaises(Exception): bridge.query({'action': 'list', 'book_id': 'book-' + 'a' * 20})

    def test_missing_index_lock_fails_without_creating_it(self):
        index_lock = self.case.parent / '.index.lock'
        index_lock.unlink()
        with self.assertRaises(Exception): bridge.query({'action': 'list', 'book_id': 'book-' + 'a' * 20})
        self.assertFalse(index_lock.exists())


if __name__ == '__main__': unittest.main()
