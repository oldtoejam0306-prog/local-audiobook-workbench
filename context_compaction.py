"""Deterministic bounded serialization of validated, book-scoped chapter memory."""
import hashlib
import json


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _chapter_key(value):
    import re
    m = re.fullmatch(r'c(\d+)(?:\.p(\d+)(?:\.(\d+))?)?', value)
    if not m:
        raise ValueError('invalid chapter identity in prior memory')
    return tuple(int(x or 0) for x in m.groups())


def _hash(value, field, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('invalid ' + field)
    return value


def compact_prior_context(book_id, target_chapter_id, records, *, corrections=(), max_bytes=12000,
                          allow_current_corrections=False):
    """Keep recent summaries and a non-merging identity index under a byte budget.

    `records` must already have been read with their artifact/file hashes checked.
    Each record remains book scoped and unreviewed. Identity rows are kept per
    chapter/name; equal names and aliases are never merged across chapters.
    """
    if type(max_bytes) is not int or max_bytes < 1024:
        raise ValueError('context budget too small')
    selection_budget = max_bytes - 64  # room for deterministic omission-counter growth
    target_key = _chapter_key(target_chapter_id)
    normalized = []
    seen = set()
    for record in records:
        if record.get('book_id') != book_id or record.get('human_reviewed') is not False:
            raise ValueError('prior memory book/status mismatch')
        chapter_id = record.get('chapter_id')
        key = _chapter_key(chapter_id)
        if key >= target_key or chapter_id in seen:
            raise ValueError('prior memory chapter order/identity mismatch')
        seen.add(chapter_id)
        record_sha = _hash(record.get('record_sha256'), 'record_sha256')
        response_sha = _hash(record.get('response_sha256'), 'response_sha256')
        source_sha = _hash(record.get('source_sha256'), 'source_sha256', optional=True)
        chars = record.get('characters')
        if not isinstance(chars, list):
            raise ValueError('invalid prior character index')
        normalized.append({**record, '_key': key, '_record_sha': record_sha,
                           '_response_sha': response_sha, '_source_sha': source_sha})
    normalized.sort(key=lambda r: r['_key'], reverse=True)

    owners = {}
    identity_groups = []
    for record in normalized:
        cid, source = record['chapter_id'], record['_source_sha']
        for char in record['characters']:
            name, als = char.get('name'), char.get('aliases', [])
            if not isinstance(name, str) or not name or not isinstance(als, list) or not all(isinstance(a, str) and a for a in als):
                raise ValueError('invalid prior character/alias')
            status = char.get('identity_status', 'unreviewed_model_candidate')
            # Records supplied here are explicitly unreviewed; never upgrade
            # an identity based on a field embedded in machine output.
            status = 'unreviewed_model_candidate'
            name_row = {'name': name, 'chapter_id': cid,
                'source_sha256': source, 'identity_status': status}
            name_priority = (record['_key'], name_row)
            alias_group = []
            for alias in als:
                alias_row = {'alias': alias, 'name': name,
                    'chapter_id': cid, 'source_sha256': source, 'identity_status': status}
                alias_priority = (record['_key'], alias_row)
                alias_group.append(alias_priority)
                owners.setdefault(alias, []).append({'name': name, 'chapter_id': cid,
                                                     'source_sha256': source})
            identity_groups.append((name_priority, alias_group))
    conflicts = []
    for alias, candidates in owners.items():
        if len({c['name'] for c in candidates}) > 1:
            latest = max((_chapter_key(c['chapter_id']) for c in candidates))
            conflicts.append((latest, {'alias': alias, 'candidates': candidates}))
    conflicts.sort(key=lambda x: (x[0], x[1]['alias']), reverse=True)

    normalized_corrections = []
    for item in corrections:
        if not isinstance(item, dict) or not isinstance(item.get('chapter_id'), str):
            raise ValueError('invalid prior correction')
        cid = item['chapter_id']; key = _chapter_key(cid)
        if (key > target_key or (key == target_key and not allow_current_corrections)
                or not isinstance(item.get('note'), str) or not item['note'].strip()):
            raise ValueError('invalid prior correction')
        source = item.get('source_sha256')
        note = {'chapter_id': cid, 'note': item['note'],
            'source_sha256': _hash(source, 'correction source_sha256', optional=True),
            'status': 'operator_note_not_identity_approval'}
        if item.get('kind') in ('reader_correction','summary_correction','pronunciation'):
            note['kind']=item['kind']
        if isinstance(item.get('character_name'),str) and item['character_name']:
            note['character_name']=item['character_name']
        normalized_corrections.append((key,note))
    normalized_corrections.sort(key=lambda x: x[0], reverse=True)

    counts = {'summaries': 0, 'names': 0, 'aliases': 0, 'conflicts': 0,
              'corrections': 0, 'source_hash_unavailable': sum(r['_source_sha'] is None for r in normalized)}
    context = {'version': 1, 'book_id': book_id, 'target_chapter_id': target_chapter_id,
        'records': [], 'character_index': [], 'alias_index': [], 'alias_conflicts': [],
        'corrections': [], 'human_reviewed': False,
        'compaction': {'budget_bytes': max_bytes, 'input_record_count': len(normalized),
            'omitted_counts': counts,
            'policy': 'newest corrections/conflicts, 8 newest chapter records, names, aliases, then older chapter records; no identity merging'}}

    def offer(kind, value):
        target = {'summary': 'records', 'name': 'character_index', 'alias': 'alias_index',
                  'conflict': 'alias_conflicts', 'correction': 'corrections'}[kind]
        context[target].append(value)
        if len(_bytes(context)) <= selection_budget:
            return
        else:
            context[target].pop()
            counts[{'summary': 'summaries', 'name': 'names', 'alias': 'aliases',
                    'conflict': 'conflicts', 'correction': 'corrections'}[kind]] += 1

    summary_candidates = []
    for r in normalized:
        summary = r.get('chapter_summary')
        if summary is not None and (not isinstance(summary, str) or len(summary) > 1500):
            raise ValueError('invalid prior chapter summary')
        summary_candidates.append({'chapter_id': r['chapter_id'], 'chapter_summary': summary,
            'record_sha256': r['_record_sha'], 'response_sha256': r['_response_sha'],
            'source_sha256': r['_source_sha'], 'status': 'unreviewed_machine_candidate',
            'unresolved': r.get('unresolved', [])})
    for _, item in normalized_corrections:
        offer('correction', item)
    for _, item in conflicts:
        offer('conflict', item)
    # Guarantee room for recent chapter context, then reserve remaining space
    # for identity names/aliases before older summaries can consume the budget.
    for item in summary_candidates[:8]:
        offer('summary', item)
    for name_item, alias_items in identity_groups:
        offer('name', name_item[1])
        for _, alias_item in alias_items:
            offer('alias', alias_item)
    for item in summary_candidates[8:]:
        offer('summary', item)

    # Array iteration order is priority order (most recent first), independently
    # of serialization key ordering, so repeated runs produce identical bytes.
    if len(_bytes(context)) > max_bytes:
        raise AssertionError('compaction audit metadata exceeds budget')
    return context
