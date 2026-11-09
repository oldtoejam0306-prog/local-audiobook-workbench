"""Create-once, per-chapter persistent storage for a trusted in-memory index.

This module stores an index built by chapter_local_index in a new isolated
workspace. It does not authenticate caller snapshots, read original book files,
render audio, or treat local hashes as provenance authentication. Restore needs
an independently authenticated snapshot and expected index digest from caller.
Only the manifest and requested chapter file are read during chapter recovery.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from collections.abc import Mapping
from typing import Any, Iterator

import chapter_local_index


_HASH = set('0123456789abcdef')
_MANIFEST_FIELDS = {
    'version', 'status', 'book_id', 'source_sha256', 'text_sha256',
    'script_sha256', 'cast_sha256', 'index_sha256', 'header_json_b64',
    'header_sha256', 'cast_json_b64', 'chapters', 'manifest_sha256',
}
_RECORD_FIELDS = {
    'version', 'book_id', 'source_sha256', 'text_sha256', 'script_sha256',
    'cast_sha256', 'index_sha256', 'chapter_id', 'original_start', 'original_end',
    'chapter_digest', 'text_sha256_local', 'text', 'rows',
}
_MAX_CHAPTERS = 100_000
_MAX_CHAPTER_FILE_BYTES = 64 * 1024 * 1024


class ChapterIndexStoreError(RuntimeError):
    pass


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                          allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError('chapter index data is not canonical JSON') from exc


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _HASH for char in value)


def _trusted_snapshot(snapshot: Mapping[str, Any]) -> dict[str, str]:
    fields = {'book_id', 'source_sha256', 'text_sha256', 'script_sha256', 'cast_sha256'}
    if not isinstance(snapshot, Mapping) or set(snapshot) != fields:
        raise ValueError('trusted snapshot fields do not match chapter index contract')
    if not isinstance(snapshot.get('book_id'), str) or not snapshot['book_id'].strip():
        raise ValueError('trusted snapshot book_id is invalid')
    if any(not _is_hash(snapshot.get(key)) for key in fields - {'book_id'}):
        raise ValueError('trusted snapshot has an invalid SHA-256')
    return dict(snapshot)


def _index_summary(identity: Mapping[str, str], header_sha256: str,
                   chapter_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        'book_id': identity['book_id'],
        'source_sha256': identity['source_sha256'],
        'text_sha256': identity['text_sha256'],
        'script_sha256': identity['script_sha256'],
        'cast_sha256': identity['cast_sha256'],
        'header_sha256': header_sha256,
        'chapters': [
            {'id': row['chapter_id'], 'start': row['original_start'],
             'end': row['original_end'], 'chapter_digest': row['chapter_digest'],
             'text_sha256': row['text_sha256']}
            for row in chapter_rows
        ],
    }


def _chapter_filename(chapter_id: str) -> str:
    if not isinstance(chapter_id, str) or not chapter_id:
        raise ValueError('chapter ID must be a nonempty string')
    return hashlib.sha256(chapter_id.encode('utf-8')).hexdigest() + '.json'


def _write_exclusive(path: Path, content: bytes) -> None:
    with path.open('xb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_dir(path: Path) -> None:
    if os.name == 'nt':
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _process_lock(path: Path) -> Iterator[None]:
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ChapterIndexStoreError('chapter index lock parent must be a real directory')
    try:
        parent_before = os.stat(parent, follow_symlinks=False)
    except OSError as exc:
        raise ChapterIndexStoreError('chapter index lock parent cannot be identified') from exc
    if not stat.S_ISDIR(parent_before.st_mode) or _is_reparse(parent_before):
        raise ChapterIndexStoreError('chapter index lock parent is not a plain directory')
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags | os.O_EXCL, 0o600)
    except FileExistsError:
        _validate_lock_path(path)
        try:
            descriptor = os.open(path, os.O_RDWR | (os.O_NOFOLLOW if hasattr(os, 'O_NOFOLLOW') else 0))
        except OSError as exc:
            raise ChapterIndexStoreError('could not open existing chapter index lock safely') from exc
    except OSError as exc:
        raise ChapterIndexStoreError('could not open chapter index lock safely') from exc
    stream = os.fdopen(descriptor, 'r+b', buffering=0)
    try:
        handle_before = os.fstat(stream.fileno())
        path_before = _validate_lock_path(path)
        _require_same_file(handle_before, path_before)
        _require_same_directory(parent_before, os.stat(parent, follow_symlinks=False))
        if os.name == 'nt':
            import msvcrt
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                _require_same_file(os.fstat(stream.fileno()), _validate_lock_path(path))
                _require_same_directory(parent_before, os.stat(parent, follow_symlinks=False))
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                _require_same_file(os.fstat(stream.fileno()), _validate_lock_path(path))
                _require_same_directory(parent_before, os.stat(parent, follow_symlinks=False))
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def _is_reparse(info) -> bool:
    flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    return bool(getattr(info, 'st_file_attributes', 0) & flag)


def _validate_lock_path(path: Path):
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ChapterIndexStoreError('chapter index lock path changed or disappeared') from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise ChapterIndexStoreError('chapter index lock may not be a symlink or reparse point')
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ChapterIndexStoreError('chapter index lock must be a single-link regular file')
    return info


def _require_same_file(handle_info, path_info) -> None:
    if (not stat.S_ISREG(handle_info.st_mode) or _is_reparse(handle_info)
            or handle_info.st_nlink != 1
            or (handle_info.st_dev, handle_info.st_ino) != (path_info.st_dev, path_info.st_ino)):
        raise ChapterIndexStoreError('chapter index lock handle/path identity changed')


def _require_same_directory(before, after) -> None:
    if (not stat.S_ISDIR(after.st_mode) or _is_reparse(after)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
        raise ChapterIndexStoreError('chapter index lock parent identity changed')


def _validate_index(index: chapter_local_index.ChapterIndex,
                    snapshot: Mapping[str, Any], expected_index_sha256: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
    identity = _trusted_snapshot(snapshot)
    if not _is_hash(expected_index_sha256):
        raise ValueError('expected_index_sha256 is invalid')
    if any(getattr(index, key) != value for key, value in identity.items()):
        raise ValueError('in-memory index identity differs from trusted snapshot')
    if index.index_sha256 != expected_index_sha256:
        raise ValueError('in-memory index digest differs from trusted expected digest')
    if not index.chapters or len(index.chapters) > _MAX_CHAPTERS:
        raise ValueError('chapter index count is empty or exceeds the supported limit')
    header_sha = _sha(index.header_json)
    if _sha(index.cast_json) != identity['cast_sha256']:
        raise ValueError('in-memory cast bytes differ from trusted cast hash')
    try:
        header = json.loads(index.header_json.decode('utf-8'))
        json.loads(index.cast_json.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('in-memory header or cast JSON is invalid') from exc
    if not isinstance(header, dict) or header.get('type') != 'huaben':
        raise ValueError('in-memory script header is invalid')
    chapter_rows = []
    seen = set()
    expected_start = 0
    for entry in index.chapters:
        if (not isinstance(entry.chapter_id, str) or not entry.chapter_id or entry.chapter_id in seen
                or type(entry.original_start) is not int or type(entry.original_end) is not int
                or entry.original_start != expected_start or entry.original_end <= entry.original_start
                or entry.original_end - entry.original_start != len(entry.text)):
            raise ValueError('in-memory chapter index has duplicate IDs or invalid ranges')
        seen.add(entry.chapter_id)
        try:
            rows = json.loads(entry.original_rows_json.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError('in-memory chapter rows are invalid JSON') from exc
        if (not isinstance(rows, list) or chapter_local_index._json(rows) != entry.original_rows_json
                or _sha(entry.original_rows_json) != entry.chapter_digest
                or _sha(entry.text.encode('utf-8')) != entry.text_sha256):
            raise ValueError('in-memory chapter text or row digest mismatch')
        chapter_rows.append({
            'chapter_id': entry.chapter_id, 'original_start': entry.original_start,
            'original_end': entry.original_end, 'chapter_digest': entry.chapter_digest,
            'text_sha256': entry.text_sha256,
        })
        expected_start = entry.original_end
    if expected_start == 0:
        raise ValueError('in-memory chapter index is empty')
    summary = _index_summary(identity, header_sha, chapter_rows)
    if chapter_local_index.object_sha256(summary) != expected_index_sha256:
        raise ValueError('in-memory index summary does not match trusted index digest')
    return identity, chapter_rows


def _building_path(workspace: Path, index_sha256: str) -> Path:
    return workspace.parent / f'.{workspace.name}.building'


def _building_residues(workspace: Path) -> list[Path]:
    prefix = f'.{workspace.name}.'
    result = []
    try:
        for item in workspace.parent.iterdir():
            if item.name == f'.{workspace.name}.building' or (
                    item.name.startswith(prefix) and item.name.endswith('.building')):
                result.append(item)
    except OSError as exc:
        raise ChapterIndexStoreError('could not check for incomplete chapter index builds') from exc
    return result


def store_chapter_index(index: chapter_local_index.ChapterIndex, workspace: str | Path, *,
                        trusted_snapshot: Mapping[str, Any],
                        expected_index_sha256: str) -> dict[str, Any]:
    """Atomically create a new isolated workspace with one file per chapter.

    A deterministic abandoned building directory blocks retries, preserving
    uncertain writes for manual reconciliation. Existing workspaces are never
    overwritten. Hashes bind content but do not authenticate snapshot origin.
    """
    raw_workspace = Path(workspace).expanduser()
    if raw_workspace.exists() or raw_workspace.is_symlink():
        raise ChapterIndexStoreError('chapter index workspace already exists; overwrite is forbidden')
    if not raw_workspace.name or raw_workspace.name in ('.', '..'):
        raise ValueError('chapter index workspace requires a new directory name')
    raw_parent = raw_workspace.parent
    if raw_parent.is_symlink():
        raise ChapterIndexStoreError('chapter index workspace parent may not be a symlink')
    parent = raw_parent.resolve(strict=True)
    if parent == Path(parent.anchor) or not parent.is_dir():
        raise ValueError('chapter index parent must be a narrow existing directory')
    target = parent / raw_workspace.name
    identity, chapter_rows = _validate_index(index, trusted_snapshot, expected_index_sha256)
    lock_path = parent / f'.{target.name}.lock'
    building = _building_path(target, expected_index_sha256)

    with _process_lock(lock_path):
        if target.exists() or target.is_symlink():
            raise ChapterIndexStoreError('chapter index workspace already exists; overwrite is forbidden')
        if _building_residues(target):
            raise ChapterIndexStoreError('incomplete chapter index build exists; reconcile before retry')
        building.mkdir(mode=0o700)
        (building / 'chapters').mkdir()
        manifest_chapters = []
        for entry in index.chapters:
            rows = json.loads(entry.original_rows_json.decode('utf-8'))
            record = {
                'version': 1, **identity, 'index_sha256': expected_index_sha256,
                'chapter_id': entry.chapter_id,
                'original_start': entry.original_start, 'original_end': entry.original_end,
                'chapter_digest': entry.chapter_digest, 'text_sha256_local': entry.text_sha256,
                'text': entry.text, 'rows': rows,
            }
            raw = _json(record)
            if len(raw) > _MAX_CHAPTER_FILE_BYTES:
                raise ValueError('one chapter index file exceeds the supported size limit')
            filename = _chapter_filename(entry.chapter_id)
            relative = f'chapters/{filename}'
            _write_exclusive(building / relative, raw)
            manifest_chapters.append({
                'chapter_id': entry.chapter_id, 'file': relative,
                'original_start': entry.original_start, 'original_end': entry.original_end,
                'chapter_digest': entry.chapter_digest, 'text_sha256': entry.text_sha256,
                'file_sha256': _sha(raw),
            })
        manifest = {
            'version': 1, 'status': 'complete', **identity,
            'index_sha256': expected_index_sha256,
            'header_json_b64': base64.b64encode(index.header_json).decode('ascii'),
            'header_sha256': _sha(index.header_json),
            'cast_json_b64': base64.b64encode(index.cast_json).decode('ascii'),
            'chapters': manifest_chapters,
        }
        manifest['manifest_sha256'] = chapter_local_index.object_sha256(manifest)
        _write_exclusive(building / 'manifest.json', _json(manifest))
        _fsync_dir(building / 'chapters')
        _fsync_dir(building)
        try:
            os.rename(building, target)
        except OSError as exc:
            raise ChapterIndexStoreError('atomic chapter index workspace publish failed') from exc
        _fsync_dir(parent)
        return manifest


def _read_regular(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ChapterIndexStoreError('chapter index file is missing, linked, or not regular')
    if path.stat().st_size > max_bytes:
        raise ChapterIndexStoreError('chapter index file exceeds supported size')
    return path.read_bytes()


def restore_chapter(workspace: str | Path, chapter_id: str, *,
                    trusted_snapshot: Mapping[str, Any],
                    expected_index_sha256: str) -> dict[str, Any]:
    """Read manifest plus the selected chapter only and rebuild its local slice."""
    identity = _trusted_snapshot(trusted_snapshot)
    if not _is_hash(expected_index_sha256):
        raise ValueError('expected_index_sha256 is invalid')
    if not isinstance(chapter_id, str) or not chapter_id:
        raise ValueError('chapter_id must be a nonempty string')
    raw_root = Path(workspace).expanduser()
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise ChapterIndexStoreError('chapter index workspace is missing or linked')
    root = raw_root.resolve(strict=True)
    parent = root.parent
    building = _building_path(root, expected_index_sha256)
    lock_path = parent / f'.{root.name}.lock'
    with _process_lock(lock_path):
        if _building_residues(root):
            raise ChapterIndexStoreError('incomplete chapter index build exists')
        manifest_raw = _read_regular(root / 'manifest.json', max_bytes=64 * 1024 * 1024)
        try:
            manifest = json.loads(manifest_raw.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ChapterIndexStoreError('chapter index manifest is invalid') from exc
        if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
            raise ChapterIndexStoreError('chapter index manifest fields do not match contract')
        unsigned = {key: value for key, value in manifest.items() if key != 'manifest_sha256'}
        if (manifest.get('version') != 1 or manifest.get('status') != 'complete'
                or chapter_local_index.object_sha256(unsigned) != manifest.get('manifest_sha256')):
            raise ChapterIndexStoreError('manifest is incomplete or its local hash changed')
        if any(manifest.get(key) != value for key, value in identity.items()):
            raise ChapterIndexStoreError('manifest identity differs from trusted snapshot')
        if manifest.get('index_sha256') != expected_index_sha256:
            raise ChapterIndexStoreError('manifest index digest differs from trusted expected digest')
        if not isinstance(manifest.get('chapters'), list) or not manifest['chapters'] or len(manifest['chapters']) > _MAX_CHAPTERS:
            raise ChapterIndexStoreError('manifest chapter list is invalid')
        try:
            header_json = base64.b64decode(manifest['header_json_b64'], validate=True)
            cast_json = base64.b64decode(manifest['cast_json_b64'], validate=True)
        except (ValueError, TypeError) as exc:
            raise ChapterIndexStoreError('manifest header/cast payload is invalid') from exc
        if _sha(header_json) != manifest['header_sha256'] or _sha(cast_json) != identity['cast_sha256']:
            raise ChapterIndexStoreError('manifest header or cast hash mismatch')
        try:
            header = json.loads(header_json.decode('utf-8'))
            cast = json.loads(cast_json.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ChapterIndexStoreError('manifest header/cast JSON is invalid') from exc
        if not isinstance(header, dict) or header.get('type') != 'huaben' or not isinstance(cast, dict):
            raise ChapterIndexStoreError('manifest header or cast shape is invalid')

        chapter_records = manifest['chapters']
        seen = set(); expected_start = 0; found = None
        for item in chapter_records:
            if not isinstance(item, dict) or set(item) != {
                    'chapter_id', 'file', 'original_start', 'original_end',
                    'chapter_digest', 'text_sha256', 'file_sha256'}:
                raise ChapterIndexStoreError('manifest chapter record fields are invalid')
            ident = item['chapter_id']; relative = item['file']
            expected_file = f'chapters/{_chapter_filename(ident)}'
            if (not isinstance(ident, str) or not ident or ident in seen or relative != expected_file
                    or type(item['original_start']) is not int or type(item['original_end']) is not int
                    or item['original_start'] != expected_start or item['original_end'] <= item['original_start']
                    or any(not _is_hash(item[key]) for key in ('chapter_digest', 'text_sha256', 'file_sha256'))):
                raise ChapterIndexStoreError('manifest chapter IDs, paths, ranges or hashes are invalid')
            seen.add(ident); expected_start = item['original_end']
            if ident == chapter_id:
                found = item
        if found is None:
            raise ChapterIndexStoreError('requested chapter is missing')
        summary = _index_summary(identity, manifest['header_sha256'], chapter_records)
        if chapter_local_index.object_sha256(summary) != expected_index_sha256:
            raise ChapterIndexStoreError('manifest chapter map does not match trusted index digest')

        chapter_path = root / found['file']
        if (chapter_path.parent.is_symlink() or chapter_path.parent.resolve(strict=True) != root / 'chapters'):
            raise ChapterIndexStoreError('chapter directory is linked or escaped workspace')
        record_raw = _read_regular(chapter_path, max_bytes=_MAX_CHAPTER_FILE_BYTES)
        if _sha(record_raw) != found['file_sha256']:
            raise ChapterIndexStoreError('selected chapter file hash mismatch')
        try:
            record = json.loads(record_raw.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ChapterIndexStoreError('selected chapter file is invalid JSON') from exc
        if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
            raise ChapterIndexStoreError('selected chapter fields do not match contract')
        for key, value in identity.items():
            if record.get(key) != value:
                raise ChapterIndexStoreError('selected chapter identity differs from trusted snapshot')
        if (record.get('version') != 1 or record.get('index_sha256') != expected_index_sha256
                or record.get('chapter_id') != chapter_id
                or record.get('original_start') != found['original_start']
                or record.get('original_end') != found['original_end']
                or record.get('chapter_digest') != found['chapter_digest']
                or record.get('text_sha256_local') != found['text_sha256']
                or not isinstance(record.get('rows'), list) or not isinstance(record.get('text'), str)):
            raise ChapterIndexStoreError('selected chapter metadata differs from manifest')
        rows_json = chapter_local_index._json(record['rows'])
        if (_sha(rows_json) != record['chapter_digest']
                or _sha(record['text'].encode('utf-8')) != record['text_sha256_local']
                or record['original_end'] - record['original_start'] != len(record['text'])):
            raise ChapterIndexStoreError('selected chapter rows, text, or source range changed')

        entries = []
        for item in chapter_records:
            if item['chapter_id'] == chapter_id:
                entries.append(chapter_local_index.ChapterEntry(
                    chapter_id, item['original_start'], item['original_end'], record['text'],
                    rows_json, item['chapter_digest'], item['text_sha256']))
            else:
                entries.append(chapter_local_index.ChapterEntry(
                    item['chapter_id'], item['original_start'], item['original_end'], '',
                    b'', item['chapter_digest'], item['text_sha256']))
        index = chapter_local_index.ChapterIndex(
            identity['book_id'], identity['source_sha256'], identity['text_sha256'],
            identity['script_sha256'], identity['cast_sha256'], expected_index_sha256,
            header_json, cast_json, tuple(entries))
        return chapter_local_index.extract_chapter(
            index, chapter_id, current_snapshot=identity,
            expected_index_sha256=expected_index_sha256)
