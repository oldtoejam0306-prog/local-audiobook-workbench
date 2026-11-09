"""Synthetic-only fictional voice worker; all real execution is hard-disabled.

No real model execution is available. A future implementation requires a
trusted launcher, authenticated multi-file model inventory, sandbox enforcement
and a provenance-bound generated WAV. Synthetic results are never trusted
real receipts; this module does not install or publish any voice assets.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import wave
from typing import Any

import local_model_inventory
from isolated_voice_design import _validate_wav, _validate_vector


class VoiceWorkerError(RuntimeError):
    pass


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _atomic(path: Path, value: dict) -> None:
    tmp=path.with_name(path.name+'.'+secrets.token_hex(12)+'.tmp')
    try:
        with tmp.open('xb') as handle:
            handle.write(_canonical(value));handle.flush();os.fsync(handle.fileno())
        os.replace(tmp,path)
        if os.name!='nt':
            fd=os.open(str(path.parent),os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
    finally:
        if tmp.exists():tmp.unlink()


def _hash_path(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
    return h.hexdigest()


def _model(model: dict, role: str) -> tuple[dict, dict]:
    if not isinstance(model,dict) or set(model)!={'role','model_id','repository_root','snapshot_root','inventory_sha256'} or model['role']!=role:
        raise VoiceWorkerError('model contract invalid')
    if (not isinstance(model['model_id'],str) or not model['model_id'].strip() or '://' in model['model_id']
            or not isinstance(model['inventory_sha256'],str) or len(model['inventory_sha256'])!=64
            or any(c not in '0123456789abcdef' for c in model['inventory_sha256'])):
        raise VoiceWorkerError('model identity/pin invalid')
    for k in ('repository_root','snapshot_root'):
        if not isinstance(model[k],str) or not Path(model[k]).is_absolute() or '://' in model[k]:raise VoiceWorkerError('local absolute model paths required')
    try: inventory=local_model_inventory.inventory_model(model['repository_root'],model['snapshot_root'])
    except (ValueError,OSError) as exc:raise VoiceWorkerError('model inventory invalid') from exc
    if _hash(_canonical(inventory))!=model['inventory_sha256']:raise VoiceWorkerError('model inventory hash mismatch')
    return dict(model),inventory


def _verify_sandbox() -> None:
    """Require observable denial before importing/loading any real model."""
    s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);s.settimeout(1)
    try:
        try:s.connect(('127.0.0.1',22))
        except PermissionError:pass
        else:raise VoiceWorkerError('network sandbox denial not established')
    finally:s.close()
    try:fd=os.open('/dev/null',os.O_WRONLY)
    except PermissionError:pass
    else:os.close(fd);raise VoiceWorkerError('external write sandbox denial not established')


def _validate_request(request: dict) -> dict:
    if not isinstance(request,dict) or set(request)!={'description','sample_text','seed'}:raise VoiceWorkerError('only text design parameters are accepted')
    for key,limit in (('description',2000),('sample_text',500)):
        val=request[key]
        if not isinstance(val,str) or not val.strip() or len(val)>limit or any(not c.isprintable() and c not in '\n\t' for c in val):raise VoiceWorkerError('invalid fictional text input')
    if type(request['seed']) is not int or not 0<=request['seed']<2**32:raise VoiceWorkerError('invalid seed')
    return dict(request)


def _child(stage: Path, phase: str) -> int:
    """Hard closed: sandbox probes alone cannot authenticate WAV provenance."""
    raise VoiceWorkerError('real child disabled until trusted launcher and generated-WAV provenance exist')


def _quote(value: str) -> str:
    return '"'+value.replace('\\','\\\\').replace('"','\\"')+'"'


def _real_launcher(stage: Path, phase: str, python: str, timeout: int) -> subprocess.CompletedProcess:
    if sys.platform!='darwin' or not Path('/usr/bin/sandbox-exec').is_file():raise VoiceWorkerError('real worker requires macOS system sandbox-exec')
    profile='(version 1)(allow default)(deny network*)(deny file-write*)(allow file-write* (subpath '+_quote(str(stage))+'))'
    env={'HOME':str(stage),'TMPDIR':str(stage/'tmp'),'HF_HOME':str(stage/'hf'),'HF_HUB_OFFLINE':'1',
         'TRANSFORMERS_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1','PYTHONDONTWRITEBYTECODE':'1',
         'PATH':str(Path(python).parent)+':/usr/bin:/bin:/usr/sbin:/sbin'}
    return subprocess.run(['/usr/bin/sandbox-exec','-p',profile,python,str(Path(__file__).resolve()),'--child',phase,str(stage)],
                          cwd=stage,env=env,capture_output=True,text=True,timeout=timeout)


def _execute(parent: Path, request: dict, design_model: dict, encoder_model: dict, python: str,
             python_sha256: str, launcher, *, timeout: int, mode: str) -> dict:
    if mode != 'synthetic_test_only':
        raise VoiceWorkerError('private execution helper accepts synthetic_test_only mode only')
    req=_validate_request(request);design,di=_model(design_model,'voice_design');encoder,ei=_model(encoder_model,'speaker_encoder')
    interpreter=Path(python)
    if not interpreter.is_absolute() or not interpreter.is_file() or not isinstance(python_sha256,str) or _hash_path(interpreter)!=python_sha256:raise VoiceWorkerError('Python binary pin mismatch')
    if not parent.is_absolute() or not parent.is_dir() or parent.is_symlink() or parent.resolve()==Path(parent.anchor):raise VoiceWorkerError('invalid private task parent')
    stage=Path(tempfile.mkdtemp(prefix='mac-fictional-voice-',dir=parent)).resolve(strict=True)
    (stage/'tmp').mkdir();(stage/'hf').mkdir()
    contract={'version':1,'mode':mode,'request':req,'reference_audio':False,'design_model':design,'encoder_model':encoder,
              'python_path':str(interpreter),'python_sha256':python_sha256,'worker_sha256':_hash_path(Path(__file__).resolve())}
    _atomic(stage/'contract.json',contract);_atomic(stage/'design-inventory.json',di);_atomic(stage/'encoder-inventory.json',ei)
    state={'status':'in_progress','contract_sha256':_hash(_canonical(contract)),'phase':'design'};_atomic(stage/'state.json',state)
    try:
        times={};returns={}
        for phase in ('design','encode'):
            state['phase']=phase;_atomic(stage/'state.json',state);start=time.monotonic()
            completed=launcher(stage,phase,python,timeout);times[phase]=round(time.monotonic()-start,3);returns[phase]=getattr(completed,'returncode',None)
            if type(returns[phase]) is not int or returns[phase]!=0:raise VoiceWorkerError(f'{phase} child failed')
            if phase=='design':
                wav=stage/'generated.wav'
                if wav.is_symlink() or not wav.is_file() or wav.stat().st_nlink!=1:raise VoiceWorkerError('invalid WAV artifact')
                raw=wav.read_bytes();_validate_wav(raw);_atomic(stage/'design-proof.json',{'wav_sha256':_hash(raw)})
            else:
                vector=stage/'speaker-vector.npy'
                if vector.is_symlink() or not vector.is_file() or vector.stat().st_nlink!=1:raise VoiceWorkerError('invalid vector artifact')
                v=vector.read_bytes();_validate_vector(v)
                if _hash_path(wav)!=_hash(raw):raise VoiceWorkerError('WAV changed during encode')
        _model(design,'voice_design');_model(encoder,'speaker_encoder')
        result={'status':'complete','mode':mode,'contract_sha256':state['contract_sha256'],'request':req,
                'reference_audio':False,'design_model':design,'encoder_model':encoder,
                'design_returncode':returns['design'],'encode_returncode':returns['encode'],'seconds':times,
                'wav_sha256':_hash(raw),'wav_size':len(raw),'vector_sha256':_hash(v),'vector_size':len(v),
                'human_reviewed':False,'stage':str(stage)}
        _atomic(stage/'result.json',result);_atomic(stage/'state.json',{'status':'complete','contract_sha256':state['contract_sha256'],'result_sha256':_hash(_canonical(result))})
        return result
    except BaseException:
        # Unknown covers timeouts, process death, partial output and interruption.
        _atomic(stage/'state.json',{'status':'unknown','contract_sha256':state['contract_sha256'],'phase':state['phase']})
        raise


def run_real(*, parent: str | Path, request: dict, design_model: dict, encoder_model: dict,
             python: str, python_sha256: str, timeout: int=1800) -> dict:
    """Real execution remains closed until a trusted launcher is implemented."""
    raise VoiceWorkerError('real worker disabled until trusted sandbox launcher and model/WAV provenance exist')


def run_synthetic(*, parent: str | Path, request: dict, design_model: dict, encoder_model: dict,
                  python: str, python_sha256: str, fake_launcher, timeout: int=30) -> dict:
    """Test-only entry, not an offline/security boundary; never use real engines."""
    if not callable(fake_launcher):raise VoiceWorkerError('fake launcher required')
    result=_execute(Path(parent),request,design_model,encoder_model,python,python_sha256,fake_launcher,timeout=timeout,mode='synthetic_test_only')
    return result


def inspect_task(stage: str | Path) -> dict:
    """Inspect synthetic test artifacts only; never attest a real task origin.

    Ordinary hashes are not authentication. No on-disk execution_mode claim can
    make this a trusted real receipt, and inspection never dispatches work.
    """
    root=Path(stage)
    if root.is_symlink() or not root.is_dir():raise VoiceWorkerError('invalid task')
    state=json.loads((root/'state.json').read_text(encoding='utf8'))
    contract=json.loads((root/'contract.json').read_text(encoding='utf8'))
    if contract.get('mode')!='synthetic_test_only':
        raise VoiceWorkerError('only synthetic test artifacts can be inspected; real origin is untrusted')
    if _hash(_canonical(contract))!=state.get('contract_sha256'):
        raise VoiceWorkerError('task identity altered')
    if state.get('status')!='complete':
        return {'status':'synthetic_only_manual_inspection_required','original_state':state}
    result=json.loads((root/'result.json').read_text(encoding='utf8'))
    if (result.get('mode')!='synthetic_test_only' or _hash(_canonical(result))!=state.get('result_sha256')
            or result.get('contract_sha256')!=state.get('contract_sha256')):raise VoiceWorkerError('task identity altered')
    for name,key in (('generated.wav','wav_sha256'),('speaker-vector.npy','vector_sha256')):
        path=root/name
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink!=1 or _hash_path(path)!=result.get(key):raise VoiceWorkerError('task artifact altered')
    _model(contract['design_model'],'voice_design');_model(contract['encoder_model'],'speaker_encoder')
    if _hash_path(Path(contract['python_path']))!=contract['python_sha256']:raise VoiceWorkerError('Python binary changed')
    return {'status':'synthetic_only_complete','trusted_origin':False,'result':result}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--child',choices=('design','encode'),required=True);parser.add_argument('stage');args=parser.parse_args()
    try:raise SystemExit(_child(Path(args.stage),args.child))
    except Exception as exc:
        print(type(exc).__name__+': '+str(exc),file=sys.stderr);raise SystemExit(2)
