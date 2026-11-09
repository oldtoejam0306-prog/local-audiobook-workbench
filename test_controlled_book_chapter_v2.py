import concurrent.futures,json,tempfile,unittest
from pathlib import Path
import controlled_book_chapter_v2 as p
import workbench_real_v2_client as c

class V2Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.legacy=self.root/'legacy';(self.legacy/'jobs').mkdir(parents=True)
  self.proto=p.Protocol(self.root/'new',self.legacy)
  self.a=c.definition('book-'+'a'*20,'c007.p001',b'synthetic','1'*64,{'narrator':'v1'})
 def tearDown(self):self.tmp.cleanup()
 def test_cross_book_and_identity(self):
  b=dict(self.a,book_id='book-'+'b'*20)
  self.assertEqual(self.proto.reserve(self.a)['status'],'reserved');self.assertEqual(self.proto.reserve(b)['status'],'reserved')
  self.assertNotEqual(p.validate(self.a),p.validate(b))
  self.assertEqual(self.proto.reserve(dict(self.a,source_sha256='2'*64))['status'],'conflict_or_uncertain')
 def test_concurrent_never_duplicate(self):
  with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:r=list(pool.map(lambda _:self.proto.reserve(self.a),range(16)))
  self.assertEqual(sum(x['status']=='reserved' for x in r),1)
  self.assertEqual(self.proto.reserve(self.a)['status'],'already_reserved')
 def test_unknown_and_failure_never_retried(self):
  for status in ('unknown','failed'):
   ident=dict(self.a,chapter_id='c001' if status=='unknown' else 'c002');calls=[]
   self.assertEqual(self.proto.run_mock(ident,lambda x:(calls.append(x),status)[1])['status'],status)
   self.assertEqual(self.proto.run_mock(ident,lambda x:calls.append(x))['status'],'already_reserved');self.assertEqual(len(calls),1)
 def test_legacy_blocks_and_preserves(self):
  d=self.legacy/'jobs'/'old';d.mkdir();(d/'state.json').write_text(json.dumps({'status':'running'}))
  self.assertEqual(self.proto.reserve(self.a)['status'],'legacy_uncertain');self.assertFalse((self.root/'new'/'books').exists())
 def test_invalid_paths(self):
  for chapter in ('../c001','c001/evil','c001..p1'):
   with self.assertRaises(ValueError):p.validate(dict(self.a,chapter_id=chapter))
 def test_private_stage_is_immutable(self):
  r=c.reserve(self.root/'new',self.legacy,self.a,b'synthetic',{'narrator':'v1'})
  self.assertEqual(r['status'],'reserved')
  stage=self.root/'new'/'books'/self.a['book_id']/self.a['chapter_id']
  self.assertEqual((stage/'source.bin').read_bytes(),b'synthetic')
  self.assertEqual(c.reserve(self.root/'new',self.legacy,self.a,b'synthetic',{'narrator':'v1'})['status'],'already_reserved')
  with self.assertRaises(ValueError):c.reserve(self.root/'new',self.legacy,self.a,b'changed',{'narrator':'v1'})
  self.assertEqual((stage/'source.bin').read_bytes(),b'synthetic')
if __name__=='__main__':unittest.main()
