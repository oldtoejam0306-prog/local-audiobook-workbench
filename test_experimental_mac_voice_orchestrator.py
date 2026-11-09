import io,json,sys,types,unittest,wave
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
import numpy as np
import experimental_mac_voice_orchestrator as o
import test_local_voice_runner as fixture

class ExperimentTests(unittest.TestCase):
 def setUp(self):
  self.f=fixture.LocalVoiceRunnerTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.parent=self.f.root/'tasks';self.parent.mkdir();self.calls=[]
 def invoke(self,**kw):
  python=Path(sys.executable).resolve();args=dict(parent=self.parent,description='fictional',text='synthetic',seed=3,
   design_model=self.f.design,base_model=self.f.encoder,python_path=python,python_sha256=o._sha(python.read_bytes()),experiment_enabled=True,resource_limits={'min_pressure_free_percent':20,'min_available_bytes':1024,'max_child_rss_bytes':10000000,'poll_seconds':0.1})
  args.update(kw);return o.run_experiment(**args)
 def simulate(self,cmd,**kw):
  phase=cmd[cmd.index('--experiment-child')+1];stage=Path(kw['cwd']);config_hash=cmd[cmd.index('--experiment-child')+3];self.calls.append(phase)
  if phase=='design':
   b=io.BytesIO()
   with wave.open(b,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\0\0'*2400)
   raw=b.getvalue();(stage/'generated.wav').write_bytes(raw);result={'wav_sha256':o._sha(raw)}
  else:
   raw=kw['stdin'].read();self.assertEqual(o._sha(raw),cmd[-1]);b=io.BytesIO();np.save(b,np.zeros(2048,dtype=np.float32),allow_pickle=False)
   (stage/'speaker-vector.npy').write_bytes(b.getvalue());result={'vector_sha256':o._sha(b.getvalue()),'input_wav_sha256':cmd[-1]}
  result.update(contract_sha256=config_hash,sandbox_probes='denied');(stage/(phase+'-result.json')).write_text(json.dumps(result))
  return types.SimpleNamespace(returncode=0,pid=123,poll=lambda:0,wait=lambda **kw:0)
 def test_disabled_default_and_no_external_audio(self):
  with self.assertRaises(o.ExperimentError):self.invoke(experiment_enabled=False)
  with self.assertRaises(TypeError):self.invoke(audio_path='external.wav')
  self.assertEqual(list(self.parent.iterdir()),[])
 def test_fake_serial_subprocess_and_open_wav(self):
  real_is_file=Path.is_file
  def is_file(p):return True if p.as_posix().endswith('/usr/bin/sandbox-exec') else real_is_file(p)
  with patch.object(o.sys,'platform','darwin'),patch.object(Path,'is_file',is_file),patch.object(o,'_sandbox_profile',return_value='synthetic-profile'),patch.object(o,'_resource_lock',return_value=nullcontext()),patch.object(o,'_check_memory',return_value={'pressure_free_percent':80,'reclaimable_estimate_bytes':10000}),patch.object(o.subprocess,'Popen',side_effect=self.simulate):
   result=self.invoke()
  self.assertEqual(self.calls,['design','base']);self.assertFalse(result['production_eligible']);self.assertFalse(result['voice_registered'])
  self.assertEqual(json.loads((Path(result['stage'])/'journal.json').read_text(encoding='utf8'))['status'],'complete_experiment')
 def test_failed_design_leaves_unknown_no_base(self):
  real_is_file=Path.is_file
  def is_file(p):return True if p.as_posix().endswith('/usr/bin/sandbox-exec') else real_is_file(p)
  def fail(*args,**kwargs):return types.SimpleNamespace(returncode=1,pid=123,poll=lambda:1,wait=lambda **kw:1)
  with patch.object(o.sys,'platform','darwin'),patch.object(Path,'is_file',is_file),patch.object(o,'_sandbox_profile',return_value='synthetic-profile'),patch.object(o,'_resource_lock',return_value=nullcontext()),patch.object(o,'_check_memory',return_value={'pressure_free_percent':80,'reclaimable_estimate_bytes':10000}),patch.object(o.subprocess,'Popen',side_effect=fail):
   with self.assertRaises(o.ExperimentError):self.invoke()
  stage=next(self.parent.iterdir());self.assertEqual(json.loads((stage/'journal.json').read_text(encoding='utf8'))['status'],'unknown_no_retry')

 def test_low_memory_blocks_before_child_and_releases_lock(self):
  real_is_file=Path.is_file
  def is_file(p):return True if p.as_posix().endswith('/usr/bin/sandbox-exec') else real_is_file(p)
  held=[]
  class Lock:
   def __enter__(self):held.append('acquire')
   def __exit__(self,*args):held.append('release')
  with patch.object(o.sys,'platform','darwin'),patch.object(Path,'is_file',is_file),patch.object(o,'_sandbox_profile',return_value='synthetic-profile'),patch.object(o,'_resource_lock',return_value=Lock()),patch.object(o,'_check_memory',side_effect=o.ExperimentError('insufficient memory')),patch.object(o.subprocess,'Popen') as popen:
   with self.assertRaises(o.ExperimentError):self.invoke()
  self.assertEqual(held,['acquire','release']);popen.assert_not_called()
 def test_busy_lock_rejects_without_child(self):
  real_is_file=Path.is_file
  def is_file(p):return True if p.as_posix().endswith('/usr/bin/sandbox-exec') else real_is_file(p)
  with patch.object(o.sys,'platform','darwin'),patch.object(Path,'is_file',is_file),patch.object(o,'_resource_lock',side_effect=o.ExperimentError('busy')),patch.object(o.subprocess,'Popen') as popen:
   with self.assertRaisesRegex(o.ExperimentError,'busy'):self.invoke()
  popen.assert_not_called()
 def test_child_over_rss_is_terminated_and_reason_recorded(self):
  real_is_file=Path.is_file
  def is_file(p):return True if p.as_posix().endswith('/usr/bin/sandbox-exec') else real_is_file(p)
  class Process:
   pid=123;returncode=None;terminated=False
   def poll(self):return self.returncode
   def terminate(self):self.terminated=True;self.returncode=-15
   def wait(self,**kwargs):return self.returncode
   def kill(self):self.returncode=-9
  proc=Process()
  snap={'pressure_free_percent':80,'reclaimable_estimate_bytes':10000}
  with patch.object(o.sys,'platform','darwin'),patch.object(Path,'is_file',is_file),patch.object(o,'_sandbox_profile',return_value='synthetic-profile'),patch.object(o,'_resource_lock',return_value=nullcontext()),patch.object(o,'_check_memory',return_value=snap),patch.object(o,'_memory_snapshot',return_value=snap),patch.object(o,'_child_rss',return_value=10000001),patch.object(o.subprocess,'Popen',return_value=proc):
   with self.assertRaises(o.ExperimentError):self.invoke()
  self.assertTrue(proc.terminated);stage=next(self.parent.iterdir())
  process=json.loads((stage/'design-process.json').read_text(encoding='utf8'));self.assertEqual(process['reason'],'child_rss_exceeded')

 def test_timeout_terminates_only_child(self):
  real_is_file=Path.is_file
  def is_file(p):return True if p.as_posix().endswith('/usr/bin/sandbox-exec') else real_is_file(p)
  class Process:
   pid=123;returncode=None;terminated=False
   def poll(self):return self.returncode
   def terminate(self):self.terminated=True;self.returncode=-15
   def wait(self,**kwargs):return self.returncode
   def kill(self):self.returncode=-9
  proc=Process();snap={'pressure_free_percent':80,'reclaimable_estimate_bytes':10000}
  with patch.object(o.sys,'platform','darwin'),patch.object(Path,'is_file',is_file),patch.object(o,'_sandbox_profile',return_value='synthetic-profile'),patch.object(o,'_resource_lock',return_value=nullcontext()),patch.object(o,'_check_memory',return_value=snap),patch.object(o.time,'monotonic',side_effect=[0,2,3]),patch.object(o.subprocess,'Popen',return_value=proc):
   with self.assertRaises(o.ExperimentError):self.invoke(timeout_seconds=1)
  self.assertTrue(proc.terminated);stage=next(self.parent.iterdir());self.assertEqual(json.loads((stage/'design-process.json').read_text(encoding='utf8'))['reason'],'timeout')
 def test_resource_lock_releases_after_exception(self):
  events=[]
  fake=types.SimpleNamespace(LOCK_EX=1,LOCK_NB=2,LOCK_UN=4,flock=lambda fd,op:events.append(op))
  with patch.dict(sys.modules,{'fcntl':fake}):
   with self.assertRaises(RuntimeError):
    with o._resource_lock(self.parent):raise RuntimeError('synthetic')
  self.assertEqual(events,[3,4])

 def test_rss_monitor_race_accepts_already_exited_child(self):
  real_is_file=Path.is_file
  def is_file(p):return True if p.as_posix().endswith('/usr/bin/sandbox-exec') else real_is_file(p)
  active=[]
  class Process:
   pid=123;returncode=None
   def poll(self):return self.returncode
   def wait(self,**kwargs):return self.returncode
   def terminate(self):self.fail('must not terminate exited child')
  def launch(cmd,**kwargs):
   self.simulate(cmd,**kwargs);p=Process();active.append(p);return p
  def disappeared(_pid):active[-1].returncode=0;raise o.ExperimentError('process exited')
  snap={'pressure_free_percent':80,'reclaimable_estimate_bytes':10000}
  with patch.object(o.sys,'platform','darwin'),patch.object(Path,'is_file',is_file),patch.object(o,'_sandbox_profile',return_value='synthetic-profile'),patch.object(o,'_resource_lock',return_value=nullcontext()),patch.object(o,'_check_memory',return_value=snap),patch.object(o,'_child_rss',side_effect=disappeared),patch.object(o.subprocess,'Popen',side_effect=launch):
   result=self.invoke()
  self.assertEqual(result['status'],'complete_experiment');self.assertIsNone(result['design_process']['reason'])
 def test_keyboard_interrupt_terminates_waits_and_releases_lock(self):
  real_is_file=Path.is_file
  def is_file(p):return True if p.as_posix().endswith('/usr/bin/sandbox-exec') else real_is_file(p)
  events=[]
  class Lock:
   def __enter__(self):events.append('lock')
   def __exit__(self,*args):events.append('unlock')
  class Process:
   pid=123;returncode=None
   def poll(self):return self.returncode
   def terminate(self):events.append('terminate');self.returncode=-15
   def wait(self,**kwargs):events.append('wait');return self.returncode
  proc=Process();snap={'pressure_free_percent':80,'reclaimable_estimate_bytes':10000}
  with patch.object(o.sys,'platform','darwin'),patch.object(Path,'is_file',is_file),patch.object(o,'_sandbox_profile',return_value='synthetic-profile'),patch.object(o,'_resource_lock',return_value=Lock()),patch.object(o,'_check_memory',return_value=snap),patch.object(o,'_child_rss',side_effect=KeyboardInterrupt()),patch.object(o.subprocess,'Popen',return_value=proc):
   with self.assertRaises(o.ExperimentError):self.invoke()
  self.assertEqual(events,['lock','terminate','wait','unlock']);stage=next(self.parent.iterdir())
  self.assertEqual(json.loads((stage/'journal.json').read_text(encoding='utf8'))['status'],'unknown_no_retry')
 def test_sandbox_profile_rejects_all_ascii_control_characters(self):
  for number in list(range(32))+list(range(127,160)):
   with self.subTest(number=number),self.assertRaises(o.ExperimentError):o._sandbox_profile('bad'+chr(number)+'path')

if __name__=='__main__':unittest.main()
