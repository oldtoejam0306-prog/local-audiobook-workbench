"""Read-only, no-service RPC for previously created experimental chapter indexes.

The registry is trusted operator configuration loaded at process startup, not a
client request. It binds each allowed book to exact workspace/index paths and a
caller-authenticated snapshot/index digest. Script/cast hashes in many current
experiments are self-computed snapshots, not independently authenticated history.
This module does not parse books again, render audio, call networks or models.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from collections.abc import Mapping
from typing import Any

import book_workspace_snapshot as workspace
import chapter_local_index as chapters
import persisted_chapter_index as persisted


class IndexRpcError(RuntimeError):
    pass


class IndexRpcBusy(IndexRpcError):
    pass


def _sha(raw:bytes)->str:return hashlib.sha256(raw).hexdigest()


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise IndexRpcError('duplicate JSON key')
        result[key]=value
    return result


def _load(raw:bytes)->Any:
    try:return json.loads(raw.decode('utf8'),object_pairs_hook=_unique,
                          parse_constant=lambda _:( _ for _ in ()).throw(ValueError('invalid number')))
    except (UnicodeError,ValueError) as exc:raise IndexRpcError('invalid JSON') from exc


def _registry_entry(book_id:str,entry:Any)->dict:
    required={'book_root','workdir','index_workspace','snapshot','index_sha256'}
    if not isinstance(book_id,str) or not book_id.strip() or not isinstance(entry,dict) or set(entry)!=required:
        raise IndexRpcError('invalid registry entry')
    snapshot=persisted._trusted_snapshot(entry['snapshot'])
    if snapshot['book_id']!=book_id or not persisted._is_hash(entry['index_sha256']):raise IndexRpcError('registry identity mismatch')
    root=workspace._plain_directory(entry['book_root'],'registered book root')
    wd=workspace._plain_directory(entry['workdir'],'registered workdir')
    index=workspace._plain_directory(entry['index_workspace'],'registered index workspace')
    if wd!=root/'work.huashuo' or index.is_relative_to(root) or root.is_relative_to(index):
        raise IndexRpcError('registry workdir/index boundary invalid')
    return {'book_root':root,'workdir':wd,'index_workspace':index,'snapshot':snapshot,'index_sha256':entry['index_sha256']}


def _restore_summary(index:Path,chapter:str|None,snapshot:dict,index_sha256:str)->dict:
    """Isolated one-shot worker uses existing-only, nonblocking index lock.

    The persistent reader normally creates/blocks on a lock. In this dedicated
    child only, route its lock to the existing, nonblocking read-only-safe lock
    implementation; never patch the parent or a shared service process.
    """
    code = """import contextlib,json,sys
from pathlib import Path
import book_workspace_snapshot as w
import persisted_chapter_index as p
request=json.load(sys.stdin)
try:
 index=Path(request['index'])
 lock=index.parent / ('.'+index.name+'.lock')
 with w._lock_existing(lock):
  before=p._read_regular(index/'manifest.json',max_bytes=64*1024*1024)
  manifest=json.loads(before)
  entries=manifest.get('chapters') if isinstance(manifest,dict) else None
  if not isinstance(entries,list) or not entries or not isinstance(entries[0],dict):
   raise ValueError('invalid chapters')
  chapter=request['chapter'] if request['chapter'] is not None else entries[0].get('chapter_id')
  p._process_lock=lambda _path: contextlib.nullcontext()
  r=p.restore_chapter(index,chapter,trusted_snapshot=request['snapshot'],expected_index_sha256=request['digest'])
  after=p._read_regular(index/'manifest.json',max_bytes=64*1024*1024)
  if before != after: raise ValueError('manifest changed')
  data={'chapter_id':r['chapter_id'],'block_count':len(r['script_rows'])-1,'chapter_digest':r['chapter_digest'],'local_text_sha256':r['local_text_sha256'],'index_sha256':r['index_sha256'],'human_reviewed':False}
  if request['chapter'] is None: data['chapter_count']=len(entries)
 print(json.dumps({'ok':True,'data':data}))
except w.WorkspaceSnapshotBusy:
 print(json.dumps({'ok':False,'error':'index_busy'}))
except Exception:
 print(json.dumps({'ok':False,'error':'index_verification_failed'}))
"""
    allowed=('SYSTEMROOT','WINDIR','PROGRAMDATA','PATH','TEMP','TMP','TMPDIR')
    env={key:value for key,value in os.environ.items() if key.upper() in allowed}
    env['PYTHONDONTWRITEBYTECODE']='1';env['PYTHONPATH']=str(Path(__file__).resolve().parent)
    request={'index':str(index),'chapter':chapter,'snapshot':snapshot,'digest':index_sha256}
    try:
        proc=subprocess.run([sys.executable,'-B','-c',code],input=json.dumps(request),capture_output=True,
                            text=True,encoding='utf8',env=env,timeout=30)
    except subprocess.TimeoutExpired as exc:raise IndexRpcBusy('index verification timed out') from exc
    if proc.returncode!=0:raise IndexRpcError('index verification worker failed')
    response=_load(proc.stdout.encode('utf8'))
    if not isinstance(response,dict) or response.get('ok') is not True:
        if isinstance(response,dict) and response.get('error')=='index_busy':raise IndexRpcBusy('index lock busy')
        raise IndexRpcError('index verification failed')
    result=response.get('data')
    fields={'chapter_id','block_count','chapter_digest','local_text_sha256','index_sha256','human_reviewed'}
    if chapter is None:fields.add('chapter_count')
    if (not isinstance(result,dict) or set(result)!=fields or not isinstance(result['chapter_id'],str)
        or not result['chapter_id'] or type(result['block_count']) is not int or result['block_count']<0
        or not all(persisted._is_hash(result[key]) for key in ('chapter_digest','local_text_sha256','index_sha256'))
        or result['index_sha256']!=index_sha256 or result['human_reviewed'] is not False
        or (chapter is not None and result['chapter_id']!=chapter)
        or (chapter is None and (type(result['chapter_count']) is not int or result['chapter_count']<1))):
        raise IndexRpcError('index verification worker response invalid')
    return result


class VerifiedIndexRpc:
    def __init__(self,registry:Mapping[str,Any]):
        if not isinstance(registry,Mapping) or not registry:raise IndexRpcError('trusted registry is empty')
        self._registry={key:_registry_entry(key,value) for key,value in registry.items()}

    def dispatch(self,request:Any)->dict:
        if not isinstance(request,dict) or request.get('action') not in ('status','verify_chapter'):
            raise IndexRpcError('unsupported action')
        action=request['action'];expected={'action','book_id'}|({'chapter_id'} if action=='verify_chapter' else set())
        if set(request)!=expected or not isinstance(request.get('book_id'),str):raise IndexRpcError('request fields are invalid')
        if request['book_id'] not in self._registry:raise IndexRpcError('book is not registered')
        chapter=request.get('chapter_id')
        if action=='verify_chapter' and (not isinstance(chapter,str) or not chapter.strip() or len(chapter)>200):raise IndexRpcError('chapter ID is invalid')
        config=self._registry[request['book_id']];root=config['book_root'];wd=config['workdir'];index=config['index_workspace'];expected_snapshot=config['snapshot']
        # The persistence module can create its sidecar lock, so preflight its
        # existing regular file; no deliberate lock creation is permitted here.
        index_lock=index.parent/f'.{index.name}.lock'
        if index_lock.is_symlink() or not index_lock.is_file() or index_lock.stat().st_nlink!=1:
            raise IndexRpcError('existing index lock is unavailable')
        with workspace._book_level_locks(root,wd):
            paths={'book_manifest':root/'book.json','text':wd/'text.txt','script':wd/'script.huaben.jsonl','cast':wd/'cast.json'}
            data={};fingerprints={}
            data['book_manifest'],fingerprints['book_manifest']=workspace._read_regular(paths['book_manifest'],root=root,label='book_manifest')
            manifest=_load(data['book_manifest'])
            if not isinstance(manifest,dict) or manifest.get('book_id')!=request['book_id'] or manifest.get('format') not in ('.txt','.docx'):
                raise IndexRpcError('current book identity is invalid')
            paths['source_docx']=root/('original'+manifest['format'])
            for name in ('source_docx','text','script','cast'):
                data[name],fingerprints[name]=workspace._read_regular(paths[name],root=root,label=name)
            source_hash=_sha(data['source_docx']);text_hash=_sha(data['text'])
            if manifest.get('source_sha256')!=source_hash:raise workspace.WorkspaceSnapshotChanged('book source changed')
            try:
                rows=[_load(line) for line in data['script'].splitlines() if line.strip()];cast=_load(data['cast'])
            except Exception as exc:raise IndexRpcError('current script/cast invalid') from exc
            if not rows or not isinstance(rows[0],dict) or rows[0].get('type')!='huaben' or rows[0].get('text')!='text.txt' or rows[0].get('text_sha256')!=text_hash:
                raise workspace.WorkspaceSnapshotChanged('workdir text/header changed')
            current={'book_id':request['book_id'],'source_sha256':source_hash,'text_sha256':text_hash,
                     'script_sha256':chapters.object_sha256(rows),'cast_sha256':chapters.object_sha256(cast)}
            if current!=expected_snapshot:raise workspace.WorkspaceSnapshotChanged('current snapshot differs from registered index')
            result=_restore_summary(index,chapter,current,config['index_sha256'])
            for name,path in paths.items():
                try:now=os.stat(path,follow_symlinks=False)
                except OSError as exc:raise workspace.WorkspaceSnapshotChanged('workspace changed during verification') from exc
                if workspace._file_identity(now)!=fingerprints[name]:raise workspace.WorkspaceSnapshotChanged('workspace changed during verification')
            if action=='status':
                return {'book_id':request['book_id'],'status':'manifest_and_first_chapter_verified','chapter_count':result['chapter_count'],'index_sha256':config['index_sha256'],'human_reviewed':False}
            return {'book_id':request['book_id'],'chapter_id':result['chapter_id'],'block_count':result['block_count'],
                    'chapter_digest':result['chapter_digest'],'local_text_sha256':result['local_text_sha256'],
                    'index_sha256':result['index_sha256'],'human_reviewed':False}


def load_registry(path:str|Path)->dict:
    """Read trusted operator config; stdin requests cannot choose this path."""
    p=Path(path).expanduser()
    if not p.is_absolute() or p.is_symlink() or not p.is_file() or p.stat().st_nlink!=1 or p.stat().st_size>1024*1024:
        raise IndexRpcError('trusted registry path invalid')
    data=_load(p.read_bytes())
    if not isinstance(data,dict) or set(data)!={'version','books'} or type(data['version']) is not int or data['version']!=1 or not isinstance(data['books'],dict):raise IndexRpcError('trusted registry format invalid')
    return data['books']


def main()->int:
    """One stdin JSON request; registry path comes from trusted process environment."""
    import sys
    try:
        configured=os.environ.get('VERIFIED_INDEX_RPC_REGISTRY')
        if not configured:raise IndexRpcError('trusted registry not configured')
        rpc=VerifiedIndexRpc(load_registry(configured))
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise IndexRpcError('request too large')
        answer=rpc.dispatch(_load(raw));print(json.dumps({'ok':True,'data':answer},ensure_ascii=False,separators=(',',':')));return 0
    except IndexRpcBusy:
        print(json.dumps({'ok':False,'error':'index_busy_or_timeout'}));return 2
    except workspace.WorkspaceSnapshotBusy:
        print(json.dumps({'ok':False,'error':'workspace_busy'}));return 2
    except workspace.WorkspaceSnapshotChanged:
        print(json.dumps({'ok':False,'error':'workspace_changed'}));return 2
    except Exception:
        print(json.dumps({'ok':False,'error':'verification_failed'}));return 2


if __name__=='__main__':raise SystemExit(main())
