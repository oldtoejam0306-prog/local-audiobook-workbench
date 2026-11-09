"""Unwired two-process Mac text voice children; production CLI is disabled.

The private functions implement the intended model calls for fake-module tests.
The trusted parent must enforce sandbox, local model provenance and serial
execution. Hash pins bind inventories but do not authenticate model origins.
"""
import hashlib,io,os,sys,wave
from pathlib import Path
import local_voice_runner as contract
from isolated_voice_design import _validate_wav

class VoiceChildError(RuntimeError): pass

def _hash(raw):return hashlib.sha256(raw).hexdigest()
def _output(stage,name,raw):
 p=Path(stage)
 if not p.is_absolute() or not p.is_dir() or p.is_symlink():raise VoiceChildError('invalid stage')
 target=p/name
 fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 try:
  with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
 except BaseException:
  try:target.unlink()
  except OSError:pass
  raise
 return _hash(raw)

def _design(*,stage,description,text,seed,model,np,mx,load_model):
 """Generate only from text and write the fixed stage WAV."""
 contract._request(description,text,seed);before=contract._inspect_model(model,'voice_design')
 path=Path(model.snapshot_root)
 if not path.is_absolute():raise VoiceChildError('local absolute model path required')
 mx.random.seed(seed);engine=load_model(str(path.resolve(strict=True)))
 chunks=[];rate=None
 for part in engine.generate_voice_design(text=text,instruct=description,language='Chinese',temperature=.85,
                                          top_p=.95,top_k=50,repetition_penalty=1.05,max_tokens=700):
  mx.eval(part.audio);samples=np.asarray(part.audio,dtype=np.float32).reshape(-1)
  if not np.isfinite(samples).all() or int(part.sample_rate)!=24000:raise VoiceChildError('invalid generated samples')
  rate=int(part.sample_rate);chunks.append(samples)
 if not chunks:raise VoiceChildError('no generated samples')
 samples=np.concatenate(chunks)
 if not 0<samples.size<=24000*120:raise VoiceChildError('invalid generated duration')
 if contract._inspect_model(model,'voice_design')!=before:raise VoiceChildError('design inventory changed')
 out=io.BytesIO()
 with wave.open(out,'wb') as w:
  w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate);w.writeframes((np.clip(samples,-1,1)*32767).astype('<i2').tobytes())
 raw=out.getvalue();_validate_wav(raw)
 return {'wav_sha256':_output(stage,'generated.wav',raw),'inventory_sha256':model.inventory_sha256,'production_eligible':False}

def _base_from_stdin(*,stage,stdin,expected_wav_sha256,model,np,mx,load_model):
 """Consume only a parent-provided stream; there is no audio path argument."""
 before=contract._inspect_model(model,'speaker_encoder');path=Path(model.snapshot_root)
 if not path.is_absolute():raise VoiceChildError('local absolute model path required')
 if not isinstance(expected_wav_sha256,str) or len(expected_wav_sha256)!=64:raise VoiceChildError('invalid expected hash')
 raw=stdin.read(6*1024*1024+1)
 if type(raw) is not bytes or len(raw)>6*1024*1024 or _hash(raw)!=expected_wav_sha256:raise VoiceChildError('parent stream mismatch')
 _validate_wav(raw)
 with wave.open(io.BytesIO(raw),'rb') as w:
  if w.getnchannels()!=1 or w.getsampwidth()!=2 or w.getframerate()!=24000:raise VoiceChildError('invalid audio format')
  samples=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(np.float32)/32767
 engine=load_model(str(path.resolve(strict=True)));vector=engine.extract_speaker_embedding(mx.array(samples));mx.eval(vector)
 arr=np.asarray(vector,dtype=np.float32).reshape(-1)
 if arr.size!=2048 or not np.isfinite(arr).all():raise VoiceChildError('invalid speaker vector')
 if contract._inspect_model(model,'speaker_encoder')!=before:raise VoiceChildError('base inventory changed')
 out=io.BytesIO();np.save(out,arr,allow_pickle=False)
 return {'vector_sha256':_output(stage,'speaker-vector.npy',out.getvalue()),'input_wav_sha256':expected_wav_sha256,
         'inventory_sha256':model.inventory_sha256,'production_eligible':False}

def real_child_cli(*_args,**_kwargs):raise VoiceChildError('real child CLI disabled pending sandbox and resource review')
if __name__=='__main__':raise SystemExit('real child CLI disabled')
