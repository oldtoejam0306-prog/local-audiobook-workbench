"""Read-only viewer for local DeepSeek usage receipts; no network access."""
import json
import hashlib
import os
import queue
import stat
import threading
import uuid
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

ROOT = Path(os.environ['LOCALAPPDATA']) / 'AudiobookTrials'
# Private historical receipts are never bundled. Users can explicitly register
# local evidence or rely on the bounded scanner for valid local receipts.
RECORDS = ()
RECEIPT_NAMES = ('usage.json', 'response.body', 'response.http-body')
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MAX_SCAN_ENTRIES = 20000
MAX_SCAN_RECEIPTS = 2000
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_OPERATION_READ_BYTES = 64 * 1024 * 1024
_READ_BUDGET = ContextVar('console_voice_billing_read_budget', default=None)
PRICE_SOURCE = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
REGISTRY = ROOT / 'console-voice-isolated-20261105' / 'billing-imports.json'

def identity(data):
    if not isinstance(data, dict): return None
    value = data.get('response_id') or data.get('id')
    return hashlib.sha256(value.encode('utf-8')).hexdigest() if isinstance(value, str) and value.strip() else None

def _read_receipt(path, *, total_limit=MAX_RECEIPT_BYTES):
    """Read a bounded regular receipt; callers must first validate containment."""
    path = Path(path)
    if _is_reparse_point(path): raise ValueError('回执文件路径不安全。')
    budget = _READ_BUDGET.get()
    cache = budget.get('cache') if budget is not None else None
    cache_key = os.path.normcase(str(path.absolute()))
    if cache is not None and cache_key in cache:
        raw = cache[cache_key]
        if len(raw) > total_limit: raise ValueError('回执超过安全读取上限，未计入。')
        return raw
    try:
        info = path.stat()
    except OSError as exc:
        raise ValueError('回执文件不可读取。') from exc
    allowed = min(MAX_RECEIPT_BYTES, total_limit,
                  budget['remaining'] if budget is not None else MAX_RECEIPT_BYTES)
    if not path.is_file() or info.st_size > allowed:
        raise ValueError('回执超过安全读取上限，未计入。')
    with path.open('rb') as stream:
        raw = stream.read(allowed + 1)
    if len(raw) > allowed:
        raise ValueError('回执超过安全读取上限，未计入。')
    if budget is not None:
        budget['remaining'] -= len(raw)
        budget['cache'][cache_key] = raw
    return raw

def _decode_receipt(path, *, total_limit=MAX_RECEIPT_BYTES):
    return json.loads(_read_receipt(path, total_limit=total_limit).decode('utf-8'))

def receipt_identity(data, path=None):
    value = identity(data)
    if value or path is None or Path(path).name != 'usage.json': return value
    alternates = _sibling_receipts(Path(path))
    if not alternates or receipt_conflict(data, path): return None
    identities = set()
    for alternate in alternates:
        if _is_reparse_point(alternate): return None
        try:
            other = _decode_receipt(alternate)
            other_id = identity(other)
            if not other_id or not _usage_matches(data, other): return None
            identities.add(other_id)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, AttributeError, ValueError):
            return None
    return next(iter(identities)) if len(identities) == 1 else None

def validated_usage(data):
    usage = data['usage']; a = usage['prompt_tokens']; b = usage['completion_tokens']
    if data.get('model') != 'deepseek-flash' or type(a) is not int or type(b) is not int or a < 0 or b < 0: raise ValueError('invalid usage')
    if 'total_tokens' in usage and (type(usage['total_tokens']) is not int or usage['total_tokens'] != a + b): raise ValueError('invalid total')
    return a, b

def _status_failure(status):
    if ((type(status) is int and status >= 400)
            or (isinstance(status, str) and status.strip().isdigit() and int(status.strip()) >= 400)):
        return 'http_failed'
    if isinstance(status, str) and status.strip().casefold() in {'error', 'failed', 'failure'}:
        return 'failed'
    return None

def response_state(data, path=None, root=None):
    """Describe request outcome separately from validated provider token usage."""
    if not isinstance(data, dict): return 'unknown'
    envelopes = [data]
    envelopes.extend(data[key] for key in ('response', 'metadata') if isinstance(data.get(key), dict))
    failure_marked = False
    for envelope in envelopes:
        for key in ('status_code', 'http_status', 'status'):
            state = _status_failure(envelope.get(key))
            if state == 'http_failed': return state
            if state == 'failed': failure_marked = True
        if (envelope.get('ok') is False or envelope.get('success') is False
                or envelope.get('error') not in (None, '', {})):
            failure_marked = True

    # The context worker keeps HTTP status in a sibling response.meta.json.
    # Trust it only when its body hash binds to the actual saved response body.
    if path is not None and root is not None:
        path = Path(path)
        safe_meta = _safe_local_receipt(path.parent / 'response.meta.json', root)
        if safe_meta is not None and safe_meta.is_file():
            try:
                metadata = _decode_receipt(safe_meta)
                expected = metadata.get('body_sha256')
                bodies = _sibling_receipts(path) if path.name == 'usage.json' else [path]
                for body_path in bodies:
                    safe_body = _safe_local_receipt(body_path, root)
                    if safe_body is None or safe_body.name not in ('response.body', 'response.http-body'):
                        continue
                    body_hash = hashlib.sha256(_read_receipt(safe_body)).hexdigest()
                    if expected and body_hash == expected:
                        state = _status_failure(metadata.get('http_status'))
                        if state: return state
                        if type(metadata.get('http_status')) is int and 200 <= metadata['http_status'] < 400:
                            return 'failed' if failure_marked else 'success'
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError, TypeError):
                pass
    return 'failed' if failure_marked else 'unknown'

def _usage_status(response_status, normal):
    if response_status == 'http_failed':
        return 'HTTP失败 / 用量已确认；费用仅估算'
    if response_status == 'failed':
        return '请求失败 / 用量已确认；费用仅估算'
    return normal

def _sibling_receipts(path):
    path = Path(path)
    if path.name == 'usage.json':
        return [candidate for name in ('response.body', 'response.http-body')
                if (candidate := path.parent / name).exists()]
    if path.name in ('response.body', 'response.http-body'):
        candidate = path.parent / 'usage.json'
        return [candidate] if candidate.exists() else []
    return []

def _usage_matches(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict): return False
    if left.get('model') != right.get('model'):
        return False
    return validated_usage(left) == validated_usage(right)

def receipt_conflict(data, path):
    """True when sibling receipt files disagree or cannot safely share an ID."""
    path = Path(path)
    siblings = _sibling_receipts(path)
    sibling_ids = []
    sibling_usage = []
    for sibling in siblings:
        if _is_reparse_point(sibling): return True
        try:
            other = _decode_receipt(sibling)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            return True
        if path.name == 'usage.json':
            usage_data, response_data = data, other
        else:
            usage_data, response_data = other, data
        if not isinstance(usage_data, dict) or not isinstance(response_data, dict):
            return True
        usage_id, response_id = identity(usage_data), identity(response_data)
        if path.name == 'usage.json':
            sibling_ids.append(response_id)
            try: sibling_usage.append(validated_usage(response_data))
            except (KeyError, TypeError, ValueError): sibling_usage.append(None)
        if usage_id and response_id and usage_id != response_id:
            return True
        if usage_data.get('model') != response_data.get('model'):
            return True
        try:
            usage_matches = validated_usage(usage_data) == validated_usage(response_data)
        except (KeyError, TypeError, ValueError):
            usage_matches = False
        if (isinstance(usage_data.get('usage'), dict)
                and isinstance(response_data.get('usage'), dict)
                and not usage_matches):
            return True
        # A usage receipt without its own ID may inherit the response ID only
        # when both files carry identical validated token usage.
        if not usage_id and response_id and not usage_matches:
            return True
    if path.name == 'usage.json' and len(siblings) > 1:
        present_ids = {value for value in sibling_ids if value is not None}
        present_usage = {value for value in sibling_usage if value is not None}
        if (len(present_ids) > 1 or len(present_usage) > 1
                or (present_ids and any(value is None for value in sibling_ids))):
            return True
    return False

def _is_reparse_point(path):
    try: info = Path(path).lstat()
    except FileNotFoundError: return False
    except OSError: return True
    return Path(path).is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & _FILE_ATTRIBUTE_REPARSE_POINT)

def _resolved_receipt_root(root):
    root = Path(root)
    if _is_reparse_point(root): return None
    try: resolved = root.resolve(strict=True)
    except (OSError, RuntimeError): return None
    return resolved if resolved.is_dir() else None

def _safe_local_receipt(path, root, *, allow_missing=False):
    """Resolve one root-relative path, rejecting symlinks and Windows junctions."""
    root_path = Path(root)
    root_real = _resolved_receipt_root(root_path)
    if root_real is None: return None
    candidate = Path(path).absolute()
    root_abs = root_path.absolute()
    try: relative = candidate.relative_to(root_abs)
    except ValueError: return None
    if any(part in ('..', '.') for part in relative.parts): return None
    cursor = root_abs
    if _is_reparse_point(cursor): return None
    for part in relative.parts:
        cursor = cursor / part
        if _is_reparse_point(cursor): return None
    if allow_missing and not candidate.exists():
        return root_real.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root_real) or not resolved.is_file(): return None
        return resolved
    except (OSError, RuntimeError): return None

def _receipt_source_keys(path, root):
    result = set()
    for candidate in [Path(path), *_sibling_receipts(Path(path))]:
        safe = _safe_local_receipt(candidate, root)
        if safe is not None:
            result.add(_source_key(safe.relative_to(root)))
    return result

def imported_records(registry=REGISTRY, root=None):
    if root is None: root = ROOT
    registry = Path(registry)
    if _is_reparse_point(registry): raise ValueError('登记文件路径不安全')
    if not registry.exists(): return []
    if root is not None:
        registry = _safe_local_receipt(registry, root)
        if registry is None: raise ValueError('登记文件不在安全数据根目录内')
    data = json.loads(_read_receipt(registry, total_limit=MAX_REGISTRY_BYTES).decode('utf-8'))
    if not isinstance(data, list) or any(not _valid_import_record(item) for item in data): raise ValueError('登记文件无效')
    return data

def _valid_import_record(item):
    if not isinstance(item, dict): return False
    source = item.get('source')
    if not isinstance(source, str) or not source or Path(source).is_absolute() or '..' in Path(source).parts: return False
    for name in ('source_hash', 'response_id_hash'):
        value = item.get(name)
        if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value): return False
    if ('response_failed' in item and type(item['response_failed']) is not bool): return False
    if 'response_status' in item and item['response_status'] not in ('success', 'failed', 'http_failed', 'unknown'): return False
    return all(type(item.get(name)) is int and item[name] >= 0 for name in ('input', 'output'))

def _source_key(value):
    return str(value).replace('\\', '/').casefold()

def _root_source_key(root, value):
    return _source_key(str(Path(root).resolve()) + '/' + str(value))

def _note_identity_usage(identity_hash, input_tokens, output_tokens, seen_usage):
    if not identity_hash: return 'unidentified'
    usage = (input_tokens, output_tokens)
    previous = seen_usage.get(identity_hash)
    if previous is None:
        seen_usage[identity_hash] = usage
        return 'first'
    return 'duplicate' if previous == usage else 'conflict'

def _receipt_identity_hashes(data, path):
    """Collect hashed identities from a conflicting receipt pair, never raw IDs."""
    values = {value for value in (identity(data),) if value}
    for sibling in _sibling_receipts(path):
        if _is_reparse_point(sibling): continue
        try:
            other = _decode_receipt(sibling)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            continue
        value = identity(other)
        if value: values.add(value)
    return values

def _scan_saved_receipts(root):
    """Return bounded safe receipts and whether the whole eligible tree was scanned."""
    root = _resolved_receipt_root(root)
    if root is None: return [], False, 'root_unavailable'
    visited_dirs = set()
    pending = [root]
    receipts = []
    entries_seen = 0
    bytes_seen = 0
    incomplete_reason = None
    while pending and incomplete_reason is None:
        base = pending.pop()
        try:
            if _is_reparse_point(base):
                incomplete_reason = 'reparse_point_skipped'; break
            base_real = base.resolve(strict=True)
            if not base_real.is_relative_to(root) or base_real in visited_dirs:
                continue
            visited_dirs.add(base_real)
            entries = os.scandir(base)
        except OSError:
            incomplete_reason = 'directory_unreadable'; break
        with entries:
            for entry in entries:
                entries_seen += 1
                if entries_seen > MAX_SCAN_ENTRIES:
                    incomplete_reason = 'entry_limit'; break
                child = Path(entry.path)
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    incomplete_reason = 'entry_unreadable'; break
                if (stat.S_ISLNK(info.st_mode)
                        or getattr(info, 'st_file_attributes', 0) & _FILE_ATTRIBUTE_REPARSE_POINT):
                    incomplete_reason = 'reparse_point_skipped'; continue
                if stat.S_ISDIR(info.st_mode):
                    try:
                        resolved = child.resolve(strict=True)
                    except OSError:
                        incomplete_reason = 'directory_unreadable'; continue
                    if not resolved.is_relative_to(root):
                        incomplete_reason = 'path_escape_skipped'; continue
                    if resolved not in visited_dirs:
                        pending.append(child)
                    continue
                if not stat.S_ISREG(info.st_mode) or entry.name not in RECEIPT_NAMES:
                    continue
                parent = child.parent
                intent = _safe_local_receipt(parent / 'intent.json', root)
                if intent is None or not intent.is_file():
                    continue
                if info.st_size > MAX_RECEIPT_BYTES:
                    incomplete_reason = 'single_receipt_size_limit'; break
                if bytes_seen + info.st_size > MAX_SCAN_BYTES:
                    incomplete_reason = 'total_receipt_size_limit'; break
                if len(receipts) >= MAX_SCAN_RECEIPTS:
                    incomplete_reason = 'receipt_count_limit'; break
                safe = _safe_local_receipt(child, root)
                if safe is None:
                    incomplete_reason = 'unsafe_receipt_skipped'; continue
                receipts.append(safe)
                bytes_seen += info.st_size
            if incomplete_reason is not None:
                break
    receipts.sort(key=lambda item: _source_key(item.relative_to(root)))
    return receipts, incomplete_reason is None, incomplete_reason

def _saved_receipts(root):
    """Yield safe receipts from the bounded scan; callers needing status use _scan_saved_receipts."""
    receipts, _, _ = _scan_saved_receipts(root)
    yield from receipts

def _register_evidence_impl(path, root=ROOT, registry=REGISTRY, records=RECORDS):
    """Explicit import of one root-contained response; persist only sanitized metadata."""
    root_input = Path(root)
    root = _resolved_receipt_root(root_input)
    path = _safe_local_receipt(path, root_input)
    if root is None or path is None or not path.is_relative_to(root) or path.name not in ('usage.json', 'response.body', 'response.http-body'):
        raise ValueError('仅可登记本机 AudiobookTrials 内的调用证据文件。')
    intent = _safe_local_receipt(path.parent / 'intent.json', root)
    if intent is None or not intent.is_file(): raise ValueError('缺少同目录安全请求意图记录。')
    raw = _read_receipt(path); data = json.loads(raw.decode('utf-8')); a, b = validated_usage(data)
    response_status = response_state(data, path, root)
    if receipt_conflict(data, path): raise ValueError('usage 与 response 回执冲突，不能登记。')
    response_id = receipt_identity(data, path)
    if not response_id: raise ValueError('响应缺少可用于去重的 ID，无法安全登记。')
    source = str(path.relative_to(root)); source_hash = hashlib.sha256(raw).hexdigest()
    registry = _safe_local_receipt(registry, root_input, allow_missing=True)
    if registry is None: raise ValueError('登记文件必须位于安全数据根目录内。')
    scanned_receipts, scan_complete, scan_reason = _scan_saved_receipts(root)
    if not scan_complete:
        raise ValueError('本机历史回执扫描不完整，无法安全验证重复；请先缩小或整理回执目录。')
    registry.parent.mkdir(parents=True, exist_ok=True)
    lock = registry.with_suffix('.lock'); fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        if _is_reparse_point(lock) or _is_reparse_point(registry):
            raise ValueError('登记文件或锁文件包含重解析点。')
        existing = imported_records(registry, root)
        known_paths = [(root / directory / filename, True) for _, directory, filename in records]
        known_paths.extend((known, False) for known in scanned_receipts)
        checked_paths = set()
        checked_bytes = 0
        for known, is_static in known_paths:
            try:
                known = _safe_local_receipt(known, root)
                if known is None: raise OSError('unsafe historical receipt')
                if known in checked_paths: continue
                checked_paths.add(known)
                if known == path:
                    if is_static: raise ValueError('该来源已登记。')
                    continue
                known_raw = _read_receipt(known, total_limit=MAX_SCAN_BYTES - checked_bytes)
                checked_bytes += len(known_raw)
                if checked_bytes > MAX_SCAN_BYTES:
                    raise ValueError('历史回执总量超过安全读取上限。')
                known_data = json.loads(known_raw.decode('utf-8'))
                if receipt_conflict(known_data, known): raise ValueError('历史usage与response回执冲突，无法安全检查重复。')
                known_id = receipt_identity(known_data, known)
                if not known_id: raise ValueError('历史响应缺少 ID，无法安全检查重复。')
                if hashlib.sha256(known_raw).hexdigest() == source_hash or known_id == response_id: raise ValueError('该响应已登记。')
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, AttributeError):
                # An unavailable historical source prevents proving uniqueness.
                raise ValueError('历史证据不可读取，无法安全检查重复。')
        if any(x.get('source','').casefold() == source.casefold() or x.get('source_hash') == source_hash or x.get('response_id_hash') == response_id for x in existing):
            raise ValueError('该来源或响应已登记。')
        existing.append({'source':source, 'source_hash':source_hash, 'response_id_hash':response_id,
                         'input':a, 'output':b, 'response_status':response_status})
        temp = registry.with_name(registry.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('x', encoding='utf-8') as out:
                json.dump(existing, out, ensure_ascii=False); out.flush(); os.fsync(out.fileno())
            os.replace(temp, registry)
        finally:
            if temp.exists(): temp.unlink()
    finally:
        os.close(fd); lock.unlink()

def register_evidence(path, root=ROOT, registry=REGISTRY, records=RECORDS):
    token = _READ_BUDGET.set({'remaining':MAX_OPERATION_READ_BYTES, 'cache':{}})
    try:
        return _register_evidence_impl(path, root, registry, records)
    finally:
        _READ_BUDGET.reset(token)

def _load_records_impl(root=ROOT, records=RECORDS, registry=REGISTRY, extra_roots=()):
    root_input = Path(root)
    root = _resolved_receipt_root(root_input)
    result = []
    seen_sources = set()
    seen_response_ids = set()
    seen_source_hashes = set()
    identity_usage = {}
    conflicting_ids = set()
    for label, directory, filename in records:
        folder = root_input / directory
        row = {'label': label, 'source': f'{directory}/{filename}', '_root':str(root_input.resolve()),
               'input': None, 'output': None, 'status': '未知 / 未返回'}
        try:
            intent = _safe_local_receipt(folder / 'intent.json', root_input)
            if intent is None or not intent.is_file(): raise ValueError('unsafe or missing intent')
            path = _safe_local_receipt(folder / filename, root_input)
            if path is None: raise ValueError('unsafe receipt path')
            raw = _read_receipt(path)
            data = json.loads(raw.decode('utf-8'))
            source_hash = hashlib.sha256(raw).hexdigest()
            if receipt_conflict(data, path):
                row['status'] = '证据冲突 / 未计入'
                row['_identity_hashes'] = _receipt_identity_hashes(data, path)
                conflicting_ids.update(row['_identity_hashes'])
                seen_sources.update(_receipt_source_keys(path, root))
                seen_source_hashes.add(source_hash)
                result.append(row)
                continue
            a, b = validated_usage(data)
            response_id = receipt_identity(data, path)
            identity_state = _note_identity_usage(response_id, a, b, identity_usage)
            if identity_state == 'conflict':
                conflicting_ids.add(response_id)
                row.update(status='证据冲突 / 同响应ID用量不一致，未计入',
                           _identity_hash=response_id)
            elif identity_state == 'duplicate' or source_hash in seen_source_hashes:
                row['status'] = '重复响应 / 未重复计入'
                if response_id: row['_identity_hash'] = response_id
            else:
                row.update(input=a, output=b, status=_usage_status(response_state(data, path, root_input), '已返回'))
                if response_id: row['_identity_hash'] = response_id
                if response_id: seen_response_ids.add(response_id)
                seen_source_hashes.add(source_hash)
        except (OSError, ValueError, KeyError, TypeError):
            row['status'] = '未知 / 回执不可读、无效或超过上限，未计入'
        seen_sources.add(_root_source_key(root_input, row['source']))
        result.append(row)
    try:
        for n, item in enumerate(imported_records(registry, root_input), 1):
            source_key = _root_source_key(root_input, item['source'])
            if source_key in seen_sources or item['source_hash'] in seen_source_hashes: continue
            a, b = item['input'], item['output']
            identity_hash = item['response_id_hash']
            identity_state = _note_identity_usage(identity_hash, a, b, identity_usage)
            if identity_state == 'conflict':
                conflicting_ids.add(identity_hash)
                result.append({'label':f'手动登记 {n}', 'source':item['source'],'_root':str(root_input.resolve()),'input':None,
                    'output':None, 'status':'证据冲突 / 同响应ID用量不一致，未计入',
                    '_identity_hash':identity_hash})
            elif identity_state == 'duplicate':
                continue
            else:
                status = _usage_status(item.get('response_status', 'unknown'), '已登记')
                result.append({'label':f'手动登记 {n}', 'source':item['source'],'_root':str(root_input.resolve()),'input':a,
                    'output':b, 'status':status, '_identity_hash':identity_hash})
            seen_sources.add(source_key); seen_source_hashes.add(item['source_hash']); seen_response_ids.add(identity_hash)
    except (OSError, ValueError, KeyError, TypeError):
        result.append({'label':'手动登记文件读取失败','source':'请检查登记文件','_root':str(root_input),
            'input':None,'output':None,'status':'未知 / 未计入'})
    automatic_index = 0
    auto_sources = []
    scanned_roots = set()
    for candidate_root in (root_input, *(Path(item) for item in (extra_roots or ()))):
        scan_root = _resolved_receipt_root(candidate_root)
        if scan_root is None:
            marker_root = Path(candidate_root).resolve()
            root_key = _source_key(marker_root)
            if root_key not in scanned_roots:
                scanned_roots.add(root_key)
                result.append({'label':'本机自动扫描','source':'AudiobookTrials 自动回执扫描',
                    '_root':str(marker_root),'input':None,'output':None,
                    'status':'扫描不完整 / 数据根不可读；自动发现费用未计入',
                    '_scan_incomplete':True})
            continue
        root_key = _source_key(scan_root)
        if root_key in scanned_roots:
            continue
        scanned_roots.add(root_key)
        auto_receipts, scan_complete, scan_reason = _scan_saved_receipts(scan_root)
        if not scan_complete:
            result.append({'label':'本机自动扫描','source':'AudiobookTrials 自动回执扫描',
                '_root':str(scan_root),'input':None,'output':None,
                'status':f'扫描不完整 / {scan_reason}；此根自动发现费用未计入',
                '_scan_incomplete':True})
            continue
        auto_sources.extend((scan_root, path) for path in auto_receipts)
    for scan_root, path in auto_sources:
        source = str(path.relative_to(scan_root)); source_key = _root_source_key(scan_root, source)
        if source_key in seen_sources: continue
        try:
            raw = _read_receipt(path, total_limit=MAX_SCAN_BYTES)
            data = json.loads(raw.decode('utf-8'))
            source_hash = hashlib.sha256(raw).hexdigest()
            if receipt_conflict(data, path):
                automatic_index += 1
                conflict_row = {'label':f'自动发现 {automatic_index}', 'source':source,
                    '_root':str(scan_root),'input':None,'output':None,
                    'status':'证据冲突 / 未计入'}
                conflict_row['_identity_hashes'] = _receipt_identity_hashes(data, path)
                conflicting_ids.update(conflict_row['_identity_hashes'])
                result.append(conflict_row)
                seen_sources.update(_root_source_key(scan_root, item) for item in _receipt_source_keys(path, scan_root))
                seen_source_hashes.add(source_hash)
                continue
            if source_hash in seen_source_hashes:
                seen_sources.update(_root_source_key(scan_root, item)
                                    for item in _receipt_source_keys(path, scan_root))
                continue
            try:
                a, b = validated_usage(data)
            except (ValueError, KeyError, TypeError):
                # A response body without usage may accompany an authoritative
                # usage.json; leave that sibling eligible for processing.
                seen_sources.add(source_key)
                seen_source_hashes.add(source_hash)
                if path.name == 'usage.json':
                    automatic_index += 1
                    result.append({'label':f'自动发现 {automatic_index}', 'source':source,
                        '_root':str(scan_root),'input':None,'output':None,
                        'status':'未知 / 用量回执无效，未计入'})
                    seen_sources.update(_root_source_key(scan_root, item) for item in _receipt_source_keys(path, scan_root))
                elif not (path.parent / 'usage.json').exists():
                    automatic_index += 1
                    result.append({'label':f'自动发现 {automatic_index}', 'source':source,
                        '_root':str(scan_root),'input':None,'output':None,
                        'status':'未知 / 失败回执未提供用量，未计入'})
                    seen_sources.add(source_key)
                continue
            response_id = receipt_identity(data, path)
            if source_hash in seen_source_hashes:
                continue
            if not response_id:
                automatic_index += 1
                result.append({'label':f'自动发现 {automatic_index}', 'source':source,
                    '_root':str(scan_root),'input':None,'output':None,
                    'status':'未知 / 缺少可去重ID，未计入'})
                seen_sources.add(source_key)
                seen_source_hashes.add(source_hash)
                if path.name == 'usage.json':
                    seen_sources.update(_root_source_key(scan_root, item) for item in _receipt_source_keys(path, scan_root))
                continue
            identity_state = _note_identity_usage(response_id, a, b, identity_usage)
            if identity_state == 'conflict':
                conflicting_ids.add(response_id)
                automatic_index += 1
                result.append({'label':f'自动发现 {automatic_index}', 'source':source,
                    '_root':str(scan_root),'input':None,'output':None,
                    'status':'证据冲突 / 同响应ID用量不一致，未计入',
                    '_identity_hash':response_id})
                seen_sources.add(source_key); seen_source_hashes.add(source_hash)
                continue
            if identity_state == 'duplicate' or response_id in seen_response_ids:
                seen_sources.add(source_key); seen_source_hashes.add(source_hash)
                continue
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError, TypeError):
            if path.name == 'usage.json' and source_key not in seen_sources:
                automatic_index += 1
                result.append({'label':f'自动发现 {automatic_index}', 'source':source,
                    '_root':str(scan_root),'input':None,'output':None,
                    'status':'未知 / 回执不可读或超过上限，未计入'})
                seen_sources.update(_root_source_key(scan_root, item) for item in _receipt_source_keys(path, scan_root))
            elif path.name != 'usage.json' and not (path.parent / 'usage.json').exists():
                automatic_index += 1
                result.append({'label':f'自动发现 {automatic_index}', 'source':source,
                    '_root':str(scan_root),'input':None,'output':None,
                    'status':'未知 / 失败回执不可读或无用量，未计入'})
                seen_sources.add(source_key)
            continue
        automatic_index += 1
        result.append({'label':f'自动发现 {automatic_index}', 'source':source,
            '_root':str(scan_root),'input':a,'output':b,
            'status':_usage_status(response_state(data, path, scan_root), '自动收集'),
            '_identity_hash':response_id})
        seen_sources.add(source_key); seen_response_ids.add(response_id); seen_source_hashes.add(source_hash)
    for row in result:
        identity_hash = row.get('_identity_hash')
        identity_hashes = set(row.pop('_identity_hashes', ()))
        if identity_hash in conflicting_ids or identity_hashes & conflicting_ids:
            row.update(input=None, output=None,
                       status='证据冲突 / 同响应ID用量不一致，未计入')
        row.pop('_identity_hash', None)
    return result

def load_records(root=ROOT, records=RECORDS, registry=REGISTRY, extra_roots=()):
    token = _READ_BUDGET.set({'remaining':MAX_OPERATION_READ_BYTES, 'cache':{}})
    try:
        return _load_records_impl(root, records, registry, extra_roots)
    finally:
        _READ_BUDGET.reset(token)

def price(value):
    try: p = Decimal(value)
    except InvalidOperation: raise ValueError('请输入非负的有效单价。')
    if not p.is_finite() or p < 0 or p > 1000000: raise ValueError('请输入非负的有效单价。')
    return p

def estimate(row, input_price, output_price):
    if row['input'] is None: return None
    return (Decimal(row['input']) * input_price + Decimal(row['output']) * output_price) / Decimal(1000000)

def usage_totals(rows, input_price, output_price):
    known = [row for row in rows if estimate(row, input_price, output_price) is not None]
    duplicates = [row for row in rows if row.get('status', '').startswith('重复响应 /')]
    unknown = [row for row in rows if row.get('input') is None and row not in duplicates]
    total = sum((estimate(row, input_price, output_price) for row in known), Decimal(0))
    return {'known_rows': known, 'unknown_count': len(unknown),
            'duplicate_count': len(duplicates), 'estimated_cost': total}

class BillingWindow(tk.Toplevel):
    def __init__(self, master, *, roots=None):
        super().__init__(master); self.title('DeepSeek 用量与估算'); self.geometry('940x570'); self.minsize(760, 450)
        self.roots = tuple(roots or (ROOT,))
        frame = ttk.Frame(self, padding=12); frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='DeepSeek 项目用量与费用估算', font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='汇总固定历史记录、已登记项目调用，以及 AudiobookTrials 下带请求意图文件的本机 DeepSeek 回执。有效 usage 表示回执中的 token 用量已核实，即使请求失败也会单独标状态并计入估算；估算费用不是供应商账单或实际扣款。未知/冲突用量不会按 0 计算，也不会计入合计。', wraplength=880).pack(anchor='w', pady=5)
        controls = ttk.Frame(frame); controls.pack(fill='x', pady=4)
        self.in_price = tk.StringVar(value='2'); self.out_price = tk.StringVar(value='8')
        ttk.Label(controls, text='输入 元/百万 token').pack(side='left'); ttk.Entry(controls, textvariable=self.in_price, width=8).pack(side='left', padx=5)
        ttk.Label(controls, text='输出 元/百万 token').pack(side='left'); ttk.Entry(controls, textvariable=self.out_price, width=8).pack(side='left', padx=5)
        ttk.Button(controls, text='刷新回执', command=self.refresh).pack(side='left', padx=8)
        ttk.Button(controls, text='按新单价重算', command=self.recalculate).pack(side='left')
        ttk.Button(controls, text='登记本机调用证据', command=self.import_evidence).pack(side='left')
        ttk.Label(frame, text='自动发现仅覆盖本机 AudiobookTrials 中带 intent.json 的有效 deepseek-flash 回执；按来源和响应 ID 去重。HTTP 失败但响应身份与 usage 均有效时，token 会计入“已知用量”，状态列标明失败；无有效 usage、缺少去重身份、冲突或扫描不完整仍显示未知且不计入估算。默认按 2026-11-04 官方中文价卡 deepseek-flash 高峰未命中输入/输出 2/8 元估算；全部输入按未命中计，不自动判断时段。', wraplength=880).pack(anchor='w')
        ttk.Label(frame, text='价卡来源：' + PRICE_SOURCE, wraplength=880).pack(anchor='w', pady=(0, 7))
        self.table = ttk.Treeview(frame, columns=('name', 'input', 'output', 'cost', 'status'), show='headings', height=10)
        for col, title, width in [('name','登记的调用',220), ('input','输入 tokens',120), ('output','输出 tokens',120), ('cost','估算费用（元）',140), ('status','回执状态',230)]:
            self.table.heading(col, text=title); self.table.column(col, width=width, anchor='w')
        self.table.pack(fill='both', expand=True)
        self.summary = tk.StringVar(); ttk.Label(frame, textvariable=self.summary, wraplength=880).pack(anchor='w', pady=6)
        self.refresh_status = tk.StringVar(value='正在读取本机回执…')
        ttk.Label(frame, textvariable=self.refresh_status, wraplength=880).pack(anchor='w')
        self.detail = tk.StringVar(); ttk.Label(frame, textvariable=self.detail, wraplength=880).pack(anchor='w')
        self.table.bind('<<TreeviewSelect>>', self.selected)
        self._scan_queue = queue.Queue(); self._scan_generation = 0
        self._scan_running = False; self._scan_closed = False
        self.rows = []; self._has_scan_result = False; self._refresh_after_id = None
        self._last_valid_prices = (price(self.in_price.get()), price(self.out_price.get()))
        self.protocol('WM_DELETE_WINDOW', self._close)
        self.refresh()
    def refresh(self):
        try: a = price(self.in_price.get()); b = price(self.out_price.get())
        except ValueError as exc: messagebox.showerror('单价无效', str(exc), parent=self); return
        self._input_price, self._output_price = a, b
        self._last_valid_prices = (a, b)
        self._scan_generation += 1
        self.refresh_status.set('正在扫描本机回执…')
        if not self._scan_running:
            self._start_scan(self._scan_generation)
        # Refresh clicks during a scan are coalesced into one follow-up scan.
    def _start_scan(self, generation):
        self._scan_running = True
        roots = self.roots
        def scan():
            try:
                rows = load_records(root=roots[0], extra_roots=roots[1:])
                self._scan_queue.put((generation, rows, None))
            except Exception as exc:
                self._scan_queue.put((generation, None, exc))
        worker = threading.Thread(target=scan, name='billing-receipt-scan', daemon=True)
        try:
            worker.start()
        except Exception as exc:
            self._scan_running = False
            self.refresh_status.set(f'扫描未能启动，保留上次结果；可重试：{exc}')
            return
        self._schedule_scan_poll()
    def _schedule_scan_poll(self):
        if not self._scan_closed and self._refresh_after_id is None:
            self._refresh_after_id = self.after(60, self._poll_scan)
    def _poll_scan(self):
        self._refresh_after_id = None
        if self._scan_closed: return
        try: generation, rows, error = self._scan_queue.get_nowait()
        except queue.Empty:
            self._schedule_scan_poll(); return
        self._scan_running = False
        if generation == self._scan_generation:
            if error is not None:
                self.refresh_status.set(f'扫描失败，保留上次结果：{error}')
            else:
                self._render_rows(rows=rows, prices=self._last_valid_prices)
                self.rows = rows
                self._has_scan_result = True
                price_warning = ''
                try:
                    price(self.in_price.get()); price(self.out_price.get())
                except ValueError:
                    price_warning = ' 当前单价输入无效，表格保留上次有效单价。'
                self.refresh_status.set('刷新完成；单价修改可直接重新估算。' + price_warning)
        if generation != self._scan_generation:
            self.refresh_status.set('有更新的刷新请求，正在重新扫描…')
            self._start_scan(self._scan_generation)
    def _render_rows(self, *, rows=None, prices=None):
        rows = self.rows if rows is None else rows
        a, b = self._last_valid_prices if prices is None else prices
        self._input_price, self._output_price = a, b
        self.table.delete(*self.table.get_children())
        totals = usage_totals(rows, a, b)
        for i, row in enumerate(rows):
            cost = estimate(row, a, b)
            self.table.insert('', 'end', iid=str(i), values=(row['label'], row['input'] if cost is not None else '—', row['output'] if cost is not None else '—', f'{cost:.6f}' if cost is not None else '—', row['status']))
        incomplete = any(row.get('_scan_incomplete') for row in rows)
        suffix = '；自动扫描不完整，自动发现费用未计入' if incomplete else ''
        known = totals['known_rows']
        self.summary.set(f'已确认用量 {len(known)} 次：输入 {sum(r["input"] for r in known):,} / 输出 {sum(r["output"] for r in known):,} tokens，费用估算 ¥{totals["estimated_cost"]:.6f}（非账单）；未知/冲突 {totals["unknown_count"]} 次未计入，重复 {totals["duplicate_count"]} 次不重复计入。{suffix}')
        self.detail.set('选中一行查看登记证据路径。')
        self.rows = rows
    def recalculate(self):
        try: a = price(self.in_price.get()); b = price(self.out_price.get())
        except ValueError as exc: messagebox.showerror('单价无效', str(exc), parent=self); return
        self._input_price, self._output_price = a, b
        self._last_valid_prices = (a, b)
        if self._has_scan_result:
            self._render_rows()
            self.refresh_status.set('已使用当前扫描结果重新估算。')
        else:
            self.refresh_status.set('尚无扫描结果；正在读取本机回执…')
            self.refresh()
    def _close(self):
        if self._scan_closed:
            return
        self._scan_closed = True
        self._scan_generation += 1
        if self._refresh_after_id is not None:
            try: self.after_cancel(self._refresh_after_id)
            except tk.TclError: pass
            self._refresh_after_id = None
        tk.Toplevel.destroy(self)
    def destroy(self):
        if getattr(self, '_scan_closed', False):
            return
        return self._close()
    def selected(self, _event):
        selection = self.table.selection()
        if selection:
            row = self.rows[int(selection[0])]
            evidence_root = Path(row.get('_root') or self.roots[0])
            self.detail.set('本机证据（读取用量字段，不展示正文）：' + str(evidence_root / row['source']))
    def import_evidence(self):
        path = filedialog.askopenfilename(parent=self, initialdir=str(self.roots[0]), title='选择项目调用的 usage.json 或响应文件')
        if not path: return
        try:
            register_evidence(path, root=self.roots[0]); self.refresh()
            messagebox.showinfo('登记完成', '仅保存用量与去重摘要；请确认该证据属于本项目。', parent=self)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            messagebox.showerror('无法登记', str(exc), parent=self)
