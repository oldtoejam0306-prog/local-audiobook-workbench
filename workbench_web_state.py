"""Local browser workbench using the verified production adapters.

Snapshots contain display data. File capabilities and frozen model inputs stay
inside this service. Only explicit actions start analysis or synthesis.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import secrets
import threading

import chapter_revision_runtime as revisions
import console_voice_billing as billing
import correction_memory
import real_book_auto_voice as auto
import workbench_audio_revision as audio_revision
import workbench_real_v2_ui as core
import workbench_voice_panel as voices
from audio_playback import AudioPlayback, parse_speed


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _claim(value):
    return value.get('value', '未知') if isinstance(value, dict) else '未知'


class WorkbenchService:
    def __init__(self, *, runtime=None, books=None, selection=None, player=None, importer=None):
        if runtime is None:
            import workbench_real_v2_runtime as runtime
        self.runtime = runtime
        self.root = Path(runtime.ROOT)
        if importer is None:
            from workbench_book_import import BookImporter
            importer = BookImporter(self.root)
        self.importer = importer
        self.books = dict(core.existing.BOOKS if books is None else books)
        from workbench_book_import import merge_book_names
        imported_books = self.importer.list_books()
        self._import_book_ids = {entry['book_id'] for entry in imported_books}
        self._chapter_titles = {
            (entry['book_id'], chapter['chapter_id']): chapter['title']
            for entry in imported_books for chapter in entry.get('chapters', [])
        }
        self.books = merge_book_names(self.books, imported_books)
        core.existing.BOOKS.update(self.books)
        self.selection = selection or core.Selection()
        self.player = player or AudioPlayback(play_fn=lambda _path: None, stop_fn=lambda: None)
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='workbench-operation')
        self._generation = 0
        self._closed = False
        self._upload_reserved = False
        self._roles = {}
        self._role_states = {}
        self._chapter_states = {}
        self._revision_candidates = {}
        self._executor = None
        self._media = None
        self._play_generation = 0
        self._pause_requested = threading.Event()
        self._billing_rows = []
        self._billing_pending = False
        self.on_shutdown = lambda: None
        self._state = {
            'books': [{'id': identity, 'name': name} for name, identity in self.books.items()],
            'book_id': None, 'book_name': '', 'chapters': [],
            'range': {'start': '', 'end': ''}, 'profiles': [], 'selected_role': 'narrator',
            'import_preview': None,
            'busy': None, 'status': '选择已登记书籍，读取章节索引。', 'error': '',
            'editor': None, 'playback': None, 'diagnostics': None,
            'billing': {'loading': False, 'rows': [], 'summary': {},
                        'prices': {'input': '2', 'output': '8'}, 'error': ''},
        }

    def snapshot(self):
        with self._lock:
            return deepcopy(self._state)

    def close(self):
        with self._lock:
            self._closed = True
            self._generation += 1
            if self._executor is not None:
                self._executor.request_stop()
            self._stop()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _book(self):
        book = self._state['book_id']
        if not book:
            raise ValueError('请先选择已登记书籍。')
        return book

    def _chapter(self, value):
        if value not in self.selection.chapters or self.selection.book != self._book():
            raise ValueError('请先读取当前书籍索引并选择有效章节。')
        return value

    def _span(self):
        start = self._chapter(self._state['range']['start'])
        end = self._chapter(self._state['range']['end'])
        first, last = self.selection.chapters.index(start), self.selection.chapters.index(end)
        if first > last:
            raise ValueError('结束章节必须不早于起始章节。')
        return list(self.selection.chapters[first:last + 1])

    def _submit(self, kind, label, work, apply=None, discard=None):
        if self._state['busy'] or self._upload_reserved:
            raise RuntimeError('当前操作正在进行，请等待完成。')
        generation = self._generation
        self._state.update(busy={'kind': kind, 'label': label}, status=label, error='')

        def run():
            try:
                result = work()
                with self._lock:
                    if generation != self._generation or self._closed:
                        if discard is not None:
                            discard(result)
                        return
                    if apply is not None:
                        apply(result)
                    self._state['status'] = '操作完成。'
            except Exception as exc:
                with self._lock:
                    if generation == self._generation and not self._closed:
                        self._state.update(status='操作失败，可核对提示后重试。', error=str(exc))
            finally:
                with self._lock:
                    if generation == self._generation and not self._closed:
                        self._state['busy'] = None
        try:
            self._pool.submit(run)
        except Exception:
            self._state['busy'] = None
            raise

    def prepare_import(self, path, original_name):
        """Queue local parsing; caller owns the staged file until background completion."""
        with self._lock:
            if self._closed:
                raise RuntimeError('工作台已关闭。')
            if self._upload_reserved:
                raise RuntimeError('书籍文件正在上传，请稍候。')
            if self._state['busy']:
                raise RuntimeError('当前操作正在进行，请等待完成。')
            def work():
                try:
                    return self.importer.preview(path, original_name)
                finally:
                    Path(path).unlink(missing_ok=True)
            def apply(summary):
                summary = dict(summary)
                book_id = summary.get('book_id')
                existing_name = next((name for name, identity in self.books.items()
                                      if identity == book_id), None)
                if existing_name is not None and book_id not in self._import_book_ids:
                    summary['existing_book'] = True
                    summary['existing_name'] = existing_name
                else:
                    summary['existing_book'] = False
                self._state['import_preview'] = summary
            self._submit('import_preview', '正在本地解析书籍…', work, apply)
            return self.snapshot()

    def _import_heading(self, payload):
        preview_id, heading = payload.get('preview_id'), payload.get('heading')
        preview = self._state.get('import_preview')
        if (not isinstance(preview_id, str) or not isinstance(heading, str)
                or not preview or preview.get('preview_id') != preview_id):
            raise ValueError('导入预览或章节标题无效。')
        def apply(matches):
            current = self._state.get('import_preview')
            if current and current.get('preview_id') == preview_id:
                current['heading_matches'] = matches[:100]
        self._submit('import_heading', '正在匹配章节标题…',
                     lambda: self.importer.find_heading(preview_id, heading), apply)

    def reserve_import_upload(self):
        with self._lock:
            if self._closed or self._state['busy'] or self._upload_reserved:
                return False
            self._upload_reserved = True
            return True

    def cancel_import_upload(self):
        with self._lock:
            self._upload_reserved = False

    def accept_import_upload(self, path, original_name):
        with self._lock:
            if not self._upload_reserved or self._closed or self._state['busy']:
                raise RuntimeError('当前操作状态已变化，请重试。')
            self._upload_reserved = False
            try:
                return self.prepare_import(path, original_name)
            except Exception:
                Path(path).unlink(missing_ok=True)
                raise

    def action(self, name, payload):
        with self._lock:
            if self._closed:
                raise RuntimeError('工作台已关闭。')
            handlers = {
                'book': self._choose_book, 'index': self._load_index,
                'range': self._set_range, 'progress': self._progress,
                'profiles_analyze': self._analyze_profiles, 'execute': self._execute,
                'pause': self._pause, 'chapter': self._open_chapter,
                'voice_draft': self._voice_draft, 'voice_generate': self._voice_generate,
                'voice_play': self._voice_play, 'voice_adopt': self._voice_adopt,
                'revision_generate': self._revision_generate, 'revision_play': self._revision_play,
                'revision_adopt': self._revision_adopt, 'revision_restore': self._revision_restore,
                'chapter_play': self._chapter_play, 'playback_started': self._playback_started,
                'stop': lambda _p: self._stop(), 'billing_refresh': self._billing_refresh,
                'billing_reprice': self._billing_reprice, 'correction_save': self._save_correction,
                'diagnostics': self._diagnostics,
                'import_commit': self._import_commit, 'import_clear': self._import_clear,
                'import_heading': self._import_heading,
                'shutdown': lambda _p: self.on_shutdown(),
            }
            if name not in handlers:
                raise ValueError('未知工作台操作。')
            scoped = {'index', 'range', 'progress', 'profiles_analyze', 'execute', 'pause',
                      'chapter', 'voice_draft', 'voice_generate', 'voice_play', 'voice_adopt',
                      'revision_generate', 'revision_play', 'revision_adopt', 'revision_restore',
                      'chapter_play', 'correction_save', 'diagnostics'}
            if name in scoped and payload.get('expected_book_id') != self._book():
                raise ValueError('书籍选择已变化，请刷新当前页面。')
            if name.startswith('revision_'):
                if payload.get('expected_chapter_id') != self._editor()['chapter_id']:
                    raise ValueError('校听章节已变化，请重新载入段落。')
            safe_during_work = {'stop', 'playback_started', 'pause',
                                'billing_refresh', 'billing_reprice'}
            if (self._state['busy'] or self._upload_reserved) and name not in safe_during_work:
                raise RuntimeError('当前操作正在进行，请等待完成。')
            handlers[name](payload)
            return self.snapshot()

    def _choose_book(self, payload):
        book = payload.get('book_id')
        name = next((name for name, identity in self.books.items() if identity == book), None)
        if name is None:
            raise ValueError('书籍未登记。')
        if book != self._state['book_id']:
            self._stop()
            self._generation += 1
            self._roles = {}
            self._executor = None
            self.selection.book = None
            self.selection.chapters = ()
            self.selection.index_sha256 = None
            self.selection.frozen = None
            self._state.update(book_id=book, book_name=name, chapters=[], editor=None,
                               profiles=[], range={'start': '', 'end': ''}, diagnostics=None)
        self._submit('book', '正在读取本书人物档案…', self._read_profiles, self._apply_profiles)

    def _import_clear(self, _payload):
        self._state['import_preview'] = None

    def _import_commit(self, payload):
        preview_id = payload.get('preview_id')
        title = payload.get('title')
        chapter_starts = payload.get('chapter_starts')
        if not isinstance(preview_id, str) or not isinstance(title, str):
            raise ValueError('preview_id与title必须是字符串。')
        preview = self._state.get('import_preview')
        if not preview or preview.get('preview_id') != preview_id:
            raise ValueError('导入预览已失效，请重新预览。')
        generation = self._generation
        def work():
            with self._lock:
                if self._closed or generation != self._generation:
                    raise RuntimeError('工作台状态已变化，导入未提交。')
            if preview.get('existing_book'):
                entry = {'book_id': preview['book_id'],
                         'display_name': preview['existing_name'],
                         'chapters': preview.get('chapters', [])}
                index_name = preview['existing_name']
            else:
                entry = self.importer.commit(preview_id, title, chapter_starts)
                from workbench_book_import import merge_book_names
                temporary_books = merge_book_names(self.books, [entry])
                index_name = next(label for label, identity in temporary_books.items()
                                  if identity == entry['book_id'])
                registry_before = dict(core.existing.BOOKS)
                core.existing.BOOKS.update(temporary_books)
            try:
                ids = self.selection.load(index_name)
            except Exception:
                if not preview.get('existing_book'):
                    core.existing.BOOKS.clear()
                    core.existing.BOOKS.update(registry_before)
                raise
            return entry, ids
        def apply(result):
            entry, ids = result
            book_id = entry['book_id']
            if not preview.get('existing_book'):
                from workbench_book_import import merge_book_names
                self.books = merge_book_names(self.books, [entry])
                self._chapter_titles.update({(book_id, chapter['chapter_id']): chapter['title']
                                             for chapter in entry.get('chapters', [])})
            name = next(name for name, identity in self.books.items() if identity == book_id)
            core.existing.BOOKS.update(self.books)
            self._stop()
            self._roles = {}
            self._executor = None
            self._state.update(book_id=book_id, book_name=name,
                               books=[{'id': identity, 'name': label}
                                      for label, identity in self.books.items()],
                               chapters=[{'id': cid, **({'title': self._chapter_titles[(book_id, cid)]}
                                                       if (book_id, cid) in self._chapter_titles else {}),
                                          'status': '待读取进度', 'stages': [], 'audio': False,
                                          'history': False, 'diagnostics': None}
                                         for cid in ids],
                               range={'start': ids[0] if ids else '', 'end': ids[0] if ids else ''},
                               profiles=[], editor=None, import_preview=None, diagnostics=None)
        self._submit('import_commit', '正在登记并读取新书索引…', work, apply)

    def _read_profiles(self):
        book = self._book()
        path = self.root / 'character-profiles' / book / 'current.json'
        document, plans = voices.read_profile_document(self.root, book) if path.exists() else ({}, {})
        plan_map = {row['character']: row for row in plans.get('plans', [])}
        targets = [('narrator', '旁白', None, None)]
        targets.extend((voices.entity_id(book, profile['name']), profile['name'], profile,
                        plan_map.get(profile['name'])) for profile in document.get('characters', []))
        output = []
        for role, name, profile, plan in targets:
            selection_error = ''
            try:
                selected = auto.get_selected_voice(self._voice_root(role), book, role)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                selected = None
                selection_error = str(exc)
            design = document.get('narration_design', {}) if profile is None else profile.get('voice_design', {})
            names = {name}
            names.update(alias.get('name') for alias in (profile or {}).get('aliases', []))
            issues = []
            for field, label in (('profile_conflicts', '画像冲突'), ('alias_conflicts', '别名冲突'),
                                 ('unresolved_profiles', '待核人物')):
                issues.extend(voices._global_issue_text(issue, label)
                              for issue in document.get(field, [])
                              if voices._issue_mentions_profile(issue, names))
            output.append({'id': role, 'name': name, 'profile': profile, 'plan': plan,
                           'selected': selected, 'description': design.get('description', ''),
                           'selection_error': selection_error,
                           'issues': issues,
                           'signature': _digest({'profile': profile, 'plan': plan, 'design': design})})
        return output

    def _voice_root(self, role):
        return self.root / ('narrators' if role == 'narrator' else 'auto-voices')

    def _apply_profiles(self, rows):
        self._roles = {}
        for row in rows:
            key = (self._book(), row['id'])
            previous = self._role_states.get(key)
            binding_signature = _digest(row['selected'])
            if previous is None or previous['signature'] != row['signature']:
                previous = dict(row, candidate=None, binding_signature=binding_signature)
            else:
                if previous['binding_signature'] != binding_signature:
                    previous['candidate'] = None
                previous.update(row, description=previous['description'],
                                binding_signature=binding_signature)
            self._role_states[key] = previous
            self._roles[row['id']] = previous
        self._sync_profiles()

    def _sync_profiles(self):
        display = []
        for row in self._roles.values():
            profile = row['profile'] or {}
            candidate = row['candidate']
            evidence = []
            for field in ('identity', 'gender', 'age', 'personality', 'behavior', 'speech_style'):
                claims = profile.get(field, [])
                claims = claims if isinstance(claims, list) else [claims]
                for claim in claims:
                    if isinstance(claim, dict):
                        evidence.extend(claim.get('evidence', []))
            for alias in profile.get('aliases', []):
                evidence.extend(alias.get('evidence', []))
            display.append({
                'id': row['id'], 'name': row['name'], 'description': row['description'],
                'profile_revision': row['signature'],
                'aliases': [alias.get('name', '') for alias in profile.get('aliases', [])],
                'identity': _claim(profile.get('identity')), 'gender': _claim(profile.get('gender')),
                'age': _claim(profile.get('age')),
                'claims': {key: profile.get(key) for key in ('identity', 'gender', 'age',
                           'personality', 'behavior', 'speech_style')},
                'personality': [_claim(item) for item in profile.get('personality', [])],
                'speech_style': [_claim(item) for item in profile.get('speech_style', [])],
                'evidence': evidence, 'uncertainties': profile.get('uncertainties', []),
                'selection_error': row.get('selection_error', ''),
                'issues': row.get('issues', []),
                'selected': ({'description': '已选定固定声线', 'vector_sha256':
                              row['selected']['record']['request']['vector_sha256']}
                             if row['selected'] else None),
                'candidate': ({key: candidate[key] for key in ('id', 'description', 'auditioned')}
                              if candidate else None),
            })
        self._state['profiles'] = display

    def _load_index(self, _payload):
        name = self._state['book_name']
        self._book()
        def apply(ids):
            self._state['chapters'] = [self._chapter_states.get((self._book(), cid), {
                'id': cid, **({'title': self._chapter_titles[(self._book(), cid)]}
                              if (self._book(), cid) in self._chapter_titles else {}),
                'status': '待读取进度', 'stages': [], 'audio': False,
                'history': False, 'diagnostics': None}) for cid in ids]
            old = self._state['range']
            self._state['range'] = {key: old.get(key) if old.get(key) in ids else ids[0]
                                    for key in ('start', 'end')}
        self._submit('index', '正在读取受信章节索引…', lambda: self.selection.load(name), apply)

    def _set_range(self, payload):
        start, end = self._chapter(payload.get('start')), self._chapter(payload.get('end'))
        if self.selection.chapters.index(start) > self.selection.chapters.index(end):
            raise ValueError('结束章节必须不早于起始章节。')
        self._state['range'] = {'start': start, 'end': end}

    def _apply_progress(self, rows):
        for row in rows:
            status = row.get('error') or row.get('audio_error')
            if not status:
                status = '可试听' if row.get('audio_path') else (' / '.join(row['completed_stages']) or '等待制作')
            self._chapter_states[(self._book(), row['chapter_id'])] = {
                'id': row['chapter_id'], 'status': status, 'stages': row['completed_stages'],
                'audio': bool(row.get('audio_path')), 'history': row.get('audio_version') == 'historical',
                'diagnostics': row.get('diagnostics'),
                **({'title': self._chapter_titles[(self._book(), row['chapter_id'])]}
                   if (self._book(), row['chapter_id']) in self._chapter_titles else {})}
        self._state['chapters'] = [self._chapter_states.get((self._book(), row['id']), row)
                                    for row in self._state['chapters']]

    def _progress(self, _payload):
        book, ids = self._book(), self._span()
        self._submit('progress', '正在核验章节进度…',
                     lambda: core.inspect_chapter_progress(self.root, book, ids), self._apply_progress)

    def _analyze_profiles(self, _payload):
        self._span()
        name, span = self._state['book_name'], dict(self._state['range'])
        def work():
            from character_profile_jobs import analyze_profile_range
            result = analyze_profile_range(name, span['start'], span['end'], root=self.root)
            return result, self._read_profiles()
        def apply(result):
            self._apply_profiles(result[1])
            summary = core.profile_result_summary(result[0])
            if result[0].get('status') in ('error', 'blocked'):
                raise RuntimeError(summary)
            self._state['analysis_summary'] = summary
        self._submit('profiles', '正在分析选定范围的人物档案…', work, apply)

    def _execute(self, _payload):
        book, ids = self._book(), self._span()
        name, span = self._state['book_name'], dict(self._state['range'])
        self._pause_requested.clear()
        def work():
            config, executor = self.runtime.make_executor(name)
            with self._lock:
                self._executor = executor
            self.selection.preview_range(name, span['start'], span['end'], config)
            if self._pause_requested.is_set():
                return ({'status': 'paused'}, core.inspect_chapter_progress(self.root, book, ids),
                        self._read_profiles())
            done = threading.Event()
            def monitor():
                while not done.wait(1.5):
                    if self._pause_requested.is_set():
                        executor.request_stop()
                    try:
                        rows = core.inspect_chapter_progress(self.root, book, ids)
                        with self._lock:
                            if (not self._closed and not done.is_set()
                                    and self._state['book_id'] == book):
                                self._apply_progress(rows)
                    except Exception:
                        pass
            thread = threading.Thread(target=monitor, name='workbench-progress', daemon=True)
            thread.start()
            try:
                result = executor.execute_range(*self.selection.frozen)
            finally:
                done.set()
                thread.join(timeout=5)
            return result, core.inspect_chapter_progress(self.root, book, ids), self._read_profiles()
        def apply(result):
            self._apply_progress(result[1])
            self._apply_profiles(result[2])
            if result[0].get('status') not in ('complete', 'ok', 'paused'):
                raise RuntimeError(result[0].get('message') or str(result[0].get('status')))
        self._submit('execute', '正在分析与配音，按阶段保存回执…', work, apply)

    def _pause(self, _payload):
        if not self._state['busy'] or self._state['busy']['kind'] != 'execute':
            raise ValueError('当前没有可暂停的制作任务。')
        self._pause_requested.set()
        if self._executor is not None:
            self._executor.request_stop()
        self._state['status'] = '已请求在当前阶段完成后暂停。'

    def _editor_data(self, context):
        paragraphs = audio_revision.list_paragraphs(context)
        stage_raw = Path(context['normalized_stage_path']).read_bytes()
        if hashlib.sha256(stage_raw).hexdigest() != context['normalized_stage_sha256']:
            raise ValueError('章节分段来源发生变化，请重新载入。')
        segments = json.loads(stage_raw)['chapters'][0]['segments']
        names = {row['id']: row['name'] for row in self._roles.values()}
        adopted = audio_revision.resolve_adopted(context)
        adopted_id = adopted.get('revision_id') if adopted else None
        adopted_record = (audio_revision.verify_candidate(context, adopted_id)
                          if adopted_id else None)
        for row in paragraphs:
            row['source_speaker_id'] = row['speaker_id']
            row['instruct'] = segments[row['index']].get('instruct', '')
            row['delivery'] = segments[row['index']].get('delivery', 'neutral')
            if adopted_record and adopted_record['paragraph_id'] == row['id']:
                row['speaker_id'] = adopted_record.get('speaker_override_id') or row['speaker_id']
                row['instruct'] = adopted_record.get('instruction') or row['instruct']
            row['speaker_name'] = names.get(row['speaker_id'], row['speaker_id'])
        key = self._book(), context['chapter_id']
        candidate = self._revision_candidates.get(key)
        candidate_error = ''
        if candidate:
            try:
                audio_revision.verify_candidate(context, candidate['record']['revision_id'])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                with self._lock:
                    self._revision_candidates.pop(key, None)
                candidate = None
                candidate_error = '旧候选校验失败，请重新生成。' + str(exc)
        candidate_display = self._candidate_display(candidate)
        if candidate_display:
            candidate_display['adopted'] = candidate_display['id'] == adopted_id
        return {'chapter_id': context['chapter_id'], 'paragraphs': paragraphs,
                'speakers': [{'id': role, 'name': names.get(role, role)} for role in context['voice_bindings']],
                'candidate': candidate_display, 'candidate_error': candidate_error,
                'adopted': bool(adopted), 'adopted_revision_id': adopted_id,
                'revision_basis': 'base'}

    @staticmethod
    def _candidate_display(candidate):
        if not candidate:
            return None
        record = candidate['record']
        return {'id': record['revision_id'], 'paragraph_id': record['paragraph_id'],
                'instruction': record['instruction'],
                'speaker_id': record.get('speaker_override_id') or record.get('speaker_id'),
                'auditioned': candidate['auditioned'],
                'adopted': False,
                'statistics': record['receipt'].get('cache_statistics', {})}

    def _open_chapter(self, payload):
        book, chapter = self._book(), self._chapter(payload.get('chapter_id'))
        self._submit('chapter', '正在核验章节与可修订段落…',
                     lambda: self._editor_data(revisions.get_context(self.root, book, chapter)),
                     lambda result: self._state.update(editor=result))

    def _role(self, payload):
        role = self._roles.get(payload.get('role_id'))
        if role is None:
            raise ValueError('请先选择本书人物或旁白。')
        return role

    def _voice_draft(self, payload):
        row = self._role(payload)
        text = payload.get('description')
        if not isinstance(text, str) or len(text) > 1500:
            raise ValueError('声音描述应在1500字以内。')
        row['description'] = text
        self._state['selected_role'] = row['id']
        self._sync_profiles()

    def _voice_generate(self, payload):
        self._voice_draft(payload)
        row, book = self._role(payload), self._book()
        if row['id'] != 'narrator' and not row['plan']:
            raise ValueError('人物声音方案缺失，请先补齐人物档案。')
        description = voices.compose_description(row['id'], row['description'])
        candidate_id = secrets.token_hex(16)
        output = self._voice_root(row['id']) / book / row['id'] / 'candidates' / candidate_id
        signature = row['signature'], row['binding_signature']
        def work():
            binding = voices.candidate_binding(book, row['id'], auto.generate_local_fictional(description, output))
            return {'id': candidate_id, 'binding': binding, 'description': description,
                    'auditioned': False, 'signature': signature}
        def apply(candidate):
            if signature != (row['signature'], row['binding_signature']):
                raise ValueError('人物或已选声线已变化，候选保存在原目录。')
            row['candidate'] = candidate
            self._sync_profiles()
        self._submit('voice', '正在本机生成虚构声音候选…', work, apply)

    def _verified_voice(self, binding):
        auto._validate_record(binding['record'])
        path = Path(binding['record']['directory']) / 'generated.wav'
        if hashlib.sha256(path.read_bytes()).hexdigest() != binding['record']['request']['wav_sha256']:
            raise ValueError('试听音频与声音回执哈希不一致。')
        return path

    def _voice_play(self, payload):
        row = self._role(payload)
        kind = payload.get('kind')
        if kind not in ('selected', 'candidate'):
            raise ValueError('未知试听版本。')
        candidate = row['candidate'] if kind == 'candidate' else None
        binding = candidate['binding'] if candidate else row['selected']
        if binding is None or (kind == 'candidate' and candidate is None):
            raise ValueError('当前声音版本不可用。')
        marker = ('voice', self._book(), row['id'], candidate['id']) if candidate else None
        self._prepare_media(lambda: self._verified_voice(binding), payload.get('speed', 1),
                            row['name'] + (' · 候选' if candidate else ' · 当前声音'), marker)

    def _voice_adopt(self, payload):
        row, book = self._role(payload), self._book()
        candidate = row['candidate']
        if not candidate or payload.get('candidate_id') != candidate['id'] or not candidate['auditioned']:
            raise ValueError('请先试听当前候选，再明确采用。')
        def work():
            selected = auto.get_selected_voice(self._voice_root(row['id']), book, row['id'])
            if _digest(selected) != row['binding_signature']:
                raise ValueError('已选声线发生变化，请重新读取人物档案。')
            self._verified_voice(candidate['binding'])
            voices.select_candidate(self._voice_root(row['id']), book, row['id'], candidate['binding'],
                                    auditioned=True, design=candidate['description'],
                                    spec_id=hashlib.sha256(candidate['description'].encode()).hexdigest()[:16])
            return self._read_profiles()
        self._submit('voice_adopt', '正在核验并采用候选声音…', work, self._apply_profiles)

    def _editor(self):
        self._book()
        editor = self._state['editor']
        if editor is None:
            raise ValueError('请先打开已有完成音频的章节。')
        return editor

    def _revision_generate(self, payload):
        editor, book = self._editor(), self._book()
        chapter = editor['chapter_id']
        paragraph_id, hint = payload.get('paragraph_id'), payload.get('instruction')
        if paragraph_id not in {row['id'] for row in editor['paragraphs']}:
            raise ValueError('段落身份无效，请重新载入章节。')
        def work():
            context = revisions.get_context(self.root, book, chapter)
            return audio_revision.generate(context, paragraph_id, hint, revisions.render_revision,
                                           speaker_id=payload.get('speaker_id'))
        def apply(record):
            candidate = {'record': record, 'auditioned': False}
            self._revision_candidates[(book, chapter)] = candidate
            editor['candidate'] = self._candidate_display(candidate)
        self._submit('revision', '正在生成段落修订并重排整章…', work, apply)

    def _revision_candidate(self, payload):
        editor = self._editor()
        candidate = self._revision_candidates.get((self._book(), editor['chapter_id']))
        if not candidate or candidate['record']['revision_id'] != payload.get('candidate_id'):
            raise ValueError('修订候选已变化，请重新选择。')
        return editor, candidate

    def _revision_play(self, payload):
        editor, candidate = self._revision_candidate(payload)
        book, chapter = self._book(), editor['chapter_id']
        revision_id = candidate['record']['revision_id']
        def source():
            context = revisions.get_context(self.root, book, chapter)
            return Path(audio_revision.verify_candidate(context, revision_id)['receipt']['chapter_wav'])
        self._prepare_media(source, payload.get('speed', 1), chapter + ' · 修订候选',
                            ('revision', book, chapter, revision_id))

    def _revision_adopt(self, payload):
        editor, candidate = self._revision_candidate(payload)
        if not candidate['auditioned']:
            raise ValueError('请先试听修订候选，再明确采用。')
        book, chapter, revision_id = self._book(), editor['chapter_id'], candidate['record']['revision_id']
        def work():
            context = revisions.get_context(self.root, book, chapter)
            audio_revision.adopt(context, revision_id)
            return self._editor_data(context)
        self._submit('revision_adopt', '正在核验并采用章节修订…', work,
                     lambda result: self._state.update(editor=result))

    def _revision_restore(self, _payload):
        editor, book = self._editor(), self._book()
        def work():
            context = revisions.get_context(self.root, book, editor['chapter_id'])
            audio_revision.restore_base(context)
            return self._editor_data(context)
        self._submit('revision_restore', '正在恢复原章播放版本…', work,
                     lambda result: self._state.update(editor=result))

    def _chapter_play(self, payload):
        book, chapter = self._book(), self._chapter(payload.get('chapter_id'))
        def source():
            context = revisions.get_context(self.root, book, chapter)
            adopted = audio_revision.resolve_adopted(context)
            return Path(adopted['chapter_wav'] if adopted else context['base_wav_path'])
        self._prepare_media(source, payload.get('speed', 1), chapter, None)

    def _prepare_media(self, source, speed, label, marker):
        speed = parse_speed(speed)
        self._stop()
        play_generation = self._play_generation
        def work():
            return self.player.prepare(source(), speed)
        def apply(prepared):
            if play_generation != self._play_generation:
                self.player.discard(prepared)
                return
            media_id = secrets.token_urlsafe(24)
            self._media = {'id': media_id, 'prepared': prepared, 'marker': marker}
            self._state['playback'] = {'id': media_id, 'url': '/media/' + media_id,
                                       'label': label, 'speed': speed}
        self._submit('playback', '正在核验并准备音频…', work, apply, self.player.discard)

    def resolve_media(self, media_id):
        with self._lock:
            if self._media is None or self._media['id'] != media_id:
                raise ValueError('试听链接已失效。')
            prepared = self._media['prepared']
            if hashlib.sha256(prepared.source.read_bytes()).hexdigest() != prepared.source_sha256:
                raise ValueError('原音频已发生变化，请重新准备播放。')
            return prepared.path, prepared.sha256

    def _playback_started(self, payload):
        media_id = payload.get('media_id')
        self.resolve_media(media_id)
        marker = self._media['marker']
        if marker:
            kind, book, entity, candidate_id = marker
            if kind == 'voice':
                row = self._role_states.get((book, entity))
                if row and row['candidate'] and row['candidate']['id'] == candidate_id:
                    row['candidate']['auditioned'] = True
                    self._sync_profiles()
            else:
                candidate = self._revision_candidates.get((book, entity))
                if candidate and candidate['record']['revision_id'] == candidate_id:
                    candidate['auditioned'] = True
                    if self._state['editor'] and self._state['editor']['chapter_id'] == entity:
                        self._state['editor']['candidate'] = self._candidate_display(candidate)

    def _stop(self):
        self._play_generation += 1
        if self._media is not None:
            self.player.discard(self._media['prepared'])
        self._media = None
        self.player.stop()
        self._state['playback'] = None

    def _billing_refresh(self, _payload):
        if self._state['billing']['loading']:
            self._billing_pending = True
            return
        self._state['billing'].update(loading=True, error='')
        def scan():
            try:
                rows = billing.load_records(root=billing.ROOT, extra_roots=(self.root,))
                with self._lock:
                    if not self._closed:
                        self._billing_rows = rows
                        self._render_billing()
            except Exception as exc:
                with self._lock:
                    if not self._closed:
                        self._state['billing']['error'] = str(exc)
            finally:
                with self._lock:
                    self._state['billing']['loading'] = False
                    if self._billing_pending and not self._closed:
                        self._billing_pending = False
                        self._billing_refresh({})
        try:
            threading.Thread(target=scan, name='web-billing-scan', daemon=True).start()
        except Exception:
            self._state['billing']['loading'] = False
            raise

    def _billing_reprice(self, payload):
        a, b = str(payload.get('input_price', '')), str(payload.get('output_price', ''))
        billing.price(a)
        billing.price(b)
        self._state['billing']['prices'] = {'input': a, 'output': b}
        self._render_billing()

    def _render_billing(self):
        prices = self._state['billing']['prices']
        a, b = billing.price(prices['input']), billing.price(prices['output'])
        totals = billing.usage_totals(self._billing_rows, a, b)
        self._state['billing']['rows'] = [
            {**{key: row.get(key) for key in ('label', 'input', 'output', 'status')},
             'cost': str(billing.estimate(row, a, b)) if row.get('input') is not None else None}
            for row in self._billing_rows]
        self._state['billing']['summary'] = {
            'known_count': len(totals['known_rows']), 'unknown_count': totals['unknown_count'],
            'duplicate_count': totals['duplicate_count'],
            'input': sum(row['input'] for row in totals['known_rows']),
            'output': sum(row['output'] for row in totals['known_rows']),
            'estimated_cost': str(totals['estimated_cost']),
            'incomplete': any(row.get('_scan_incomplete') for row in self._billing_rows),
        }

    def _save_correction(self, payload):
        chapter = self._chapter(payload.get('chapter_id'))
        text, name = payload.get('text'), self._state['book_name']
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError('纠错说明应为1至2000字。')
        role = self._roles.get(payload.get('role_id'))
        character = role['name'] if role and role['id'] != 'narrator' else None
        def work():
            _book, source, _sha = core.verified_correction_source(self.selection, name, chapter)
            return correction_memory.save_note(self.root, source, text, character_name=character)
        self._submit('correction', '正在保存与章节来源绑定的纠错说明…', work)

    def _diagnostics(self, payload):
        chapter, book = self._chapter(payload.get('chapter_id')), self._book()
        self._submit('diagnostics', '正在读取归属与记忆诊断…',
                     lambda: core.task_diagnostics(self.root, book, chapter),
                     lambda result: self._state.update(diagnostics=result))
