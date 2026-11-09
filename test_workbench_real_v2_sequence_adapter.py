import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import workbench_real_v2_sequence_adapter as a
import workbench_real_v2_client as client
import real_book_sequence_callbacks as bridge
import real_pipeline_workflow as workflow

class AdapterTests(unittest.TestCase):
 def test_mock_cross_book_chapters_and_no_repeat(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);legacy=root/'legacy';(legacy/'jobs').mkdir(parents=True);calls=[]
   voices={'narrator':{'id':'n','voice':'preset:test'},'characters':{}}
   def dispatch(payload,tasks,jobs):
    calls.append((payload['book_id'],payload['chapter_id']));task=Path(tasks)/'fake';task.mkdir(parents=True);receipt=task/'receipt';receipt.mkdir();source=task/'input.json';source.write_bytes(workflow._bytes(payload))
    body={'usage':{'prompt_tokens':1,'completion_tokens':1},'choices':[{'message':{'content':json.dumps({'characters':[],'chapter_summary':None})}}]};raw=workflow._bytes(body);(receipt/'response.body').write_bytes(raw)
    (receipt/'intent.json').write_bytes(workflow._bytes({'book_id':payload['book_id'],'chapter_id':payload['chapter_id'],'input_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}));(receipt/'response.meta.json').write_bytes(workflow._bytes({'http_status':200,'body_sha256':hashlib.sha256(raw).hexdigest()}))
    return {'input_path':str(source),'receipt_dir':str(receipt)}
   def stage(source,receipt,voices,roles,out,narrator_id):
    out.mkdir();p=out/'render.json';p.write_text('{}');return {'path':str(p)}
   with patch.object(bridge.analysis,'build',side_effect=lambda source,receipt,voices:{'provenance':{'response_sha256':hashlib.sha256((Path(receipt)/'response.body').read_bytes()).hexdigest()}}),patch.object(bridge.workflow,'stage_receipt',side_effect=stage):
    for book,cid in [('book-'+'a'*20,'c001'),('book-'+'a'*20,'c002'),('book-'+'b'*20,'c001')]:
     payload={'book_id':book,'chapter_id':cid,'text':'自写','blocks':[]};identity=client.definition(book,cid,workflow._bytes(payload),'f'*64,voices)
     kw=dict(legacy_root=legacy,restore=lambda *x,p=payload:p,dispatch=dispatch,renderer=lambda *x:{'mock':True},verify_renderer=lambda *x:True,verify_voice_assets=lambda v:True)
     self.assertEqual(a.run_one(root/'run',identity,payload,voices,**kw)['status'],'complete')
     self.assertEqual(a.run_one(root/'run',identity,payload,voices,**kw)['status'],'complete')
    self.assertEqual(len(calls),3)
 def test_rejects_unverified_assets_and_modified_source(self):
  with tempfile.TemporaryDirectory() as tmp:
   p={'book_id':'book-'+'a'*20,'chapter_id':'c001','text':'synthetic'};v={'narrator':{'id':'n','voice':'x'},'characters':{}};i=client.definition(p['book_id'],p['chapter_id'],workflow._bytes(p),'f'*64,v)
   kw=dict(legacy_root=Path(tmp)/'legacy',restore=lambda *x:p,dispatch=lambda *x:None,renderer=lambda *x:None,verify_renderer=lambda *x:True,verify_voice_assets=lambda *x:False)
   with self.assertRaises(ValueError):a.run_one(Path(tmp)/'run',i,p,v,**kw)
   with self.assertRaises(ValueError):a.run_one(Path(tmp)/'run',i,dict(p,text='changed'),v,**kw)
if __name__=='__main__':unittest.main()
