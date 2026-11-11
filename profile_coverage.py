"""Reconcile requested chapter coverage with profile source and batch records.

This is an evidence report, not a character-accuracy metric. It never reads
chapter text and can operate on an in-memory registry or a saved JSON file.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SUCCESS = {'needs_review', 'complete', 'covered'}
FAILED = {'failed', 'invalid_result', 'source_pin_mismatch', 'credential_unavailable'}
UNKNOWN = {'unknown', 'usage_unknown', 'invalid_or_usage_unknown', 'partial'}


def _load_registry(value: dict[str, Any] | str | Path | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    parsed = json.loads(Path(value).read_text(encoding='utf-8'))
    if not isinstance(parsed, dict):
        raise ValueError('registry must be a JSON object')
    return parsed


def _chapter_ids(row: dict[str, Any]) -> list[str]:
    values = row.get('chapters')
    if isinstance(values, list):
        out = []
        for item in values:
            value = item.get('chapter_id') if isinstance(item, dict) else item
            if isinstance(value, str) and value:
                out.append(value)
        return out
    sources = row.get('source_chapters')
    if isinstance(sources, list):
        return [x['chapter_id'] for x in sources if isinstance(x, dict) and isinstance(x.get('chapter_id'), str)]
    return []


def build_coverage_report(requested_chapter_ids: list[str], registry: dict[str, Any] | str | Path | None,
                          batch_receipts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return chapter-level coverage and processing status for a requested range.

    `batch_receipts` may contain saved per-batch status records. Registry
    `profile_jobs` remain useful when receipts are unavailable; source rows are
    authoritative only for which chapter text hashes the registry references.
    """
    if (not isinstance(requested_chapter_ids, list) or not requested_chapter_ids
            or any(not isinstance(x, str) or not x for x in requested_chapter_ids)
            or len(requested_chapter_ids) != len(set(requested_chapter_ids))):
        raise ValueError('requested chapter IDs must be nonempty, unique strings in order')
    document = _load_registry(registry)
    sources = document.get('source', {}).get('chapters', []) if isinstance(document.get('source'), dict) else []
    hashes: dict[str, set[str]] = {}
    for row in sources if isinstance(sources, list) else []:
        if isinstance(row, dict) and isinstance(row.get('chapter_id'), str):
            value = row.get('text_sha256')
            if isinstance(value, str) and value:
                hashes.setdefault(row['chapter_id'], set()).add(value)
    jobs = document.get('profile_jobs', [])
    receipts = list(batch_receipts or [])
    if not isinstance(jobs, list) or not isinstance(receipts, list):
        raise ValueError('batch jobs and receipts must be arrays')
    batches = [('registry_job', row) for row in jobs if isinstance(row, dict)]
    batches.extend(('receipt', row) for row in receipts if isinstance(row, dict))
    rows = []
    for chapter_id in requested_chapter_ids:
        matching = [(origin, batch) for origin, batch in batches if chapter_id in _chapter_ids(batch)]
        statuses = [str(batch.get('status', 'unknown')) for _origin, batch in matching]
        successful = any(status in SUCCESS for status in statuses)
        failed = any(status in FAILED for status in statuses)
        unknown = any(status in UNKNOWN or status not in SUCCESS | FAILED for status in statuses)
        source_hashes = sorted(hashes.get(chapter_id, set()))
        conflict = len(source_hashes) > 1
        if conflict:
            status = 'source_hash_conflict'
        elif successful and source_hashes:
            status = 'covered_reused' if any(batch.get('reused') is True for _o, batch in matching
                if batch.get('status') in SUCCESS) else 'covered'
        elif failed:
            status = 'failed'
        elif unknown:
            status = 'unknown'
        elif source_hashes:
            status = 'source_referenced_status_unconfirmed'
        else:
            status = 'deferred'
        rows.append({'chapter_id': chapter_id, 'status': status, 'source_sha256': source_hashes,
            'source_hash_conflict': conflict, 'batch_statuses': statuses})
    counts = {key: sum(row['status'] == key for row in rows) for key in
        ('covered', 'covered_reused', 'failed', 'unknown', 'deferred', 'source_hash_conflict',
         'source_referenced_status_unconfirmed')}
    return {'version': 1, 'book_id': document.get('book_id'),
        'requested_chapter_ids': requested_chapter_ids[:], 'chapters': rows,
        'counts': {'requested': len(rows), **counts},
        'unresolved_counts': {'profiles': len(document.get('unresolved_profiles', [])) if isinstance(document.get('unresolved_profiles', []), list) else None,
            'profile_conflicts': len(document.get('profile_conflicts', [])) if isinstance(document.get('profile_conflicts', []), list) else None,
            'source_conflicts': len(document.get('source_conflicts', [])) if isinstance(document.get('source_conflicts', []), list) else None,
            'alias_conflicts': len(document.get('alias_conflicts', [])) if isinstance(document.get('alias_conflicts', []), list) else None,
            'unresolved_relations': len(document.get('unresolved_relations', [])) if isinstance(document.get('unresolved_relations', []), list) else None},
        'semantic_accuracy': 'not_assessed', 'human_reviewed': False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('registry', type=Path)
    parser.add_argument('requested_chapters', type=Path, help='JSON array of ordered chapter IDs')
    parser.add_argument('--receipts', type=Path, help='Optional JSON array of per-batch status records')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    requested = json.loads(args.requested_chapters.read_text(encoding='utf-8'))
    receipts = json.loads(args.receipts.read_text(encoding='utf-8')) if args.receipts else []
    report = build_coverage_report(requested, args.registry, receipts)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(rendered, encoding='utf-8')
    else:
        print(rendered, end='')


if __name__ == '__main__':
    main()
