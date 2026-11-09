"""Small, fail-closed resource guards for the macOS MLX renderer."""
from __future__ import annotations

import contextlib
import ctypes
import errno
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

GIB = 1024 ** 3
_LOCK_NAME = ".tts-resource.lock"


def _validate_tree(source: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise ValueError("source must be a plain directory")
    def fail(error: OSError) -> None:
        raise error

    for base, dirs, files in os.walk(source, followlinks=False, onerror=fail):
        for name in dirs + files:
            path = Path(base) / name
            info = path.lstat()
            if path.is_symlink():
                raise ValueError(f"source tree contains a symlink: {path}")
            if name in dirs and not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"source tree contains a non-directory: {path}")
            if name in files and not stat.S_ISREG(info.st_mode):
                raise ValueError(f"source tree contains a non-regular file: {path}")


def clone_model_tree(source: str | os.PathLike, destination: str | os.PathLike) -> Path:
    """Clone a regular-file tree with clonefile(2), never copying as fallback."""
    if sys.platform != "darwin":
        raise OSError("model tree cloning requires macOS clonefile")
    source, destination = Path(source), Path(destination)
    _validate_tree(source)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(errno.EEXIST, "destination already exists", str(destination))

    libc = ctypes.CDLL(None, use_errno=True)
    clonefile = libc.clonefile
    clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
    clonefile.restype = ctypes.c_int

    def clonefile_wrapper(src: str, dst: str) -> None:
        if clonefile(os.fsencode(src), os.fsencode(dst), 0) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), dst)

    return Path(shutil.copytree(source, destination, copy_function=clonefile_wrapper,
                                symlinks=False))


def configure_mlx_limits() -> dict:
    """Apply per-process MLX hints; memory_limit is advisory, wired=0 disables pinning."""
    import mlx.core as mx

    limits = {"cache_limit": 256 * 1024 ** 2,
              "memory_limit": 6 * GIB,
              "wired_limit": 0}
    previous = {
        "cache_limit": mx.set_cache_limit(limits["cache_limit"]),
        "memory_limit": mx.set_memory_limit(limits["memory_limit"]),
        "wired_limit": mx.set_wired_limit(limits["wired_limit"]),
    }
    return {"limits": limits, "previous": previous,
            "memory_limit_is_guideline": True}


def _free_memory_percent() -> int:
    result = subprocess.run(["/usr/bin/memory_pressure", "-Q"], check=True,
                            capture_output=True, text=True, timeout=10)
    match = re.search(r"free percentage\s*:\s*(\d+)%", result.stdout, re.I)
    if not match:
        raise RuntimeError("cannot read free-memory percentage from memory_pressure -Q")
    return int(match.group(1))


@contextlib.contextmanager
def render_slot(root: str | os.PathLike):
    """Hold the single-render slot after checking disk and memory headroom."""
    import fcntl

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock = open(root / _LOCK_NAME, "a+b")
    wakeguard = None
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another TTS render holds the resource slot") from None
        free_disk = shutil.disk_usage(root).free
        if free_disk < 16 * GIB:
            raise RuntimeError(f"render refused: free disk {free_disk} bytes is below 16 GiB")
        free_memory_percent = _free_memory_percent()
        if free_memory_percent < 50:
            raise RuntimeError(f"render refused: free memory is {free_memory_percent}%, below 50%")
        try:
            wakeguard = subprocess.Popen(
                ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise RuntimeError(f"render refused: cannot start caffeinate idle-sleep guard: {exc}") from exc
        yield {"free_disk_bytes": free_disk,
               "free_memory_percent": free_memory_percent,
               "caffeinate_pid": wakeguard.pid,
               "lock_file": str(root / _LOCK_NAME)}
    finally:
        try:
            if wakeguard is not None:
                wakeguard.terminate()
                try:
                    wakeguard.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    wakeguard.kill()
                    wakeguard.wait()
        finally:
            try:
                if not lock.closed:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()
