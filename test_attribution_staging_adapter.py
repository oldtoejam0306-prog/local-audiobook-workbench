import copy,unittest
import attribution_staging_adapter as a
import staging_render_cast as s

class AttributionStagingTests(unittest.TestCase):
 def setUp(self):
  self.rows=[{'type':'huaben','title':'synthetic'},{'type':'chapter','id':'c1','text':'一'},
    {'type':'dialogue','id':'q1','text':'“你好。”','speaker':'林舟'},
    {'type':'dialogue','id':'q2','text':'“是谁？”','speaker':'unknown'},
    {'type':'dialogue','id':'q3','text':'“再见。”','speaker':'顾晴'},
    {'type':'narration','id':'n1','text':'夜色。'},
    {'type':'chapter','id':'c2','text':'二'},
    {'type':'dialogue','id':'q4','text':'“远处。”','speaker':'unknown'}]
  self.cast={'narrator':{'voice':'library:zh/narrator'},'characters':{'林舟':{'voice':'preset:Vivian'},'顾晴':{'voice':'preset:Serena'}}}
  self.attr={'assignments':[{'id':'q1','speaker':'林舟'},{'id':'q2','speaker':'unknown'},{'id':'q3','speaker':'林舟'}],'unknown_ids':['q2'],'conflicts':[]}
  self.binding=a.capture_binding('book','c1',self.rows)
 def build(self,attribution=None,binding=None,rows=None,cast=None):return a.adapt_attribution('book','c1',self.rows if rows is None else rows,self.cast if cast is None else cast,self.attr if attribution is None else attribution,self.binding if binding is None else binding)
 def test_fallback_and_mismatch_are_isolated_and_reversible(self):
  original=copy.deepcopy(self.rows);cast=copy.deepcopy(self.cast);out=self.build();self.assertEqual(self.rows,original);self.assertEqual(self.cast,cast)
  self.assertEqual(out['script_rows'][2]['speaker'],'林舟');self.assertEqual(out['script_rows'][3]['speaker'],s.FALLBACK_ROLE);self.assertEqual(out['script_rows'][4]['speaker'],s.FALLBACK_ROLE);self.assertEqual(out['script_rows'][7]['speaker'],'unknown')
  audit=out['audit'];self.assertEqual(audit['fallback_count'],2);self.assertFalse(audit['human_reviewed']);self.assertEqual(audit['changes'][2]['reasons'],['conflicts_with_existing_speaker']);self.assertEqual(audit['changes'][2]['original_speaker'],'顾晴');self.assertEqual(a.restore_original(out),{'script_rows':original,'cast':cast})
 def test_unknown_source_can_get_model_proposal_but_not_confirmed_identity(self):
  attr=copy.deepcopy(self.attr);attr['assignments'][1]['speaker']='林舟';attr['unknown_ids']=[];attr['assignments'][2]['speaker']='顾晴'
  out=self.build(attr);entry=out['audit']['changes'][1];self.assertEqual(out['script_rows'][3]['speaker'],'林舟');self.assertEqual(entry['identity_status'],'model_attribution_not_confirmed');self.assertTrue(entry['requires_identity_review']);self.assertTrue(entry['regeneration_required_after_correction']);self.assertEqual(a.restore_original(out)['script_rows'],self.rows)
 def test_conflict_for_known_speaker_uses_fallback(self):
  attr=copy.deepcopy(self.attr);attr['assignments'][2]['speaker']='顾晴';attr['conflicts']=[{'id':'q1','candidates':['林舟','顾晴']}]
  out=self.build(attr);self.assertEqual(out['script_rows'][2]['speaker'],s.FALLBACK_ROLE);self.assertEqual(out['audit']['changes'][0]['reasons'],['model_conflict']);self.assertEqual(a.restore_original(out)['script_rows'],self.rows)
 def test_identity_uncertainty_is_preserved_and_falls_back(self):
  attr=copy.deepcopy(self.attr);attr['assignments'][0]['identity_status']='same_name_assumption';attr['assignments'][0]['identity_uncertainty']='同名可能是不同人物'
  out=self.build(attr);entry=out['audit']['changes'][0];self.assertEqual(out['script_rows'][2]['speaker'],s.FALLBACK_ROLE);self.assertEqual(entry['model_reported_identity_status'],'same_name_assumption');self.assertEqual(entry['model_reported_identity_uncertainty'],'同名可能是不同人物');self.assertIn('model_identity_uncertain',entry['reasons']);self.assertEqual(a.restore_original(out)['script_rows'],self.rows)
 def test_model_claimed_confirmation_is_not_user_confirmation(self):
  attr=copy.deepcopy(self.attr);attr['assignments'][1]['speaker']='林舟';attr['assignments'][1]['identity_status']='confirmed';attr['unknown_ids']=[]
  out=self.build(attr);entry=out['audit']['changes'][1];self.assertEqual(entry['identity_status'],'model_attribution_not_confirmed');self.assertTrue(entry['requires_identity_review']);self.assertFalse(out['audit']['human_reviewed'])
 def test_delivery_is_kept_in_separate_audit_map(self):
  attr=copy.deepcopy(self.attr);attr['assignments'][0]['delivery']='tense'
  out=self.build(attr)
  self.assertEqual({'q1':'tense'},out['audit']['delivery'])
  self.assertEqual(self.rows,a.restore_original(out)['script_rows'])
  for bad in ('excited',None,{'x':1}):
   invalid=copy.deepcopy(attr);invalid['assignments'][0]['delivery']=bad
   with self.subTest(bad=bad),self.assertRaises(ValueError):self.build(invalid)
 def test_conflicted_unregistered_proposal_uses_fallback_not_cast(self):
  attr=copy.deepcopy(self.attr);attr['assignments'][1]['speaker']='未登记人物';attr['unknown_ids']=[];attr['conflicts']=[{'id':'q2'}]
  out=self.build(attr);self.assertEqual(out['script_rows'][3]['speaker'],s.FALLBACK_ROLE);self.assertNotIn('未登记人物',out['cast']['characters']);self.assertEqual(out['audit']['changes'][1]['model_speaker'],'未登记人物')
 def test_assignment_must_cover_exact_ids_and_consistent_unknowns(self):
  for mode in ('missing','extra','duplicate','bad_unknown','duplicate_unknown','bad_conflict','duplicate_conflict','empty_speaker','other_speaker','bad_fields'):
   with self.subTest(mode=mode):
    attr=copy.deepcopy(self.attr)
    if mode=='missing':attr['assignments'].pop()
    if mode=='extra':attr['assignments'].append({'id':'q4','speaker':'unknown'})
    if mode=='duplicate':attr['assignments'].append(copy.deepcopy(attr['assignments'][0]))
    if mode=='bad_unknown':attr['unknown_ids']=[]
    if mode=='duplicate_unknown':attr['unknown_ids']=['q2','q2']
    if mode=='bad_conflict':attr['conflicts']=[{'id':'q4'}]
    if mode=='duplicate_conflict':attr['conflicts']=[{'id':'q1'},{'id':'q1'}]
    if mode=='empty_speaker':attr['assignments'][0]['speaker']=''
    if mode=='other_speaker':attr['assignments'][0]['speaker']='不存在'
    if mode=='bad_fields':attr['assignments'][0]['kind']='narration'
    before=copy.deepcopy(self.rows)
    with self.assertRaises(ValueError):self.build(attr)
    self.assertEqual(self.rows,before)
 def test_source_drift_and_cross_book_or_chapter_rejected(self):
  for mode in ('text','emotion','book','chapter','hash','missing'):
   with self.subTest(mode=mode):
    rows=copy.deepcopy(self.rows);binding=copy.deepcopy(self.binding)
    if mode=='text':rows[3]['text']='不同'
    if mode=='emotion':rows[3]['emotion']='新增'
    if mode=='book':binding['book_id']='other'
    if mode=='chapter':binding['chapter_id']='c2'
    if mode=='hash':binding['dialogue_text_sha256']['q1']='0'*64
    if mode=='missing':del binding['dialogue_text_sha256']['q1']
    with self.assertRaises(ValueError):self.build(binding=binding,rows=rows)
 def test_tampering_breaks_reversible_integrity(self):
  for mode in ('script','cast','mapping','segment_manifest'):
   with self.subTest(mode=mode):
    out=self.build()
    if mode=='script':out['script_rows'][3]['text']='伪造'
    if mode=='cast':out['cast']['characters'][s.FALLBACK_ROLE]['voice']='preset:Vivian'
    if mode=='mapping':out['audit']['changes'][2]['original_speaker']='林舟'
    if mode=='segment_manifest':out['audit']['staging_manifest']['fallback_count']=0
    with self.assertRaises(ValueError):a.restore_original(out)
 def test_missing_duplicate_chapters_and_dialogue_ids_rejected(self):
  rows=copy.deepcopy(self.rows);rows[6]['id']='c1'
  with self.assertRaises(ValueError):a.capture_binding('book','c1',rows)
  rows=copy.deepcopy(self.rows);rows[7]['id']='q1'
  with self.assertRaises(ValueError):a.capture_binding('book','c1',rows)
 def test_chapter_without_detected_dialogue_does_not_assert_review(self):
  rows=[{'type':'chapter','id':'c1','text':'章'},{'type':'narration','id':'n','text':'正文。'}];binding=a.capture_binding('book','c1',rows)
  out=a.adapt_attribution('book','c1',rows,self.cast,{'assignments':[],'unknown_ids':[],'conflicts':[]},binding);self.assertFalse(out['audit']['human_reviewed']);self.assertEqual(out['audit']['changes'],[])
if __name__=='__main__':unittest.main()
