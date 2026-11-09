"""Isolated continuity runner over the existing reviewed-chapter batch worker.

Mac POSIX only. Never imports a book or creates review approvals. The caller must
prepare an isolated self-written book and review it before creating the batch.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import release_config

import batch_worker as batch
from render_queue import RenderQueue

BOOK = 'book-eeeeeeeeeeeeeeeeeeee'


def configure(root):
    root = Path(root).resolve(strict=True)
    expected = release_config.mac_stage_root().resolve(strict=True)
    if root.parent != expected or not re.fullmatch(r'continuity-synthetic-[A-Za-z0-9_-]+', root.name):
        raise ValueError('only a named isolated synthetic experiment directory is allowed')
    fake = (root / '.runtime').resolve(strict=True)
    if fake.parent != root:
        raise ValueError('experiment root is not isolated')
    import voice_console_backend as backend
    backend.ROOT = fake
    backend.PYTHON = Path(sys.executable)
    backend.HUASHUO = root / 'isolated-huashuo' if (root / 'isolated-huashuo').is_file() else release_config.mac_huashuo_cli()
    backend.select_book(BOOK)
    if not Path(backend.WD).resolve().is_relative_to(fake):
        raise ValueError('book workdir outside isolation')
    return backend


def book_lock(backend):
    lock = (Path(backend.WD) / '.console.lock').open('a+b')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        lock.close()
        raise
    return lock


def inspect(backend):
    data = batch.status(backend)
    if data is None:
        return {'batch': None, 'lock_available': None}
    try:
        with book_lock(backend):
            available = True
    except BlockingIOError:
        available = False
    return {'batch': data, 'lock_available': available,
            'note': 'lock availability does not prove ownership or process identity'}


def playback_manifest(backend, ffmpeg):
    data = batch.status(backend)
    if not data:
        raise ValueError('no batch')
    queue = RenderQueue(backend.WD, backend.BOOK_ID)
    current_config = batch._config(backend, data['speed'])
    if current_config != data['config_fingerprint']:
        raise ValueError('batch configuration changed')
    files = []
    for item in data['chapters']:
        if item['status'] != 'complete':
            continue
        ident = item['id']
        if not re.fullmatch(r'c[0-9]+(?:\.p[0-9]+(?:\.[0-9]+)?)?', ident):
            raise ValueError('invalid chapter id')
        job = queue.get(ident)
        expected = (Path(backend.WD) / ('console-output-' + ident + '.wav')).resolve()
        if (queue._proof(ident) != item['script_digest'] or job.get('script_digest') != item['script_digest']
                or job.get('config_fingerprint') != current_config or Path(job['output_path']).resolve() != expected
                or not expected.is_relative_to(Path(backend.WD).resolve()) or not queue.verify_output(ident)):
            raise ValueError('chapter proof, configuration or output mismatch: ' + ident)
        subprocess.run([str(ffmpeg), '-v', 'error', '-xerror', '-err_detect', 'explode',
                        '-i', str(expected), '-f', 'null', '-'], check=True, timeout=300,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        files.append({'chapter_id': ident, 'path': str(expected), 'sha256': job['output_sha256'],
                      'bytes': job['output_size']})
    return {'version': 1, 'book_id': backend.BOOK_ID, 'batch_id': data['id'],
            'batch_status': data['status'], 'chapters': files,
            'scope': 'verified local files; listening quality not assessed'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('action', choices=['create', 'run', 'resume', 'status', 'stop', 'manifest'])
    parser.add_argument('--chapters', nargs='+')
    parser.add_argument('--speed', type=float, default=1.0)
    args = parser.parse_args()
    backend = configure(args.root)
    if args.action == 'status':
        result = inspect(backend)
    elif args.action == 'stop':
        result = batch.request_stop(backend)
    else:
        with book_lock(backend) as lock:
            if args.action == 'create':
                result = batch.create(backend, args.chapters, args.speed, lock.fileno())
            elif args.action in ('run', 'resume'):
                result = batch.run(backend, lock.fileno(), resume=args.action == 'resume')
            else:
                result = playback_manifest(backend, release_config.mac_ffmpeg())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()



