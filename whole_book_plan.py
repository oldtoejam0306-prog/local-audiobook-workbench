"""Persistent, offline plan for a two-pass whole-book voice workflow.

This module only manages JSON planning state. It does not read/write book
content, launch workers, call models/APIs, synthesize audio, or render chapters.
Workers must separately perform a claimed task and report its outcome.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


PLAN_VERSION = 1
HEX64 = set('0123456789abcdef')


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value.lower()) <= HEX64


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Cross-process exclusive lock using the platform's standard library."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b'\0'); stream.flush()
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f'.tmp-{os.getpid()}')
    with temp.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temp, path)


def _normalize_inputs(book_id: str, chapters: list[dict[str, Any]],
                      source_hashes: dict[str, str], characters: list[dict[str, Any]],
                      fixed_voice_snapshot: dict[str, str] | None = None,
                      external_profile_hash: str | None = None,
                      attribution_bindings: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    if not isinstance(book_id, str) or not book_id.strip():
        raise ValueError('book_id 必须为非空字符串。')
    if not isinstance(chapters, list) or not chapters:
        raise ValueError('全书计划必须提供章节清单。')
    if not isinstance(source_hashes, dict):
        raise ValueError('source_hashes 必须为文件名到 SHA-256 的映射。')
    clean_hashes = {}
    for name, digest in source_hashes.items():
        if not isinstance(name, str) or not name or not _valid_hash(digest):
            raise ValueError(f'源文件哈希无效：{name!r}')
        clean_hashes[name] = digest.lower()
    if not clean_hashes:
        raise ValueError('至少需要一个素材源哈希。')
    clean_chapters = []
    seen = set()
    for item in chapters:
        if not isinstance(item, dict):
            raise ValueError('章节条目必须为对象。')
        ident = item.get('id')
        if not isinstance(ident, str) or not ident.strip() or ident in seen:
            raise ValueError(f'章节编号无效或重复：{ident!r}')
        seen.add(ident)
        row = {'id': ident, 'source_sha256': item.get('source_sha256')}
        if not _valid_hash(row['source_sha256']):
            raise ValueError(f'章节 {ident} 缺少有效的源文本哈希。')
        row['source_sha256'] = row['source_sha256'].lower()
        if item.get('title') is not None:
            row['title'] = str(item['title'])[:300]
        clean_chapters.append(row)
    clean_characters = []
    seen_chars = set()
    for item in characters or []:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name'].strip():
            raise ValueError('角色条目必须包含非空 name。')
        name = item['name'].strip()
        if name in seen_chars:
            raise ValueError(f'角色重复：{name}')
        seen_chars.add(name)
        count = item.get('appearance_count', 0)
        if type(count) is not int or count < 0:
            raise ValueError(f'{name} 的 appearance_count 无效。')
        clean_characters.append({'name': name, 'appearance_count': count,
                                 'fixed_voice': str(item.get('fixed_voice') or '').strip()})
    clean_characters.sort(key=lambda x: (-x['appearance_count'], x['name'].casefold(), x['name']))
    fixed_voice_snapshot = fixed_voice_snapshot or {}
    if not isinstance(fixed_voice_snapshot, dict):
        raise ValueError('fixed_voice_snapshot 必须为人物到固定声音编号的映射。')
    fixed = {}
    for name, voice in fixed_voice_snapshot.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(voice, str) or not voice.strip():
            raise ValueError('fixed_voice_snapshot 中的人物或声音编号无效。')
        fixed[name.strip()] = voice.strip()
    if attribution_bindings is None:
        attribution_bindings = {}
    if not isinstance(attribution_bindings, dict):
        raise ValueError('attribution_bindings must map chapter IDs to captured chapter bindings')
    bindings = {}
    chapter_ids = {chapter['id'] for chapter in clean_chapters}
    if set(attribution_bindings) - chapter_ids:
        raise ValueError('attribution_bindings contains unknown chapter IDs')
    for chapter_id, binding in attribution_bindings.items():
        if (not isinstance(binding, dict)
                or set(binding) != {'version', 'book_id', 'chapter_id', 'chapter_sha256', 'dialogue_text_sha256'}
                or type(binding.get('version')) is not int or binding.get('version') != 1
                or binding.get('book_id') != book_id.strip()
                or binding.get('chapter_id') != chapter_id or not _valid_hash(binding.get('chapter_sha256'))
                or not isinstance(binding.get('dialogue_text_sha256'), dict)):
            raise ValueError(f'invalid captured attribution binding for {chapter_id}')
        dialogue_hashes = binding['dialogue_text_sha256']
        if any(not isinstance(ident, str) or not ident or not _valid_hash(digest)
               for ident, digest in dialogue_hashes.items()):
            raise ValueError(f'invalid dialogue text hash in binding for {chapter_id}')
        bindings[chapter_id] = {'version': 1, 'book_id': book_id.strip(), 'chapter_id': chapter_id,
                                'chapter_sha256': binding['chapter_sha256'].lower(),
                                'dialogue_text_sha256': dict(sorted(dialogue_hashes.items()))}
    if external_profile_hash is not None and not _valid_hash(external_profile_hash):
        raise ValueError('external_profile_hash 必须是 SHA-256。')
    return {'book_id': book_id.strip(), 'chapters': clean_chapters,
            'source_hashes': dict(sorted(clean_hashes.items())),
            # Startup hints are kept apart from dynamic fused identities and are
            # not treated as source material in the immutable fingerprint.
            'initial_character_hints': clean_characters,
            'fixed_voice_snapshot': dict(sorted(fixed.items())),
            'attribution_bindings': dict(sorted(bindings.items())),
            # Hash only user-maintained profile inputs here; never hash dynamic
            # fusion output into this field.
            'external_profile_hash': external_profile_hash}


def _input_fingerprint(inputs: dict[str, Any]) -> str:
    return _digest({'book_id': inputs['book_id'], 'chapters': inputs['chapters'],
                    'source_hashes': inputs['source_hashes'],
                    'fixed_voice_snapshot': inputs.get('fixed_voice_snapshot', {}),
                    'external_profile_hash': inputs.get('external_profile_hash'),
                    'attribution_bindings': inputs.get('attribution_bindings', {})})


def _task_id(plan_id: str, revision: int, key: str) -> str:
    return hashlib.sha256(f'{plan_id}|{revision}|{key}'.encode('utf-8')).hexdigest()[:24]


def _make_tasks(plan_id: str, revision: int, inputs: dict[str, Any]) -> list[dict[str, Any]]:
    chapters = inputs['chapters']
    tasks: list[dict[str, Any]] = []
    by_key: dict[str, str] = {}

    def add(key: str, stage: str, chapter: str | None = None,
            dependencies: list[str] | None = None, details: dict[str, Any] | None = None) -> str:
        ident = _task_id(plan_id, revision, key)
        task = {'id': ident, 'key': key, 'stage': stage, 'chapter_id': chapter,
                'status': 'queued', 'attempt': 0, 'dependencies': list(dependencies or []),
                'details': details or {}, 'result': None, 'error': None,
                'redoable': True, 'redo_generation': 0, 'history': []}
        tasks.append(task); by_key[key] = ident
        return ident

    # Pass 1: one chapter analysis request yields both profile and summary.
    first_pass_ids = []
    for chapter in chapters:
        cid = chapter['id']
        first_pass_ids.append(add(f'chapter_analysis:{cid}', 'chapter_analysis', cid,
                                  details={'outputs': ['profile', 'summary'], 'single_request': True,
                                           'billing_mode': 'external',
                                           'generation': 'automatic', 'human_reviewed': False}))
    fusion = add('cast_fusion', 'whole_book_cast_fusion', dependencies=first_pass_ids,
                 details={'billing_mode': 'external', 'generation': 'automatic', 'human_reviewed': False})

    # Pass 2: attribution by chapter, then voice planning/synthesis, then chapter output.
    attribution_ids = []
    for chapter in chapters:
        cid = chapter['id']
        attribution_ids.append(add(f'dialogue_attribution:{cid}', 'dialogue_attribution', cid, [fusion],
                                   {'generation': 'automatic', 'unknown_and_conflicts_preserved': True,
                                    'billing_mode': 'external', 'human_reviewed': False}))
    for chapter in chapters:
        cid = chapter['id']
        render_deps = [by_key[f'dialogue_attribution:{cid}']]
        render = add(f'render:{cid}', 'chapter_render', cid, render_deps,
                     {'billing_mode': 'local', 'generation': 'automatic',
                      'attribution_binding_sha256': _digest(inputs.get('attribution_bindings', {}).get(cid))
                      if cid in inputs.get('attribution_bindings', {}) else None,
                      'human_reviewed': False})
        add(f'verify_output:{cid}', 'output_verification', cid, [render],
            {'billing_mode': 'local', 'generation': 'automatic', 'human_reviewed': False})
    return tasks


def _new_plan(inputs: dict[str, Any], revision: int = 1) -> dict[str, Any]:
    plan_id = hashlib.sha256(inputs['book_id'].encode('utf-8')).hexdigest()[:24]
    return {
        'version': PLAN_VERSION, 'plan_id': plan_id, 'book_id': inputs['book_id'],
        'revision': revision, 'status': 'ready', 'stale': False, 'stale_reason': None,
        'inputs': inputs, 'input_fingerprint': _input_fingerprint(inputs),
        'stop_requested': False, 'archived_revisions': [],
        'tasks': _make_tasks(plan_id, revision, inputs),
    }


def load_plan(plan_path: str | Path) -> dict[str, Any]:
    path = Path(plan_path)
    with _file_lock(path.with_suffix(path.suffix + '.lock')):
        if not path.is_file():
            raise FileNotFoundError(path)
        return json.loads(path.read_text(encoding='utf-8-sig'))


def create_or_update_plan(plan_path: str | Path, *, book_id: str,
                          chapters: list[dict[str, Any]], source_hashes: dict[str, str],
                          characters: list[dict[str, Any]] | None = None,
                          fixed_voice_snapshot: dict[str, str] | None = None,
                          external_profile_hash: str | None = None,
                          attribution_bindings: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Create a plan once, return it idempotently, or mark changed input stale.

    Changed source hashes, initial locked-voice settings, or the separately
    maintained ``external_profile_hash`` mark the plan stale. Fused character
    identities in ``characters`` are informational hints and excluded from the
    immutable fingerprint. The caller must keep external_profile_hash limited
    to user-maintained profile inputs, never fusion outputs. Changed inputs are
    recorded but never silently requeued; use ``rebuild_stale_plan`` after
    reconciling any in-flight/uncertain operation.
    """
    path = Path(plan_path)
    inputs = _normalize_inputs(book_id, chapters, source_hashes, characters or [],
                               fixed_voice_snapshot, external_profile_hash, attribution_bindings)
    with _file_lock(path.with_suffix(path.suffix + '.lock')):
        if not path.exists():
            plan = _new_plan(inputs)
        else:
            plan = json.loads(path.read_text(encoding='utf-8-sig'))
            if plan.get('book_id') != inputs['book_id']:
                raise ValueError('现有计划属于另一部书，拒绝覆盖。')
            fingerprint = _input_fingerprint(inputs)
            if fingerprint != plan.get('input_fingerprint'):
                plan['stale'] = True
                plan['status'] = 'stale'
                plan['stale_reason'] = '章节、素材哈希或人物规划输入已变化。'
                plan['observed_input_fingerprint'] = fingerprint
                plan['observed_inputs'] = inputs
                for task in plan.get('tasks', []):
                    if task.get('status') == 'running':
                        task['status'] = 'interrupted'
                        task['stale_execution_uncertain'] = True
                        task['error'] = '输入快照在任务运行期间变化；执行结果和计费状态需先核实。'
                    elif task.get('status') in ('queued', 'complete'):
                        if task.get('status') == 'complete':
                            task.setdefault('history', []).append({
                                'status': 'complete', 'attempt': task.get('attempt', 0),
                                'result': copy.deepcopy(task.get('result')),
                                'completed_at': task.get('completed_at'),
                                'invalidated_by': 'input_snapshot_changed',
                            })
                        task['status'] = 'stale'
                        task['error'] = '输入快照已变化；需显式重建计划。'
        _atomic_json(path, plan)
        return copy.deepcopy(plan)


def rebuild_stale_plan(plan_path: str | Path, *, book_id: str,
                       chapters: list[dict[str, Any]], source_hashes: dict[str, str],
                       characters: list[dict[str, Any]] | None = None,
                       fixed_voice_snapshot: dict[str, str] | None = None,
                       external_profile_hash: str | None = None,
                       attribution_bindings: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Explicitly archive a stale revision and create a fresh idempotent one."""
    path = Path(plan_path)
    inputs = _normalize_inputs(book_id, chapters, source_hashes, characters or [],
                               fixed_voice_snapshot, external_profile_hash, attribution_bindings)
    with _file_lock(path.with_suffix(path.suffix + '.lock')):
        if not path.is_file():
            raise FileNotFoundError(path)
        plan = json.loads(path.read_text(encoding='utf-8-sig'))
        if plan.get('book_id') != inputs['book_id']:
            raise ValueError('现有计划属于另一部书，拒绝覆盖。')
        if not plan.get('stale'):
            raise ValueError('只能显式重建已标 stale 的计划。')
        unresolved = [task.get('key') for task in plan.get('tasks', [])
                      if task.get('status') in ('running', 'failed', 'billing_unknown', 'interrupted')]
        if unresolved:
            raise ValueError('旧计划仍有执行/计费状态待核实任务，不能重建：' + ', '.join(unresolved))
        archived = copy.deepcopy(plan)
        archived['status'] = 'archived_stale'
        archived['archived_revisions'] = []
        revision = int(plan.get('revision', 0)) + 1
        new = _new_plan(inputs, revision)
        new['archived_revisions'] = [*plan.get('archived_revisions', []), archived]
        _atomic_json(path, new)
        return copy.deepcopy(new)


def expand_voice_tasks_from_fusion(plan_path: str | Path, fusion_task_id: str,
                                   characters: list[dict[str, Any]] | None = None, *,
                                   checkpoint_receipt_sha256: str) -> dict[str, Any]:
    """Atomically extend a plan from a receipt-validated whole-book cast result.

    The executor must validate and persist the fusion worker receipt before
    calling this function. Existing fixed voices are recorded but get no
    design/synthesis tasks. A cast fingerprint namespaces generated tasks so a
    later explicitly redone fusion can add a new cast without reusing old work.
    """
    if not _valid_hash(checkpoint_receipt_sha256):
        raise ValueError('角色融合必须附带已验证的 checkpoint receipt SHA-256。')
    path = Path(plan_path)
    with _file_lock(path.with_suffix(path.suffix + '.lock')):
        plan = json.loads(path.read_text(encoding='utf-8-sig'))
        if plan.get('stale') or plan.get('stop_requested'):
            raise ValueError('计划 stale 或已停止，不能扩展角色任务。')
        tasks = plan.get('tasks', [])
        fusion = next((task for task in tasks if task.get('id') == fusion_task_id), None)
        if not fusion or fusion.get('stage') != 'whole_book_cast_fusion' or fusion.get('status') != 'complete':
            raise ValueError('角色融合任务尚未完成。')
        fusion_result = fusion.get('result') or {}
        if (fusion_result.get('checkpoint_receipt_validated') is not True
                or fusion_result.get('checkpoint_receipt_sha256') != checkpoint_receipt_sha256):
            raise ValueError('角色融合结果没有匹配的执行器收据校验记录。')
        output = fusion_result.get('output')
        if not isinstance(output, dict) or not isinstance(output.get('characters'), list):
            raise ValueError('经过收据校验的融合结果缺少 characters 列表。')
        if characters is not None and characters != output['characters']:
            raise ValueError('扩展人物清单与收据校验的融合结果不一致。')
        characters = output['characters']
        fusion_generation = fusion.get('redo_generation', 0)

        normalized = []
        names = set()
        for item in characters:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name'].strip():
                raise ValueError('融合人物条目缺少名称。')
            name = item['name'].strip()
            if name in names:
                raise ValueError(f'融合人物重复：{name}')
            names.add(name)
            count = item.get('appearance_count', 0)
            if type(count) is not int or count < 0:
                raise ValueError(f'{name} 的全书出现次数无效。')
            fixed = str(item.get('fixed_voice') or '').strip()
            normalized.append({'name': name, 'appearance_count': count, 'fixed_voice': fixed,
                               'profile': copy.deepcopy(item)})
        fixed_conflicts = []
        initial_locked = plan.get('inputs', {}).get('fixed_voice_snapshot', {})
        hints = {item['name']: item for item in plan.get('inputs', {}).get('initial_character_hints', [])}
        by_name = {item['name']: item for item in normalized}
        for name, voice in initial_locked.items():
            item = by_name.get(name)
            if item is None:
                hint = hints.get(name, {})
                item = {'name': name, 'appearance_count': hint.get('appearance_count', 0),
                        'fixed_voice': voice, 'profile': copy.deepcopy(hint)}
                normalized.append(item); by_name[name] = item
            elif item['fixed_voice'] and item['fixed_voice'] != voice:
                fixed_conflicts.append({'character': name, 'locked_voice': voice,
                                        'fusion_suggestion': item['fixed_voice']})
                item['fixed_voice'] = voice
            else:
                item['fixed_voice'] = voice
        normalized.sort(key=lambda x: (-x['appearance_count'], x['name'].casefold(), x['name']))
        cast_fingerprint = _digest(normalized)

        prior = plan.get('voice_expansion')
        if prior and prior.get('cast_fingerprint') == cast_fingerprint:
            prior['characters'] = normalized
            prior['fixed_voices_preserved'] = [x for x in normalized if x['fixed_voice']]
            prior['fixed_voice_conflicts'] = fixed_conflicts
            if prior.get('fusion_redo_generation') != fusion_generation:
                prior['fusion_redo_generation'] = fusion_generation
                prior['checkpoint_receipt_sha256'] = checkpoint_receipt_sha256
                expansion_task = next((task for task in tasks
                                       if task.get('id') == prior.get('expansion_task_id')), None)
                if expansion_task:
                    expansion_task['status'] = 'complete'
                    expansion_task['details']['fusion_generation'] = fusion_generation
                    expansion_task['details']['fixed_voices_preserved'] = [
                        x['name'] for x in normalized if x['fixed_voice']]
                    expansion_task['result'] = {'output': {'cast_fingerprint': cast_fingerprint},
                                                'checkpoint_receipt_sha256': checkpoint_receipt_sha256,
                                                'checkpoint_receipt_validated': True,
                                                'generation': 'automatic', 'human_reviewed': False}
                    expansion_task['error'] = None
                for task in tasks:
                    if task.get('details', {}).get('fusion_generation') is not None:
                        task['details']['fusion_generation'] = fusion_generation
                _atomic_json(path, plan)
            return copy.deepcopy(plan)
        if prior and prior.get('fusion_redo_generation', 0) >= fusion_generation:
            raise ValueError('融合人物发生变化，但融合任务没有在扩展后重新执行。')

        # Do not expand while prior downstream work is active.
        if any(task.get('status') == 'running' for task in tasks):
            raise ValueError('仍有任务运行中，不能扩展角色任务。')
        attribution_ids = [task['id'] for task in tasks if task.get('stage') == 'dialogue_attribution']
        if not attribution_ids:
            raise ValueError('计划缺少逐章对白归属任务。')

        old_synthesis_ids = {task['id'] for task in tasks if task.get('stage') == 'local_voice_synthesis'}
        old_expansion_ids = {task['id'] for task in tasks if task.get('stage') == 'voice_task_expansion'}
        for task in tasks:
            if task.get('id') in old_synthesis_ids | old_expansion_ids or (task.get('stage') == 'voice_design_text'
                    and task.get('details', {}).get('fusion_generation') is not None):
                task['status'] = 'obsolete'
                task['error'] = '融合角色清单已通过新收据更新。'

        voice_synthesis_ids = []
        created = []
        expansion_key = f"voice_expansion:cast-{cast_fingerprint[:12]}"
        expansion_id = _task_id(plan['plan_id'], plan['revision'], expansion_key)
        expansion_task = {'id': expansion_id, 'key': expansion_key, 'stage': 'voice_task_expansion',
                          'chapter_id': None, 'status': 'complete', 'attempt': 0,
                          'dependencies': [fusion_task_id],
                          'details': {'generation': 'automatic', 'fusion_generation': fusion_generation,
                                      'billing_mode': 'local',
                                      'fixed_voices_preserved': [x['name'] for x in normalized if x['fixed_voice']],
                                      'human_reviewed': False},
                          'result': {'output': {'cast_fingerprint': cast_fingerprint},
                                     'checkpoint_receipt_sha256': checkpoint_receipt_sha256,
                                     'checkpoint_receipt_validated': True,
                                     'generation': 'automatic', 'human_reviewed': False},
                          'error': None, 'redoable': False, 'redo_generation': 0, 'history': []}
        created.append(expansion_task)
        for character in normalized:
            if character['fixed_voice']:
                continue
            name = character['name']
            suffix = cast_fingerprint[:12]
            design_key = f"voice_design:{name}:cast-{suffix}"
            design_id = _task_id(plan['plan_id'], plan['revision'], design_key)
            design = {'id': design_id, 'key': design_key, 'stage': 'voice_design_text', 'chapter_id': None,
                      'status': 'queued', 'attempt': 0, 'dependencies': attribution_ids.copy(),
                      'details': {'generation': 'automatic', 'character': name,
                                  'appearance_count': character['appearance_count'],
                                  'character_profile': copy.deepcopy(character.get('profile', {})),
                                  'fixed_voice': False, 'fusion_generation': fusion_generation,
                                  'billing_mode': 'external',
                                  'human_reviewed': False},
                      'result': None, 'error': None, 'redoable': True, 'redo_generation': 0, 'history': []}
            synth_key = f"voice_synthesis:{name}:cast-{suffix}"
            synth_id = _task_id(plan['plan_id'], plan['revision'], synth_key)
            synthesis = {'id': synth_id, 'key': synth_key, 'stage': 'local_voice_synthesis', 'chapter_id': None,
                         'status': 'queued', 'attempt': 0, 'dependencies': [design_id],
                         'details': {'generation': 'automatic', 'character': name,
                                     'fixed_voice_after_acceptance': True, 'human_reviewed': False,
                                     'local_only': True, 'uses_real_person_reference': False,
                                     'billing_mode': 'local', 'fusion_generation': fusion_generation},
                         'result': None, 'error': None, 'redoable': True, 'redo_generation': 0, 'history': []}
            created.extend((design, synthesis)); voice_synthesis_ids.append(synth_id)
        tasks.extend(created)
        for task in tasks:
            if task.get('stage') == 'chapter_render':
                task['dependencies'] = [dep for dep in task.get('dependencies', [])
                                        if dep not in old_synthesis_ids | old_expansion_ids]
                task['dependencies'].extend([expansion_id, *voice_synthesis_ids])
                task['dependencies'] = list(dict.fromkeys(task['dependencies']))
                if task.get('status') == 'complete':
                    task['status'] = 'queued'; task['result'] = None; task['error'] = None
                    task['redo_generation'] = task.get('redo_generation', 0) + 1
                    task.pop('completed_at', None)
        plan['voice_expansion'] = {'fusion_task_id': fusion_task_id,
                                   'fusion_redo_generation': fusion_generation,
                                   'checkpoint_receipt_sha256': checkpoint_receipt_sha256,
                                   'cast_fingerprint': cast_fingerprint,
                                   'characters': normalized,
                                   'fixed_voices_preserved': [x for x in normalized if x['fixed_voice']],
                                   'fixed_voice_conflicts': fixed_conflicts,
                                   'expansion_task_id': expansion_id,
                                   'created_task_ids': [task['id'] for task in created]}
        _atomic_json(path, plan)
        return copy.deepcopy(plan)


def mark_plan_stale(plan_path: str | Path, *, reason: str,
                    observed_evidence_sha256: str) -> dict[str, Any]:
    """Fail closed when a live render snapshot drifts outside initial file inputs."""
    if not isinstance(reason, str) or not reason.strip() or not _valid_hash(observed_evidence_sha256):
        raise ValueError('stale evidence requires a reason and SHA-256.')
    path = Path(plan_path)
    with _file_lock(path.with_suffix(path.suffix + '.lock')):
        plan = json.loads(path.read_text(encoding='utf-8-sig'))
        plan['stale'] = True
        plan['status'] = 'stale'
        plan['stale_reason'] = reason.strip()[:1000]
        plan.setdefault('observed_runtime_evidence', []).append({
            'sha256': observed_evidence_sha256.lower(), 'at': time.time(),
        })
        for task in plan.get('tasks', []):
            if task.get('status') == 'running':
                task['status'] = 'interrupted'
                task['stale_execution_uncertain'] = True
                task['error'] = '渲染输入快照在任务运行期间变化；执行结果需先核实。'
            elif task.get('status') in ('queued', 'complete'):
                if task.get('status') == 'complete':
                    task.setdefault('history', []).append({
                        'status': 'complete', 'attempt': task.get('attempt', 0),
                        'result': copy.deepcopy(task.get('result')),
                        'completed_at': task.get('completed_at'),
                        'invalidated_by': 'runtime_render_evidence_changed',
                    })
                task['status'] = 'stale'
                task['error'] = '渲染输入快照已变化；需显式重建计划。'
        _atomic_json(path, plan)
        return copy.deepcopy(plan)


def _blocked_reason(plan: dict[str, Any]) -> str | None:
    for task in plan.get('tasks', []):
        if task.get('status') in ('failed', 'billing_unknown', 'interrupted'):
            return f"任务 {task.get('key')} 状态为 {task['status']}，必须先核实，不能自动重试。"
    return None


def claim_task(plan_path: str | Path, task_id: str) -> dict[str, Any]:
    """Claim one dependency-ready task; this only changes plan state."""
    path = Path(plan_path)
    with _file_lock(path.with_suffix(path.suffix + '.lock')):
        plan = json.loads(path.read_text(encoding='utf-8-sig'))
        if plan.get('stale') or plan.get('status') == 'stale':
            raise ValueError('计划输入已变化，不能领取任务。')
        if plan.get('stop_requested'):
            raise ValueError('计划已停止；显式恢复后才能领取任务。')
        blocked = _blocked_reason(plan)
        if blocked:
            raise ValueError(blocked)
        if any(task.get('status') == 'running' for task in plan['tasks']):
            raise ValueError('已有任务正在运行；同一计划一次只允许一个活动任务。')
        task = next((t for t in plan['tasks'] if t['id'] == task_id), None)
        if not task:
            raise ValueError('任务编号不属于当前计划。')
        if task.get('status') != 'queued':
            raise ValueError('任务不是可领取状态。')
        if task.get('stage') == 'dialogue_attribution':
            binding = plan.get('inputs', {}).get('attribution_bindings', {}).get(task.get('chapter_id'))
            if (not isinstance(binding, dict) or binding.get('book_id') != plan.get('book_id')
                    or binding.get('chapter_id') != task.get('chapter_id')
                    or not _valid_hash(binding.get('chapter_sha256'))
                    or not isinstance(binding.get('dialogue_text_sha256'), dict)):
                raise ValueError('对白归属发送前必须存在捕获的本章原文绑定。')
        if task.get('stage') == 'chapter_render':
            binding = plan.get('inputs', {}).get('attribution_bindings', {}).get(task.get('chapter_id'))
            expected_binding_hash = task.get('details', {}).get('attribution_binding_sha256')
            if (not isinstance(binding, dict) or binding.get('book_id') != plan.get('book_id')
                    or binding.get('chapter_id') != task.get('chapter_id')
                    or not _valid_hash(binding.get('chapter_sha256'))
                    or not isinstance(binding.get('dialogue_text_sha256'), dict)
                    or not _valid_hash(expected_binding_hash) or _digest(binding) != expected_binding_hash):
                raise ValueError('渲染前缺少计划快照中的可信章节对白绑定。')
            expansion = plan.get('voice_expansion') or {}
            expansion_id = expansion.get('expansion_task_id')
            if not expansion_id or expansion_id not in task.get('dependencies', []):
                raise ValueError('章节渲染必须依赖已完成的 voice task expansion 屏障。')
            expansion_task = next((item for item in plan['tasks'] if item.get('id') == expansion_id), None)
            if not expansion_task or expansion_task.get('status') != 'complete':
                raise ValueError('voice task expansion 尚未完成。')
            fusion = next((item for item in plan['tasks'] if item.get('id') == expansion.get('fusion_task_id')), None)
            fusion_result = (fusion or {}).get('result') or {}
            expansion_result = expansion_task.get('result') or {}
            if (not fusion or fusion.get('status') != 'complete'
                    or fusion_result.get('checkpoint_receipt_validated') is not True
                    or expansion_result.get('checkpoint_receipt_validated') is not True
                    or expansion_result.get('checkpoint_receipt_sha256')
                    != fusion_result.get('checkpoint_receipt_sha256')):
                raise ValueError('渲染前必须完成与融合收据绑定的角色任务扩展。')
            planned_synthesis = {ident for ident in expansion.get('created_task_ids', [])
                                 if (next((item for item in plan['tasks'] if item.get('id') == ident), {})
                                     .get('stage') == 'local_voice_synthesis')}
            if not planned_synthesis.issubset(set(task.get('dependencies', []))):
                raise ValueError('章节渲染未依赖全部已规划角色声音合成任务。')
        statuses = {t['id']: t['status'] for t in plan['tasks']}
        if any(statuses.get(dep) != 'complete' for dep in task.get('dependencies', [])):
            raise ValueError('任务依赖尚未全部完成。')
        task['status'] = 'running'; task['attempt'] += 1
        task['idempotency_key'] = f"{plan['plan_id']}:{plan['revision']}:{task['key']}:r{task.get('redo_generation', 0)}"
        task['started_at'] = time.time()
        plan['status'] = 'running'
        _atomic_json(path, plan)
        return copy.deepcopy(task)


def complete_task(plan_path: str | Path, task_id: str, *, result: dict[str, Any] | None = None,
                  idempotency_key: str | None = None) -> dict[str, Any]:
    path = Path(plan_path)
    with _file_lock(path.with_suffix(path.suffix + '.lock')):
        plan = json.loads(path.read_text(encoding='utf-8-sig'))
        task = next((t for t in plan['tasks'] if t['id'] == task_id), None)
        if not task or task.get('status') != 'running':
            raise ValueError('只有运行中的任务可以报告完成。')
        if idempotency_key is not None and idempotency_key != task.get('idempotency_key'):
            raise ValueError('任务幂等键不匹配。')
        if result is not None and not isinstance(result, dict):
            raise ValueError('任务结果必须为对象。')
        recorded_result = copy.deepcopy(result or {})
        recorded_result['generation'] = 'automatic'
        recorded_result['human_reviewed'] = False
        task['status'] = 'complete'; task['result'] = recorded_result; task['error'] = None
        task['completed_at'] = time.time()
        plan['status'] = ('stale' if plan.get('stale') else
                          'stopped' if plan.get('stop_requested') else 'ready')
        if all(t.get('status') in ('complete', 'skipped') for t in plan['tasks']):
            plan['status'] = 'complete'
        _atomic_json(path, plan)
        return copy.deepcopy(task)


def fail_task(plan_path: str | Path, task_id: str, *, error: str,
              billing_unknown: bool = False) -> dict[str, Any]:
    path = Path(plan_path)
    with _file_lock(path.with_suffix(path.suffix + '.lock')):
        plan = json.loads(path.read_text(encoding='utf-8-sig'))
        task = next((t for t in plan['tasks'] if t['id'] == task_id), None)
        if not task or task.get('status') != 'running':
            raise ValueError('只有运行中的任务可以标记失败。')
        task['status'] = 'billing_unknown' if billing_unknown else 'failed'
        task['error'] = str(error)[:2000]
        plan['status'] = 'blocked'
        _atomic_json(path, plan)
        return copy.deepcopy(task)


def reconcile_task(plan_path: str | Path, task_id: str, *, outcome: str,
                   evidence: str) -> dict[str, Any]:
    """Record a human reconciliation; retry requires explicit non-execution proof."""
    if outcome not in ('confirmed_complete', 'confirmed_not_executed', 'allow_retry'):
        raise ValueError('outcome 必须是 confirmed_complete、confirmed_not_executed 或 allow_retry。')
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError('协调结果必须附带核查依据。')
    path = Path(plan_path)
    with _file_lock(path.with_suffix(path.suffix + '.lock')):
        plan = json.loads(path.read_text(encoding='utf-8-sig'))
        task = next((t for t in plan['tasks'] if t['id'] == task_id), None)
        if not task or task.get('status') not in ('failed', 'billing_unknown', 'interrupted', 'running'):
            raise ValueError('任务当前不需要结果协调。')
        task.setdefault('reconciliations', []).append({'outcome': outcome, 'evidence': evidence[:2000], 'at': time.time()})
        if outcome == 'confirmed_complete':
            task['status'] = 'complete'; task['result'] = {
                'reconciled': True, 'generation': 'automatic', 'human_reviewed': False,
            }
        elif outcome in ('confirmed_not_executed', 'allow_retry'):
            task['status'] = 'queued'; task['error'] = None
            task.pop('idempotency_key', None)
        plan['status'] = ('stale' if plan.get('stale') else
                          'stopped' if plan.get('stop_requested') else 'ready')
        _atomic_json(path, plan)
        return copy.deepcopy(task)


def queue_redo(plan_path: str | Path, task_id: str, *, reason: str,
               confirm_redo: bool = False, cascade: bool = True) -> dict[str, Any]:
    """Explicitly queue a completed automatic task and invalidate its descendants.

    This only updates the plan; it never reruns the operation. Failed or
    billing-unknown work cannot use this path and must be reconciled first.
    """
    if confirm_redo is not True or not isinstance(reason, str) or not reason.strip():
        raise ValueError('重做需要显式 confirm_redo=True 和非空 reason。')
    if cascade is not True:
        raise ValueError('上游任务只能级联重做；不支持保留下游旧结果。')
    path = Path(plan_path)
    with _file_lock(path.with_suffix(path.suffix + '.lock')):
        plan = json.loads(path.read_text(encoding='utf-8-sig'))
        if plan.get('stale') or plan.get('stop_requested'):
            raise ValueError('计划 stale 或已停止，不能安排重做。')
        if _blocked_reason(plan):
            raise ValueError(_blocked_reason(plan))
        if any(t.get('status') == 'running' for t in plan['tasks']):
            raise ValueError('存在运行中任务，不能同时安排重做。')
        tasks = {task['id']: task for task in plan['tasks']}
        target = tasks.get(task_id)
        if not target or not target.get('redoable') or target.get('status') != 'complete':
            raise ValueError('只有已完成且标记 redoable 的任务可以安排重做。')
        affected = {task_id}
        if cascade:
            changed = True
            while changed:
                changed = False
                for task in tasks.values():
                    if task['id'] not in affected and any(dep in affected for dep in task.get('dependencies', [])):
                        affected.add(task['id']); changed = True
        for ident in affected:
            task = tasks[ident]
            if task.get('status') == 'complete':
                task['history'].append({'status': 'complete', 'attempt': task.get('attempt', 0),
                                        'result': copy.deepcopy(task.get('result')),
                                        'completed_at': task.get('completed_at')})
                task['redo_generation'] = task.get('redo_generation', 0) + 1
                task['status'] = 'queued'; task['result'] = None; task['error'] = None
                task.pop('completed_at', None); task.pop('idempotency_key', None)
        plan.setdefault('redo_events', []).append({'task_id': task_id, 'affected_task_ids': sorted(affected),
                                                  'reason': reason.strip()[:1000], 'at': time.time()})
        plan['status'] = 'ready'
        _atomic_json(path, plan)
        return copy.deepcopy(plan)


def stop_plan(plan_path: str | Path, *, reason: str = 'operator_stop') -> dict[str, Any]:
    path = Path(plan_path)
    with _file_lock(path.with_suffix(path.suffix + '.lock')):
        plan = json.loads(path.read_text(encoding='utf-8-sig'))
        plan['stop_requested'] = True; plan['stop_reason'] = str(reason)[:500]
        if not any(t.get('status') == 'running' for t in plan['tasks']):
            plan['status'] = 'stopped'
        _atomic_json(path, plan)
        return copy.deepcopy(plan)


def resume_plan(plan_path: str | Path) -> dict[str, Any]:
    path = Path(plan_path)
    with _file_lock(path.with_suffix(path.suffix + '.lock')):
        plan = json.loads(path.read_text(encoding='utf-8-sig'))
        if plan.get('stale'):
            raise ValueError('计划已 stale，不能直接恢复；请显式重建。')
        if any(t.get('status') == 'running' for t in plan['tasks']):
            raise ValueError('存在运行中任务，须先确认该步骤结果再恢复。')
        blocked = _blocked_reason(plan)
        if blocked:
            raise ValueError(blocked)
        plan['stop_requested'] = False; plan.pop('stop_reason', None)
        plan['status'] = 'ready'
        _atomic_json(path, plan)
        return copy.deepcopy(plan)

