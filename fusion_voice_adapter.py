"""Offline fusion-to-voice planning bridge; no network, models, or file writes.

Input must already come from authenticated receipt/ledger validation. Hashes here
bind evidence but do not authenticate it. Output is provisional: `blocked` must
be honored by callers; nothing is human-approved or a voice asset.
"""
import copy
import hashlib
import json
import re

import auto_voice_planner
import whole_book_adapters

_SHA = re.compile(r'^[0-9a-f]{64}$')


def adapt_fusion_voice(snapshot, chapter_texts, script_rows, *, user_locked_voices=None):
    """Return a complete evidence bundle and an explicitly provisional voice plan.

    `chapter_texts` must cover the entire ordered book; `user_locked_voices`
    are externally authenticated user lock records accepted by fusion_to_executor.
    Unlinked conflict/evidence blocks downstream use globally. This pure
    adapter never asserts that caller-supplied ledger provenance is authentic.
    """
    source = copy.deepcopy(snapshot)
    if not isinstance(source, dict) or len(source.get('processed', [])) != len(source.get('chapter_order', [])):
        raise ValueError('all chapters must be processed before voice planning')
    adapted = whole_book_adapters.fusion_to_executor(source, chapter_texts, user_locked_voices=user_locked_voices)
    meta = source.get('chapter_map_v1')
    if not isinstance(meta, dict) or not _SHA.fullmatch(str(meta.get('source_sha256', ''))):
        raise ValueError('chapter-map source provenance missing')
    records = meta.get('chapters')
    processed = source['processed']
    if not isinstance(records, list) or len(records) != len(processed):
        raise ValueError('chapter-map history incomplete')
    for record, item in zip(records, processed):
        if (not isinstance(record, dict) or record.get('chapter_id') != item['chapter_id']
            or record.get('chapter_sha256') != item['source_sha256']
            or any(not _SHA.fullmatch(str(record.get(k, ''))) for k in ('export_sha256','receipt_sha256','input_snapshot_sha256'))):
            raise ValueError('chapter-map provenance mismatch')
    people = adapted['characters']
    by_id = {p['entity_id']: p for p in people}
    names = {}
    for person in people:
        for name in [person['name'], *[alias['name'] for alias in person['aliases']]]:
            names.setdefault(name, set()).add(person['entity_id'])
    uncertainties = {p['entity_id']: copy.deepcopy(p['uncertainties']) for p in people}
    unlinked = []
    def attach(entry, ids, label):
        ids = set(ids)
        if not ids or not ids <= by_id.keys():
            unlinked.append({'kind': label, 'entry': copy.deepcopy(entry)})
        else:
            for ident in ids:
                uncertainties[ident].append({'kind': label, 'status': 'needs_review', 'entry': copy.deepcopy(entry)})
    for conflict in adapted['unresolved_conflicts']:
        ids = set()
        for ident in [conflict.get('entity_id'), conflict.get('link_to'), *conflict.get('possible_entities', [])]:
            if isinstance(ident, str): ids.add(ident)
        for key in ('name', 'alias'):
            if conflict.get(key) in names: ids.update(names[conflict[key]])
        attach(conflict, ids, 'fusion_conflict')
    voice_by_id = {p['entity_id']: [] for p in people}
    for record in records:
        chapter = record['chapter_id']
        voices = record.get('voice_candidates')
        unresolved = record.get('unresolved_evidence')
        if not isinstance(voices, list) or not isinstance(unresolved, list):
            raise ValueError('voice or unresolved evidence missing')
        evidence_chars = record.get('source_evidence', {}).get('characters', [])
        for voice in voices:
            if not isinstance(voice, dict): raise ValueError('invalid voice candidate')
            ids = names.get(voice.get('name'), set())
            entry = {'chapter_id': chapter, 'chapter_sha256': record['chapter_sha256'],
                     'receipt_sha256': record['receipt_sha256'], 'candidate': copy.deepcopy(voice)}
            if len(ids) != 1:
                unlinked.append({'kind': 'unlinked_voice_candidate', 'entry': entry})
            else:
                voice_by_id[next(iter(ids))].append(entry)
        for item in unresolved:
            if not isinstance(item, dict): raise ValueError('invalid unresolved evidence')
            field = item.get('field', '')
            match = re.match(r'^characters\[(\d+)\]', field) if isinstance(field, str) else None
            ids = set()
            if match and int(match.group(1)) < len(evidence_chars):
                ids = names.get(evidence_chars[int(match.group(1))].get('name'), set())
            # Even non-character unresolved evidence must remain visible and block.
            attach({'chapter_id': chapter, **copy.deepcopy(item)}, ids, 'unresolved_evidence')
    memory = {'book_id': source['book_id'], 'characters': {}}
    contradictions = {}
    for person in people:
        ident = person['entity_id']; candidates = voice_by_id[ident]
        # Model and source evidence are all provisional. They are planning hints,
        # never verified acoustic facts or user voice locks.
        descriptions = [v['candidate'].get('description', '') for v in candidates]
        combined = '；'.join(x for x in descriptions if isinstance(x, str) and x)
        conflicts = auto_voice_planner._conflicts(combined)
        if conflicts:
            contradictions[ident] = {'warnings': conflicts, 'voice_candidates': copy.deepcopy(candidates), 'status': 'needs_review'}
            uncertainties[ident].append({'kind': 'voice_description_conflict', 'status': 'needs_review', 'warnings': conflicts})
        memory['characters'][person['name']] = {
            'aliases': [a['name'] for a in person['aliases']],
            'voice_description': combined, 'voice_evidence_status': 'assumption',
        }
        person['uncertainties'] = uncertainties[ident]
    # Only externally verified user locks accepted by the upstream adapter.
    pins = {p['name']: p['fixed_voice'] for p in people if p.get('fixed_voice_source') == 'user_locked'}
    plan = auto_voice_planner.plan_voice_designs(memory, script_rows, pinned_voices=pins)
    return {'version': 1, 'book_id': source['book_id'], 'blocked': bool(unlinked),
            'block_reasons': unlinked, 'fusion_snapshot': source, 'fusion_adapted': adapted,
            'chapter_texts': copy.deepcopy(chapter_texts), 'source_sha256': meta['source_sha256'],
            'voice_candidates_by_entity': voice_by_id, 'voice_contradictions': contradictions,
            'planner_memory': memory, 'voice_plan': plan,
            'trust': 'offline provisional; caller authenticates ledger, source and user locks'}
