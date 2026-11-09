import hashlib
import json
import tempfile
import types
import unittest
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
        view._playback_ready=lambda generation,prepared,candidate:panel.VoiceView._playback_ready(view,generation,prepared,candidate)
        view._playback_started=lambda generation,prepared,candidate,error:panel.VoiceView._playback_started(view,generation,prepared,candidate,error)
        view._playback_failed=lambda generation,error:panel.VoiceView._playback_failed(view,generation,error)
        view._release_app_busy=lambda:panel.VoiceView._release_app_busy(view)
        view._start_binding_playback=lambda binding,**kwargs:panel.VoiceView._start_binding_playback(view,binding,**kwargs)
        view._prepare_binding=lambda binding,speed:panel.VoiceView._prepare_binding(view,binding,speed)
        return view

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
