import hashlib
import io
import json
import os
import tempfile
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

import console_voice_ui as ui


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
        wav.writeframes(b'\0\0' * 48)
    return buffer.getvalue()


class ConsoleVoiceTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'voice-candidate'
        self.directory.mkdir()
        if hasattr(ui, 'release_config'):
            self.config_patches = [
                patch.object(ui.release_config, 'remote_voice_root',
                             return_value=PurePosixPath('/private-remote/voices')),
                patch.object(ui.release_config, 'remote_path_is_within',
                             side_effect=lambda value, root: PurePosixPath(value).is_relative_to(PurePosixPath(root))),
            ]
            for item in self.config_patches:
                item.start(); self.addCleanup(item.stop)
        self.wav = wav_bytes()
        self.vector = b'fixture-vector-bytes'
        self.result = {'ok': True, 'stage': self._stage(),
            'wav_sha256': digest_bytes(self.wav), 'vector_sha256': digest_bytes(self.vector)}

    @staticmethod
    def _stage():
        return str(ui.release_config.remote_voice_root() / 'voice-experiment-transfer-fixture')

    def _patch_commands(self, stack, *, fail_download=None, ssh_counter=None):
        def ssh(*args, **kwargs):
            if ssh_counter is not None:
                ssh_counter.append(1)
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.result), stderr='')

        def download(remote, target):
            filename = PurePosixName(remote)
            target = Path(target)
            if fail_download == filename:
                return SimpleNamespace(returncode=1, stdout='', stderr='fixture scp failure')
            target.write_bytes(self.wav if filename == 'generated.wav' else self.vector)
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        if hasattr(ui, 'release_config'):
            stack.enter_context(patch.object(ui, 'ssh_python', side_effect=ssh))
            stack.enter_context(patch.object(ui, 'scp_from_mac', side_effect=download))
        else:
            def run(args, **kwargs):
                if args[0] == 'ssh':
                    return ssh(args, **kwargs)
                if args[0] == 'scp':
                    return download(args[-2].split(':', 1)[-1], args[-1])
                raise AssertionError('unexpected subprocess')
            stack.enter_context(patch.object(ui.subprocess, 'run', side_effect=run))

    def test_remote_completion_is_fsynced_before_first_scp(self):
        with ExitStack() as stack:
            if hasattr(ui, 'release_config'):
                stack.enter_context(patch.object(ui, 'ssh_python', return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(self.result), stderr='')))

                def download(remote, target):
                    marker = self.directory / 'remote-completion.json'
                    self.assertTrue(marker.is_file())
                    value = json.loads(marker.read_bytes())
                    self.assertEqual(value['status'], 'remote_complete')
                    self.assertTrue(value['request_sha256'])
                    target = Path(target)
                    target.write_bytes(self.wav if PurePosixName(remote) == 'generated.wav' else self.vector)
                    return SimpleNamespace(returncode=0, stdout='', stderr='')
                stack.enter_context(patch.object(ui, 'scp_from_mac', side_effect=download))
            else:
                def run(args, **kwargs):
                    if args[0] == 'ssh':
                        return SimpleNamespace(returncode=0, stdout=json.dumps(self.result), stderr='')
                    self.assertTrue((self.directory / 'remote-completion.json').is_file())
                    target = Path(args[-1]); remote = args[-2].split(':', 1)[-1]
                    target.write_bytes(self.wav if PurePosixName(remote) == 'generated.wav' else self.vector)
                    return SimpleNamespace(returncode=0, stdout='', stderr='')
                stack.enter_context(patch.object(ui.subprocess, 'run', side_effect=run))
            request = ui.invoke('fictional design', 'fictional sample', self.directory)
        marker = json.loads((self.directory / 'remote-completion.json').read_bytes())
        self.assertEqual(marker['request']['seed'], request['seed'])
        self.assertEqual(marker['request']['description'], 'fictional design')
        self.assertEqual(marker['result'], self.result)
        self.assertTrue((self.directory / 'transfer-generated.wav.json').is_file())
        self.assertTrue((self.directory / 'transfer-speaker-vector.npy.json').is_file())

    def test_scp_failure_keeps_marker_and_resume_only_retries_scp(self):
        with ExitStack() as stack:
            self._patch_commands(stack, fail_download='generated.wav')
            with self.assertRaisesRegex(RuntimeError, 'SCP failed'):
                ui.invoke('fictional design', 'fictional sample', self.directory)
        marker = self.directory / 'remote-completion.json'
        self.assertTrue(marker.is_file())
        self.assertFalse((self.directory / 'transfer-generated.wav.json').exists())
        ssh_calls = []
        with ExitStack() as stack:
            def forbidden_ssh(*args, **kwargs):
                ssh_calls.append(1)
                raise AssertionError('resume must not run generation SSH')
            if hasattr(ui, 'release_config'):
                stack.enter_context(patch.object(ui, 'ssh_python', side_effect=forbidden_ssh))
                self._patch_scp_only(stack)
            else:
                def run(args, **kwargs):
                    if args[0] == 'ssh':
                        return forbidden_ssh(args, **kwargs)
                    return self._download(args[-2].split(':', 1)[-1], args[-1])
                stack.enter_context(patch.object(ui.subprocess, 'run', side_effect=run))
            resumed = ui.resume_voice_transfer(self.directory)
        self.assertEqual([], ssh_calls)
        self.assertEqual(self.result['stage'], resumed['stage'])
        self.assertEqual(self.result['vector_sha256'], resumed['vector_sha256'])

    def _download(self, remote, target):
        target = Path(target)
        target.write_bytes(self.wav if PurePosixName(remote) == 'generated.wav' else self.vector)
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def _patch_scp_only(self, stack):
        stack.enter_context(patch.object(ui, 'scp_from_mac', side_effect=self._download))

    def test_invoke_with_existing_marker_refuses_generation(self):
        with ExitStack() as stack:
            self._patch_commands(stack, fail_download='generated.wav')
            with self.assertRaises(RuntimeError):
                ui.invoke('fictional design', 'fictional sample', self.directory)
        with ExitStack() as stack:
            if hasattr(ui, 'release_config'):
                stack.enter_context(patch.object(ui, 'ssh_python', side_effect=AssertionError('must not generate')))
            else:
                stack.enter_context(patch.object(ui.subprocess, 'run', side_effect=AssertionError('must not generate')))
            with self.assertRaisesRegex(RuntimeError, 'resume_voice_transfer'):
                ui.invoke('fictional design', 'fictional sample', self.directory)

    def test_resume_rejects_bad_marker_schema_stage_and_result_hash(self):
        marker_path = self.directory / 'remote-completion.json'
        cases = [
            {},
            self._marker(stage='/outside/voice-experiment-x'),
            self._marker(result_hash='0' * 64),
        ]
        for marker in cases:
            with self.subTest(marker=marker.get('status')):
                marker_path.write_bytes(json.dumps(marker).encode())
                with patch_scp(self) as calls:
                    with self.assertRaises(ValueError):
                        ui.resume_voice_transfer(self.directory)
                    self.assertEqual([], calls)
                marker_path.unlink()

    def _marker(self, stage=None, result_hash=None):
        request = {'description': 'fictional design', 'text': 'fictional sample', 'seed': 7}
        result = dict(self.result)
        if stage is not None:
            result['stage'] = stage
        marker = {'version': 1, 'status': 'remote_complete', 'created_unix_ns': 1,
            'request': request, 'request_sha256': digest_bytes(ui._canonical(request)),
            'result': result, 'result_sha256': digest_bytes(ui._canonical(result))}
        if result_hash is not None:
            marker['result_sha256'] = result_hash
        return marker


def PurePosixName(path):
    return str(path).rsplit('/', 1)[-1]


class patch_scp:
    def __init__(self, test):
        self.test = test
        self.calls = []
    def __enter__(self):
        if hasattr(ui, 'release_config'):
            self.patch = patch.object(ui, 'scp_from_mac', side_effect=self.calls.append)
        else:
            self.patch = patch.object(ui.subprocess, 'run', side_effect=self.calls.append)
        self.patch.start(); return self.calls
    def __exit__(self, *args):
        self.patch.stop()


if __name__ == '__main__':
    unittest.main()
