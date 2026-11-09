"""Bounded chapter attribution input for an independently verified local index.

No network, filesystem writes, provider dispatch, or review-state mutation.
Hashes bind inputs but do not authenticate the index or model/provider. The caller
must hold the book lock, independently verify the snapshot and authenticate any
eventual response. Model candidates remain unreviewed.
"""
import copy
import hashlib
import json

import attribution_staging_adapter as staging


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def prepare(chapter, *, max_text_chars=100000, max_dialogues=1000):
    """Build one whole-chapter request; reject excess instead of truncating.

    ``chapter`` is the result of persisted_chapter_index.restore_chapter.
    The return includes novel text and must be kept private by the caller.
    """
    if type(max_text_chars) is not int or not 1 <= max_text_chars <= 1000000 or type(max_dialogues) is not int or not 1 <= max_dialogues <= 10000:
        raise ValueError('invalid bounds')
    if not isinstance(chapter, dict):
        raise ValueError('invalid chapter')
    text = chapter.get('normalized_text'); rows = chapter.get('script_rows')
    if not isinstance(text, str) or not text or len(text) > max_text_chars or not isinstance(rows, list):
        raise ValueError('invalid or oversized chapter')
    if hashlib.sha256(text.encode('utf-8')).hexdigest() != chapter.get('local_text_sha256'):
        raise ValueError('chapter text mismatch')
    if not rows or rows[0].get('type') != 'huaben' or rows[0].get('text_sha256') != chapter['local_text_sha256']:
        raise ValueError('header mismatch')
    binding = staging.capture_binding(chapter.get('book_id'), chapter.get('chapter_id'), rows)
    if sum(row.get('type') == 'chapter' for row in rows) != 1:
        raise ValueError('expected exactly one chapter')
    end = 0
    for row in rows[1:]:
        src = row.get('src')
        if (not isinstance(src, list) or len(src) != 2 or any(type(x) is not int for x in src)
                or not 0 <= end <= src[0] < src[1] <= len(text)
                or text[end:src[0]].strip() or text[src[0]:src[1]] != row.get('text')):
            raise ValueError('invalid local block range or coverage')
        end = src[1]
    if text[end:].strip() or len(binding['dialogue_text_sha256']) > max_dialogues:
        raise ValueError('uncovered text or too many dialogues')
    # Deliberately no approved identity assertion or prior-summary fabrication.
    payload = {'version': 1, 'book_id': chapter['book_id'], 'chapter_id': chapter['chapter_id'],
        'text': text, 'blocks': copy.deepcopy(rows[1:]),
        'candidate_speakers': sorted(chapter['cast']['characters']),
        'instruction': '逐条归属对白；不确定使用 unknown。所有人物和归属均为待审核推断。'}
    return {'payload': payload, 'payload_sha256': _hash(payload), 'binding': binding,
        'source_index_sha256': chapter.get('index_sha256'), 'human_reviewed': False,
        'production_dispatch_enabled': False}


def stage(chapter, prepared, attribution, *, fallback_voice=None):
    """Convert authenticated caller-supplied response to reversible staging only.

    This verifies binding, not response origin or whether a model saw this input.
    """
    current = prepare(chapter)
    if prepared != current:
        raise ValueError('prepared input changed')
    return staging.adapt_attribution(chapter['book_id'], chapter['chapter_id'],
        chapter['script_rows'], chapter['cast'], attribution, current['binding'],
        fallback_voice=fallback_voice)
