"""Reparse a locked workspace into a new, isolated persistent chapter index.

The existing book is only read. Its two existing book/workdir locks are held
throughout read, reparse, index construction, persistence, and final check.
Locks coordinate cooperative writers; they are not an adversarial filesystem
sandbox. Existing book metadata, script and cast are not externally signed.
No model, provider, rendering, or source mutation is performed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import book_workspace_snapshot as workspace
import chapter_local_index as chapters
import local_txt_parser
import persisted_chapter_index as persisted


class VerifiedWorkspaceIndexError(RuntimeError):
    pass


def _sha(raw: bytes)->str:return hashlib.sha256(raw).hexdigest()


def _unique(pairs):
    out={}
    for key,value in pairs:
        if key in out:raise VerifiedWorkspaceIndexError('duplicate JSON field')
        out[key]=value
    return out


def _load_json(raw:bytes):
    try:return json.loads(raw.decode('utf8'),object_pairs_hook=_unique,
                          parse_constant=lambda value:(_ for _ in ()).throw(ValueError(value)))
    except (UnicodeError,ValueError) as exc:raise VerifiedWorkspaceIndexError('invalid UTF-8 JSON') from exc


def _write_new(path:Path,value:dict)->None:
    raw=chapters._json(value)
    with path.open('xb') as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())


def _check_fingerprints(paths:dict, fingerprints:dict)->None:
    for name,path in paths.items():
        try:info=os.stat(path,follow_symlinks=False)
        except OSError as exc:raise workspace.WorkspaceSnapshotChanged(f'{name} changed during index build') from exc
        if workspace._file_identity(info)!=fingerprints[name]:raise workspace.WorkspaceSnapshotChanged(f'{name} changed during index build')


def build_verified_workspace_index(*,book_root:str|Path,workdir:str|Path,expected_book_id:str,
                                   experimental_workspace:str|Path,
                                   legacy_txt_runtime:Any=None,
                                   expected_legacy_txt_runtime_identity:dict[str,Any]|None=None)->dict[str,Any]:
    """Create one isolated experimental workspace; never overwrite a target.

    Uses the snapshot module's existing nonblocking locks and no nested locking.
    Caller-supplied legacy TXT callbacks are disabled: callback identity and
    file hashes do not prove the executable code or its behavior. A source/text
    match is not a human review or source authenticity proof.
    If interrupted, the exclusive target remains and blocks automatic retries.
    """
    if not isinstance(expected_book_id,str) or not expected_book_id.strip():raise ValueError('book identity required')
    if legacy_txt_runtime is not None or expected_legacy_txt_runtime_identity is not None:
        raise VerifiedWorkspaceIndexError('legacy TXT runtime injection is disabled pending an independently authenticated runtime')
    root=workspace._plain_directory(book_root,'book_root');wd=workspace._plain_directory(workdir,'workdir')
    if wd!=root/'work.huashuo':raise workspace.WorkspaceSnapshotError('workdir must be the book root work.huashuo directory')
    target_raw=Path(experimental_workspace).expanduser()
    if not target_raw.is_absolute() or not target_raw.name or target_raw.name in ('.','..'):
        raise VerifiedWorkspaceIndexError('experimental workspace must be a new absolute path')
    parent=workspace._plain_directory(target_raw.parent,'experimental workspace parent');target=parent/target_raw.name
    if target.is_relative_to(root) or root.is_relative_to(target):raise VerifiedWorkspaceIndexError('experimental workspace must be outside the book root')
    if target.exists() or target.is_symlink():raise VerifiedWorkspaceIndexError('experimental workspace already exists; overwrite forbidden')
    with workspace._book_level_locks(root,wd):
        paths={'book_manifest':root/'book.json','text':wd/'text.txt','script':wd/'script.huaben.jsonl','cast':wd/'cast.json'}
        contents={};fingerprints={}
        contents['book_manifest'],fingerprints['book_manifest']=workspace._read_regular(paths['book_manifest'],root=root,label='book_manifest')
        manifest=_load_json(contents['book_manifest'])
        if not isinstance(manifest,dict) or manifest.get('book_id')!=expected_book_id:raise VerifiedWorkspaceIndexError('book manifest identity mismatch')
        fmt=manifest.get('format')
        if fmt not in ('.txt','.docx'):raise VerifiedWorkspaceIndexError('unsupported source format')
        paths['source_docx']=root/('original'+fmt)
        for name in ('source_docx','text','script','cast'):
            contents[name],fingerprints[name]=workspace._read_regular(paths[name],root=root,label=name)
        source=contents['source_docx'];source_sha=_sha(source)
        if manifest.get('source_sha256')!=source_sha:raise VerifiedWorkspaceIndexError('source hash differs from book manifest')
        try:text=contents['text'].decode('utf8',errors='strict')
        except UnicodeError as exc:raise VerifiedWorkspaceIndexError('text is not UTF-8') from exc
        rows=[_load_json(line) for line in contents['script'].splitlines() if line.strip()];cast=_load_json(contents['cast'])
        if not isinstance(cast,dict) or not rows or not isinstance(rows[0],dict):raise VerifiedWorkspaceIndexError('invalid script/cast')
        text_sha=_sha(contents['text'])
        if rows[0].get('type')!='huaben' or rows[0].get('text')!='text.txt' or rows[0].get('text_sha256')!=text_sha:
            raise VerifiedWorkspaceIndexError('script header does not bind text')
        if fmt=='.txt':
            parsed=local_txt_parser.parse_local_txt_bytes(source,source_name='original.txt')
            if parsed['normalized_text']!=text or parsed['text_sha256']!=text_sha:raise VerifiedWorkspaceIndexError('TXT reparse does not match workdir text')
            parser_evidence=parsed['parser_evidence']
        else:
            try:import local_docx_legacy_parser
            except ImportError as exc:raise VerifiedWorkspaceIndexError('DOCX parser dependency unavailable') from exc
            parsed=local_docx_legacy_parser.parse_legacy_docx_bytes(source,source_name='original.docx',expected_legacy_text_sha256=text_sha)
            if parsed.get('historical_compatibility')!='hash_match' or parsed.get('normalized_text')!=text:
                raise VerifiedWorkspaceIndexError('legacy DOCX reparse does not match workdir text')
            parser_evidence=parsed['parser_evidence']
        snapshot={'book_id':expected_book_id,'source_sha256':source_sha,'text_sha256':text_sha,
                  'script_sha256':chapters.object_sha256(rows),'cast_sha256':chapters.object_sha256(cast)}
        index=chapters.build_index(book_id=expected_book_id,source_bytes=source,normalized_text=text,
                                   script_rows=rows,cast=cast,trusted_snapshot=snapshot)
        _check_fingerprints(paths,fingerprints)
        try:target.mkdir(mode=0o700)
        except FileExistsError as exc:raise VerifiedWorkspaceIndexError('experimental workspace already exists; overwrite forbidden') from exc
        _write_new(target/'started.json',{'version':1,'status':'in_progress','book_id':expected_book_id})
        persisted.store_chapter_index(index,target/'index',trusted_snapshot=snapshot,expected_index_sha256=index.index_sha256)
        _check_fingerprints(paths,fingerprints)
        evidence={'version':1,'status':'source_reparse_matched','human_confirmed':False,
                  'authentication':'book metadata and script/cast hashes computed during this locked read; not independent authentication',
                  'source_format':fmt,'snapshot':snapshot,'index_sha256':index.index_sha256,
                  'parser_evidence':parser_evidence,'chapter_count':len(index.chapters)}
        _write_new(target/'evidence.json',evidence)
        # Exclusive completion marker; a crash before this point leaves an
        # incomplete target which must not be automatically resumed/overwritten.
        _write_new(target/'complete.json',{'version':1,'status':'complete','evidence_sha256':_sha(chapters._json(evidence))})
        return {'status':'complete','workspace':str(target),'index_workspace':str(target/'index'),
                'snapshot':snapshot,'index_sha256':index.index_sha256,'evidence':evidence}
