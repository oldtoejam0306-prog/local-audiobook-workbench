#!/usr/bin/env python3
"""Local, explicit book memory for Huashuo. No network or model calls."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
from datetime import datetime, timezone


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def load_rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]


def validate(memory):
    chars = memory['characters']
    owners = {}
    for name, data in chars.items():
        if not name or not data.get('voice'):
            raise ValueError('every character needs a name and voice')
        for alias in [name, *data.get('aliases', [])]:
            if alias in owners and owners[alias] != name:
                raise ValueError(f'conflicting alias: {alias}')
            owners[alias] = name
    seen = set()
    for item in memory.get('corrections', []):
        block_id = item['block_id']
        if block_id in seen:
            raise ValueError(f'duplicate correction: {block_id}')
        seen.add(block_id)
        if item['speaker'] not in chars:
            raise ValueError(f'unknown character in correction: {item["speaker"]}')
        if len(item.get('text_sha256', '')) != 64:
            raise ValueError(f'correction requires text_sha256: {block_id}')
    return owners


def initialize(workdir, book_id):
    target = workdir / 'book-memory.json'
    if target.exists():
        raise ValueError(f'memory already exists: {target}')
    cast = read_json(workdir / 'cast.json')
    memory = {'version': 1, 'book_id': book_id, 'narrator': cast['narrator'],
              'characters': cast.get('characters', {}), 'chapter_summaries': [],
              'corrections': [], 'notes': 'Aliases are book-scoped; ambiguous titles require manual review.'}
    validate(memory)
    write_json(target, memory)
    print(f'Created {target}')


def inspect(workdir, apply):
    memory = read_json(workdir / 'book-memory.json')
    names = validate(memory)
    script_path, cast_path = workdir / 'script.huaben.jsonl', workdir / 'cast.json'
    rows = load_rows(script_path)
    indexed = {r['id']: r for r in rows if 'id' in r}
    if len(indexed) != sum('id' in r for r in rows):
        raise ValueError('duplicate block IDs')
    changes, unresolved = [], []
    corrections = {c['block_id']: c for c in memory.get('corrections', [])}
    missing = set(corrections) - set(indexed)
    if missing:
        raise ValueError(f'correction block not found: {sorted(missing)}')
    for row in rows:
        if row.get('type') != 'dialogue':
            continue
        block_id = row['id']
        current = row.get('speaker', 'unknown')
        correction = corrections.get(block_id)
        if correction:
            if digest(row.get('text', '')) != correction['text_sha256']:
                raise ValueError(f'correction text changed: {block_id}; review required')
            new = correction['speaker']
        else:
            new = names.get(current, current)
        if new not in memory['characters']:
            unresolved.append({'id': block_id, 'speaker': current})
        elif new != current:
            changes.append({'id': block_id, 'from': current, 'to': new})
            row['speaker'] = new
    old_cast = read_json(cast_path)
    new_cast = copy.deepcopy(old_cast)
    new_cast['narrator'] = memory['narrator']
    new_cast.setdefault('characters', {}).update(memory['characters'])
    for name, data in new_cast['characters'].items():
        if name in memory['characters']:
            data['voice'] = memory['characters'][name]['voice']
    print(json.dumps({'book_id': memory['book_id'], 'changes': changes,
                      'unresolved': unresolved, 'summaries': len(memory.get('chapter_summaries', [])),
                      'applied': apply}, ensure_ascii=False, indent=2))
    if not apply:
        return
    if unresolved:
        raise ValueError('unresolved speakers; no files changed')
    backups = workdir / 'memory-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backups.mkdir(parents=True)
    shutil.copy2(script_path, backups / script_path.name)
    shutil.copy2(cast_path, backups / cast_path.name)
    text = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)
    tmp = script_path.with_suffix('.jsonl.tmp')
    tmp.write_text(text, encoding='utf-8')
    write_json(cast_path, new_cast)
    os.replace(tmp, script_path)
    checkpoint = {'version': 1, 'book_id': memory['book_id'],
                  'memory_sha256': hashlib.sha256((workdir/'book-memory.json').read_bytes()).hexdigest(),
                  'script_sha256': hashlib.sha256(script_path.read_bytes()).hexdigest(),
                  'cast_sha256': hashlib.sha256(cast_path.read_bytes()).hexdigest(),
                  'backup': str(backups), 'updated_at': datetime.now(timezone.utc).isoformat()}
    write_json(workdir / 'memory-checkpoint.json', checkpoint)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init'); init.add_argument('workdir', type=Path); init.add_argument('--book-id', required=True)
    check = sub.add_parser('review'); check.add_argument('workdir', type=Path)
    apply = sub.add_parser('apply'); apply.add_argument('workdir', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'init': initialize(args.workdir, args.book_id)
        else: inspect(args.workdir, args.action == 'apply')
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f'error: {exc}\n')


if __name__ == '__main__':
    main()
