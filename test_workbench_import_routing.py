"""Verify imported books through the normal chapter selection entry point."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workbench_book_import import BookImporter
import workbench_real
import workbench_real_flow as flow
import workbench_real_v2_runtime as runtime
from workbench_real_v2_ui import Selection
import real_pipeline_workflow as workflow


class ImportedBookRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.txt'
        self.text = ('第一章 夜班书店\n阿岚把书放回架上。\n'
                     '“小舟，灯还亮着。”阿岚说。\n'
                     '“我再整理一会儿。”小舟答道。\n'
                     '第二章 雨停以后\n窗外的雨已经停了。\n')
        self.source.write_text(self.text, encoding='utf-8')
        self.importer = BookImporter(self.root)
        self.preview = self.importer.preview(self.source, '夜班书店.txt')
        self.entry = self.importer.commit(self.preview['preview_id'], '夜班书店')
        self.book_id = self.entry['book_id']
        self.addCleanup(patch.stopall)
        patch.object(runtime, 'ROOT', self.root).start()
        patch.dict(workbench_real.BOOKS, {'夜班书店': self.book_id}).start()

    def test_selection_and_frozen_chapter_use_local_source_without_ssh(self):
        remote = Mock(side_effect=AssertionError('Imported books must use the local index'))
        selection = Selection(request=lambda action, book, chapter=None:
                              flow.index_request(action, book, chapter, runner=remote))
        chapters = selection.load('夜班书店')
        identity, payload = selection._prepare('夜班书店', chapters[0], {'narrator': 'test'})
        self.assertEqual(identity['book_id'], self.book_id)
        self.assertEqual(identity['index_sha256'], self.entry['index_sha256'])
        self.assertEqual(identity['source_sha256'], hashlib.sha256(workflow._bytes(payload)).hexdigest())
        self.assertIn('阿岚', payload['text'])
        self.assertGreaterEqual(sum(row['type'] == 'dialogue' for row in payload['blocks']), 2)
        remote.assert_not_called()

    def test_unlisted_chapter_does_not_fall_back_to_remote(self):
        remote = Mock(side_effect=AssertionError('Invalid local chapter must fail locally'))
        with self.assertRaises((ValueError, KeyError, RuntimeError)):
            flow.index_request('prepare', self.book_id, 'c99999', runner=remote)
        remote.assert_not_called()


if __name__ == '__main__':
    unittest.main()
