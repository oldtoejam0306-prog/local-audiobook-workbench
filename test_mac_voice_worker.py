import io,json,subprocess,sys,tempfile,unittest,wave
from pathlib import Path
import numpy as np
import local_model_inventory as inv
import mac_voice_worker as w

class WorkerTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
  def model(name,role):
   p=self.root/name;p.mkdir();(p/'config.json').write_text('{}');(p/'tokenizer_config.json').write_text('{}');(p/'vocab.json').write_text('{}');(p/'model.safetensors').write_bytes(b'fake')
   i=inv.inventory_model(p,p)
   return {'role':role,'model_id':'synthetic-'+name,'repository_root':str(p),'snapshot_root':str(p),'inventory_sha256':w._hash(w._canonical(i))}
  self.design=model('design','voice_design');self.encoder=model('encoder','speaker_encoder');self.python=str(Path(sys.executable).resolve());self.pin=w._hash_path(Path(self.python))
  self.params={'parent':self.root,'request':{'description':'虚构中性声音','sample_text':'天亮了。','seed':4},'design_model':self.design,'encoder_model':self.encoder,'python':self.python,'python_sha256':self.pin}
  self.calls=[]
 def fake(self,stage,phase,python,timeout):
  self.calls.append(phase)
  if phase=='design':
   with wave.open(str(stage/'generated.wav'),'wb') as f:f.setnchannels(1);f.setsampwidth(2);f.setframerate(24000);f.writeframes(b'\0\0'*40)
  else:
   proof=json.loads((stage/'design-proof.json').read_text());self.assertEqual(proof['wav_sha256'],w._hash_path(stage/'generated.wav'))
   with (stage/'speaker-vector.npy').open('xb') as f:np.save(f,np.zeros(2048,dtype=np.float32),allow_pickle=False)
  return subprocess.CompletedProcess([],0)
 def test_synthetic_outputs_bound_and_inspect_only(self):
  r=w.run_synthetic(**self.params,fake_launcher=self.fake);self.assertEqual(self.calls,['design','encode']);self.assertEqual(r['mode'],'synthetic_test_only');self.assertFalse(r['reference_audio'])
  self.assertEqual(w.inspect_task(r['stage'])['status'],'synthetic_only_complete');self.assertEqual(self.calls,['design','encode'])
 def test_real_disabled_non_mac_and_no_reference_parameter(self):
  with self.assertRaises(w.VoiceWorkerError):w.run_real(**self.params)
  with self.assertRaises(TypeError):w.run_synthetic(**self.params,reference_wav=b'prohibited',fake_launcher=self.fake)
 def test_private_execute_rejects_real_mode_before_stage_or_launcher(self):
  with self.assertRaisesRegex(w.VoiceWorkerError,'synthetic_test_only mode only'):
   w._execute(self.root,self.params['request'],self.design,self.encoder,self.python,self.pin,
              self.fake,timeout=30,mode='real')
  self.assertFalse(list(self.root.glob('mac-fictional-voice-*')));self.assertEqual(self.calls,[])
 def test_mutated_model_and_binary_rejected_before_stage(self):
  (self.root/'design/model.safetensors').write_bytes(b'tampered')
  with self.assertRaises(w.VoiceWorkerError):w.run_synthetic(**self.params,fake_launcher=self.fake)
  self.assertFalse(list(self.root.glob('mac-fictional-voice-*')));self.assertEqual(self.calls,[])
 def test_failure_persists_unknown_and_never_repeats(self):
  def fail(stage,phase,python,timeout):self.calls.append(phase);raise TimeoutError('synthetic')
  with self.assertRaises(TimeoutError):w.run_synthetic(**self.params,fake_launcher=fail)
  stages=list(self.root.glob('mac-fictional-voice-*'));self.assertEqual(len(stages),1)
  status=w.inspect_task(stages[0]);self.assertEqual(status['status'],'synthetic_only_manual_inspection_required');self.assertEqual(status['original_state']['phase'],'design');self.assertEqual(self.calls,['design'])
 def test_output_tamper_is_rejected(self):
  result=w.run_synthetic(**self.params,fake_launcher=self.fake);(Path(result['stage'])/'speaker-vector.npy').write_bytes(b'tampered')
  with self.assertRaises(w.VoiceWorkerError):w.inspect_task(result['stage'])
 def test_untrusted_encode_child_is_hard_closed(self):
  stage=self.root/'external-reference';stage.mkdir();(stage/'generated.wav').write_bytes(b'fake external bytes');(stage/'design-proof.json').write_text(json.dumps({'wav_sha256':w._hash_path(stage/'generated.wav')}))
  with self.assertRaisesRegex(w.VoiceWorkerError,'real child disabled'):w._child(stage,'encode')
  proc=subprocess.run([sys.executable,str(Path(w.__file__).resolve()),'--child','encode',str(stage)],capture_output=True,text=True)
  self.assertNotEqual(proc.returncode,0);self.assertIn('real child disabled',proc.stderr);self.assertFalse((stage/'speaker-vector.npy').exists())
 def test_synthetic_inspection_never_becomes_trusted_real(self):
  result=w.run_synthetic(**self.params,fake_launcher=self.fake);stage=Path(result['stage'])
  proof=w.inspect_task(stage);self.assertEqual(proof['status'],'synthetic_only_complete');self.assertIs(proof['trusted_origin'],False)
  contract=json.loads((stage/'contract.json').read_text(encoding='utf8'));record=json.loads((stage/'result.json').read_text(encoding='utf8'));state=json.loads((stage/'state.json').read_text(encoding='utf8'))
  contract['mode']='real_sandbox';record['mode']='real_sandbox';state['contract_sha256']=w._hash(w._canonical(contract));record['contract_sha256']=state['contract_sha256'];state['result_sha256']=w._hash(w._canonical(record))
  (stage/'contract.json').write_bytes(w._canonical(contract));(stage/'result.json').write_bytes(w._canonical(record));(stage/'state.json').write_bytes(w._canonical(state))
  with self.assertRaisesRegex(w.VoiceWorkerError,'real origin is untrusted'):w.inspect_task(stage)
 def test_nonzero_child_blocks_encoding(self):
  def fail(stage,phase,python,timeout):self.calls.append(phase);return subprocess.CompletedProcess([],2)
  with self.assertRaises(w.VoiceWorkerError):w.run_synthetic(**self.params,fake_launcher=fail)
  self.assertEqual(self.calls,['design'])

if __name__=='__main__':unittest.main()
