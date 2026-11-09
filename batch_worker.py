"""Persistent chapter batch runner. No listener; invoked by a detached local process.

The caller acquires WD/.console.lock and passes its open descriptor to run().
The descriptor must also be inherited by backend.run's subprocesses.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import uuid
from datetime import datetime, timezone
from render_queue import RenderQueue, _FileLock


def _now():
    return datetime.now(timezone.utc).isoformat()


def _paths(backend):
    state = Path(backend.WD).resolve() / 'state'
    return state / 'chapter-batch.json', state / 'chapter-batch.lock'


def _load(path, backend):
    data = json.loads(path.read_text(encoding='utf-8'))
    if (data.get('version') != 1 or data.get('book_id') != backend.BOOK_ID
            or data.get('workdir') != str(Path(backend.WD).resolve())):
        raise ValueError('批次书籍或路径不匹配。')
    return data


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists(): tmp.unlink()


def _config(backend, speed):
    # Match the inputs used by backend.render's fingerprint, including voice assets.
    h = hashlib.sha256((backend.stamp() + '|' + str(speed) + '|console-render-v1').encode())
    h.update((backend.WD / 'state/ingest.json').read_bytes())
    cast = backend.load(backend.WD / 'cast.json')
    import re
    for voice in sorted({x.get('voice', '') for x in [cast.get('narrator', {}), *cast.get('characters', {}).values()]}):
        if not voice.startswith('library:zh/'): continue
        ident = voice.removeprefix('library:zh/')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', ident): raise ValueError('音色编号无效。')
        for ext in ('.json', '.npy'):
            path = backend.ROOT / 'source/src/huashuo/voices/zh' / (ident + ext)
            if path.is_file(): h.update(path.read_bytes())
    return h.hexdigest()


def _check_lock(backend, fd):
    import fcntl
    lock_path = Path(backend.WD) / '.console.lock'
    actual, expected = os.fstat(fd), os.stat(lock_path)
    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError('传入的书级锁不属于当前书籍。')
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def create(backend, chapters, speed, lock_fd):
    """Create a new batch while the caller holds the inherited book lock."""
    _check_lock(backend, lock_fd)
    speed = backend.playback_speed({'speed': speed})
    if not isinstance(chapters, list) or not chapters or len(set(chapters)) != len(chapters):
        raise ValueError('章节列表为空或重复。')
    queue = RenderQueue(backend.WD, backend.BOOK_ID)
    proofs = {chapter: queue._proof(chapter) for chapter in chapters}
    config = _config(backend, speed)
    path, lock = _paths(backend)
    with _FileLock(lock):
        if path.exists():
            prior = _load(path, backend)
            if prior.get('status') not in ('complete', 'failed', 'stopped'):
                raise ValueError('已有未完成批次，请先处理。')
        data = {'version': 1, 'id': uuid.uuid4().hex, 'book_id': backend.BOOK_ID,
                'workdir': str(Path(backend.WD).resolve()), 'speed': speed,
                'config_fingerprint': config, 'status': 'queued', 'stop_requested': False,
                'chapters': [{'id': c, 'script_digest': proofs[c], 'status': 'queued'} for c in chapters],
                'created_at': _now(), 'updated_at': _now()}
        _save(path, data)
    return data


def status(backend):
    path, lock = _paths(backend)
    with _FileLock(lock): return _load(path, backend) if path.exists() else None


def request_stop(backend):
    path, lock = _paths(backend)
    with _FileLock(lock):
        data = _load(path, backend)
        if data['status'] in ('queued', 'running', 'interrupted'):
            data['stop_requested'] = True; data['updated_at'] = _now(); _save(path, data)
        return data


def run(backend, lock_fd, resume=False):
    """Run with the already-acquired book lock; explicit resume after interruption."""
    _check_lock(backend, lock_fd)
    backend.BOOK_LOCK_FD = lock_fd
    path, lock = _paths(backend)
    queue = RenderQueue(backend.WD, backend.BOOK_ID)
    with _FileLock(lock):
        data = _load(path, backend)
        orphaned = any(job.get('status') == 'running' for job in queue.get().values())
        if data['status'] in ('running', 'interrupted') or orphaned:
            queue.recover(book_lock_held=True)
            for item in data['chapters']:
                if item['status'] == 'running': item['status'] = 'interrupted'
            data['status'] = 'interrupted'; data['updated_at'] = _now(); _save(path, data)
            if not resume: raise ValueError('上次批次中断；需要明确确认恢复。')
        elif data['status'] == 'stopped':
            if not resume: raise ValueError('批次已停止；需要明确确认恢复。')
        elif data['status'] != 'queued':
            raise ValueError('该批次不可运行。')
        if resume: data['stop_requested'] = False
        data['status'] = 'running'; data['updated_at'] = _now(); _save(path, data)
    try:
        for index in range(len(data['chapters'])):
            with _FileLock(lock):
                data = _load(path, backend); item = data['chapters'][index]
                if data['stop_requested']:
                    data['status'] = 'stopped'; data['updated_at'] = _now(); _save(path, data); return data
                if _config(backend, data['speed']) != data['config_fingerprint']:
                    raise ValueError('批次配置已改变，请重新建立批次。')
                if queue._proof(item['id']) != item['script_digest']:
                    raise ValueError('章节审核内容已改变。')
                if item['status'] == 'complete':
                    job = queue.get(item['id'])
                    if job.get('config_fingerprint') != data['config_fingerprint'] or not queue.verify_output(item['id']):
                        raise ValueError('已完成章节的配置或输出校验失败。')
                    continue
                if item['status'] not in ('queued', 'interrupted'):
                    raise ValueError('章节状态不允许继续。')
                item['status'] = 'running'; item['started_at'] = _now(); data['updated_at'] = _now(); _save(path, data)
            # A prior completed queue job can be adopted only after proof, config and output checks.
            existing = queue.get().get(item['id'])
            if existing and existing.get('status') == 'complete' and not (
                    existing.get('script_digest') == item['script_digest']
                    and existing.get('config_fingerprint') == data['config_fingerprint']
                    and queue.verify_output(item['id'])):
                raise ValueError('已有完成结果与批次不匹配；请先人工检查。')
            if not (existing and existing.get('status') == 'complete'
                    and existing.get('script_digest') == item['script_digest']
                    and existing.get('config_fingerprint') == data['config_fingerprint']
                    and queue.verify_output(item['id'])):
                backend.render({'chapter': item['id'], 'speed': data['speed']})
            job = queue.get(item['id'])
            if (job.get('status') != 'complete' or job.get('config_fingerprint') != data['config_fingerprint']
                    or not queue.verify_output(item['id']) or _config(backend, data['speed']) != data['config_fingerprint']):
                raise ValueError('章节结果或配置校验失败。')
            with _FileLock(lock):
                data = _load(path, backend); item = data['chapters'][index]
                item['status'] = 'complete'; item['completed_at'] = _now(); data['updated_at'] = _now(); _save(path, data)
        with _FileLock(lock):
            data = _load(path, backend); data['status'] = 'complete'; data['updated_at'] = _now(); _save(path, data)
        return data
    except Exception as exc:
        with _FileLock(lock):
            data = _load(path, backend)
            for item in data['chapters']:
                if item['status'] == 'running': item['status'] = 'failed'; item['error'] = str(exc)[:1000]
            data['status'] = 'failed'; data['error'] = str(exc)[:1000]; data['updated_at'] = _now(); _save(path, data)
        raise


if __name__ == '__main__':
    import argparse
    import voice_console_backend as backend
    parser = argparse.ArgumentParser()
    parser.add_argument('--book', required=True); parser.add_argument('--lock-fd', required=True, type=int)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    backend.select_book(args.book)
    run(backend, args.lock_fd, args.resume)
