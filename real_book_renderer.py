"""Explicit, isolated adapter for the fixed-voice experimental renderer.

No invocation occurs on import. The operator must authenticate the mapping from
book/role IDs to fixed-console records and verify the Mac environment. Output
is private, unreviewed experimental audio; one synchronous call per segment.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
import wave
from natural_pacing import split_text, gap_seconds

import isolated_chapter_voice_bridge as bridge


def sha(raw): return hashlib.sha256(raw).hexdigest()


NATIVE_KIND = 'native_huashuo_chapter_v1'
_HASH = re.compile(r'[0-9a-f]{64}\Z')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _atomic_json(path, value):
    path = Path(path); temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(_canonical(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists(): temp.unlink()


def _file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): digest.update(block)
    return digest.hexdigest()


def _validate_native_wav(path):
    with wave.open(str(path), 'rb') as stream:
        fmt = (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype())
        frames = stream.getnframes(); decoded = 0
        while True:
            block = stream.readframes(32768)
            if not block: break
            if len(block) % 2: raise ValueError('native chapter WAV has truncated PCM frames')
            decoded += len(block) // 2
        if fmt != (1, 2, 24000, 'NONE') or frames <= 0 or decoded != frames:
            raise ValueError('native chapter WAV format or frame count invalid')


def _wav(path):
    with wave.open(str(path), 'rb') as w:
        fmt = (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getcomptype())
        if fmt != (1, 2, 24000, 'NONE') or not w.getnframes():
            raise ValueError('invalid audio format')
        data = w.readframes(w.getnframes())
        if len(data) != w.getnframes() * 2:
            raise ValueError('truncated WAV')
        return data


def prepare(stage_path, book_id, chapter_id, source_sha256, role_bindings):
    """Return document and console assignments after fully validating inputs.

    role_bindings maps speaker_id to {'console_key': '<book>\\0<role>',
    'record': fixed console record}. The explicit operator mapping is not proof
    that a model-assigned character identity is correct.
    """
    raw_stage = Path(stage_path).read_bytes()
    stage = json.loads(raw_stage)
    chapters = stage['chapters']; audit = stage['audit']
    if len(chapters) != 1 or chapters[0]['id'] != chapter_id or audit['source_text_sha256'] != source_sha256:
        raise ValueError('stage identity mismatch')
    segments = chapters[0]['segments']
    if not isinstance(segments, list) or not segments:
        raise ValueError('empty stage')
    if sha(''.join(s['text'] for s in segments).encode('utf8')) != source_sha256:
        raise ValueError('source text mismatch')
    # Coalesce adjacent same-speaker blocks, then split only at natural
    # punctuation when possible. Keep explicit audit for controlled hard cuts.
    merged = []
    for s in segments:
        if set(s) != {'speaker_id', 'text'} or not isinstance(s['speaker_id'], str) or not s['speaker_id'] or not isinstance(s['text'], str) or not s['text']:
            raise ValueError('invalid segment')
        if merged and merged[-1]['speaker_id'] == s['speaker_id']:
            merged[-1]['text'] += s['text']
        else:
            merged.append(dict(s))
    chunks = []
    pacing = []
    for s in merged:
        for piece in split_text(s['text']):
            if not piece.text.strip(): raise ValueError('whitespace-only segment requires review')
            previous = chunks[-1] if chunks else None
            frames, reason = gap_seconds(previous['text'] if previous else '', piece.text,
                previous['speaker_id'] if previous else '', s['speaker_id'])
            chunks.append({'speaker_id': s['speaker_id'], 'text': piece.text})
            pacing.append({'gap_frames': frames, 'reason': reason,
                           'boundary': piece.boundary})
    if ''.join(s['text'] for s in chunks) != ''.join(s['text'] for s in segments):
        raise ValueError('segment coverage mismatch')
    assignments = {}
    for role in {s['speaker_id'] for s in chunks}:
        spec = role_bindings[role]; record = spec['record']; key = spec['console_key']
        if not isinstance(key, str) or key.count('\0') != 1 or not all(key.split('\0')) or record.get('book_role_key') != key:
            raise ValueError('console identity mismatch')
        req = record['request']; vector = Path(record['directory']) / 'speaker-vector.npy'
        if not isinstance(req.get('stage'), str) or not req['stage'] or sha(vector.read_bytes()) != req.get('vector_sha256'):
            raise ValueError('fixed voice identity mismatch')
        # Adapt the console identity key only in memory, never change the record.
        assignments[book_id+'\0'+role] = dict(record, book_role_key=book_id+'\0'+role)
    return {'book': book_id, 'chapters': [{'id': chapter_id, 'segments': chunks}],
            'pacing': pacing, '_source_stage_sha256': sha(raw_stage), '_source_audit': audit}, assignments


def _native_normalized_stage(document):
    return {'chapters': document['chapters'], 'audit': document['_source_audit'],
            'original_stage_sha256': document['_source_stage_sha256']}


def _verify_native(root, manifest, document, assignments, source_sha256):
    root = Path(root).resolve(strict=True)
    if (manifest.get('renderer_kind') != NATIVE_KIND or manifest.get('status') != 'complete'
            or manifest.get('book') != document['book'] or len(manifest.get('chapters', [])) != 1):
        raise ValueError('native chapter manifest identity invalid')
    chapter = document['chapters'][0]; entry = manifest['chapters'][0]
    if (entry.get('renderer_kind') != NATIVE_KIND or entry.get('status') != 'complete'
            or entry.get('id') != chapter['id'] or entry.get('segments') != []):
        raise ValueError('native chapter manifest must describe one final WAV without per-segment receipts')
    segments = chapter.get('segments')
    if not isinstance(segments, list) or not segments:
        raise ValueError('native source segments missing')
    text = ''.join(item['text'] for item in segments)
    if sha(text.encode('utf-8')) != source_sha256 or manifest.get('source_sha256') != source_sha256:
        raise ValueError('native chapter source coverage mismatch')
    if (entry.get('source_sha256') != source_sha256 or entry.get('text_sha256') != sha(text.encode('utf-8'))
            or type(entry.get('source_segment_count')) is not int or entry['source_segment_count'] != len(segments)):
        raise ValueError('native manifest source coverage fields mismatch')
    if document.get('_source_audit', {}).get('source_text_sha256') != source_sha256:
        raise ValueError('native source audit mismatch')
    normalized = _native_normalized_stage(document)
    normalized_raw = _canonical(normalized); normalized_sha = sha(normalized_raw)
    if (manifest.get('normalized_stage_sha256') != normalized_sha
            or manifest.get('original_stage_sha256') != document.get('_source_stage_sha256')):
        raise ValueError('native normalized/original stage digest mismatch')
    normalized_path = root / 'normalized-stage.json'
    if normalized_path.is_symlink() or not normalized_path.is_file() or normalized_path.read_bytes() != normalized_raw:
        raise ValueError('native normalized stage file mismatch')

    roles = list(dict.fromkeys(item['speaker_id'] for item in segments))
    expected_hashes = {}; expected_coverage = []
    for role in roles:
        if not isinstance(role, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', role):
            raise ValueError('native speaker ID is invalid for the fixed chapter protocol')
        voice = assignments.get(document['book'] + '\0' + role)
        if not isinstance(voice, dict) or voice.get('book_role_key') != document['book'] + '\0' + role:
            raise ValueError('native fixed voice assignment missing or misbound')
        vector = Path(voice.get('directory', '')) / 'speaker-vector.npy'
        digest = voice.get('request', {}).get('vector_sha256')
        if not isinstance(digest, str) or not _HASH.fullmatch(digest) or not vector.is_file() or _file_sha(vector) != digest:
            raise ValueError('native fixed voice vector changed')
        expected_hashes[role] = digest
        expected_coverage.append({'speaker_id': role, 'vector_sha256': digest,
                                  'characters': sum(len(s['text']) for s in segments if s['speaker_id'] == role)})

    receipt_path = root / 'native-render-receipt.json'
    if (manifest.get('native_receipt_path') != str(receipt_path)
            or receipt_path.is_symlink() or not receipt_path.is_file()): raise ValueError('native renderer receipt missing')
    receipt_raw = receipt_path.read_bytes()
    if sha(receipt_raw) != manifest.get('native_receipt_sha256'): raise ValueError('native renderer receipt hash mismatch')
    receipt = json.loads(receipt_raw)
    if (receipt.get('kind') != NATIVE_KIND or receipt.get('human_reviewed') is not False
            or receipt.get('book_id') != document['book'] or receipt.get('chapter_id') != chapter['id']
            or receipt.get('source_sha256') != source_sha256
            or receipt.get('source_document_sha256') != normalized_sha
            or receipt.get('text_sha256') != sha(text.encode('utf-8'))
            or receipt.get('voice_hashes') != expected_hashes
            or receipt.get('voice_coverage') != expected_coverage):
        raise ValueError('native renderer receipt source/voice identity mismatch')

    refs = receipt.get('voice_refs')
    if not isinstance(refs, dict) or set(refs) != set(roles) or any(
            not isinstance(refs[r], str) or not refs[r].startswith('library:zh/') for r in roles):
        raise ValueError('native voice refs do not cover source roles')
    expected_refs = {}
    for role, ref in refs.items():
        if ref in expected_refs and expected_refs[ref] != expected_hashes[role]:
            raise ValueError('native voice ref maps to conflicting fixed vectors')
        expected_refs[ref] = expected_hashes[role]
    mapping = [(character, refs[segment['speaker_id']]) for segment in segments
               for character in segment['text'] if not character.isspace()]
    mapping_sha = sha(json.dumps(mapping, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    proof = receipt.get('coverage_proof'); proof_sha = receipt.get('coverage_proof_sha256')
    if (not isinstance(proof, dict) or sha(_canonical(proof)) != proof_sha
            or proof.get('status') != 'verified' or proof.get('source_sha256') != source_sha256
            or proof.get('page_text_voice_match') is not True
            or proof.get('page_text_voice_sha256') != mapping_sha
            or type(proof.get('nonwhitespace_characters')) is not int
            or proof['nonwhitespace_characters'] != len(mapping)
            or type(proof.get('units')) is not int or proof['units'] < 1):
        raise ValueError('native planned page_text/voice coverage proof invalid')

    vector_reads = receipt.get('vector_reads'); vector_reads_sha = receipt.get('vector_reads_sha256')
    if not isinstance(vector_reads, list) or sha(_canonical(vector_reads)) != vector_reads_sha:
        raise ValueError('native vector-read proof digest invalid')
    if any(not isinstance(row, dict) or row.get('ref') not in expected_refs
           or row.get('sha256') != expected_refs[row['ref']] or row.get('shape') != [2048]
           for row in vector_reads):
        raise ValueError('native vector-read proof contains unknown vector')
    if not all(any(row.get('ref') == ref and row.get('sha256') == digest and row.get('shape') == [2048]
                   for row in vector_reads) for ref, digest in expected_refs.items()):
        raise ValueError('native vector-read proof misses a source voice')

    monitor = receipt.get('runtime_monitor'); monitor_sha = receipt.get('monitor_sha256')
    if (not isinstance(monitor, dict) or type(monitor.get('exit_code')) is not int or monitor['exit_code'] != 0
            or monitor.get('reason') is not None
            or type(monitor.get('samples')) is not int or monitor['samples'] < 1
            or type(monitor.get('minimum_pressure_free_percent')) is not int
            or monitor['minimum_pressure_free_percent'] < 30
            or type(monitor.get('sampled_peak_rss_bytes')) is not int
            or monitor['sampled_peak_rss_bytes'] > 6 * 1024**3
            or not isinstance(monitor_sha, str) or not _HASH.fullmatch(monitor_sha)
            or not isinstance(receipt.get('adapter_sha256'), str) or not _HASH.fullmatch(receipt['adapter_sha256'])):
        raise ValueError('native runtime monitor or adapter pin invalid')
    reviewed_adapter = root / 'adapter-source.py'
    if not reviewed_adapter.is_file() or reviewed_adapter.is_symlink() or _file_sha(reviewed_adapter) != receipt['adapter_sha256']:
        raise ValueError('native Mac adapter differs from locally reviewed source')

    audio_path = Path(entry.get('path', ''))
    if audio_path.is_symlink(): raise ValueError('native chapter WAV symlink refused')
    audio_path = audio_path.resolve(strict=True)
    if audio_path.parent != root or audio_path.name != 'chapter.wav': raise ValueError('native chapter WAV escaped run root')
    if (receipt.get('wav_path') != str(audio_path) or receipt.get('wav_sha256') != entry.get('sha256')
            or _file_sha(audio_path) != entry.get('sha256') or audio_path.stat().st_size != entry.get('bytes')):
        raise ValueError('native chapter WAV hash or receipt mismatch')
    _validate_native_wav(audio_path)
    for key in ('native_receipt_sha256', 'normalized_stage_sha256', 'original_stage_sha256'):
        if not isinstance(manifest.get(key), str) or not _HASH.fullmatch(manifest[key]): raise ValueError('native manifest digest invalid')
    return {'manifest_path': str(root/'manifest.json'), 'manifest_sha256': sha((root/'manifest.json').read_bytes()),
            'chapter_wav': str(audio_path), 'wav_sha256': entry['sha256'], 'source_sha256': source_sha256,
            'renderer_kind': NATIVE_KIND, 'native_receipt_path': str(receipt_path),
            'native_receipt_sha256': manifest['native_receipt_sha256'], 'human_reviewed': False}


def verify(root, document, assignments, source_sha256):
    root = Path(root).resolve(); manifest = json.loads((root/'manifest.json').read_bytes())
    if manifest.get('renderer_kind') == NATIVE_KIND:
        return _verify_native(root, manifest, document, assignments, source_sha256)
    if manifest.get('status') != 'complete' or manifest.get('book') != document['book'] or len(manifest.get('chapters', [])) != 1:
        raise ValueError('incomplete manifest')
    chapter = document['chapters'][0]; entry = manifest['chapters'][0]
    if entry.get('status') != 'complete' or entry.get('id') != chapter['id'] or len(entry.get('segments', [])) != len(chapter['segments']):
        raise ValueError('manifest identity mismatch')
    combined = bytearray()
    pacing = document.get('pacing', [{'gap_frames': 0}] * len(chapter['segments']))
    if len(pacing) != len(chapter['segments']): raise ValueError('pacing manifest mismatch')
    for i, (seg, actual) in enumerate(zip(chapter['segments'], entry['segments'])):
        voice = assignments[document['book']+'\0'+seg['speaker_id']]
        if sha((Path(voice['directory'])/'speaker-vector.npy').read_bytes()) != voice['request']['vector_sha256']:
            raise ValueError('fixed voice changed during render')
        if (actual.get('speaker_id') != seg['speaker_id'] or actual.get('text_sha256') != sha(seg['text'].encode('utf8'))
                or actual.get('vector_sha256') != voice['request']['vector_sha256']
                or actual.get('gap_frames', 0) != pacing[i]['gap_frames']):
            raise ValueError('segment manifest mismatch')
        p = root/'chapter-0000'/('segment-%04d' % i)/'rendered.wav'
        if sha(p.read_bytes()) != actual.get('wav_sha256'): raise ValueError('segment WAV mismatch')
        if pacing[i]['gap_frames']:
            combined.extend(b'\0\0' * pacing[i]['gap_frames'])
        combined.extend(_wav(p))
    p = root/'chapter-0000'/'chapter.wav'
    if Path(entry['path']).resolve() != p or sha(p.read_bytes()) != entry.get('sha256') or p.stat().st_size != entry.get('bytes') or _wav(p) != combined:
        raise ValueError('chapter WAV mismatch')
    if sha(''.join(s['text'] for s in chapter['segments']).encode('utf8')) != source_sha256:
        raise ValueError('source coverage mismatch')
    return {'manifest_path': str(root/'manifest.json'), 'manifest_sha256': sha((root/'manifest.json').read_bytes()),
            'chapter_wav': str(p), 'wav_sha256': sha(p.read_bytes()), 'source_sha256': source_sha256,
            'human_reviewed': False}


def _render_native(stage_path, document, assignments, book_id, chapter_id, source_sha256,
                   role_bindings, output_parent, native_renderer):
    output_parent = Path(output_parent)
    output_parent.mkdir(parents=True, exist_ok=True)
    if output_parent.is_symlink(): raise ValueError('native output parent symlink refused')
    output_parent = output_parent.resolve(strict=True)
    root = Path(tempfile.mkdtemp(prefix='native-chapter-', dir=output_parent)).resolve(strict=True)
    normalized = _native_normalized_stage(document)
    normalized_path = root / 'normalized-stage.json'; normalized_raw = _canonical(normalized)
    normalized_path.write_bytes(normalized_raw)
    adapter_source = Path(__file__).with_name('console_chapter_render_mac.py')
    (root / 'adapter-source.py').write_bytes(adapter_source.read_bytes())
    role_map = {role: {'console_key': assignments[book_id+'\0'+role]['book_role_key'],
                       'record': assignments[book_id+'\0'+role]} for role in dict.fromkeys(
                           s['speaker_id'] for s in document['chapters'][0]['segments'])}
    if native_renderer is None:
        from mac_chapter_renderer import MacChapterRenderer
        native_renderer = MacChapterRenderer(output_parent)
    call = getattr(native_renderer, 'render', native_renderer)
    native_receipt = call(normalized_path, book_id, chapter_id, source_sha256, role_map, output_dir=root)
    if not isinstance(native_receipt, dict) or native_receipt.get('kind') != NATIVE_KIND:
        raise ValueError('native renderer did not return the required chapter receipt kind')
    native_receipt_path = root / 'native-render-receipt.json'; _atomic_json(native_receipt_path, native_receipt)
    receipt_sha = sha(native_receipt_path.read_bytes())
    wav_path = Path(native_receipt.get('wav_path', ''))
    if wav_path.is_symlink(): raise ValueError('native WAV symlink refused')
    wav_path = wav_path.resolve(strict=True)
    if wav_path.parent != root.resolve() or wav_path.name != 'chapter.wav': raise ValueError('native renderer WAV escaped chapter output root')
    wav_digest = _file_sha(wav_path); wav_size = wav_path.stat().st_size
    manifest = {'version': 1, 'renderer_kind': NATIVE_KIND, 'book': book_id, 'status': 'complete',
        'source_sha256': source_sha256, 'original_stage_sha256': document['_source_stage_sha256'],
        'normalized_stage_sha256': sha(normalized_raw), 'native_receipt_path': str(native_receipt_path),
        'native_receipt_sha256': receipt_sha, 'chapters': [{'id': chapter_id, 'status': 'complete',
            'renderer_kind': NATIVE_KIND, 'segments': [], 'source_segment_count': len(document['chapters'][0]['segments']),
            'source_sha256': source_sha256, 'text_sha256': sha(''.join(s['text'] for s in document['chapters'][0]['segments']).encode('utf-8')),
            'path': str(wav_path), 'sha256': wav_digest, 'bytes': wav_size}]}
    manifest_path = root / 'manifest.json'; _atomic_json(manifest_path, manifest)
    try: return verify(root, document, assignments, source_sha256)
    except BaseException:
        manifest_path.unlink(missing_ok=True)
        raise


def render(stage_path, book_id, chapter_id, source_sha256, role_bindings, output_parent, *,
           render_segment=None, native_renderer=None):
    document, assignments = prepare(stage_path, book_id, chapter_id, source_sha256, role_bindings)
    if render_segment is None:
        return _render_native(stage_path, document, assignments, book_id, chapter_id,
                              source_sha256, role_bindings, output_parent, native_renderer)
    bridge_document = {key: value for key, value in document.items() if key in ('book', 'chapters', 'pacing')}
    root, _ = bridge.render_document(bridge_document, assignments, output_parent, render_segment=render_segment)
    return verify(root, document, assignments, source_sha256)
