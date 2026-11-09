import hashlib
import json
import unittest
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import correction_memory as memory


BOOK='book-'+'2'*20


def sha(text):return hashlib.sha256(text.encode('utf-8')).hexdigest()


class CorrectionMemoryTests(unittest.TestCase):
    def payload(self,chapter,text):
        return {'book_id':BOOK,'chapter_id':chapter,'text':text,'source_sha256':sha(text)}

    def test_same_note_is_idempotent_edit_keeps_history_and_prior_scope(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);payload=self.payload('c001','林舟说：“阿野来了。”')
            first=memory.save_note(root,payload,'这处阿野应映射为既有别名。',
                kind='reader_correction',character_name='阿野')
            repeated=memory.save_note(root,payload,'这处阿野应映射为既有别名。',
                kind='reader_correction',character_name='阿野')
            self.assertEqual(first['version_id'],repeated['version_id'])
            self.assertTrue(repeated['reused']);self.assertFalse(repeated['conflict'])
            old_path=Path(first['pointer']['path']);self.assertTrue(old_path.is_file())
            edited=memory.save_note(root,payload,'校对后：本处称谓未能确认。',
                kind='reader_correction',character_name='阿野')
            self.assertNotEqual(first['version_id'],edited['version_id'])
            self.assertTrue(old_path.is_file())
            prior=memory.read_notes(root,BOOK,'c002')
            self.assertEqual(1,len(prior));self.assertEqual('校对后：本处称谓未能确认。',prior[0]['note'])
            self.assertEqual('阿野',prior[0]['character_name'])
            self.assertEqual(payload['source_sha256'],prior[0]['source_sha256'])
            self.assertEqual('operator_note_not_identity_approval',prior[0]['status'])
            self.assertFalse(prior[0]['is_current_chapter'])
            chapter_current=json.loads((root/'corrections'/BOOK/'c001'/'current.json').read_bytes())
            source_current=json.loads((root/'corrections'/BOOK/'c001'/payload['source_sha256'][:20]/'current.json').read_bytes())
            self.assertEqual(2,len(source_current['events']))
            self.assertEqual(1,len(source_current['slots']))
            self.assertEqual(payload['source_sha256'],chapter_current['current_source_sha256'])

    def test_current_chapter_notes_are_separate_and_chapter_notes_allow_no_person(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);previous=self.payload('c001','第一章原文。');current=self.payload('c002','第二章原文。')
            memory.save_note(root,previous,'摘要更正：保留原章节顺序。',kind='summary_correction')
            memory.save_note(root,current,'阿野的读音按原文明示执行。',kind='pronunciation',character_name='阿野')
            self.assertEqual(['c001'],[x['chapter_id'] for x in memory.read_notes(root,BOOK,'c002')])
            all_notes=memory.read_notes(root,BOOK,'c002',include_current=True)
            self.assertEqual(['c001','c002'],[x['chapter_id'] for x in all_notes])
            self.assertTrue(all_notes[1]['is_current_chapter'])
            self.assertIsNone(all_notes[0]['character_name'])
            self.assertEqual('summary_correction',all_notes[0]['kind'])
            self.assertEqual([],memory.read_notes(root,'book-'+'3'*20,'c003',include_current=True))

    def test_changed_source_is_preserved_but_blocks_unambiguous_read(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);old=self.payload('c001','旧版章节内容。');new=self.payload('c001','校订后的章节内容。')
            first=memory.save_note(root,old,'按旧版本提醒。')
            changed=memory.save_note(root,new,'按新版本修正。')
            self.assertNotEqual(first['version_id'],changed['version_id'])
            self.assertTrue(changed['conflict'])
            pointer=json.loads((root/'corrections'/BOOK/'c001'/'current.json').read_bytes())
            self.assertEqual({old['source_sha256'],new['source_sha256']},set(pointer['source_hashes']))
            with self.assertRaises(memory.CorrectionSourceConflict):
                memory.read_notes(root,BOOK,'c002')

    def test_validates_source_hash_kind_length_and_character_name(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);payload=self.payload('c001','章节文本。')
            with self.assertRaisesRegex(ValueError,'source hash mismatch'):
                memory.save_note(root,{**payload,'source_sha256':'0'*64},'校正')
            for kind in ('model_correction',''):
                with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'kind'):
                    memory.save_note(root,payload,'校正',kind=kind)
            for note in ('', '   ', '字'*2001):
                with self.subTest(note_length=len(note)),self.assertRaisesRegex(ValueError,'1-2000'):
                    memory.save_note(root,payload,note)
            with self.assertRaisesRegex(ValueError,'character name'):
                memory.save_note(root,payload,'校正',character_name=' ')

    def test_concurrent_slots_are_not_lost_and_edited_versions_are_verified(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);payload=self.payload('c001','林舟对阿野说话。')
            requests=[('人物甲应为林舟。','reader_correction','林舟'),
                     ('阿野读音按原文标注。','pronunciation','阿野'),
                     ('本章摘要保留发生顺序。','summary_correction',None)]
            with ThreadPoolExecutor(max_workers=3) as pool:
                results=list(pool.map(lambda row:memory.save_note(root,payload,row[0],kind=row[1],character_name=row[2]),requests))
            notes=memory.read_notes(root,BOOK,'c002')
            self.assertEqual(3,len(notes));self.assertEqual({row[0] for row in requests},{x['note'] for x in notes})
            # Immutable versions remain byte-verified on every read.
            path=Path(results[0]['pointer']['path']);path.write_text('{}',encoding='utf-8')
            with self.assertRaisesRegex(memory.CorrectionMemoryError,'integrity failure'):
                memory.read_notes(root,BOOK,'c002')


if __name__=='__main__':unittest.main()
