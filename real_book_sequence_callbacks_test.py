import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import real_book_sequence as seq
import real_book_sequence_callbacks as bridge


def digest(s): return hashlib.sha256(s.encode()).hexdigest()


class BridgeTests(unittest.TestCase):
    def test_prior_source_hash_must_match_plan_even_when_artifact_hash_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); book='book-'+'f'*20
            voices={'narrator':{'id':'narrator','voice':'preset:test'},'characters':{}}
            record={'book_id':book,'chapter_id':'c001','human_reviewed':False,
                'response_sha256':digest('response'),'source_sha256':digest('wrong source'),
                'characters':[],'chapter_summary':'prior'}
            path=root/'memory.json';path.write_bytes(bridge.workflow._bytes(record))
            chapter={'book_id':book,'id':'c002','source_sha256':digest('current'),
                'voice_bindings_sha256':seq.fingerprint(voices),'index_sha256':'e'*64}
            prior=[{'chapter':{'id':'c001','source_sha256':digest('expected source')},
                'artifacts':{'memory':{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}}]
            adapter=bridge.Bridge(root/'data','index',{},voices,lambda *x:None,lambda *x:False,
                dispatch=lambda *x: self.fail('must reject prior source mismatch'),
                restore=lambda *x:{'book_id':book,'chapter_id':'c002','text':'current'})
            with self.assertRaisesRegex(ValueError,'source hash mismatch'):
                adapter.analyze(chapter,prior,{})

    def test_two_chapters_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); book = 'book-' + 'a'*20
            voices = {'narrator': {'id':'narrator', 'voice':'preset:test'}, 'characters': {}}
            chapters = [{'id':'c001','source_sha256':digest('甲说话')}, {'id':'c002','source_sha256':digest('乙说话')}]
            plan = seq.create_plan(root/'sequence', book, 'f'*64, chapters, voices)
            requests = []
            def restore(index, cid, snapshot, expected):
                return {'book_id':book, 'chapter_id':cid, 'text': '甲说话' if cid == 'c001' else '乙说话', 'blocks':[]}
            def dispatch(payload, tasks, jobs):
                requests.append(payload)
                task = Path(tasks)/'fake'; task.mkdir(parents=True); receipt = task/'receipt'; receipt.mkdir()
                source = task/'input.json'; source.write_bytes(bridge.workflow._bytes(payload))
                character = {'name':'甲','aliases':['同名'],'evidence':'甲说话'} if payload['chapter_id']=='c001' else {'name':'乙','aliases':['同名'],'evidence':'乙说话'}
                result = {'characters':[character, {'name':'无证','aliases':[], 'evidence':'不存在'}], 'chapter_summary':None}
                body = {'usage': {'prompt_tokens':10,'completion_tokens':5}, 'choices':[{'message':{'content':json.dumps(result, ensure_ascii=False)}}]}
                (receipt/'response.body').write_bytes(bridge.workflow._bytes(body))
                (receipt/'intent.json').write_bytes(bridge.workflow._bytes({'book_id': book, 'chapter_id':payload['chapter_id'], 'input_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}))
                (receipt/'response.meta.json').write_bytes(bridge.workflow._bytes({'http_status':200, 'body_sha256':hashlib.sha256((receipt/'response.body').read_bytes()).hexdigest()}))
                return {'input_path':str(source),'receipt_dir':str(receipt)}
            def build(source, receipt, voices):
                return {'provenance':{'response_sha256':hashlib.sha256((Path(receipt)/'response.body').read_bytes()).hexdigest()}}
            def stage(source, receipt, voices, roles, out, narrator_id):
                out.mkdir(); p=out/'render.json'; p.write_text('{}'); return {'path':str(p)}
            def render(path, voices): return {'file':'fixture'}
            adapter = bridge.Bridge(root/'data', 'index', {}, voices, render, lambda *x: True, dispatch=dispatch, restore=restore)
            with patch.object(bridge.analysis, 'build', side_effect=build), patch.object(bridge.workflow, 'stage_receipt', side_effect=stage):
                self.assertEqual(seq.run(root/'sequence',plan,adapter.callbacks())['status'],'complete')
                self.assertEqual(seq.run(root/'sequence',plan,adapter.callbacks())['status'],'complete')
            self.assertEqual(len(requests),2)
            self.assertEqual(requests[1]['prior_context']['records'][0]['chapter_id'],'c001')
            self.assertEqual(requests[1]['prior_context']['records'][0]['unresolved'][0]['count'],1)
            self.assertEqual(requests[1]['prior_context']['records'][0]['source_sha256'],digest('甲说话'))
            self.assertEqual(requests[1]['prior_context']['compaction']['budget_bytes'],12000)
            self.assertTrue((root/'data'/book/'c002'/'billing.json').exists())

    def test_unknown_never_redispatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            book='book-'+'b'*20; voices={'narrator':{'id':'n','voice':'preset:test'},'characters':{}}
            plan=seq.create_plan(Path(tmp)/'seq',book,'e'*64,[{'id':'c001','source_sha256':digest('a')}],voices)
            calls=[]
            def fail(*args): calls.append(1); raise RuntimeError('unknown')
            b=bridge.Bridge(Path(tmp)/'data','i',{},voices,lambda *x:None,lambda *x:False,dispatch=fail,restore=lambda *x:{'book_id':book,'chapter_id':'c001','text':'a'})
            self.assertEqual(seq.run(Path(tmp)/'seq',plan,b.callbacks())['status'],'unknown')
            self.assertEqual(seq.run(Path(tmp)/'seq',plan,b.callbacks())['status'],'unknown')
            self.assertEqual(len(calls),1)

if __name__=='__main__': unittest.main()
