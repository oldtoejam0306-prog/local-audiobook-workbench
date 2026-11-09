import copy,unittest
import character_fusion as f
import whole_book_adapters as a

class WholeBookFusionAdapterTests(unittest.TestCase):
 def setUp(self):
  self.texts={'c1':'林舟又名阿舟。林舟来了。','c2':'林舟和顾晴见面。'}
  snap=f.new_book('book',['c1','c2'])
  snap=f.fuse_chapter(snap,'c1',self.texts['c1'],[{'name':'林舟','evidence':'林舟又名阿舟。','aliases':[{'name':'阿舟','evidence':'林舟又名阿舟。'}],'claims':[{'text':'来了','evidence':'林舟来了。'}]},{'name':'林舟','evidence':'林舟来了。'}])
  self.snapshot=f.fuse_chapter(snap,'c2',self.texts['c2'],[{'name':'林舟','evidence':'林舟和顾晴见面。'},{'name':'顾晴','evidence':'林舟和顾晴见面。'}],relations=[{'source_index':0,'target_index':1,'label':'见面','evidence':'林舟和顾晴见面。'}])
 def test_counts_unique_chapters_and_preserves_evidence_and_uncertainty(self):
  before=copy.deepcopy(self.snapshot);out=a.fusion_to_executor(self.snapshot,self.texts);by={x['name']:x for x in out['characters']}
  self.assertEqual(by['林舟']['appearance_count'],2);self.assertEqual(len(by['林舟']['evidence']),3);self.assertEqual([x['chapter_id'] for x in by['林舟']['timeline']],['c1','c2'])
  self.assertEqual(by['顾晴']['appearance_count'],1);self.assertEqual(out['relations'][0]['status'],'textual_candidate_needs_review');self.assertEqual(by['林舟']['claims'][0]['status'],'textual_candidate_needs_review');self.assertEqual(by['林舟']['aliases'][0]['evidence'],'林舟又名阿舟。');self.assertTrue(by['林舟']['uncertainties']);self.assertEqual(by['林舟']['uncertainties'][-1]['kind'],'same_name_assumption');self.assertEqual(out['unresolved_conflicts'],self.snapshot['conflicts']);self.assertEqual(self.snapshot,before)
 def test_executor_projection_keeps_conflicts_and_excludes_nonexecutor_metadata(self):
  adapted=a.fusion_to_executor(self.snapshot,self.texts);output=a.fusion_output_for_executor(adapted)
  self.assertEqual(output['unresolved_conflicts'],adapted['unresolved_conflicts'])
  self.assertEqual(set(output),{'characters','unresolved_conflicts'})
 def test_fusion_and_staging_voice_hints_never_become_user_locks(self):
  snapshot=copy.deepcopy(self.snapshot);snapshot['entities'][0]['voice']='library:zh/temporary';snapshot['entities'][0]['fixed_voice']='library:zh/temporary'
  out=a.fusion_to_executor(snapshot,self.texts);self.assertEqual(out['characters'][0]['fixed_voice'],'');self.assertIsNone(out['characters'][0]['fixed_voice_source'])
  out=a.fusion_to_executor(snapshot,self.texts,user_locked_voices={'林舟':{'voice_id':'library:zh/approved','locked_by':'user','status':'fixed'}})
  self.assertEqual(out['characters'][0]['fixed_voice'],'library:zh/approved');self.assertEqual(out['characters'][0]['fixed_voice_source'],'user_locked')
  for record in ({'voice_id':'library:zh/temporary','locked_by':'model','status':'fixed'},{'voice_id':'library:zh/temporary','locked_by':'user','status':'temporary'},{'voice_id':'../../tmp','locked_by':'user','status':'fixed'}):
   with self.subTest(record=record):
    with self.assertRaises(ValueError):a.fusion_to_executor(snapshot,self.texts,user_locked_voices={'林舟':record})
 def test_missing_or_wrong_sources_rejected(self):
  for texts in ({'c1':self.texts['c1']},{**self.texts,'c2':'伪造文本'},{**self.texts,'future':'尚未处理'}):
   with self.subTest(texts=list(texts)):
    with self.assertRaises(ValueError):a.fusion_to_executor(self.snapshot,texts)
 def test_no_evidence_or_fabricated_mentions_rejected(self):
  for change in ('empty','fake_quote','wrong_name','wrong_chapter','duplicate_id','generic','bad_first','future_alias','forged_claim','forged_relation','short_name'):
   with self.subTest(change=change):
    snap=copy.deepcopy(self.snapshot);entity=snap['entities'][0]
    if change=='empty':entity['mentions']=[]
    if change=='fake_quote':entity['mentions'][0]['evidence']='林舟不存在'
    if change=='wrong_name':entity['mentions'][0]['name']='顾晴'
    if change=='wrong_chapter':entity['mentions'][0]['chapter_id']='c2'
    if change=='duplicate_id':snap['entities'][1]['id']=entity['id']
    if change=='generic':entity['name']='船长'
    if change=='short_name':entity['name']='林'
    if change=='bad_first':entity['mentions'][1]['identity_basis']='first_occurrence'
    if change=='future_alias':entity['aliases'][0]['chapter_id']='c2'
    if change=='forged_claim':entity['claims'][0]['evidence']='虚构'
    if change=='forged_relation':snap['relations'][0]['label']='亲属'
    with self.assertRaises(ValueError):a.fusion_to_executor(snap,self.texts)
 def test_name_collision_and_unknown_lock_rejected(self):
  snap=copy.deepcopy(self.snapshot);snap['entities'][1]['name']='林舟'
  with self.assertRaises(ValueError):a.fusion_to_executor(snap,self.texts)
  with self.assertRaises(ValueError):a.fusion_to_executor(self.snapshot,self.texts,user_locked_voices={'不存在':{'voice_id':'voice','locked_by':'user','status':'fixed'}})
 def test_partial_snapshot_only_accepts_current_chapters(self):
  snap=f.new_book('book',['c1','c2']);snap=f.fuse_chapter(snap,'c1',self.texts['c1'],[{'name':'林舟','evidence':'林舟来了。'}]);out=a.fusion_to_executor(snap,{'c1':self.texts['c1']});self.assertEqual(out['characters'][0]['appearance_count'],1)
  with self.assertRaises(ValueError):a.fusion_to_executor(snap,self.texts)
if __name__=='__main__':unittest.main()
