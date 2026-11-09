"""Offline-only synthetic voice artifact adapter; not wired to production.

Runner is an injected TRUSTED offline implementation, launched under an OS
sandbox with network disabled and writes confined to the new directory. This
module itself cannot enforce runner behavior, authenticate model identity, or
verify that output is perceptually suitable. No reference audio is accepted.
"""
import hashlib
import hmac
import io
import json
import math
import re
import os
import secrets
from contextlib import contextmanager
import tempfile
import wave
from pathlib import Path

import numpy as np

_SAFE = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_TOKEN = object()


def _sha(data): return hashlib.sha256(data).hexdigest()
def _json(data): return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
def _file_bytes(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError('artifact must be a regular non-linked file')
    return path.read_bytes()

def _write_new(path, data):
    with path.open('xb') as file:
        file.write(data);file.flush();os.fsync(file.fileno())

def _validate_wav(data):
    try:
        with wave.open(io.BytesIO(data), 'rb') as audio:
            if audio.getnchannels()!=1 or audio.getsampwidth()!=2 or audio.getframerate()!=24000 or audio.getnframes()<1:
                raise ValueError('generated WAV must contain mono 24 kHz PCM16 audio')
            frames=audio.getnframes(); raw=audio.readframes(frames)
            if len(raw)!=frames*2: raise ValueError('truncated WAV')
    except (wave.Error, EOFError) as exc: raise ValueError('invalid generated WAV') from exc

def _validate_vector(data):
    try:
        source=io.BytesIO(data); vector=np.load(source, allow_pickle=False)
        if source.read(1) or not isinstance(vector,np.ndarray) or vector.dtype!=np.dtype('float32') or vector.shape!=(2048,) or not np.isfinite(vector).all():
            raise ValueError('vector must be finite float32 with shape (2048,) and no trailing data')
    except (ValueError, EOFError, OSError) as exc: raise ValueError('invalid speaker vector') from exc

def _atomic(path, data):
    temp=path.with_name(path.name+'.'+secrets.token_hex(12)+'.tmp')
    try:
        with temp.open('xb') as file:
            file.write(data);file.flush();os.fsync(file.fileno())
        os.replace(temp,path)
        if os.name != 'nt':
            fd=os.open(str(path.parent),os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
    finally:
        if temp.exists():temp.unlink()

@contextmanager
def _lock(path, expected):
    with path.open('r+b') as file:
        st=os.fstat(file.fileno())
        if (st.st_dev,st.st_ino)!=expected:raise ValueError('lock file identity changed')
        if os.name=='nt':
            import msvcrt
            file.seek(0);msvcrt.locking(file.fileno(),msvcrt.LK_LOCK,1)
            try:
                st=path.stat()
                if (st.st_dev,st.st_ino)!=expected:raise ValueError('lock file replaced')
                yield
                st=path.stat()
                if (st.st_dev,st.st_ino)!=expected:raise ValueError('lock file replaced')
            finally:file.seek(0);msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(file.fileno(),fcntl.LOCK_EX)
            try:
                st=path.stat()
                if (st.st_dev,st.st_ino)!=expected:raise ValueError('lock file replaced')
                yield
                st=path.stat()
                if (st.st_dev,st.st_ino)!=expected:raise ValueError('lock file replaced')
            finally:fcntl.flock(file.fileno(),fcntl.LOCK_UN)

def _verify_model(model):
    if (not isinstance(model,dict) or set(model)!={'id','sha256','path'}
        or not isinstance(model['id'],str) or not model['id'].strip() or '://' in model['id']
        or not isinstance(model['sha256'],str) or not _SHA.fullmatch(model['sha256'])
        or not isinstance(model['path'],str)):
        raise ValueError('local model file identity and SHA-256 required')
    path=Path(model['path'])
    if not path.is_absolute() or _sha(_file_bytes(path))!=model['sha256']:
        raise ValueError('actual local model hash mismatch')

class IsolatedVoiceBook:
    """New isolated book or explicitly authenticated recovery under a safe root.

    Only injected test runners are enabled. Real runners remain disabled until
    the caller can prove OS sandbox and trusted model/lock provenance. Caller
    must protect recovery marker, book ID, safe root and request hash externally.
    """
    def __init__(self, root, book_id, safe_root, marker, expected_request_sha256=None, token=None):
        if token is not _TOKEN:raise ValueError('use create or reopen')
        self.root=root;self.book_id=book_id;self.safe_root=safe_root;self.marker=marker
        self.expected_request_sha256=expected_request_sha256
        st=root.stat();self._identity=(st.st_dev,st.st_ino)
    @classmethod
    def create(cls, parent, book_id, *, safe_root):
        if not isinstance(book_id,str) or not _SAFE.fullmatch(book_id):raise ValueError('invalid book id')
        safe=Path(safe_root).resolve(strict=True);parent=Path(parent).resolve(strict=True)
        if not safe.is_dir() or not parent.is_dir() or not parent.is_relative_to(safe):raise ValueError('parent outside safe root')
        root=Path(tempfile.mkdtemp(prefix='isolated-voice-book-',dir=parent)).resolve(strict=True)
        (root/'zh').mkdir();(root/'artifacts').mkdir()
        marker=secrets.token_hex(32)
        _write_new(root/'lock',b'0')
        stat=(root/'lock').stat()
        identity={'book_id':book_id,'marker_sha256':_sha(marker.encode()),'safe_root':str(safe),'root':str(root),'lock_dev':stat.st_dev,'lock_ino':stat.st_ino}
        identity['auth']=hmac.new(bytes.fromhex(marker),_json(identity),hashlib.sha256).hexdigest()
        _write_new(root/'identity.json',_json(identity))
        return cls(root,book_id,safe,marker,token=_TOKEN)
    @classmethod
    def reopen(cls, root, book_id, *, safe_root, marker, expected_request_sha256):
        safe=Path(safe_root).resolve(strict=True);original=Path(root)
        if original.is_symlink():raise ValueError('unsafe root')
        resolved=original.resolve(strict=True)
        if not resolved.is_relative_to(safe) or resolved==safe or not _SAFE.fullmatch(book_id):raise ValueError('root or book mismatch')
        if not isinstance(marker,str) or not _SHA.fullmatch(marker) or not isinstance(expected_request_sha256,str) or not _SHA.fullmatch(expected_request_sha256):raise ValueError('trusted marker and request hash required')
        obj=cls(resolved,book_id,safe,marker,expected_request_sha256,_TOKEN);obj._check_root()
        return obj
    def _check_root(self):
        if self.root.is_symlink() or not self.root.is_dir() or not self.root.is_relative_to(self.safe_root):raise ValueError('unsafe book root')
        st=self.root.stat()
        if (st.st_dev,st.st_ino)!=self._identity:raise ValueError('book root changed')
        identity=json.loads(_file_bytes(self.root/'identity.json').decode())
        if not isinstance(identity,dict):raise ValueError('book identity mismatch')
        signature=identity.get('auth'); unsigned={k:v for k,v in identity.items() if k!='auth'}
        if signature!=hmac.new(bytes.fromhex(self.marker),_json(unsigned),hashlib.sha256).hexdigest():raise ValueError('book identity authentication failed')
        lock_path=self.root/'lock'
        if lock_path.is_symlink() or not lock_path.is_file() or lock_path.stat().st_nlink!=1:raise ValueError('unsafe lock')
        st=lock_path.stat()
        expected={'book_id':self.book_id,'marker_sha256':_sha(self.marker.encode()),'safe_root':str(self.safe_root),'root':str(self.root),'lock_dev':st.st_dev,'lock_ino':st.st_ino}
        if unsigned!=expected:raise ValueError('book identity or lock identity mismatch')
        return (st.st_dev,st.st_ino)
        for name in ('zh','artifacts'):
            path=self.root/name
            if path.is_symlink() or not path.is_dir() or path.resolve().parent!=self.root:raise ValueError('unsafe library directory')
    def design(self, *, character_id, voice_id, description, sample_text, seed, design_model, encoder_model, runner, verified_voice_locks):
        lock_identity=self._check_root()
        with _lock(self.root/'lock',lock_identity):
            self._check_root()
            return self._design(character_id=character_id,voice_id=voice_id,description=description,sample_text=sample_text,seed=seed,
                                design_model=design_model,encoder_model=encoder_model,runner=runner,verified_voice_locks=verified_voice_locks)
    def _design(self, *, character_id, voice_id, description, sample_text, seed, design_model, encoder_model, runner, verified_voice_locks):
        if not isinstance(voice_id,str) or not _SAFE.fullmatch(voice_id):raise ValueError('invalid voice id')
        if not isinstance(character_id,str) or not _SAFE.fullmatch(character_id):raise ValueError('invalid character id')
        if not isinstance(verified_voice_locks,dict):raise ValueError('explicit verified voice locks required')
        seen=set()
        for character,record in verified_voice_locks.items():
            if (not isinstance(character,str) or not _SAFE.fullmatch(character) or not isinstance(record,dict)
                or set(record)!={'voice_id','locked_by','status'} or record['locked_by']!='user' or record['status']!='fixed'
                or not isinstance(record['voice_id'],str) or not _SAFE.fullmatch(record['voice_id']) or record['voice_id'] in seen):
                raise ValueError('invalid or conflicting user voice lock')
            seen.add(record['voice_id'])
        if character_id in verified_voice_locks or voice_id in seen:raise ValueError('user-locked character or voice cannot be replaced')
        if not isinstance(description,str) or not description.strip() or len(description)>2000:raise ValueError('fictional voice description required')
        if not isinstance(sample_text,str) or not sample_text.strip() or len(sample_text)>500:raise ValueError('sample text required')
        if type(seed) is not int or not 0<=seed<=2**32-1:raise ValueError('invalid seed')
        _verify_model(design_model);_verify_model(encoder_model)
        request={'version':1,'book_id':self.book_id,'character_id':character_id,'voice_id':voice_id,'description':description,
                 'sample_text':sample_text,'seed':seed,'design_model':dict(design_model),'encoder_model':dict(encoder_model),
                 'fictional_voice_only':True,'reference_audio':False}
        request_hash=_sha(_json(request))
        if self.expected_request_sha256 is not None and self.expected_request_sha256!=request_hash:raise ValueError('trusted request hash mismatch')
        paths={'wav':self.root/'artifacts'/f'{voice_id}.wav','vector':self.root/'zh'/f'{voice_id}.npy',
               'metadata':self.root/'zh'/f'{voice_id}.json','manifest':self.root/'artifacts'/f'{voice_id}.manifest.json'}
        state=self.root/'artifacts'/f'{voice_id}.state.json'
        exists=[p.exists() or p.is_symlink() for p in paths.values()]
        if state.exists() or state.is_symlink():
            record=json.loads(_file_bytes(state).decode('utf-8'))
            if not isinstance(record,dict) or record.get('state')!='complete' or record.get('request_sha256')!=request_hash:raise ValueError('incomplete or unknown state; manual inspection required')
            if not all(exists):raise ValueError('missing completed artifacts')
            manifest_raw=_file_bytes(paths['manifest'])
            expected_state={'state':'complete','request_sha256':request_hash,'manifest_sha256':_sha(manifest_raw)}
            signature=hmac.new(bytes.fromhex(self.marker),_json(expected_state),hashlib.sha256).hexdigest()
            if record!={**expected_state,'auth':signature}:raise ValueError('completed state authentication failed')
            manifest=json.loads(manifest_raw.decode('utf-8'))
            if not isinstance(manifest,dict) or set(manifest)!={'request','request_sha256','wav_sha256','vector_sha256','metadata_sha256'} or manifest['request']!=request or manifest['request_sha256']!=request_hash:raise ValueError('saved request or manifest mismatch')
            wav=_file_bytes(paths['wav']);vector=_file_bytes(paths['vector']);metadata=_file_bytes(paths['metadata'])
            if any(manifest[k]!=_sha(data) for k,data in [('wav_sha256',wav),('vector_sha256',vector),('metadata_sha256',metadata)]):raise ValueError('saved artifact hash mismatch')
            _validate_wav(wav);_validate_vector(vector)
            return {'reused':True,'manifest':manifest,'root':str(self.root),'voice_ref':f'library:zh/{voice_id}'}
        if any(exists):raise ValueError('untracked partial artifacts; manual inspection required')
        if self.expected_request_sha256 is not None:raise ValueError('recovery cannot start a new request')
        if getattr(runner,'offline_test_only',False) is not True or not callable(getattr(runner,'design',None)) or not callable(getattr(runner,'encode',None)):
            raise ValueError('real runners disabled; test-only injected runner required')
        _atomic(state,_json({'state':'in_progress','request_sha256':request_hash}))
        wav=runner.design(dict(request))
        if not isinstance(wav,bytes):raise ValueError('runner must return WAV bytes')
        _validate_wav(wav)
        vector=runner.encode(wav,dict(encoder_model))
        if not isinstance(vector,bytes):raise ValueError('runner must return NPY bytes')
        _validate_vector(vector)
        _verify_model(design_model);_verify_model(encoder_model)
        metadata=_json({'gender':'unknown','age':'unknown','description':'Fictional synthetic voice; unreviewed','id':voice_id})
        manifest={'request':request,'request_sha256':request_hash,'wav_sha256':_sha(wav),'vector_sha256':_sha(vector),'metadata_sha256':_sha(metadata)}
        for key,data in [('wav',wav),('vector',vector),('metadata',metadata),('manifest',_json(manifest))]:
            _write_new(paths[key],data)
        completed={'state':'complete','request_sha256':request_hash,'manifest_sha256':_sha(_json(manifest))}
        completed['auth']=hmac.new(bytes.fromhex(self.marker),_json(completed),hashlib.sha256).hexdigest()
        _atomic(state,_json(completed))
        return {'reused':False,'manifest':manifest,'root':str(self.root),'voice_ref':f'library:zh/{voice_id}'}
