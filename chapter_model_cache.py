"""APFS-only model COW cloning with confined Hugging Face file-link support."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
from pathlib import Path
import shutil
import stat
import sys


def _resolve_file_link(path: Path, allowed_root: Path | None) -> Path:
    if not path.is_symlink(): return path
    if allowed_root is None or path.is_dir():
        raise ValueError(f'disallowed model link: {path}')
    target = path.resolve(strict=True); root = allowed_root.resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_file() or target.is_symlink():
        raise ValueError(f'model file link escapes configured cache root: {path}')
    return target


def _validate_tree(source: Path, allowed_file_link_root: Path | None) -> None:
    if source.is_symlink() or not source.is_dir(): raise ValueError('model source must be a plain directory')
    allowed_root = allowed_file_link_root.resolve(strict=True) if allowed_file_link_root else None
    def fail(error): raise error
    for base, dirs, files in os.walk(source, followlinks=False, onerror=fail):
        dirs.sort(); files.sort()
        for name in dirs:
            path = Path(base) / name; info = path.lstat()
            if stat.S_ISLNK(info.st_mode): raise ValueError(f'model directory link refused: {path}')
            if not stat.S_ISDIR(info.st_mode): raise ValueError(f'model tree contains a non-directory: {path}')
        for name in files:
            path = Path(base) / name; info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                _resolve_file_link(path, allowed_root)
            elif not stat.S_ISREG(info.st_mode):
                raise ValueError(f'model tree contains a non-regular file: {path}')


def model_tree_inventory(source: str | os.PathLike, *,
                         allowed_file_link_root: str | os.PathLike | None = None) -> dict[str, str]:
    source = Path(source).expanduser().resolve(strict=True)
    allowed = Path(allowed_file_link_root).expanduser().resolve(strict=True) if allowed_file_link_root else None
    _validate_tree(source, allowed)
    result = {}
    for base, dirs, files in os.walk(source, followlinks=False):
        dirs.sort(); files.sort()
        for name in files:
            entry = Path(base) / name; actual = _resolve_file_link(entry, allowed)
            digest = hashlib.sha256()
            with actual.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''): digest.update(block)
            result[entry.relative_to(source).as_posix()] = digest.hexdigest()
    return result


def clone_model_tree(source: str | os.PathLike, destination: str | os.PathLike, *,
                     allowed_file_link_root: str | os.PathLike | None = None) -> Path:
    """Use clonefile(2) only; confined file links are cloned as regular files."""
    if sys.platform != 'darwin': raise OSError('model COW clone requires macOS APFS clonefile')
    source = Path(source).expanduser(); destination = Path(destination).expanduser()
    allowed = Path(allowed_file_link_root).expanduser().resolve(strict=True) if allowed_file_link_root else None
    _validate_tree(source, allowed)
    if destination.exists() or destination.is_symlink(): raise FileExistsError(errno.EEXIST, 'destination exists', str(destination))
    libc = ctypes.CDLL(None, use_errno=True); clonefile = libc.clonefile
    clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int); clonefile.restype = ctypes.c_int
    def clone_one(src, dst):
        resolved = _resolve_file_link(Path(src), allowed)
        if clonefile(os.fsencode(resolved), os.fsencode(dst), 0) != 0:
            error = ctypes.get_errno(); raise OSError(error, os.strerror(error), dst)
    return Path(shutil.copytree(source, destination, copy_function=clone_one, symlinks=False))
