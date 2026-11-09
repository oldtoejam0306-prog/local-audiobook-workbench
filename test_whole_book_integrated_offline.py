"""Synthetic in-process integration test. No APIs, models, or real books."""
import copy,hashlib,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import character_fusion as fusion
import auto_voice_planner as voices
import staging_render_cast as staging
import attribution_staging_adapter as attribution_adapter
import whole_book_adapters as adapters
import whole_book_plan as plan
import whole_book_executor as runner


def sha(raw):return hashlib.sha256(raw if isinstance(raw,bytes) else raw.encode()).hexdigest()

class SyntheticWorkers:
 def __init__(self,root):
  self.root=root;self.calls=[];self.packages={};self.saved_voice_plans=[]
  self.chapters={'c1':{'text':'林舟推开门。','names':['林舟'],'line':'“你好。”','speaker':'林舟'},
                 'c2':{'text':'顾晴看见林舟。','names':['顾晴','林舟'],'line':'“是谁？”','speaker':'unknown'}}
  self.original_rows=[{'type':'huaben','title':'合成书'},{'type':'chapter','id':'c1','text':'第一章'},
   {'type':'dialogue','id':'qc1','text':'“你好。”','speaker':'顾晴'},
   {'type':'chapter','id':'c2','text':'第二章'},{'type':'dialogue','id':'qc2','text':'“是谁？”','speaker':'unknown'},
   {'type':'dialogue','id':'q-assumed','text':'“别走。”','speaker':'unknown'}]
  self.bindings={chapter:attribution_adapter.capture_binding('synthetic',chapter,self.original_rows) for chapter in self.chapters}
 def callback(self,ctx):
  task=ctx['task'];stage=task['stage'];cid=task.get('chapter_id');self.calls.append((stage,cid))
  deps=ctx['dependencies']
  if stage=='chapter_analysis':
   c=self.chapters[cid];output={'profile':{'characters':[{'name':name,'evidence':c['text']} for name in c['names']]},'summary':'合成摘要'}
  elif stage=='whole_book_cast_fusion':
   snapshot=fusion.new_book('synthetic',['c1','c2'])
   for ch in ('c1','c2'):
    dep=next(x for x in deps if x['key']=='chapter_analysis:'+ch);items=dep['result']['output']['profile']['characters']
    snapshot=fusion.fuse_chapter(snapshot,ch,self.chapters[ch]['text'],items)
   adapted=adapters.fusion_to_executor(snapshot,{cid:self.chapters[cid]['text'] for cid in ('c1','c2')})
   self.fusion_snapshot=snapshot;self.fusion_adapter_output=adapted
   output={'characters':adapted['characters']}
  elif stage=='dialogue_attribution':
   c=self.chapters[cid];qid='q'+cid;assignment={'id':qid,'speaker':c['speaker']}
   uncertain_names={x['name'] for x in self.fusion_adapter_output['characters'] if x.get('uncertainties')}
   if c['speaker'] in uncertain_names:assignment.update({'identity_status':'same_name_assumption','identity_uncertainty':'synthetic fusion identity uncertainty'})
   assignments=[assignment]
   if cid=='c2':assignments.append({'id':'q-assumed','speaker':'林舟','identity_status':'same_name_assumption','identity_uncertainty':'同名身份待核'})
   output={'assignments':assignments,'unknown_ids':[qid] if c['speaker']=='unknown' else [],'conflicts':[]}
  elif stage=='voice_design_text':
   name=task['details']['character'];rows=[]
   for dep in deps:
    for row in dep['result']['output']['assignments']:rows.append({'type':'dialogue','id':row['id'],'text':'合成对白','speaker':row['speaker']})
   memory={'book_id':'synthetic','characters':{name:{'aliases':[],'description':'谨慎'}}}
   result=voices.plan_voice_designs(memory,rows);self.saved_voice_plans.append(result)
   output={'design_text':result['plans'][0]['design_text'],'uses_real_person_reference':False}
  elif stage=='local_voice_synthesis':
   self.assert_dependency(deps,'voice_design_text');name=task['details']['character'];output={'voice_id':'library:zh/fake_voice_'+('a' if name=='林舟' else 'b'),'fixed':True,'local_only':True,'uses_real_person_reference':False}
  elif stage=='chapter_render':
   self.assert_dependency(deps,'dialogue_attribution');self.assert_dependency(deps,'voice_task_expansion')
   synth=[d for d in deps if d['stage']=='local_voice_synthesis']
   if len(synth)!=2:raise AssertionError('render reached before both voice tasks')
   attrib=next(d for d in deps if d['stage']=='dialogue_attribution')['result']['output'];assign=attrib['assignments'][0]
   existing={x['id']:x for x in plan.load_plan(self.root/'plan.json')['tasks']}
   cast_chars={existing[d['task_id']]['details']['character']:{'voice':d['result']['output']['voice_id']} for d in synth}
   cast={'narrator':{'voice':'library:zh/narrator'},'characters':cast_chars}
   before=copy.deepcopy(self.original_rows)
   package=ctx['render_evidence']['staging_package'];self.packages[cid]=package
   assert attribution_adapter.restore_original(package)=={'script_rows':before,'cast':cast}
   assert self.original_rows==before
   file=self.root/(cid+'.fake');file.write_bytes(('synthetic-not-audio-'+ctx['render_evidence']['render_input_sha256']).encode())
   output={'artifact_ref':str(file),'artifact_sha256':sha(file.read_bytes()),'staging_only':True,'human_reviewed':False,
    'staging_audit':ctx['render_evidence']['staging_audit'],'staging_audit_sha256':ctx['render_evidence']['staging_audit_sha256'],
    'staging_package_sha256':ctx['render_evidence']['staging_package_sha256'],'render_input_sha256':ctx['render_evidence']['render_input_sha256'],
    'attribution_task_id':ctx['render_evidence']['attribution_task_id'],'attribution_receipt_sha256':ctx['render_evidence']['attribution_receipt_sha256'],
    'attribution_output_sha256':ctx['render_evidence']['attribution_output_sha256']}
  elif stage=='output_verification':
   render=next(d for d in deps if d['stage']=='chapter_render')['result']['output'];file=Path(render['artifact_ref']);output={'artifact_sha256':sha(file.read_bytes()),'artifact_ref':render['artifact_ref'],'verified':True,'exists':file.is_file(),'staging_audit_sha256':render['staging_audit_sha256'],'render_input_sha256':render['render_input_sha256']}
  else:raise AssertionError('unexpected stage '+stage)
  receipt={'receipt_id':'synthetic-'+ctx['idempotency_key'],'task_id':task['id'],'idempotency_key':ctx['idempotency_key'],
   'input_fingerprint':ctx['input_fingerprint'],'output_sha256':runner.payload_sha256(output),'cache_status':'fresh'}
  if task['details']['billing_mode']=='external':
   request_id='synthetic-request-'+task['id'];response_id='synthetic-response-'+task['id']
   receipt.update({'billing_status':'known','request_id':request_id,'response_id':response_id,
    'billing_receipt':{'receipt_id':'synthetic-billing-'+task['id'],'task_id':task['id'],
      'idempotency_key':ctx['idempotency_key'],'request_id':request_id,'response_id':response_id,
      'usage':{'input_tokens':1,'output_tokens':1}}})
  else:receipt['billing_status']='not_applicable'
  return {'output':output,'receipt':receipt}
 @staticmethod
 def assert_dependency(deps,stage):
  if not any(x['stage']==stage for x in deps):raise AssertionError('missing dependency '+stage)
 def mapping(self):return {x:self.callback for x in ('chapter_analysis','whole_book_cast_fusion','dialogue_attribution','voice_design_text','local_voice_synthesis','chapter_render','output_verification')}
 def render_input_provider(self,plan_state,task):
  expansion=plan_state['voice_expansion'];tasks={item['id']:item for item in plan_state['tasks']}
  synthesized={tasks[dep['id']]['details']['character']:dep['result']['output']['voice_id']
    for dep in plan_state['tasks'] if dep.get('stage')=='local_voice_synthesis' and dep.get('status')=='complete'}
  cast_chars={}
  for char in expansion['characters']:
   voice=char.get('fixed_voice') or synthesized.get(char['name'])
   if not voice:raise AssertionError('missing staged test voice')
   cast_chars[char['name']]={'voice':voice}
  return {'script_rows':copy.deepcopy(self.original_rows),'cast':{'narrator':{'voice':'library:zh/narrator'},'characters':cast_chars}}
 def artifact_reader(self,artifact_ref):
  path=Path(artifact_ref).resolve();path.relative_to(self.root.resolve());return path.read_bytes()

class IntegratedOfflineTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'plan.json';self.fake=SyntheticWorkers(self.root);self.snap={'book_id':'synthetic','chapters':[{'id':'c1','source_sha256':sha('林舟推开门。')},{'id':'c2','source_sha256':sha('顾晴看见林舟。')}],'source_hashes':{'synthetic.txt':sha('合成源')},'characters':[],'fixed_voice_snapshot':{},'external_profile_hash':sha('manual'),'attribution_bindings':self.fake.bindings};plan.create_or_update_plan(self.path,**self.snap)
 def tearDown(self):self.tmp.cleanup()
 def executor(self):return runner.WholeBookExecutor(self.path,workers=self.fake.mapping(),snapshot_provider=lambda:self.snap,render_input_provider=self.fake.render_input_provider,artifact_reader=self.fake.artifact_reader)
 def test_two_chapter_fusion_voice_fallback_and_render_order(self):
  result=self.executor().run_until_blocked(max_steps=40);self.assertEqual(result['status'],'complete')
  stages=[x[0] for x in self.fake.calls];self.assertEqual(stages.count('chapter_analysis'),2);self.assertEqual(stages.count('whole_book_cast_fusion'),1);self.assertEqual(stages.count('local_voice_synthesis'),2);self.assertEqual(stages.count('chapter_render'),2)
  self.assertLess(max(i for i,x in enumerate(stages) if x=='local_voice_synthesis'),min(i for i,x in enumerate(stages) if x=='chapter_render'))
  first=self.fake.packages['c1'];second=self.fake.packages['c2']
  self.assertEqual(first['audit']['fallback_count'],1);self.assertEqual(first['audit']['changes'][0]['original_speaker'],'顾晴');self.assertEqual(first['audit']['changes'][0]['model_speaker'],'林舟');self.assertIn('conflicts_with_existing_speaker',first['audit']['changes'][0]['reasons'])
  self.assertEqual(second['audit']['fallback_count'],2);changes={x['id']:x for x in second['audit']['changes']};self.assertEqual(changes['qc2']['original_speaker'],'unknown');self.assertEqual(changes['q-assumed']['model_reported_identity_status'],'same_name_assumption');self.assertIn('model_identity_uncertain',changes['q-assumed']['reasons'])
  for package,cid in ((first,'qc1'),(second,'qc2'),(second,'q-assumed')):
   self.assertEqual(next(row['speaker'] for row in package['script_rows'] if row.get('id')==cid),staging.FALLBACK_ROLE)
  self.assertTrue(all(x['regeneration_required_after_correction'] for x in second['audit']['changes']))
  self.assertEqual(self.fake.original_rows[2]['speaker'],'顾晴');self.assertEqual(self.fake.original_rows[4]['speaker'],'unknown');self.assertEqual(self.fake.original_rows[5]['speaker'],'unknown')
  self.assertTrue(self.fake.saved_voice_plans);self.assertTrue(all(not x['audio_generated'] and not x['model_called'] for x in self.fake.saved_voice_plans))
  p=plan.load_plan(self.path);fused=next(x for x in p['tasks'] if x['stage']=='whole_book_cast_fusion')['result']['output'];self.assertEqual([x['name'] for x in fused['characters']],['林舟','顾晴']);self.assertFalse(any(t.get('result',{}).get('human_reviewed') for t in p['tasks'] if t.get('result')))
  by_name={x['name']:x for x in fused['characters']};self.assertEqual(by_name['林舟']['appearance_count'],2);self.assertTrue(by_name['林舟']['uncertainties']);self.assertEqual(by_name['顾晴']['appearance_count'],1);self.assertTrue(all(x['evidence'] and not x['fixed_voice'] for x in fused['characters']))
  for task in p['tasks']:
   if task['stage']=='voice_task_expansion':continue
   receipt=task['result']['receipt']
   if task['details']['billing_mode']=='external':self.assertEqual(receipt['billing_status'],'known');self.assertTrue(receipt['billing_receipt']['receipt_id'].startswith('synthetic-'))
   else:self.assertEqual(receipt['billing_status'],'not_applicable')
 def test_dialogue_drift_after_capture_blocks_affected_render(self):
  executor=self.executor()
  while sum(stage=='local_voice_synthesis' for stage,_ in self.fake.calls)<2:executor.execute_next()
  next(row for row in self.fake.original_rows if row.get('id')=='qc2')['text']='“篡改的对白。”'
  with self.assertRaises(runner.ExecutorBlocked):executor.run_until_blocked(max_steps=10)
  self.assertFalse((self.root/'c2.fake').exists());self.assertNotIn('c2',self.fake.packages)
 def test_receipt_ready_restart_recovers_without_duplicate_worker(self):
  executor=self.executor();original=plan.complete_task;attempts=[]
  def crash_once(*args,**kwargs):
   attempts.append(1)
   if len(attempts)==1:raise RuntimeError('synthetic commit interruption')
   return original(*args,**kwargs)
  with patch.object(plan,'complete_task',side_effect=crash_once):
   with self.assertRaises(runner.ExecutorBlocked):executor.execute_next()
  self.assertEqual(self.fake.calls,[('chapter_analysis','c1')]);restarted=self.executor();recovery=restarted.recover();self.assertEqual(recovery['outcome'],'recovered_complete');self.assertEqual(self.fake.calls,[('chapter_analysis','c1')])
  self.assertEqual(restarted.run_until_blocked(max_steps=40)['status'],'complete');self.assertEqual(self.fake.calls.count(('chapter_analysis','c1')),1)
 def test_interrupted_unreceipted_worker_blocks_without_retry(self):
  workers=self.fake.mapping();calls=[]
  def interrupted(context):calls.append(context['task']['id']);raise KeyboardInterrupt('simulated power loss')
  workers['chapter_analysis']=interrupted;executor=runner.WholeBookExecutor(self.path,workers=workers,snapshot_provider=lambda:self.snap)
  with self.assertRaises(KeyboardInterrupt):executor.execute_next()
  recovery=self.executor().recover();self.assertEqual(recovery['outcome'],'blocked_unknown')
  with self.assertRaises(runner.ExecutorBlocked):self.executor().execute_next()
  self.assertEqual(len(calls),1);self.assertEqual(self.fake.calls,[])
if __name__=='__main__':unittest.main()
