"""Unwired VoiceDesign-to-Base child implementation; real CLI remains disabled.

Private implementation is exercised only with fake MLX models in tests. A
trusted launcher, sandbox, pinned dependencies and M2 memory validation are
required before any real execution; model paths/digests alone do not prove them.
No input audio or separate encode operation exists in this interface.
"""
import gc
import hashlib
import io
import json
import os
from pathlib import Path
import wave
import local_voice_runner as contract

class VoiceChildError(RuntimeError): pass

def _hash(raw): return hashlib.sha256(raw).hexdigest()
def _write_new(path,raw):
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(raw);f.flush();os.fsync(f.fileno())
    except BaseException:
        try:path.unlink()
        except OSError:pass
        raise

def _execute_unwired(*, stage, description, text, seed, design_model, base_model, np, mx, load_model):
    """Implementation for explicit fake-module testing, not a security boundary."""
    root=Path(stage)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink() or any(root.iterdir()):
        raise VoiceChildError('empty new absolute stage required')
    req=contract._request(description,text,seed)
    design_inv=contract._inspect_model(design_model,'voice_design')
    base_inv=contract._inspect_model(base_model,'speaker_encoder')
    _write_new(root/'started.json',contract._json({'request_sha256':_hash(contract._json(req)),
        'design_inventory_sha256':design_model.inventory_sha256,'base_inventory_sha256':base_model.inventory_sha256}))
    published=[]
    try:
        mx.random.seed(seed)
        design=load_model(str(design_model.snapshot_root.resolve(strict=True)))
        parts=[];rate=None
        stream=design.generate_voice_design(text=text,instruct=description,language='Chinese',temperature=.85,
            top_p=.95,top_k=50,repetition_penalty=1.05,max_tokens=700)
        for part in stream:
            mx.eval(part.audio)
            samples_part=np.asarray(part.audio,dtype=np.float32).reshape(-1)
            if not np.isfinite(samples_part).all():raise VoiceChildError('nonfinite generated audio')
            sample_rate=int(part.sample_rate)
            if sample_rate!=24000 or (rate is not None and sample_rate!=rate):raise VoiceChildError('invalid sample rate')
            rate=sample_rate;parts.append(samples_part)
        if not parts:raise VoiceChildError('no generated samples')
        samples=np.concatenate(parts)
        if samples.size==0 or samples.size>24000*120:raise VoiceChildError('audio length invalid')
        # Release all design/generator references before loading Base.
        del stream,part,parts,samples_part,design
        gc.collect()
        if callable(getattr(mx,'clear_cache',None)):mx.clear_cache()
        elif callable(getattr(getattr(mx,'metal',None),'clear_cache',None)):mx.metal.clear_cache()
        base=load_model(str(base_model.snapshot_root.resolve(strict=True)))
        vector=base.extract_speaker_embedding(mx.array(samples));mx.eval(vector)
        array=np.asarray(vector,dtype=np.float32).reshape(-1)
        if array.size!=2048 or not np.isfinite(array).all():raise VoiceChildError('invalid speaker vector')
        if contract._inspect_model(design_model,'voice_design')!=design_inv or contract._inspect_model(base_model,'speaker_encoder')!=base_inv:
            raise VoiceChildError('model inventory changed')
        wav_io=io.BytesIO()
        with wave.open(wav_io,'wb') as w:
            w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate)
            w.writeframes((np.clip(samples,-1,1)*32767).astype('<i2').tobytes())
        npy_io=io.BytesIO();np.save(npy_io,array,allow_pickle=False)
        wav_bytes=wav_io.getvalue();npy_bytes=npy_io.getvalue()
        _write_new(root/'generated.wav',wav_bytes);published.append(root/'generated.wav')
        _write_new(root/'speaker-vector.npy',npy_bytes);published.append(root/'speaker-vector.npy')
        result={'status':'complete_test_only','wav_sha256':_hash(wav_bytes),'vector_sha256':_hash(npy_bytes),
            'input_sha256':_hash(contract._json(req)),'design_inventory_sha256':design_model.inventory_sha256,
            'base_inventory_sha256':base_model.inventory_sha256,'reference_audio':False,'production_eligible':False}
        _write_new(root/'complete.json',contract._json(result));return result
    except BaseException as exc:
        for path in published:
            try:path.unlink()
            except OSError:pass
        try:_write_new(root/'unknown.json',b'{"status":"unknown_no_retry"}')
        except OSError:pass
        raise VoiceChildError('child failed; output status unknown, no retry') from exc

def run_real(*_args,**_kwargs):
    raise VoiceChildError('real execution disabled pending sandbox and memory review')

if __name__=='__main__':raise SystemExit('real child CLI disabled')
