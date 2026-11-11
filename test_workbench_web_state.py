import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from workbench_web_state import WorkbenchService, _digest
import workbench_real_v2_ui as core
from workbench_book_import import BookImporter

VALID_BOOK_ID = 'book-0123456789abcdefabcd'


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def action(service, name, payload=None):
    payload = dict(payload or {})
    scoped = {'index', 'range', 'progress', 'profiles_analyze', 'execute', 'pause',
              'chapter', 'voice_draft', 'voice_generate', 'voice_play', 'voice_adopt',
              'revision_generate', 'revision_play', 'revision_adopt', 'revision_restore',
              'chapter_play', 'correction_save', 'diagnostics'}
    if name in scoped:
        payload.setdefault('expected_book_id', service.snapshot()['book_id'])
    if name.startswith('revision_'):
        editor = service.snapshot()['editor']
        payload.setdefault('expected_chapter_id', editor['chapter_id'])
    return service.action(name, payload)


class FakeSelection:
    def __init__(self):
        self.book = None
        self.chapters = ()
        self.frozen = ()

    def load(self, name):
        self.book = {'Book A':'book-a', 'Book B':'book-b', 'Imported':'book-0123456789abcdefabcd'}[name]
        self.chapters = ('c001', 'c002')
        return self.chapters


class FakeImporter:
    def __init__(self):
        self.commits = []
    def list_books(self):
        return []
    def preview(self, path, name):
        return {'preview_id':'preview-1', 'book_id':'book-0123456789abcdefabcd', 'filename':name,
                'title':'Imported', 'chapters':[{'id':'c001','title':'Chapter 1'}]}
    def find_heading(self, preview_id, heading):
        return [{'id':'c002','title':heading,'start':10,'end':20,'characters':10,'excerpt':'x'}]
    def commit(self, preview_id, title, starts):
        self.commits.append((preview_id, title, starts))
        zero = '0' * 64
        identity = 'book-0123456789abcdefabcd'
        return {'book_id':identity, 'display_name':'Imported',
                'chapters':[{'chapter_id':'c001','title':'Chapter 1'}],
                'source_sha256':zero, 'text_sha256':zero, 'index_sha256':zero,
                'snapshot':{'book_id':identity, 'source_sha256':zero, 'text_sha256':zero,
                            'script_sha256':zero, 'cast_sha256':zero},
                'index_workspace':'book-imports/' + zero[:32] + '/index/workspace',
                'import_dir':'book-imports/' + zero[:32]}

    def preview_range(self, *args):
        self.frozen = args


class FakePlayer:
    def __init__(self):
        self.stops = 0
        self.discarded = []
        self.prepare_hook = None

    def prepare(self, source, speed):
        if self.prepare_hook:
            return self.prepare_hook(source, speed)
        path = Path(source)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return SimpleNamespace(source=path, path=path, source_sha256=digest, sha256=digest,
                               speed=speed, generation=self.stops, temporary=False)

    def stop(self):
        self.stops += 1

    def discard(self, prepared):
        self.discarded.append(prepared)


class FakeExecutor:
    def __init__(self, result=None):
        self.result = result or {'status':'complete'}
        self.stop_requests = 0
        self.calls = []

    def request_stop(self):
        self.stop_requests += 1

    def execute_range(self, frozen, voices):
        self.calls.append((frozen, voices))
        return self.result


class WorkbenchWebStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        runtime = SimpleNamespace(ROOT=self.root)
        self.selection = FakeSelection()
        self.player = FakePlayer()
        self.importer = FakeImporter()
        self.service = WorkbenchService(runtime=runtime, books={'Book A':'book-a', 'Book B':'book-b'},
                                        selection=self.selection, player=self.player,
                                        importer=self.importer)
        self.service._state.update(book_id='book-a', book_name='Book A')
        self.selection.book = 'book-a'
        self.selection.chapters = ('c001', 'c002')

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def wait_idle(self):
        self.assertTrue(wait_for(lambda: self.service.snapshot()['busy'] is None),
                        self.service.snapshot())

    def test_import_preview_heading_and_explicit_commit_select_book(self):
        staged = self.root / 'upload.txt'; staged.write_text('local text', encoding='utf-8')
        self.service.prepare_import(staged, 'novel.txt')
        self.wait_idle()
        self.assertEqual(self.service.snapshot()['import_preview']['preview_id'], 'preview-1')
        action(self.service, 'import_heading', {'preview_id':'preview-1','heading':'Chapter 2'})
        self.wait_idle()
        self.assertEqual(self.service.snapshot()['import_preview']['heading_matches'][0]['id'], 'c002')
        action(self.service, 'import_commit', {'preview_id':'preview-1','title':'Imported',
                                               'chapter_starts':[0]})
        self.wait_idle()
        state = self.service.snapshot()
        self.assertEqual(state['book_id'], 'book-0123456789abcdefabcd')
        self.assertEqual(state['book_name'], 'Imported')
        self.assertIsNone(state['import_preview'])
        self.assertEqual(self.importer.commits, [('preview-1','Imported',[0])])
        self.assertIn('book-0123456789abcdefabcd', core.existing.BOOKS.values())

    def test_real_importer_commit_loads_its_local_index_without_private_paths_in_state(self):
        importer = BookImporter(self.root)
        selection = core.Selection(request=lambda action_name, book_id, chapter=None:
                                   importer.query(action_name, book_id, chapter))
        service = WorkbenchService(runtime=SimpleNamespace(ROOT=self.root), books={},
                                   selection=selection, importer=importer)
        staged = self.root / 'uploaded.txt'
        staged.write_text('第一章 开始\n这是本地导入的正文。\n第二章 继续\n第二段正文。\n', encoding='utf-8')
        service.prepare_import(staged, '本地书.txt')
        self.assertTrue(wait_for(lambda: service.snapshot()['busy'] is None))
        summary = service.snapshot()['import_preview']
        self.assertEqual(summary['filename'], '本地书.txt')
        service.action('import_commit', {'preview_id':summary['preview_id'],
                                         'title':'本地确认书名', 'chapter_starts':None})
        self.assertTrue(wait_for(lambda: service.snapshot()['busy'] is None))
        state = service.snapshot()
        self.assertEqual(state['book_id'], summary['book_id'])
        self.assertEqual(state['book_name'], '本地确认书名')
        self.assertEqual(state['chapters'][0]['id'], 'c001')
        self.assertEqual(state['chapters'][0]['title'], '第一章 开始')
        self.assertEqual(importer.query('list', summary['book_id'])['chapters'], ['c001', 'c002'])
        self.assertNotIn('import_dir', repr(state))
        self.assertNotIn('index_workspace', repr(state))
        service.close()

    def test_matching_existing_remote_book_reuses_its_old_index_without_catalog_commit(self):
        self.importer.preview = lambda _path, name: {
            'preview_id':'preview-old', 'book_id':'book-a', 'filename':name,
            'title':'same raw source', 'chapters':[{'id':'c001','title':'New local title'}]}
        staged = self.root / 'old-source.txt'; staged.write_text('same source', encoding='utf-8')
        self.service.prepare_import(staged, 'same.txt')
        self.wait_idle()
        preview = self.service.snapshot()['import_preview']
        self.assertTrue(preview['existing_book'])
        self.assertEqual(preview['existing_name'], 'Book A')
        action(self.service, 'import_commit', {'preview_id':'preview-old', 'title':'Ignored title'})
        self.wait_idle()
        state = self.service.snapshot()
        self.assertEqual(state['book_id'], 'book-a')
        self.assertEqual(state['book_name'], 'Book A')
        self.assertEqual(self.importer.commits, [])
        self.assertEqual(self.selection.book, 'book-a')

    def test_upload_reservation_blocks_state_changing_actions_until_cancelled(self):
        self.assertTrue(self.service.reserve_import_upload())
        with self.assertRaisesRegex(RuntimeError, '正在进行'):
            self.service.action('book', {'book_id':'book-b'})
        with self.assertRaisesRegex(RuntimeError, '正在进行'):
            action(self.service, 'execute')
        with self.assertRaisesRegex(RuntimeError, '正在进行'):
            self.service.action('shutdown', {})
        with self.assertRaisesRegex(RuntimeError, '正在进行'):
            self.service.action('import_clear', {})
        self.service.cancel_import_upload()
        action(self.service, 'book', {'book_id':'book-b'})
        self.wait_idle()
        self.assertEqual(self.service.snapshot()['book_id'], 'book-b')

    def test_async_submit_returns_busy_without_blocking_and_rejects_second_work(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def work():
            entered.set()
            release.wait(3)
            return {'done':True}
        self.service._submit('probe', 'working', work, lambda result: finished.set())
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.service.snapshot()['busy']['kind'], 'probe')
        with self.assertRaisesRegex(RuntimeError, '正在进行'):
            action(self.service, 'progress', {})
        with self.assertRaisesRegex(RuntimeError, '正在进行'):
            self.service.action('voice_draft', {'expected_book_id':'book-a',
                                                 'role_id':'role-1', 'description':'new draft'})
        release.set()
        self.assertTrue(finished.wait(2))
        self.wait_idle()

    def test_invalid_or_cross_book_chapter_is_rejected(self):
        with self.assertRaises(ValueError):
            action(self.service, 'chapter', {'chapter_id':'c999'})
        self.selection.book = 'book-b'
        with self.assertRaises(ValueError):
            action(self.service, 'chapter', {'chapter_id':'c001'})

    def _install_voice_row(self, *, auditioned=False, signature='same', binding=None):
        selected = binding if binding is not None else {'record':{'request':{'vector_sha256':'original'}}}
        row = {'id':'role-1', 'name':'角色一', 'profile':{}, 'plan':{'plan':'ready'},
               'selected':selected, 'description':'用户草稿', 'signature':signature,
               'binding_signature':_digest(selected),
               'candidate':{'id':'candidate-1', 'binding':{}, 'description':'candidate voice',
                            'auditioned':auditioned, 'signature':(signature, _digest(selected))}}
        self.service._roles[row['id']] = row
        self.service._role_states[('book-a', row['id'])] = row
        return row

    def test_same_book_profile_refresh_keeps_description_and_candidate(self):
        row = self._install_voice_row(auditioned=True)
        self.service._apply_profiles([{'id':'role-1', 'name':'角色一', 'profile':{},
                                       'plan':{'plan':'ready'},
                                       'selected':{'record':{'request':{'vector_sha256':'original'}}},
                                       'description':'disk description', 'signature':'same'}])
        refreshed = self.service._roles['role-1']
        self.assertEqual(refreshed['description'], '用户草稿')
        self.assertEqual(refreshed['candidate']['id'], 'candidate-1')
        self.service._apply_profiles([{'id':'role-1', 'name':'角色一', 'profile':{},
                                       'plan':{'plan':'ready'},
                                       'selected':{'record':{'request':{'vector_sha256':'changed'}}},
                                       'description':'new disk', 'signature':'same'}])
        refreshed = self.service._roles['role-1']
        self.assertEqual(refreshed['description'], '用户草稿')
        self.assertIsNone(refreshed['candidate'])

    def test_voice_adoption_requires_audition_and_rejects_stale_binding(self):
        row = self._install_voice_row(auditioned=False)
        with self.assertRaisesRegex(ValueError, '先试听'):
            action(self.service, 'voice_adopt', {'role_id':'role-1', 'candidate_id':'candidate-1'})
        row['candidate']['auditioned'] = True
        stale = {'record':{'request':{'vector_sha256':'changed'}}}
        with patch('workbench_web_state.auto.get_selected_voice', return_value=stale):
            action(self.service, 'voice_adopt', {'role_id':'role-1', 'candidate_id':'candidate-1'})
            self.wait_idle()
        self.assertIn('已选声线发生变化', self.service.snapshot()['error'])

    def test_voice_audition_marker_requires_current_candidate_and_hash_is_checked(self):
        row = self._install_voice_row()
        self.service._media = {'id':'media-1', 'prepared':None,
                               'marker':('voice','book-a','role-1','candidate-1')}
        with patch.object(self.service, 'resolve_media', return_value=(None, None)):
            self.service._playback_started({'media_id':'media-1'})
        self.assertTrue(row['candidate']['auditioned'])
        audio = self.root/'generated.wav'; audio.write_bytes(b'actual bytes')
        binding = {'record':{'directory':str(self.root), 'request':{'wav_sha256':'0'*64}}}
        with patch('workbench_web_state.auto._validate_record'):
            with self.assertRaisesRegex(ValueError, '哈希不一致'):
                self.service._verified_voice(binding)

    def test_revision_generation_forwards_speaker_and_adoption_requires_audition(self):
        self.service._state['editor'] = {'chapter_id':'c001', 'paragraphs':[{'id':'p1'}],
                                         'candidate':None, 'adopted':False}
        record = {'revision_id':'rev1', 'paragraph_id':'p1', 'instruction':'收句',
                  'speaker_id':'role-1', 'receipt':{'cache_statistics':{}}}
        with patch('workbench_web_state.revisions.get_context', return_value={'chapter_id':'c001'}), \
             patch('workbench_web_state.audio_revision.generate', return_value=record) as generate:
            action(self.service, 'revision_generate', {'paragraph_id':'p1', 'instruction':'收句',
                                                        'speaker_id':'role-1'})
            self.wait_idle()
        self.assertEqual(generate.call_args.kwargs['speaker_id'], 'role-1')
        self.assertEqual(self.service.snapshot()['editor']['candidate']['speaker_id'], 'role-1')
        with self.assertRaisesRegex(ValueError, '先试听'):
            action(self.service, 'revision_adopt', {'candidate_id':'rev1'})
        candidate = self.service._revision_candidates[('book-a', 'c001')]
        self.service._media = {'id':'revision-media', 'prepared':None,
                               'marker':('revision','book-a','c001','rev1')}
        with patch.object(self.service, 'resolve_media', return_value=(None, None)):
            self.service._playback_started({'media_id':'revision-media'})
        self.assertTrue(candidate['auditioned'])
        with patch('workbench_web_state.revisions.get_context', return_value={'chapter_id':'c001'}), \
             patch('workbench_web_state.audio_revision.adopt', return_value={}) as adopt, \
             patch.object(self.service, '_editor_data', return_value={
                 'chapter_id':'c001', 'candidate':{'id':'rev1','adopted':True},
                 'adopted':True, 'adopted_revision_id':'rev1'}) as editor_data:
            action(self.service, 'revision_adopt', {'candidate_id':'rev1'})
            self.wait_idle()
        adopt.assert_called_once()
        editor_data.assert_called_once()
        self.assertTrue(self.service.snapshot()['editor']['adopted'])

    def test_old_active_revision_does_not_mark_new_candidate_and_restore_preserves_audition(self):
        normalized = {'chapters':[{'segments':[{'instruct':'base instruction',
                                                'delivery':'neutral'}]}]}
        stage_path = self.root/'normalized-editor-stage.json'
        stage_raw = core.workflow._bytes(normalized)
        stage_path.write_bytes(stage_raw)
        context = {'chapter_id':'c001', 'normalized_stage_path':str(stage_path),
                   'normalized_stage_sha256':hashlib.sha256(stage_raw).hexdigest(),
                   'voice_bindings':{'narrator':'voice'}}
        old_record = {'revision_id':'old-active', 'paragraph_id':'p1',
                      'speaker_override_id':'old-speaker', 'instruction':'old active instruction',
                      'receipt':{'cache_statistics':{}}}
        new_record = {'revision_id':'new-candidate', 'paragraph_id':'p1',
                      'speaker_override_id':'new-speaker', 'instruction':'new candidate instruction',
                      'receipt':{'cache_statistics':{}}}
        candidate = {'record':new_record, 'auditioned':True}
        self.service._revision_candidates[('book-a','c001')] = candidate
        paragraphs = [{'id':'p1', 'index':0, 'speaker_id':'narrator', 'text':'测试'}]
        with patch('workbench_web_state.audio_revision.list_paragraphs', return_value=paragraphs), \
             patch('workbench_web_state.audio_revision.resolve_adopted',
                   return_value={'revision_id':'old-active'}), \
             patch('workbench_web_state.audio_revision.verify_candidate',
                   side_effect=lambda _context, revision_id:
                   {'old-active':old_record, 'new-candidate':new_record}[revision_id]):
            editor = self.service._editor_data(context)
        self.assertEqual(editor['adopted_revision_id'], 'old-active')
        self.assertFalse(editor['candidate']['adopted'])
        self.assertEqual(editor['paragraphs'][0]['speaker_id'], 'old-speaker')
        self.assertEqual(editor['paragraphs'][0]['source_speaker_id'], 'narrator')
        self.assertEqual(editor['paragraphs'][0]['instruct'], 'old active instruction')
        self.service._state['editor'] = editor
        with patch('workbench_web_state.revisions.get_context', return_value=context), \
             patch('workbench_web_state.audio_revision.restore_base') as restore, \
             patch('workbench_web_state.audio_revision.list_paragraphs', return_value=paragraphs), \
             patch('workbench_web_state.audio_revision.resolve_adopted', return_value=None), \
             patch('workbench_web_state.audio_revision.verify_candidate', return_value=new_record):
            action(self.service, 'revision_restore', {})
            self.wait_idle()
        restore.assert_called_once_with(context)
        restored = self.service.snapshot()['editor']
        self.assertIsNone(restored['adopted_revision_id'])
        self.assertFalse(restored['candidate']['adopted'])
        self.assertTrue(restored['candidate']['auditioned'])
        self.assertIn(('book-a','c001'), self.service._revision_candidates)

    def test_media_source_hash_stop_and_stop_before_player_prepare(self):
        source = self.root/'chapter.wav'; source.write_bytes(b'audio bytes')
        self.service._prepare_media(lambda: source, 1.1, 'chapter', None)
        self.wait_idle()
        media_id = self.service.snapshot()['playback']['id']
        _, expected = self.service.resolve_media(media_id)
        self.assertEqual(expected, hashlib.sha256(source.read_bytes()).hexdigest())
        source.write_bytes(b'tampered source')
        with self.assertRaisesRegex(ValueError, '发生变化'):
            self.service.resolve_media(media_id)

        entered, release = threading.Event(), threading.Event()
        prepared = SimpleNamespace(source=source, path=source, source_sha256='s', sha256='p',
                                   speed=1.0, generation=0, temporary=True)
        def delayed_source():
            entered.set(); release.wait(3); return source
        self.player.prepare_hook = lambda path, speed: prepared
        self.service._prepare_media(delayed_source, 1.0, 'stale', None)
        self.assertTrue(entered.wait(2))
        action(self.service, 'stop', {})
        release.set()
        self.wait_idle()
        self.assertIsNone(self.service.snapshot()['playback'])
        # Root's play-generation guard is expected to discard results that finish
        # after stop, even when the player itself accepts the late prepare.
        self.assertIn(prepared, self.player.discarded)

    def test_billing_refresh_coalesces_background_scans_unknown_rows_survive_reprice(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        rows = [{'label':'known', 'input':100, 'output':20, 'status':'success'},
                {'label':'unknown', 'input':None, 'output':None, 'status':'unknown'}]
        def load_records(**kwargs):
            calls.append(True)
            if len(calls) == 1:
                entered.set(); release.wait(3)
            return rows
        with patch('workbench_web_state.billing.load_records', side_effect=load_records):
            action(self.service, 'billing_refresh', {})
            self.assertTrue(entered.wait(2))
            action(self.service, 'billing_refresh', {})
            action(self.service, 'billing_refresh', {})
            release.set()
            self.assertTrue(wait_for(lambda: not self.service.snapshot()['billing']['loading']))
            self.assertEqual(len(calls), 2)
            state = self.service.snapshot()['billing']
            self.assertEqual(state['summary']['unknown_count'], 1)
            self.assertIsNone(state['rows'][1]['cost'])
            action(self.service, 'billing_reprice', {'input_price':'3', 'output_price':'9'})
            self.assertEqual(len(calls), 2)
            self.assertEqual(self.service.snapshot()['billing']['prices'], {'input':'3', 'output':'9'})

    def test_close_drops_late_billing_result(self):
        entered, release = threading.Event(), threading.Event()
        def load_records(**kwargs):
            entered.set(); release.wait(3)
            return [{'label':'late', 'input':1, 'output':1, 'status':'success'}]
        with patch('workbench_web_state.billing.load_records', side_effect=load_records):
            action(self.service, 'billing_refresh', {})
            self.assertTrue(entered.wait(2))
            self.service.close()
            release.set()
            self.assertTrue(wait_for(lambda: not self.service.snapshot()['billing']['loading']))
        self.assertEqual(self.service.snapshot()['billing']['rows'], [])

    def test_close_drops_and_discards_late_media_result(self):
        source = self.root/'late.wav'; source.write_bytes(b'late bytes')
        entered, release = threading.Event(), threading.Event()
        prepared = SimpleNamespace(source=source, path=source, source_sha256='s', sha256='p',
                                   speed=1.0, generation=0, temporary=True)
        def delayed_prepare(path, speed):
            entered.set(); release.wait(3)
            return prepared
        self.player.prepare_hook = delayed_prepare
        self.service._prepare_media(lambda: source, 1.0, 'late', None)
        self.assertTrue(entered.wait(2))
        self.service.close()
        release.set()
        self.assertTrue(wait_for(lambda: prepared in self.player.discarded))
        self.assertIsNone(self.service.snapshot()['playback'])

    def _real_selection_service(self, make_executor):
        index_sha = 'a' * 64
        def request(action, book, chapter=None):
            if action == 'list':
                return {'book_id':book, 'human_reviewed':False,
                        'chapters':['c001','c002'], 'index_sha256':index_sha}
            payload = {'book_id':book, 'chapter_id':chapter, 'text':'短小测试章节'}
            return {'book_id':book, 'human_reviewed':False, 'index_sha256':index_sha,
                    'payload':payload,
                    'payload_sha256':hashlib.sha256(core.workflow._bytes(payload)).hexdigest()}
        selection = core.Selection(request=request)
        runtime = SimpleNamespace(ROOT=self.root, make_executor=make_executor)
        service = WorkbenchService(runtime=runtime, books={'Book A':VALID_BOOK_ID}, selection=selection,
                                   player=self.player)
        self.service.close()
        self.service = service
        return service, selection

    def _enter_book_index_range(self, service):
        service.action('book', {'book_id':next(iter(service.books.values()))})
        self.assertTrue(wait_for(lambda: service.snapshot()['busy'] is None))
        action(service, 'index', {})
        self.assertTrue(wait_for(lambda: service.snapshot()['busy'] is None))
        action(service, 'range', {'start':'c001', 'end':'c002'})

    def test_normal_action_flow_loads_real_index_and_passes_frozen_range(self):
        executor = FakeExecutor()
        runtime_calls = []
        def make_executor(name):
            runtime_calls.append(name)
            return {'narrator':'fake-voice'}, executor
        service, selection = self._real_selection_service(make_executor)
        progress = [{'chapter_id':cid, 'completed_stages':[], 'audio_path':None,
                     'audio_version':None, 'diagnostics':None} for cid in ('c001','c002')]
        with patch('workbench_web_state.core.existing.BOOKS', {'Book A':VALID_BOOK_ID}), \
             patch.object(service, '_read_profiles', return_value=[]), \
             patch('workbench_web_state.core.inspect_chapter_progress', return_value=progress), \
             patch('workbench_web_state.core.task_diagnostics', return_value={'status':'ok'}) as diagnostics, \
             patch('workbench_web_state.correction_memory.save_note', return_value={'saved':True}) as save:
            self._enter_book_index_range(service)
            self.assertEqual(service.snapshot()['range'], {'start':'c001','end':'c002'})
            with self.assertRaisesRegex(ValueError, '结束章节必须不早于'):
                action(service, 'range', {'start':'c002','end':'c001'})
            with self.assertRaisesRegex(ValueError, '书籍选择已变化'):
                service.action('progress', {'expected_book_id':'book-old'})
            action(service, 'progress', {})
            self.wait_idle()
            action(service, 'execute', {})
            self.wait_idle()
            self.assertEqual(runtime_calls, ['Book A'])
            self.assertEqual(len(executor.calls), 1)
            frozen, voices = executor.calls[0]
            self.assertEqual([pair[0]['chapter_id'] for pair in frozen], ['c001','c002'])
            self.assertEqual(voices, {'narrator':'fake-voice'})
            self.assertEqual(selection.frozen[0], frozen)
            normalized = {'chapters':[{'segments':[{'instruct':'轻声一些','delivery':'calm'}]}]}
            stage_path = self.root/'normalized.json'
            stage_raw = core.workflow._bytes(normalized)
            stage_path.write_bytes(stage_raw)
            context = {'chapter_id':'c001', 'normalized_stage_path':str(stage_path),
                       'normalized_stage_sha256':hashlib.sha256(stage_raw).hexdigest(),
                       'voice_bindings':{'narrator':'voice'}}
            with patch('workbench_web_state.revisions.get_context', return_value=context), \
                 patch('workbench_web_state.audio_revision.list_paragraphs', return_value=[
                     {'id':'p1', 'index':0, 'speaker_id':'narrator', 'text':'测试'}]), \
                 patch('workbench_web_state.audio_revision.resolve_adopted', return_value=None):
                action(service, 'chapter', {'chapter_id':'c001'})
                self.wait_idle()
            editor = service.snapshot()['editor']
            self.assertEqual(editor['paragraphs'][0]['instruct'], '轻声一些')
            self.assertEqual(editor['paragraphs'][0]['delivery'], 'calm')
            self.assertEqual(editor['revision_basis'], 'base')
            with self.assertRaisesRegex(ValueError, '校听章节已变化'):
                service.action('revision_generate', {'expected_book_id':VALID_BOOK_ID,
                    'expected_chapter_id':'c002', 'paragraph_id':'p1', 'instruction':'stale'})
            action(service, 'diagnostics', {'chapter_id':'c001'})
            self.wait_idle()
            diagnostics.assert_called_once()
            action(service, 'correction_save', {'chapter_id':'c001','text':'核对这个短句'})
            self.wait_idle()
            save.assert_called_once()
            self.assertEqual(service.snapshot()['error'], '')

    def test_pause_during_executor_creation_or_preview_skips_execute_range(self):
        for pause_stage in ('make_executor', 'preview'):
            with self.subTest(pause_stage=pause_stage):
                entered, release = threading.Event(), threading.Event()
                executor = FakeExecutor()
                def make_executor(_name):
                    if pause_stage == 'make_executor':
                        entered.set(); release.wait(3)
                    return {'narrator':'fake'}, executor
                service, selection = self._real_selection_service(make_executor)
                progress = [{'chapter_id':cid, 'completed_stages':[], 'audio_path':None,
                             'audio_version':None, 'diagnostics':None} for cid in ('c001','c002')]
                with patch('workbench_web_state.core.existing.BOOKS', {'Book A':VALID_BOOK_ID}), \
                     patch.object(service, '_read_profiles', return_value=[]), \
                     patch('workbench_web_state.core.inspect_chapter_progress', return_value=progress):
                    self._enter_book_index_range(service)
                    if pause_stage == 'preview':
                        original = selection.preview_range
                        def blocked_preview(*args):
                            entered.set(); release.wait(3)
                            return original(*args)
                        selection.preview_range = blocked_preview
                    action(service, 'execute', {})
                    self.assertTrue(entered.wait(2))
                    action(service, 'pause', {})
                    release.set()
                    self.wait_idle()
                self.assertEqual(executor.calls, [])
                if pause_stage == 'preview':
                    self.assertGreaterEqual(executor.stop_requests, 1)

    def test_failed_task_clears_busy_and_allows_retry(self):
        attempts = []
        executor = FakeExecutor({'status':'error', 'message':'synthetic failure'})
        def make_executor(_name):
            attempts.append(True)
            return {'narrator':'fake'}, executor
        service, _selection = self._real_selection_service(make_executor)
        progress = [{'chapter_id':cid, 'completed_stages':[], 'audio_path':None,
                     'audio_version':None, 'diagnostics':None} for cid in ('c001','c002')]
        with patch('workbench_web_state.core.existing.BOOKS', {'Book A':VALID_BOOK_ID}), \
             patch.object(service, '_read_profiles', return_value=[]), \
             patch('workbench_web_state.core.inspect_chapter_progress', return_value=progress):
            self._enter_book_index_range(service)
            action(service, 'execute', {})
            self.wait_idle()
            self.assertIn('synthetic failure', service.snapshot()['error'])
            self.assertIsNone(service.snapshot()['busy'])
            action(service, 'progress', {})
            self.wait_idle()
            self.assertEqual(len(attempts), 1)


if __name__ == '__main__':
    unittest.main()
