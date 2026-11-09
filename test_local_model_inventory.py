import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import local_model_inventory as m

class ModelInventoryTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.repo=Path(self.temp.name);self.snap=self.repo/'snapshots'/'rev';self.snap.mkdir(parents=True)
  (self.snap/'config.json').write_text('{"model_type":"qwen"}')
  (self.snap/'tokenizer_config.json').write_text('{"type":"fictional"}')
  (self.snap/'vocab.json').write_text('{}')
  (self.snap/'model-1.safetensors').write_bytes(b'0123456789')
  (self.snap/'model-2.safetensors').write_bytes(b'abcdefghij')
  self.index={'metadata':{'total_size':15},'weight_map':{'weight.a':'model-1.safetensors','weight.b':'model-2.safetensors'}}
  self.idx=self.snap/'model.safetensors.index.json';self.idx.write_text(json.dumps(self.index))
 def tearDown(self):self.temp.cleanup()
 def run_inventory(self):return m.inventory_model(self.repo,self.snap,chunk_size=3)
 def test_deterministic_complete_index_and_streamed_hash(self):
  before=self.run_inventory();after=self.run_inventory();self.assertEqual(before,after)
  self.assertEqual(len(before['files']),6);self.assertEqual(before['indexes'][0]['shards'],['model-1.safetensors','model-2.safetensors'])
  self.assertEqual(before['total_size_bytes'],sum(p.stat().st_size for p in self.snap.iterdir()))
  original=Path.read_bytes
  def guarded(path):
   if path.suffix=='.safetensors':raise AssertionError('weight read_bytes')
   return original(path)
  with patch('pathlib.Path.read_bytes',guarded):self.run_inventory()
 def test_missing_bad_index_and_escape(self):
  for shard in ('missing.safetensors','../outside.safetensors','https://example.com/model.safetensors','C:\\outside.safetensors'):
   bad=dict(self.index,weight_map={'w':shard});self.idx.write_text(json.dumps(bad))
   with self.subTest(shard=shard),self.assertRaises(m.ModelInventoryError):self.run_inventory()
  self.idx.write_text('{"weight_map":{"x":"model-1.safetensors","x":"model-2.safetensors"}}')
  with self.assertRaises(m.ModelInventoryError):self.run_inventory()
  self.idx.write_text(json.dumps({'metadata':{'total_size':-1},'weight_map':{'x':'model-1.safetensors'}}))
  with self.assertRaises(m.ModelInventoryError):self.run_inventory()
 def test_missing_config_and_bad_json_fail(self):
  (self.snap/'config.json').unlink()
  with self.assertRaises(m.ModelInventoryError):self.run_inventory()
  (self.snap/'config.json').write_text('{bad')
  with self.assertRaises(m.ModelInventoryError):self.run_inventory()
 def test_hf_blob_symlink_within_repo_and_escape(self):
  blob=self.repo/'blobs';blob.mkdir();(blob/'hash').write_bytes(b'synthetic')
  weight=self.snap/'model-1.safetensors';weight.unlink()
  try:os.symlink(os.path.relpath(blob/'hash',self.snap),weight)
  except OSError:self.skipTest('symlink creation unavailable')
  result=self.run_inventory();row=next(x for x in result['files'] if x['path']=='model-1.safetensors');self.assertEqual(row['repository_target'],'blobs/hash')
  outside=self.repo.parent/(self.repo.name+'-outside');outside.write_bytes(b'x')
  try:
   weight.unlink();os.symlink(outside,weight)
   with self.assertRaises(m.ModelInventoryError):self.run_inventory()
  finally:outside.unlink()
 def test_reject_url_and_outside_snapshot(self):
  with self.assertRaises(m.ModelInventoryError):m.inventory_model('https://example.com/repo',self.snap)
  with tempfile.TemporaryDirectory() as other:
   with self.assertRaises(m.ModelInventoryError):m.inventory_model(other,self.snap)

if __name__=='__main__':unittest.main()
