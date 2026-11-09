"""Publish one completed text-voice experiment to a new isolated library.

Only task-produced artifacts are accepted. Caller authenticates the task,
model pins and explicit user-lock snapshot; self-computed hashes are not proof
of user consent or model origin. This is not a production voice registry.
"""
import hashlib,json,os,re,secrets,tempfile
from pathlib import Path
import local_voice_runner as contract
from isolated_voice_design import _validate_wav,_validate_vector
import release_config

class LibraryExperimentError(ValueError):pass

def _sha(raw):return hashlib.sha256(raw).hexdigest()
def _canonical(obj):return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def _read_file(path):
 if path.is_symlink() or not path.is_file() or path.stat().st_nlink!=1:raise LibraryExperimentError('expected a single-link regular task file')
 return path.read_bytes()
def _object(raw):
 def pairs(items):
  d={}
  for k,v in items:
   if k in d:raise ValueError('duplicate key')
   d[k]=v
  return d
 try:
  value=json.loads(raw.decode('utf8'),object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('invalid number')))
  if not isinstance(value,dict):raise ValueError('not object')
  return value
 except (UnicodeError,ValueError) as exc:raise LibraryExperimentError('invalid task JSON') from exc
def _model(value,role):
 keys={'role','model_id','repository_root','snapshot_root','inventory_sha256'}
 if not isinstance(value,dict) or set(value)!=keys or value['role']!=role:raise LibraryExperimentError('model contract malformed')
 return contract.LocalModel(value['role'],value['model_id'],Path(value['repository_root']),Path(value['snapshot_root']),value['inventory_sha256'])
def _write_new(path,raw):
 fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 try:
  with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
 except BaseException:
  try:path.unlink()
  except OSError:pass
  raise

def publish_experiment(*,task_dir,experiment_parent,book_id,character_id,locked_snapshot,locked_snapshot_sha256,
                       trusted_presets_path,trusted_presets_sha256):
 """Copy a verified task vector into a newly-created experiment-only directory."""
 task=Path(task_dir);parent=Path(experiment_parent)
 if (not task.is_absolute() or not parent.is_absolute() or task.is_symlink() or parent.is_symlink()
     or not task.is_dir() or not parent.is_dir()):
  raise LibraryExperimentError('existing configured experiment paths required')
 task=task.resolve(strict=True);parent=parent.resolve(strict=True)
 if parent!=release_config.mac_stage_root().resolve(strict=True):
  raise LibraryExperimentError('experiment parent differs from configured staging root')
 if not task.is_relative_to(parent) or task==parent:raise LibraryExperimentError('task must be within isolated experiment parent')
 if any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',v) for v in (book_id,character_id)):
  raise LibraryExperimentError('book and character identifiers invalid')
 if (not isinstance(locked_snapshot,dict) or set(locked_snapshot)!={'version','book_id','locked'}
     or type(locked_snapshot['version']) is not int or locked_snapshot['version']!=1
     or locked_snapshot['book_id']!=book_id or not isinstance(locked_snapshot['locked'],dict)
     or any(not isinstance(k,str) or not isinstance(v,str) for k,v in locked_snapshot['locked'].items())
     or _sha(_canonical(locked_snapshot))!=locked_snapshot_sha256):
  raise LibraryExperimentError('explicit user-lock snapshot missing or mismatched')
 if character_id in locked_snapshot['locked']:raise LibraryExperimentError('character has a locked voice')
 journal=_object(_read_file(task/'journal.json'));config_raw=_read_file(task/'contract.json');config=_object(config_raw)
 if (journal.get('status')!='complete_experiment' or journal.get('production_eligible') is not False
     or journal.get('voice_registered') is not False or journal.get('contract_sha256')!=_sha(config_raw)
     or journal.get('stage')!=str(task)):
  raise LibraryExperimentError('task not complete or contract mismatch')
 request=config.get('request')
 if not isinstance(request,dict) or set(request)!={'description','sample_text','seed','fictional_voice_only','reference_audio'}:
  raise LibraryExperimentError('task input contract invalid')
 contract._request(request['description'],request['sample_text'],request['seed'])
 if request['fictional_voice_only'] is not True or request['reference_audio'] is not False:
  raise LibraryExperimentError('task is not text-only')
 for role,key in (('voice_design','design'),('speaker_encoder','base')):
  model=_model(config.get(key),role)
  try:contract._inspect_model(model,role)
  except Exception as exc:raise LibraryExperimentError('model inventory mismatch') from exc
  if not Path(model.snapshot_root).is_absolute():raise LibraryExperimentError('model path must be absolute')
 for phase in ('design','base'):
  result=_object(_read_file(task/(phase+'-result.json')));process=_object(_read_file(task/(phase+'-process.json')))
  if (result!=journal.get(phase) or process!=journal.get(phase+'_process') or process.get('exit_code')!=0
      or result.get('contract_sha256')!=journal['contract_sha256'] or result.get('sandbox_probes')!='denied'):
   raise LibraryExperimentError('child result evidence mismatch')
 wav=_read_file(task/'generated.wav');vector=_read_file(task/'speaker-vector.npy')
 try:_validate_wav(wav);_validate_vector(vector)
 except Exception as exc:raise LibraryExperimentError('invalid task artifacts') from exc
 if (journal.get('wav_sha256')!=_sha(wav) or journal.get('vector_sha256')!=_sha(vector)
     or journal['design'].get('wav_sha256')!=_sha(wav) or journal['base'].get('input_wav_sha256')!=_sha(wav)
     or journal['base'].get('vector_sha256')!=_sha(vector)
     or journal['design'].get('inventory_sha256')!=config['design']['inventory_sha256']
     or journal['base'].get('inventory_sha256')!=config['base']['inventory_sha256']):
  raise LibraryExperimentError('artifact or model binding mismatch')
 presets_path=Path(trusted_presets_path)
 if not presets_path.is_absolute():raise LibraryExperimentError('absolute preset source required')
 presets_raw=_read_file(presets_path)
 if _sha(presets_raw)!=trusted_presets_sha256:raise LibraryExperimentError('preset source pin mismatch')
 presets=_object(presets_raw)
 if (not presets or any(not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',name)
     or not isinstance(info,dict) or any(not isinstance(info.get(k),str) or not info[k].strip() for k in ('language','gender','age'))
     for name,info in presets.items()) or not any(info['language']=='zh' for info in presets.values())):
  raise LibraryExperimentError('preset metadata invalid')
 # Reserve an exclusive fresh library directory. Never edit the supplied lock snapshot.
 library=Path(tempfile.mkdtemp(prefix='voice-library-experiment-',dir=parent))
 metadata={'status':'experimental_automatic_candidate','book_id':book_id,'character_id':character_id,
  'task_contract_sha256':journal['contract_sha256'],'wav_sha256':_sha(wav),'vector_sha256':_sha(vector),
  'design_inventory_sha256':config['design']['inventory_sha256'],'base_inventory_sha256':config['base']['inventory_sha256'],
  'locked_snapshot_sha256':locked_snapshot_sha256,'human_reviewed':False,'production_eligible':False,
  'scope':'isolated_local_huashuo_experiment_only','voice_registered':False}
 voice_id='candidate_'+secrets.token_hex(12)
 zh=library/'zh';zh.mkdir(mode=0o700)
 voice_metadata={'id':voice_id,'gender':'unknown','age':'unknown',**metadata}
 metadata_raw=_canonical(voice_metadata)
 _write_new(library/'presets.json',presets_raw)
 _write_new(zh/(voice_id+'.npy'),vector)
 _write_new(zh/(voice_id+'.json'),metadata_raw)
 receipt={'voice_id':voice_id,'book_id':book_id,'vector_sha256':_sha(vector),
          'metadata_sha256':_sha(metadata_raw),'presets_sha256':_sha(presets_raw),
          'locked_snapshot_sha256':locked_snapshot_sha256}
 receipt_raw=_canonical(receipt)
 _write_new(library/'complete.json',receipt_raw)
 if os.name!='nt':
  for path in (zh,library,parent):
   fd=os.open(path,os.O_RDONLY)
   try:os.fsync(fd)
   finally:os.close(fd)
 return {'library_dir':str(library),'voice_ref':'library:zh/'+voice_id,
         'metadata':voice_metadata,'receipt_sha256':_sha(receipt_raw)}

def verify_experiment_library(*,library_dir,expected_receipt_sha256,trusted_presets_sha256):
 """Detect changed assets using an independently retained receipt digest."""
 root=Path(library_dir)
 if not root.is_absolute() or root.is_symlink() or not root.is_dir():raise LibraryExperimentError('invalid experiment library')
 receipt_raw=_read_file(root/'complete.json')
 if _sha(receipt_raw)!=expected_receipt_sha256:raise LibraryExperimentError('receipt hash mismatch')
 receipt=_object(receipt_raw);voice_id=receipt.get('voice_id')
 if not isinstance(voice_id,str) or not re.fullmatch(r'candidate_[0-9a-f]{24}',voice_id):raise LibraryExperimentError('invalid candidate id')
 zh=root/'zh'
 if zh.is_symlink() or not zh.is_dir():raise LibraryExperimentError('invalid language directory')
 presets=_read_file(root/'presets.json');metadata_raw=_read_file(zh/(voice_id+'.json'));vector=_read_file(zh/(voice_id+'.npy'))
 if (_sha(presets)!=trusted_presets_sha256 or receipt.get('presets_sha256')!=trusted_presets_sha256
     or _sha(metadata_raw)!=receipt.get('metadata_sha256') or _sha(vector)!=receipt.get('vector_sha256')):
  raise LibraryExperimentError('library asset hash mismatch')
 info=_object(metadata_raw)
 if (info.get('id')!=voice_id or info.get('book_id')!=receipt.get('book_id')
     or any(not isinstance(info.get(k),str) or not info[k] for k in ('gender','age'))
     or info.get('human_reviewed') is not False or info.get('production_eligible') is not False
     or info.get('locked_snapshot_sha256')!=receipt.get('locked_snapshot_sha256')):
  raise LibraryExperimentError('library metadata mismatch')
 try:_validate_vector(vector)
 except Exception as exc:raise LibraryExperimentError('library vector invalid') from exc
 return {'voice_ref':'library:zh/'+voice_id,'metadata':info}
