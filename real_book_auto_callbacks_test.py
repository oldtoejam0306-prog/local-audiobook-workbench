import hashlib,json,tempfile,unittest,wave
from pathlib import Path
from unittest.mock import patch
import real_book_sequence as seq
import real_book_sequence_callbacks as base
import real_book_auto_callbacks as ac
import real_book_renderer as renderer

def h(x):return hashlib.sha256(x).hexdigest()
def voice(path,key):
 path.mkdir(parents=True,exist_ok=True);(path/'speaker-vector.npy').write_bytes(b'v')
 return {'directory':str(path),'request':{'stage':'local','vector_sha256':h(b'v')},'book_role_key':key}
def profile_doc(book,chapter,name,quote,alias=None):
 sha=h(quote.encode());anchor={'chapter_id':chapter,'quote':quote,'source_range':[0,len(quote)],'source_sha256':sha}
 unknown={'value':'','status':'unknown','evidence':[]}
 claimed={'value':'沉稳','status':'explicit','evidence':[anchor]}
 aliases=[] if not alias else [{'name':alias,'evidence':[anchor]}]
 return {'version':1,'mode':'character_profiles_v1','book_id':book,'chapter_id':chapter,'status':'needs_review',
  'source':{'chapters':[{'chapter_id':chapter,'text_sha256':sha,'codepoint_length':len(quote)}]},
  'characters':[{'name':name,'aliases':aliases,'identity':unknown,'gender':unknown,'age':unknown,
   'personality':[claimed],'behavior':[],'speech_style':[],
   'voice_design':{'description':'沉稳、克制的说话节奏','rationale':'画像明确记载沉稳性格','creative_choices':['语速平稳','重音克制'],'status':'candidate_needs_review'},'uncertainties':[]}],
  'scope_summary':'synthetic test profile','issues':[],'human_reviewed':False}
class Tests(unittest.TestCase):
 def test_missing_named_profiles_are_requested_even_when_chapter_source_is_current(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);book='book-'+'7'*20;cid='c005'
   text='看门大妈守在门口。柳恺问了一句。林舟回答。';source=h(text.encode())
   chapter={'book_id':book,'id':cid,'source_sha256':source}
   registry=root/'character-profiles'/book/'current.json';registry.parent.mkdir(parents=True)
   current=profile_doc(book,cid,'林舟',text)
   registry.write_text(json.dumps(current,ensure_ascii=False),encoding='utf-8')
   stable_id='e'+'1'*24
   prior_auto=base._save(root/'prior-auto.json',{'book_id':book,'entities':[
    {'entity_id':stable_id,'name':'柳恺','labels':['柳恺']}]})
   prior=[{'artifacts':{'memory':{'auto':prior_auto}}}]
   old_record=voice(root/'voices'/book/stable_id,book+'\0'+stable_id)
   old_binding={'console_key':book+'\0'+stable_id,'record':old_record}
   ac.auto.select_voice(root/'voices',book,stable_id,old_binding,basis='existing selected voice')
   calls=[]
   def ensure(payload,*,root,dispatcher=None):
    calls.append((payload,root,dispatcher))
    self.assertEqual(sorted(['看门大妈','柳恺']),payload['profile_required_names'])
    doc=json.loads(registry.read_text(encoding='utf-8'))
    doc['characters'].extend([
     profile_doc(book,cid,'看门大妈',text)['characters'][0],
     profile_doc(book,cid,'柳恺',text)['characters'][0]])
    registry.write_text(json.dumps(doc,ensure_ascii=False),encoding='utf-8')
    return {'status':'needs_review'}
   names=['林舟','看门大妈','柳恺']
   memory=base._save(root/'memory.json',{'characters':[
    {'name':name,'aliases':[],'evidence':text} for name in names]})
   payload={'book_id':book,'chapter_id':cid,'text':text,'blocks':[]}
   input_path=root/'input.json';input_path.write_bytes(base.workflow._bytes(payload));input_sha=h(input_path.read_bytes())
   generated=[]
   def generate(description,directory):
    generated.append((description,directory))
    return voice(directory/'asset','temporary\0generated')
   narrator_record=voice(root/'narrator',book+'\0narrator')
   bridge=ac.AutoBridge(root/'data',None,None,{},voice_root=root/'voices',
    narrator={'console_key':book+'\0narrator','record':narrator_record},
    auto_renderer=lambda *x:None,auto_verify=lambda *x:True,generator=generate,
    profile_ensurer=ensure,profile_dispatcher='stub')
   with patch.object(base.Bridge,'memory',return_value={'artifact':memory}):
    result=bridge.memory({**chapter,'voice_bindings_sha256':'0'*64},prior,
     {'analyze':{'input_path':str(input_path),'input_sha256':input_sha}})
   self.assertEqual(1,len(calls));self.assertEqual(root,calls[0][1]);self.assertEqual('stub',calls[0][2])
   self.assertNotIn('profile_required_names',json.loads(input_path.read_bytes()))
   self.assertEqual(input_sha,h(input_path.read_bytes()))
   self.assertFalse(result['artifact']['profile_blocked'])
   saved=base._read(**result['artifact']['auto'])
   pin=result['artifact']['profile_registry'];version=Path(pin['path'])
   self.assertEqual(registry.parent/'versions',version.parent)
   self.assertEqual(pin['sha256'],h(version.read_bytes()))
   pinned_doc=json.loads(version.read_bytes())
   self.assertTrue({'林舟','柳恺','看门大妈'} <= {item['name'] for item in pinned_doc['characters']})
   changed=json.loads(registry.read_text(encoding='utf-8'));changed['characters']=[]
   registry.write_text(json.dumps(changed,ensure_ascii=False),encoding='utf-8')
   self.assertEqual(pin['sha256'],h(version.read_bytes()))
   self.assertTrue({'林舟','柳恺','看门大妈'} <= {item['name'] for item in json.loads(version.read_bytes())['characters']})
   self.assertEqual(stable_id,next(e['entity_id'] for e in saved['entities'] if e['chapter_name']=='柳恺'))
   self.assertEqual(old_binding,saved['bindings'][stable_id])
   new_id='e'+hashlib.sha256((book+'\0看门大妈').encode()).hexdigest()[:24]
   self.assertIn(new_id,saved['bindings'])
   self.assertNotEqual(saved['bindings'][new_id],saved['bindings']['narrator'])
   generated_ids={path.parents[1].name for _,path in generated}
   expected_lin='e'+hashlib.sha256((book+'\0林舟').encode()).hexdigest()[:24]
   self.assertEqual({new_id,expected_lin},generated_ids)

 def test_missing_profile_triggers_one_rich_discovery_and_registry_is_used(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);book='book-'+'9'*20;cid='c004';text='林舟是律师。';source=h(text.encode())
   chapter={'book_id':book,'id':cid,'source_sha256':source}
   registry=root/'character-profiles'/book/'current.json';calls=[]
   def ensure(payload,*,root,dispatcher=None):
    calls.append((payload['chapter_id'],dispatcher));registry.parent.mkdir(parents=True,exist_ok=True)
    registry.write_text(json.dumps(profile_doc(book,cid,'林舟',text),ensure_ascii=False),encoding='utf-8')
    return {'status':'needs_review'}
   candidate=base._save(root/'memory.json',{'characters':[{'name':'林舟','aliases':[],'evidence':text}]})
   payload={'book_id':book,'chapter_id':cid,'text':text,'blocks':[]};source_path=root/'input.json';source_path.write_bytes(base.workflow._bytes(payload))
   b=ac.AutoBridge(root/'data',None,None,{},voice_root=root/'voices',narrator={},auto_renderer=lambda *x:None,
       auto_verify=lambda *x:True,profile_ensurer=ensure,profile_dispatcher='stub')
   def allocate(_root,_book,profiles,_narrator,_generator):return {'status':'complete','bindings':{},'unresolved':[]}
   with patch.object(base.Bridge,'memory',return_value={'artifact':candidate}),patch.object(ac.auto,'allocate',side_effect=allocate):
    result=b.memory({**chapter,'voice_bindings_sha256':'0'*64},[],
      {'analyze':{'input_path':str(source_path),'input_sha256':h(source_path.read_bytes())}})
   self.assertEqual([('c004','stub')],calls)
   self.assertFalse(result['artifact']['profile_blocked'])
   saved=base._read(**result['artifact']['auto'])
   self.assertEqual(book,saved['book_id'])
   pin=result['artifact']['profile_registry'];version=Path(pin['path'])
   self.assertEqual(registry.parent/'versions',version.parent)
   self.assertEqual(pin['sha256'],h(version.read_bytes()))
   self.assertEqual(source,json.loads(version.read_text(encoding='utf-8'))['source']['chapters'][0]['text_sha256'])

 def test_alias_requires_explicit_identity_and_book_scoping(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); book='book-'+'c'*20; other='book-'+'d'*20
   prior_pointer=base._save(root/'prior.json',{'book_id':book,'entities':[{'entity_id':'existing','name':'甲','labels':['甲']}]})
   prior=[{'artifacts':{'memory':{'auto':prior_pointer}}}]
   def exercise(cid,book_id,name,alias,evidence,history):
    mem=base._save(root/(cid+book_id+'.json'),{'characters':[{'name':name,'aliases':[alias],'evidence':evidence}]})
    b=ac.AutoBridge(root/'data',None,None,{},voice_root=root/'voices',narrator={},auto_renderer=lambda *x:None,auto_verify=lambda *x:True)
    registry=root/'character-profiles'/book_id/'current.json';registry.parent.mkdir(parents=True,exist_ok=True)
    registry.write_text(json.dumps(profile_doc(book_id,cid,name,evidence,alias if alias in evidence else None),ensure_ascii=False),encoding='utf-8')
    payload={'book_id':book_id,'chapter_id':cid,'text':evidence,'blocks':[]};input_path=root/(cid+book_id+'-input.json');input_path.write_bytes(base.workflow._bytes(payload))
    captured=[]
    def allocate(root,book,profiles,narrator,generator):captured.extend(profiles);return {'status':'complete','bindings':{},'unresolved':[]}
    with patch.object(base.Bridge,'memory',return_value={'artifact':mem}),patch.object(ac.auto,'allocate',side_effect=allocate):
     result=b.memory({'book_id':book_id,'id':cid,'source_sha256':h(evidence.encode())},history,
       {'analyze':{'input_path':str(input_path),'input_sha256':h(input_path.read_bytes())}})
    return base._read(**result['artifact']['auto']),result,b,{'book_id':book_id,'id':cid,'source_sha256':h(evidence.encode())},history
   coincident,_,_,_,_=exercise('c001',book,'乙','甲','乙遇见甲。',prior)
   self.assertEqual(coincident['entities'],[])
   self.assertEqual(coincident['unresolved'][0]['reason'],'unproven_existing_alias')
   explicit,_,_,_,_=exercise('c002',book,'乙','甲','乙又名甲。',prior)
   self.assertEqual(explicit['entities'][0]['entity_id'],'existing')
   separate,_,_,_,_=exercise('c003',other,'乙','甲','乙又名甲。',[])
   self.assertNotEqual(separate['entities'][0]['entity_id'],'existing')
   collision=base._save(root/'collision.json',{'book_id':book,'entities':[{'entity_id':'e1','name':'甲','labels':['甲']},{'entity_id':'e2','name':'乙','labels':['乙']}]})
   blocked,result,b,chapter,history=exercise('c004',book,'乙','甲','乙又名甲。',[{'artifacts':{'memory':{'auto':collision}}}])
   self.assertEqual(blocked['entities'],[])
   self.assertEqual(blocked['unresolved'][0]['reason'],'multiple_existing_entities')
   self.assertTrue(blocked['blocked']);self.assertTrue(result['artifact']['profile_blocked'])
   with self.assertRaisesRegex(ValueError,'未解决'):
    b.stage(chapter,history,{'memory':{'auto':result['artifact']['auto']}})
 def test_historical_allocator_alias_conflict_blocks_stage_without_narrator_fallback(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);book='book-'+'e'*20;cid='c003';text='甲又名阿甲。';source=h(text.encode())
   narrator_record=voice(root/'narrator',book+'\0narrator')
   historical_record=voice(root/'voices'/book/'e-existing',book+'\0e-existing')
   old_voice={'book_id':book,'entity_id':'e-existing','name':'乙','labels':['阿甲'],
       'binding':{'console_key':book+'\0e-existing','record':historical_record},
       'identity_status':'unreviewed_model_candidate'}
   (root/'voices'/book/'e-existing'/'voice.json').write_bytes(base.workflow._bytes(old_voice))
   registry=root/'character-profiles'/book/'current.json';registry.parent.mkdir(parents=True)
   registry.write_text(json.dumps(profile_doc(book,cid,'甲',text,'阿甲'),ensure_ascii=False),encoding='utf-8')
   memory=base._save(root/'memory.json',{'book_id':book,'chapter_id':cid,
       'characters':[{'name':'甲','aliases':['阿甲'],'evidence':text}],'human_reviewed':False})
   payload={'book_id':book,'chapter_id':cid,'text':text,'blocks':[]};input_path=root/'input.json';input_path.write_bytes(base.workflow._bytes(payload))
   voices={'narrator':{'id':'narrator','voice':'preset:test'},'characters':{}}
   bridge=ac.AutoBridge(root/'data',None,None,voices,voice_root=root/'voices',
       narrator={'console_key':book+'\0narrator','record':narrator_record},
       auto_renderer=lambda *args:None,auto_verify=lambda *args:True,
       generator=lambda *args:self.fail('alias conflict must not generate another voice'))
   chapter={'book_id':book,'id':cid,'source_sha256':source,'voice_bindings_sha256':seq.fingerprint(voices)}
   artifacts={'analyze':{'input_path':str(input_path),'input_sha256':h(input_path.read_bytes())}}
   with patch.object(base.Bridge,'memory',return_value={'artifact':memory}),\
        patch.object(base.workflow,'stage_receipt') as stage:
    result=bridge.memory(chapter,[],artifacts)
    auto_doc=base._read(**result['artifact']['auto'])
    profile_pin=auto_doc['profile_registry'];profile_version=Path(profile_pin['path'])
    self.assertEqual(registry.parent/'versions',profile_version.parent)
    self.assertEqual(profile_pin['sha256'],h(profile_version.read_bytes()))
    self.assertTrue(auto_doc['blocked']);self.assertTrue(result['artifact']['profile_blocked'])
    self.assertEqual('alias_or_name_conflict',auto_doc['unresolved'][0]['reason'])
    self.assertEqual('甲',[entity['chapter_name'] for entity in auto_doc['entities']][0])
    with self.assertRaisesRegex(ValueError,'未解决'):
     bridge.stage(chapter,[],{'memory':{'auto':result['artifact']['auto']}})
    stage.assert_not_called()
 def test_every_allocator_unresolved_reason_blocks_staging_and_preserves_evidence(self):
  for reason in ('invalid_design_spec','saved_identity_differs','generation_unknown_no_retry'):
   with self.subTest(reason=reason),tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);book='book-'+'f'*20;cid='c006';text='林舟是律师。';source=h(text.encode())
    registry=root/'character-profiles'/book/'current.json';registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps(profile_doc(book,cid,'林舟',text),ensure_ascii=False),encoding='utf-8')
    memory=base._save(root/'memory.json',{'book_id':book,'chapter_id':cid,
        'characters':[{'name':'林舟','aliases':[],'evidence':text}],'human_reviewed':False})
    payload={'book_id':book,'chapter_id':cid,'text':text,'blocks':[]};input_path=root/'input.json';input_path.write_bytes(base.workflow._bytes(payload))
    voices={'narrator':{'id':'narrator','voice':'preset:test'},'characters':{}}
    narrator={'console_key':book+'\0narrator','record':{}}
    bridge=ac.AutoBridge(root/'data',None,None,voices,voice_root=root/'voices',narrator=narrator,
        auto_renderer=lambda *args:None,auto_verify=lambda *args:True)
    chapter={'book_id':book,'id':cid,'source_sha256':source,'voice_bindings_sha256':seq.fingerprint(voices)}
    allocation={'status':'needs_review','bindings':{'narrator':narrator},
        'unresolved':[{'entity_id':'e-ambiguous','reason':reason,'source_conflict':'saved evidence'}]}
    artifacts={'analyze':{'input_path':str(input_path),'input_sha256':h(input_path.read_bytes())}}
    with patch.object(base.Bridge,'memory',return_value={'artifact':memory}),\
         patch.object(ac.auto,'allocate',return_value=allocation) as allocate,\
         patch.object(base.workflow,'stage_receipt') as stage:
     result=bridge.memory(chapter,[],artifacts)
     saved=base._read(**result['artifact']['auto'])
     self.assertTrue(saved['blocked']);self.assertTrue(result['artifact']['profile_blocked'])
     self.assertEqual('saved evidence',saved['unresolved'][0]['source_conflict'])
     self.assertEqual(reason,saved['blocking_conflicts'][0]['reason'])
     with self.assertRaisesRegex(ValueError,'未解决'):
      bridge.stage(chapter,[],{'memory':{'auto':result['artifact']['auto']}})
     allocate.assert_called_once();stage.assert_not_called()
 def test_auto_bridge_to_real_renderer_with_synthetic_audio(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);book='book-'+'b'*20;chapter={'book_id':book,'id':'c001','source_sha256':h('甲来了'.encode())}
   n=voice(root/'n','temporary\0n');a=voice(root/'a','temporary\0a')
   bindings={'narrator':{'console_key':'temporary\0n','record':n},'e123':{'console_key':'temporary\0a','record':a}}
   auto=base._save(root/'auto.json',{'bindings':bindings})
   stage=base._save(root/'stage.json',{'chapters':[{'id':'c001','segments':[{'speaker_id':'narrator','text':'甲'},{'speaker_id':'e123','text':'来了'}]}],'audit':{'source_text_sha256':chapter['source_sha256']}})
   stage['auto_sha256']=auto['sha256']
   def fake_segment(record,text,folder):
    p=folder/'rendered.wav'
    with wave.open(str(p),'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\0\0'*10)
    return p
   def render(path,role_bindings,ch):return renderer.render(path,ch['book_id'],ch['id'],ch['source_sha256'],role_bindings,root/'output',render_segment=fake_segment)
   def verify(receipt,path,role_bindings,ch):
    document,assignments=renderer.prepare(path,ch['book_id'],ch['id'],ch['source_sha256'],role_bindings)
    return renderer.verify(Path(receipt['manifest_path']).parent,document,assignments,ch['source_sha256'])==receipt
   b=ac.AutoBridge(root/'data',None,None,{},voice_root=root/'voices',narrator=bindings['narrator'],auto_renderer=render,auto_verify=verify)
   outcome=b.render(chapter,[],{'memory':{'auto':auto},'stage':stage})
   self.assertEqual(outcome['status'],'ok')
   self.assertTrue((root/'data'/book/'c001'/'render-receipt.json').exists())
   (root/'a'/'speaker-vector.npy').write_bytes(b'changed')
   with self.assertRaises(ValueError):b.render(chapter,[],{'memory':{'auto':auto},'stage':stage})
 def test_multichapter_reuse_and_no_redispatch(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); book='book-'+'a'*20;n=voice(root/'n','n\0n');narr={'console_key':'n\0n','record':n}
   registry=root/'character-profiles'/book/'current.json';registry.parent.mkdir(parents=True)
   one=profile_doc(book,'c001','甲','甲又名阿甲','阿甲')['characters'][0]
   two=profile_doc(book,'c002','乙','阿甲遇见乙')['characters'][0]
   registry.write_text(json.dumps({'version':1,'mode':'character_profiles_v1','book_id':book,'chapter_id':'c002','status':'needs_review',
    'source':{'chapters':[{'chapter_id':'c001','text_sha256':h('甲又名阿甲'.encode()),'codepoint_length':5},
                          {'chapter_id':'c002','text_sha256':h('阿甲遇见乙'.encode()),'codepoint_length':5}]},
    'characters':[one,two],'scope_summary':'test','issues':[],'human_reviewed':False},ensure_ascii=False),encoding='utf-8')
   voices={'narrator':{'id':'narrator','voice':'preset:test'},'characters':{}}
   specs=[{'id':'c001','source_sha256':h('甲又名阿甲'.encode())},{'id':'c002','source_sha256':h('阿甲遇见乙'.encode())}]
   plan=seq.create_plan(root/'seq',book,'f'*64,specs,voices);dispatches=[];gens=[];renders=[]
   def restore(i,c,s,e):return {'book_id':book,'chapter_id':c,'text':'甲又名阿甲' if c=='c001' else '阿甲遇见乙','blocks':[]}
   def dispatch(payload,tasks,jobs):
    dispatches.append(payload);p=Path(tasks)/'fake';p.mkdir(parents=True);r=p/'receipt';r.mkdir();src=p/'input.json';src.write_bytes(base.workflow._bytes(payload))
    chars=([{'name':'甲','aliases':['阿甲'],'evidence':'甲又名阿甲'}] if payload['chapter_id']=='c001' else [{'name':'阿甲','aliases':[],'evidence':'阿甲遇见乙'},{'name':'乙','aliases':[],'evidence':'阿甲遇见乙'}])
    body={'usage':{'prompt_tokens':1,'completion_tokens':1},'choices':[{'message':{'content':json.dumps({'characters':chars},ensure_ascii=False)}}]};(r/'response.body').write_bytes(base.workflow._bytes(body))
    (r/'intent.json').write_bytes(base.workflow._bytes({'book_id':book,'chapter_id':payload['chapter_id'],'input_sha256':h(src.read_bytes())}))
    (r/'response.meta.json').write_bytes(base.workflow._bytes({'http_status':200,'body_sha256':h((r/'response.body').read_bytes())}))
    return {'input_path':str(src),'receipt_dir':str(r)}
   def gen(description,directory):gens.append(description);return voice(directory/'asset','unused')
   def build(i,r,v):return {'provenance':{'response_sha256':h((Path(r)/'response.body').read_bytes())}}
   def stage(i,r,v,roles,out,narrator_id):out.mkdir();p=out/'render.json';p.write_text('{}');return {'path':str(p)}
   def render(path,bindings,chapter):renders.append((chapter['id'],bindings));return {'id':chapter['id']}
   b=ac.AutoBridge(root/'data','index',{},voices,voice_root=root/'voices',narrator=narr,auto_renderer=render,auto_verify=lambda *x:True,generator=gen,dispatch=dispatch,restore=restore)
   with patch.object(base.analysis,'build',side_effect=build),patch.object(base.workflow,'stage_receipt',side_effect=stage):
    self.assertEqual(seq.run(root/'seq',plan,b.callbacks())['status'],'complete')
    self.assertEqual(seq.run(root/'seq',plan,b.callbacks())['status'],'complete')
   self.assertEqual(len(dispatches),2);self.assertEqual(len(gens),2);self.assertEqual(len(renders),2)
   ids1=set(renders[0][1]) - {'narrator'};ids2=set(renders[1][1]) - {'narrator'}
   self.assertEqual(len(ids1 & ids2),1);self.assertEqual(len(ids2),2)
if __name__=='__main__':unittest.main()


