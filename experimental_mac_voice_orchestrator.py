"""Mac isolated text voice experiment. Not connected to production.

Real execution requires explicit experiment configuration, local pins and a
macOS sandbox. It never accepts caller audio or arbitrary launch callbacks.
Model hashes are local inventory pins, not vendor authentication. Serial child
processes reduce overlap but do not prove M2 memory safety or audio quality.
"""
import hashlib,json,os,socket,subprocess,sys,tempfile,time,unicodedata
from contextlib import contextmanager
from pathlib import Path
import local_voice_runner as contract
from isolated_voice_design import _validate_wav,_validate_vector

class ExperimentError(RuntimeError):pass

def _sha(raw):return hashlib.sha256(raw).hexdigest()
def _json(obj):return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def _atomic(path,obj):
 fd,name=tempfile.mkstemp(prefix='.state-',dir=path.parent)
 try:
  with os.fdopen(fd,'wb') as f:f.write(_json(obj));f.flush();os.fsync(f.fileno())
  os.replace(name,path)
  if os.name!='nt':
   d=os.open(path.parent,os.O_RDONLY)
   try:os.fsync(d)
   finally:os.close(d)
 finally:
  if os.path.exists(name):os.unlink(name)

def _model_dict(model):
 return {'role':model.role,'model_id':model.model_id,'repository_root':str(model.repository_root),
         'snapshot_root':str(model.snapshot_root),'inventory_sha256':model.inventory_sha256}
def _model_from(obj):
 if not isinstance(obj,dict) or set(obj)!={'role','model_id','repository_root','snapshot_root','inventory_sha256'}:raise ExperimentError('bad model configuration')
 return contract.LocalModel(obj['role'],obj['model_id'],Path(obj['repository_root']),Path(obj['snapshot_root']),obj['inventory_sha256'])
def _sandbox_profile(stage):
 path=str(stage)
 if '"' in path or '\\' in path or any(unicodedata.category(c)=='Cc' for c in path):raise ExperimentError('unsupported stage path')
 return '(version 1)(allow default)(deny network*)(deny file-write*)(allow file-write* (subpath "'+path+'"))'

@contextmanager
def _resource_lock(parent):
 """Nonblocking advisory lock for this worker family, not all machine workloads."""
 import fcntl
 path=parent/'.text-voice-experiment.lock'
 with path.open('a+b') as handle:
  try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:raise ExperimentError('voice experiment resource busy') from None
  try:yield
  finally:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

def _memory_snapshot():
 pressure=subprocess.run(['/usr/bin/memory_pressure'],capture_output=True,text=True,timeout=10,check=True).stdout
 line=next(x for x in pressure.splitlines() if 'System-wide memory free percentage:' in x)
 percent=int(line.split(':',1)[1].strip().rstrip('%'))
 vm=subprocess.run(['/usr/bin/vm_stat'],capture_output=True,text=True,timeout=10,check=True).stdout
 import re
 page=int(re.search(r'page size of (\d+) bytes',vm).group(1))
 def pages(label):return int(re.search(r'^'+re.escape(label)+r':\s*(\d+)\.',vm,re.M).group(1))
 available=page*(pages('Pages free')+pages('Pages inactive')+pages('Pages speculative'))
 return {'pressure_free_percent':percent,'reclaimable_estimate_bytes':available}

def _child_rss(pid):
 r=subprocess.run(['/bin/ps','-o','rss=','-p',str(pid)],capture_output=True,text=True,timeout=5)
 if r.returncode or not r.stdout.strip().isdigit():raise ExperimentError('child RSS monitor failed')
 return int(r.stdout.strip())*1024

def _check_memory(limits):
 snapshot=_memory_snapshot()
 if (snapshot['pressure_free_percent']<limits['min_pressure_free_percent']
     or snapshot['reclaimable_estimate_bytes']<limits['min_available_bytes']):
  raise ExperimentError('insufficient memory before phase')
 return snapshot

def _validate_limits(limits):
 keys={'min_pressure_free_percent','min_available_bytes','max_child_rss_bytes','poll_seconds'}
 if not isinstance(limits,dict) or set(limits)!=keys:raise ExperimentError('explicit resource limits required')
 if (type(limits['min_pressure_free_percent']) is not int or not 1<=limits['min_pressure_free_percent']<=100
     or type(limits['min_available_bytes']) is not int or limits['min_available_bytes']<=0
     or type(limits['max_child_rss_bytes']) is not int or limits['max_child_rss_bytes']<=0
     or type(limits['poll_seconds']) not in (int,float) or not 0.1<=limits['poll_seconds']<=10):
  raise ExperimentError('resource limits invalid')
 return dict(limits)

def run_experiment(*, parent,description,text,seed,design_model,base_model,python_path,python_sha256,
                   experiment_enabled=False,timeout_seconds=300,resource_limits=None):
 """Explicit standalone experiment; no audio input, callback, or production registration."""
 if experiment_enabled is not True:raise ExperimentError('real experiment disabled by default')
 if sys.platform!='darwin' or not Path('/usr/bin/sandbox-exec').is_file():raise ExperimentError('macOS sandbox-exec required')
 root=Path(parent);python=Path(python_path)
 if (not root.is_absolute() or not root.is_dir() or root.is_symlink() or not python.is_absolute()
     or not python.is_file() or not isinstance(python_sha256,str) or _sha(python.read_bytes())!=python_sha256):
  raise ExperimentError('experiment parent or interpreter pin invalid')
 if type(timeout_seconds) is not int or not 1<=timeout_seconds<=1800:raise ExperimentError('timeout invalid')
 limits=_validate_limits(resource_limits)
 with _resource_lock(root):
  return _run_locked(root,description,text,seed,design_model,base_model,python,python_sha256,timeout_seconds,limits)

def _run_locked(root,description,text,seed,design_model,base_model,python,python_sha256,timeout_seconds,limits):
 request=contract._request(description,text,seed)
 di=contract._inspect_model(design_model,'voice_design');bi=contract._inspect_model(base_model,'speaker_encoder')
 stage=Path(tempfile.mkdtemp(prefix='voice-experiment-',dir=root)).resolve();profile=_sandbox_profile(stage)
 module_paths=[Path(__file__).resolve(),Path(contract.__file__).resolve()]
 import experimental_mac_voice_process_children as children
 import local_model_inventory,isolated_voice_design
 module_paths += [Path(children.__file__).resolve(),Path(local_model_inventory.__file__).resolve(),Path(isolated_voice_design.__file__).resolve()]
 pins={str(p):_sha(p.read_bytes()) for p in module_paths}
 config={'request':request,'design':_model_dict(design_model),'base':_model_dict(base_model),
         'python_sha256':python_sha256,'script_pins':pins,'resource_limits':limits,'synthetic_or_experimental':True}
 _atomic(stage/'contract.json',config);config_hash=_sha((stage/'contract.json').read_bytes())
 env={'HOME':str(stage),'TMPDIR':str(stage),'HF_HOME':str(stage/'hf'),'HF_HUB_OFFLINE':'1',
      'TRANSFORMERS_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1','PYTHONDONTWRITEBYTECODE':'1',
      'PATH':str(python.parent)+':/usr/bin:/bin:/usr/sbin:/sbin'}
 def phase(name,stdin=None,wav_hash=None):
  _atomic(stage/'journal.json',{'status':name+'_running','contract_sha256':config_hash})
  cmd=['/usr/bin/sandbox-exec','-p',profile,str(python),'-B',str(Path(__file__).resolve()),'--experiment-child',name,str(stage),config_hash]
  if wav_hash is not None:cmd.append(wav_hash)
  snapshot=_check_memory(limits)
  start=time.monotonic();reason=None;peak=0
  with (stage/(name+'.stdout')).open('xb') as out,(stage/(name+'.stderr')).open('xb') as err:
   p=subprocess.Popen(cmd,stdin=stdin,stdout=out,stderr=err,cwd=stage,env=env)
   try:
    while p.poll() is None:
     if time.monotonic()-start>timeout_seconds:reason='timeout';break
     try:
      peak=max(peak,_child_rss(p.pid))
      live=_memory_snapshot()
     except Exception:
      if p.poll() is not None:break
      reason='monitor_failure';break
     if peak>limits['max_child_rss_bytes']:reason='child_rss_exceeded';break
     if live['pressure_free_percent']<limits['min_pressure_free_percent']:reason='memory_pressure';break
     time.sleep(limits['poll_seconds'])
    if reason and p.poll() is None:
     p.terminate()
     try:p.wait(timeout=5)
     except subprocess.TimeoutExpired:p.kill();p.wait()
    else:p.wait()
   except BaseException:
    if p.poll() is None:
     try:p.terminate()
     except ProcessLookupError:pass
    try:p.wait(timeout=5)
    except subprocess.TimeoutExpired:
     try:p.kill()
     except ProcessLookupError:pass
     p.wait()
    raise
   finally:
    out.flush();os.fsync(out.fileno());err.flush();os.fsync(err.fileno())
  record={'exit_code':p.returncode,'elapsed_seconds':round(time.monotonic()-start,3),
          'sampled_peak_rss_bytes':peak,'reason':reason,'preflight':snapshot,'thresholds':limits}
  _atomic(stage/(name+'-process.json'),record)
  if reason or p.returncode:raise ExperimentError(name+' child stopped: '+str(reason or p.returncode))
  result=json.loads((stage/(name+'-result.json')).read_text())
  if result.get('contract_sha256')!=config_hash or result.get('sandbox_probes')!='denied':raise ExperimentError('child evidence mismatch')
  return result,record
 try:
  design,dproc=phase('design');wav_path=stage/'generated.wav'
  if wav_path.is_symlink() or not wav_path.is_file() or wav_path.stat().st_nlink!=1:raise ExperimentError('WAV invalid')
  with wav_path.open('rb') as wav:
   raw=wav.read();_validate_wav(raw)
   if _sha(raw)!=design.get('wav_sha256'):raise ExperimentError('WAV hash mismatch')
   _atomic(stage/'journal.json',{'status':'design_complete','contract_sha256':config_hash,'wav_sha256':_sha(raw)})
   wav.seek(0);base,bproc=phase('base',wav,_sha(raw));wav.seek(0)
   if _sha(wav.read())!=_sha(raw) or base.get('input_wav_sha256')!=_sha(raw):raise ExperimentError('WAV changed')
  vector=stage/'speaker-vector.npy'
  if vector.is_symlink() or not vector.is_file() or vector.stat().st_nlink!=1:raise ExperimentError('vector invalid')
  v=vector.read_bytes();_validate_vector(v)
  if _sha(v)!=base.get('vector_sha256'):raise ExperimentError('vector hash mismatch')
  if contract._inspect_model(design_model,'voice_design')!=di or contract._inspect_model(base_model,'speaker_encoder')!=bi:raise ExperimentError('model inventory changed')
  result={'status':'complete_experiment','contract_sha256':config_hash,'wav_sha256':_sha(raw),'vector_sha256':_sha(v),
          'design':design,'base':base,'design_process':dproc,'base_process':bproc,'production_eligible':False,'voice_registered':False,'stage':str(stage)}
  _atomic(stage/'journal.json',result);return result
 except BaseException as exc:
  try:_atomic(stage/'journal.json',{'status':'unknown_no_retry','contract_sha256':config_hash,'reason':str(exc),'thresholds':limits})
  except OSError:pass
  raise ExperimentError('experiment failed or interrupted; do not retry automatically') from exc

def _child(phase,stage,expected_contract,wav_hash=None):
 stage=Path(stage)
 raw=(stage/'contract.json').read_bytes()
 if _sha(raw)!=expected_contract:raise ExperimentError('contract hash mismatch')
 config=json.loads(raw)
 for path,digest in config['script_pins'].items():
  if _sha(Path(path).read_bytes())!=digest:raise ExperimentError('script pin mismatch')
 sock=socket.socket();sock.settimeout(1)
 try:
  try:sock.connect(('127.0.0.1',9))
  except PermissionError:pass
  else:raise ExperimentError('network denial not established')
 finally:sock.close()
 try:fd=os.open('/dev/null',os.O_WRONLY)
 except PermissionError:pass
 else:os.close(fd);raise ExperimentError('outside write denial not established')
 import resource
 import numpy as np,mlx.core as mx
 from mlx_audio.tts.utils import load_model
 import experimental_mac_voice_process_children as children
 start=time.monotonic();req=config['request']
 if phase=='design':
  result=children._design(stage=stage,description=req['description'],text=req['sample_text'],seed=req['seed'],model=_model_from(config['design']),np=np,mx=mx,load_model=load_model)
 elif phase=='base' and wav_hash is not None:
  result=children._base_from_stdin(stage=stage,stdin=sys.stdin.buffer,expected_wav_sha256=wav_hash,model=_model_from(config['base']),np=np,mx=mx,load_model=load_model)
 else:raise ExperimentError('unknown child operation')
 result.update(contract_sha256=expected_contract,sandbox_probes='denied',seconds=round(time.monotonic()-start,3),peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
 _atomic(stage/(phase+'-result.json'),result)

if __name__=='__main__':
 if len(sys.argv) not in (5,6) or sys.argv[1]!='--experiment-child':raise SystemExit('disabled')
 try:_child(sys.argv[2],sys.argv[3],sys.argv[4],sys.argv[5] if len(sys.argv)==6 else None)
 except Exception as exc:print(type(exc).__name__+': '+str(exc),file=sys.stderr);raise SystemExit(2)

