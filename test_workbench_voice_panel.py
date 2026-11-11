import hashlib
import json
import tempfile
import types
import unittest
import tkinter as tk
from pathlib import Path
from unittest.mock import Mock, patch

import workbench_voice_panel as panel
import workbench_real_v2_ui as ui
import real_book_sequence as sequence


class InlineThread:
    def __init__(self, target, daemon=True): self.target = target
    def start(self): self.target()


class ValueBox:
    def __init__(self, value=''): self.value = value
    def set(self, value): self.value = value
    def get(self): return self.value


_ISOLATED_TK_ROOT = None


def isolated_tk_root():
    global _ISOLATED_TK_ROOT
    if _ISOLATED_TK_ROOT is None:
        _ISOLATED_TK_ROOT = tk.Tk()
    return _ISOLATED_TK_ROOT


def clear_isolated_tk_root(root):
    for child in root.winfo_children():
        child.destroy()
    root.withdraw()


class ProfilePanelHelpersTests(unittest.TestCase):
    def test_entity_identity_is_book_scoped_and_stable(self):
        expected = 'e' + hashlib.sha256('book-abc\0钟镇野'.encode()).hexdigest()[:24]
        self.assertEqual(panel.entity_id('book-abc', '钟镇野'), expected)
        self.assertNotEqual(panel.entity_id('book-def', '钟镇野'), expected)

    def test_profile_reader_checks_book_and_uses_offline_planner(self):
        doc = {'book_id': 'book-abc', 'mode': 'character_profiles_v1', 'human_reviewed': False,
               'characters': []}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'character-profiles' / 'book-abc'
            path.mkdir(parents=True)
            (path / 'current.json').write_text(json.dumps(doc), encoding='utf-8')
            with patch.object(panel.auto_voice_planner, 'plans_from_character_profiles', return_value={'plans': []}) as planner:
                actual, plans = panel.read_profile_document(temp, 'book-abc')
        self.assertEqual(actual, doc)
        self.assertEqual(plans, {'plans': []})
        planner.assert_called_once_with(doc)

    def test_profile_reader_rejects_wrong_book_without_planning(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'character-profiles' / 'book-abc'
            path.mkdir(parents=True)
            (path / 'current.json').write_text(json.dumps({
                'book_id': 'book-other', 'mode': 'character_profiles_v1', 'human_reviewed': False,
            }), encoding='utf-8')
            with patch.object(panel.auto_voice_planner, 'plans_from_character_profiles') as planner:
                with self.assertRaises(ValueError):
                    panel.read_profile_document(temp, 'book-abc')
        planner.assert_not_called()

    def test_profile_body_scrollbar_uses_same_container_and_resizes(self):
        try:
            root=isolated_tk_root()
        except tk.TclError as exc:
            self.skipTest(f'隔离 Tk 不可用：{exc}')
        try:
            root.geometry('640x420')
            root.deiconify()
            root.update_idletasks()
            view=panel.ProfileView(root);view.pack(fill='both',expand=True)
            long_text='\n'.join(f'证据行 {index}：用于验证人物档案可滚动。' for index in range(160))
            view._write(long_text)
            root.update_idletasks()
            self.assertEqual(view.body.winfo_parent(),str(view.body_frame))
            scrollbar=next(child for child in view.body_frame.winfo_children()
                           if isinstance(child,tkinter_scrollbar_type()))
            self.assertEqual(scrollbar.get()[0],0.0)
            view.body.yview_moveto(1.0);root.update_idletasks()
            self.assertGreater(scrollbar.get()[0],0.0)
            before=view.body.winfo_height()
            root.geometry('640x620');root.update()
            self.assertGreater(view.body.winfo_height(),before)
        finally:
            clear_isolated_tk_root(root)


def tkinter_scrollbar_type():
    return panel.ttk.Scrollbar

    def test_voice_description_is_labeled_as_creative_choice(self):
        with patch.object(panel.auto, 'creative_description', side_effect=lambda value: value) as guard:
            value = panel.compose_description('e123', '低沉、清楚，语速舒缓')
        self.assertIn('创作选择', value)
        self.assertIn('不作为人物事实', value)
        guard.assert_called_once_with(value)

    def test_voice_description_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            panel.compose_description('narrator', '  ')

    def test_candidate_binding_adds_scoped_key(self):
        record = {'directory': '/private/candidate', 'request': {'vector_sha256': 'a' * 64}}
        with patch.object(panel.auto, '_validate_record') as validate:
            binding = panel.candidate_binding('book-abc', 'e012', record)
        self.assertEqual(binding['console_key'], 'book-abc\0e012')
        self.assertEqual(binding['record']['book_role_key'], 'book-abc\0e012')
        self.assertNotIn('book_role_key', record)
        validate.assert_called_once_with(record)

    def test_adopting_is_explicit_and_targets_selected_root(self):
        with patch.object(panel.auto, 'select_voice', return_value={'status': 'selected'}) as select:
            result = panel.select_candidate(Path('/isolated/auto-voices'), 'book-abc', 'e012',
                                            {'console_key': 'book-abc\0e012'},
                                            auditioned=True,
                                            design='创作设计', spec_id='a' * 16)
        self.assertEqual(result, {'status': 'selected'})
        select.assert_called_once_with(Path('/isolated/auto-voices'), 'book-abc', 'e012',
                                       {'console_key': 'book-abc\0e012'},
                                       basis='操作者试听后显式采用', design='创作设计', spec_id='a' * 16)

    def test_candidate_cannot_be_adopted_before_audition(self):
        with patch.object(panel.auto, 'select_voice') as select:
            with self.assertRaises(ValueError):
                panel.select_candidate(Path('/isolated/auto-voices'), 'book-abc', 'e012',
                                       {'console_key': 'book-abc\0e012'})
        select.assert_not_called()

    def test_completed_audio_requires_receipt_manifest_and_wav_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            chapter = root / 'sequence' / 'book-abc' / 'c001'
            chapter.mkdir(parents=True)
            audio = root / 'chapter.wav'
            audio.write_bytes(b'fake-wav-bytes')
            manifest = root / 'manifest.json'
            manifest.write_text('{}', encoding='utf-8')
            renderer_receipt = {
                'manifest_path': str(manifest),
                'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
                'chapter_wav': str(audio),
                'wav_sha256': hashlib.sha256(audio.read_bytes()).hexdigest(),
                'human_reviewed': False,
            }
            render_artifact = {'receipt': renderer_receipt, 'human_reviewed': False}
            receipt_path = root / 'render-receipt.json'
            receipt_path.write_text(json.dumps(render_artifact), encoding='utf-8')
            artifact = {'path': str(receipt_path), 'sha256': hashlib.sha256(receipt_path.read_bytes()).hexdigest()}
            done = {'status': 'ok', 'artifact': artifact, 'artifact_sha256': sequence.fingerprint(artifact),
                    'human_reviewed': False,
                    'identity': {'book_id': 'book-abc', 'chapter_id': 'c001', 'stage': 'render'}}
            (chapter / 'render.done.json').write_text(json.dumps(done), encoding='utf-8')
            self.assertEqual(ui.completed_audio(root / 'sequence', 'book-abc', 'c001'), audio)
            audio.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                ui.completed_audio(root / 'sequence', 'book-abc', 'c001')


class VoicePanelButtonFlowTests(unittest.TestCase):
    def _view(self, temp, *, generator=None, audio_player=None):
        view = types.SimpleNamespace()
        entity = 'e' + '1' * 24
        plan = {'spec_id':'a' * 16}
        view.book_id='book-'+'a'*20
        view.root_provider=lambda:Path(temp)
        view.target=ValueBox('林舟')
        view.targets={'林舟':(entity,{'name':'林舟'},plan)}
        view._current_target=lambda:view.targets[view.target.get()]
        view._target_states={}
        view._active_target_key=(view.book_id,entity)
        view._target_signature=lambda book,role:{'profile':'p'*64,'binding':'b'*64}
        view.description=types.SimpleNamespace(get=lambda *args:'清晰自然的中文声音')
        view.generator=generator or Mock(return_value={'directory':str(Path(temp)/'candidate'),
            'request':{'vector_sha256':'b'*64,'wav_sha256':'c'*64}})
        view.audio_player=audio_player
        view.speed_var=ValueBox('1.08')
        view.on_busy=Mock()
        view.on_playback_state=Mock()
        view.on_stop=None
        view.operation=None
        view.play_generation=0
        view.play_pending=False
        view.owns_busy=False
        view.external_busy=False
        view.playing=False
        view.candidate=None
        view.candidate_played=False
        view.selected_binding=None
        view.errors=[]
        view.state=ValueBox()
        view.voice_detail=ValueBox()
        view._update_buttons=Mock()
        view.after=lambda _delay, callback: callback()
        view._log=lambda line:view.errors.append(line)
        view._candidate_ready=lambda result:panel.VoiceView._candidate_ready(view,result)
        view._candidate_failed=lambda error:panel.VoiceView._candidate_failed(view,error)
        view._playback_ready=lambda generation,prepared,candidate,identity=None:panel.VoiceView._playback_ready(view,generation,prepared,candidate,identity)
        view._playback_started=lambda generation,prepared,candidate,error,identity=None:panel.VoiceView._playback_started(view,generation,prepared,candidate,error,identity)
        view._playback_failed=lambda generation,error,identity=None:panel.VoiceView._playback_failed(view,generation,error,identity)
        view._release_app_busy=lambda:panel.VoiceView._release_app_busy(view)
        view._save_active_target_state=lambda:panel.VoiceView._save_active_target_state(view)
        view._store_target_state=lambda *args,**kwargs:panel.VoiceView._store_target_state(view,*args,**kwargs)
        view._start_binding_playback=lambda binding,**kwargs:panel.VoiceView._start_binding_playback(view,binding,**kwargs)
        view._prepare_binding=lambda binding,speed:panel.VoiceView._prepare_binding(view,binding,speed)
        return view

    def test_book_and_target_reload_preserve_drafts_audition_and_identity(self):
        try:
            root=isolated_tk_root()
        except tk.TclError as exc:
            self.skipTest(f'隔离 Tk 不可用：{exc}')
        try:
            root.withdraw()
            with tempfile.TemporaryDirectory() as temp, patch.object(panel.auto,'get_selected_voice',return_value=None):
                view=panel.VoiceView(root,lambda:temp)
                view.pack(fill='both',expand=True)
                book='book-'+'a'*20
                profile={'name':'林舟','voice_design':{'description':'默认描述','rationale':'依据'}}
                document={'book_id':book,'narration_design':{'description':'旁白描述'},'characters':[profile]}
                plans={'plans':[{'character':'林舟','spec_id':'a'*16}]}
                view.load_book(book,document,plans)
                view.target.set('林舟');view._show_target()
                view.description.delete('1.0','end');view.description.insert('1.0','保留的编辑草稿')
                candidate={'entity':view.targets['林舟'][0],
                           'binding':{'record':{'request':{'vector_sha256':'b'*64}}},
                           'description':'生成时的描述','spec_id':'c'*16}
                view.candidate=candidate;view.candidate_played=True;view.playing=True
                view._show_target()
                self.assertEqual(view.description.get('1.0','end-1c'),'保留的编辑草稿')
                self.assertIs(view.candidate,candidate);self.assertTrue(view.candidate_played)
                view.target.set('旁白');view._show_target()
                view.target.set('林舟');view._show_target()
                self.assertIs(view.candidate,candidate);self.assertTrue(view.candidate_played)
                view.load_book(book,document,plans)
                self.assertIs(view.candidate,candidate);self.assertTrue(view.candidate_played)
                self.assertEqual(view.description.get('1.0','end-1c'),'保留的编辑草稿')
                other_book='book-'+'d'*20
                other_document={'book_id':other_book,'narration_design':{'description':'旁白'},
                                'characters':[profile]}
                view.load_book(other_book,other_document,plans)
                self.assertNotEqual(view.targets['林舟'][0],candidate['entity'])
                self.assertIsNone(view.candidate)
                self.assertFalse(view.candidate_played)
                self.assertEqual(view.description.get('1.0','end-1c'),'默认描述\n创作依据：依据')
                changed_profile={'name':'林舟','voice_design':{'description':'更新后的描述','rationale':'新依据'}}
                changed_document={'book_id':book,'narration_design':{'description':'旁白描述'},
                                  'characters':[changed_profile]}
                view.load_book(book,changed_document,plans)
                view.target.set('林舟');view._show_target()
                self.assertIsNone(view.candidate)
                self.assertFalse(view.candidate_played)
                self.assertEqual(view.description.get('1.0','end-1c'),'更新后的描述\n创作依据：新依据')
                view.target.set('旁白');view._show_target()
                narrator_candidate={'entity':'narrator','binding':{'record':{'request':{'vector_sha256':'d'*64}}}}
                view.candidate=narrator_candidate;view.candidate_played=True
                updated_narration={'book_id':book,'narration_design':{'description':'新旁白方案'},
                                   'characters':[changed_profile]}
                view.load_book(book,updated_narration,plans)
                self.assertIsNone(view.candidate)
                self.assertFalse(view.candidate_played)
                self.assertEqual(view.description.get('1.0','end-1c'),'新旁白方案')
        finally:
            clear_isolated_tk_root(root)

    def test_candidate_result_from_old_target_is_saved_without_leaking(self):
        with tempfile.TemporaryDirectory() as temp:
            view=self._view(temp)
            old_book=view.book_id;entity=view.targets['林舟'][0]
            signature=view._target_signature(old_book,entity)
            view._target_states[(old_book,entity)]={'signature':signature}
            view._active_target_key=('other-book',entity)
            result=(old_book,entity,{},view.targets['林舟'][2],'低沉清晰',
                    {'record':{'request':{'vector_sha256':'b'*64}}},signature)
            panel.VoiceView._candidate_ready(view,result)
            self.assertIsNone(view.candidate)
            self.assertIn((old_book,entity),view._target_states)
            self.assertEqual(view._target_states[(old_book,entity)]['candidate']['entity'],entity)
            self.assertIn('未自动采用',view.errors[-1])

    def test_binding_change_invalidates_candidate_but_keeps_edit_draft(self):
        try:
            root=isolated_tk_root()
        except tk.TclError as exc:
            self.skipTest(f'隔离 Tk 不可用：{exc}')
        try:
            root.withdraw()
            with tempfile.TemporaryDirectory() as temp:
                binding_a={'record':{'request':{'vector_sha256':'a'*64}}}
                binding_b={'record':{'request':{'vector_sha256':'b'*64}}}
                with patch.object(panel.auto,'get_selected_voice',side_effect=[binding_a,binding_a,binding_a,binding_b]):
                    view=panel.VoiceView(root,lambda:temp);view.pack(fill='both',expand=True)
                    book='book-'+'e'*20
                    profile={'name':'林舟','voice_design':{'description':'默认','rationale':'依据'}}
                    document={'book_id':book,'characters':[profile]}
                    plans={'plans':[{'character':'林舟','spec_id':'d'*16}]}
                    view.load_book(book,document,plans)
                    view.target.set('林舟');view._show_target()
                    view.description.delete('1.0','end');view.description.insert('1.0','我的编辑')
                    view.candidate={'entity':view.targets['林舟'][0],'binding':binding_a}
                    view.candidate_played=True
                    view.load_book(book,document,plans)
                    view.target.set('林舟');view._show_target()
                    self.assertIsNone(view.candidate)
                    self.assertFalse(view.candidate_played)
                    self.assertEqual(view.description.get('1.0','end-1c'),'我的编辑')
        finally:
            clear_isolated_tk_root(root)

    def test_cancelled_playback_callback_discards_prepared_audio(self):
        with tempfile.TemporaryDirectory() as temp:
            player=Mock();view=self._view(temp,audio_player=player)
            view.play_generation=4
            panel.VoiceView._playback_ready(view,3,'prepared-audio',True,view._active_target_key)
            player.discard.assert_called_once_with('prepared-audio')
            self.assertFalse(view.candidate_played)

    def test_nonactive_candidate_playback_state_preserves_saved_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            view=self._view(temp)
            book=view.book_id;entity=view.targets['林舟'][0]
            candidate={'entity':entity,'binding':{'record':{'request':{'vector_sha256':'b'*64}}}}
            key=(book,entity)
            view._target_states[key]={'candidate':candidate,'candidate_played':False}
            view._active_target_key=('another-book','narrator')
            view.play_generation=2
            panel.VoiceView._playback_started(view,2,'prepared-audio',True,None,key)
            self.assertIs(view._target_states[key]['candidate'],candidate)
            self.assertTrue(view._target_states[key]['candidate_played'])
            self.assertTrue(view._target_states[key]['playing'])

    def test_generate_button_reaches_candidate_ready(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(panel.threading,'Thread',InlineThread), \
                patch.object(panel,'candidate_binding',return_value={'console_key':'fake','record':{
                    'request':{'vector_sha256':'b'*64}}}):
            view=self._view(temp)
            panel.VoiceView.generate_candidate(view)
        self.assertEqual(view.candidate['entity'],'e'+'1'*24)
        self.assertFalse(view.candidate_played)
        self.assertIn('先试听',view.state.value)
        self.assertEqual([call.args[0] for call in view.on_busy.call_args_list],[True,False])

    def test_generate_failure_and_thread_start_failure_release_busy_and_log(self):
        for generator,thread_cls in ((Mock(side_effect=RuntimeError('offline')),InlineThread),
                                     (Mock(),type('BrokenThread',(),{'__init__':lambda self,**kw:None,
                                         'start':lambda self:(_ for _ in ()).throw(OSError('thread blocked'))}))):
            with self.subTest(thread=thread_cls), tempfile.TemporaryDirectory() as temp, \
                    patch.object(panel.threading,'Thread',thread_cls):
                view=self._view(temp,generator=generator)
                with patch.object(panel,'candidate_binding',return_value={'console_key':'fake','record':{'request':{}}}):
                    panel.VoiceView.generate_candidate(view)
                self.assertIsNone(view.operation)
                self.assertTrue(view.errors)
                self.assertEqual([call.args[0] for call in view.on_busy.call_args_list],[True,False])

    def test_play_selected_button_reaches_success_and_failure_without_hanging(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp);wav=directory/'generated.wav';wav.write_bytes(b'verified sample')
            binding={'record':{'directory':str(directory),'request':{'wav_sha256':hashlib.sha256(wav.read_bytes()).hexdigest()}}}
            for start_error in (None,RuntimeError('fake audio start failure')):
                with self.subTest(start_error=start_error):
                    player=Mock()
                    player.prepare.return_value='prepared-audio'
                    if start_error: player.start.side_effect=start_error
                    view=self._view(temp,audio_player=player);view.selected_binding=binding
                    with patch.object(panel.threading,'Thread',InlineThread):
                        panel.VoiceView.play_selected(view)
                    self.assertFalse(view.play_pending)
                    self.assertFalse(view.owns_busy)
                    self.assertEqual([call.args[0] for call in view.on_busy.call_args_list],[True,False])
                    player.prepare.assert_called_once_with(wav,1.08)
                    if start_error:
                        self.assertTrue(view.errors)
                        self.assertFalse(view.playing)
                    else:
                        self.assertTrue(view.playing)
                        player.start.assert_called_once_with('prepared-audio')

    def test_playback_thread_start_failure_releases_busy_and_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp);wav=directory/'generated.wav';wav.write_bytes(b'verified sample')
            binding={'record':{'directory':str(directory),'request':{'wav_sha256':hashlib.sha256(wav.read_bytes()).hexdigest()}}}
            player=Mock();player.prepare.return_value='prepared-audio'
            broken=type('BrokenThread',(),{'__init__':lambda self,**kw:None,
                'start':lambda self:(_ for _ in ()).throw(OSError('thread blocked'))})
            view=self._view(temp,audio_player=player);view.selected_binding=binding
            with patch.object(panel.threading,'Thread',broken):
                panel.VoiceView.play_selected(view)
            self.assertFalse(view.play_pending)
            self.assertFalse(view.owns_busy)
            self.assertTrue(view.errors)
            self.assertEqual([call.args[0] for call in view.on_busy.call_args_list],[True,False])

    def test_audio_start_thread_failure_releases_busy_and_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp);wav=directory/'generated.wav';wav.write_bytes(b'verified sample')
            binding={'record':{'directory':str(directory),'request':{'wav_sha256':hashlib.sha256(wav.read_bytes()).hexdigest()}}}
            player=Mock();player.prepare.return_value='prepared-audio'
            class SecondThreadFails:
                count=0
                def __init__(self,target,daemon=True):self.target=target
                def start(self):
                    type(self).count+=1
                    if type(self).count==1:self.target()
                    else:raise OSError('audio start thread blocked')
            view=self._view(temp,audio_player=player);view.selected_binding=binding
            with patch.object(panel.threading,'Thread',SecondThreadFails):
                panel.VoiceView.play_selected(view)
            self.assertEqual(SecondThreadFails.count,2)
            self.assertFalse(view.play_pending)
            self.assertFalse(view.owns_busy)
            self.assertTrue(view.errors)
            self.assertEqual([call.args[0] for call in view.on_busy.call_args_list],[True,False])


if __name__ == '__main__':
    unittest.main()
