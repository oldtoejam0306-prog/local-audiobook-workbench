import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import context_compaction as compaction
import real_pipeline_workflow as workflow


def sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def memory_record(book, chapter, *, name='阿青', alias='青哥', summary='本章摘要'):
    return {'book_id': book, 'chapter_id': chapter, 'human_reviewed': False,
        'response_sha256': sha('response-' + chapter), 'source_sha256': sha('source-' + chapter),
        'characters': [{'name': name, 'aliases': [alias], 'identity_status': 'unreviewed_model_candidate'}],
        'chapter_summary': summary, 'unresolved': []}


class ContextCompactionTests(unittest.TestCase):
    def test_300_chapters_compact_under_byte_budget_with_recent_first(self):
        book = 'book-' + 'a' * 20
        records = []
        for number in range(1, 301):
            record = memory_record(book, f'c{number:03d}', name=f'人物{number:03d}',
                                   alias=f'别称{number:03d}', summary=(f'第{number:03d}章' + '情节' * 32))
            record['record_sha256'] = sha('record-' + str(number))
            records.append(record)
        first = compaction.compact_prior_context(book, 'c301', records)
        second = compaction.compact_prior_context(book, 'c301', records)
        raw = workflow._bytes(first)
        self.assertLessEqual(len(raw), 12000)
        self.assertEqual(raw, workflow._bytes(second))
        self.assertEqual(300, first['compaction']['input_record_count'])
        self.assertGreater(first['compaction']['omitted_counts']['summaries'], 0)
        self.assertEqual('c300', first['records'][0]['chapter_id'])
        self.assertEqual('c299', first['records'][1]['chapter_id'])
        self.assertEqual('人物300', first['character_index'][0]['name'])
        self.assertGreater(len(first['alias_index']), 0)
        self.assertTrue(any(item['chapter_id'] != 'c300' for item in first['character_index']))
        self.assertFalse(first['human_reviewed'])
        self.assertEqual(12000, first['compaction']['budget_bytes'])

    def test_conflicting_aliases_and_operator_correction_survive_without_merge(self):
        book = 'book-' + 'b' * 20
        rows = [memory_record(book, 'c001', name='甲', alias='同名'),
                memory_record(book, 'c002', name='乙', alias='同名')]
        for index, row in enumerate(rows):
            row['record_sha256'] = sha('raw-' + str(index))
        result = compaction.compact_prior_context(book, 'c003', rows,
            corrections=[{'chapter_id': 'c001', 'note': '待核对该别名对应身份。',
                          'source_sha256': rows[0]['source_sha256']}])
        self.assertEqual(['乙', '甲'], [x['name'] for x in result['character_index']])
        self.assertEqual('同名', result['alias_conflicts'][0]['alias'])
        self.assertEqual({'甲', '乙'}, {x['name'] for x in result['alias_conflicts'][0]['candidates']})
        self.assertIn('待核对', result['corrections'][0]['note'])
        self.assertEqual('operator_note_not_identity_approval', result['corrections'][0]['status'])
        self.assertFalse(result['human_reviewed'])

    def test_freeze_validates_all_records_then_compacts_and_rejects_cross_book(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); book = 'book-' + 'c' * 20
            for number in range(1, 301):
                row = memory_record(book, f'c{number:03d}', name=f'人物{number:03d}',
                                    summary='摘要' * 80)
                (root / f'{book}-c{number:03d}.json').write_bytes(workflow._bytes(row))
            frozen = workflow.freeze_prior_context(book, 'c301', root,
                corrections=[{'chapter_id': 'c001', 'note': '首章仍需核对。'}])
            self.assertLessEqual(len(workflow._bytes(frozen)), 12000)
            self.assertEqual('c300', frozen['records'][0]['chapter_id'])
            self.assertIn('c001', {r['chapter_id'] for r in frozen['corrections']})
            other = 'book-' + 'd' * 20
            (root / f'{book}-c301.json').write_bytes(workflow._bytes(memory_record(other, 'c301')))
            with self.assertRaisesRegex(ValueError, 'invalid memory identity'):
                workflow.freeze_prior_context(book, 'c302', root)

    def test_invalid_record_hash_is_rejected(self):
        book = 'book-' + 'e' * 20
        row = memory_record(book, 'c001'); row['record_sha256'] = 'bad'
        with self.assertRaisesRegex(ValueError, 'record_sha256'):
            compaction.compact_prior_context(book, 'c002', [row])


if __name__ == '__main__':
    unittest.main()
