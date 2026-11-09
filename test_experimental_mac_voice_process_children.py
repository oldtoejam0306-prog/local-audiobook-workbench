import io,types,unittest
import numpy as np
import experimental_mac_voice_process_children as c
import test_local_voice_runner as f

class Mx:
 random=types.SimpleNamespace(seed=lambda _:None)
 def eval(self,_):pass
 def array(self,x):return x
class ChildrenTests(unittest.TestCase):
 def setUp(self):
  self.f=f.LocalVoiceRunnerTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.stage=self.f.root/'stage';self.stage.mkdir();self.mx=Mx();self.received=[]
 def loader(self,path):
  if path==str(self.f.design.snapshot_root.resolve()):
   class Design:
    def generate_voice_design(self,**kwargs):yield types.SimpleNamespace(audio=np.array([.1,-.2],dtype=np.float32),sample_rate=24000)
   return Design()
  received=self.received
  class Base:
   def extract_speaker_embedding(self,samples):received.append(samples.copy());return np.ones(2048,dtype=np.float32)
  return Base()
 def design(self):return c._design(stage=self.stage,description='fictional',text='synthetic',seed=3,model=self.f.design,np=np,mx=self.mx,load_model=self.loader)
 def base(self,stream,digest,**kw):return c._base_from_stdin(stage=self.stage,stdin=stream,expected_wav_sha256=digest,model=self.f.encoder,np=np,mx=self.mx,load_model=self.loader,**kw)
 def test_design_and_parent_stream_produce_2048_vector(self):
  result=self.design();raw=(self.stage/'generated.wav').read_bytes();out=self.base(io.BytesIO(raw),result['wav_sha256'])
  self.assertEqual(out['input_wav_sha256'],result['wav_sha256']);self.assertEqual(np.load(self.stage/'speaker-vector.npy',allow_pickle=False).shape,(2048,));self.assertEqual(len(self.received),1)
 def test_bad_or_external_path_and_hash_rejected(self):
  result=self.design();raw=(self.stage/'generated.wav').read_bytes()
  with self.assertRaises(c.VoiceChildError):self.base(io.BytesIO(raw+b'x'),result['wav_sha256'])
  with self.assertRaises(TypeError):self.base(io.BytesIO(raw),result['wav_sha256'],audio_path='outside.wav')
  with self.assertRaises(c.VoiceChildError):c.real_child_cli()
  self.assertFalse((self.stage/'speaker-vector.npy').exists())
 def test_base_failure_does_not_create_vector(self):
  result=self.design();raw=(self.stage/'generated.wav').read_bytes()
  def failing(_):raise RuntimeError('synthetic load failure')
  with self.assertRaises(RuntimeError):c._base_from_stdin(stage=self.stage,stdin=io.BytesIO(raw),expected_wav_sha256=result['wav_sha256'],model=self.f.encoder,np=np,mx=self.mx,load_model=failing)
  self.assertFalse((self.stage/'speaker-vector.npy').exists())
 def test_changed_model_pin_is_rejected(self):
  (self.f.design.snapshot_root/'config.json').write_text('changed')
  with self.assertRaises(Exception):self.design()
  self.assertFalse((self.stage/'generated.wav').exists())

if __name__=='__main__':unittest.main()
