"""Book-scoped voice workbench UI.

Opening the window and loading an index do not generate audio or call a model.
The operator starts paid analysis explicitly from the Production tab.
"""
import hashlib
import json
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path

import workbench_real as existing
import workbench_real_flow as flow
import workbench_real_v2_client as client
import real_pipeline_workflow as workflow
import real_book_sequence as sequence
import correction_memory
import workbench_voice_panel as voice_panel
from audio_playback import AudioPlayback, PlaybackCancelled, parse_speed


class Selection:
    def __init__(self, request=flow.index_request):
        self.request = request
        self.book = None
        self.index_sha256 = None
        self.chapters = ()
        self.frozen = None

    def load(self, name):
        self.book = None
        self.index_sha256 = None
        self.chapters = ()
        self.frozen = None
        book = existing.BOOKS.get(name)
        if book is None:
            raise ValueError('书籍未登记')
        data = self.request('list', book)
        ids = data.get('chapters')
        digest = data.get('index_sha256')
        if (data.get('book_id') != book or data.get('human_reviewed') is not False
                or not isinstance(ids, list) or not ids
                or not all(isinstance(x, str) and re.fullmatch(r'c\d+(?:\.p\d+(?:\.\d+)?)?', x) for x in ids)
                or len(set(ids)) != len(ids) or not isinstance(digest, str)
                or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise ValueError('索引响应无效')
        self.book, self.index_sha256, self.chapters = book, digest, tuple(ids)
        return self.chapters

    def preview(self, name, chapter, voice_mapping):
        return self._prepare(name, chapter, voice_mapping)[0]

    def _prepare(self, name, chapter, voice_mapping):
        book = existing.BOOKS.get(name)
        if book != self.book or chapter not in self.chapters:
            raise ValueError('请先读取当前书的索引并选择章节')
        if not isinstance(voice_mapping, dict) or not voice_mapping:
            raise ValueError('自动声音配置无效')
        data = self.request('prepare', book, chapter)
        if (data.get('book_id') != book or data.get('human_reviewed') is not False
                or data.get('index_sha256') != self.index_sha256):
            raise ValueError('索引身份发生变化，请重新读取')
        payload = data.get('payload')
        if not isinstance(payload, dict) or payload.get('book_id') != book or payload.get('chapter_id') != chapter:
            raise ValueError('章节身份不一致')
        source = workflow._bytes(payload)
        if hashlib.sha256(source).hexdigest() != data.get('payload_sha256'):
            raise ValueError('章节内容哈希不一致')
        identity = client.definition(book, chapter, source, self.index_sha256, voice_mapping)
        return identity, json.loads(source)

    def preview_range(self, name, start, end, voice_mapping):
        if start not in self.chapters or end not in self.chapters:
            raise ValueError('请选择起止章节')
        first, last = self.chapters.index(start), self.chapters.index(end)
        if first > last:
            raise ValueError('结束章节必须不早于起始章节')
        frozen = [self._prepare(name, cid, voice_mapping) for cid in self.chapters[first:last + 1]]
        self.frozen = ([(identity, payload) for identity, payload in frozen],
                       json.loads(json.dumps(voice_mapping, ensure_ascii=False)))
        return {
            'book_id': self.book, 'start': start, 'end': end, 'chapters': len(frozen),
            'index_sha256': self.index_sha256,
            'voice_mapping_sha256': frozen[0][0]['voice_mapping_sha256'],
        }


def completed_audio(sequence_root, book_id, chapter_id):
    """Return a verified chapter WAV path from an existing render.done receipt."""
    root = sequence_root / book_id / chapter_id
    done_path = root / 'render.done.json'
    done = json.loads(done_path.read_text(encoding='utf-8'))
    identity = done.get('identity', {})
    if (done.get('status') != 'ok' or done.get('human_reviewed') is not False
            or identity.get('book_id') != book_id or identity.get('chapter_id') != chapter_id
            or identity.get('stage') != 'render'):
        raise ValueError('章节渲染回执状态无效')
    artifact = done.get('artifact')
    if not isinstance(artifact, dict) or hashlib.sha256(json.dumps(
            artifact, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
    ).encode('utf-8')).hexdigest() != done.get('artifact_sha256'):
        raise ValueError('章节产物清单哈希不匹配')
    receipt_path = Path(artifact['path'])
    receipt_raw = receipt_path.read_bytes()
    if hashlib.sha256(receipt_raw).hexdigest() != artifact.get('sha256'):
        raise ValueError('渲染回执文件哈希不匹配')
    receipt_file = json.loads(receipt_raw.decode('utf-8'))
    renderer_receipt = receipt_file.get('receipt')
    if not isinstance(renderer_receipt, dict):
        raise ValueError('缺少渲染器回执')
    manifest = Path(renderer_receipt['manifest_path'])
    manifest_raw = manifest.read_bytes()
    if hashlib.sha256(manifest_raw).hexdigest() != renderer_receipt.get('manifest_sha256'):
        raise ValueError('音频清单哈希不匹配')
    audio = Path(renderer_receipt['chapter_wav'])
    if hashlib.sha256(audio.read_bytes()).hexdigest() != renderer_receipt.get('wav_sha256'):
        raise ValueError('章节 WAV 哈希不匹配')
    return audio


def _sequence_root(runtime_root, book_id, chapter_id=None):
    """Resolve the job-specific sequence base lazily, with legacy layout fallback."""
    try:
        import workbench_job_store
    except ModuleNotFoundError as exc:
        if exc.name != 'workbench_job_store':
            raise
        return Path(runtime_root) / 'sequence'
    return Path(workbench_job_store.find_sequence_root(runtime_root, book_id, chapter_id))


def verified_correction_source(selection, name, chapter_id):
    """Fetch a chapter through the loaded index and verify its source identity."""
    book = existing.BOOKS.get(name)
    if book != selection.book or chapter_id not in selection.chapters or not selection.index_sha256:
        raise ValueError('请先读取当前书籍的登记索引并选择有效章节。')
    data = selection.request('prepare', book, chapter_id)
    if (data.get('book_id') != book or data.get('human_reviewed') is not False
            or data.get('index_sha256') != selection.index_sha256):
        raise ValueError('索引身份发生变化，请重新读取。')
    payload = data.get('payload')
    if not isinstance(payload, dict) or payload.get('book_id') != book or payload.get('chapter_id') != chapter_id:
        raise ValueError('章节身份不一致。')
    source = workflow._bytes(payload)
    payload_digest = hashlib.sha256(source).hexdigest()
    if payload_digest != data.get('payload_sha256'):
        raise ValueError('章节内容哈希不一致。')
    verified_payload = json.loads(source)
    source_digest = hashlib.sha256(verified_payload['text'].encode('utf-8')).hexdigest()
    return book, verified_payload, source_digest


def profile_result_summary(result):
    if not isinstance(result, dict):
        return '画像任务返回未知格式。'
    batches = result.get('batches') if isinstance(result.get('batches'), list) else []
    known_input = known_output = known_batches = 0
    failed_batches = invalid_batches = 0
    for batch in batches:
        if isinstance(batch, dict):
            if batch.get('status') in ('failed', 'error') or batch.get('error'):
                failed_batches += 1
            if batch.get('status') in ('invalid', 'invalid_result') or batch.get('valid') is False:
                invalid_batches += 1
        usage = batch.get('usage') if isinstance(batch, dict) else None
        if (isinstance(usage, dict) and type(usage.get('prompt_tokens')) is int
                and type(usage.get('completion_tokens')) is int
                and usage['prompt_tokens'] >= 0 and usage['completion_tokens'] >= 0):
            known_input += usage['prompt_tokens']
            known_output += usage['completion_tokens']
            known_batches += 1
    status = result.get('status', 'unknown')
    aggregate = result.get('usage') if isinstance(result.get('usage'), dict) else {}
    unknown_batches = aggregate.get('unknown_usage_batches', aggregate.get('unknown_batches'))
    if type(aggregate.get('known_usage_batches')) is int:
        known_batches = aggregate['known_usage_batches']
    if type(aggregate.get('prompt_tokens')) is int and aggregate['prompt_tokens'] >= 0:
        known_input = aggregate['prompt_tokens']
    if type(aggregate.get('completion_tokens')) is int and aggregate['completion_tokens'] >= 0:
        known_output = aggregate['completion_tokens']
    total_batches = ((aggregate['known_usage_batches'] if type(aggregate.get('known_usage_batches')) is int else len(batches))
                     + (unknown_batches if type(unknown_batches) is int and unknown_batches > 0 else 0))
    total = (f'；已知用量 {known_input:,}/{known_output:,} tokens（输入/输出，{known_batches}/{total_batches} 批）'
             if known_batches else '；用量未知，请查看费用窗口中的本机回执状态')
    if type(unknown_batches) is int and unknown_batches > 0:
        total += f'；另有 {unknown_batches} 批用量未知'
    batch_notes = f'；失败批次 {failed_batches}，无效批次 {invalid_batches}'
    return (f"画像状态：{status}；范围 {result.get('start', '?')}–{result.get('end', '?')}；"
            f'{len(batches)} 个批次{total}{batch_notes}。')


def _read_sequence_plan(sequence_root, book_id):
    path = Path(sequence_root) / book_id / 'plan.json'
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError('章节计划是符号链接，拒绝读取。')
    plan = json.loads(path.read_text(encoding='utf-8'))
    if (plan.get('book_id') != book_id or plan.get('human_reviewed') is not False
            or not isinstance(plan.get('chapters'), list)):
        raise ValueError('章节计划身份或审核标记无效。')
    return plan


def _verified_stage_done(sequence_root, book_id, chapter_id, stage, plan):
    path = Path(sequence_root) / book_id / chapter_id / (stage + '.done.json')
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError(f'{stage}完成回执是符号链接，拒绝读取。')
    done = json.loads(path.read_text(encoding='utf-8'))
    identity = done.get('identity') if isinstance(done.get('identity'), dict) else {}
    artifact = done.get('artifact')
    if (done.get('status') != 'ok' or done.get('human_reviewed') is not False
            or identity.get('book_id') != book_id or identity.get('chapter_id') != chapter_id
            or identity.get('stage') != stage or not isinstance(artifact, dict)
            or sequence.fingerprint(artifact) != done.get('artifact_sha256')):
        raise ValueError(f'{stage}完成回执身份或artifact hash不匹配。')
    if plan is None:
        raise ValueError(f'{stage}完成回执存在但章节计划缺失。')
    chapter = next((row for row in plan['chapters'] if isinstance(row, dict) and row.get('id') == chapter_id), None)
    if (chapter is None or identity.get('plan_sha256') != sequence.fingerprint(plan)
            or ('index_sha256' in identity and identity.get('index_sha256') != plan.get('index_sha256'))
            or identity.get('voice_bindings_sha256') != plan.get('voice_bindings_sha256')):
        raise ValueError(f'{stage}完成回执与章节计划不匹配。')
    return done


def _read_hash_pinned_json(pointer, allowed_root, label):
    if (not isinstance(pointer, dict) or not isinstance(pointer.get('path'), str)
            or not isinstance(pointer.get('sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', pointer['sha256'])):
        raise ValueError(f'{label}文件指针格式无效。')
    base = Path(allowed_root).resolve(strict=True)
    original = Path(pointer['path'])
    if not original.is_absolute() or original.is_symlink():
        raise ValueError(f'{label}文件路径无效或为符号链接。')
    resolved = original.resolve(strict=True)
    if not resolved.is_relative_to(base) or not resolved.is_file():
        raise ValueError(f'{label}文件不在本任务数据根内。')
    current = base
    for part in resolved.relative_to(base).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f'{label}路径包含符号链接。')
    raw = resolved.read_bytes()
    if hashlib.sha256(raw).hexdigest() != pointer['sha256']:
        raise ValueError(f'{label}文件hash不匹配。')
    return json.loads(raw.decode('utf-8')), resolved


_LITERAL_DOWNGRADE_REASONS = {
    'unproven_profile_alias', 'unproven_existing_alias', 'unproven_alias_ignored',
    'profile_alias_conflict', 'ambiguous_profile_alias', 'ambiguous',
}


def _diagnostic_issue(row):
    if not isinstance(row, dict):
        return {'reason': 'invalid_issue_row'}
    keys = ('type', 'reason', 'name', 'candidate_name', 'alias', 'aliases',
            'possible_profiles', 'possible_entities', 'field', 'blocking', 'status',
            'count', 'entity_id', 'chapter_name', 'labels', 'source', 'block_id',
            'candidate_index')
    result = {key: row[key] for key in keys if key in row and isinstance(
        row[key], (str, bool, int, list, type(None)))}
    if row.get('type') == 'local_attribution_normalization' and isinstance(row.get('changes'), list):
        result['count'] = len(row['changes'])
    candidates = row.get('candidates')
    if isinstance(candidates, list):
        result['candidates'] = [item.get('name') for item in candidates
                                if isinstance(item, dict) and isinstance(item.get('name'), str)]
    return result


def task_diagnostics(runtime_root, book_id, chapter_id, *, sequence_root=None, memory_done=None):
    """Verify the memory stage and its auto/profile pointers before showing reasons."""
    sequence_root = (Path(sequence_root) if sequence_root is not None else
                     _sequence_root(runtime_root, book_id, chapter_id))
    plan = _read_sequence_plan(sequence_root, book_id)
    done = memory_done if memory_done is not None else _verified_stage_done(
        sequence_root, book_id, chapter_id, 'memory', plan)
    if done is None:
        return {'status': 'not_started', 'book_id': book_id, 'chapter_id': chapter_id,
                'profile_blocked': False, 'blocking_conflicts': [], 'unresolved': [],
                'narrator_only': [], 'literal_evidence_downgrades': [],
                'profile_conflicts': [], 'alias_conflicts': [], 'unresolved_profiles': [],
                'profile_registry_status': 'memory_done缺失；未读取当前画像作回退'}

    job_root = Path(sequence_root).resolve(strict=True).parent
    artifact_pointer = done.get('artifact')
    if not isinstance(artifact_pointer, dict):
        raise ValueError('memory完成回执artifact必须是对象。')
    memory_doc, _ = _read_hash_pinned_json(artifact_pointer, job_root, 'memory artifact')
    plan_chapter = next((row for row in (plan or {}).get('chapters', [])
                         if isinstance(row, dict) and row.get('id') == chapter_id), None)
    if (memory_doc.get('book_id') != book_id or memory_doc.get('chapter_id') != chapter_id
            or memory_doc.get('human_reviewed') is not False
            or (plan_chapter and memory_doc.get('source_sha256') != plan_chapter.get('source_sha256'))):
        raise ValueError('memory artifact书籍/章节/source身份或审核标记无效。')
    memory_unresolved = memory_doc.get('unresolved', [])
    if not isinstance(memory_unresolved, list):
        raise ValueError('memory unresolved格式无效。')
    memory_literal_downgrades = _memory_literal_downgrades(memory_unresolved)
    if not isinstance(artifact_pointer.get('auto'), dict):
        return {'status': 'missing_auto_pointer', 'book_id': book_id, 'chapter_id': chapter_id,
                'profile_blocked': bool(artifact_pointer.get('profile_blocked')),
                'blocking_conflicts': _diagnostic_rows(artifact_pointer.get('blocking_conflicts', []), 'blocking_conflicts'),
                'unresolved': [], 'memory_unresolved': _diagnostic_rows(memory_unresolved, 'memory_unresolved'),
                'narrator_only': [], 'literal_evidence_downgrades': memory_literal_downgrades, 'entities': [],
                'bound_roles': [], 'unbound_roles': [],
                'profile_conflicts': [], 'alias_conflicts': [], 'unresolved_profiles': [],
                'profile_registry_status': 'memory artifact未记录auto指针；未回退到其他文件'}
    auto_doc, _ = _read_hash_pinned_json(artifact_pointer['auto'], job_root, 'auto-voices artifact')
    if (auto_doc.get('book_id') != book_id or auto_doc.get('chapter_id') != chapter_id
            or auto_doc.get('human_reviewed') is not False or type(auto_doc.get('blocked')) is not bool):
        raise ValueError('auto-voices artifact身份或审核标记无效。')
    blockers = auto_doc.get('blocking_conflicts', [])
    unresolved = auto_doc.get('unresolved', [])
    entities = auto_doc.get('entities', [])
    bindings = auto_doc.get('bindings', {})
    if (not isinstance(blockers, list) or not isinstance(unresolved, list)
            or not isinstance(entities, list) or not isinstance(bindings, dict)):
        raise ValueError('auto-voices诊断数组格式无效。')
    if ('profile_blocked' in artifact_pointer and artifact_pointer['profile_blocked'] is not auto_doc['blocked']):
        raise ValueError('memory与auto-voices profile_blocked标记不一致。')
    if ('blocking_conflicts' in artifact_pointer and artifact_pointer['blocking_conflicts'] != blockers):
        raise ValueError('memory与auto-voices blocking_conflicts不一致。')

    profile_conflicts, alias_conflicts, unresolved_profiles = [], [], []
    registry_pointer = artifact_pointer.get('profile_registry')
    registry_status = 'memory artifact未记录profile registry指针；未回退到current.json'
    if registry_pointer is not None:
        registry, _ = _read_hash_pinned_json(registry_pointer, runtime_root, 'profile registry snapshot')
        if (registry.get('book_id') != book_id or registry.get('mode') != 'character_profiles_v1'
                or registry.get('human_reviewed') is not False):
            raise ValueError('profile registry snapshot身份或审核标记无效。')
        profile_conflicts = _diagnostic_rows(registry.get('profile_conflicts', []), 'profile_conflicts')
        alias_conflicts = _diagnostic_rows(registry.get('alias_conflicts', []), 'alias_conflicts')
        unresolved_profiles = _diagnostic_rows(registry.get('unresolved_profiles', []), 'unresolved_profiles')
        registry_status = 'hash和书籍身份已核验'

    narrator_only = [row for row in unresolved if isinstance(row, dict)
                     and row.get('reason') == 'narrator_only_no_direct_character_voice']
    all_issues = [*blockers, *unresolved, *profile_conflicts, *alias_conflicts, *unresolved_profiles]
    literal_downgrades = [row for row in all_issues if isinstance(row, dict)
                          and (row.get('reason') in _LITERAL_DOWNGRADE_REASONS
                               or row.get('type') in _LITERAL_DOWNGRADE_REASONS)]
    literal_downgrades.extend(memory_literal_downgrades)
    entity_rows = _diagnostic_rows(entities, 'entities')
    bound_roles = [row for row in entity_rows if row.get('entity_id') in bindings]
    unbound_roles = [row for row in entity_rows if row.get('entity_id') not in bindings]
    return {
        'status': 'verified', 'book_id': book_id, 'chapter_id': chapter_id,
        'profile_blocked': auto_doc['blocked'] or artifact_pointer.get('profile_blocked') is True,
        'blocking_conflicts': _diagnostic_rows(blockers, 'blocking_conflicts'),
        'unresolved': _diagnostic_rows(unresolved, 'unresolved'),
        'memory_unresolved': _diagnostic_rows(memory_unresolved, 'memory_unresolved'),
        'narrator_only': _diagnostic_rows(narrator_only, 'narrator_only'),
        'literal_evidence_downgrades': _diagnostic_rows(literal_downgrades, 'literal_evidence_downgrades'),
        'entities': entity_rows, 'bound_roles': bound_roles, 'unbound_roles': unbound_roles,
        'profile_conflicts': profile_conflicts, 'alias_conflicts': alias_conflicts,
        'unresolved_profiles': unresolved_profiles,
        'profile_registry_status': registry_status,
    }


def _diagnostic_rows(rows, label):
    if not isinstance(rows, list):
        raise ValueError(label + ' must be a list')
    return [_diagnostic_issue(row) for row in rows]


def _memory_literal_downgrades(rows):
    """Expose downgrade reasons and IDs without copying raw source/model text."""
    change_types = {'dialogue_downgraded', 'character_candidate_dropped'}
    reasons = {'evidence_not_literal_source', 'missing_literal_evidence',
               'candidate_evidence_not_literal_source', 'candidate_name_missing_from_evidence'}
    output = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get('type') == 'rejected_character_evidence':
            output.append(_diagnostic_issue(row))
        elif row.get('type') == 'local_attribution_normalization':
            changes = row.get('changes')
            if not isinstance(changes, list):
                raise ValueError('local_attribution_normalization changes must be a list')
            for change in changes:
                if (not isinstance(change, dict) or change.get('type') not in change_types
                        or change.get('reason') not in reasons):
                    raise ValueError('local attribution normalization change is invalid')
                item = {'source': 'local_attribution_normalization',
                        'type': change['type'], 'reason': change['reason']}
                if 'id' in change:
                    if not isinstance(change['id'], str) or not change['id']:
                        raise ValueError('local attribution block ID is invalid')
                    item['block_id'] = change['id']
                if 'index' in change:
                    if type(change['index']) is not int or change['index'] < 0:
                        raise ValueError('local attribution candidate index is invalid')
                    item['candidate_index'] = change['index']
                if 'block_id' not in item and 'candidate_index' not in item:
                    raise ValueError('local attribution change has no safe item identity')
                output.append(item)
    return output


def inspect_chapter_progress(runtime_root, book_id, chapter_ids):
    """Read and verify persisted progress and audio only; never dispatch work."""
    output = []
    for chapter_id in chapter_ids:
        try:
            sequence_root = _sequence_root(runtime_root, book_id, chapter_id)
            plan = _read_sequence_plan(sequence_root, book_id)
            completed_stages = []
            memory_done = None
            render_done = None
            for stage in sequence.STAGES:
                done = _verified_stage_done(sequence_root, book_id, chapter_id, stage, plan)
                if done is None:
                    break
                completed_stages.append(stage)
                if stage == 'memory': memory_done = done
                if stage == 'render': render_done = done
            diagnostics = task_diagnostics(runtime_root, book_id, chapter_id,
                                           sequence_root=sequence_root, memory_done=memory_done)
            audio = completed_audio(sequence_root, book_id, chapter_id) if render_done else None
            output.append({'chapter_id': chapter_id, 'sequence_root': str(sequence_root),
                           'completed_stages': completed_stages, 'audio_path': str(audio) if audio else None,
                           'diagnostics': diagnostics, 'error': None})
        except Exception as exc:
            output.append({'chapter_id': chapter_id, 'sequence_root': None, 'completed_stages': [],
                           'audio_path': None, 'diagnostics': None,
                           'error': type(exc).__name__ + ': ' + str(exc)})
    return output


class App(tk.Tk):
    def __init__(self, executor=None, voice_config=None, runtime_module=None, player=None):
        super().__init__()
        self.title('有声书工作台')
        self.geometry('1050x720')
        self.minsize(820, 600)
        self.selection = Selection()
        self.executor = executor
        self.managed_executor = executor is None
        self.runtime_module = runtime_module
        self.voice_config = voice_config
        self.player = player if player is not None else AudioPlayback()
        self.play_speed = tk.StringVar(value='1.0')
        self.playback_generation = 0
        self.messages = queue.Queue()
        self.busy = False
        self.busy_kind = None
        self.generation = 0
        self.completed = {}
        self.active_range = None
        self._last_progress_probe = 0.0

        self.book = tk.StringVar(value='')
        self.start_chapter = tk.StringVar()
        self.end_chapter = tk.StringVar()
        self.status = tk.StringVar(value='先读取登记索引，再选择章节范围。')
        self.selection_info = tk.StringVar(value='未读取索引')
        self.task_info = tk.StringVar(value='尚无任务')

        shell = ttk.Frame(self, padding=(18, 16))
        shell.pack(fill='both', expand=True)
        ttk.Label(shell, text='有声书工作台', font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(shell, text='选择已登记的章节范围。分析请求会发送给 DeepSeek，配音会使用本机隔离候选音色；操作不会自动重试。',
                  wraplength=980).pack(anchor='w', pady=(4, 12))
        self.tabs = ttk.Notebook(shell)
        self.tabs.pack(fill='both', expand=True)
        self.production = ttk.Frame(self.tabs, padding=14)
        self.people_tab = ttk.Frame(self.tabs, padding=10)
        self.voice_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.production, text='制作')
        self.tabs.add(self.people_tab, text='人物')
        self.tabs.add(self.voice_tab, text='声音')
        self._build_production()

        self.people = voice_panel.ProfileView(self.people_tab)
        self.people.correction_callback = self.open_correction_dialog
        self.people.pack(fill='both', expand=True)
        self.voices = voice_panel.VoiceView(self.voice_tab, self._runtime_root,
                                            on_busy=self._set_busy,
                                            audio_player=self.player,
                                            on_playback_state=self._set_playback_state,
                                            on_stop=self._stop_shared_playback,
                                            speed_var=self.play_speed)
        self.voices.pack(fill='both', expand=True)
        self.book.trace_add('write', self.changed)
        self.tabs.bind('<<NotebookTabChanged>>', self._tab_changed)
        self.after(100, self.poll)

    def _build_production(self):
        row = ttk.Frame(self.production)
        row.pack(fill='x')
        ttk.Label(row, text='书籍').pack(side='left')
        self.book_box = ttk.Combobox(row, textvariable=self.book, values=tuple(existing.BOOKS),
                                     state='readonly', width=24)
        self.book_box.pack(side='left', padx=(6, 16))
        self.load_button = ttk.Button(row, text='读取登记索引', command=self.load_index)
        self.load_button.pack(side='left')
        self.correction_button = ttk.Button(row, text='纠错说明', command=self.open_correction_dialog)
        self.correction_button.pack(side='left', padx=(8, 0))
        ttk.Label(row, text='起始章节').pack(side='left', padx=(22, 4))
        self.start_box = ttk.Combobox(row, textvariable=self.start_chapter, state='readonly', width=22)
        self.start_box.pack(side='left')
        ttk.Label(row, text='结束章节').pack(side='left', padx=(12, 4))
        self.end_box = ttk.Combobox(row, textvariable=self.end_chapter, state='readonly', width=22)
        self.end_box.pack(side='left')

        action_row = ttk.Frame(self.production)
        action_row.pack(fill='x', pady=(18, 8))
        self.profile_button = ttk.Button(action_row, text='分析所选章节人物', command=self.analyze_profiles)
        self.profile_button.pack(side='left')
        self.progress_button = ttk.Button(action_row, text='查看所选章节进度', command=self.inspect_progress)
        self.progress_button.pack(side='left', padx=(8, 0))
        self.execute_button = ttk.Button(action_row, text='开始分析与配音', command=self.execute)
        self.execute_button.pack(side='left', padx=(8, 0))
        self.pause_button = ttk.Button(action_row, text='当前阶段完成后暂停',
                                       command=self.pause_after_stage, state='disabled')
        self.pause_button.pack(side='left', padx=8)
        ttk.Button(action_row, text='费用估算', command=self.billing).pack(side='left', padx=8)
        ttk.Button(action_row, text='高级身份详情', command=self.advanced_details).pack(side='right')
        ttk.Label(self.production, textvariable=self.status, wraplength=960).pack(anchor='w', pady=(3, 5))
        ttk.Label(self.production, textvariable=self.selection_info).pack(anchor='w')
        ttk.Separator(self.production).pack(fill='x', pady=12)
        ttk.Label(self.production, text='章节任务', font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w')
        self.task_tree = ttk.Treeview(self.production, columns=('chapter', 'state', 'audio'),
                                      show='headings', height=12, selectmode='browse')
        for col, title, width in (('chapter', '章节', 190), ('state', '状态', 220), ('audio', '已验证音频', 500)):
            self.task_tree.heading(col, text=title)
            self.task_tree.column(col, width=width, anchor='w')
        self.task_tree.pack(fill='both', expand=True, pady=(7, 5))
        footer = ttk.Frame(self.production)
        footer.pack(fill='x')
        self.diagnostics_button = ttk.Button(footer, text='查看人物归属原因', command=self.show_diagnostics)
        self.diagnostics_button.pack(side='left')
        ttk.Label(footer, textvariable=self.task_info).pack(side='left')
        ttk.Label(footer, text='倍速（0.8–1.5×，保持音高）').pack(side='right', padx=(0, 5))
        self.speed_box = ttk.Spinbox(footer, from_=0.8, to=1.5, increment=0.01,
                                     textvariable=self.play_speed, width=6)
        self.speed_box.pack(side='right', padx=(0, 8))
        self.play_button = ttk.Button(footer, text='播放选中章节', command=self.play_selected, state='disabled')
        self.play_button.pack(side='right')
        self.stop_button = ttk.Button(footer, text='停止播放', command=self.stop_playback, state='disabled')
        self.stop_button.pack(side='right', padx=(0, 8))

    def _runtime(self):
        if self.runtime_module is not None:
            return self.runtime_module
        import workbench_real_v2_runtime as runtime
        self.runtime_module = runtime
        return runtime

    def _runtime_root(self):
        return self._runtime().ROOT

    def _load_profile_panels(self):
        book = existing.BOOKS.get(self.book.get())
        if not book:
            self.people.show_empty('请选择已登记书籍。')
            return
        self.people.load(self._runtime_root(), book)
        self.voices.load_book(book, self.people.document, self.people.plans)

    def changed(self, *_):
        self.generation += 1
        self._stop_shared_playback()
        self._set_playback_state(False)
        self.selection = Selection()
        self.start_chapter.set('')
        self.end_chapter.set('')
        self.start_box['values'] = ()
        self.end_box['values'] = ()
        self.selection_info.set('未读取索引')
        self.task_info.set('尚无任务')
        self.completed = {}
        self.task_tree.delete(*self.task_tree.get_children())
        self.active_range = None
        self.play_button.configure(state='disabled')
        self.status.set('书籍已切换，请重新读取登记索引。')
        if self.managed_executor:
            self.executor = None
        if not self.busy:
            try:
                self._load_profile_panels()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.people.show_empty('人物档案暂不可读：' + str(exc))
                self.voices.show_empty('人物档案暂不可读；未生成任何声音。')

    def _tab_changed(self, _event=None):
        if self.tabs.index(self.tabs.select()) in (1, 2) and not self.busy:
            try:
                self._load_profile_panels()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.people.show_empty('人物档案暂不可读：' + str(exc))
                self.voices.show_empty('人物档案暂不可读；未生成任何声音。')

    def _set_busy(self, busy):
        if not busy:
            self.busy_kind = None
        elif not self.busy:
            self.busy_kind = None
        if self.busy != busy:
            self.busy = busy
        self.execute_button.configure(state='disabled' if busy else 'normal')
        self.profile_button.configure(state='disabled' if busy else 'normal')
        self.load_button.configure(state='disabled' if busy else 'normal')
        self.book_box.configure(state='disabled' if busy else 'readonly')
        self.start_box.configure(state='disabled' if busy else 'readonly')
        self.end_box.configure(state='disabled' if busy else 'readonly')
        self.pause_button.configure(state='normal' if self._can_pause() else 'disabled')
        self.play_button.configure(state='disabled' if busy or not self.completed else 'normal')
        self.speed_box.configure(state='disabled' if busy else 'normal')
        if hasattr(self, 'voices'):
            self.voices.set_external_busy(busy)

    def _can_pause(self):
        return (self.busy and self.busy_kind == 'execute'
                and callable(getattr(self.executor, 'request_stop', None)))

    def pause_after_stage(self):
        request_stop = getattr(self.executor, 'request_stop', None)
        if not self._can_pause() or not callable(request_stop):
            self.status.set('当前执行器不支持阶段完成后暂停。')
            return
        try:
            request_stop()
            self.pause_button.configure(state='disabled')
            self.status.set('已请求暂停：当前分析或合成阶段完成后停止；不会取消当前请求或自动重发。')
        except Exception as exc:
            self.status.set('暂停请求失败：' + str(exc))

    def _run(self, kind, label, fn):
        if self.busy:
            return
        self.busy = True
        self.busy_kind = kind
        generation = self.generation
        self._set_busy(True)
        self.status.set(label)

        def worker():
            try:
                self.messages.put((generation, kind, fn(), None))
            except Exception as exc:
                self.messages.put((generation, kind, None, exc))

        threading.Thread(target=worker, daemon=True).start()

    def load_index(self):
        name = self.book.get()
        self._run('list', '正在读取受信章节索引…', lambda: self.selection.load(name))

    def execute(self):
        name = self.book.get()
        start, end = self.start_chapter.get(), self.end_chapter.get() or self.start_chapter.get()
        if not self.selection.chapters:
            self.status.set('请先读取登记索引。')
            return
        if not start or not end:
            self.status.set('请选择起始和结束章节。')
            return
        if (start not in self.selection.chapters or end not in self.selection.chapters
                or self.selection.chapters.index(start) > self.selection.chapters.index(end)):
            self.status.set('结束章节必须在索引顺序中不早于起始章节。')
            return
        if self.executor is None:
            try:
                config, executor = self._runtime().make_executor(name)
                self.voice_config, self.executor = config, executor
            except Exception as exc:
                self.status.set(str(exc))
                return
        executor, config, selection = self.executor, self.voice_config, self.selection
        if not isinstance(config, dict) or not config:
            self.status.set('工作台执行配置不可用；未发送分析请求。')
            return

        def run_range():
            preview = selection.preview_range(name, start, end, config)
            try:
                result = executor.execute_range(selection.frozen[0], selection.frozen[1])
            except Exception as exc:
                result = {'status': 'error', 'message': str(exc)}
            audio_paths = {}
            audio_errors = {}
            for chapter in chapter_ids:
                try:
                    sequence_root = _sequence_root(self._runtime_root(), existing.BOOKS[name], chapter)
                    audio_paths[chapter] = completed_audio(sequence_root, existing.BOOKS[name], chapter)
                except Exception as exc:
                    audio_errors[chapter] = type(exc).__name__
            return {'preview': preview, 'result': result, 'audio_paths': audio_paths,
                    'audio_errors': audio_errors}

        self.task_tree.delete(*self.task_tree.get_children())
        first, last = self.selection.chapters.index(start), self.selection.chapters.index(end)
        chapter_ids = self.selection.chapters[first:last + 1]
        self.active_range = (existing.BOOKS[name], chapter_ids)
        for chapter in chapter_ids:
            self.task_tree.insert('', 'end', iid=chapter, values=(chapter, '等待执行', '尚无已验证音频'))
        self.task_info.set(f'已排入 {len(chapter_ids)} 章；等待分析与配音执行器。')
        try:
            self._run('execute', '正在核对章节并执行分析与配音；失败不会自动重试。', run_range)
        except Exception as exc:
            self.status.set(str(exc))

    def analyze_profiles(self):
        name = self.book.get()
        start, end = self.start_chapter.get(), self.end_chapter.get() or self.start_chapter.get()
        if not self.selection.chapters:
            self.status.set('请先读取登记索引。')
            return
        if (not start or not end or start not in self.selection.chapters or end not in self.selection.chapters
                or self.selection.chapters.index(start) > self.selection.chapters.index(end)):
            self.status.set('请选择有效章节范围，结束章节须不早于起始章节。')
            return
        root = self._runtime_root()
        span = f'{start}–{end}'

        def run_analysis():
            from character_profile_jobs import analyze_profile_range
            return analyze_profile_range(name, start, end, root=root)

        self._run('profiles', f'正在分析 {span} 的人物画像；按服务回执处理，不会自动重发。', run_analysis)

    def inspect_progress(self):
        name = self.book.get()
        book = existing.BOOKS.get(name)
        if not self.selection.chapters or book != self.selection.book:
            self.status.set('请先读取当前书籍的登记索引。')
            return
        start, end = self.start_chapter.get(), self.end_chapter.get() or self.start_chapter.get()
        if start not in self.selection.chapters or end not in self.selection.chapters:
            self.status.set('请选择有效章节范围。')
            return
        first, last = self.selection.chapters.index(start), self.selection.chapters.index(end)
        if first > last:
            self.status.set('结束章节必须不早于起始章节。')
            return
        chapters = self.selection.chapters[first:last + 1]
        self.active_range = (book, chapters)
        self._run('progress', '正在只读核验所选章节进度；不会启动分析或配音。',
                  lambda: inspect_chapter_progress(self._runtime_root(), book, chapters))

    def show_diagnostics(self):
        selected = self.task_tree.selection()
        if not selected:
            self.status.set('请先选择章节。')
            return
        chapter = selected[0]
        book = existing.BOOKS.get(self.book.get())
        self._run('diagnostics', f'正在只读核验 {chapter} 的人物归属原因…',
                  lambda: task_diagnostics(self._runtime_root(), book, chapter))

    def open_correction_dialog(self, chapter_id=None, character_name=None):
        name = self.book.get()
        if not self.selection.chapters or self.selection.book != existing.BOOKS.get(name):
            self.status.set('请先读取当前书籍的登记索引，再添加纠错说明。')
            return
        chapter = chapter_id
        if chapter is None:
            selected = self.task_tree.selection()
            chapter = selected[0] if selected else self.start_chapter.get()
        if chapter not in self.selection.chapters:
            self.status.set('请选择已读取索引中的章节。')
            return
        window = tk.Toplevel(self)
        window.title('纠错说明（独立历史）')
        window.geometry('640x560')
        ttk.Label(window, text=f'书籍：{name}    章节：{chapter}').pack(anchor='w', padx=12, pady=(12, 4))
        ttk.Label(window, text='原始画像为只读证据，操作者说明在独立历史保存，后续分析引用说明。',
                  wraplength=600).pack(anchor='w', padx=12, pady=4)
        kind = tk.StringVar(value='reader_correction')
        character = tk.StringVar(value=character_name or '')
        kind_row = ttk.Frame(window); kind_row.pack(fill='x', padx=12, pady=4)
        ttk.Label(kind_row, text='说明类型').pack(side='left')
        ttk.Combobox(kind_row, textvariable=kind, state='readonly', width=26,
                     values=('reader_correction', 'summary_correction', 'pronunciation')).pack(side='left', padx=8)
        char_row = ttk.Frame(window); char_row.pack(fill='x', padx=12, pady=4)
        ttk.Label(char_row, text='人物（可留空）').pack(side='left')
        ttk.Entry(char_row, textvariable=character).pack(side='left', fill='x', expand=True, padx=8)
        note = tk.Text(window, wrap='word', height=8)
        note.pack(fill='both', expand=True, padx=12, pady=6)
        latest = tk.Text(window, wrap='word', height=8, state='disabled')
        latest.pack(fill='both', expand=True, padx=12, pady=6)
        info = tk.StringVar(value='正在读取本章来源并查找当前说明…')
        ttk.Label(window, textvariable=info, wraplength=600).pack(anchor='w', padx=12)

        def load_latest():
            try:
                book, _payload, source_sha = verified_correction_source(self.selection, name, chapter)
                rows = correction_memory.read_notes(self._runtime_root(), book, chapter, include_current=True)
                matches = [r for r in rows if r.get('chapter_id') == chapter
                           and r.get('source_sha256') == source_sha and r.get('kind') == kind.get()
                           and r.get('character_name') == (character.get().strip() or None)]
                latest.configure(state='normal'); latest.delete('1.0', 'end')
                if matches:
                    current = matches[-1]
                    note.delete('1.0', 'end'); note.insert('1.0', current['note'])
                    latest.insert('1.0', '当前版本（可编辑并另存为新历史版本）：\n' + current['note'])
                else:
                    latest.insert('1.0', '当前来源版本没有匹配的历史说明。')
                latest.configure(state='disabled')
                info.set('来源已核验；保存仅写入本机纠错说明，不调用模型。')
            except Exception as exc:
                info.set('无法读取说明：' + str(exc))

        def save():
            value = note.get('1.0', 'end-1c')
            if not value.strip() or len(value) > 2000:
                info.set('说明须为 1–2000 字。'); return
            selected_character = character.get().strip() or None
            def persist():
                book, payload, _digest = verified_correction_source(self.selection, name, chapter)
                return correction_memory.save_note(self._runtime_root(), payload, value,
                    kind=kind.get(), character_name=selected_character)
            self._correction_info = info
            self._run('correction_save', '正在核验章节来源并保存本地说明；不会调用模型。', persist)
            info.set('已提交后台保存；保存完成后会显示版本结果。')

        controls = ttk.Frame(window); controls.pack(fill='x', padx=12, pady=10)
        ttk.Button(controls, text='查看当前说明', command=load_latest).pack(side='left')
        ttk.Button(controls, text='保存为新版本', command=save).pack(side='right')
        load_latest()

    def billing(self):
        import console_voice_billing as billing
        BillingWindow = billing.BillingWindow
        BillingWindow(self, roots=[billing.ROOT, self._runtime_root()])

    def advanced_details(self):
        data = {
            'voice_mapping': self.voice_config,
            'identity_preview': self.selection.frozen[0] if self.selection.frozen else None,
            'index_sha256': self.selection.index_sha256,
        }
        window = tk.Toplevel(self)
        window.title('高级身份详情（只读）')
        window.geometry('780x460')
        view = tk.Text(window, wrap='none', font=('Consolas', 10))
        view.pack(fill='both', expand=True, padx=12, pady=12)
        view.insert('1.0', json.dumps(data, ensure_ascii=False, indent=2, default=str))
        view.configure(state='disabled')

    def _refresh_task_rows(self, preview, result, audio_paths, audio_errors=None):
        for item in self.task_tree.get_children():
            self.task_tree.delete(item)
        book = preview['book_id']
        self.completed = dict(audio_paths)
        for chapter in self.selection.chapters[
                self.selection.chapters.index(preview['start']):self.selection.chapters.index(preview['end']) + 1]:
            try:
                path = self.completed[chapter]
                state, audio_label = '已完成 · 回执与 WAV 哈希通过', str(path)
            except KeyError:
                if chapter in (audio_errors or {}):
                    state, audio_label = '存储定位或回执核验失败', '无可播放的已验证音频'
                else:
                    state, audio_label = '未完成或状态未知', '无可播放的已验证音频'
            self.task_tree.insert('', 'end', iid=chapter, values=(chapter, state, audio_label))
        status = result.get('status') if isinstance(result, dict) else None
        if status == 'error':
            self.task_info.set(f"任务失败：{result.get('message', '原因未知')}；已验证完成章节仍可播放。")
        else:
            self.task_info.set(f'任务返回状态：{status or "需查看高级详情"}；仅验证并列出已有完成回执。')
        self.play_button.configure(state='normal' if self.completed else 'disabled')
        self.active_range = None

    def _refresh_inflight(self):
        if not self.busy or self.busy_kind != 'execute' or not self.active_range:
            return
        now = time.monotonic()
        if now - self._last_progress_probe < 1.0:
            return
        self._last_progress_probe = now
        book, chapters = self.active_range
        labels = {'analyze': '分析回执已保存', 'memory': '跨章记忆已保存',
                  'stage': '对白分段已保存', 'render': '渲染回执已保存'}
        for chapter in chapters:
            state = '等待执行'
            try:
                sequence_root = _sequence_root(self._runtime_root(), book, chapter)
            except Exception as exc:
                if self.task_tree.exists(chapter):
                    self.task_tree.item(chapter, values=(chapter, '存储定位失败', type(exc).__name__))
                self.status.set(f'章节任务状态无法读取：{type(exc).__name__}；未回退到旧序列目录。')
                continue
            directory = sequence_root / book / chapter
            for stage in ('analyze', 'memory', 'stage', 'render'):
                try:
                    saved = json.loads((directory / (stage + '.done.json')).read_text(encoding='utf-8'))
                    if saved.get('status') != 'ok':
                        break
                    state = labels[stage]
                except (OSError, ValueError, TypeError):
                    break
            audio_label = '渲染完成后将在结束时核验 WAV 哈希' if state == labels['render'] else '尚无已验证音频'
            if self.task_tree.exists(chapter):
                self.task_tree.item(chapter, values=(chapter, state, audio_label))

    def play_selected(self):
        selection = self.task_tree.selection()
        if not selection:
            self.status.set('先选择一条已有完成音频的章节。')
            return
        if selection[0] not in self.completed:
            self.status.set('该章节没有通过回执和哈希核验的音频。')
            return
        chapter = selection[0]
        book = existing.BOOKS[self.book.get()]
        try:
            root = _sequence_root(self._runtime_root(), book, chapter)
        except Exception as exc:
            self.status.set(f'章节任务目录校验失败：{type(exc).__name__}；不会播放旧目录。')
            return
        try:
            speed = parse_speed(self.play_speed.get())
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.playback_generation += 1
        generation = self.playback_generation

        def prepare_playback():
            path = completed_audio(root, book, chapter)
            if callable(getattr(self.player, 'prepare', None)):
                prepared = self.player.prepare(path, speed)
            elif speed == 1.0 and callable(self.player):
                prepared = path
            else:
                raise RuntimeError('当前播放器不支持倍速处理。')
            if callable(getattr(self.player, 'start', None)):
                self.player.start(prepared)
            else:
                self.player(prepared)
            return generation, chapter, prepared

        self._set_playback_state(True)
        self._run('play', '正在重新核验章节回执与音频哈希…',
                  prepare_playback)

    def _stop_native_audio(self):
        try:
            import winsound
            winsound.PlaySound(None, 0)
        except (OSError, RuntimeError):
            pass

    def _set_playback_state(self, playing):
        state = 'normal' if playing else 'disabled'
        if hasattr(self, 'stop_button'):
            self.stop_button.configure(state=state)
        if hasattr(self, 'voices'):
            self.voices.set_playback_state(playing)

    def _stop_shared_playback(self):
        self.playback_generation += 1
        stop = getattr(self.player, 'stop', None)
        if callable(stop):
            stop()
        else:
            self._stop_native_audio()
        if hasattr(self, 'voices'):
            self.voices.invalidate_pending_playback()

    def stop_playback(self):
        try:
            self._stop_shared_playback()
        except Exception as exc:
            self.status.set('停止播放失败：' + str(exc))
            return
        self._set_playback_state(False)
        self.status.set('已停止播放。')

    def poll(self):
        try:
            while True:
                generation, kind, value, error = self.messages.get_nowait()
                self.busy = False
                self._set_busy(False)
                if generation != self.generation:
                    continue
                if error:
                    if kind == 'play':
                        self._set_playback_state(False)
                        if isinstance(error, PlaybackCancelled):
                            self.status.set('播放准备已取消。')
                            continue
                    self.status.set(f'{kind}失败：{error}；没有自动重试。')
                    error_status = getattr(error, 'status', None)
                    artifact_path = getattr(error, 'artifact_path', None)
                    retry_allowed = getattr(error, 'retry_allowed', False)
                    details = f'状态：{error_status or "未知"}；可重试标记：{retry_allowed}'
                    if artifact_path:
                        details += f'；证据：{artifact_path}'
                    partial = getattr(error, 'result', None)
                    if isinstance(partial, dict):
                        details = profile_result_summary(partial) + '；' + details
                    self.task_info.set(details + '。再次执行前请核对私有回执。')
                    if kind == 'profiles' and isinstance(partial, dict) and partial.get('current_path'):
                        try:
                            self._load_profile_panels()
                            self.status.set('画像批次部分完成，已载入已提交版本；未知批次未自动重发。')
                        except (OSError, ValueError, KeyError, TypeError) as exc:
                            self.status.set(f'画像部分结果已保存，但不能载入：{exc}；未知批次未自动重发。')
                    continue
                if kind == 'list':
                    self.start_box['values'] = value
                    self.end_box['values'] = value
                    self.start_chapter.set('')
                    self.end_chapter.set('')
                    self.selection_info.set(f'已读取 {len(value)} 个候选章节；索引 {self.selection.index_sha256[:12]}…，章节边界尚未人工审核。')
                    self.status.set('请选择起止章节。索引读取不会调用模型。')
                elif kind == 'profiles':
                    try:
                        self._load_profile_panels()
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        self.people.show_empty('画像已保存，但当前结构无法展示：' + str(exc))
                        self.voices.show_empty('画像已保存；校验失败，未生成候选声音。')
                    self.task_info.set(profile_result_summary(value))
                    profile_status = value.get('status') if isinstance(value, dict) else None
                    self.status.set('人物画像已载入；来源版本与逐章引文已保留，人物判断仍需人工核对。'
                                    if profile_status == 'needs_review' else
                                    f'画像任务返回状态：{profile_status or "未知"}；请先核对本机回执。')
                elif kind == 'execute':
                    preview, result = value['preview'], value['result']
                    self._refresh_task_rows(preview, result, value['audio_paths'], value.get('audio_errors'))
                    self.status.set('执行链已返回。章节分析、人物归属和音频仍需人工审核。')
                elif kind == 'progress':
                    self.task_tree.delete(*self.task_tree.get_children())
                    self.completed = {}
                    stage_names = {'analyze': '分析', 'memory': '记忆', 'stage': '分段', 'render': '渲染'}
                    for row in value:
                        chapter = row['chapter_id']
                        if row.get('error'):
                            state, audio_label = '核验失败', row['error']
                        else:
                            stages = row.get('completed_stages', [])
                            state = '已完成：' + '、'.join(stage_names.get(x, x) for x in stages) if stages else '尚无已验证阶段'
                            audio_label = row.get('audio_path') or '无可播放的已验证音频'
                            if row.get('audio_path'):
                                self.completed[chapter] = Path(row['audio_path'])
                        self.task_tree.insert('', 'end', iid=chapter, values=(chapter, state, audio_label))
                    self.play_button.configure(state='normal' if self.completed else 'disabled')
                    self.active_range = (existing.BOOKS[self.book.get()], tuple(x['chapter_id'] for x in value))
                    self.task_info.set(f'只读检查完成：{len(value)} 章；音频仅来自已核验的渲染回执。')
                    self.status.set('章节进度已核验；未启动分析或配音。选择章节可查看人物归属原因。')
                elif kind == 'diagnostics':
                    window = tk.Toplevel(self)
                    window.title('人物归属诊断（只读）')
                    window.geometry('760x520')
                    view = tk.Text(window, wrap='word', font=('Consolas', 10))
                    view.pack(fill='both', expand=True, padx=10, pady=10)
                    view.insert('1.0', json.dumps(value, ensure_ascii=False, indent=2, default=str))
                    view.configure(state='disabled')
                    self.status.set('已显示所选章节的核验诊断。')
                elif kind == 'correction_save':
                    self.task_info.set('纠错说明已在本机保存为独立版本；没有修改原始画像，也未调用模型。')
                    if getattr(self, '_correction_info', None) is not None:
                        self._correction_info.set('已保存独立历史版本；原始画像保持只读。')
                    self.status.set('纠错说明保存完成；后续分析可读取该说明。')
                elif kind == 'play':
                    generation, chapter, prepared = value
                    if generation != self.playback_generation:
                        discard = getattr(self.player, 'discard', None)
                        if callable(discard): discard(prepared)
                        continue
                    self._set_playback_state(True)
                    self.status.set(f'正在播放已重新核验章节：{chapter}')
        except queue.Empty:
            pass
        self._refresh_inflight()
        self.after(100, self.poll)


if __name__ == '__main__':
    App().mainloop()
