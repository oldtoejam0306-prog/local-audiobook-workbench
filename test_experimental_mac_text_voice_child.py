import types
import unittest
import numpy as np
import experimental_mac_text_voice_child as child
import test_local_voice_runner as fixture

class FakeMlx:
 def __init__(self,events):self.events=events;self.random=types.SimpleNamespace(seed=lambda n:events.append(('seed',n)))
 def eval(self,x):pass
 def clear_cache(self):self.events.append('cache_clear')
 def array(self,x):self.events.append(('encoder_input',x.copy()));return x

class ChildTests(unittest.TestCase):
 def setUp(self):
  self.f=fixture.LocalVoiceRunnerTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.stage=self.f.root/'stage';self.stage.mkdir();self.events=[];self.mx=FakeMlx(self.events)
 def loader(self,path):
  events=self.events
  if path==str(self.f.design.snapshot_root.resolve()):
   class Design:
    def generate_voice_design(self,**kwargs):
     events.append('design');yield types.SimpleNamespace(audio=np.array([.1,-.2],dtype=np.float32),sample_rate=24000)
    def __del__(self):events.append('design_released')
   return Design()
  events.append('base_loaded')
  class Base:
   def extract_speaker_embedding(self,samples):
    events.append(('base_samples',samples.copy()));return np.ones(2048,dtype=np.float32)
  return Base()
 def run_child(self,**kw):
  args=dict(stage=self.stage,description='fictional neutral',text='synthetic words',seed=4,
   design_model=self.f.design,base_model=self.f.encoder,np=np,mx=self.mx,load_model=self.loader)
  args.update(kw);return child._execute_unwired(**args)
 def test_same_generated_samples_encode_after_release(self):
  result=self.run_child();self.assertFalse(result['production_eligible']);self.assertFalse(result['reference_audio'])
  self.assertLess(self.events.index('design_released'),self.events.index('base_loaded'))
  np.testing.assert_array_equal(next(x[1] for x in self.events if isinstance(x,tuple) and x[0]=='base_samples'),np.array([.1,-.2],dtype=np.float32))
  self.assertEqual(np.load(self.stage/'speaker-vector.npy',allow_pickle=False).shape,(2048,))
  with self.assertRaises(child.VoiceChildError):self.run_child()
 def test_no_external_audio_or_real_cli(self):
  with self.assertRaises(TypeError):self.run_child(audio_bytes=b'x')
  with self.assertRaises(child.VoiceChildError):child.run_real()
 def test_failure_leaves_unknown_and_no_outputs(self):
  def bad(path):
   if path==str(self.f.encoder.snapshot_root.resolve()):raise RuntimeError('fake OOM')
   return self.loader(path)
  with self.assertRaises(child.VoiceChildError):self.run_child(load_model=bad)
  self.assertTrue((self.stage/'unknown.json').exists());self.assertFalse((self.stage/'generated.wav').exists());self.assertFalse((self.stage/'speaker-vector.npy').exists())
  with self.assertRaises(child.VoiceChildError):self.run_child()
 def test_model_pin_rejects_before_started(self):
  (self.f.design.snapshot_root/'config.json').write_text('changed')
  with self.assertRaises(Exception):self.run_child()
  self.assertEqual(list(self.stage.iterdir()),[])

if __name__=='__main__':unittest.main()
