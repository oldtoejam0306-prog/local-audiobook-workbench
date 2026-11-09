"""Read-only, lock-coordinated snapshot of a local Huashuo book workspace.

The reader uses existing book initialization and workdir console locks and
creates no lock files. It hashes files read under those locks, detects ordinary
file replacement/change, and returns an in-memory identity for the chapter index.
It cannot authenticate book.json or caller trust; script/cast hashes are
computed at read time, and header.source is recorded as intermediate provenance
without resolving its path.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from dataclasses import dataclass
from collections.abc import Iterator
from typing import Any

import chapter_local_index

_SHA256 = re.compile(r'^[0-9a-f]{64}$')


_MAX_BYTES = {
    'book_manifest': 2 * 1024 * 1024,
    'source_docx': 256 * 1024 * 1024,
    'text': 128 * 1024 * 1024,
    'script': 256 * 1024 * 1024,
    'cast': 32 * 1024 * 1024,
}


class WorkspaceSnapshotError(RuntimeError):
    pass


class WorkspaceSnapshotBusy(WorkspaceSnapshotError):
    pass


class WorkspaceSnapshotChanged(WorkspaceSnapshotError):
    pass


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_reparse(info) -> bool:
    return bool(getattr(info, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _dir_identity(info) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _file_identity(info) -> tuple[int, int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_nlink, info.st_size,
            getattr(info, 'st_mtime_ns', int(info.st_mtime * 1e9)),
            getattr(info, 'st_ctime_ns', int(info.st_ctime * 1e9)))


def _plain_directory(path: str | Path, label: str) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise WorkspaceSnapshotError(f'{label} must be an explicit absolute path')
    current = Path(raw.anchor)
    try:
        for component in raw.parts[1:]:
            current = current / component
            info = os.stat(current, follow_symlinks=False)
            if (stat.S_ISLNK(info.st_mode) or _is_reparse(info)
                    or not stat.S_ISDIR(info.st_mode)):
                raise WorkspaceSnapshotError(f'{label} path contains a link or non-directory')
    except OSError as exc:
        raise WorkspaceSnapshotError(f'{label} path is missing or inaccessible') from exc
    return current.resolve(strict=True)


@contextlib.contextmanager
def _lock_existing(path: Path) -> Iterator[None]:
    """Take a nonblocking exclusive lock on an existing lock file without writing it."""
    if path.is_symlink():
        raise WorkspaceSnapshotError('workspace lock may not be a symlink')
    try:
        parent_info = os.stat(path.parent, follow_symlinks=False)
        path_info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceSnapshotError('required existing workspace lock is unavailable') from exc
    if not stat.S_ISDIR(parent_info.st_mode) or _is_reparse(parent_info):
        raise WorkspaceSnapshotError('workspace lock parent is not a plain directory')
    if (not stat.S_ISREG(path_info.st_mode) or stat.S_ISLNK(path_info.st_mode)
            or _is_reparse(path_info) or path_info.st_nlink != 1):
        raise WorkspaceSnapshotError('workspace lock must be a single-link regular file')
    flags = os.O_RDWR
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkspaceSnapshotError('could not open existing workspace lock read-only-safe') from exc
    stream = os.fdopen(descriptor, 'r+b', buffering=0)
    try:
        parent_after_open = os.stat(path.parent, follow_symlinks=False)
        handle_info = os.fstat(stream.fileno())
        after_open = os.stat(path, follow_symlinks=False)
        if (_file_identity(handle_info) != _file_identity(after_open)
                or _dir_identity(parent_info) != _dir_identity(parent_after_open)
                or handle_info.st_nlink != 1):
            raise WorkspaceSnapshotChanged('workspace lock path or handle identity changed')
        if os.name == 'nt':
            import msvcrt
            try:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise WorkspaceSnapshotBusy('workspace lock is busy or cannot be acquired without writes') from exc
            try:
                locked_handle = os.fstat(stream.fileno())
                locked_path = os.stat(path, follow_symlinks=False)
                locked_parent = os.stat(path.parent, follow_symlinks=False)
                if (_file_identity(locked_handle) != _file_identity(locked_path)
                        or _dir_identity(parent_info) != _dir_identity(locked_parent)):
                    raise WorkspaceSnapshotChanged('workspace lock identity changed after acquisition')
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise WorkspaceSnapshotBusy('workspace lock is busy') from exc
            try:
                locked_handle = os.fstat(stream.fileno())
                locked_path = os.stat(path, follow_symlinks=False)
                locked_parent = os.stat(path.parent, follow_symlinks=False)
                if (_file_identity(locked_handle) != _file_identity(locked_path)
                        or _dir_identity(parent_info) != _dir_identity(locked_parent)):
                    raise WorkspaceSnapshotChanged('workspace lock identity changed after acquisition')
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


@contextlib.contextmanager
def _book_level_locks(book_root: Path, workdir: Path) -> Iterator[None]:
    locks = sorted((book_root / '.initialize.lock', workdir / '.console.lock'),
                   key=lambda path: str(path))
    with contextlib.ExitStack() as stack:
        for lock in locks:
            stack.enter_context(_lock_existing(lock))
        yield


def _read_regular(path: Path, *, root: Path, label: str) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    if path.is_symlink():
        raise WorkspaceSnapshotError(f'{label} may not be a symlink')
    try:
        path_info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceSnapshotError(f'{label} is missing or inaccessible') from exc
    if (not stat.S_ISREG(path_info.st_mode) or _is_reparse(path_info)
            or path_info.st_nlink != 1):
        raise WorkspaceSnapshotError(f'{label} must be a single-link regular file')
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceSnapshotError(f'{label} cannot be resolved safely') from exc
    if not resolved.is_relative_to(root):
        raise WorkspaceSnapshotError(f'{label} escapes the explicit book root')
    before = _file_identity(path_info)
    if path_info.st_size > _MAX_BYTES[label]:
        raise WorkspaceSnapshotError(f'{label} exceeds the snapshot read limit')
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            handle_before = os.fstat(stream.fileno())
            if _file_identity(handle_before) != before:
                raise WorkspaceSnapshotChanged(f'{label} changed before read')
            raw = stream.read(_MAX_BYTES[label] + 1)
            handle_after = os.fstat(stream.fileno())
        path_after = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceSnapshotError(f'{label} read failed') from exc
    after = _file_identity(handle_after)
    if (len(raw) > _MAX_BYTES[label] or before != after
            or _file_identity(path_after) != after or not resolved.is_relative_to(root)):
        raise WorkspaceSnapshotChanged(f'{label} changed during read')
    return raw, after


@dataclass(frozen=True)
class WorkspaceSnapshot:
    book_id: str
    source_bytes: bytes
    normalized_text: str
    script_rows_json: bytes
    cast_json: bytes
    source_sha256: str
    text_sha256: str
    script_sha256: str
    cast_sha256: str
    provenance_json: bytes

    @property
    def script_rows(self) -> list[dict[str, Any]]:
        return json.loads(self.script_rows_json.decode('utf-8'))

    @property
    def cast(self) -> dict[str, Any]:
        return json.loads(self.cast_json.decode('utf-8'))

    @property
    def persisted_index_snapshot(self) -> dict[str, str]:
        return {'book_id': self.book_id, 'source_sha256': self.source_sha256,
                'text_sha256': self.text_sha256, 'script_sha256': self.script_sha256,
                'cast_sha256': self.cast_sha256}

    @property
    def provenance(self) -> dict[str, Any]:
        return json.loads(self.provenance_json.decode('utf-8'))


def read_workspace_snapshot(*, book_root: str | Path, workdir: str | Path,
                            expected_book_id: str) -> WorkspaceSnapshot:
    """Read the existing DOCX/text/script/cast under existing book/workdir locks.

    Existing lock files are opened and locked without creating or writing them.
    Source/text hashes are checked against book.json and the script header.
    Script/cast hashes are computed at this read time, not independently
    authenticated. Header source paths are recorded only as hashes/booleans and
    are never resolved or opened.
    """
    if not isinstance(expected_book_id, str) or not expected_book_id.strip():
        raise ValueError('expected_book_id must be nonempty')
    root = _plain_directory(book_root, 'book_root')
    wd = _plain_directory(workdir, 'workdir')
    expected_workdir = root / 'work.huashuo'
    if wd != expected_workdir:
        raise WorkspaceSnapshotError('workdir must be the book root work.huashuo directory')
    paths = {
        'book_manifest': root / 'book.json',
        'source_docx': root / 'original.docx',
        'text': wd / 'text.txt',
        'script': wd / 'script.huaben.jsonl',
        'cast': wd / 'cast.json',
    }
    with _book_level_locks(root, wd):
        fingerprints = {}
        contents = {}
        try:
            for name, path in paths.items():
                contents[name], fingerprints[name] = _read_regular(path, root=root, label=name)
        except WorkspaceSnapshotError:
            raise
        except OSError as exc:
            raise WorkspaceSnapshotError('workspace snapshot read was interrupted') from exc
        try:
            manifest = json.loads(contents['book_manifest'].decode('utf-8'))
            text = contents['text'].decode('utf-8', errors='strict')
            lines = contents['script'].decode('utf-8', errors='strict').splitlines()
            rows = [json.loads(line) for line in lines if line.strip()]
            cast = json.loads(contents['cast'].decode('utf-8', errors='strict'))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise WorkspaceSnapshotError('workspace JSON or UTF-8 decoding failed') from exc
        if not isinstance(manifest, dict) or manifest.get('book_id') != expected_book_id:
            raise WorkspaceSnapshotError('book.json identity differs from expected book_id')
        if manifest.get('format') != '.docx':
            raise WorkspaceSnapshotError('book.json is not a DOCX import manifest')
        source_sha = _sha(contents['source_docx'])
        if (not isinstance(manifest.get('source_sha256'), str)
                or not _SHA256.fullmatch(manifest['source_sha256'])
                or manifest.get('source_sha256') != source_sha):
            raise WorkspaceSnapshotError('original.docx hash differs from book.json')
        text_sha = _sha(contents['text'])
        if not rows or not isinstance(rows[0], dict) or rows[0].get('type') != 'huaben':
            raise WorkspaceSnapshotError('script header is missing or invalid')
        header = rows[0]
        if (header.get('text') != 'text.txt' or header.get('text_sha256') != text_sha
                or '\r' in text):
            raise WorkspaceSnapshotError('text.txt differs from script header or is not LF-normalized')
        source_meta = header.get('source')
        header_source = None
        if source_meta is not None:
            if (not isinstance(source_meta, dict) or set(source_meta) != {'format', 'path', 'sha256'}
                    or not isinstance(source_meta.get('format'), str)
                    or not isinstance(source_meta.get('path'), str)
                    or not isinstance(source_meta.get('sha256'), str)
                    or not _SHA256.fullmatch(source_meta['sha256'])):
                raise WorkspaceSnapshotError('script header source metadata shape is invalid')
            source_path = source_meta['path']
            if ('://' in source_path or PurePosixPath(source_path).is_absolute()
                    or PureWindowsPath(source_path).is_absolute() or PureWindowsPath(source_path).drive):
                raise WorkspaceSnapshotError('header source must be relative metadata and is never resolved')
            header_source = {
                'format': source_meta['format'],
                'sha256': source_meta['sha256'],
                'sha256_matches_original_docx': source_meta['sha256'] == source_sha,
                'sha256_matches_text_file': source_meta['sha256'] == text_sha,
                'path_sha256': _sha(source_path.encode('utf-8')),
                'path_has_parent_component': '..' in PurePosixPath(source_path).parts,
                'path_resolved': False,
            }
        script_sha = chapter_local_index.object_sha256(rows)
        cast_sha = chapter_local_index.object_sha256(cast)
        for name, path in paths.items():
            current = os.stat(path, follow_symlinks=False)
            if _file_identity(current) != fingerprints[name]:
                raise WorkspaceSnapshotChanged(f'{name} changed during workspace snapshot')
        provenance = {
            'book_manifest_source_sha256_matches_original_docx': True,
            'script_header_text_sha256_matches_workdir_text': True,
            'script_sha256_basis': 'canonical hash computed during this locked read; not historical authentication',
            'cast_sha256_basis': 'canonical hash computed during this locked read; not historical authentication',
            'source_header_intermediate_metadata': header_source,
            'source_header_sha256_is_not_assumed_to_equal_original_docx': True,
            'lock_paths': ['.initialize.lock', str(wd.relative_to(root) / '.console.lock')],
            'lock_policy': 'existing nonblocking exclusive cross-process locks; no lock file creation or write',
            'external_parser_provenance_authenticated': False,
        }
        return WorkspaceSnapshot(
            expected_book_id, contents['source_docx'], text, chapter_local_index._json(rows),
            chapter_local_index._json(cast), source_sha, text_sha, script_sha, cast_sha,
            chapter_local_index._json(provenance))
