import json, unittest
import attribution_batch_validation as a

class BatchValidationTests(unittest.TestCase):
 def setUp(self):
  self.targets=[{'id':'q1','context':'小林说道：“你好。”'}, {'id':'q2','context':'小王摇头：“不。”'}]
  self.items=[{'id':'q1','speaker':'小林','evidence':'小林说道'}, {'id':'q2','speaker':'小王','evidence':'小王摇头'}]
 def validate(self,items=None,finish='stop'):
  return a.validate(json.dumps(self.items if items is None else items,ensure_ascii=False),self.targets,{'小林','小王'},finish)
 def test_exact_evidence_is_only_inference(self):
  out,errors=self.validate(); self.assertFalse(errors); self.assertEqual([x['speaker'] for x in out],['小林','小王']); self.assertTrue(all(x['identity_status']=='model_inference' for x in out))
 def test_wrong_name_missing_evidence_and_unregistered_fall_back(self):
  for name,evidence in [('小王','小林说道'),('小林','改写'),('旁人','小林说道')]:
   items=[dict(x) for x in self.items];items[0].update(speaker=name,evidence=evidence)
   self.assertEqual(self.validate(items)[0][0]['speaker'],'unknown')
 def test_incomplete_duplicate_and_length_stop_fail_batch(self):
  for items,finish in [([self.items[0],self.items[0]],'stop'),(self.items,'length')]:
   out,errors=self.validate(items,finish);self.assertTrue(errors);self.assertTrue(all(x['speaker']=='unknown' for x in out))
 def test_missing_ids_are_individually_unknown(self):
  audit=[];out,errors=a.validate(json.dumps(self.items[:1],ensure_ascii=False),self.targets,{'小林','小王'},'stop',audit=audit)
  self.assertEqual([x['speaker'] for x in out],['小林','unknown']);self.assertIn('missing_ids:1',errors)
  self.assertEqual([x['outcome'] for x in audit],['evidence_matched_unreviewed','missing_id'])
 def test_extra_id_and_invalid_json_reject_whole_batch(self):
  items=[dict(x) for x in self.items];items[1]['id']='outside'
  out,errors=self.validate(items);self.assertTrue(errors);self.assertTrue(all(x['speaker']=='unknown' for x in out))
  out,errors=a.validate('[',self.targets,{'小林','小王'},'stop');self.assertTrue(errors);self.assertTrue(all(x['speaker']=='unknown' for x in out))
 def test_staging_marks_known_and_new_proposals_unconfirmed(self):
  out,_=self.validate();result=a.for_staging(out,{'q1','q2'},{'小林'})
  self.assertEqual(result['conflicts'],[{'id':'q2','reason':'unregistered_model_candidate'}])
  self.assertTrue(all(x['identity_status']=='model_inference' for x in result['assignments']))
  with self.assertRaises(ValueError):a.for_staging(out,{'q1','q2','q3'},{'小林'})
if __name__=='__main__':unittest.main()
