"""Private Mac stdio adapter for one multi-voice Huashuo chapter.

Actions are render_chapter and read-only status. This module is not a server.
It never calls a language model or ASR; Huashuo synth is invoked with --no-asr.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import release_config
import native_unit_cache

CODE_DIR = Path(__file__).resolve().parent
BASE = None
ROOT = None
TEMPLATE = CODE_DIR
ORCH = CODE_DIR
PYTHON = Path(sys.executable)
FFMPEG = None
MAX_CHARS = 18000
HASH = re.compile(r'[0-9a-f]{64}\Z')
SAFE = re.compile(r'[A-Za-z0-9_.-]{1,100}\Z')
INPUT_PREFIX = " text='\u6d4b\u8bd5\u7ae0\u8282"


def _sha(raw: bytes) -> str: return hashlib.sha256(raw).hexdigest()
def _canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _configure_runtime():
    global BASE, ROOT, TEMPLATE, ORCH, PYTHON, FFMPEG
    release_config.apply_mac_environment()
    BASE = release_config.mac_stage_root().expanduser()
    ROOT = release_config.mac_voice_root().expanduser()
    TEMPLATE = CODE_DIR
    ORCH = CODE_DIR
    PYTHON = Path(os.path.abspath(sys.executable))
    FFMPEG = release_config.mac_ffmpeg()


def _atomic(path: Path, obj):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(_canonical(obj)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists(): temp.unlink()


def validate_request(req: dict) -> dict:
    required = {'action', 'book_id', 'chapter_id', 'source_sha256', 'segments', 'voices'}
    if not isinstance(req, dict) or set(req) != required or req['action'] != 'render_chapter':
        raise ValueError('invalid render_chapter request')
    if any(not isinstance(req[k], str) or not SAFE.fullmatch(req[k]) for k in ('book_id', 'chapter_id')):
        raise ValueError('invalid book/chapter identifier')
    if not isinstance(req['source_sha256'], str) or not HASH.fullmatch(req['source_sha256']):
        raise ValueError('invalid source SHA-256')
    segments = req['segments']
    if not isinstance(segments, list) or not segments: raise ValueError('segments required')
    text_parts = []
    roles = set()
    for seg in segments:
        if not isinstance(seg, dict) or set(seg) != {'speaker_id', 'text'}:
            raise ValueError('invalid source segment')
        speaker, text = seg['speaker_id'], seg['text']
        if (not isinstance(speaker, str) or not SAFE.fullmatch(speaker) or not isinstance(text, str)
                or not text.strip() or any(not c.isprintable() and c not in '\n\r\t' for c in text)):
            raise ValueError('invalid source segment text/speaker')
        text_parts.append(text); roles.add(speaker)
    text = ''.join(text_parts)
    if not text.strip() or len(text) > MAX_CHARS: raise ValueError('chapter must contain 1–18000 codepoints')
    if _sha(text.encode('utf-8')) != req['source_sha256']: raise ValueError('source text hash mismatch')
    if not isinstance(req['voices'], dict) or set(req['voices']) != roles:
        raise ValueError('voice map must exactly cover source speakers')
    for speaker, voice in req['voices'].items():
        if (not isinstance(voice, dict) or set(voice) != {'stage', 'vector_sha256'}
                or not isinstance(voice['stage'], str) or not voice['stage'].startswith('/')
                or not isinstance(voice['vector_sha256'], str) or not HASH.fullmatch(voice['vector_sha256'])):
            raise ValueError('invalid voice binding for ' + speaker)
    return {'text': text, 'roles': roles}


def _voice_assets(req):
    resolved = {}
    for speaker, spec in req['voices'].items():
        stage_arg = Path(spec['stage']).expanduser()
        if stage_arg.is_symlink(): raise ValueError('voice stage symlink refused')
        stage = stage_arg.resolve(strict=True)
        voice_root = ROOT.resolve(strict=True)
        if (not stage.is_relative_to(voice_root) or stage.parent != voice_root
                or not stage.name.startswith('voice-experiment-') or not stage.is_dir()):
            raise ValueError('voice stage outside pinned voice experiment root')
        vector = stage / 'speaker-vector.npy'
        if vector.is_symlink() or not vector.is_file() or _sha(vector.read_bytes()) != spec['vector_sha256']:
            raise ValueError('voice vector hash mismatch: ' + speaker)
        resolved[speaker] = {'stage': str(stage), 'vector_sha256': spec['vector_sha256']}
    return resolved


def _stop_group(proc):
    try: os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError: proc.wait(); return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        proc.poll()
        try: os.killpg(proc.pid, 0)
        except ProcessLookupError: break
        time.sleep(.1)
    else:
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    proc.wait()


def _resource_slot():
    """Reuse the shared voice/render lock, headroom gate, and caffeinate lifecycle."""
    helper_dir = CODE_DIR
    if str(helper_dir) not in sys.path: sys.path.insert(0, str(helper_dir))
    from mac_render_resources import render_slot
    return render_slot(ROOT)


def _prepare_run(req, resolved, text):
    runs = ROOT / 'chapter-runs'; runs.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=req['book_id'] + '-' + req['chapter_id'] + '-run-', dir=runs))
    (run / 'request.json').write_bytes(_canonical(req))
    (run / 'stderr.log').write_text('', encoding='utf-8')
    cache_components = ('console_chapter_render_mac.py', 'native_unit_cache.py',
        'chapter-render-driver.py', 'chapter-monitor.py', 'chapter_model_cache.py',
        'mac_render_resources.py', 'isolated_huashuo_library.py', 'release_config.py')
    pins = {name: _sha((CODE_DIR / name).read_bytes()) for name in cache_components}
    _atomic(run / 'cache-settings.json', {'root': str(ROOT / 'unit-cache'),
        'adapter_sha256': _sha(_canonical(pins)), 'components': pins})
    _atomic(run / 'journal.json', {'schema_version': 1, 'run_id': run.name, 'status': 'started',
        'book_id': req['book_id'], 'chapter_id': req['chapter_id'], 'source_sha256': req['source_sha256'],
        'voices': resolved, 'resume_supported': False})
    return run


def _publish_voices(req, resolved, run):
    sys.path[:0] = [str(ORCH), str(TEMPLATE)]
    import experimental_isolated_voice_library as library
    preset_source = release_config.mac_presets_path().expanduser().resolve(strict=True)
    preset_sha = _sha(preset_source.read_bytes())
    snapshot = {'version': 1, 'book_id': 'isolated-book', 'locked': {}}
    snapshot_sha = _sha(_canonical(snapshot))
    published_by_vector = {}
    for i, (speaker, voice) in enumerate(resolved.items()):
        vector_sha = voice['vector_sha256']
        if vector_sha in published_by_vector: continue
        result = library.publish_experiment(task_dir=Path(voice['stage']), experiment_parent=BASE,
            book_id='isolated-book', character_id='chapter-role-' + str(i), locked_snapshot=snapshot,
            locked_snapshot_sha256=snapshot_sha, trusted_presets_path=preset_source,
            trusted_presets_sha256=preset_sha)
        if result['metadata']['vector_sha256'] != vector_sha: raise ValueError('published vector mismatch')
        published_by_vector[vector_sha] = result
    voice_map = {speaker: 'library:zh/fixed_' + v['vector_sha256'] for speaker, v in resolved.items()}
    merged = run / 'voice-library'; merged.mkdir()
    first = next(iter(published_by_vector.values()))
    shutil.copyfile(Path(first['library_dir']) / 'presets.json', merged / 'presets.json')
    (merged / 'zh').mkdir()
    for vector_sha, result in published_by_vector.items():
        source = Path(result['library_dir']) / 'zh'
        original = result['voice_ref'].split('/')[-1]
        stable_name = 'fixed_' + vector_sha
        shutil.copyfile(source / (original + '.npy'), merged / 'zh' / (stable_name + '.npy'))
        metadata = json.loads((source / (original + '.json')).read_bytes())
        metadata['id'] = stable_name
        (merged / 'zh' / (stable_name + '.json')).write_bytes(_canonical(metadata))
    data = {'library_dir': str(merged), 'voice_refs': voice_map,
            'vectors': {s: v['vector_sha256'] for s, v in resolved.items()},
            'publications': list(published_by_vector.values())}
    (run / 'publish-result.json').write_bytes(_canonical(data))
    return data


def _build_driver(run, request, text):
    """Stage the checked portable chapter driver and its private one-shot helpers."""
    driver = CODE_DIR / 'chapter-render-driver.py'
    monitor = CODE_DIR / 'chapter-monitor.py'
    required = (driver, monitor, CODE_DIR / 'isolated_huashuo_library.py',
                CODE_DIR / 'release_config.py', CODE_DIR / 'native_unit_cache.py',
                CODE_DIR / 'mac_render_resources.py', CODE_DIR / 'chapter_model_cache.py')
    if not all(path.is_file() and not path.is_symlink() for path in required):
        raise RuntimeError('portable chapter driver closure is incomplete')
    shutil.copyfile(driver, run / 'render-driver.py')
    shutil.copyfile(monitor, run / 'monitor.py')
    monitor_sha = _sha((run / 'monitor.py').read_bytes())
    title_marker = "if command in ('synth','package'):args.extend(['--no-opening','--no-closing'])"
    wrapper = (CODE_DIR / 'isolated_huashuo_library.py').read_text(encoding='utf-8')
    if wrapper.count(title_marker) != 1: raise RuntimeError('wrapper no-titles marker changed')
    wrapper = wrapper.replace(title_marker, "if command in ('synth','package'):args.extend(['--no-opening','--no-closing','--no-titles'])")
    (run / 'isolated_huashuo_library.py').write_text(wrapper, encoding='utf-8')
    for name in ('release_config.py', 'native_unit_cache.py', 'mac_render_resources.py', 'chapter_model_cache.py'):
        shutil.copyfile(CODE_DIR / name, run / name)
    return monitor_sha


def _save_finished_units(run):
    binding_path = run / 'cache-binding.json'
    if not binding_path.is_file():
        return
    binding = json.loads(binding_path.read_bytes())
    stage = Path(binding['stage']).resolve(strict=True)
    if stage.parent != run.resolve() or not stage.name.startswith('huashuo-isolated-'):
        raise ValueError('cache stage outside render run')
    result = native_unit_cache.publish_units(ROOT / 'unit-cache', stage / 'work/cache/units',
        model_sha256=binding['model_sha256'], adapter_sha256=binding['adapter_sha256'])
    _atomic(run / 'cache-checkpoint.json', result)


def render(req):
    checked = validate_request(req); resolved = _voice_assets(req)
    with _resource_slot():
        run = _prepare_run(req, resolved, checked['text'])
        try:
            published = _publish_voices(req, resolved, run)
            monitor_sha = _build_driver(run, req, checked['text'])
            proc = subprocess.Popen([str(PYTHON), str(run / 'monitor.py')], cwd=run,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            try: stdout, stderr = proc.communicate(timeout=1800)
            except subprocess.TimeoutExpired as timeout_error:
                _stop_group(proc)
                out_partial = timeout_error.stdout or b'chapter render timed out'
                err_partial = timeout_error.stderr or b'chapter render timed out'
                if isinstance(out_partial, bytes): out_partial = out_partial.decode('utf-8', errors='replace')
                if isinstance(err_partial, bytes): err_partial = err_partial.decode('utf-8', errors='replace')
                (run / 'stdout.log').write_text(out_partial, encoding='utf-8')
                (run / 'stderr.log').write_text(err_partial, encoding='utf-8')
                raise TimeoutError('chapter render timed out')
            finally:
                _stop_group(proc)
                _save_finished_units(run)
            (run / 'stdout.log').write_text(stdout, encoding='utf-8')
            (run / 'stderr.log').write_text(stderr, encoding='utf-8')
            if proc.returncode: raise RuntimeError('Huashuo render process failed; see stderr.log')
            result = json.loads((run / 'render-result.json').read_text(encoding='utf-8'))
            monitor_path = run / 'monitor-result.json'
            monitor = json.loads(monitor_path.read_text(encoding='utf-8'))
            if (monitor.get('exit_code') != 0 or monitor.get('reason') is not None
                    or type(monitor.get('samples')) is not int or monitor['samples'] < 1
                    or type(monitor.get('minimum_pressure_free_percent')) is not int
                    or monitor['minimum_pressure_free_percent'] < 30
                    or type(monitor.get('sampled_peak_rss_bytes')) is not int
                    or monitor['sampled_peak_rss_bytes'] > 6 * 1024**3
                    or _sha((run / 'monitor.py').read_bytes()) != monitor_sha):
                raise ValueError('chapter monitor did not meet existing 30%/6GiB runtime limits')
            vector_reads = result.get('vector_reads')
            expected_refs = {ref: published['vectors'][speaker] for speaker, ref in published['voice_refs'].items()}
            if (not isinstance(vector_reads, list) or not all(any(row.get('ref') == ref
                    and row.get('sha256') == digest and row.get('shape') == [2048] for row in vector_reads)
                    for ref, digest in expected_refs.items())):
                raise ValueError('published voice vector read proof incomplete')
            vector_reads_path = run / 'vector-reads.json'; vector_reads_path.write_bytes(_canonical(vector_reads))
            mp3 = Path(result['stage']) / 'synthetic-result.mp3'
            if not mp3.is_file() or _sha(mp3.read_bytes()) != result.get('audio_sha256'):
                raise ValueError('packaged chapter audio hash mismatch')
            proof_path = run / 'coverage-proof.json'
            proof = json.loads(proof_path.read_text(encoding='utf-8'))
            if proof.get('status') != 'verified' or proof.get('source_sha256') != req['source_sha256']:
                raise ValueError('chapter source/voice coverage proof missing')
            wav = run / 'chapter.wav'
            subprocess.run([str(FFMPEG), '-v', 'error', '-xerror', '-err_detect', 'explode',
                '-i', str(mp3), '-ac', '1', '-ar', '24000', '-c:a', 'pcm_s16le', str(wav)], check=True,
                timeout=180, capture_output=True)
            audio_sha = _sha(wav.read_bytes())
            out = {'ok': True, 'run_id': run.name, 'run_dir': str(run), 'wav': str(wav),
                'wav_sha256': audio_sha, 'source_sha256': req['source_sha256'],
                'text_sha256': req['source_sha256'],
                'voice_hashes': {k: v['vector_sha256'] for k, v in resolved.items()},
                'status': 'complete', 'metadata_path': str(run / 'render-result.json'),
                'coverage_proof': str(proof_path), 'vector_reads': str(vector_reads_path),
                'coverage_proof_data': proof, 'coverage_proof_sha256': _sha(proof_path.read_bytes()),
                'vector_reads_data': vector_reads, 'vector_reads_sha256': _sha(vector_reads_path.read_bytes()),
                'voice_refs': published['voice_refs'], 'runtime_monitor': monitor,
                'monitor_sha256': monitor_sha,
                'synthesis_statistics': json.loads((run / 'synthesis-statistics.json').read_bytes()),
                'adapter_sha256': _sha(Path(__file__).read_bytes()), 'human_reviewed': False}
            _atomic(run / 'response.json', out)
            _atomic(run / 'journal.json', {'schema_version': 1, 'run_id': run.name, 'status': 'complete',
                'book_id': req['book_id'], 'chapter_id': req['chapter_id'], 'source_sha256': req['source_sha256'],
                'voice_hashes': out['voice_hashes'], 'wav_sha256': audio_sha, 'coverage_proof': str(proof_path),
                'monitor_sha256': monitor_sha, 'resume_supported': False})
            return out
        except BaseException as exc:
            _atomic(run / 'journal.json', {'schema_version': 1, 'run_id': run.name, 'status': 'failed_or_unknown',
                'book_id': req['book_id'], 'chapter_id': req['chapter_id'], 'source_sha256': req['source_sha256'],
                'error_type': type(exc).__name__, 'stderr_path': str(run / 'stderr.log'), 'resume_supported': False})
            raise


def status(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,240}', run_id): raise ValueError('invalid run id')
    parent = (ROOT / 'chapter-runs').resolve(strict=True)
    run = (parent / run_id).resolve(strict=True)
    if run.parent != parent or run.is_symlink(): raise ValueError('run path outside private root')
    journal = json.loads((run / 'journal.json').read_bytes())
    response = run / 'response.json'
    return json.loads(response.read_bytes()) if response.exists() else {'ok': True, 'run_id': run_id, 'journal': journal}


def main():
    _configure_runtime()
    req = json.load(sys.stdin)
    if isinstance(req, dict) and req.get('action') == 'status': result = status(req.get('run_id'))
    else: result = render(req)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    def _terminate(signum, frame): raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, _terminate)
    try: main()
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False)); sys.exit(1)
