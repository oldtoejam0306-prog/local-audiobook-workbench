import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import workbench_real_v2_experiment as ex
import real_pipeline_workflow as wf
import real_book_sequence_callbacks as bridge
SYNTHETIC_BOOK_ID='book-0123456789abcdefabcd'
ex.index_bridge.books=lambda:{'Synthetic Book':SYNTHETIC_BOOK_ID}

class ExperimentTests(unittest.TestCase):
 def test_two_chapters_prior_and_no_repeat(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);legacy=root/'legacy';(legacy/'jobs').mkdir(parents=True);book=SYNTHETIC_BOOK_ID;digest='a'*64;calls=[]
   voices={'narrator':{'id':'n','voice':'preset:test'},'characters':{}}
   def index(action,b,c=None):
    d={'book_id':b,'index_sha256':digest,'human_reviewed':False}
    if action=='list':return dict(d,chapters=['c001','c002'])
    p={'book_id':b,'chapter_id':c,'text':'合成'+c,'blocks':[]};return dict(d,payload=p,payload_sha256=hashlib.sha256(wf._bytes(p)).hexdigest())
   def dispatch(payload,tasks,jobs):
    calls.append(payload);task=Path(tasks)/'fake';task.mkdir(parents=True);receipt=task/'receipt';receipt.mkdir();source=task/'input.json';source.write_bytes(wf._bytes(payload));body=wf._bytes({'usage':{'prompt_tokens':1,'completion_tokens':1},'choices':[{'message':{'content':json.dumps({'characters':[],'chapter_summary':None})}}]})
    (receipt/'response.body').write_bytes(body);(receipt/'intent.json').write_bytes(wf._bytes({'book_id':book,'chapter_id':payload['chapter_id'],'input_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}));(receipt/'response.meta.json').write_bytes(wf._bytes({'http_status':200,'body_sha256':hashlib.sha256(body).hexdigest()}));return {'input_path':str(source),'receipt_dir':str(receipt)}
   def stage(source,receipt,voices,roles,out,narrator_id):
    x=json.loads(Path(source).read_bytes());out.mkdir();p=out/'render.json';p.write_text(json.dumps({'chapters':[{'id':x['chapter_id']}]}));return {'path':str(p)}
   def render(path,b,c,h,roles,out,**kw):return {'manifest_path':str(out/'manifest.json'),'mock':True}
   with patch.object(bridge.analysis,'build',side_effect=lambda source,receipt,voices:{'provenance':{'response_sha256':hashlib.sha256((Path(receipt)/'response.body').read_bytes()).hexdigest()}}),patch.object(bridge.workflow,'stage_receipt',side_effect=stage),patch.object(ex.renderer,'render',side_effect=render),patch.object(ex.renderer,'prepare',return_value=({},{})),patch.object(ex.renderer,'verify',side_effect=lambda root,*args:{'manifest_path':str(root/'manifest.json'),'mock':True}):
    kw=dict(single_user_ack=True,index_request=index,dispatch=dispatch)
    result=ex.run_experiment(root/'new',legacy,book,['c001','c002'],voices,{},lambda *x:True,**kw)
    self.assertEqual(result['status'],'complete');self.assertEqual(len(calls),2);self.assertEqual(calls[1]['prior_context']['records'][0]['chapter_id'],'c001')
    self.assertEqual(ex.run_experiment(root/'new',legacy,book,['c001','c002'],voices,{},lambda *x:True,**kw)['status'],'complete');self.assertEqual(len(calls),2)
 def test_requires_coordination_and_records(self):
  with self.assertRaises(ValueError):ex.run_experiment('x','y',SYNTHETIC_BOOK_ID,['c001'],{}, {},lambda *x:True)
  with self.assertRaises(ValueError):ex.run_experiment('x','y',SYNTHETIC_BOOK_ID,['c001'],{}, {},lambda *x:False,single_user_ack=True)

 def test_auto_two_chapters_real_renderer_mock_audio(self):
  import wave
  import real_book_sequence as seq
  from real_book_auto_callbacks_test import profile_doc
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);legacy=root/'legacy';(legacy/'jobs').mkdir(parents=True);book=SYNTHETIC_BOOK_ID;digest='a'*64;gens=[];calls=[]
   n=root/'narrator';n.mkdir();(n/'speaker-vector.npy').write_bytes(b'n');key='synthetic\0n';record={'directory':str(n),'request':{'stage':'local','vector_sha256':hashlib.sha256(b'n').hexdigest()},'book_role_key':key};narr={'console_key':key,'record':record}
   voices={'narrator':{'id':'narrator','voice':'preset:test'},'characters':{},'auto_narrator_sha256':seq.fingerprint(narr)}
   registry=root/'new'/'character-profiles'/book/'current.json';registry.parent.mkdir(parents=True)
   registry.write_text(json.dumps(profile_doc(book,'c001','甲','甲来了'),ensure_ascii=False),encoding='utf-8')
   def index(action,b,c=None):
    d={'book_id':b,'index_sha256':digest,'human_reviewed':False}
    if action=='list':return dict(d,chapters=['c001','c002'])
    p={'book_id':b,'chapter_id':c,'text':'甲来了','blocks':[]};return dict(d,payload=p,payload_sha256=hashlib.sha256(wf._bytes(p)).hexdigest())
   def dispatch(payload,tasks,jobs):
    calls.append(payload);task=Path(tasks)/'fake';task.mkdir(parents=True);r=task/'receipt';r.mkdir();src=task/'input.json';src.write_bytes(wf._bytes(payload));body=wf._bytes({'usage':{'prompt_tokens':1,'completion_tokens':1},'choices':[{'message':{'content':json.dumps({'characters':[{'name':'甲','aliases':[],'evidence':'甲来了'}]},ensure_ascii=False)}}]})
    (r/'response.body').write_bytes(body);(r/'intent.json').write_bytes(wf._bytes({'book_id':book,'chapter_id':payload['chapter_id'],'input_sha256':hashlib.sha256(src.read_bytes()).hexdigest()}));(r/'response.meta.json').write_bytes(wf._bytes({'http_status':200,'body_sha256':hashlib.sha256(body).hexdigest()}));return {'input_path':str(src),'receipt_dir':str(r)}
   def gen(desc,directory):
    gens.append(desc);p=directory/'voice';p.mkdir();(p/'speaker-vector.npy').write_bytes(b'v');return {'directory':str(p),'request':{'stage':'local','vector_sha256':hashlib.sha256(b'v').hexdigest()}}
   def stage(source,receipt,voices,roles,out,narrator_id):
    x=json.loads(Path(source).read_bytes());out.mkdir();p=out/'render.json';p.write_bytes(wf._bytes({'chapters':[{'id':x['chapter_id'],'segments':[{'speaker_id':roles['甲'],'text':x['text']}]}],'audit':{'source_text_sha256':hashlib.sha256(x['text'].encode()).hexdigest()}}));return {'path':str(p)}
   def segment(record,text,folder):
    p=folder/'rendered.wav'
    with wave.open(str(p),'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\0\0'*10)
    return p
   with patch.object(bridge.analysis,'build',side_effect=lambda source,receipt,voices:{'provenance':{'response_sha256':hashlib.sha256((Path(receipt)/'response.body').read_bytes()).hexdigest()}}),patch.object(bridge.workflow,'stage_receipt',side_effect=stage):
    result=ex.run_experiment(root/'new',legacy,book,['c001','c002'],voices,{'narrator':narr},lambda *x:True,single_user_ack=True,index_request=index,dispatch=dispatch,render_segment=segment,auto_narrator=narr,generator=gen)
    self.assertEqual(result['status'],'complete');self.assertEqual(len(gens),1);self.assertEqual(len(calls),2)

