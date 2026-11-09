"""Book-scoped immutable operator notes, separate from model/profile facts."""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from real_book_auto_voice import _selection_lock as _os_book_lock

BOOK=re.compile(r'book-[0-9a-f]{20}\Z')
CHAPTER=re.compile(r'c\d+(?:\.p\d+(?:\.\d+)?)?\Z')
HASH=re.compile(r'[0-9a-f]{64}\Z')
KINDS={'reader_correction','summary_correction','pronunciation'}


class CorrectionMemoryError(ValueError):
    pass


class CorrectionSourceConflict(CorrectionMemoryError):
    pass


def _bytes(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def _sha(raw):return hashlib.sha256(raw).hexdigest()


def _valid_hash(value):return isinstance(value,str) and HASH.fullmatch(value) is not None


def _chapter_key(value):
    match=re.fullmatch(r'c(\d+)(?:\.p(\d+)(?:\.(\d+))?)?',value)
    if not match:raise CorrectionMemoryError('invalid chapter identity')
    return tuple(int(part or 0) for part in match.groups())


def _atomic(path,raw):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=str(path.parent));temp=Path(name)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        if temp.exists():temp.unlink()


def _immutable(path,raw):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    try:
        with path.open('xb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_bytes()!=raw:raise CorrectionMemoryError('immutable correction version collision')


def _paths(root,book_id,chapter_id,source_sha256):
    chapter_root=Path(root)/'corrections'/book_id/chapter_id
    source_root=chapter_root/source_sha256[:20]
    return chapter_root,source_root


def _source_directories(chapter_root):
    result=[]
    for child in chapter_root.iterdir():
        if child.name=='current.json':continue
        if child.is_symlink() or not child.is_dir() or not re.fullmatch(r'[0-9a-f]{20}',child.name):
            raise CorrectionMemoryError('correction source entry invalid')
        result.append(child.name)
    return result


def _slot_id(kind,character_name):
    return _sha(_bytes({'kind':kind,'character_name':character_name}))[:24]


def _verify_note_version(source_root,version_id,book,chapter,source_sha):
    if not _valid_hash(version_id):raise CorrectionMemoryError('correction version ID invalid')
    versions=source_root/'versions'
    if versions.is_symlink():raise CorrectionMemoryError('correction versions path redirected')
    version_dir=source_root/'versions'/version_id[:20]
    if version_dir.is_symlink():raise CorrectionMemoryError('correction version path redirected')
    note_path=version_dir/'note.json'
    if note_path.is_symlink():raise CorrectionMemoryError('correction note record redirected')
    raw=note_path.read_bytes();record=json.loads(raw)
    fields=('version','book_id','chapter_id','source_sha256','kind','character_name','note','status')
    version_input={key:record.get(key) for key in fields}
    if (_sha(raw)!=_sha(_bytes(record)) or record.get('version_id')!=version_id
            or record.get('book_id')!=book or record.get('chapter_id')!=chapter
            or record.get('source_sha256')!=source_sha or record.get('version')!=1
            or record.get('kind') not in KINDS or not isinstance(record.get('note'),str)
            or not record['note'].strip() or len(record['note'])>2000
            or (record.get('character_name') is not None and not isinstance(record.get('character_name'),str))
            or record.get('status')!='operator_note_not_identity_approval'
            or _sha(_bytes(version_input))!=version_id):
        raise CorrectionMemoryError('correction immutable record integrity failure')
    return record


def _source_identity(payload):
    if not isinstance(payload,dict):raise CorrectionMemoryError('verified chapter payload required')
    book=payload.get('book_id');chapter=payload.get('chapter_id');text=payload.get('text')
    if not isinstance(book,str) or not BOOK.fullmatch(book) or not isinstance(chapter,str) or not CHAPTER.fullmatch(chapter):
        raise CorrectionMemoryError('invalid book/chapter identity')
    _chapter_key(chapter)
    if not isinstance(text,str) or not text.strip():raise CorrectionMemoryError('chapter source text required')
    actual=_sha(text.encode('utf-8'));provided=payload.get('source_sha256')
    if provided is not None and (not _valid_hash(provided) or provided!=actual):
        raise CorrectionMemoryError('verified source hash mismatch')
    return book,chapter,actual


def save_note(root,payload,note,*,kind='reader_correction',character_name=None):
    """Save or edit a source-bound human note; never changes source/model data."""
    book,chapter,source_sha=_source_identity(payload)
    if kind not in KINDS:raise CorrectionMemoryError('invalid correction kind')
    if not isinstance(note,str) or not note.strip() or len(note)>2000:
        raise CorrectionMemoryError('note must contain 1-2000 characters')
    if character_name is not None and (not isinstance(character_name,str) or not character_name.strip() or len(character_name)>80):
        raise CorrectionMemoryError('invalid character name')
    root=Path(root)
    slot={'kind':kind,'character_name':character_name}
    slot_id=_sha(_bytes(slot))[:24]
    chapter_root,source_root=_paths(root,book,chapter,source_sha)
    for path in (root/'corrections',root/'corrections'/book,chapter_root,source_root):
        if path.is_symlink():raise CorrectionMemoryError('correction storage path redirected')
    version_input={'version':1,'book_id':book,'chapter_id':chapter,'source_sha256':source_sha,
        'kind':kind,'character_name':character_name,'note':note,
        'status':'operator_note_not_identity_approval'}
    version_id=_sha(_bytes(version_input))
    with _os_book_lock(root/'.correction-memory-locks',book):
        chapter_root.mkdir(parents=True,exist_ok=True)
        current_path=chapter_root/'current.json'
        if current_path.is_symlink():raise CorrectionMemoryError('chapter current pointer redirected')
        source_dirs=[]
        for child in chapter_root.iterdir():
            if child.name=='current.json':continue
            if child.is_symlink() or not child.is_dir() or not re.fullmatch(r'[0-9a-f]{20}',child.name):
                raise CorrectionMemoryError('correction source entry invalid')
            source_dirs.append(child.name)
        existing_current=json.loads(current_path.read_bytes()) if current_path.is_file() else None
        source_hashes=list(existing_current.get('source_hashes',[])) if existing_current else []
        if existing_current and (existing_current.get('book_id')!=book or existing_current.get('chapter_id')!=chapter
                or not isinstance(existing_current.get('conflicts'),list)):
            raise CorrectionMemoryError('chapter current identity invalid')
        source_dirs=_source_directories(chapter_root)
        if existing_current:
            if (not all(_valid_hash(value) for value in source_hashes)
                    or len(source_hashes)!=len(set(source_hashes))
                    or existing_current.get('current_source_sha256') not in source_hashes
                    or set(source_dirs)!={value[:20] for value in source_hashes}):
                raise CorrectionMemoryError('chapter current source set differs from stored directories')
        elif source_dirs:
            raise CorrectionMemoryError('correction source directory exists without current pointer')
        if source_sha not in source_hashes:source_hashes.append(source_sha)
        source_current_path=source_root/'current.json'
        if source_current_path.is_symlink():raise CorrectionMemoryError('source current pointer redirected')
        if not source_current_path.exists() and source_root.exists() and any(source_root.iterdir()):
            raise CorrectionMemoryError('correction source directory exists without current pointer')
        source_current=json.loads(source_current_path.read_bytes()) if source_current_path.is_file() else {
            'version':1,'book_id':book,'chapter_id':chapter,'source_sha256':source_sha,
            'slots':{},'events':[],'human_reviewed':False}
        if (source_current.get('book_id')!=book or source_current.get('chapter_id')!=chapter
                or source_current.get('source_sha256')!=source_sha or not isinstance(source_current.get('slots'),dict)
                or not isinstance(source_current.get('events'),list)):
            raise CorrectionMemoryError('source correction pointer identity invalid')
        if source_current_path.is_file():
            _read_source_notes(root,chapter_root,chapter,source_sha)
        versions=source_root/'versions';versions.mkdir(parents=True,exist_ok=True)
        version_dir=versions/version_id[:20];version_dir.mkdir(parents=True,exist_ok=True)
        if version_dir.is_symlink():raise CorrectionMemoryError('correction version redirected')
        version_doc={**version_input,'version_id':version_id}
        version_raw=_bytes(version_doc)
        _immutable(version_dir/'note.json',version_raw)
        old_version=source_current['slots'].get(slot_id)
        if old_version!=version_id:
            source_current['slots'][slot_id]=version_id
            source_current['events'].append({'slot_id':slot_id,'version_id':version_id})
        _atomic(source_current_path,_bytes(source_current))
        conflicts=[]
        if len(source_hashes)>1:
            conflicts=[{'type':'same_chapter_multiple_source_hashes','source_hashes':source_hashes[:]}]
        chapter_current={'version':1,'book_id':book,'chapter_id':chapter,
            'current_source_sha256':source_sha,'source_hashes':source_hashes,
            'conflicts':conflicts,'human_reviewed':False}
        _atomic(current_path,_bytes(chapter_current))
    pointer={'path':str(version_dir/'note.json'),'sha256':_sha(version_raw)}
    return {'record':version_doc,'pointer':pointer,'version_id':version_id,
        'reused':old_version==version_id,'conflict':len(source_hashes)>1,'human_reviewed':False}


def _read_source_notes(root,chapter_root,chapter_id,source_sha):
    source_root=chapter_root/source_sha[:20]
    if source_root.is_symlink() or not source_root.is_dir():raise CorrectionMemoryError('correction source path missing')
    current_path=source_root/'current.json'
    if current_path.is_symlink() or not current_path.is_file():raise CorrectionMemoryError('correction source pointer missing')
    current=json.loads(current_path.read_bytes())
    if (current.get('book_id')!=chapter_root.parent.name or current.get('chapter_id')!=chapter_id
            or current.get('source_sha256')!=source_sha or not isinstance(current.get('slots'),dict)
            or not isinstance(current.get('events'),list)):
        raise CorrectionMemoryError('correction source pointer identity mismatch')
    latest_events={}
    for event in current['events']:
        if (not isinstance(event,dict) or set(event)!={'slot_id','version_id'}
                or not isinstance(event.get('slot_id'),str) or not re.fullmatch(r'[0-9a-f]{24}',event['slot_id'])):
            raise CorrectionMemoryError('correction history event invalid')
        record=_verify_note_version(source_root,event['version_id'],chapter_root.parent.name,chapter_id,source_sha)
        if _slot_id(record['kind'],record['character_name'])!=event['slot_id']:
            raise CorrectionMemoryError('correction history slot mismatch')
        latest_events[event['slot_id']]=event['version_id']
    rows=[]
    for slot_id,version_id in current['slots'].items():
        if (not isinstance(slot_id,str) or not re.fullmatch(r'[0-9a-f]{24}',slot_id)
                or not _valid_hash(version_id) or latest_events.get(slot_id)!=version_id):
            raise CorrectionMemoryError('correction slot pointer invalid')
        record=_verify_note_version(source_root,version_id,chapter_root.parent.name,chapter_id,source_sha)
        if _slot_id(record['kind'],record['character_name'])!=slot_id:
            raise CorrectionMemoryError('correction slot identity mismatch')
        rows.append(record)
    return rows


def read_notes(root,book,target_chapter,*,include_current=False):
    """Read same-book notes before target; optionally append target notes marked current."""
    if not isinstance(book,str) or not BOOK.fullmatch(book):raise CorrectionMemoryError('invalid book identity')
    target_key=_chapter_key(target_chapter)
    root=Path(root);book_root=root/'corrections'/book
    if (root/'corrections').is_symlink() or book_root.is_symlink():
        raise CorrectionMemoryError('correction book path redirected')
    if not book_root.exists():return []
    rows=[]
    with _os_book_lock(root/'.correction-memory-locks',book):
        for chapter_root in book_root.iterdir():
            if chapter_root.is_symlink() or not chapter_root.is_dir():raise CorrectionMemoryError('correction chapter entry invalid')
            chapter_id=chapter_root.name;key=_chapter_key(chapter_id)
            is_current=chapter_id==target_chapter
            if key>target_key or (is_current and not include_current):continue
            current_path=chapter_root/'current.json'
            if current_path.is_symlink() or not current_path.is_file():raise CorrectionMemoryError('correction chapter pointer missing')
            current=json.loads(current_path.read_bytes())
            if (current.get('book_id')!=book or current.get('chapter_id')!=chapter_id
                    or not isinstance(current.get('source_hashes'),list) or not isinstance(current.get('conflicts'),list)):
                raise CorrectionMemoryError('correction chapter pointer identity mismatch')
            source_hashes=current['source_hashes']
            if (not all(_valid_hash(value) for value in source_hashes)
                    or len(source_hashes)!=len(set(source_hashes))
                    or current.get('current_source_sha256') not in source_hashes
                    or set(_source_directories(chapter_root))!={value[:20] for value in source_hashes}):
                raise CorrectionMemoryError('correction source set/path mismatch')
            if current['conflicts'] or len(current['source_hashes'])>1:
                raise CorrectionSourceConflict('chapter has correction notes for multiple source versions: '+chapter_id)
            if not source_hashes:continue
            source_sha=source_hashes[0]
            source_root=chapter_root/source_sha[:20]
            if source_root.is_symlink() or source_sha[:20]!=_source_directories(chapter_root)[0]:
                raise CorrectionMemoryError('correction source hash/path mismatch')
            for record in _read_source_notes(root,chapter_root,chapter_id,source_sha):
                rows.append({**record,'status':'operator_note_not_identity_approval',
                             'is_current_chapter':is_current})
    rows.sort(key=lambda row:(_chapter_key(row['chapter_id']),row['kind'],row.get('character_name') or ''))
    return rows
