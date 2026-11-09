"""Read-only, offline inventory for a local HF model snapshot.

Pass an explicit trusted repository root and snapshot directory beneath it. HF
file symlinks may point into that repository (e.g. ../.. /blobs), never outside.
This inventory authenticates no origin, proves no model quality, and does not
prove that any runner will load these files. Revalidate at use under a suitable
OS sandbox; file hashes alone cannot close path replacement races.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


class ModelInventoryError(ValueError):
    pass


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ModelInventoryError('duplicate JSON key in model declaration')
        out[key] = value
    return out


def _local_file(root: Path, path: Path) -> Path:
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ModelInventoryError(f'missing or invalid file: {path}') from exc
    if not real.is_relative_to(root) or not real.is_file():
        raise ModelInventoryError(f'file escapes local repository or is not regular: {path}')
    return real


def _hash_file(path: Path, *, chunk_size: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    try:
        with path.open('rb') as handle:
            before = os.fstat(handle.fileno())
            size = 0
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or size != after.st_size:
            raise ModelInventoryError(f'file changed while hashing: {path}')
        return digest.hexdigest(), size
    except OSError as exc:
        raise ModelInventoryError(f'cannot read model file: {path}') from exc


def _declared_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ModelInventoryError('invalid declared shard path')
    uri = urlsplit(value)
    path = PurePosixPath(value)
    if uri.scheme or uri.netloc or value.startswith('/') or any(part in ('..', '.') for part in value.split('/')):
        raise ModelInventoryError('remote or escaping declared shard path')
    if not value.endswith('.safetensors'):
        raise ModelInventoryError('declared shard is not a safetensors file')
    return path


def inventory_model(repository_root: str | Path, snapshot_root: str | Path, *, chunk_size: int = 1024 * 1024) -> dict:
    """Return deterministic per-snapshot-file SHA-256 and validated shard indexes.

    `repository_root` is a caller-chosen trust boundary, normally the HF
    models--... repository directory, and must contain the snapshot and blobs.
    Direct model folders can be passed for both arguments. Every snapshot file,
    including tokenizer/config and auxiliary data, is included; directory links
    are rejected. Regular file symlinks may resolve inside the repository.
    """
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size < 1:
        raise ModelInventoryError('chunk_size must be a positive integer')
    for arg in (repository_root, snapshot_root):
        if not isinstance(arg, (str, Path)) or '://' in str(arg):
            raise ModelInventoryError('local paths are required')
    try:
        repo = Path(repository_root).expanduser().resolve(strict=True)
        snapshot = Path(snapshot_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ModelInventoryError('repository or snapshot is unavailable') from exc
    if not repo.is_dir() or not snapshot.is_dir() or not snapshot.is_relative_to(repo):
        raise ModelInventoryError('snapshot must be inside repository root')
    files = {}
    def fail(error):
        raise ModelInventoryError('cannot inspect snapshot') from error
    for folder, dirs, names in os.walk(snapshot, followlinks=False, onerror=fail):
        base = Path(folder)
        for name in dirs:
            directory = base / name
            if directory.is_symlink():
                raise ModelInventoryError('directory symlinks are not accepted')
        for name in names:
            item = base / name
            rel = item.relative_to(snapshot).as_posix()
            if rel in files:
                raise ModelInventoryError('duplicate snapshot path')
            resolved = _local_file(repo, item)
            digest, size = _hash_file(resolved, chunk_size=chunk_size)
            # Confirm the snapshot link did not change during the read.
            if _local_file(repo, item) != resolved:
                raise ModelInventoryError('file target changed while hashing')
            files[rel] = {'path': rel, 'sha256': digest, 'size_bytes': size,
                          'repository_target': resolved.relative_to(repo).as_posix()}
    if 'config.json' not in files:
        raise ModelInventoryError('model config.json is required')
    if 'tokenizer_config.json' not in files or not any(name in files for name in ('tokenizer.json','vocab.json','tokenizer.model','sentencepiece.bpe.model')):
        raise ModelInventoryError('model tokenizer configuration or vocabulary missing')
    if not any(name.endswith('.safetensors') for name in files):
        raise ModelInventoryError('no safetensors weights found')
    # Parse every configuration/tokenizer JSON as well as every index so malformed
    # declarations do not become part of an apparently successful inventory.
    parsed = {}
    for name in sorted(files):
        if not name.endswith('.json'):
            continue
        path = _local_file(repo, snapshot / name)
        try:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != files[name]['sha256']:
                raise ModelInventoryError(f'JSON changed after hashing: {name}')
            value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique,
                               parse_constant=lambda _: (_ for _ in ()).throw(ModelInventoryError('invalid JSON constant')))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ModelInventoryError(f'invalid JSON declaration: {name}') from exc
        if not isinstance(value, dict):
            raise ModelInventoryError(f'JSON declaration must be an object: {name}')
        parsed[name] = value
    indexes = []
    for name in sorted(parsed):
        if not name.endswith('.safetensors.index.json'):
            continue
        index = parsed[name]
        weights = index.get('weight_map')
        if not isinstance(weights, dict) or not weights:
            raise ModelInventoryError(f'index has no weight_map: {name}')
        shards = set()
        for tensor, shard in weights.items():
            if not isinstance(tensor, str) or not tensor.strip():
                raise ModelInventoryError(f'invalid tensor key: {name}')
            declared = _declared_path(shard)
            rel = (PurePosixPath(name).parent / declared).as_posix()
            if rel not in files:
                raise ModelInventoryError(f'declared shard is missing: {rel}')
            shards.add(rel)
        metadata = index.get('metadata', {})
        if not isinstance(metadata, dict):
            raise ModelInventoryError(f'invalid index metadata: {name}')
        if 'total_size' in metadata and (type(metadata['total_size']) is not int or metadata['total_size'] < 0):
            raise ModelInventoryError(f'invalid index total_size: {name}')
        indexes.append({'path': name, 'tensor_count': len(weights), 'shards': sorted(shards),
                        'declared_total_size': metadata.get('total_size')})
    ordered = [files[name] for name in sorted(files)]
    return {'version': 1, 'files': ordered, 'indexes': indexes,
            'total_size_bytes': sum(row['size_bytes'] for row in ordered),
            'trust': 'local content inventory only; no model quality or runner-use verification'}
