"""Offline sequential batch coordinator. Callbacks own all external operations.

A reserved operation is never repeated automatically, including after exceptions.
Records are unreviewed machine output; callers must validate callback artifacts.
"""
import hashlib
import json
import os
import re
from pathlib import Path

_BOOK = re.compile(r'book-[0-9a-f]{20}\Z')
_CHAPTER = re.compile(r'c\d+(?:\.p\d+(?:\.\d+)?)?\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
STAGES = ('analyze', 'memory', 'stage', 'render')


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def fingerprint(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _write_new(path, value):
    with path.open('xb') as f:
        f.write(_bytes(value))
        f.flush()
        os.fsync(f.fileno())


def _load(path):
    return json.loads(path.read_bytes())


def create_plan(root, book_id, index_sha256, chapters, voice_bindings):
    """Create an immutable plan; chapters is ordered [{id, source_sha256}, ...].

    The caller is responsible for deriving order and hashes from a verified index.
    Existing identical plans may be reopened; differing plans are rejected.
    """
    if not isinstance(book_id, str) or not _BOOK.fullmatch(book_id):
        raise ValueError('invalid book_id')
    if not isinstance(index_sha256, str) or not _HASH.fullmatch(index_sha256):
        raise ValueError('invalid index digest')
    if not isinstance(chapters, list) or not chapters:
        raise ValueError('chapters required')
    ids = []
    for c in chapters:
        if not isinstance(c, dict) or set(c) != {'id', 'source_sha256'} or not isinstance(c['id'], str) or not _CHAPTER.fullmatch(c['id']) or not isinstance(c['source_sha256'], str) or not _HASH.fullmatch(c['source_sha256']):
            raise ValueError('invalid chapter specification')
        ids.append(c['id'])
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate chapter')
    plan = {'version': 1, 'book_id': book_id, 'index_sha256': index_sha256,
            'chapters': chapters, 'voice_bindings_sha256': fingerprint(voice_bindings),
            'human_reviewed': False}
    directory = Path(root) / book_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'plan.json'
    try:
        _write_new(path, plan)
    except FileExistsError:
        if path.read_bytes() != _bytes(plan):
            raise ValueError('existing plan differs')
    return plan


def run(root, plan, callbacks, *, stop_requested=None, resumable_render=False):
    """Run ordered chapters. Each callback receives (chapter, prior, artifacts).

    Return {'status':'ok', 'artifact': JSON value} only after durable completion;
    other outcomes, exceptions and stale reservations stop without automatic retry.
    prior contains completed earlier chapter artifacts only, never another book.
    The chapter argument also identifies the book, frozen index and voice mapping.
    Callbacks must verify source/index and voice fingerprints against live inputs.
    Artifact values must be JSON serializable and should reference durable receipts.
    """
    book = plan.get('book_id', '')
    if not isinstance(book, str) or not _BOOK.fullmatch(book):
        raise ValueError('invalid book_id')
    directory = Path(root) / book
    if (directory / 'plan.json').read_bytes() != _bytes(plan):
        raise ValueError('plan mismatch')
    if set(callbacks) != set(STAGES) or not all(callable(callbacks[s]) for s in STAGES):
        raise ValueError('four stage callbacks required')
    if stop_requested is not None and not callable(stop_requested):
        raise ValueError('stop_requested must be callable')
    if type(resumable_render) is not bool:
        raise ValueError('resumable_render must be bool')
    lock = directory / 'active.lock'
    try:
        _write_new(lock, {'book_id': book})
    except FileExistsError:
        return {'status': 'busy_or_uncertain', 'book_id': book}
    try:
        prior = []
        for chapter in plan['chapters']:
            cdir = directory / chapter['id']
            cdir.mkdir(exist_ok=True)
            artifacts = {}
            for stage in STAGES:
                if stop_requested is not None and stop_requested():
                    return {'status': 'paused', 'book_id': book,
                            'chapter_id': chapter['id'], 'next_stage': stage,
                            'chapters': len(prior), 'human_reviewed': False}
                done = cdir / (stage + '.done.json')
                reservation = cdir / (stage + '.reserved.json')
                identity = {'plan_sha256': fingerprint(plan), 'book_id': book,
                            'chapter_id': chapter['id'], 'stage': stage,
                            'prior_sha256': fingerprint(prior),
                            'artifacts_sha256': fingerprint(artifacts),
                            'voice_bindings_sha256': plan['voice_bindings_sha256']}
                if done.exists():
                    result = _load(done)
                    if (result.get('status') != 'ok' or result.get('human_reviewed') is not False
                            or result.get('identity') != identity or 'artifact' not in result
                            or result.get('artifact_sha256') != fingerprint(result['artifact'])):
                        raise ValueError('invalid stored stage')
                    artifacts[stage] = result['artifact']
                    continue
                try:
                    _write_new(reservation, {'identity': identity, 'status': 'may_have_occurred', 'human_reviewed': False})
                except FileExistsError:
                    expected = {'identity': identity, 'status': 'may_have_occurred', 'human_reviewed': False}
                    if stage != 'render' or not resumable_render or _load(reservation) != expected:
                        return {'status': 'unknown', 'book_id': book, 'chapter_id': chapter['id'], 'stage': stage}
                try:
                    context = dict(chapter, book_id=book, index_sha256=plan['index_sha256'],
                                   voice_bindings_sha256=plan['voice_bindings_sha256'],
                                   plan_sha256=identity['plan_sha256'])
                    result = callbacks[stage](context, json.loads(_bytes(prior)), json.loads(_bytes(artifacts)))
                    if not isinstance(result, dict) or result.get('status') != 'ok' or 'artifact' not in result:
                        return _unknown(book, chapter['id'], stage, 'callback_no_success', 'Unknown')
                    record = {'status': 'ok', 'artifact': result['artifact'],
                              'artifact_sha256': fingerprint(result['artifact']),
                              'identity': identity, 'human_reviewed': False}
                    _write_new(done, record)
                    artifacts[stage] = record['artifact']
                except Exception as exc:
                    if stage=='analyze' and getattr(exc,'operation_not_started',False) is True:
                        reservation.unlink()
                        return {'status':'configuration_required','book_id':book,'chapter_id':chapter['id'],
                                'stage':stage,'reason_code':'credential_unavailable',
                                'reason_zh':'模型凭据不可用，尚未调用模型。请配置后重新开始。'}
                    return _unknown(book, chapter['id'], stage, 'callback_exception', _safe_exception_type(exc))
            prior.append({'chapter': dict(chapter), 'artifacts': artifacts, 'human_reviewed': False})
        return {'status': 'complete', 'book_id': book, 'chapters': len(prior), 'human_reviewed': False}
    finally:
        lock.unlink()


def _safe_exception_type(exc):
    """Return only a short allowlisted type label; never persist exception text."""
    name = type(exc).__name__
    allowed = {'OSError', 'ValueError', 'TypeError', 'KeyError', 'JSONDecodeError',
               'RuntimeError', 'TimeoutError', 'PermissionError', 'FileNotFoundError'}
    return name if name in allowed else 'Error'


def _unknown(book, chapter, stage, reason, error_type):
    explanations = {
        'callback_exception': '此阶段发生异常，结果状态不确定；系统不会自动重发。可检查已保存回执后离线恢复。',
        'callback_no_success': '此阶段未确认完成；系统不会自动重发。请检查已保存回执。',
    }
    return {'status': 'unknown', 'book_id': book, 'chapter_id': chapter, 'stage': stage,
            'reason_code': reason, 'error_type': error_type,
            'reason_zh': explanations.get(reason, '结果状态不确定；系统不会自动重发。')}
