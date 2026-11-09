import io, tempfile, unittest, wave, hashlib, subprocess, sys, json
from pathlib import Path
import numpy as np
from isolated_voice_design import IsolatedVoiceBook

class FakeRunner:
 offline_test_only=True
 def __init__(self):self.design_calls=0;self.encode_calls=0;self.wav=None
 def design(self, request):
  self.design_calls+=1;assert request['reference_audio'] is False
  buf=io.BytesIO()
  with wave.open(buf,'wb') as out:out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000);out.writeframes(b'\0\0'*64)
  self.wav=buf.getvalue();return self.wav
 def encode(self,wav,model):
  assert wav==self.wav;self.encode_calls+=1;buf=io.BytesIO();np.save(buf,np.zeros(2048,dtype=np.float32),allow_pickle=False);return buf.getvalue()

class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.book=IsolatedVoiceBook.create(self.root,'book',safe_root=self.root);self.runner=FakeRunner()
  self.design_file=self.root/'design.model';self.design_file.write_bytes(b'design');self.encoder_file=self.root/'encoder.model';self.encoder_file.write_bytes(b'encoder')
  self.args={'character_id':'person_a','verified_voice_locks':{},'voice_id':'character_a','description':'虚构人物，清晰中性','sample_text':'这是合成文字。','seed':42,'design_model':{'id':'local-voice-design','sha256':hashlib.sha256(b'design').hexdigest(),'path':str(self.design_file)},'encoder_model':{'id':'local-base','sha256':hashlib.sha256(b'encoder').hexdigest(),'path':str(self.encoder_file)},'runner':self.runner}
 def tearDown(self):self.temp.cleanup()
 def test_design_then_resume_verifies_all_and_does_not_rerun(self):
  first=self.book.design(**self.args);self.assertFalse(first['reused']);second=self.book.design(**self.args);self.assertTrue(second['reused']);self.assertEqual((self.runner.design_calls,self.runner.encode_calls),(1,1))
  self.assertEqual(first['voice_ref'],'library:zh/character_a');self.assertEqual(first['manifest']['request']['seed'],42)
  self.assertTrue((self.book.root/'zh/character_a.json').is_file())
 def test_all_saved_artifacts_and_request_rechecked(self):
  self.book.design(**self.args)
  for rel in ('artifacts/character_a.wav','zh/character_a.npy','zh/character_a.json','artifacts/character_a.manifest.json'):
   path=self.book.root/rel;data=path.read_bytes();path.write_bytes(data+b'x')
   with self.subTest(rel=rel), self.assertRaises(ValueError):self.book.design(**self.args)
   path.write_bytes(data)
  for key,value in [('seed',99),('description','不同虚构人物'),('encoder_model',{'id':'local-base','sha256':'c'*64,'path':str(self.encoder_file)})]:
   with self.subTest(key=key),self.assertRaises(ValueError):self.book.design(**{**self.args,key:value})
  self.assertEqual(self.runner.design_calls,1)
 def test_locked_or_incomplete_never_overwritten(self):
  with self.assertRaises(ValueError):self.book.design(**{**self.args,'verified_voice_locks':{'person_a':{'voice_id':'old_voice','locked_by':'user','status':'fixed'}}})
  (self.book.root/'artifacts/character_a.wav').write_bytes(b'partial')
  with self.assertRaises(ValueError):self.book.design(**self.args)
  self.assertEqual(self.runner.design_calls,0)
 def test_no_reference_argument_or_existing_directory_adoption(self):
  with self.assertRaises(TypeError):self.book.design(**self.args,reference_wav=b'real')
  with self.assertRaises(TypeError):IsolatedVoiceBook(self.root,'book')
  with self.assertRaises(ValueError):self.book.design(**{**self.args,'design_model':{'id':'https://example.com','sha256':'a'*64,'path':str(self.design_file)}})
 def test_invalid_runner_output_never_written(self):
  self.runner.design=lambda request:b'not wav'
  with self.assertRaises(ValueError):self.book.design(**self.args)
  self.assertFalse(any((self.book.root/'zh').iterdir()))
  self.assertTrue((self.book.root/'artifacts/character_a.state.json').exists())
  with self.assertRaises(ValueError):self.book.design(**self.args)

 def test_cross_process_recovery_and_model_tamper(self):
  first=self.book.design(**self.args);marker=self.book.marker;request=first['manifest']['request_sha256']
  command="""import sys,json
from isolated_voice_design import IsolatedVoiceBook
payload=json.load(sys.stdin)
b=IsolatedVoiceBook.reopen(payload['root'],'book',safe_root=payload['safe'],marker=payload['marker'],expected_request_sha256=payload['request'])
a=payload['args'];a['runner']=None
print('REUSED',b.design(**a)['reused'])
"""
  args={k:v for k,v in self.args.items() if k!='runner'}
  result=subprocess.run([sys.executable,'-c',command],input=json.dumps({'root':str(self.book.root),'safe':str(self.root),'marker':marker,'request':request,'args':args},ensure_ascii=False),capture_output=True,text=True)
  self.assertEqual(result.returncode,0,result.stderr);self.assertIn('REUSED True',result.stdout)
  self.design_file.write_bytes(b'tampered')
  with self.assertRaises(ValueError):IsolatedVoiceBook.reopen(self.book.root,'book',safe_root=self.root,marker=marker,expected_request_sha256=request).design(**self.args)
  with self.assertRaises(ValueError):IsolatedVoiceBook.reopen(self.book.root,'book',safe_root=self.root,marker='0'*64,expected_request_sha256=request)
 def test_interrupted_state_never_retries(self):
  def interrupted(request):raise RuntimeError('synthetic interruption')
  self.runner.design=interrupted
  with self.assertRaises(RuntimeError):self.book.design(**self.args)
  state=json.loads((self.book.root/'artifacts/character_a.state.json').read_text())
  self.assertEqual(state['state'],'in_progress')
  recovered=IsolatedVoiceBook.reopen(self.book.root,'book',safe_root=self.root,marker=self.book.marker,expected_request_sha256=state['request_sha256'])
  with self.assertRaisesRegex(ValueError,'incomplete'):recovered.design(**self.args)
 def test_concurrent_process_lock_serializes_recovery(self):
  first=self.book.design(**self.args);request=first['manifest']['request_sha256']
  command="""import sys,json
from isolated_voice_design import IsolatedVoiceBook
payload=json.load(sys.stdin)
b=IsolatedVoiceBook.reopen(payload['root'],'book',safe_root=payload['safe'],marker=payload['marker'],expected_request_sha256=payload['request'])
a=payload['args'];a['runner']=None
assert b.design(**a)['reused']
"""
  args={k:v for k,v in self.args.items() if k!='runner'}
  cmd=[sys.executable,'-c',command]
  payload=json.dumps({'root':str(self.book.root),'safe':str(self.root),'marker':self.book.marker,'request':request,'args':args},ensure_ascii=False)
  processes=[subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(3)]
  for process in processes:
   out,err=process.communicate(input=payload,timeout=20);self.assertEqual(process.returncode,0,err)
  self.assertEqual(self.runner.design_calls,1)
 def test_explicit_role_and_voice_locks_reject_conflicts(self):
  with self.assertRaises(TypeError):self.book.design(**{k:v for k,v in self.args.items() if k!='verified_voice_locks'})
  lock={'person_b':{'voice_id':'character_a','locked_by':'user','status':'fixed'}}
  with self.assertRaises(ValueError):self.book.design(**{**self.args,'verified_voice_locks':lock})
  duplicate={'person_b':{'voice_id':'v1','locked_by':'user','status':'fixed'},'person_c':{'voice_id':'v1','locked_by':'user','status':'fixed'}}
  with self.assertRaises(ValueError):self.book.design(**{**self.args,'verified_voice_locks':duplicate})
 def test_lock_file_replacement_is_rejected(self):
  lock=self.book.root/'lock';lock.rename(self.book.root/'old-lock');lock.write_bytes(b'0')
  with self.assertRaises(ValueError):self.book.design(**self.args)
 def test_real_lock_contention(self):
  import time
  result=self.book.design(**self.args);request=result['manifest']['request_sha256']
  command="""import json,sys
from isolated_voice_design import _lock
from pathlib import Path
p=Path(json.loads(sys.stdin.readline())['path']);st=p.stat()
with _lock(p,(st.st_dev,st.st_ino)):
 print('LOCKED',flush=True);sys.stdin.readline()
"""
  holder=subprocess.Popen([sys.executable,'-c',command],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  holder.stdin.write(json.dumps({'path':str(self.book.root/'lock')})+'\n');holder.stdin.flush();self.assertEqual(holder.stdout.readline().strip(),'LOCKED')
  worker="""import sys,json
from isolated_voice_design import IsolatedVoiceBook
p=json.load(sys.stdin);b=IsolatedVoiceBook.reopen(p['root'],'book',safe_root=p['safe'],marker=p['marker'],expected_request_sha256=p['request']);a=p['args'];a['runner']=None
assert b.design(**a)['reused']
"""
  payload={'root':str(self.book.root),'safe':str(self.root),'marker':self.book.marker,'request':request,'args':{k:v for k,v in self.args.items() if k!='runner'}}
  proc=subprocess.Popen([sys.executable,'-c',worker],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  proc.stdin.write(json.dumps(payload,ensure_ascii=False));proc.stdin.close();time.sleep(1);self.assertIsNone(proc.poll(),'recovery did not wait for held lock')
  holder.stdin.write('release\n');holder.stdin.flush();holder.wait(timeout=10)
  proc.wait(timeout=20);self.assertEqual(proc.returncode,0,proc.stderr.read())
  holder.stdout.close();holder.stderr.close();holder.stdin.close();proc.stdout.close();proc.stderr.close()
 def test_process_kill_leaves_in_progress_and_cannot_replay(self):
  worker="""import json,sys
from isolated_voice_design import IsolatedVoiceBook
p=json.loads(sys.stdin.readline());b=IsolatedVoiceBook.create(p['safe'],'book',safe_root=p['safe'])
class Runner:
 offline_test_only=True
 def design(self,request):
  state=json.loads((b.root/'artifacts/character_a.state.json').read_text())
  print(json.dumps({'root':str(b.root),'marker':b.marker,'request':state['request_sha256']}),flush=True)
  sys.stdin.readline();raise RuntimeError('unexpected')
 def encode(self,wav,model):raise RuntimeError('unexpected')
p['args']['runner']=Runner();b.design(**p['args'])
"""
  args={k:v for k,v in self.args.items() if k!='runner'}
  proc=subprocess.Popen([sys.executable,'-c',worker],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  proc.stdin.write(json.dumps({'safe':str(self.root),'args':args},ensure_ascii=False)+'\n');proc.stdin.flush()
  ready=json.loads(proc.stdout.readline());proc.kill();proc.wait(timeout=10)
  recovered=IsolatedVoiceBook.reopen(ready['root'],'book',safe_root=self.root,marker=ready['marker'],expected_request_sha256=ready['request'])
  with self.assertRaisesRegex(ValueError,'incomplete'):recovered.design(**self.args)
  proc.stdout.close();proc.stderr.close();proc.stdin.close()
 def test_real_runner_is_disabled(self):
  self.runner.offline_test_only=False
  with self.assertRaisesRegex(ValueError,'real runners disabled'):self.book.design(**self.args)

if __name__=='__main__':unittest.main()
