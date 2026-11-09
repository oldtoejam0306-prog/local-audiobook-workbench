"""Durable, renderer-agnostic queue for chapter audio jobs.

This module never starts TTS. Callers must claim a job, perform rendering, then
explicitly complete it with the resulting file. Uncertain/interrupted work is
left for inspection and is never inferred to have succeeded from an output file.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def chapter_digest(rows, chapter_id):
    selected, current = [], None
    for row in rows:
        if row.get('type') == 'chapter':
            current = row.get('id')
        if current == chapter_id:
            selected.append(row)
    if not selected:
        raise ValueError('审核章节不存在。')
    raw = json.dumps(selected, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines()
            if line.strip()]


class _FileLock:
    """Small cross-process exclusive lock (fcntl on POSIX, msvcrt on Windows)."""
    def __init__(self, path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        if os.name == 'nt':
            import msvcrt
            self.file.seek(0)
            if not self.file.read(1):
                self.file.seek(0); self.file.write(b'0'); self.file.flush()
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        if os.name == 'nt':
            import msvcrt
            self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


class RenderQueue:
    """Persistent queue rooted at a Huashuo work directory.

    The caller must serialize render/recovery against the book using its
    book-level lock. In particular, pass ``book_lock_held=True`` to recover()
    only while that lock is held; the queue lock protects queue state only.
    """
    def __init__(self, workdir, book_id, script_path=None, reviews_path=None):
        self.workdir = Path(workdir).resolve()
        self.book_id = str(book_id)
        self.script = Path(script_path).resolve() if script_path else self.workdir / 'script.huaben.jsonl'
        self.reviews = Path(reviews_path).resolve() if reviews_path else self.workdir / 'state/chapter-reviews.json'
        self.state_dir = self.workdir / 'state'
        self.state_path = self.state_dir / 'render-queue.json'
        self.lock_path = self.state_dir / 'render-queue.lock'
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked(self):
        with self._thread_lock, _FileLock(self.lock_path):
            yield

    def _load(self):
        if not self.state_path.exists():
            return {'version': 1, 'book_id': self.book_id, 'jobs': {}}
        data = json.loads(self.state_path.read_text(encoding='utf-8'))
        if data.get('version') != 1 or data.get('book_id') != self.book_id or not isinstance(data.get('jobs'), dict):
            raise ValueError('队列状态格式或书籍编号不匹配。')
        return data

    def _save(self, data):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + '.' + uuid.uuid4().hex + '.tmp')
        with tmp.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, self.state_path)

    def _proof(self, chapter_id):
        if not self.script.is_file() or not self.reviews.is_file():
            raise ValueError('缺少话本或章节审核凭证。')
        records = json.loads(self.reviews.read_text(encoding='utf-8-sig'))
        proof = records.get(chapter_id)
        actual = chapter_digest(_read_rows(self.script), chapter_id)
        if not isinstance(proof, dict) or proof.get('book_id') != self.book_id or proof.get('script_digest') != actual:
            raise ValueError('本章审核凭证无效或话本已变更。')
        return actual

    def enqueue(self, chapter_id, output_path, config_fingerprint):
        if not chapter_id:
            raise ValueError('章节编号不能为空。')
        if not isinstance(config_fingerprint, str) or not config_fingerprint.strip():
            raise ValueError('必须显式提供非空生成配置指纹。')
        digest = self._proof(chapter_id)
        output = Path(output_path).resolve()
        with self._locked():
            data = self._load()
            old = data['jobs'].get(chapter_id)
            if old and old.get('status') in ('failed', 'interrupted', 'stale'):
                raise ValueError('失败或中断任务必须显式调用 requeue()。')
            if old and old.get('status') in ('queued', 'running', 'complete'):
                same = (old.get('script_digest') == digest and old.get('output_path') == str(output)
                        and old.get('config_fingerprint') == config_fingerprint)
                if old.get('status') == 'complete':
                    if same and self._valid_output(old):
                        return dict(old)
                    raise ValueError('已完成任务的配置或输出校验不匹配，不能复用。')
                if same:
                    return dict(old)
                raise ValueError('该章节已有未完成或已完成任务；请显式检查或重置。')
            job = {'chapter_id': chapter_id, 'book_id': self.book_id,
                   'script_digest': digest, 'output_path': str(output),
                   'config_fingerprint': config_fingerprint,
                   'status': 'queued', 'attempt': (old or {}).get('attempt', 0),
                   'created_at': (old or {}).get('created_at', _now()), 'updated_at': _now()}
            data['jobs'][chapter_id] = job
            self._save(data)
            return dict(job)

    def get(self, chapter_id=None):
        with self._locked():
            jobs = self._load()['jobs']
            return dict(jobs[chapter_id]) if chapter_id else {k: dict(v) for k, v in jobs.items()}

    def claim(self, chapter_id=None):
        with self._locked():
            data = self._load()
            candidates = ([chapter_id] if chapter_id else sorted(data['jobs']))
            for ident in candidates:
                job = data['jobs'].get(ident)
                if not job or job.get('status') != 'queued':
                    continue
                try:
                    current_digest = self._proof(ident)
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    current_digest = None
                if current_digest != job.get('script_digest'):
                    job.update(status='stale', error='审核后话本已变更。', updated_at=_now())
                    self._save(data)
                    continue
                job.update(status='running', attempt=job.get('attempt', 0) + 1,
                           lease=uuid.uuid4().hex, started_at=_now(), updated_at=_now())
                self._save(data)
                return dict(job)
            if chapter_id and chapter_id not in data['jobs']:
                raise ValueError('队列中不存在该章节任务。')
            return None

    def complete(self, chapter_id, lease, output_path=None):
        with self._locked():
            data = self._load(); job = data['jobs'].get(chapter_id)
            if not job or job.get('status') != 'running' or not lease or job.get('lease') != lease:
                raise ValueError('任务租约无效或任务不处于运行状态。')
            if self._proof(chapter_id) != job.get('script_digest'):
                raise ValueError('生成期间话本或审核凭证已改变。')
            output = Path(output_path).resolve() if output_path else Path(job['output_path'])
            if str(output) != job['output_path'] or not output.is_file() or output.stat().st_size == 0:
                raise ValueError('任务输出缺失、为空或路径不匹配。')
            job.update(status='complete', output_sha256=_digest(output), output_size=output.stat().st_size,
                       completed_at=_now(), updated_at=_now())
            job.pop('lease', None); job.pop('error', None)
            self._save(data)
            return dict(job)

    def fail(self, chapter_id, lease, error):
        with self._locked():
            data = self._load(); job = data['jobs'].get(chapter_id)
            if not job or job.get('status') != 'running' or job.get('lease') != lease:
                raise ValueError('任务租约无效或任务不处于运行状态。')
            job.update(status='failed', error=str(error)[:1000], updated_at=_now())
            job.pop('lease', None); self._save(data)
            return dict(job)

    def recover(self, *, book_lock_held=False):
        """Mark running jobs interrupted while caller holds the book-level lock.

        The queue's file lock cannot establish whether a renderer in another
        process is still alive. Caller must first acquire the same book lock
        used by rendering, then attest with ``book_lock_held=True``.
        Never infer success from an output file.
        """
        if book_lock_held is not True:
            raise ValueError('恢复前必须由调用方持有该书的书级锁。')
        changed = []
        with self._locked():
            data = self._load()
            for ident, job in data['jobs'].items():
                if job.get('status') == 'running':
                    job.update(status='interrupted', error='进程中断；输出需人工核验后重新排队。', updated_at=_now())
                    job.pop('lease', None); changed.append(ident)
            if changed:
                self._save(data)
        return changed

    def requeue(self, chapter_id, config_fingerprint=None):
        digest = self._proof(chapter_id)
        with self._locked():
            data = self._load(); job = data['jobs'].get(chapter_id)
            if not job or job.get('status') not in ('failed', 'interrupted', 'stale'):
                raise ValueError('只有失败、中断或凭证过期任务可重新排队。')
            if config_fingerprint is not None and (not isinstance(config_fingerprint, str) or not config_fingerprint.strip()):
                raise ValueError('生成配置指纹无效。')
            job.update(status='queued', script_digest=digest, updated_at=_now())
            if config_fingerprint is not None:
                job['config_fingerprint'] = config_fingerprint
            for key in ('lease', 'error', 'output_sha256', 'output_size', 'completed_at'):
                job.pop(key, None)
            self._save(data)
            return dict(job)

    def restart(self, chapter_id, config_fingerprint):
        """Explicitly schedule a fresh render for an existing terminal job.

        The old output file is deliberately left in place; only its recorded
        completion metadata is cleared. Callers should use a distinct output
        path or arrange their own safe replacement strategy.
        """
        if not isinstance(config_fingerprint, str) or not config_fingerprint.strip():
            raise ValueError('必须显式提供非空生成配置指纹。')
        digest = self._proof(chapter_id)
        with self._locked():
            data = self._load(); job = data['jobs'].get(chapter_id)
            if not job or job.get('status') not in ('complete', 'failed', 'interrupted', 'stale'):
                raise ValueError('只有已完成、失败、中断或过期任务可显式重新生成。')
            job.update(status='queued', script_digest=digest,
                       config_fingerprint=config_fingerprint, attempt=0,
                       updated_at=_now())
            for key in ('lease', 'error', 'output_sha256', 'output_size', 'completed_at', 'started_at'):
                job.pop(key, None)
            self._save(data)
            return dict(job)

    def verify_output(self, chapter_id):
        with self._locked():
            job = self._load()['jobs'].get(chapter_id)
            if not job or job.get('status') != 'complete':
                return False
            return self._valid_output(job)

    @staticmethod
    def _valid_output(job):
        path = Path(job['output_path'])
        try:
            return (path.is_file() and path.stat().st_size > 0
                    and path.stat().st_size == job.get('output_size')
                    and _digest(path) == job.get('output_sha256'))
        except OSError:
            return False
