"""Isolated chapter renderer using already-fixed fictional voices.

Input is explicit segment attribution, not a claim of speaker correctness. No
book, voice assignment, or existing output is modified. No network model API.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
import wave


def digest(data):
    return hashlib.sha256(data).hexdigest()


def render_document(document, assignments, output_parent, render_segment=None):
    if set(document) - {'book', 'chapters', 'pacing'} or not {'book', 'chapters'} <= set(document) or not isinstance(document['book'], str) or not document['book'].strip():
        raise ValueError('book and chapters required')
    chapters = document['chapters']
    if not isinstance(chapters, list) or not chapters:
        raise ValueError('chapters required')
    if render_segment is None:
        from console_voice_ui import render_fixed
        render_segment = render_fixed
    seen = set()
    pacing = document.get('pacing')
    total_segments = sum(len(c.get('segments', [])) for c in chapters if isinstance(c, dict))
    if pacing is None:
        pacing = [{'gap_frames': 0, 'reason': 'unspecified', 'boundary': 'legacy'} for _ in range(total_segments)]
    if not isinstance(pacing, list) or len(pacing) != total_segments:
        raise ValueError('pacing must match the complete segment list')
    if any(not isinstance(p, dict) or not isinstance(p.get('gap_frames'), int) or p['gap_frames'] < 0
           or p['gap_frames'] > 24000 or not isinstance(p.get('reason'), str)
           or not isinstance(p.get('boundary'), str) for p in pacing):
        raise ValueError('invalid pacing entry')
    # Validate all bindings before starting a costly render.
    pacing_cursor = 0
    for chapter in chapters:
        if set(chapter) != {'id', 'segments'} or not isinstance(chapter['id'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', chapter['id']) or chapter['id'] in seen:
            raise ValueError('invalid or duplicate chapter id')
        seen.add(chapter['id'])
        if not isinstance(chapter['segments'], list) or not chapter['segments']:
            raise ValueError('empty chapter')
        if pacing[pacing_cursor]['gap_frames'] != 0:
            raise ValueError('first segment of each chapter cannot have a leading gap')
        for segment in chapter['segments']:
            if set(segment) != {'speaker_id', 'text'} or not isinstance(segment['speaker_id'], str) or not isinstance(segment['text'], str) or not segment['text'].strip() or len(segment['text']) > 500:
                raise ValueError('invalid segment')
            voice = assignments.get(document['book'] + '\0' + segment['speaker_id'])
            if not isinstance(voice, dict) or voice.get('book_role_key') != document['book'] + '\0' + segment['speaker_id']:
                raise ValueError('missing fixed voice: ' + segment['speaker_id'])
            req = voice.get('request', {})
            vector = Path(voice.get('directory', '')) / 'speaker-vector.npy'
            if not re.fullmatch(r'[0-9a-f]{64}', str(req.get('vector_sha256', ''))) or not vector.is_file() or digest(vector.read_bytes()) != req['vector_sha256']:
                raise ValueError('fixed voice vector missing or changed: ' + segment['speaker_id'])
            pacing_cursor += 1
    root = Path(output_parent) / ('chapter-render-' + uuid.uuid4().hex)
    root.mkdir(parents=True, exist_ok=False)
    manifest = {'version': 1, 'book': document['book'], 'status': 'running', 'chapters': [], 'note': 'Attribution is supplied by the caller and is not verified.'}
    def save():
        tmp = root / 'manifest.tmp'
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, root / 'manifest.json')
    save()
    try:
        pacing_index = 0
        for ci, chapter in enumerate(chapters):
            chapter_dir = root / ('chapter-%04d' % ci)
            chapter_dir.mkdir()
            entry = {'id': chapter['id'], 'status': 'running', 'segments': []}
            manifest['chapters'].append(entry); save()
            audio_format = (1, 2, 24000, 'NONE')
            output = chapter_dir / 'chapter.wav'
            with wave.open(str(output), 'wb') as out:
                # A failed first segment must not mask its error while wave closes.
                out.setnchannels(audio_format[0]); out.setsampwidth(audio_format[1]); out.setframerate(audio_format[2])
                for si, segment in enumerate(chapter['segments']):
                    folder = chapter_dir / ('segment-%04d' % si); folder.mkdir()
                    gap = pacing[pacing_index]
                    if gap['gap_frames']:
                        out.writeframesraw(b'\0' * (gap['gap_frames'] * audio_format[0] * audio_format[1]))
                    voice = assignments[document['book'] + '\0' + segment['speaker_id']]
                    path = Path(render_segment(voice, segment['text'], folder))
                    with wave.open(str(path), 'rb') as wav:
                        fmt = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype())
                        if fmt[3] != 'NONE' or not wav.getnframes(): raise ValueError('invalid WAV')
                        if fmt != audio_format: raise ValueError('segment WAV formats differ')
                        frames = wav.readframes(wav.getnframes())
                        if len(frames) != wav.getnframes() * fmt[0] * fmt[1]: raise ValueError('truncated WAV')
                        out.writeframes(frames)
                    entry['segments'].append({'speaker_id': segment['speaker_id'], 'text_sha256': digest(segment['text'].encode('utf-8')), 'vector_sha256': voice['request']['vector_sha256'], 'wav_sha256': digest(path.read_bytes()), 'gap_frames': gap['gap_frames'], 'gap_reason': gap['reason'], 'boundary': gap['boundary']})
                    pacing_index += 1
                    save()
            entry.update(status='complete', path=str(output.resolve()), sha256=digest(output.read_bytes()), bytes=output.stat().st_size)
            save()
        manifest['status'] = 'complete'; save()
    except BaseException:
        manifest['status'] = 'failed'; save()
        raise
    return root, manifest
