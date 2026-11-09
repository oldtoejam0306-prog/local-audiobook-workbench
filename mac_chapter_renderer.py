"""Normal renderer adapter for the isolated single-call Mac chapter renderer."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import PurePosixPath
import wave
from pathlib import Path
from typing import Any, Callable

import release_config

_HASH = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[A-Za-z0-9_.-]{1,100}\Z')


def _remote_voice_root():
    return release_config.remote_voice_root()


def _remote_render_command():
    return release_config.remote_command('console_chapter_render_mac.py')


def _sha(raw: bytes) -> str: return hashlib.sha256(raw).hexdigest()
def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _decode_stdout(raw: str) -> dict:
    for line in reversed(raw.splitlines()):
        try:
            result = json.loads(line)
            if isinstance(result, dict): return result
        except ValueError: pass
    raise ValueError('Mac chapter adapter returned no JSON object')


class MacChapterRenderer:
    """Callable chapter renderer; one SSH request and one WAV SCP per chapter."""

    kind = 'native_huashuo_chapter_v1'

    def __init__(self, output_root: str | Path, *, runner: Callable[..., Any] = subprocess.run):
        self.output_root = Path(output_root).resolve(strict=True)
        if not self.output_root.is_dir() or self.output_root.is_symlink(): raise ValueError('output root must be a plain directory')
        self.runner = runner

    def _run(self, args: list[str], *, input_text: str | None = None, timeout: int = 30):
        result = self.runner(args, input=input_text, capture_output=True, text=True, encoding='utf-8',
                             timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode: raise RuntimeError('Mac chapter adapter command failed')
        return result

    def render(self, stage_path: str | Path, book_id: str, chapter_id: str,
               source_sha256: str, role_bindings: dict[str, dict], *, output_dir: str | Path) -> dict:
        if not isinstance(book_id, str) or not _ID.fullmatch(book_id) or not isinstance(chapter_id, str) or not _ID.fullmatch(chapter_id):
            raise ValueError('invalid book/chapter identity')
        if not isinstance(source_sha256, str) or not _HASH.fullmatch(source_sha256): raise ValueError('invalid source hash')
        stage = Path(stage_path)
        if stage.is_symlink() or not stage.is_file(): raise ValueError('stage must be a regular file')
        raw = stage.read_bytes(); document = json.loads(raw)
        chapters = document.get('chapters'); audit = document.get('audit')
        if (not isinstance(chapters, list) or len(chapters) != 1 or chapters[0].get('id') != chapter_id
                or not isinstance(audit, dict) or audit.get('source_text_sha256') != source_sha256):
            raise ValueError('source document identity mismatch')
        segments = chapters[0].get('segments')
        if not isinstance(segments, list) or not segments: raise ValueError('empty chapter')
        text = ''.join(s['text'] for s in segments)
        if _sha(text.encode('utf-8')) != source_sha256: raise ValueError('source text hash mismatch')
        voices = {}; coverage = []
        for speaker in dict.fromkeys(s['speaker_id'] for s in segments):
            binding = role_bindings.get(speaker, role_bindings.get(book_id + '\0' + speaker))
            if not isinstance(binding, dict) or not isinstance(binding.get('record'), dict): raise ValueError('fixed voice binding missing: ' + speaker)
            record = binding['record']; request = record.get('request', {})
            if binding.get('console_key') != record.get('book_role_key'): raise ValueError('fixed voice record identity mismatch')
            local_vector = Path(record.get('directory', '')) / 'speaker-vector.npy'
            vector_sha = _sha(local_vector.read_bytes())
            remote_stage = request.get('stage')
            remote_root = _remote_voice_root()
            remote_stage_path = PurePosixPath(remote_stage) if isinstance(remote_stage, str) else PurePosixPath('.')
            if (vector_sha != request.get('vector_sha256') or not isinstance(remote_stage, str)
                    or remote_stage_path.parent != remote_root
                    or not re.fullmatch(r'voice-experiment-[A-Za-z0-9_-]{1,80}', remote_stage_path.name)):
                raise ValueError('fixed voice vector or Mac stage is invalid: ' + speaker)
            voices[speaker] = {'stage': remote_stage, 'vector_sha256': vector_sha}
            coverage.append({'speaker_id': speaker, 'vector_sha256': vector_sha,
                             'characters': sum(len(s['text']) for s in segments if s['speaker_id'] == speaker)})
        request = {'action': 'render_chapter', 'book_id': book_id, 'chapter_id': chapter_id,
                   'source_sha256': source_sha256,
                   'segments': [{'speaker_id': s['speaker_id'], 'text': s['text']} for s in segments],
                   'voices': voices}
        expected_adapter_sha = _sha(Path(__file__).with_name('console_chapter_render_mac.py').read_bytes())
        output = Path(output_dir)
        if output.is_symlink(): raise ValueError('output directory symlink refused')
        output = output.resolve(strict=True)
        if not output.is_relative_to(self.output_root): raise ValueError('output directory escapes private root')
        wav = output / 'chapter.wav'; receipt_path = output / 'native-huashuo-chapter-receipt.json'
        if wav.exists() or receipt_path.exists(): raise ValueError('chapter output already exists')
        ssh = ['ssh', *release_config.ssh_options(), release_config.ssh_target(), _remote_render_command()]
        response = _decode_stdout(self._run(ssh,
            input_text=json.dumps(request, ensure_ascii=False), timeout=1900).stdout)
        voice_hashes = {k: v['vector_sha256'] for k, v in voices.items()}
        if (response.get('ok') is not True or response.get('status') != 'complete'
                or response.get('source_sha256') != source_sha256 or response.get('voice_hashes') != voice_hashes
                or response.get('text_sha256') != _sha(text.encode('utf-8'))
                or not isinstance(response.get('wav_sha256'), str) or not _HASH.fullmatch(response['wav_sha256'])):
            raise ValueError('Mac chapter response failed source/voice binding checks')
        if response.get('adapter_sha256') != expected_adapter_sha:
            raise ValueError('Mac chapter adapter source differs from reviewed local source')
        refs = response.get('voice_refs'); proof = response.get('coverage_proof_data')
        proof_sha = response.get('coverage_proof_sha256'); reads = response.get('vector_reads_data')
        reads_sha = response.get('vector_reads_sha256'); monitor = response.get('runtime_monitor')
        monitor_sha = response.get('monitor_sha256')
        if (not isinstance(refs, dict) or set(refs) != set(voices) or not isinstance(proof, dict)
                or _sha(_canonical(proof)) != proof_sha or proof.get('status') != 'verified'
                or proof.get('source_sha256') != source_sha256 or proof.get('page_text_voice_match') is not True
                or type(proof.get('nonwhitespace_characters')) is not int
                or proof['nonwhitespace_characters'] != sum(not c.isspace() for c in text)
                or type(proof.get('units')) is not int or proof['units'] < 1
                or not isinstance(reads, list) or _sha(_canonical(reads)) != reads_sha):
            raise ValueError('Mac chapter coverage/vector proof malformed')
        expected_refs = {}
        for role, ref in refs.items():
            if not isinstance(ref, str) or not ref.startswith('library:zh/'):
                raise ValueError('published voice ref invalid')
            if ref in expected_refs and expected_refs[ref] != voice_hashes[role]:
                raise ValueError('one published voice ref maps to conflicting vectors')
            expected_refs[ref] = voice_hashes[role]
        mapping = [(c, refs[s['speaker_id']]) for s in segments for c in s['text'] if not c.isspace()]
        mapping_sha = _sha(json.dumps(mapping, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if proof.get('page_text_voice_sha256') != mapping_sha:
            raise ValueError('Mac chapter source/voice sequence digest differs from source')
        if any(not isinstance(row, dict) or row.get('ref') not in expected_refs
               or row.get('sha256') != expected_refs[row['ref']] or row.get('shape') != [2048] for row in reads):
            raise ValueError('vector read proof contains unexpected vector')
        if not all(any(row.get('ref') == ref and row.get('sha256') == digest and row.get('shape') == [2048]
                       for row in reads) for ref, digest in expected_refs.items()):
            raise ValueError('vector read proof misses a mapped voice')
        if (not isinstance(monitor, dict) or monitor.get('exit_code') != 0 or monitor.get('reason') is not None
                or type(monitor.get('samples')) is not int or monitor['samples'] < 1
                or type(monitor.get('minimum_pressure_free_percent')) is not int
                or monitor['minimum_pressure_free_percent'] < 30
                or type(monitor.get('sampled_peak_rss_bytes')) is not int
                or monitor['sampled_peak_rss_bytes'] > 6 * 1024**3
                or not isinstance(monitor_sha, str) or not _HASH.fullmatch(monitor_sha)):
            raise ValueError('Mac chapter runtime monitor evidence invalid')
        run_id = response.get('run_id'); run_dir = response.get('run_dir')
        remote_root = _remote_voice_root()
        expected_run_dir = remote_root / 'chapter-runs' / run_id if isinstance(run_id, str) else None
        if (not isinstance(run_id, str) or not re.fullmatch(re.escape(book_id + '-' + chapter_id + '-run-') + r'[A-Za-z0-9_-]{1,80}', run_id)
                or not isinstance(run_dir, str) or PurePosixPath(run_dir) != expected_run_dir):
            raise ValueError('Mac chapter run identity/path invalid')
        remote_wav = response.get('wav')
        run_prefix = run_dir + '/'
        if (not isinstance(remote_wav, str) or not remote_wav.startswith(run_prefix)
                or not remote_wav.endswith('/chapter.wav') or '..' in remote_wav):
            raise ValueError('Mac chapter WAV path outside fixed run root')
        for evidence_path in (response.get('metadata_path'), response.get('coverage_proof'), response.get('vector_reads')):
            if not isinstance(evidence_path, str) or not evidence_path.startswith(run_prefix) or '..' in evidence_path:
                raise ValueError('Mac chapter evidence path outside fixed run root')
        self._run(['scp', *release_config.ssh_options(), release_config.scp_source(remote_wav), str(wav)], timeout=180)
        if not wav.is_file() or wav.is_symlink() or _sha(wav.read_bytes()) != response['wav_sha256']:
            raise ValueError('downloaded chapter WAV hash mismatch')
        with wave.open(str(wav), 'rb') as stream:
            fmt = (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype())
            frames = stream.getnframes(); decoded = 0
            while True:
                block = stream.readframes(32768)
                if not block: break
                if len(block) % 2: raise ValueError('downloaded chapter WAV has truncated PCM frames')
                decoded += len(block) // 2
            if fmt != (1, 2, 24000, 'NONE') or frames <= 0 or decoded != frames:
                raise ValueError('downloaded chapter WAV PCM is invalid')
        receipt = {'schema_version': 1, 'kind': self.kind, 'book_id': book_id, 'chapter_id': chapter_id,
            'source_document_sha256': _sha(raw), 'source_sha256': source_sha256,
            'text_sha256': _sha(text.encode('utf-8')), 'voice_hashes': voice_hashes,
            'voice_coverage': coverage, 'wav_path': str(wav), 'wav_sha256': response['wav_sha256'],
            'remote_run_id': run_id, 'metadata_path': response['metadata_path'],
            'coverage_proof_path': response['coverage_proof'], 'coverage_proof': proof,
            'coverage_proof_sha256': proof_sha, 'voice_refs': refs,
            'vector_reads_path': response['vector_reads'], 'vector_reads': reads, 'vector_reads_sha256': reads_sha,
            'runtime_monitor': monitor, 'monitor_sha256': monitor_sha, 'human_reviewed': False}
        receipt['adapter_sha256'] = expected_adapter_sha
        receipt['synthesis_statistics'] = response.get('synthesis_statistics')
        receipt_path.write_bytes(_canonical(receipt))
        return receipt

    def status(self, run_id: str) -> dict:
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,240}', run_id): raise ValueError('invalid run id')
        req = {'action': 'status', 'run_id': run_id}
        result = _decode_stdout(self._run(['ssh', *release_config.ssh_options(), release_config.ssh_target(), _remote_render_command()],
            input_text=json.dumps(req), timeout=30).stdout)
        if result.get('run_id') != run_id: raise ValueError('remote status identity mismatch')
        return result
