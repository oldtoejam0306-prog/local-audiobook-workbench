"""Local, book-scoped run registry and immutable prior-memory store.

This module is filesystem-only. It does not read an index, dispatch a model,
render audio, or trust paths stored in job metadata.
"""
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from real_book_auto_voice import _selection_lock as _os_book_lock

BOOK = re.compile(r'book-[0-9a-f]{20}\Z')
CHAPTER = re.compile(r'c\d+(?:\.p\d+(?:\.\d+)?)?\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')
MAX_POINTER_BYTES = 32 * 1024 * 1024


class JobStoreError(ValueError):
    pass


class MemoryConflictError(JobStoreError):
    pass


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _is_hash(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None


def _chapter_key(value):
    match = re.fullmatch(r'c(\d+)(?:\.p(\d+)(?:\.(\d+))?)?', value)
    if not match:
        raise JobStoreError('invalid chapter identity')
    return tuple(int(part or 0) for part in match.groups())


def _atomic_write(path, raw):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _write_new(path, raw):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb') as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != raw:
            raise JobStoreError('immutable file collision')


@contextmanager
def _book_lock(root, book_id):
    # Separate lock tree from selected-voice and job paths. No provider work
    # occurs while this short registry/memory lock is held.
    with _os_book_lock(Path(root) / '.job-store-locks', book_id):
        yield


def _identity(book_id, index_sha256, chapters, voice_fingerprint):
    if not isinstance(book_id, str) or not BOOK.fullmatch(book_id):
        raise JobStoreError('invalid book identity')
    if not _is_hash(index_sha256) or not _is_hash(voice_fingerprint):
        raise JobStoreError('invalid index or voice fingerprint')
    if not isinstance(chapters, list) or not chapters:
        raise JobStoreError('chapters required')
    normalized=[]; seen=set()
    for item in chapters:
        if (not isinstance(item, dict) or set(item) != {'id', 'source_sha256'}
                or not isinstance(item['id'], str) or not CHAPTER.fullmatch(item['id'])
                or not _is_hash(item['source_sha256']) or item['id'] in seen):
            raise JobStoreError('invalid chapter specification')
        seen.add(item['id']); normalized.append({'id': item['id'], 'source_sha256': item['source_sha256']})
    return {'version': 1, 'book_id': book_id, 'index_sha256': index_sha256,
            'chapters': normalized, 'voice_fingerprint': voice_fingerprint}


def _job_id(identity):
    return _hash(_bytes(identity))


def _job_paths(root, book_id, job_id):
    if not _is_hash(job_id):
        raise JobStoreError('invalid job id')
    book_root = Path(root) / 'runs' / book_id
    # Keep Windows paths short; the full digest remains in manifest and all
    # current/history pointers, and is checked against this directory prefix.
    job_root = book_root / job_id[:20]
    return book_root, job_root, job_root / 'sequence'


def _derived_path(root, *parts):
    path=Path(root)
    for part in parts:
        path=path/part
        if path.is_symlink():
            raise JobStoreError('store path contains a symlink')
    return path


def _load_job(root, book_id, job_id, expected_sha=None):
    book_root, job_root, sequence_root = _job_paths(root, book_id, job_id)
    _derived_path(root,'runs',book_id,job_id[:20])
    if not job_root.is_dir():
        raise JobStoreError('registered job directory missing or redirected')
    manifest_path = job_root / 'job.json'
    if manifest_path.is_symlink():
        raise JobStoreError('job manifest redirected')
    raw = manifest_path.read_bytes()
    if expected_sha and _hash(raw) != expected_sha:
        raise JobStoreError('job manifest hash mismatch')
    manifest = json.loads(raw)
    identity = {k: manifest.get(k) for k in ('version', 'book_id', 'index_sha256', 'chapters', 'voice_fingerprint')}
    if manifest.get('job_id') != job_id or identity['book_id'] != book_id or _job_id(identity) != job_id:
        raise JobStoreError('job manifest identity mismatch')
    if sequence_root.is_symlink():
        raise JobStoreError('sequence directory redirected')
    return manifest, job_root, sequence_root, _hash(raw)


def _read_book_registry(root, book_id):
    book_root, _, _ = _job_paths(root, book_id, '0' * 64)
    _derived_path(root,'runs',book_id)
    history_path = book_root / 'jobs.json'
    if not history_path.exists():
        raise JobStoreError('job history missing')
    if history_path.is_symlink():
        raise JobStoreError('job history redirected')
    history = json.loads(history_path.read_bytes())
    if history.get('version') != 1 or history.get('book_id') != book_id or not isinstance(history.get('jobs'), list):
        raise JobStoreError('job history identity or schema invalid')
    seen=set()
    for row in history['jobs']:
        if not isinstance(row, dict) or not _is_hash(row.get('job_id')) or row['job_id'] in seen or not _is_hash(row.get('job_sha256')):
            raise JobStoreError('job history entry invalid')
        seen.add(row['job_id'])
        _load_job(root, book_id, row['job_id'], row['job_sha256'])
    current_path = book_root / 'current-job.json'
    if not current_path.is_file():
        raise JobStoreError('current job pointer missing')
    if current_path.is_symlink():
        raise JobStoreError('current job pointer redirected')
    current = json.loads(current_path.read_bytes())
    if current.get('book_id') != book_id or current.get('job_id') not in seen:
        raise JobStoreError('current job pointer identity invalid')
    current_row=next((r for r in history['jobs'] if r['job_id']==current['job_id']),None)
    if not current_row or current.get('job_sha256')!=current_row['job_sha256']:
        raise JobStoreError('current job pointer hash mismatch')
    return book_root, history, current


def begin_job(root, book_id, index_sha256, chapters, voice_fingerprint):
    """Create/reuse a deterministic immutable job and atomically select it."""
    root=Path(root); identity=_identity(book_id,index_sha256,chapters,voice_fingerprint)
    job_id=_job_id(identity); book_root,job_root,sequence_root=_job_paths(root,book_id,job_id)
    with _book_lock(root,book_id):
        _derived_path(root,'runs',book_id)
        book_root.mkdir(parents=True,exist_ok=True)
        manifest={**identity,'job_id':job_id}
        manifest_raw=_bytes(manifest); manifest_sha=_hash(manifest_raw)
        if job_root.exists():
            if job_root.is_symlink():
                raise JobStoreError('job directory redirected')
            _,_,_,verified_sha=_load_job(root,book_id,job_id,manifest_sha)
            if verified_sha!=manifest_sha:
                raise JobStoreError('job manifest differs from deterministic input')
        else:
            job_root.mkdir(parents=True,exist_ok=False)
            _write_new(job_root/'job.json',manifest_raw)
            sequence_root.mkdir()
        history_path=book_root/'jobs.json'
        if history_path.exists():
            _,history,_=_read_book_registry(root,book_id)
        else:
            history={'version':1,'book_id':book_id,'jobs':[]}
        rows=[r for r in history['jobs'] if r['job_id']!=job_id]
        rows.append({'job_id':job_id,'job_sha256':manifest_sha})
        history={'version':1,'book_id':book_id,'jobs':rows}
        _atomic_write(history_path,_bytes(history))
        current={'version':1,'book_id':book_id,'job_id':job_id,'job_sha256':manifest_sha}
        _atomic_write(book_root/'current-job.json',_bytes(current))
    return {'job_id':job_id,'job_root':str(job_root),'sequence_root':str(sequence_root)}


def _plan_for_job(sequence_root, book_id, manifest):
    _derived_path(sequence_root,book_id)
    plan_path=Path(sequence_root)/book_id/'plan.json'
    if not plan_path.exists():
        return None
    if plan_path.is_symlink():
        raise JobStoreError('sequence plan redirected')
    plan=json.loads(plan_path.read_bytes())
    if (plan.get('book_id')!=book_id or plan.get('index_sha256')!=manifest['index_sha256']
            or plan.get('chapters')!=manifest['chapters']
            or plan.get('voice_bindings_sha256')!=manifest['voice_fingerprint']
            or plan.get('human_reviewed') is not False):
        raise JobStoreError('sequence plan differs from registered job')
    return plan


def _valid_render_done(sequence_root, book_id, chapter_id, manifest, plan):
    _derived_path(sequence_root,book_id,chapter_id)
    path=Path(sequence_root)/book_id/chapter_id/'render.done.json'
    if not path.exists():
        return False
    if path.is_symlink():
        raise JobStoreError('render receipt redirected')
    done=json.loads(path.read_bytes())
    identity=done.get('identity',{})
    expected_plan_hash=_hash(_bytes(plan)) if plan is not None else None
    if (done.get('status')!='ok' or done.get('human_reviewed') is not False
            or identity.get('book_id')!=book_id or identity.get('chapter_id')!=chapter_id
            or identity.get('stage')!='render'
            or (expected_plan_hash is not None and identity.get('plan_sha256')!=expected_plan_hash)
            or not isinstance(done.get('artifact'),dict)
            or _hash(_bytes(done['artifact']))!=done.get('artifact_sha256')):
        raise JobStoreError('render receipt identity or artifact hash invalid')
    return True


def find_sequence_root(root, book_id, chapter_id=None):
    """Return the selected job's sequence root or the latest completed prior job.

    Returns root/sequence only when no per-book job store exists or no matching
    run has a completed render. A corrupt existing store raises and never falls
    back to a potentially stale legacy path.
    """
    if not isinstance(book_id,str) or not BOOK.fullmatch(book_id):
        raise JobStoreError('invalid book identity')
    if chapter_id is not None:
        _chapter_key(chapter_id)
    root=Path(root); book_root=root/'runs'/book_id
    if not book_root.exists():
        return str(root/'sequence')
    _,history,current=_read_book_registry(root,book_id)
    current_job,current_root,current_sequence,_=_load_job(root,book_id,current['job_id'],current['job_sha256'])
    plan=_plan_for_job(current_sequence,book_id,current_job)
    planned={c['id'] for c in current_job['chapters']}
    if chapter_id is None or chapter_id in planned:
        return str(current_sequence)
    for row in reversed(history['jobs']):
        manifest,_,sequence_root,_=_load_job(root,book_id,row['job_id'],row['job_sha256'])
        plan=_plan_for_job(sequence_root,book_id,manifest)
        if plan is None:
            chapter_root=sequence_root/book_id
            if chapter_root.exists() and any(chapter_root.glob('*/render.done.json')):
                raise JobStoreError('registered run has render receipts but no sequence plan')
            continue
        planned={c['id'] for c in manifest['chapters']}
        if chapter_id in planned and _valid_render_done(sequence_root,book_id,chapter_id,manifest,plan):
            return str(sequence_root)
    return str(root/'sequence')


def _pointer_bytes(root, pointer, *, label):
    if not isinstance(pointer,dict) or not isinstance(pointer.get('path'),str) or not _is_hash(pointer.get('sha256')):
        raise JobStoreError(label+' pointer invalid')
    original=Path(pointer['path'])
    if original.is_symlink():
        raise JobStoreError(label+' pointer is a symlink')
    path=original.resolve()
    root_resolved=Path(root).resolve()
    if not path.is_relative_to(root_resolved) or not path.is_file():
        raise JobStoreError(label+' pointer outside runtime root or missing')
    raw=path.read_bytes()
    if len(raw)>MAX_POINTER_BYTES or _hash(raw)!=pointer['sha256']:
        raise JobStoreError(label+' pointer bytes/hash mismatch')
    return raw


def _memory_partition(root, book_id, index_sha256, chapter_id):
    return _derived_path(root,'memory',book_id,index_sha256[:20],chapter_id)


def _registered_chapter_sources(root, book_id, index_sha256):
    book_root=root/'runs'/book_id
    if not book_root.exists():
        raise JobStoreError('book job registry not found')
    _,history,_=_read_book_registry(root,book_id)
    found=set()
    for row in history['jobs']:
        manifest,_,_,_=_load_job(root,book_id,row['job_id'],row['job_sha256'])
        if manifest['index_sha256']==index_sha256:
            found.update((item['id'],item['source_sha256']) for item in manifest['chapters'])
    return found


def publish_memory(root, book, index_sha, chapter, memory_artifact):
    """Copy a verified memory+auto artifact into an immutable internal version."""
    root=Path(root)
    if not isinstance(book,str) or not BOOK.fullmatch(book) or not _is_hash(index_sha):
        raise JobStoreError('invalid memory identity')
    if not isinstance(chapter,dict) or not isinstance(chapter.get('id'),str) or not CHAPTER.fullmatch(chapter['id']) or not _is_hash(chapter.get('source_sha256')):
        raise JobStoreError('invalid memory chapter source')
    _chapter_key(chapter['id'])
    chapter={'id':chapter['id'],'source_sha256':chapter['source_sha256']}
    if (chapter['id'],chapter['source_sha256']) not in _registered_chapter_sources(root,book,index_sha):
        raise JobStoreError('memory source is not present in a registered job')
    if not isinstance(memory_artifact,dict):
        raise JobStoreError('memory artifact pointer required')
    memory_raw=_pointer_bytes(root,memory_artifact,label='memory')
    auto_pointer=memory_artifact.get('auto')
    external_auto_raw=_pointer_bytes(root,auto_pointer,label='auto')
    memory_doc=json.loads(memory_raw);auto_doc=json.loads(external_auto_raw)
    if (memory_doc.get('book_id')!=book or memory_doc.get('chapter_id')!=chapter['id']
            or memory_doc.get('source_sha256')!=chapter['source_sha256']
            or memory_doc.get('human_reviewed') is not False
            or auto_doc.get('book_id')!=book or auto_doc.get('chapter_id')!=chapter['id']
            or auto_doc.get('human_reviewed') is not False):
        raise JobStoreError('memory or auto artifact identity mismatch')
    entities=auto_doc.get('entities',[]);unresolved=auto_doc.get('unresolved',[])
    blocked=auto_doc.get('blocked',False)
    if not isinstance(entities,list) or not isinstance(unresolved,list) or type(blocked) is not bool:
        raise JobStoreError('auto memory semantics invalid')
    semantic_entities=[]
    for entity in entities:
        if (not isinstance(entity,dict) or not isinstance(entity.get('entity_id'),str)
                or not isinstance(entity.get('name'),str) or not isinstance(entity.get('labels'),list)
                or not all(isinstance(label,str) and label for label in entity['labels'])):
            raise JobStoreError('auto entity identity/labels invalid')
        semantic={'entity_id':entity['entity_id'],'name':entity['name'],'labels':entity['labels']}
        if isinstance(entity.get('chapter_name'),str):
            semantic['chapter_name']=entity['chapter_name']
        semantic_entities.append(semantic)
    # Preserve only narrative/entity semantics. Runtime bindings, selected
    # voices, design pointers and output paths remain in the original artifact.
    semantic_auto={'book_id':book,'chapter_id':chapter['id'],'entities':semantic_entities,
        'unresolved':unresolved,'blocked':blocked,'human_reviewed':False}
    auto_raw=_bytes(semantic_auto)
    memory_sha=_hash(memory_raw);auto_sha=_hash(auto_raw)
    version_input={'version':1,'book_id':book,'index_sha256':index_sha,'chapter':chapter,
                   'memory_sha256':memory_sha,'auto_sha256':auto_sha}
    version_id=_hash(_bytes(version_input))
    chapter_root=_memory_partition(root,book,index_sha,chapter['id'])
    partition=_derived_path(root,'memory',book,index_sha[:20],chapter['id'],chapter['source_sha256'][:20])
    with _book_lock(root,book):
        chapter_current_path=chapter_root/'current.json'
        existing_sources=[]
        if chapter_current_path.is_symlink():
            raise JobStoreError('chapter memory current pointer redirected')
        if chapter_current_path.is_file():
            chapter_current=json.loads(chapter_current_path.read_bytes())
            if (chapter_current.get('book_id')!=book or chapter_current.get('index_sha256')!=index_sha
                    or chapter_current.get('chapter_id')!=chapter['id']
                    or not isinstance(chapter_current.get('source_hashes'),list)
                    or not isinstance(chapter_current.get('conflicts'),list)):
                raise JobStoreError('chapter memory current identity invalid')
            existing_sources=chapter_current['source_hashes']
            if (chapter_current.get('current_source_sha256') not in existing_sources
                    or not all(_is_hash(x) for x in existing_sources)
                    or len(existing_sources)!=len(set(existing_sources))):
                raise JobStoreError('chapter memory source set invalid')
            disk_sources=[]
            for child in chapter_root.iterdir():
                if child.name=='current.json':
                    continue
                if child.is_symlink() or not child.is_dir() or len(child.name)!=20 or not re.fullmatch(r'[0-9a-f]{20}',child.name):
                    raise JobStoreError('chapter memory source directory invalid')
                disk_sources.append(child.name)
            if set(disk_sources)!={value[:20] for value in existing_sources}:
                raise JobStoreError('chapter memory source pointer differs from stored directories')
        else:
            for child in chapter_root.iterdir() if chapter_root.exists() else []:
                if child.is_symlink():
                    raise JobStoreError('chapter memory source redirected')
                if child.is_dir():
                    existing_sources.append(child.name)
            if not all(len(x)==20 and re.fullmatch(r'[0-9a-f]{20}',x) for x in existing_sources):
                raise JobStoreError('chapter memory source directory invalid')
        partition.mkdir(parents=True,exist_ok=True)
        versions=_derived_path(root,'memory',book,index_sha[:20],chapter['id'],chapter['source_sha256'][:20],'versions')
        versions.mkdir(parents=True,exist_ok=True)
        version_dir=versions/version_id[:20]
        version_dir.mkdir(parents=True,exist_ok=True)
        if version_dir.is_symlink():
            raise JobStoreError('memory version directory redirected')
        _write_new(version_dir/'memory.json',memory_raw)
        _write_new(version_dir/'auto.json',auto_raw)
        stored={'version':1,'version_id':version_id,'book_id':book,'index_sha256':index_sha,
            'chapter':chapter,'memory':{'path':str(version_dir/'memory.json'),'sha256':memory_sha},
            'auto':{'path':str(version_dir/'auto.json'),'sha256':auto_sha}}
        record_raw=_bytes(stored)
        _write_new(version_dir/'record.json',record_raw)
        current_path=partition/'current.json'
        old=None
        if current_path.is_symlink():
            raise JobStoreError('memory current pointer redirected')
        if current_path.exists():
            old=json.loads(current_path.read_bytes())
            _validate_current_memory(root,partition,old,book,index_sha,chapter)
        versions_seen=list(old.get('versions',[])) if old else []
        if version_id not in versions_seen:
            versions_seen.append(version_id)
        conflict = len(versions_seen)>1
        conflicts=list(old.get('conflicts',[])) if old else []
        if conflict and not any(set(row.get('versions',[]))==set(versions_seen) for row in conflicts if isinstance(row,dict)):
            conflicts.append({'type':'same_chapter_source_multiple_versions','versions':versions_seen[:]})
        current={'version':1,'book_id':book,'index_sha256':index_sha,'chapter':chapter,
            'current_version':version_id,'versions':versions_seen,'conflicts':conflicts,
            'human_reviewed':False}
        _atomic_write(current_path,_bytes(current))
        source_hashes=list(existing_sources)
        for item in (chapter['source_sha256'],):
            if item not in source_hashes:
                source_hashes.append(item)
        source_conflict=len(source_hashes)>1
        source_conflicts=[]
        if source_conflict:
            source_conflicts=[{'type':'same_chapter_multiple_source_hashes','source_hashes':source_hashes[:]}]
        chapter_current={'version':1,'book_id':book,'index_sha256':index_sha,'chapter_id':chapter['id'],
            'current_source_sha256':chapter['source_sha256'],'source_hashes':source_hashes,
            'conflicts':source_conflicts,'human_reviewed':False}
        _atomic_write(chapter_current_path,_bytes(chapter_current))
    return {'path':str(current_path),'sha256':_hash(current_path.read_bytes()),
            'version_id':version_id,'version_path':str(version_dir/'record.json'),
            'conflict':conflict or source_conflict,'version_conflict':conflict,
            'source_conflict':source_conflict,'human_reviewed':False}


def _validate_current_memory(root, partition, current, book, index_sha, chapter):
    if (current.get('book_id')!=book or current.get('index_sha256')!=index_sha
            or current.get('chapter')!=chapter or not isinstance(current.get('versions'),list)
            or current.get('current_version') not in current.get('versions',[])
            or not isinstance(current.get('conflicts'),list)):
        raise JobStoreError('memory current pointer identity invalid')
    if len(current['versions'])>1 and not current['conflicts']:
        raise MemoryConflictError('divergent memory versions lack conflict marker')
    for version_id in current['versions']:
        if not _is_hash(version_id):
            raise JobStoreError('memory version id invalid')
        version_dir=_derived_path(partition,'versions',version_id[:20])
        record=json.loads((version_dir/'record.json').read_bytes())
        memory_raw=(version_dir/'memory.json').read_bytes();auto_raw=(version_dir/'auto.json').read_bytes()
        expected={'version':1,'book_id':book,'index_sha256':index_sha,'chapter':chapter,
                  'memory_sha256':_hash(memory_raw),'auto_sha256':_hash(auto_raw)}
        if (record.get('version_id')!=version_id or any(record.get(k)!=v for k,v in expected.items()
                if k not in ('memory_sha256','auto_sha256'))
                or record.get('memory',{}).get('sha256')!=expected['memory_sha256']
                or record.get('auto',{}).get('sha256')!=expected['auto_sha256']):
            raise JobStoreError('memory immutable version hash invalid')
        version_input={'version':1,'book_id':book,'index_sha256':index_sha,'chapter':chapter,
            'memory_sha256':expected['memory_sha256'],'auto_sha256':expected['auto_sha256']}
        if _hash(_bytes(version_input))!=version_id:
            raise JobStoreError('memory version identifier mismatch')
        if (Path(record.get('memory',{}).get('path','')).resolve()!=(version_dir/'memory.json').resolve()
                or Path(record.get('auto',{}).get('path','')).resolve()!=(version_dir/'auto.json').resolve()):
            raise JobStoreError('memory immutable pointer path mismatch')


def prior_memories(root, book, index_sha, target_chapter):
    """Return verified sequence prior artifacts; conflicts are explicit errors."""
    root=Path(root)
    if not isinstance(book,str) or not BOOK.fullmatch(book) or not _is_hash(index_sha):
        raise JobStoreError('invalid prior memory identity')
    target_key=_chapter_key(target_chapter)
    with _book_lock(root,book):
        return _prior_memories_locked(root,book,index_sha,target_chapter,target_key)


def _prior_memories_locked(root,book,index_sha,target_chapter,target_key):
    if (root/'runs'/book).exists():
        _registered_chapter_sources(root,book,index_sha)  # validates all job pointers
    memory_root=_derived_path(root,'memory',book,index_sha[:20])
    if not memory_root.exists():
        return []
    if memory_root.is_symlink():
        raise JobStoreError('memory book path redirected')
    registered_sources=_registered_chapter_sources(root,book,index_sha)
    rows=[]
    for chapter_dir in memory_root.iterdir():
        if chapter_dir.is_symlink() or not chapter_dir.is_dir():
            raise JobStoreError('unexpected memory chapter entry')
        cid=chapter_dir.name;key=_chapter_key(cid)
        if key>=target_key:
            continue
        children=list(chapter_dir.iterdir())
        if any(p.is_symlink() for p in children):
            raise JobStoreError('memory source entry redirected')
        source_dirs=[p for p in children if p.is_dir()]
        if len(source_dirs)>1:
            raise MemoryConflictError('same chapter has multiple source hashes')
        if not source_dirs:
            continue
        chapter_current_path=chapter_dir/'current.json'
        if chapter_current_path.is_symlink() or not chapter_current_path.is_file():
            raise JobStoreError('chapter memory current pointer missing or redirected')
        chapter_current=json.loads(chapter_current_path.read_bytes())
        if (chapter_current.get('book_id')!=book or chapter_current.get('index_sha256')!=index_sha
                or chapter_current.get('chapter_id')!=cid or not isinstance(chapter_current.get('source_hashes'),list)
                or not isinstance(chapter_current.get('conflicts'),list)):
            raise JobStoreError('chapter memory pointer identity mismatch')
        if chapter_current['conflicts'] or len(chapter_current['source_hashes'])>1:
            raise MemoryConflictError('same chapter has multiple source hashes')
        source_dir=source_dirs[0]
        if len(source_dir.name)!=20 or not re.fullmatch(r'[0-9a-f]{20}',source_dir.name) or source_dir.is_symlink():
            raise JobStoreError('memory source directory invalid')
        current_path=source_dir/'current.json'
        if current_path.is_symlink() or not current_path.is_file():
            raise JobStoreError('memory current pointer missing')
        current=json.loads(current_path.read_bytes())
        chapter=current.get('chapter')
        if (not isinstance(chapter,dict) or chapter.get('id')!=cid or not _is_hash(chapter.get('source_sha256'))
                or chapter['source_sha256'][:20]!=source_dir.name
                or chapter_current.get('current_source_sha256')!=chapter['source_sha256']
                or chapter_current.get('source_hashes')!=[chapter['source_sha256']]):
            raise JobStoreError('memory source directory and current pointer mismatch')
        if (cid,chapter['source_sha256']) not in registered_sources:
            raise JobStoreError('prior memory source is not registered for this book/index')
        if current.get('conflicts'):
            raise MemoryConflictError('same chapter/source has divergent memory versions')
        _validate_current_memory(root,source_dir,current,book,index_sha,chapter)
        version_id=current['current_version'];version_dir=_derived_path(source_dir,'versions',version_id[:20])
        record_path=version_dir/'record.json';record_raw=record_path.read_bytes();record=json.loads(record_raw)
        memory_pointer=record['memory'];auto_pointer=record['auto']
        for pointer,label,expected in ((memory_pointer,'memory','memory.json'),(auto_pointer,'auto','auto.json')):
            expected_path=(version_dir/expected).resolve()
            if Path(pointer.get('path','')).resolve()!=expected_path:
                raise JobStoreError(label+' stored pointer path mismatch')
            _pointer_bytes(root,pointer,label=label)
        memory_doc=json.loads(Path(memory_pointer['path']).read_bytes())
        auto_doc=json.loads(Path(auto_pointer['path']).read_bytes())
        if (memory_doc.get('book_id')!=book or memory_doc.get('chapter_id')!=cid
                or memory_doc.get('source_sha256')!=chapter['source_sha256']
                or auto_doc.get('book_id')!=book or auto_doc.get('chapter_id')!=cid):
            raise JobStoreError('stored prior memory identity mismatch')
        pointer={'path':memory_pointer['path'],'sha256':memory_pointer['sha256'],
                 'auto':{'path':auto_pointer['path'],'sha256':auto_pointer['sha256']}}
        rows.append({'chapter':chapter,'artifacts':{'memory':pointer},'human_reviewed':False})
    rows.sort(key=lambda row:_chapter_key(row['chapter']['id']))
    return rows
