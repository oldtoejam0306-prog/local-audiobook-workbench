import hashlib,unittest
import sys,types
import tempfile
from unittest.mock import Mock,patch
import workbench_real_v2_ui as ui
ui.existing.BOOKS={'Synthetic Book':'book-0123456789abcdefabcd','Other Synthetic Book':'book-abcdef0123456789abcd'}
import real_pipeline_workflow as workflow

class SelectionTests(unittest.TestCase):
 def setUp(self):
  self.book=ui.existing.BOOKS['Synthetic Book'];self.digest='a'*64;self.payload={'book_id':self.book,'chapter_id':'c1128.p0735','text':'合成'}
  self.data={'book_id':self.book,'human_reviewed':False,'index_sha256':self.digest}
  self.prepared=dict(self.data,payload=self.payload,payload_sha256=hashlib.sha256(workflow._bytes(self.payload)).hexdigest())
  self.request=Mock(side_effect=lambda action,*args:dict(self.data,chapters=['c001','c1128.p0735']) if action=='list' else self.prepared)
  self.selection=ui.Selection(self.request)
 def test_identity_from_selected_index(self):
  self.selection.load('Synthetic Book');r=self.selection.preview('Synthetic Book','c1128.p0735',{'narrator':'synthetic'})
  self.assertEqual(r['book_id'],self.book);self.assertEqual(r['index_sha256'],self.digest);self.assertEqual(r['source_sha256'],self.prepared['payload_sha256'])
 def test_cross_book_and_unlisted_never_prepare(self):
  self.selection.load('Synthetic Book')
  for book,chapter in [('Other Synthetic Book','c001'),('Synthetic Book','../c001'),('Synthetic Book','c002')]:
   with self.assertRaises(ValueError):self.selection.preview(book,chapter,{'n':'v'})
  self.assertEqual(self.request.call_count,1)
 def test_changed_index_and_payload_rejected(self):
  self.selection.load('Synthetic Book');self.prepared['index_sha256']='b'*64
  with self.assertRaises(ValueError):self.selection.preview('Synthetic Book','c001',{'n':'v'})
  self.prepared['index_sha256']=self.digest;self.prepared['payload_sha256']='0'*64
  with self.assertRaises(ValueError):self.selection.preview('Synthetic Book','c1128.p0735',{'n':'v'})
 def test_failed_load_clears_previous_selection(self):
  self.selection.load('Synthetic Book');self.request.side_effect=RuntimeError('offline')
  with self.assertRaises(RuntimeError):self.selection.load('Other Synthetic Book')
  self.assertIsNone(self.selection.book);self.assertEqual(self.selection.chapters,())
 def test_switch_event_clears_ui(self):
  app=Mock();app.generation=0;app.busy=True;app.selection=self.selection;app.start_chapter=Mock();app.start_box={};app.end_box={};app.end_chapter=Mock();app.selection_info=Mock();app.task_info=Mock();app.completed={};app.task_tree=Mock();app.task_tree.get_children.return_value=[];app.play_button=Mock();app.status=Mock();app.managed_executor=True;app.executor=object();app._stop_shared_playback=Mock();app._set_playback_state=Mock()
  ui.App.changed(app)
  self.assertEqual(app.generation,1);app.start_chapter.set.assert_called_once_with('');self.assertEqual(app.start_box['values'],());self.assertIsNone(app.executor)
  app._stop_shared_playback.assert_called_once_with();app._set_playback_state.assert_called_once_with(False)

 def test_stop_button_state_is_shared_across_tabs(self):
  app=Mock();app.stop_button=Mock();app.voices.stop_button=Mock()
  ui.App._set_playback_state(app,True)
  app.stop_button.configure.assert_called_once_with(state='normal')
  app.voices.set_playback_state.assert_called_once_with(True)

class SequenceStoreTests(unittest.TestCase):
 def test_missing_store_falls_back_to_legacy_sequence_root(self):
  with patch.dict(sys.modules,{'workbench_job_store':None}):
   self.assertEqual(ui._sequence_root('runtime-root','book-abc','c001'),ui.Path('runtime-root')/'sequence')
 def test_store_root_is_passed_through_and_validated_errors_propagate(self):
  expected=ui.Path('runtime-root')/'runs'/'book-abc'/'job'/'sequence';lookup=Mock(return_value=expected)
  with patch.dict(sys.modules,{'workbench_job_store':types.SimpleNamespace(find_sequence_root=lookup)}):
   self.assertEqual(ui._sequence_root('runtime-root','book-abc','c001'),expected)
  lookup.assert_called_once_with('runtime-root','book-abc','c001')
  lookup.side_effect=RuntimeError('invalid pointer')
  with patch.dict(sys.modules,{'workbench_job_store':types.SimpleNamespace(find_sequence_root=lookup)}):
   with self.assertRaisesRegex(RuntimeError,'invalid pointer'):
    ui._sequence_root('runtime-root','book-abc','c001')

 def test_real_store_job_root_is_used(self):
  import workbench_job_store as store
  with tempfile.TemporaryDirectory() as td:
   book='book-'+'e'*20
   job=store.begin_job(td,book,'a'*64,[{'id':'c001','source_sha256':'b'*64}],'c'*64)
   self.assertEqual(ui._sequence_root(td,book,'c001'),ui.Path(job['sequence_root']))

 def test_corrupt_existing_store_error_does_not_fall_back(self):
  import workbench_job_store as store
  with tempfile.TemporaryDirectory() as td:
   book='book-'+'f'*20
   (ui.Path(td)/'runs'/book).mkdir(parents=True)
   with self.assertRaises(store.JobStoreError):
    ui._sequence_root(td,book,'c001')

class PauseTests(unittest.TestCase):
 def test_pause_requests_current_phase_stop_without_cancelling(self):
  app=Mock();app.busy=True;app.busy_kind='execute';app.executor.request_stop=Mock();app.pause_button=Mock();app.status=Mock();app._can_pause=lambda:ui.App._can_pause(app)
  ui.App.pause_after_stage(app)
  app.executor.request_stop.assert_called_once_with()
  app.pause_button.configure.assert_called_once_with(state='disabled')
  app.status.set.assert_called_once()
  self.assertIn('不会取消当前请求',app.status.set.call_args.args[0])
 def test_injected_executor_without_pause_hook_cannot_enable_pause(self):
  app=Mock();app.busy=True;app.busy_kind='execute';app.executor=object()
  self.assertFalse(ui.App._can_pause(app))

class BillingRootsTests(unittest.TestCase):
 def test_billing_window_receives_primary_and_runtime_roots(self):
  module=types.SimpleNamespace(ROOT=ui.Path('legacy-root'),BillingWindow=Mock())
  app=Mock();app._runtime_root.return_value=ui.Path('runtime-root')
  with patch.dict(sys.modules,{'console_voice_billing':module}):
   ui.App.billing(app)
  module.BillingWindow.assert_called_once_with(app,roots=[ui.Path('legacy-root'),ui.Path('runtime-root')])
if __name__=='__main__':unittest.main()


class RangeTests(unittest.TestCase):
 def test_ordered_selection_and_bad_reverse(self):
  book=ui.existing.BOOKS['Synthetic Book'];digest='a'*64
  def request(action,b,c=None):
   d={'book_id':b,'human_reviewed':False,'index_sha256':digest}
   if action=='list':return dict(d,chapters=['c001','c002'])
   p={'book_id':b,'chapter_id':c,'text':'合成'};return dict(d,payload=p,payload_sha256=hashlib.sha256(workflow._bytes(p)).hexdigest())
  s=ui.Selection(request);s.load('Synthetic Book');result=s.preview_range('Synthetic Book','c001','c002',{'mode':'fake'})
  self.assertEqual(result['chapters'],2);self.assertEqual([i['chapter_id'] for i,p in s.frozen[0]],['c001','c002'])
  with self.assertRaises(ValueError):s.preview_range('Synthetic Book','c002','c001',{'mode':'fake'})

class ProfileSummaryTests(unittest.TestCase):
 def test_summary_keeps_usage_unknown_instead_of_zero(self):
  result=ui.profile_result_summary({'status':'needs_review','start':'c001','end':'c004','batches':[{'usage':None}]})
  self.assertIn('用量未知',result)
  self.assertNotIn('0/0 tokens',result)
 def test_partial_summary_marks_unresolved_batches(self):
  result=ui.profile_result_summary({'status':'partial','start':'c001','end':'c009','batches':[{'usage':{'prompt_tokens':7,'completion_tokens':2}}],'usage':{'unknown_batches':1}})
  self.assertIn('已知用量 7/2 tokens',result);self.assertIn('1/2 批',result);self.assertIn('1 批用量未知',result)
 def test_summary_aggregates_only_valid_usage(self):
  result=ui.profile_result_summary({'status':'needs_review','start':'c001','end':'c004','batches':[{'usage':{'prompt_tokens':12,'completion_tokens':3}},{'usage':'bad'}]})
  self.assertIn('12/3 tokens',result);self.assertIn('1/2 批',result);self.assertIn('2 个批次',result)
 def test_aggregate_usage_counts_known_invalid_batches_and_failed_batches_separately(self):
  result=ui.profile_result_summary({'status':'partial','start':'c001','end':'c004',
   'batches':[{'status':'invalid','usage':{'prompt_tokens':8,'completion_tokens':2}}, {'status':'failed','usage':None}],
   'usage':{'prompt_tokens':8,'completion_tokens':2,'known_usage_batches':1,'unknown_usage_batches':1}})
  self.assertIn('8/2 tokens（输入/输出，1/2 批）',result)
  self.assertIn('失败批次 1，无效批次 1',result)

class ProfileActionTests(unittest.TestCase):
 def test_profile_analysis_is_scheduled_only_by_explicit_action(self):
  app=Mock();app.book.get.return_value='Synthetic Book';app.start_chapter.get.return_value='c001';app.end_chapter.get.return_value='c003'
  app.selection.chapters=('c001','c002','c003');app._runtime_root.return_value='runtime-root';scheduled={}
  app._run.side_effect=lambda kind,label,fn:scheduled.update(kind=kind,label=label,fn=fn)
  ui.App.analyze_profiles(app)
  self.assertEqual(scheduled['kind'],'profiles');self.assertFalse(app.messages.put.called)
  service=Mock(return_value={'status':'needs_review'})
  stub=types.SimpleNamespace(analyze_profile_range=service)
  with patch.dict(sys.modules,{'character_profile_jobs':stub}):
   self.assertEqual(scheduled['fn'](),{'status':'needs_review'})
  service.assert_called_once_with('Synthetic Book','c001','c003',root='runtime-root')

 def test_progress_action_schedules_only_read_only_inspection(self):
  app=Mock();app.book.get.return_value='Synthetic Book';app.start_chapter.get.return_value='c001';app.end_chapter.get.return_value='c002'
  app.selection.book=ui.existing.BOOKS['Synthetic Book'];app.selection.chapters=('c001','c002');app._runtime_root.return_value='runtime-root'
  scheduled={};app._run.side_effect=lambda kind,label,fn:scheduled.update(kind=kind,label=label,fn=fn)
  ui.App.inspect_progress(app)
  self.assertEqual(scheduled['kind'],'progress');self.assertIn('不会启动分析或配音',scheduled['label'])
  with patch.object(ui,'inspect_chapter_progress',return_value=[{'chapter_id':'c001'}]) as inspect:
   self.assertEqual(scheduled['fn'](),[{'chapter_id':'c001'}])
  inspect.assert_called_once_with('runtime-root',app.selection.book,('c001','c002'))

class CorrectionSourceTests(unittest.TestCase):
 def test_people_panel_correction_entry_uses_selected_profile(self):
  panel=Mock();panel.listbox.curselection.return_value=(1,);panel.profiles=[{'name':'甲'},{'name':'乙'}]
  panel.correction_callback=Mock()
  ui.voice_panel.ProfileView._open_correction(panel)
  panel.correction_callback.assert_called_once_with(character_name='乙')

 def test_verified_source_uses_fake_loaded_index_and_checks_hash(self):
  book=ui.existing.BOOKS['Synthetic Book'];payload={'book_id':book,'chapter_id':'c001','text':'原文'}
  payload_digest=hashlib.sha256(workflow._bytes(payload)).hexdigest();source_digest=hashlib.sha256(payload['text'].encode()).hexdigest()
  selection=types.SimpleNamespace(book=book,chapters=('c001',),index_sha256='a'*64,
    request=Mock(return_value={'book_id':book,'human_reviewed':False,'index_sha256':'a'*64,
      'payload':payload,'payload_sha256':payload_digest}))
  got=ui.verified_correction_source(selection,'Synthetic Book','c001')
  self.assertEqual(got,(book,payload,source_digest));selection.request.assert_called_once_with('prepare',book,'c001')
 def test_cross_book_index_drift_and_source_tamper_fail_closed(self):
  book=ui.existing.BOOKS['Synthetic Book'];payload={'book_id':book,'chapter_id':'c001','text':'原文'}
  digest=hashlib.sha256(workflow._bytes(payload)).hexdigest()
  selection=types.SimpleNamespace(book=book,chapters=('c001',),index_sha256='a'*64,request=Mock())
  with self.assertRaises(ValueError):ui.verified_correction_source(selection,'Other Synthetic Book','c001')
  selection.request.return_value={'book_id':book,'human_reviewed':False,'index_sha256':'b'*64,'payload':payload,'payload_sha256':digest}
  with self.assertRaises(ValueError):ui.verified_correction_source(selection,'Synthetic Book','c001')
  selection.request.return_value={'book_id':book,'human_reviewed':False,'index_sha256':'a'*64,'payload':payload,'payload_sha256':'0'*64}
  with self.assertRaises(ValueError):ui.verified_correction_source(selection,'Synthetic Book','c001')

class InflightRefreshTests(unittest.TestCase):
 def test_play_busy_with_active_range_preserves_verified_task_row(self):
  app=Mock();app.busy=True;app.busy_kind='play';app.active_range=('book-'+('a'*20),('c003',));app._last_progress_probe=0
  with patch.object(ui,'_sequence_root') as locate:
   ui.App._refresh_inflight(app)
  locate.assert_not_called()
  app.task_tree.item.assert_not_called()

class RealSequenceProgressTests(unittest.TestCase):
 def _create_job(self,root):
  import workbench_job_store
  book=ui.existing.BOOKS['Synthetic Book'];index_sha='a'*64;source_sha='b'*64
  voices={'narrator':{'voice':'preset:test'}}
  job=workbench_job_store.begin_job(root,book,index_sha,[{'id':'c003','source_sha256':source_sha}],
                                    ui.sequence.fingerprint(voices))
  plan=ui.sequence.create_plan(job['sequence_root'],book,index_sha,
                               [{'id':'c003','source_sha256':source_sha}],voices)
  return book,job,plan,source_sha

 def test_progress_accepts_actual_sequence_run_receipt_without_index_identity(self):
  with tempfile.TemporaryDirectory() as td:
   root=ui.Path(td);book,job,plan,_source_sha=self._create_job(root);calls={'count':0}
   def stop_before_memory():
    calls['count']+=1
    return calls['count']>1
   callbacks={stage:(lambda chapter,prior,artifacts,s=stage:{'status':'ok','artifact':{'stage':s}})
              for stage in ui.sequence.STAGES}
   result=ui.sequence.run(job['sequence_root'],plan,callbacks,stop_requested=stop_before_memory)
   self.assertEqual('paused',result['status'])
   done_path=ui.Path(job['sequence_root'])/book/'c003'/'analyze.done.json'
   done=ui.json.loads(done_path.read_text(encoding='utf-8'))
   self.assertNotIn('index_sha256',done['identity'])
   rows=ui.inspect_chapter_progress(root,book,['c003'])
   self.assertEqual(['analyze'],rows[0]['completed_stages'])
   self.assertIsNone(rows[0]['error'])

 def test_optional_index_identity_if_present_must_match_real_plan(self):
  with tempfile.TemporaryDirectory() as td:
   root=ui.Path(td);book,job,plan,_source_sha=self._create_job(root);calls={'count':0}
   def stop_before_memory():
    calls['count']+=1
    return calls['count']>1
   callbacks={stage:(lambda chapter,prior,artifacts,s=stage:{'status':'ok','artifact':{'stage':s}})
              for stage in ui.sequence.STAGES}
   ui.sequence.run(job['sequence_root'],plan,callbacks,stop_requested=stop_before_memory)
   done_path=ui.Path(job['sequence_root'])/book/'c003'/'analyze.done.json'
   done=ui.json.loads(done_path.read_text(encoding='utf-8'));done['identity']['index_sha256']='0'*64
   done_path.write_text(ui.json.dumps(done,ensure_ascii=False),encoding='utf-8')
   rows=ui.inspect_chapter_progress(root,book,['c003'])
   self.assertIn('analyze完成回执与章节计划不匹配',rows[0]['error'])

 def test_literal_normalization_diagnostics_show_safe_details_for_all_changes(self):
  with tempfile.TemporaryDirectory() as td:
   root=ui.Path(td);book,job,_plan,source_sha=self._create_job(root)
   artifacts=ui.Path(job['job_root'])/'artifacts';artifacts.mkdir()
   changes=[{'type':'dialogue_downgraded','id':f'b{i:03d}',
             'reason':'evidence_not_literal_source','original_evidence':'PRIVATE SOURCE QUOTE'}
            for i in range(6)]
   memory={'book_id':book,'chapter_id':'c003','source_sha256':source_sha,
           'unresolved':[{'type':'local_attribution_normalization','changes':changes}],
           'human_reviewed':False}
   memory_path=artifacts/'memory.json';memory_raw=workflow._bytes(memory);memory_path.write_bytes(memory_raw)
   auto={'book_id':book,'chapter_id':'c003','blocked':False,'entities':[],'bindings':{},
         'unresolved':[],'blocking_conflicts':[],'human_reviewed':False}
   auto_path=artifacts/'auto.json';auto_raw=workflow._bytes(auto);auto_path.write_bytes(auto_raw)
   done={'artifact':{'path':str(memory_path),'sha256':hashlib.sha256(memory_raw).hexdigest(),
                     'auto':{'path':str(auto_path),'sha256':hashlib.sha256(auto_raw).hexdigest()}}}
   result=ui.task_diagnostics(root,book,'c003',sequence_root=job['sequence_root'],memory_done=done)
   rows=result['literal_evidence_downgrades']
   self.assertEqual(6,len(rows))
   self.assertEqual({f'b{i:03d}' for i in range(6)},{row['block_id'] for row in rows})
   self.assertTrue(all(row['reason']=='evidence_not_literal_source' for row in rows))
   self.assertTrue(all('original_evidence' not in row for row in rows))
   self.assertEqual(6,result['memory_unresolved'][0]['count'])

 def test_malformed_local_normalization_changes_are_rejected(self):
  with self.assertRaisesRegex(ValueError,'changes must be a list'):
   ui._memory_literal_downgrades([{'type':'local_attribution_normalization','changes':None}])
