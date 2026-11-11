"""Verified workbench context and local renderer for chapter audio revisions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import real_book_auto_voice as auto
import real_book_renderer as renderer
from delivery_policy import validate_override


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def get_context(runtime_root, book_id, chapter_id):
    """Restore an existing completed job, including its frozen voice records."""
    from workbench_real_v2_ui import (
        _read_hash_pinned_json, _read_sequence_plan, _sequence_root,
        _verified_stage_done, completed_audio,
    )

    base = Path(runtime_root).resolve(strict=True)
    selected = _sequence_root(base, book_id, chapter_id, require_completed=True)
    if selected is None:
        raise ValueError('no source-matching verified completed render')
    sequence_root = selected.resolve(strict=True)
    if not sequence_root.is_relative_to(base):
        raise ValueError('chapter sequence escapes the runtime root')
    job_root = sequence_root.parent
    plan = _read_sequence_plan(sequence_root, book_id)
    memory = _verified_stage_done(sequence_root, book_id, chapter_id, 'memory', plan)
    stage = _verified_stage_done(sequence_root, book_id, chapter_id, 'stage', plan)
    render = _verified_stage_done(sequence_root, book_id, chapter_id, 'render', plan)
    if memory is None or stage is None or render is None:
        raise ValueError('chapter has no complete source-bound render')
    completed_audio(sequence_root, book_id, chapter_id)

    auto_pointer = memory['artifact'].get('auto')
    if not isinstance(auto_pointer, dict) or stage['artifact'].get('auto_sha256') != auto_pointer.get('sha256'):
        raise ValueError('chapter stage and frozen voice records disagree')
    voices, _ = _read_hash_pinned_json(auto_pointer, job_root, 'frozen chapter voices')
    if (voices.get('book_id') != book_id or voices.get('chapter_id') != chapter_id
            or voices.get('blocked') is not False):
        raise ValueError('frozen chapter voice identity is invalid')
    source_stage, stage_path = _read_hash_pinned_json(stage['artifact'], job_root, 'chapter stage')
    saved_render, _ = _read_hash_pinned_json(render['artifact'], job_root, 'chapter render receipt')
    receipt = saved_render.get('receipt')
    if not isinstance(receipt, dict):
        raise ValueError('chapter renderer receipt is missing')
    manifest_path = Path(receipt['manifest_path'])
    manifest, manifest_path = _read_hash_pinned_json(
        {'path': str(manifest_path), 'sha256': receipt['manifest_sha256']}, job_root, 'chapter manifest')
    normalized_path = manifest_path.parent / 'normalized-stage.json'
    normalized, normalized_path = _read_hash_pinned_json(
        {'path': str(normalized_path), 'sha256': manifest.get('normalized_stage_sha256')},
        job_root, 'normalized chapter stage')
    source_sha = source_stage.get('audit', {}).get('source_text_sha256')
    if source_sha != receipt.get('source_sha256'):
        raise ValueError('chapter source identity changed')
    roles = {row['speaker_id'] for row in normalized['chapters'][0]['segments']}
    bindings = {role: voices['bindings'][role] for role in roles}
    for binding in bindings.values():
        auto._validate_record(binding['record'])
    document, assignments = renderer.prepare(stage_path, book_id, chapter_id, source_sha, bindings)
    verified = renderer.verify(manifest_path.parent, document, assignments, source_sha)
    if (verified.get('manifest_sha256') != receipt.get('manifest_sha256')
            or verified.get('wav_sha256') != receipt.get('wav_sha256')):
        raise ValueError('chapter renderer verification differs from its completion receipt')

    done_path = sequence_root / book_id / chapter_id / 'render.done.json'
    return {
        'runtime_root': str(base), 'book_id': book_id, 'chapter_id': chapter_id,
        'source_sha256': source_sha,
        'normalized_stage_path': str(normalized_path),
        'normalized_stage_sha256': manifest['normalized_stage_sha256'],
        'base_manifest_path': str(manifest_path),
        'base_manifest_sha256': receipt['manifest_sha256'],
        'base_wav_path': receipt['chapter_wav'], 'base_wav_sha256': receipt['wav_sha256'],
        'base_render_done_path': str(done_path), 'base_render_done_sha256': _sha(done_path),
        'voice_bindings': bindings,
        'revision_root': str(base / 'audio-revisions' / book_id / chapter_id),
    }


def render_revision(context, stage_path, output_dir):
    """Render a local-only instruction revision through the normal native path."""
    fresh = get_context(context['runtime_root'], context['book_id'], context['chapter_id'])
    pins = ('source_sha256', 'normalized_stage_sha256', 'base_manifest_sha256',
            'base_wav_sha256', 'base_render_done_sha256', 'voice_bindings')
    if any(context.get(key) != fresh[key] for key in pins):
        raise ValueError('the completed chapter changed before revision rendering')
    revision_root = Path(fresh['revision_root']).resolve()
    stage_path = Path(stage_path)
    output_dir = Path(output_dir)
    if (stage_path.is_symlink() or not stage_path.resolve(strict=True).is_relative_to(revision_root)
            or output_dir.is_symlink() or not output_dir.resolve().is_relative_to(revision_root)):
        raise ValueError('revision files escape their chapter namespace')
    original = json.loads(Path(fresh['normalized_stage_path']).read_text(encoding='utf-8'))
    candidate = json.loads(stage_path.read_text(encoding='utf-8'))
    before = original['chapters'][0]['segments']
    after = candidate['chapters'][0]['segments']
    if (candidate['chapters'][0].get('id') != fresh['chapter_id']
            or len(candidate['chapters']) != 1 or len(before) != len(after)
            or candidate.get('audit') != original.get('audit')):
        raise ValueError('revision changed the chapter source structure')
    changed = []
    for index, (old, new) in enumerate(zip(before, after)):
        if {k: v for k, v in old.items() if k != 'instruct'} != {
                k: v for k, v in new.items() if k != 'instruct'}:
            raise ValueError('revision changed paragraph words, voice, or delivery')
        if old.get('instruct') != new.get('instruct'):
            validate_override(new.get('instruct'))
            changed.append(index)
    if len(changed) != 1:
        raise ValueError('a paragraph revision must change one explicit instruction')
    output_dir.mkdir(parents=True, exist_ok=True)
    receipt = renderer.render(stage_path, fresh['book_id'], fresh['chapter_id'],
                              fresh['source_sha256'], fresh['voice_bindings'], output_dir)
    for key in ('manifest_path', 'chapter_wav'):
        produced = Path(receipt[key])
        if produced.is_symlink() or not produced.resolve(strict=True).is_relative_to(output_dir.resolve(strict=True)):
            raise ValueError('renderer output escaped the revision output directory')
    document, assignments = renderer.prepare(stage_path, fresh['book_id'], fresh['chapter_id'],
                                             fresh['source_sha256'], fresh['voice_bindings'])
    verified = renderer.verify(Path(receipt['manifest_path']).parent, document,
                               assignments, fresh['source_sha256'])
    after_render = get_context(context['runtime_root'], context['book_id'], context['chapter_id'])
    if any(after_render[key] != fresh[key] for key in pins):
        raise ValueError('the completed chapter changed during revision rendering')
    native_path = verified.get('native_receipt_path')
    if native_path:
        native = json.loads(Path(native_path).read_text(encoding='utf-8'))
        statistics = native.get('synthesis_statistics', {})
        if all(type(statistics.get(key)) is int and statistics[key] >= 0
               for key in ('generated', 'cached')):
            verified = dict(verified, cache_statistics={
                key: statistics[key] for key in ('generated', 'cached', 'retried')
                if type(statistics.get(key)) is int and statistics[key] >= 0})
    return verified
