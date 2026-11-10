import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path, PurePosixPath
from unittest.mock import patch
from contextlib import ExitStack

import console_chapter_render_mac as mac
import mac_chapter_renderer as adapter


def sha(data): return hashlib.sha256(data).hexdigest()

REMOTE_ROOT = PurePosixPath('/synthetic/remote-voice-root')

def patch_remote(stack):
    stack.enter_context(patch.object(adapter, '_remote_voice_root', return_value=REMOTE_ROOT))
    stack.enter_context(patch.object(adapter.release_config, 'ssh_options', return_value=[]))
    stack.enter_context(patch.object(adapter.release_config, 'ssh_target', return_value='synthetic-host'))
    stack.enter_context(patch.object(adapter.release_config, 'remote_command', return_value='python console_chapter_render_mac.py'))
    stack.enter_context(patch.object(adapter.release_config, 'scp_source', side_effect=lambda path: 'synthetic-host:' + path))


def make_wav(path):
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000); out.writeframes(b'\0\0' * 24)


class MacChapterProtocolTests(unittest.TestCase):
    def test_request_binds_concatenated_source_and_exact_speakers(self):
        text = '旁白甲说。'
        req = {'action': 'render_chapter', 'book_id': 'book-test', 'chapter_id': 'c001',
            'source_sha256': sha(text.encode()),
            'segments': [{'speaker_id': 'narrator', 'text': '旁白'}, {'speaker_id': 'hero', 'text': '甲说。'}],
            'voices': {'narrator': {'stage': '/tmp/v1', 'vector_sha256': 'a' * 64},
                       'hero': {'stage': '/tmp/v2', 'vector_sha256': 'b' * 64}}}
        self.assertEqual(mac.validate_request(req)['text'], text)
        altered = dict(req, source_sha256='0' * 64)
        with self.assertRaises(ValueError): mac.validate_request(altered)
        altered = dict(req, voices={'narrator': req['voices']['narrator']})
        with self.assertRaises(ValueError): mac.validate_request(altered)

    def test_status_is_read_only_and_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); runs = root / 'chapter-runs'; runs.mkdir(); run = runs / 'book-c001-run-abcd'; run.mkdir()
            journal = run / 'journal.json'; journal.write_text(json.dumps({'run_id': run.name, 'status': 'failed_or_unknown'}))
            before = journal.read_bytes()
            with patch.object(mac, 'ROOT', root):
                result = mac.status(run.name)
                self.assertEqual(result['journal']['status'], 'failed_or_unknown')
                self.assertEqual(journal.read_bytes(), before)
                with self.assertRaises(ValueError): mac.status('../outside')

    def test_driver_adaptation_fails_closed_on_template_and_emits_generic_voice_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); code = root / 'code'; code.mkdir()
            files = {'chapter-render-driver.py': '# generic multi-role driver\n',
                     'chapter-monitor.py': 'if time.monotonic()-start>1800: break\n',
                     'isolated_huashuo_library.py': "if command in ('synth','package'):args.extend(['--no-opening','--no-closing'])\n",
                     'release_config.py': '# portable config helper\n',
                     'native_unit_cache.py': '# content-addressed unit cache\n',
                     'mac_render_resources.py': '# shared resource slot\n',
                     'chapter_model_cache.py': '# confined COW model helper\n',
                     'natural_pacing.py': '# synthetic pacing helper\n'}
            for name, body in files.items(): (code / name).write_text(body, encoding='utf-8')
            request = {'book_id': 'b', 'chapter_id': 'c', 'source_sha256': 'a' * 64,
                'segments': [{'speaker_id': 'alpha', 'text': '甲'}, {'speaker_id': 'beta', 'text': '乙'},
                             {'speaker_id': 'gamma', 'text': '丙'}],
                'voices': {'alpha': {}, 'beta': {}, 'gamma': {}}}
            run = root / 'run'; run.mkdir()
            with patch.object(mac, 'CODE_DIR', code):
                monitor_sha = mac._build_driver(run, request, '甲乙丙')
            driver = (run / 'render-driver.py').read_text()
            self.assertIn('time.monotonic()-start>1800', (run / 'monitor.py').read_text())
            self.assertEqual(monitor_sha, sha((run / 'monitor.py').read_bytes()))
            self.assertIn('generic multi-role driver', driver)
            self.assertIn("'--no-titles'", (run / 'isolated_huashuo_library.py').read_text())
            self.assertEqual(sha((code/'natural_pacing.py').read_bytes()),sha((run/'natural_pacing.py').read_bytes()))

    def test_normal_render_entry_pins_and_stages_active_pacing_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            request={'book_id':'book-test','chapter_id':'c001','source_sha256':'a'*64,
                     'segments':[],'voices':{}}
            with patch.object(mac,'ROOT',root):
                run=mac._prepare_run(request,{},'')
            settings=json.loads((run/'cache-settings.json').read_bytes())
            helper=Path(mac.CODE_DIR)/'natural_pacing.py'
            names=('console_chapter_render_mac.py','native_unit_cache.py','chapter-render-driver.py',
                   'chapter-monitor.py','chapter_model_cache.py','mac_render_resources.py',
                   'isolated_huashuo_library.py','release_config.py','natural_pacing.py')
            pins={name:sha((Path(mac.CODE_DIR)/name).read_bytes()) for name in names}
            self.assertEqual(sha(mac._canonical(pins)),settings['adapter_sha256'])
            mac._build_driver(run,request,'')
            self.assertEqual(sha(helper.read_bytes()),sha((run/'natural_pacing.py').read_bytes()))
            driver=(run/'render-driver.py').read_text(encoding='utf-8')
            self.assertIn('post.layout=handoff_layout',driver)
            self.assertIn('adjust_narration_dialogue_pauses(plan,refs',driver)
            self.assertIn('expected=[(c,refs[role])',driver)
            self.assertIn('actual=[(c,u.voice)',driver)
            self.assertIn('original_synthesize(units,engine,workdir,language,*args,**kwargs)',driver)


class WindowsAdapterProtocolTests(unittest.TestCase):
    def test_stubbed_render_validates_response_download_pcm_and_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / 'out'; out.mkdir(); vectors = root / 'vectors'; vectors.mkdir()
            vector = vectors / 'speaker-vector.npy'; vector.write_bytes(b'vector'); vector_sha = sha(vector.read_bytes())
            book, chapter, text = 'book-test', 'c001', '旁白：你好。'
            source_sha = sha(text.encode())
            source = root / 'source.json'
            source.write_text(json.dumps({'chapters': [{'id': chapter, 'segments': [
                {'speaker_id': 'narrator', 'text': text}]}], 'audit': {'source_text_sha256': source_sha}}, ensure_ascii=False), encoding='utf-8')
            voice = {'console_key': book + '\0narrator', 'record': {'directory': str(vectors),
                'book_role_key': book + '\0narrator', 'request': {'stage': str(REMOTE_ROOT / 'voice-experiment-abc'),
                'vector_sha256': vector_sha}}}
            remote_run = 'book-test-c001-run-123abc'
            remote_dir = str(REMOTE_ROOT / 'chapter-runs' / remote_run)
            remote_wav = remote_dir + '/chapter.wav'; wav_bytes_path = root / 'remote.wav'; make_wav(wav_bytes_path)
            wav_sha = sha(wav_bytes_path.read_bytes()); captured = {}
            def runner(args, **kwargs):
                if args[0] == 'ssh':
                    payload = json.loads(kwargs['input']); captured.update(payload)
                    response = {'ok': True, 'status': 'complete', 'run_id': remote_run, 'run_dir': remote_dir,
                        'wav': remote_wav, 'wav_sha256': wav_sha, 'source_sha256': source_sha,
                        'text_sha256': source_sha,
                        'voice_hashes': {'narrator': vector_sha}, 'metadata_path': remote_dir + '/render-result.json',
                        'coverage_proof': remote_dir + '/coverage-proof.json', 'vector_reads': remote_dir + '/vector-reads.json',
                        'voice_refs': {'narrator': 'library:zh/candidate_0123456789abcdef01234567'},
                        'coverage_proof_data': {'status': 'verified', 'source_sha256': source_sha,
                            'nonwhitespace_characters': sum(not c.isspace() for c in text), 'units': 1,
                            'page_text_voice_match': True,
                            'page_text_voice_sha256': sha(json.dumps([(c, 'library:zh/candidate_0123456789abcdef01234567')
                                for c in text if not c.isspace()],ensure_ascii=False,separators=(',',':')).encode())},
                        'vector_reads_data': [{'ref': 'library:zh/candidate_0123456789abcdef01234567',
                            'sha256': vector_sha, 'shape': [2048]}],
                        'runtime_monitor': {'exit_code': 0, 'minimum_pressure_free_percent': 42,
                            'sampled_peak_rss_bytes': 1024, 'samples': 3, 'reason': None}, 'monitor_sha256': 'c' * 64,
                        'adapter_sha256': sha(Path(mac.__file__).read_bytes())}
                    response['coverage_proof_sha256'] = sha(adapter._canonical(response['coverage_proof_data']))
                    response['vector_reads_sha256'] = sha(adapter._canonical(response['vector_reads_data']))
                    return type('P', (), {'returncode': 0, 'stdout': json.dumps(response), 'stderr': ''})()
                Path(args[-1]).write_bytes(wav_bytes_path.read_bytes())
                return type('P', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
            render = adapter.MacChapterRenderer(out, runner=runner)
            with ExitStack() as stack:
                patch_remote(stack)
                receipt = render.render(source, book, chapter, source_sha, {'narrator': voice}, output_dir=out)
            self.assertEqual(captured['action'], 'render_chapter')
            self.assertEqual(receipt['kind'], 'native_huashuo_chapter_v1')
            self.assertEqual(receipt['wav_sha256'], wav_sha)
            self.assertTrue((out / 'native-huashuo-chapter-receipt.json').is_file())

    def test_unsafe_remote_path_and_bad_pcm_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / 'out'; out.mkdir(); vecdir = root / 'v'; vecdir.mkdir()
            vector = vecdir / 'speaker-vector.npy'; vector.write_bytes(b'x'); vh = sha(b'x')
            source = root / 's.json'; text = 'x'; sh = sha(text.encode())
            source.write_text(json.dumps({'chapters': [{'id': 'c', 'segments': [{'speaker_id': 'narrator', 'text': text}]}],
                'audit': {'source_text_sha256': sh}}), encoding='utf-8')
            binding = {'console_key': 'b\0narrator', 'record': {'directory': str(vecdir), 'request': {
                'stage': str(REMOTE_ROOT / 'voice-experiment-x'), 'vector_sha256': vh}}}
            def bad_remote(args, **kwargs):
                body = {'ok': True, 'status': 'complete', 'run_id': 'b-c-run-x',
                    'run_dir': str(REMOTE_ROOT / 'chapter-runs' / 'b-c-run-x'),
                    'wav': str(REMOTE_ROOT / 'chapter-runs' / '..' / 'escape' / 'chapter.wav'), 'wav_sha256': 'a' * 64,
                    'source_sha256': sh, 'text_sha256': sh, 'voice_hashes': {'narrator': vh},
                    'metadata_path': str(REMOTE_ROOT / 'x'), 'coverage_proof': str(REMOTE_ROOT / 'x'),
                    'vector_reads': str(REMOTE_ROOT / 'x'), 'voice_refs': {'narrator': 'library:zh/candidate_x'},
                    'coverage_proof_data': {'status': 'verified', 'source_sha256': sh,
                        'nonwhitespace_characters': 1, 'units': 1, 'page_text_voice_match': True,
                        'page_text_voice_sha256': sha(json.dumps([('x','library:zh/candidate_x')],
                            ensure_ascii=False,separators=(',',':')).encode())},
                    'vector_reads_data': [{'ref': 'library:zh/candidate_x', 'sha256': vh, 'shape': [2048]}],
                    'runtime_monitor': {'exit_code': 0, 'reason': None, 'samples': 1,
                        'minimum_pressure_free_percent': 30, 'sampled_peak_rss_bytes': 1}, 'monitor_sha256': 'b' * 64,
                    'adapter_sha256': sha(Path(mac.__file__).read_bytes())}
                body['coverage_proof_sha256'] = sha(adapter._canonical(body['coverage_proof_data']))
                body['vector_reads_sha256'] = sha(adapter._canonical(body['vector_reads_data']))
                return type('P', (), {'returncode': 0, 'stdout': json.dumps(body), 'stderr': ''})()
            with ExitStack() as stack:
                patch_remote(stack)
                with self.assertRaises(ValueError):
                    adapter.MacChapterRenderer(out, runner=bad_remote).render(source, 'b', 'c', sh, {'narrator': binding}, output_dir=out)


if __name__ == '__main__': unittest.main()
