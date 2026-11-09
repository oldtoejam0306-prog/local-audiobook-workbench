import copy
import hashlib
import unittest

import chapter_attribution_input as subject
import attribution_batch_validation as batch


class ChapterAttributionInputTests(unittest.TestCase):
    def setUp(self):
        text = '第一章\n“你好。”\n夜色。'
        sha = hashlib.sha256(text.encode()).hexdigest()
        self.chapter = {'book_id':'book', 'chapter_id':'c1', 'normalized_text':text,
            'local_text_sha256':sha, 'index_sha256':'a'*64,
            'script_rows':[{'type':'huaben','text_sha256':sha},
                {'type':'chapter','id':'c1','text':'第一章','src':[0,3]},
                {'type':'dialogue','id':'q1','text':'“你好。”','src':[4,9],'speaker':'unknown'},
                {'type':'narration','id':'n1','text':'夜色。','src':[10,13]}],
            'cast':{'narrator':{'voice':'preset:Vivian'},'characters':{}}}

    def test_prepare_and_stage_unknown_are_reversible(self):
        prepared = subject.prepare(self.chapter)
        out = subject.stage(self.chapter, prepared,
            {'assignments':[{'id':'q1','speaker':'unknown'}], 'unknown_ids':['q1'], 'conflicts':[]})
        self.assertFalse(prepared['production_dispatch_enabled'])
        self.assertEqual(out['audit']['fallback_count'], 1)
        self.assertEqual(subject.staging.restore_original(out)['script_rows'], self.chapter['script_rows'])

    def test_source_drift_and_limits_are_rejected(self):
        for mutation in ('text','range','gap','two_chapters','limit'):
            with self.subTest(mutation=mutation):
                chapter=copy.deepcopy(self.chapter)
                if mutation=='text': chapter['normalized_text']+='x'
                if mutation=='range': chapter['script_rows'][2]['src']=[4,8]
                if mutation=='gap': chapter['script_rows'].pop(2)
                if mutation=='two_chapters': chapter['script_rows'][3]['type']='chapter'
                with self.assertRaises(ValueError):
                    subject.prepare(chapter, max_text_chars=3 if mutation=='limit' else 100000)

    def test_prepared_payload_is_bound(self):
        prepared=subject.prepare(self.chapter)
        prepared['payload']['text']='different'
        with self.assertRaises(ValueError):
            subject.stage(self.chapter,prepared,{'assignments':[],'unknown_ids':[],'conflicts':[]})

    def test_unregistered_model_candidate_falls_back_in_actual_staging(self):
        prepared=subject.prepare(self.chapter)
        proposals=[{'id':'q1','speaker':'候选人物','identity_status':'model_inference'}]
        value=batch.for_staging(proposals,{'q1'},set())
        out=subject.stage(self.chapter,prepared,value)
        self.assertEqual(out['audit']['fallback_count'],1)
        self.assertEqual(out['audit']['changes'][0]['model_speaker'],'候选人物')
        self.assertFalse(out['audit']['human_reviewed'])


if __name__=='__main__': unittest.main()
