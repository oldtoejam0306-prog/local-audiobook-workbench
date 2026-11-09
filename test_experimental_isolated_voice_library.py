import copy,json,unittest
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import patch
import isolated_huashuo_library as huashuo_library
import experimental_isolated_voice_library as lib
import experimental_mac_voice_orchestrator as orch
import test_experimental_mac_voice_orchestrator as fixture

class LibraryTests(unittest.TestCase):
 def setUp(self):
  self.f=fixture.ExperimentTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.parent=self.f.f.root/'experiment-stage';self.parent.mkdir();self.f.parent=self.parent
  stage_root=patch.object(lib.release_config,'mac_stage_root',return_value=self.parent);stage_root.start();self.addCleanup(stage_root.stop)
  real=Path.is_file
  def is_file(p):return True if p.as_posix().endswith('/usr/bin/sandbox-exec') else real(p)
  snap={'pressure_free_percent':80,'reclaimable_estimate_bytes':10000}
  def simulate(cmd,**kwargs):
   result=self.f.simulate(cmd,**kwargs);phase=cmd[cmd.index('--experiment-child')+1];path=Path(kwargs['cwd'])/(phase+'-result.json')
   data=json.loads(path.read_text(encoding='utf8'));data['inventory_sha256']=(self.f.f.design if phase=='design' else self.f.f.encoder).inventory_sha256;path.write_text(json.dumps(data),encoding='utf8')
   return result
  with patch.object(orch.sys,'platform','darwin'),patch.object(Path,'is_file',is_file),patch.object(orch,'_sandbox_profile',return_value='synthetic-profile'),patch.object(orch,'_resource_lock',return_value=nullcontext()),patch.object(orch,'_check_memory',return_value=snap),patch.object(orch.subprocess,'Popen',side_effect=simulate):self.task=Path(self.f.invoke()['stage'])
  self.presets=self.f.f.root/'presets.json';self.presets.write_text(json.dumps({'synthetic':{'language':'zh','gender':'unknown','age':'unknown'}}),encoding='utf8');self.preset_pin=lib._sha(self.presets.read_bytes())
  self.snapshot={'version':1,'book_id':'synthetic-book','locked':{'other':'voice-locked'}}
 def publish(self,**kw):
  args=dict(task_dir=self.task,experiment_parent=self.parent,book_id='synthetic-book',character_id='c001',locked_snapshot=self.snapshot,locked_snapshot_sha256=lib._sha(lib._canonical(self.snapshot)),trusted_presets_path=self.presets,trusted_presets_sha256=self.preset_pin);args.update(kw);return lib.publish_experiment(**args)
 def test_new_isolated_library_copies_only_vector_and_candidate(self):
  before=(self.task/'speaker-vector.npy').read_bytes();out=self.publish();root=Path(out['library_dir'])
  self.assertEqual(root.parent,self.parent);voice_id=out['voice_ref'].split('/')[-1];self.assertEqual((root/'zh'/(voice_id+'.npy')).read_bytes(),before)
  self.assertEqual(lib.verify_experiment_library(library_dir=root,expected_receipt_sha256=out['receipt_sha256'],trusted_presets_sha256=self.preset_pin)['voice_ref'],out['voice_ref'])
  self.assertEqual({p.name for p in root.iterdir()},{'zh','presets.json','complete.json'})
  self.assertEqual(huashuo_library.validate_library(root,trusted_presets=json.loads(self.presets.read_text(encoding='utf8')))['zh_voices'],1)
  self.assertFalse(out['metadata']['human_reviewed']);self.assertFalse(out['metadata']['production_eligible']);self.assertFalse(out['metadata']['voice_registered'])
  second=self.publish();self.assertNotEqual(second['library_dir'],out['library_dir'])
 def test_locked_character_or_snapshot_mismatch_rejects(self):
  with self.assertRaises(lib.LibraryExperimentError):self.publish(character_id='other')
  with self.assertRaises(lib.LibraryExperimentError):self.publish(locked_snapshot_sha256='0'*64)
 def test_tampered_vector_or_journal_rejects(self):
  p=self.task/'speaker-vector.npy';raw=p.read_bytes();p.write_bytes(raw+b'x')
  with self.assertRaises(lib.LibraryExperimentError):self.publish()
  p.write_bytes(raw);j=self.task/'journal.json';obj=json.loads(j.read_text(encoding='utf8'));obj['status']='unknown_no_retry';j.write_text(json.dumps(obj),encoding='utf8')
  with self.assertRaises(lib.LibraryExperimentError):self.publish()
 def test_model_changed_or_direct_external_vector_rejects(self):
  with self.assertRaises(TypeError):self.publish(vector_path='external.npy')
  (self.f.f.design.snapshot_root/'config.json').write_text('changed')
  with self.assertRaises(lib.LibraryExperimentError):self.publish()
 def test_preset_pin_and_published_metadata_tamper_rejected(self):
  with self.assertRaises(lib.LibraryExperimentError):self.publish(trusted_presets_sha256='0'*64)
  out=self.publish();root=Path(out['library_dir']);voice_id=out['voice_ref'].split('/')[-1]
  meta=root/'zh'/(voice_id+'.json');meta.write_bytes(meta.read_bytes()+b' ')
  with self.assertRaises(lib.LibraryExperimentError):lib.verify_experiment_library(library_dir=root,expected_receipt_sha256=out['receipt_sha256'],trusted_presets_sha256=self.preset_pin)
  self.presets.write_text('{}',encoding='utf8')
  with self.assertRaises(lib.LibraryExperimentError):self.publish()
 def test_shared_or_unrelated_parent_rejects(self):
  with self.assertRaises(lib.LibraryExperimentError):self.publish(experiment_parent=self.f.parent.parent)

if __name__=='__main__':unittest.main()
