"""Profile and explicit voice-selection panels for the Tk workbench."""
import hashlib
import json
import threading
import uuid
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import auto_voice_planner
import real_book_auto_voice as auto
from audio_playback import AudioPlayback, PlaybackCancelled, parse_speed

_UNSET = object()


def entity_id(book_id, name):
    """Stable allocator identity shared by the profile workbench contract."""
    return 'e' + hashlib.sha256((book_id + '\0' + name).encode('utf-8')).hexdigest()[:24]


def read_profile_document(root, book_id):
    path = Path(root) / 'character-profiles' / book_id / 'current.json'
    document = json.loads(path.read_text(encoding='utf-8'))
    if (document.get('book_id') != book_id or document.get('mode') != 'character_profiles_v1'
            or document.get('human_reviewed') is not False):
        raise ValueError('人物档案书籍身份或审核状态无效。')
    plans = auto_voice_planner.plans_from_character_profiles(document)
    return document, plans


def compose_description(kind, text):
    """Wrap an editable Chinese description in the generator's creative guard."""
    text = text.strip()
    if not text:
        raise ValueError('请先填写声音描述。')
    if kind == 'narrator':
        prompt = ('为中文有声书设计虚构旁白的表演声音。声音描述：' + text
                  + ' 以下声线与表演细节均为创作选择，不作为人物事实；保持清晰、自然，避免模仿真人。')
    else:
        prompt = ('为中文有声书设计虚构角色的表演声音。声音描述：' + text
                  + ' 创作依据：操作者可试听并决定。以下声线与表演细节均为创作选择，不作为人物事实；不模仿真人。')
    return auto.creative_description(prompt)


def candidate_binding(book_id, entity, record):
    auto._validate_record(record)
    key = book_id + '\0' + entity
    pinned_record = dict(record, book_role_key=key)
    return {'console_key': key, 'record': pinned_record}


def select_candidate(root, book_id, entity, candidate, *, auditioned=False, design=None, spec_id=None):
    """Persist a candidate only after the view records an explicit audition."""
    if auditioned is not True:
        raise ValueError('候选必须先试听才能采用。')
    return auto.select_voice(root, book_id, entity, candidate,
                             basis='操作者试听后显式采用', design=design, spec_id=spec_id)


def _claim_line(label, claim):
    if not isinstance(claim, dict):
        return f'{label}：未知'
    state = {'explicit': '原文明确', 'inferred': '推断', 'unknown': '未知'}.get(claim.get('status'), '待核对')
    value = claim.get('value') or '未提供'
    return f'{label}：{value}（{state}）'


def _issue_mentions_profile(row, names):
    if not isinstance(row, dict):
        return False
    mentioned = set()
    for key in ('name', 'candidate_name', 'alias'):
        if isinstance(row.get(key), str): mentioned.add(row[key])
    for key in ('possible_profiles', 'aliases'):
        values = row.get(key)
        if isinstance(values, list): mentioned.update(x for x in values if isinstance(x, str))
    candidates = row.get('candidates')
    if isinstance(candidates, list):
        mentioned.update(item.get('name') for item in candidates if isinstance(item, dict) and isinstance(item.get('name'), str))
    candidate = row.get('candidate')
    if isinstance(candidate, dict) and isinstance(candidate.get('name'), str):
        mentioned.add(candidate['name'])
    return bool(mentioned & names)


def _global_issue_text(row, category):
    if not isinstance(row, dict): return category + '：无效记录'
    kind = row.get('reason') or row.get('type') or row.get('status') or '待核对'
    subject = row.get('candidate_name') or row.get('name') or row.get('alias') or ''
    aliases = row.get('aliases')
    if isinstance(aliases, list) and aliases:
        subject = (subject + ' · 别名：' if subject else '别名：') + '、'.join(str(x) for x in aliases)
    possible = row.get('possible_profiles')
    if not possible and isinstance(row.get('candidates'), list):
        possible = [x.get('name') for x in row['candidates'] if isinstance(x, dict) and x.get('name')]
    if possible:
        subject += (' · 候选：' if subject else '候选：') + '、'.join(str(x) for x in possible)
    if kind in ('unproven_profile_alias', 'ambiguous_profile_alias', 'unproven_existing_alias', 'profile_alias_conflict'):
        kind = '字面身份证据不足，保持未知/未合并（' + str(kind) + '）'
    return f'{category}：{subject + " · " if subject else ""}{kind}'


class ProfileView(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.document = None
        self.plans = None
        self.profiles = []
        list_frame = ttk.Frame(self)
        list_frame.pack(side='left', fill='y', padx=(0, 10))
        self.listbox = tk.Listbox(list_frame, width=22, exportselection=False, activestyle='underline')
        list_scroll = ttk.Scrollbar(list_frame, orient='vertical', command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=list_scroll.set)
        self.listbox.pack(side='left', fill='y')
        list_scroll.pack(side='right', fill='y')
        detail = ttk.Frame(self)
        detail.pack(side='left', fill='both', expand=True)
        self.heading = tk.StringVar(value='选择人物查看画像')
        ttk.Label(detail, textvariable=self.heading, font=('Microsoft YaHei UI', 13, 'bold')).pack(anchor='w')
        self.correction_callback = None
        self.correction_button = ttk.Button(detail, text='为所选人物添加纠错说明', command=self._open_correction)
        self.correction_button.pack(anchor='e', pady=(4, 0))
        body_frame = ttk.Frame(detail)
        self.body_frame = body_frame
        body_frame.pack(fill='both', expand=True, pady=(8, 0))
        self.body = tk.Text(body_frame, wrap='word', height=20, font=('Microsoft YaHei UI', 10),
                            padx=10, pady=10, state='disabled')
        body_scroll = ttk.Scrollbar(body_frame, orient='vertical', command=self.body.yview)
        self.body.configure(yscrollcommand=body_scroll.set)
        self.body.pack(side='left', fill='both', expand=True)
        body_scroll.pack(side='right', fill='y')
        self.listbox.bind('<<ListboxSelect>>', self._select)

    def _open_correction(self):
        selected = self.listbox.curselection()
        if callable(self.correction_callback):
            character = self.profiles[selected[0]].get('name') if selected and selected[0] < len(self.profiles) else None
            self.correction_callback(character_name=character)

    def show_empty(self, text):
        self.document, self.plans, self.profiles = None, None, []
        self.listbox.delete(0, 'end')
        self.heading.set('人物画像')
        self._write(text)

    def load(self, root, book_id):
        document, plans = read_profile_document(root, book_id)
        self.document, self.plans = document, plans
        self.profiles = list(document.get('characters', []))
        self.listbox.delete(0, 'end')
        for profile in self.profiles:
            self.listbox.insert('end', profile['name'])
        if self.profiles:
            self.listbox.selection_set(0)
            self._show_profile(0)
        else:
            self.heading.set('尚无人物档案')
            self._write('当前书籍没有可显示的人物记录。')

    def _write(self, text):
        self.body.configure(state='normal')
        self.body.delete('1.0', 'end')
        self.body.insert('1.0', text)
        self.body.configure(state='disabled')

    def _select(self, _event=None):
        selected = self.listbox.curselection()
        if selected:
            self._show_profile(selected[0])

    def _show_profile(self, index):
        p = self.profiles[index]
        facts = [f"人物：{p.get('name', '')}",
                 _claim_line('身份', p.get('identity')),
                 _claim_line('性别', p.get('gender')),
                 _claim_line('年龄', p.get('age'))]
        for key, label in (('personality', '性格'), ('behavior', '行为'), ('speech_style', '说话方式')):
            items = p.get(key, [])
            values = [f"{x.get('value', '未知')}（{'原文明确' if x.get('status') == 'explicit' else '推断'}）"
                      for x in items if isinstance(x, dict)]
            facts.append(label + '：' + ('；'.join(values) if values else '未知'))
        aliases = p.get('aliases', [])
        facts.append('别名：' + ('、'.join(x.get('name', '') for x in aliases if isinstance(x, dict)) or '无已核实别名'))
        facts.append('声音设计（创作方案，不代表人物事实）：')
        design = p.get('voice_design') if isinstance(p.get('voice_design'), dict) else {}
        facts.extend([design.get('description') or '未提供声音描述',
                      '设计依据：' + (design.get('rationale') or '未提供')])
        facts.append('证据引文：')
        for key in ('identity', 'gender', 'age', 'personality', 'behavior', 'speech_style'):
            values = p.get(key, []) if isinstance(p.get(key), list) else [p.get(key)]
            for claim in values:
                if not isinstance(claim, dict):
                    continue
                for item in claim.get('evidence', []) or []:
                    if isinstance(item, dict):
                        facts.append(f"[{item.get('chapter_id', '?')}] {item.get('quote', '')} 位置 {item.get('source_range', '')}")
        for alias in aliases:
            if isinstance(alias, dict):
                for item in alias.get('evidence', []) or []:
                    if isinstance(item, dict):
                        facts.append(f"[别名 {alias.get('name', '')} · {item.get('chapter_id', '?')}] {item.get('quote', '')}")
        if p.get('uncertainties'):
            facts.append('待核对：' + '；'.join(p['uncertainties']))
        names = {p.get('name')} if isinstance(p.get('name'), str) else set()
        names.update(x.get('name') for x in aliases if isinstance(x, dict) and isinstance(x.get('name'), str))
        for field, label in (('profile_conflicts', '全局画像冲突'),
                             ('alias_conflicts', '全局别名冲突'),
                             ('unresolved_profiles', '全局未解决人物')):
            rows = self.document.get(field, []) if isinstance(self.document, dict) else []
            relevant = [row for row in rows if _issue_mentions_profile(row, names)] if isinstance(rows, list) else []
            if relevant:
                facts.append(label + '：')
                facts.extend(_global_issue_text(row, label) for row in relevant)
        self.heading.set(p.get('name', '人物'))
        self._write('\n'.join(facts))


class VoiceView(ttk.Frame):
    def __init__(self, master, root_provider, *, on_book_change=None, on_busy=None,
                 generator=None, audio_player=None, on_playback_state=None, on_stop=None,
                 speed_var=None):
        super().__init__(master)
        self.root_provider = root_provider
        self.on_book_change = on_book_change or (lambda: None)
        self.on_busy = on_busy or (lambda busy: None)
        self.generator = generator or auto.generate_local_fictional
        self.audio_player = audio_player if audio_player is not None else AudioPlayback()
        self.on_playback_state = on_playback_state or (lambda playing: None)
        self.on_stop = on_stop
        self.speed_var = speed_var if speed_var is not None else tk.StringVar(value='1.0')
        self.book_id = None
        self.document = None
        self.plans = None
        self.targets = {}
        self.selected_binding = None
        self.candidate = None
        self.candidate_played = False
        self.operation = None
        self.play_generation = 0
        self.play_pending = False
        self._playback_identity = None
        self.owns_busy = False
        self.external_busy = False
        self.playing = False
        self.errors = []
        self._selected_cache = {}
        self._selection_errors = {}
        self._target_states = {}
        self._active_target_key = None

        top = ttk.Frame(self)
        top.pack(fill='x')
        ttk.Label(top, text='人物或旁白').pack(side='left')
        self.target = tk.StringVar()
        self.target_box = ttk.Combobox(top, textvariable=self.target, state='readonly', width=34)
        self.target_box.pack(side='left', padx=8)
        self.target_box.bind('<<ComboboxSelected>>', self._target_changed)
        status_frame = ttk.Frame(self)
        status_frame.pack(fill='x', pady=(8, 0))
        self.status_tree = ttk.Treeview(status_frame, columns=('role', 'voice_state'), show='headings',
                                        height=5, selectmode='browse')
        self.status_tree.heading('role', text='角色')
        self.status_tree.heading('voice_state', text='音色状态')
        self.status_tree.column('role', width=220, anchor='w')
        self.status_tree.column('voice_state', width=500, anchor='w')
        scroll = ttk.Scrollbar(status_frame, orient='vertical', command=self.status_tree.yview)
        self.status_tree.configure(yscrollcommand=scroll.set)
        self.status_tree.pack(side='left', fill='x', expand=True)
        scroll.pack(side='right', fill='y')
        self.status_tree.bind('<<TreeviewSelect>>', self._status_selected)
        self.state = tk.StringVar(value='先选择人物；读取不会生成声音。')
        ttk.Label(self, textvariable=self.state, wraplength=840).pack(anchor='w', pady=(10, 4))
        self.voice_detail = tk.StringVar(value='')
        ttk.Label(self, textvariable=self.voice_detail, wraplength=840).pack(anchor='w', pady=3)
        ttk.Label(self, text='声音描述（只影响创作性配音方案，不会改人物事实）').pack(anchor='w', pady=(12, 3))
        self.description = tk.Text(self, height=7, wrap='word', font=('Microsoft YaHei UI', 10), padx=8, pady=8)
        self.description.pack(fill='x')
        actions = ttk.Frame(self)
        actions.pack(fill='x', pady=8)
        ttk.Label(actions, text='倍速（保持音高）').pack(side='left')
        self.speed_box = ttk.Spinbox(actions, from_=0.8, to=1.5, increment=0.01,
                                     textvariable=self.speed_var, width=6)
        self.speed_box.pack(side='left', padx=(4, 12))
        self.generate_button = ttk.Button(actions, text='生成候选', command=self.generate_candidate)
        self.generate_button.pack(side='left')
        self.play_button = ttk.Button(actions, text='试听候选', command=self.play_candidate, state='disabled')
        self.play_button.pack(side='left', padx=6)
        self.keep_button = ttk.Button(actions, text='采用候选', command=self.adopt_candidate, state='disabled')
        self.keep_button.pack(side='left')
        self.play_selected_button = ttk.Button(actions, text='播放当前选定音色', command=self.play_selected, state='disabled')
        self.play_selected_button.pack(side='left', padx=(16, 0))
        self.stop_button = ttk.Button(actions, text='停止播放', command=self.stop_playback, state='disabled')
        self.stop_button.pack(side='left', padx=6)
        ttk.Label(self, text='候选生成失败时不会自动重试。先查看下方日志并核对本机回执，再决定是否再次点击生成。',
                  wraplength=840).pack(anchor='w', pady=(0, 5))
        self.log = tk.Text(self, height=7, wrap='word', state='disabled', font=('Consolas', 9))
        self.log.pack(fill='both', expand=True)

    def show_empty(self, text):
        self._save_active_target_state()
        self.book_id = None
        self.document = self.plans = None
        self.targets = {}
        self._selected_cache = {}
        self._selection_errors = {}
        self.target_box['values'] = ()
        self.target.set('')
        self.status_tree.delete(*self.status_tree.get_children())
        self.selected_binding = self.candidate = None
        self.candidate_played = False
        self._active_target_key = None
        self._update_buttons()
        self.state.set(text)
        self.voice_detail.set('')

    def load_book(self, book_id, document, plans):
        self._save_active_target_state()
        self.book_id, self.document, self.plans = book_id, document, plans
        self.targets = {'旁白': ('narrator', None, None)}
        plan_map = {item['character']: item for item in (plans or {}).get('plans', [])}
        for profile in document.get('characters', []) if isinstance(document, dict) else []:
            name = profile.get('name')
            if isinstance(name, str) and name:
                self.targets[name] = (entity_id(book_id, name), profile, plan_map.get(name))
        values = tuple(self.targets)
        self.target_box['values'] = values
        self._refresh_voice_statuses()
        if values:
            if self.target.get() not in self.targets:
                self.target.set(values[0])
            self._show_target()
        else:
            self.show_empty('当前书籍没有可选人物档案。')

    def _target_changed(self, _event=None):
        label = self.target.get()
        if label and self.status_tree.exists(label):
            self.status_tree.selection_set(label)
        self._show_target()

    def _status_selected(self, _event=None):
        selected = self.status_tree.selection()
        if selected and selected[0] in self.targets:
            self.target.set(selected[0])
            self._show_target()

    def _refresh_voice_statuses(self):
        self.status_tree.delete(*self.status_tree.get_children())
        self._selected_cache = {}
        self._selection_errors = {}
        for label, (entity, _profile, _plan) in self.targets.items():
            selection_root = Path(self.root_provider()) / ('narrators' if entity == 'narrator' else 'auto-voices')
            try:
                binding = auto.get_selected_voice(selection_root, self.book_id, entity)
                self._selected_cache[entity] = binding
                state = '未选定'
                if binding:
                    vector = binding['record'].get('request', {}).get('vector_sha256', '未知')
                    state = '已选定 · 向量 ' + (vector[:12] + '…' if isinstance(vector, str) else '未知')
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._selection_errors[entity] = str(exc)
                state = '登记损坏或无法读取'
            self.status_tree.insert('', 'end', iid=label, values=(label, state))

    def _show_target(self):
        target = self.target.get()
        if target not in self.targets:
            return
        entity, profile, plan = self.targets[target]
        self._save_active_target_state()
        key = (self.book_id, entity)
        state = dict(self._target_states.get(key, {}))
        signature = self._target_signature(self.book_id, entity)
        previous_signature = state.get('signature')
        profile_changed = bool(previous_signature and
                               previous_signature.get('profile') != signature.get('profile'))
        binding_changed = bool(previous_signature and
                               previous_signature.get('binding') != signature.get('binding'))
        if profile_changed or binding_changed:
            state['candidate'] = None
            state['candidate_played'] = False
        if profile_changed:
            state.pop('description', None)
        state['signature'] = signature
        self._target_states[key] = state
        self._active_target_key = key
        self.candidate = state.get('candidate')
        self.candidate_played = bool(state.get('candidate_played', False))
        self.selected_binding = None
        self.description.delete('1.0', 'end')
        if entity == 'narrator':
            design = self.document.get('narration_design') if isinstance(self.document, dict) else None
            if isinstance(design, dict):
                text = design.get('description') or ''
            else:
                text = ''
            status = '旁白声音方案'
            selection_root = Path(self.root_provider()) / 'narrators'
        else:
            design = profile.get('voice_design') if isinstance(profile.get('voice_design'), dict) else {}
            rationale = design.get('rationale') if isinstance(design.get('rationale'), str) else ''
            choices = design.get('creative_choices') if isinstance(design.get('creative_choices'), list) else []
            text = (design.get('description') or '').strip()
            if rationale:
                text += '\n创作依据：' + rationale[:240]
            if choices:
                text += '\n创作选择：' + '；'.join(str(item) for item in choices[:3])[:240]
            status = '人物声线候选仅是创作选择；身份与证据保持原样。'
            selection_root = Path(self.root_provider()) / 'auto-voices'
        description = state.get('description', text)
        if description:
            self.description.insert('1.0', description)
        try:
            if entity in self._selection_errors:
                raise ValueError(self._selection_errors[entity])
            if entity not in self._selected_cache:
                self._selected_cache[entity] = auto.get_selected_voice(selection_root, self.book_id, entity)
            self.selected_binding = self._selected_cache[entity]
            if self.selected_binding:
                self.state.set('已读取当前选定音色。' if entity != 'narrator' else '已读取当前选定旁白音色。')
                record = self.selected_binding['record']
                request = record.get('request', {})
                self.voice_detail.set(f"状态：显式选定 · 向量 SHA256 {request.get('vector_sha256', '未知')}")
            else:
                self.state.set(status + '；尚无选定声音。')
                self.voice_detail.set('当前选定：无。可生成试听候选，采用前会保留现有绑定。')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.state.set('选定音色记录不可读：' + str(exc))
            self.voice_detail.set('损坏记录不会被静默覆盖；如需修复，请先核对日志与向量文件。')
        self._update_buttons()

    def _save_active_target_state(self):
        key = getattr(self, '_active_target_key', None)
        if key is None:
            return
        states = getattr(self, '_target_states', None)
        if states is None:
            self._target_states = states = {}
        description = ''
        try:
            description = self.description.get('1.0', 'end-1c')
        except (AttributeError, tk.TclError):
            pass
        previous = states.get(key, {})
        states[key] = {
            'description': description,
            'candidate': getattr(self, 'candidate', None),
            'candidate_played': bool(getattr(self, 'candidate_played', False)),
            'playing': bool(getattr(self, 'playing', False) and getattr(self, '_playback_identity', None) == key),
            'signature': previous.get('signature') or self._target_signature(*key),
        }

    def _target_signature(self, book_id, entity):
        target = next(((profile, plan) for candidate_entity, profile, plan in self.targets.values()
                       if candidate_entity == entity), (None, None))
        profile, plan = target
        narration_design = (self.document.get('narration_design')
                            if book_id == self.book_id and entity == 'narrator'
                            and isinstance(self.document, dict) else None)
        profile_digest = hashlib.sha256(json.dumps(
            {'profile': profile, 'plan': plan, 'narration_design': narration_design},
            ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), default=str).encode('utf-8')).hexdigest()
        binding = self._selected_cache.get(entity)
        binding_record = binding.get('record') if isinstance(binding, dict) else None
        binding_digest = hashlib.sha256(json.dumps(
            {'binding': binding_record, 'error': self._selection_errors.get(entity)},
            ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str
        ).encode('utf-8')).hexdigest()
        return {'profile': profile_digest, 'binding': binding_digest}

    def _store_target_state(self, book_id, entity, *, candidate=_UNSET, candidate_played=None,
                            playing=None, description=None, signature=None):
        states = getattr(self, '_target_states', None)
        if states is None:
            self._target_states = states = {}
        key = (book_id, entity)
        state = dict(states.get(key, {}))
        if candidate is not _UNSET:
            state['candidate'] = candidate
        if candidate_played is not None:
            state['candidate_played'] = bool(candidate_played)
        if playing is not None:
            state['playing'] = bool(playing)
        if description is not None:
            state['description'] = description
        if signature is not None:
            state['signature'] = signature
        states[key] = state

    def _current_target(self):
        if self.target.get() not in self.targets or not self.book_id:
            raise ValueError('请先载入人物档案并选择一个人物。')
        return self.targets[self.target.get()]

    def _log(self, text):
        self.errors.append(text)
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _update_buttons(self):
        busy = self.operation is not None or self.play_pending or self.external_busy
        self.generate_button.configure(state='disabled' if busy or not self.book_id else 'normal')
        self.play_button.configure(state='normal' if self.candidate is not None and not busy else 'disabled')
        self.keep_button.configure(state='normal' if self.candidate is not None and self.candidate_played and not busy else 'disabled')
        self.play_selected_button.configure(state='normal' if self.selected_binding and not busy else 'disabled')
        self.target_box.configure(state='disabled' if busy else ('readonly' if self.book_id else 'disabled'))
        self.speed_box.configure(state='disabled' if busy else 'normal')
        self.stop_button.configure(state='normal' if self.playing or self.play_pending else 'disabled')

    def set_external_busy(self, busy):
        self.external_busy = bool(busy)
        self._update_buttons()

    def set_playback_state(self, playing):
        self.playing = bool(playing)
        identity = getattr(self, '_playback_identity', None)
        if identity is not None:
            self._store_target_state(identity[0], identity[1], playing=self.playing)
            if not self.playing:
                self._playback_identity = None
        self._update_buttons()

    def invalidate_pending_playback(self):
        self.play_generation += 1
        if self.play_pending:
            self.play_pending = False
            self._release_app_busy()
        self.set_playback_state(False)

    def _release_app_busy(self):
        if self.owns_busy:
            self.owns_busy = False
            self.on_busy(False)

    def generate_candidate(self):
        try:
            entity, profile, plan = self._current_target()
            description = compose_description(entity, self.description.get('1.0', 'end'))
        except (ValueError, KeyError, TypeError) as exc:
            self.state.set(str(exc))
            return
        if entity != 'narrator' and (not plan or not plan.get('spec_id')):
            self.state.set('当前人物缺少经校验的声音方案；请先补齐画像与创作依据。')
            return
        root = Path(self.root_provider())
        voice_root = root / ('narrators' if entity == 'narrator' else 'auto-voices')
        output = voice_root / self.book_id / entity / 'candidates' / (str(uuid.uuid4()))
        self._save_active_target_state()
        generation_signature = self._target_signature(self.book_id, entity)
        self._store_target_state(self.book_id, entity, candidate=None, candidate_played=False,
                                 description=self.description.get('1.0', 'end-1c'),
                                 signature=generation_signature)
        self.operation = ('generate', entity, profile, plan, description, output)
        self.candidate = None
        self.candidate_played = False
        self.state.set('正在生成一个本机虚构声音候选；没有自动重试…')
        self._update_buttons()
        self.on_busy(True)
        target_book = self.book_id

        def worker():
            try:
                record = self.generator(description, output)
                binding = candidate_binding(target_book, entity, record)
                result = (target_book, entity, profile, plan, description, binding, generation_signature)
                self.after(0, lambda: self._candidate_ready(result))
            except Exception as exc:
                error = str(exc)
                self.after(0, lambda: self._candidate_failed(error))

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            self._candidate_failed('后台生成线程启动失败：' + str(exc))

    def _candidate_ready(self, result):
        self.operation = None
        book_id, entity, profile, plan, description, binding = result[:6]
        signature = result[6] if len(result) > 6 else self._target_signature(book_id, entity)
        candidate = {'entity': entity, 'binding': binding, 'description': description,
                     'spec_id': hashlib.sha256(description.encode('utf-8')).hexdigest()[:16]}
        saved = self._target_states.get((book_id, entity), {})
        signature_valid = saved.get('signature') == signature
        active = (book_id == self.book_id and self._active_target_key == (book_id, entity)
                  and self._target_signature(book_id, entity) == signature)
        if signature_valid or active:
            self._store_target_state(book_id, entity, candidate=candidate, candidate_played=False,
                                     signature=signature)
        if not active:
            message = ('候选已生成；原书籍/人物身份已保留，未自动采用。切回该目标后可继续试听。'
                       if signature_valid else '候选完成时人物档案或已选声线已变化，已忽略过期候选。')
            self._log(message)
        else:
            self.candidate = candidate
            self.candidate_played = False
            self.state.set('候选已生成。先试听，满意后再点“采用候选”；旧声音仍保持选定。')
            self.voice_detail.set('候选向量 SHA256 ' + binding['record']['request']['vector_sha256'])
        self.on_busy(False)
        self._update_buttons()

    def _candidate_failed(self, error):
        self.operation = None
        self._log('候选生成失败（未自动重试）：' + error)
        self.state.set('候选生成失败；日志已保留，请先核对远端/本机生成回执。')
        self.on_busy(False)
        self._update_buttons()

    def _prepare_binding(self, binding, speed):
        path = Path(binding['record']['directory']) / 'generated.wav'
        if not path.is_file():
            raise FileNotFoundError('试听 WAV 不存在。')
        expected = binding['record'].get('request', {}).get('wav_sha256')
        if not isinstance(expected, str) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('试听 WAV 哈希与生成回执不匹配。')
        if callable(getattr(self.audio_player, 'prepare', None)):
            return self.audio_player.prepare(path, speed)
        if speed == 1.0 and callable(self.audio_player):
            return path
        raise RuntimeError('当前播放器不支持倍速处理。')

    def _start_binding_playback(self, binding, *, candidate=False):
        if self.operation is not None or self.play_pending or self.external_busy:
            return
        try:
            speed = parse_speed(self.speed_var.get())
        except ValueError as exc:
            self.state.set(str(exc))
            return
        self.play_generation += 1
        generation = self.play_generation
        identity = self._active_target_key
        self._playback_identity = identity
        self.play_pending = True
        self.owns_busy = True
        self.state.set('正在校验音频并准备播放…')
        self.on_busy(True)
        self._update_buttons()

        def worker():
            try:
                prepared = self._prepare_binding(binding, speed)
                self.after(0, lambda: self._playback_ready(generation, prepared, candidate, identity))
            except Exception as exc:
                error = str(exc)
                self.after(0, lambda: self._playback_failed(generation, error, identity))

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            self._playback_failed(generation, '播放准备线程启动失败：' + str(exc))

    def _playback_ready(self, generation, prepared, candidate, identity=None):
        if generation != self.play_generation:
            discard = getattr(self.audio_player, 'discard', None)
            if callable(discard): discard(prepared)
            return
        if identity is not None and identity != self._active_target_key:
            discard = getattr(self.audio_player, 'discard', None)
            if callable(discard): discard(prepared)
            self.play_pending = False
            self._release_app_busy()
            self._update_buttons()
            return

        def start_worker():
            try:
                if callable(getattr(self.audio_player, 'start', None)):
                    self.audio_player.start(prepared)
                else:
                    self.audio_player(prepared)
                self.after(0, lambda: self._playback_started(generation, prepared, candidate, None, identity))
            except Exception as exc:
                self.after(0, lambda error=exc: self._playback_started(generation, prepared, candidate, error, identity))

        try:
            threading.Thread(target=start_worker, daemon=True).start()
        except Exception as exc:
            self._playback_failed(generation, '播放线程启动失败：' + str(exc))

    def _playback_started(self, generation, prepared, candidate, error, identity=None):
        if generation != self.play_generation:
            discard = getattr(self.audio_player, 'discard', None)
            if callable(discard): discard(prepared)
            return
        try:
            if error:
                raise error
            self.playing = True
            if identity is not None:
                self._store_target_state(identity[0], identity[1], playing=True)
            if candidate:
                if identity is None or identity == self._active_target_key:
                    self.candidate_played = True
                if identity is not None:
                    self._store_target_state(identity[0], identity[1], candidate_played=True)
                if identity is None or identity == self._active_target_key:
                    self.state.set('正在试听候选；满意后可采用。')
            else:
                if identity is None or identity == self._active_target_key:
                    self.state.set('正在播放当前选定音色。')
            self.on_playback_state(True)
        except PlaybackCancelled:
            self.state.set('播放准备已取消。')
        except Exception as exc:
            self._log('播放失败：' + str(exc))
            self.state.set('播放失败：' + str(exc))
        finally:
            self.play_pending = False
            self._release_app_busy()
            self._update_buttons()

    def _playback_failed(self, generation, error, identity=None):
        if generation != self.play_generation:
            return
        self.play_pending = False
        self._log('播放失败：' + error)
        self.state.set('播放失败：' + error)
        self._release_app_busy()
        self._update_buttons()

    def play_candidate(self):
        if not self.candidate:
            return
        self._start_binding_playback(self.candidate['binding'], candidate=True)

    def play_selected(self):
        if not self.selected_binding:
            return
        self._start_binding_playback(self.selected_binding)

    def stop_playback(self):
        try:
            if callable(self.on_stop):
                self.on_stop()
            elif callable(getattr(self.audio_player, 'stop', None)):
                self.audio_player.stop()
            else:
                import winsound
                winsound.PlaySound(None, 0)
            self.play_generation += 1
            self.play_pending = False
            self.playing = False
            if self._playback_identity is not None:
                self._store_target_state(*self._playback_identity, playing=False)
            self._playback_identity = None
            self._release_app_busy()
            self.on_playback_state(False)
            self.state.set('已停止播放。')
            self._update_buttons()
        except (OSError, RuntimeError) as exc:
            self._log('停止播放失败：' + str(exc))
            self.state.set('停止播放失败：' + str(exc))

    def adopt_candidate(self):
        if not self.candidate or not self.candidate_played:
            self.state.set('请先生成并试听候选，再采用。')
            return
        entity = self.candidate['entity']
        root = Path(self.root_provider()) / ('narrators' if entity == 'narrator' else 'auto-voices')
        try:
            select_candidate(root, self.book_id, entity, self.candidate['binding'],
                             auditioned=self.candidate_played,
                             design=self.candidate['description'], spec_id=self.candidate.get('spec_id'))
            self._refresh_voice_statuses()
            self.state.set('候选已显式采用；仅替换该人物（或旁白）的选定音色。')
            self._log('已采用 ' + self.target.get() + ' 的候选；画像事实与原文引文未修改。')
            self.candidate = None
            self.candidate_played = False
            self._show_target()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._log('采用候选失败：' + str(exc))
            self.state.set('未更改现有选定音色：' + str(exc))
        self._update_buttons()
