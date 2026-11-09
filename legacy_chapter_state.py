"""Read-only check for a user-configured legacy chapter job root."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import release_config

_BOOK = re.compile(r'book-[0-9a-f]{20}\Z')
_CHAPTER = re.compile(r'c\d+(?:\.p\d+(?:\.\d+)?)?\Z')


def check(payload: dict) -> dict:
    if (not isinstance(payload, dict) or set(payload) != {'action', 'book_id', 'chapter_id', 'legacy_root'}
            or payload.get('action') != 'check'):
        raise ValueError('invalid legacy state request')
    book, chapter = payload['book_id'], payload['chapter_id']
    if not isinstance(book, str) or not _BOOK.fullmatch(book) or not isinstance(chapter, str) or not _CHAPTER.fullmatch(chapter):
        raise ValueError('invalid legacy job identity')
    configured_arg = Path(str(release_config.remote_legacy_chapter_root()))
    requested_arg = Path(payload['legacy_root']) if isinstance(payload['legacy_root'], str) else Path('.')
    if configured_arg.is_symlink() or requested_arg.is_symlink(): raise ValueError('legacy root symlink refused')
    configured = configured_arg.resolve(strict=True)
    requested = requested_arg.resolve(strict=True)
    if requested != configured or not requested.is_dir():
        raise ValueError('legacy root differs from remote config')
    jobs = requested / 'jobs'
    if jobs.is_symlink() or not jobs.is_dir() or (requested / 'active.lock').exists():
        return {'status': 'blocked'}
    if (jobs / (book + '-' + chapter)).exists():
        return {'status': 'blocked'}
    for entry in jobs.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            return {'status': 'blocked'}
        state_path = entry / 'state.json'
        if state_path.is_symlink() or not state_path.is_file():
            return {'status': 'blocked'}
        try: state = json.loads(state_path.read_bytes())
        except (OSError, ValueError, UnicodeError): return {'status': 'blocked'}
        if state.get('status') != 'complete': return {'status': 'blocked'}
    return {'status': 'clear'}


def main():
    request = json.load(sys.stdin)
    result = check(request)
    print(json.dumps(result))


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'status': 'blocked', 'error': str(exc)}))
        raise SystemExit(1)
