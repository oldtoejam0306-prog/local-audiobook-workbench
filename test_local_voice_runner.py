import io,json,tempfile,unittest,wave
from pathlib import Path
import numpy as np
import local_model_inventory as inventory
import local_voice_runner as runner

class FakeEngine:
 def __init__(self):self.wav=None;self.encoded=None;self.calls=[]
 def design(self,request):
  self.calls.append(('design',request));buf=io.BytesIO()
  with wave.open(buf,'wb') as f:f.setnchannels(1);f.setsampwidth(2);f.setframerate(24000);f.writeframes(b'\0\0'*80)
  self.wav=buf.getvalue();return self.wav
 def encode(self,wav):
  self.calls.append(('encode',wav));self.encoded=wav;buf=io.BytesIO();np.save(buf,np.zeros(2048,dtype=np.float32),allow_pickle=False);return buf.getvalue()

class LocalVoiceRunnerTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  def model(name,role):
   p=self.root/name;p.mkdir();(p/'config.json').write_text('{}');(p/'tokenizer_config.json').write_text('{}');(p/'vocab.json').write_text('{}');(p/'model.safetensors').write_bytes(b'synthetic-not-a-model')
   return runner.LocalModel(role=role,model_id='synthetic-'+name,repository_root=p,snapshot_root=p,inventory_sha256=runner.inventory_sha256(inventory.inventory_model(p,p)))
  self.design=model('design','voice_design');self.encoder=model('encoder','speaker_encoder');self.obj=runner.LocalVoiceRunner(design_model=self.design,encoder_model=self.encoder)
  self.engine=FakeEngine();self.args={'description':'虚构中文角色，清晰中性。','sample_text':'夜晚的微风吹过街道。','seed':17}
 def test_design_then_encode_exact_generated_bytes_and_hashes(self):
  before=sorted(str(x.relative_to(self.root)) for x in self.root.rglob('*'))
  result=self.obj.run_synthetic(**self.args,engine=self.engine)
  self.assertEqual(self.engine.encoded,self.engine.wav);self.assertEqual([x[0] for x in self.engine.calls],['design','encode'])
  self.assertEqual(result['mode'],'synthetic_test_only');self.assertFalse(result['request']['reference_audio']);self.assertFalse(result['human_reviewed'])
  self.assertEqual(result['generated_wav_sha256'],runner._hash(self.engine.wav));self.assertEqual(result['speaker_vector_sha256'],runner._hash(result['speaker_vector_npy']))
  self.assertEqual(before,sorted(str(x.relative_to(self.root)) for x in self.root.rglob('*')))
 def test_real_default_closed_and_no_reference_parameter(self):
  with self.assertRaises(runner.VoiceRunnerBlocked):self.obj.run_real(**self.args)
  with self.assertRaises(TypeError):self.obj.run_synthetic(**self.args,engine=self.engine,reference_wav=b'real')
 def test_inventory_tamper_and_role_swap_rejected(self):
  (self.design.snapshot_root/'model.safetensors').write_bytes(b'changed')
  with self.assertRaises(runner.VoiceRunnerBlocked):self.obj.run_synthetic(**self.args,engine=self.engine)
  self.assertFalse(self.engine.calls)
  other=runner.LocalVoiceRunner(design_model=self.encoder,encoder_model=self.design)
  with self.assertRaises(runner.VoiceRunnerBlocked):other.run_synthetic(**self.args,engine=self.engine)
 def test_model_mutation_during_run_cannot_be_accepted(self):
  class Tamper(FakeEngine):
   def encode(inner,wav):
    result=super().encode(wav);(self.encoder.snapshot_root/'vocab.json').write_text('{"changed":1}');return result
  with self.assertRaises(runner.VoiceRunnerBlocked):self.obj.run_synthetic(**self.args,engine=Tamper())
 def test_invalid_wav_or_vector_stops(self):
  self.engine.design=lambda request:b'bad'
  with self.assertRaises(runner.VoiceRunnerBlocked):self.obj.run_synthetic(**self.args,engine=self.engine)
  self.engine=FakeEngine();self.engine.encode=lambda wav:b'bad'
  with self.assertRaises(runner.VoiceRunnerBlocked):self.obj.run_synthetic(**self.args,engine=self.engine)
 def test_bad_request_or_remote_identity_rejected(self):
  with self.assertRaises(runner.VoiceRunnerBlocked):self.obj.run_synthetic(**{**self.args,'seed':True},engine=self.engine)
  from dataclasses import replace
  invalid=runner.LocalVoiceRunner(design_model=replace(self.design,model_id='https://example.invalid/model'),encoder_model=self.encoder)
  with self.assertRaises(runner.VoiceRunnerBlocked):invalid.run_synthetic(**self.args,engine=self.engine)

if __name__=='__main__':unittest.main()
