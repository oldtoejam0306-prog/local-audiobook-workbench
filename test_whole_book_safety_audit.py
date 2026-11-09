import copy,tempfile,unittest
from pathlib import Path
import whole_book_plan as plan
import whole_book_executor as ex
import test_whole_book_executor as fixture
import attribution_staging_adapter as staging_binding

class WholeBookSafetyAuditTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.path=Path(self.t.name)/'plan.json';self.script_rows=[{'type':'chapter','id':'c1','src':[0,0]},{'type':'dialogue','id':'q-c1','text':'synthetic audit line','speaker':'synthetic-hero'}];self.snapshot={'book_id':'audit-book','chapters':[{'id':'c1','source_sha256':fixture.sha('chapter')}],'source_hashes':{'book.txt':fixture.sha('book')},'characters':[{'name':'synthetic-hero','appearance_count':1}],'fixed_voice_snapshot':{},'external_profile_hash':fixture.sha('profiles'),'attribution_bindings':{'c1':staging_binding.capture_binding('audit-book','c1',self.script_rows)}};plan.create_or_update_plan(self.path,**self.snapshot)
 def tearDown(self):self.t.cleanup()
 def make_executor(self,workers=None):
  fake=fixture.FakeWorkers(self.script_rows);return ex.WholeBookExecutor(self.path,workers=workers or fake.mapping(),snapshot_provider=lambda:self.snapshot,render_input_provider=fake.render_input_provider,artifact_reader=fake.artifact_reader),fake
 def test_invalid_profile_and_summary_must_not_complete(self):
  fake=fixture.FakeWorkers();workers=fake.mapping(external_billing=True);seen=[]
  def invalid(ctx):
   r=workers_base(ctx);seen.append(copy.deepcopy(r['receipt']));r['output']={'profile':None,'summary':None};r['receipt']['output_sha256']=ex.payload_sha256(r['output']);return r
  workers_base=workers['chapter_analysis']
  workers['chapter_analysis']=invalid;executor=ex.WholeBookExecutor(self.path,workers=workers,snapshot_provider=lambda:self.snapshot)
  with self.assertRaises(ex.ExecutorBlocked):executor.execute_next()
  receipt=seen[0];billing=receipt['billing_receipt'];task=plan.load_plan(self.path)['tasks'][0]
  self.assertEqual(receipt['billing_status'],'known')
  self.assertEqual(billing['task_id'],task['id'])
  self.assertEqual(billing['idempotency_key'],receipt['idempotency_key'])
 def test_cloud_analysis_must_have_billing_evidence(self):
  executor,_=self.make_executor()
  # A nominally remote chapter-analysis callback cannot self-declare billing not applicable.
  with self.assertRaises(ex.ExecutorBlocked):executor.execute_next()
 def test_billing_receipt_cannot_be_arbitrary_truthy_text(self):
  fake=fixture.FakeWorkers();workers=fake.mapping()
  def false_evidence(ctx):
   result=fake.callback(ctx);result['receipt']['billing_status']='known';result['receipt']['billing_receipt']='fabricated';return result
  workers['chapter_analysis']=false_evidence;executor=ex.WholeBookExecutor(self.path,workers=workers,snapshot_provider=lambda:self.snapshot)
  with self.assertRaises(ex.ExecutorBlocked):executor.execute_next()
 def test_plan_claim_cannot_render_before_voice_expansion(self):
  data=plan.load_plan(self.path)
  for stage in ('chapter_analysis','whole_book_cast_fusion','dialogue_attribution'):
   task=next(t for t in data['tasks'] if t['stage']==stage);claimed=plan.claim_task(self.path,task['id']);plan.complete_task(self.path,task['id'],result={'test':'synthetic'},idempotency_key=claimed['idempotency_key']);data=plan.load_plan(self.path)
  render=next(t for t in data['tasks'] if t['stage']=='chapter_render')
  with self.assertRaises(ValueError):plan.claim_task(self.path,render['id'])
 def test_redo_upstream_must_invalidate_existing_descendants(self):
  fake=fixture.FakeWorkers(self.script_rows);executor=ex.WholeBookExecutor(self.path,workers=fake.mapping(external_billing=True),snapshot_provider=lambda:self.snapshot,render_input_provider=fake.render_input_provider,artifact_reader=fake.artifact_reader);executor.run_until_blocked(max_steps=30);data=plan.load_plan(self.path);self.assertEqual(data['status'],'complete')
  chapter=next(t for t in data['tasks'] if t['stage']=='chapter_analysis')
  with self.assertRaises(ValueError):plan.queue_redo(self.path,chapter['id'],reason='changed source interpretation',confirm_redo=True,cascade=False)
 def test_stale_after_worker_must_persist_accepted_receipt(self):
  fake=fixture.FakeWorkers();workers=fake.mapping()
  def drift(ctx):
   output=fake.callback(ctx);self.snapshot['source_hashes']={'book.txt':fixture.sha('changed')};return output
  workers['chapter_analysis']=drift;executor=ex.WholeBookExecutor(self.path,workers=workers,snapshot_provider=lambda:self.snapshot)
  with self.assertRaises(ex.StaleInputs):executor.execute_next()
  checkpoint=ex._read_checkpoints(executor.checkpoint_path);records=[r for items in checkpoint['attempts'].values() for r in items]
  accepted=[r for r in records if r.get('state')=='stale_after_worker' and r.get('receipt')]
  self.assertEqual(len(accepted),1)
  record=accepted[0];receipt=record['receipt'];billing=receipt['billing_receipt']
  self.assertEqual(receipt['billing_status'],'known')
  self.assertEqual(receipt['task_id'],record['task_id'])
  self.assertEqual(receipt['idempotency_key'],record['idempotency_key'])
  self.assertEqual(billing['task_id'],record['task_id'])
  self.assertEqual(billing['idempotency_key'],record['idempotency_key'])
  self.assertEqual(record['receipt_sha256'],ex.payload_sha256(receipt))
 def test_empty_fusion_cannot_silently_omit_known_unvoiced_character(self):
  self.snapshot['characters']=[{'name':'KnownName','appearance_count':1,'fixed_voice':''}];self.path.unlink();plan.create_or_update_plan(self.path,**self.snapshot)
  fake=fixture.FakeWorkers(self.script_rows);workers=fake.mapping(external_billing=True);base=workers['whole_book_cast_fusion']
  def empty(ctx):
   r=base(ctx);r['output']={'characters':[],'no_characters_detected':True};r['receipt']['output_sha256']=ex.payload_sha256(r['output']);return r
  workers['whole_book_cast_fusion']=empty;executor=ex.WholeBookExecutor(self.path,workers=workers,snapshot_provider=lambda:self.snapshot)
  with self.assertRaises(ex.ExecutorBlocked):executor.run_until_blocked(max_steps=30)
  self.assertIn('whole_book_cast_fusion',[stage for stage,_ in fake.calls])
  self.assertNotIn('chapter_render',[stage for stage,_ in fake.calls])
if __name__=='__main__':unittest.main()
