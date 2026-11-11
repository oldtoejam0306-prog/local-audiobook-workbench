"""Isolated automatic fictional voice allocation; candidate identities unreviewed.

No model invocation on import. Generator is injected and must return a console
record with directory and request (including remote stage and vector_sha256).
Aliases must be supplied with explicit source evidence; this does not establish
identity truth. No user confirmation, copying or modification of the main library.
"""
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

_BOOK = re.compile(r'book-[0-9a-f]{20}\Z')
_ENTITY = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')


def _hash(data): return hashlib.sha256(data).hexdigest()
def _encoded(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf8')
def _new(path, value):
    with path.open('xb') as f:
        f.write(_encoded(value)); f.flush(); os.fsync(f.fileno())
def _load(path): return json.loads(path.read_bytes())


def _validate_record(record):
    req = record['request']; path = Path(record['directory'])/'speaker-vector.npy'
    if (not isinstance(req.get('stage'), str) or not req['stage'] or not path.is_file()
            or not re.fullmatch(r'[0-9a-f]{64}', str(req.get('vector_sha256', '')))
            or _hash(path.read_bytes()) != req['vector_sha256']):
        raise ValueError('voice vector or identity invalid')


def _selection_path(root, book_id):
    return Path(root) / book_id / 'selected-voices.json'


@contextmanager
def _selection_lock(root, book_id):
    lock_path = _selection_path(root, book_id).with_name('selected-voices.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            while True:
                try:
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1); break
                except OSError:
                    time.sleep(.05)
            try:
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.seek(0); handle.write(b'0'); handle.flush()
                yield
            finally:
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_selection_write(path, doc):
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    temp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(_encoded(doc)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists(): temp.unlink()


def _read_selection_document(root, book_id, skip_entity=None):
    path = _selection_path(root, book_id)
    if not path.exists(): return {'book_id': book_id, 'version': 1, 'voices': {}}
    doc = _load(path)
    if doc.get('book_id') != book_id or doc.get('version') != 1 or not isinstance(doc.get('voices'), dict):
        raise ValueError('invalid selected voice document')
    for entity, item in doc['voices'].items():
        if not _ENTITY.fullmatch(str(entity)) or not isinstance(item, dict): raise ValueError('invalid selected voice identity')
        binding = item.get('binding')
        if not isinstance(binding, dict) or not isinstance(binding.get('record'), dict): raise ValueError('invalid selected voice binding')
        if entity != skip_entity: _validate_record(binding['record'])
        vector_sha = binding['record']['request']['vector_sha256']
        if (binding.get('console_key') != binding['record'].get('book_role_key')
                or binding.get('console_key') != book_id + '\0' + entity
                or item.get('vector_sha256') != vector_sha
                or not re.fullmatch(r'[0-9a-f]{64}', str(item.get('design_sha256', '')))
                or item['design_sha256'] != _hash(_encoded(item.get('design')))
                or not isinstance(item.get('selection_basis'), str) or not item['selection_basis'].strip()):
            raise ValueError('selected voice evidence mismatch')
    return doc


def _read_selections(root, book_id):
    with _selection_lock(root, book_id):
        return _read_selection_document(root, book_id)


def get_selected_voice(root, book_id, entity_id):
    """Read a book-scoped selected binding; never generates or invokes a model."""
    if not isinstance(book_id, str) or not _BOOK.fullmatch(book_id): raise ValueError('invalid book')
    if not isinstance(entity_id, str) or not _ENTITY.fullmatch(entity_id): raise ValueError('invalid entity')
    with _selection_lock(root, book_id):
        doc = _read_selection_document(root, book_id, skip_entity=entity_id)
        item = doc['voices'].get(entity_id)
        if item is None: return None
        _validate_record(item['binding']['record'])
        return item['binding']


def select_voice(root, book_id, entity_id, binding, *, basis, design=None, spec_id=None):
    """Explicitly select a validated binding for one book/entity; overwrite only by this call."""
    if not isinstance(book_id, str) or not _BOOK.fullmatch(book_id): raise ValueError('invalid book')
    if not isinstance(entity_id, str) or not _ENTITY.fullmatch(entity_id): raise ValueError('invalid entity')
    if not isinstance(basis, str) or not basis.strip(): raise ValueError('selection basis required')
    if not isinstance(binding, dict) or not isinstance(binding.get('console_key'), str): raise ValueError('invalid binding')
    _validate_record(binding['record'])
    if (binding['console_key'] != binding['record'].get('book_role_key')
            or binding['console_key'] != book_id + '\0' + entity_id): raise ValueError('voice identity mismatch')
    path = _selection_path(root, book_id); path.parent.mkdir(parents=True, exist_ok=True)
    with _selection_lock(root, book_id):
        # A deliberate replacement may repair this entity's corrupt old vector.
        doc = _read_selection_document(root, book_id, skip_entity=entity_id)
        item = {'binding': binding, 'vector_sha256': binding['record']['request']['vector_sha256'],
                'selection_basis': basis.strip(), 'design': design, 'design_sha256': _hash(_encoded(design)), 'spec_id': spec_id,
                'status': 'explicitly_selected'}
        doc['voices'][entity_id] = item
        _atomic_selection_write(path, doc)
    return item


def _save_recommendation(root, book_id, entity_id, binding, design, spec_id):
    path = _selection_path(root, book_id)
    with _selection_lock(root, book_id):
        doc = _read_selection_document(root, book_id)
        if entity_id in doc['voices']: return
        doc['voices'][entity_id] = {'binding': binding,
            'vector_sha256': binding['record']['request']['vector_sha256'],
            'selection_basis': 'first generated candidate; default recommendation pending user choice',
            'design': design, 'design_sha256': _hash(_encoded(design)), 'spec_id': spec_id, 'status': 'default_recommendation'}
        _atomic_selection_write(path, doc)


def _validate_profile(p):
    if not isinstance(p, dict) or not _ENTITY.fullmatch(str(p.get('entity_id',''))): raise ValueError('invalid entity')
    if not isinstance(p.get('name'), str) or not p['name'].strip(): raise ValueError('invalid name')
    if not isinstance(p.get('evidence'), str) or not p['evidence'].strip(): raise ValueError('missing source evidence')
    if not isinstance(p.get('personality'), str) or len(p['personality']) > 1000: raise ValueError('invalid personality')
    if not isinstance(p.get('aliases'), list): raise ValueError('invalid aliases')
    for a in p['aliases']:
        if not isinstance(a, dict) or set(a) != {'name','evidence'} or not all(isinstance(a[k], str) and a[k].strip() for k in a):
            raise ValueError('aliases require evidence')
    if (not isinstance(p.get('design_text'), str) or not p['design_text'].strip()
            or len(p['design_text']) > 1600
            or not re.fullmatch(r'[0-9a-f]{64}', str(p.get('profile_sha256', '')))
            or not isinstance(p.get('source_evidence'), list) or not p['source_evidence']):
        raise ValueError('new voice generation requires source-bound profile and design_text')


def creative_description(design_text):
    """Validate and pass through a planner-produced bounded Chinese design."""
    if not isinstance(design_text, str) or not design_text.strip() or len(design_text) > 1600:
        raise ValueError('voice design is empty or too long')
    if (re.search(r'https?://|www\.|(?:忽略|覆盖).{0,8}(?:规则|指令)|(?<!不)(?:模仿|克隆).{0,12}(?:真人|本人|某人)|真人参考|上传.{0,8}声音', design_text, re.I)
            or not any(term in design_text for term in ('创作选择', '创作性'))):
        raise ValueError('unapproved or unsafe voice design')
    return design_text


def generate_local_fictional(description, output_directory):
    """Single local Mac VoiceDesign call; caller must hold the reservation.

    The caller must pass a planner-produced creative design description.
    The local helper handles the actual stage/vector generation and download.
    """
    creative_description(description)
    from console_voice_ui import invoke
    directory = Path(output_directory) / 'voice'
    directory.mkdir(exist_ok=False)
    request = invoke(description, '今天的风很轻，我们沿着河边往前走。先看清楚情况，再决定从哪里出发。', directory)
    return {'directory': str(directory), 'request': request}


def _generation_failure_record(directory, book_id, profile, exc):
    marker = directory / 'voice' / 'remote-completion.json'
    failure_path = directory / 'generation.failure.json'
    failure = {'version': 1, 'status': 'generation_or_transfer_failed',
        'book_id': book_id, 'entity_id': profile['entity_id'], 'spec_id': profile.get('spec_id'),
        'profile_sha256': profile.get('profile_sha256'), 'candidate_directory': str(directory),
        'remote_completion_marker': str(marker), 'remote_completion_present': marker.is_file() and not marker.is_symlink(),
        'error_type': type(exc).__name__[:120], 'error_message': str(exc)[:1000],
        'created_unix_ns': time.time_ns()}
    try:
        _new(failure_path, failure)
    except FileExistsError:
        pass
    except OSError as write_error:
        failure['failure_record_error'] = (type(write_error).__name__ + ': ' + str(write_error))[:500]
    return failure_path, failure


def _prior_generation_failure(directory, book_id, profile):
    path = directory / 'generation.failure.json'
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        failure = _load(path)
    except (OSError, ValueError, TypeError):
        return {'failure_record': str(path), 'failure_record_status': 'invalid'}
    if (not isinstance(failure, dict) or failure.get('version') != 1
            or failure.get('book_id') != book_id or failure.get('entity_id') != profile['entity_id']
            or failure.get('spec_id') != profile.get('spec_id')):
        return {'failure_record': str(path), 'failure_record_status': 'identity_mismatch'}
    return {key: failure[key] for key in ('failure_record', 'remote_completion_marker',
            'remote_completion_present', 'error_type', 'error_message') if key in failure} | {
                'failure_record': str(path), 'failure_record_status': failure.get('status')}


def allocate(root, book_id, profiles, narrator, generate=generate_local_fictional):
    """Return role bindings and unresolved candidates; no silent retries.

    profiles: source-bound profiles with planner-produced design_text and profile_sha256.
    Entity IDs and evidence must be derived and checked by the caller against
    verified source before calling. Ambiguous labels are never assigned.
    narrator is an existing renderer-compatible {'console_key','record'} binding.
    generate(description, output_directory) returns a console voice record;
    it must itself be single-attempt and write only to output_directory.
    """
    if not isinstance(book_id,str) or not _BOOK.fullmatch(book_id): raise ValueError('invalid book')
    if not isinstance(profiles,list) or not callable(generate): raise ValueError('invalid inputs')
    for p in profiles: _validate_profile(p)
    if len({p['entity_id'] for p in profiles}) != len(profiles): raise ValueError('duplicate entity ID')
    _validate_record(narrator['record'])
    if narrator['record'].get('book_role_key') != narrator['console_key']: raise ValueError('narrator identity mismatch')
    book = Path(root)/book_id; book.mkdir(parents=True,exist_ok=True)
    lock = book/'active.lock'
    try: _new(lock, {'status':'active_or_uncertain'})
    except FileExistsError: return {'status':'busy_or_uncertain','bindings':{},'unresolved':[]}
    try:
        existing = []
        for path in list(book.glob('*/voice.json')) + list(book.glob('*/candidates/*/voice.json')):
            record = _load(path)
            entity_dir = path.parents[2].name if 'candidates' in path.parts else path.parent.name
            if record.get('book_id') != book_id or entity_dir != record.get('entity_id'):
                raise ValueError('invalid saved identity')
            _validate_record(record['binding']['record'])
            existing.append(record)
        labels = {}
        for item in existing:
            for label in item['labels']: labels.setdefault(label,set()).add(item['entity_id'])
        for p in profiles:
            for label in [p['name']] + [a['name'] for a in p['aliases']]:
                labels.setdefault(label,set()).add(p['entity_id'])
        conflicting = {name: sorted(ids) for name,ids in labels.items() if len(ids)>1}
        selected = _read_selections(root, book_id)['voices']
        output = {'narrator':selected.get('narrator', {}).get('binding', narrator)}; unresolved=[]; recommendations=[]
        for p in profiles:
            entity=p['entity_id']; names=[p['name']]+[a['name'] for a in p['aliases']]
            if any(n in conflicting for n in names):
                unresolved.append({'entity_id':entity,'reason':'alias_or_name_conflict','labels':[n for n in names if n in conflicting]}); continue
            if p.get('voicing') == 'narrator_only':
                output[entity] = output['narrator']
                continue
            if entity in selected:
                output[entity] = selected[entity]['binding']
                continue
            spec_id=p.get('spec_id')
            if not isinstance(spec_id,str) or not re.fullmatch(r'[0-9a-f]{16}',spec_id):
                unresolved.append({'entity_id':entity,'reason':'invalid_design_spec'}); continue
            entity_dir=book/entity; entity_dir.mkdir(exist_ok=True)
            directory=entity_dir/'candidates'/spec_id; directory.mkdir(parents=True,exist_ok=True)
            saved=directory/'voice.json'; reservation=directory/'generation.reserved.json'
            if saved.exists():
                result=_load(saved)
                if (result['name'] != p['name'] or result['entity_id'] != entity or result['book_id'] != book_id
                        or result.get('profile_sha256') != p['profile_sha256']
                        or result.get('spec_id') != spec_id
                        or result.get('voice_description_creative_inference') != p['design_text']):
                    unresolved.append({'entity_id':entity,'reason':'saved_identity_differs'}); continue
                _validate_record(result['binding']['record']); output[entity]=result['binding']; recommendations.append(entity)
                _save_recommendation(root, book_id, entity, result['binding'], p['design_text'], spec_id); continue
            description=creative_description(p['design_text'])
            try: _new(reservation, {'book_id':book_id,'entity_id':entity,'status':'generation_may_have_occurred','profile_sha256':_hash(_encoded(p))})
            except FileExistsError:
                unresolved.append({'entity_id':entity,'reason':'generation_unknown_no_retry',
                    'candidate_directory':str(directory),**_prior_generation_failure(directory,book_id,p)}); continue
            try:
                record=generate(description,directory)
                if not Path(record['directory']).resolve().is_relative_to(directory.resolve()):
                    raise ValueError('generator output outside isolated directory')
                _validate_record(record)
                key=book_id+'\0'+entity
                # This is an experiment-local automatic key, not an assertion of user approval.
                record=dict(record,book_role_key=key)
                binding={'console_key':key,'record':record}
                result={'book_id':book_id,'entity_id':entity,'name':p['name'],'labels':names,
                        'source_evidence':p['source_evidence'],'alias_evidence':p['aliases'],
                        'profile_sha256':p['profile_sha256'],'spec_id':spec_id,
                        'voice_description_creative_inference':description,
                        'binding':binding,'identity_status':'unreviewed_model_candidate',
                        'voice_status':'automatically_assigned_not_user_confirmed'}
                _new(saved,result); output[entity]=binding; recommendations.append(entity)
                _save_recommendation(root, book_id, entity, binding, p['design_text'], spec_id)
            except Exception as exc:
                failure_path,failure=_generation_failure_record(directory,book_id,p,exc)
                unresolved.append({'entity_id':entity,'reason':'generation_unknown_no_retry',
                    'candidate_directory':str(directory),'failure_record':str(failure_path),
                    'remote_completion_marker':failure['remote_completion_marker'],
                    'remote_completion_present':failure['remote_completion_present'],
                    'error_type':failure['error_type'],'error_message':failure['error_message']})
        return {'status':'complete' if not unresolved else 'needs_review','bindings':output,
                'unresolved':unresolved,'alias_conflicts':conflicting,'human_reviewed':False,
                'selection_status':{entity:selected.get(entity, {}).get('status', 'default_recommendation') for entity in output if entity != 'narrator'},
                'recommendations':recommendations}
    finally:
        lock.unlink()
