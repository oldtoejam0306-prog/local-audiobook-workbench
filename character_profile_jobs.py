"""Bounded, book-scoped character profile discovery jobs.

Real dispatch is one-shot and receipt-backed. Tests inject an index reader and
dispatcher; importing this module never reads a book or contacts a provider.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import real_pipeline_workflow as workflow
import workbench_real as registered_books
import workbench_real_flow as flow
from real_book_auto_voice import _selection_lock as _book_os_lock
from character_fusion import _linked as _identity_linked

WORKER = Path(__file__).with_name('real_pipeline_context_worker.mjs')
PROFILE_PROTOCOL = Path(__file__).with_name('character_profile_protocol.mjs')
CHAPTER = re.compile(r'c\d+(?:\.p\d+(?:\.\d+)?)?\Z')
BOOK = re.compile(r'book-[0-9a-f]{20}\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')


class ProfileJobError(RuntimeError):
    def __init__(self, status, message, *, artifact_path=None, batches=()):
        super().__init__(message)
        self.status = status
        self.retry_allowed = False
        self.artifact_path = str(artifact_path) if artifact_path else None
        self.batches = list(batches)


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    return _hash(workflow._bytes(value))


def _runtime_root():
    import workbench_real_v2_runtime as runtime
    return Path(runtime.ROOT)


def _book_id(name):
    if isinstance(name, str) and BOOK.fullmatch(name):
        return name
    book = registered_books.BOOKS.get(name) if isinstance(name, str) else None
    if not isinstance(book, str) or not BOOK.fullmatch(book):
        raise ValueError('书籍未登记')
    return book


def _default_preflight():
    node = shutil.which('node') or 'node'
    try:
        result = subprocess.run([node, str(WORKER), '--preflight'], capture_output=True,
            text=True, encoding='utf-8', timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        report = json.loads(result.stdout)
    except Exception as exc:
        raise ProfileJobError('credential_unavailable', '无法完成模型凭据预检') from exc
    if result.returncode != 0 or report.get('status') != 'credential_available':
        raise ProfileJobError('credential_unavailable', '未配置可用模型凭据；尚未创建任务或调用模型')


def _default_dispatcher(input_path, receipt_dir, jobs_dir):
    node = shutil.which('node') or 'node'
    try:
        result = subprocess.run([node, str(WORKER), '--run', str(input_path),
            str(receipt_dir), str(jobs_dir)], capture_output=True, text=True,
            encoding='utf-8', timeout=180,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as exc:
        raise ProfileJobError('usage_unknown', '模型任务超时，结果未知；不会自动重发',
                              artifact_path=receipt_dir) from exc
    if result.returncode:
        if 'credential_unavailable' in (result.stdout + '\n' + result.stderr):
            raise ProfileJobError('credential_unavailable', 'worker凭据检查未通过；没有派发模型请求',
                                  artifact_path=Path(receipt_dir).parent)
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
        if result.returncode and report.get('status') not in ('needs_review', 'invalid_or_usage_unknown'):
            raise ValueError('worker returned an incomplete status')
        return report
    except (ValueError, IndexError) as exc:
        raise ProfileJobError('usage_unknown', 'worker状态输出无法核实；不会自动重发',
                              artifact_path=receipt_dir) from exc


def _source_chapter(response, book_id, chapter_id, index_sha256):
    if (not isinstance(response, dict) or response.get('book_id') != book_id
            or response.get('chapter_id') != chapter_id
            or response.get('human_reviewed') is not False
            or response.get('index_sha256') != index_sha256):
        raise ValueError('verified chapter index identity mismatch')
    payload = response.get('payload')
    if not isinstance(payload, dict) or payload.get('book_id') != book_id or payload.get('chapter_id') != chapter_id:
        raise ValueError('verified chapter payload mismatch')
    raw = workflow._bytes(payload)
    if _hash(raw) != response.get('payload_sha256'):
        raise ValueError('verified chapter payload hash mismatch')
    text = payload.get('text')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('verified chapter text missing')
    return {'chapter_id': chapter_id, 'text': text,
            'text_sha256': _hash(text.encode('utf-8'))}, payload


def _read_success(batch_dir, expected_input_sha):
    input_path = batch_dir / 'input.json'
    raw_input = input_path.read_bytes()
    if _hash(raw_input) != expected_input_sha:
        raise ProfileJobError('unknown', '已有批次输入哈希不匹配；需要人工检查', artifact_path=batch_dir)
    receipt = batch_dir / 'receipt' / 'character_profiles_v1'
    required = [receipt / 'intent.json', receipt / 'request.body.json', receipt / 'response.body',
                receipt / 'response.meta.json', receipt / 'usage.json']
    if not all(p.is_file() for p in required):
        raise ProfileJobError('unknown', '批次已有预留但缺少完整回执；不会重发', artifact_path=batch_dir)
    intent, request_body, body, meta, usage_file = [
        json.loads(p.read_bytes()) if p.name not in ('response.body', 'request.body.json') else p.read_bytes()
        for p in required]
    meta_raw = (receipt / 'response.meta.json').read_bytes()
    if (intent.get('input_sha256') != expected_input_sha or intent.get('purpose') != 'character_profiles_v1'
            or intent.get('request_sha256') != _hash(request_body)
            or meta.get('http_status') != 200 or meta.get('body_sha256') != _hash(body)):
        raise ProfileJobError('unknown', '批次回执身份、状态或响应hash无效；不会重发', artifact_path=batch_dir)
    provider = json.loads(body)
    actual_usage = provider.get('usage')
    if not isinstance(actual_usage, dict) or not all(type(actual_usage.get(k)) is int and actual_usage[k] >= 0
            for k in ('prompt_tokens', 'completion_tokens')) or usage_file.get('usage') != actual_usage:
        raise ProfileJobError('usage_unknown', 'provider usage无法核实；不会重发', artifact_path=batch_dir)
    for filename, field in (('worker-source.mjs','worker_source_sha256'),
                            ('protocol-source.mjs','protocol_source_sha256')):
        snapshot=batch_dir/filename
        if snapshot.is_symlink() or not snapshot.is_file() or _hash(snapshot.read_bytes())!=intent.get(field):
            error=ProfileJobError('source_pin_mismatch','画像worker或协议源码快照无法核实；请检查原回执',
                                  artifact_path=batch_dir)
            error.usage=actual_usage
            raise error
    result_path = receipt / 'result.json'
    recovery_path = receipt / 'local-format-recovery.json'
    recovered_path = receipt / 'result.local-format-recovered.json'
    if recovery_path.exists():
        recovery_helper = batch_dir / 'profile-json-recovery-source.mjs'
        try:
            for artifact in (recovery_path, recovery_helper, recovered_path):
                if artifact.is_symlink() or not artifact.is_file() or artifact.stat().st_nlink != 1:
                    raise ValueError('recovery artifact must be a single-link regular file')
            if result_path.exists() and (result_path.is_symlink() or not result_path.is_file()
                                         or result_path.stat().st_nlink != 1):
                raise ValueError('original result must be a single-link regular file')
            recovery_raw = recovery_path.read_bytes()
            recovery = json.loads(recovery_raw)
            helper_raw = recovery_helper.read_bytes()
            recovered_raw = recovered_path.read_bytes()
            original_sha = _hash(result_path.read_bytes()) if result_path.is_file() else None
            repair = recovery.get('repair') if isinstance(recovery, dict) else None
            removals = repair.get('removals') if isinstance(repair, dict) else None
            repair_rules = {
                'remove_premature_name_evidence_closers': ('}]', {
                    'aliases', 'identity', 'gender', 'age', 'personality', 'behavior',
                    'speech_style', 'voice_design', 'uncertainties'}, 8),
                'remove_premature_scope_summary_root_closer': ('}', {'scope_summary'}, 1),
            }
            rule = repair_rules.get(repair.get('kind')) if isinstance(repair, dict) else None
            if (not isinstance(recovery, dict) or set(recovery) != {'version', 'kind', 'input_sha256',
                        'response_body_sha256', 'response_meta_sha256', 'original_result_sha256',
                        'protocol_source_sha256', 'helper_source_sha256', 'recovered_result_sha256',
                        'response_id', 'usage', 'repair'}
                    or recovery.get('version') != 1 or recovery.get('kind') != 'offline_profile_json_boundary_recovery'
                    or recovery.get('input_sha256') != expected_input_sha or recovery.get('response_body_sha256') != _hash(body)
                    or recovery.get('response_meta_sha256') != _hash(meta_raw)
                    or recovery.get('original_result_sha256') != original_sha
                    or recovery.get('protocol_source_sha256') != intent.get('protocol_source_sha256')
                    or recovery.get('helper_source_sha256') != _hash(helper_raw)
                    or recovery.get('recovered_result_sha256') != _hash(recovered_raw)
                    or recovery.get('response_id') != provider.get('id') or recovery.get('usage') != actual_usage
                    or rule is None
                    or not isinstance(removals, list) or not (1 <= len(removals) <= rule[2])
                    or any(not isinstance(row, dict) or row.get('removed') != rule[0]
                        or type(row.get('character_offset')) is not int or row['character_offset'] < 0
                        or row.get('next_field') not in rule[1] for row in removals)
                    or [row['character_offset'] for row in removals] != sorted(set(row['character_offset'] for row in removals))):
                raise ValueError('recovery binding or schema mismatch')
            result = json.loads(recovered_raw)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            error = ProfileJobError('invalid_result', '离线画像格式修复回执无法核实；不会重发', artifact_path=recovery_path)
            error.usage = actual_usage
            raise error from exc
    else:
        if not result_path.is_file():
            error = ProfileJobError('invalid_result', '原始usage有效，但画像结果文件缺失；不会重发', artifact_path=result_path)
            error.usage = actual_usage
            raise error
        result = json.loads(result_path.read_bytes())
    if (result.get('status') != 'needs_review' or result.get('retry_allowed') is not False
            or not isinstance(result.get('result'), dict)
            or result['result'].get('mode') != 'character_profiles_v1'):
        error = ProfileJobError('invalid_result', '画像候选未通过协议校验；不会重发', artifact_path=batch_dir)
        error.usage = actual_usage
        raise error
    if result.get('usage') != actual_usage or result.get('response_id') != provider.get('id'):
        error = ProfileJobError('usage_unknown', '结果文件usage与原始provider回执不一致', artifact_path=batch_dir)
        error.usage = actual_usage
        raise error
    return result, actual_usage, receipt


def _read_or_recover_success(batch_dir, expected_input_sha):
    """Recover recognized JSON syntax locally after a verified, known-usage reply."""
    try:
        return _read_success(batch_dir, expected_input_sha)
    except ProfileJobError as original:
        receipt = batch_dir / 'receipt' / 'character_profiles_v1'
        if (original.status != 'invalid_result' or not isinstance(getattr(original, 'usage', None), dict)
                or (receipt / 'local-format-recovery.json').exists()):
            raise
        try:
            provider = json.loads((receipt / 'response.body').read_bytes())
            content = provider['choices'][0]['message']['content']
            if not isinstance(content, str):
                raise original
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            raise original
        try:
            json.loads(content)
        except json.JSONDecodeError:
            pass
        else:
            raise original
        helper = Path(__file__).with_name('profile_json_recovery.mjs')
        if helper.is_symlink() or not helper.is_file():
            raise original
        try:
            completed = subprocess.run([shutil.which('node') or 'node', str(helper),
                '--recover', str(batch_dir)], capture_output=True, text=True, encoding='utf-8',
                timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.SubprocessError):
            raise original
        if completed.returncode:
            raise original
        return _read_success(batch_dir, expected_input_sha)


def _merge_evidence(left, right):
    out = list(left) if isinstance(left, list) else []
    seen = {_digest(x) for x in out if isinstance(x, dict)}
    for item in right if isinstance(right, list) else []:
        if isinstance(item, dict) and _digest(item) not in seen:
            out.append(item); seen.add(_digest(item))
    return out


def _claim_known(claim):
    return isinstance(claim, dict) and claim.get('status') in ('explicit', 'inferred') and bool(claim.get('value'))


def _claim_sources(claim):
    return [{'chapter_id': e.get('chapter_id'), 'source_sha256': e.get('source_sha256'),
             'quote': e.get('quote')} for e in claim.get('evidence', []) if isinstance(e, dict)] if isinstance(claim, dict) else []


def _merge_character(old, new, conflicts):
    merged = dict(old)
    source_refs = _merge_evidence(old.get('profile_sources', []), new.get('profile_sources', []))
    merged['profile_sources'] = source_refs
    for key, value in new.items():
        if key in ('name', 'profile_sources', 'voice_design_candidates'):
            continue
        if key in ('identity', 'gender', 'age'):
            before = merged.get(key)
            before_known, after_known = _claim_known(before), _claim_known(value)
            if not before_known and after_known:
                merged[key] = value
            elif before_known and after_known and before.get('value') == value.get('value'):
                updated = dict(before); updated['evidence'] = _merge_evidence(before.get('evidence'), value.get('evidence'))
                if before.get('status') == 'inferred' and value.get('status') == 'explicit': updated['status'] = 'explicit'
                merged[key] = updated
            elif before_known and after_known and before.get('value') != value.get('value'):
                conflicts.append({'type': 'profile_field_conflict', 'name': old['name'], 'field': key,
                    'candidates': [{'claim': before, 'sources': _claim_sources(before)},
                                   {'claim': value, 'sources': _claim_sources(value)}]})
            elif not before_known and not after_known and before is None:
                merged[key] = value
            # An unknown incoming claim never erases an existing known fact.
        elif key in ('personality', 'behavior', 'speech_style', 'name_evidence', 'uncertainties'):
            merged[key] = _merge_evidence(merged.get(key, []), value if isinstance(value, list) else [])
        elif key == 'aliases':
            existing = merged.get('aliases', [])
            by_name = {a.get('name'): dict(a) for a in existing if isinstance(a, dict) and a.get('name')}
            for alias in value if isinstance(value, list) else []:
                if not isinstance(alias, dict) or not alias.get('name'):
                    continue
                if alias['name'] in by_name:
                    item = by_name[alias['name']]
                    item['evidence'] = _merge_evidence(item.get('evidence'), alias.get('evidence'))
                else:
                    by_name[alias['name']] = dict(alias)
            merged['aliases'] = list(by_name.values())
        elif key == 'voice_design':
            before = merged.get('voice_design')
            if not isinstance(before, dict) or not any(before.get(k) for k in ('description', 'rationale', 'creative_choices')):
                merged['voice_design'] = value
            # Existing candidate design stays the visible candidate. New design
            # is retained below as a versioned candidate, never selected audio.
        elif key not in merged:
            merged[key] = value
    candidates = list(old.get('voice_design_candidates', []))
    new_design = new.get('voice_design')
    if isinstance(new_design, dict) and any(new_design.get(k) for k in ('description', 'rationale', 'creative_choices')):
        candidate = {'design': new_design, 'sources': new.get('profile_sources', [])}
        if _digest(candidate) not in {_digest(x) for x in candidates if isinstance(x, dict)}:
            candidates.append(candidate)
    if candidates:
        merged['voice_design_candidates'] = candidates
    return merged


def _merge_registry(existing, normalized, batch_info):
    doc = dict(existing or {})
    doc.setdefault('version', 1); doc['mode'] = 'character_profiles_v1'
    doc['book_id'] = normalized['book_id']; doc['status'] = 'needs_review'; doc['human_reviewed'] = False
    sources = doc.get('source') if isinstance(doc.get('source'), dict) else {}
    source_chapters = list(sources.get('chapters', []))
    source_by_id = {(x.get('chapter_id'), x.get('text_sha256')) for x in source_chapters if isinstance(x, dict)}
    source_conflicts = list(doc.get('source_conflicts', []))
    for item in normalized.get('source', {}).get('chapters', []):
        same_id = [x for x in source_chapters if isinstance(x, dict) and x.get('chapter_id') == item['chapter_id']]
        if same_id and all(x.get('text_sha256') != item['text_sha256'] for x in same_id):
            source_conflicts.append({'chapter_id': item['chapter_id'], 'existing': same_id,
                                     'candidate': item, 'type': 'source_hash_conflict'})
        if (item['chapter_id'], item['text_sha256']) not in source_by_id:
            source_chapters.append(item); source_by_id.add((item['chapter_id'], item['text_sha256']))
    sources['chapters'] = source_chapters; doc['source'] = sources
    if normalized.get('narration_design'):
        before = doc.get('narration_design')
        candidate = normalized['narration_design']
        if not isinstance(before, dict) or not before.get('description'):
            doc['narration_design'] = candidate
        elif before != candidate:
            doc.setdefault('profile_conflicts', []).append({'type': 'narration_design_candidate',
                'existing': before, 'candidate': candidate,
                'sources': normalized.get('source', {}).get('chapters', [])})
    profiles = list(doc.get('characters', [])); by_name = {p.get('name'): i for i, p in enumerate(profiles) if isinstance(p, dict) and p.get('name')}
    conflicts = list(doc.get('profile_conflicts', []))
    unresolved_profiles = list(doc.get('unresolved_profiles', []))
    trusted_source_hashes = {}
    for source_row in sources.get('chapters', []):
        if isinstance(source_row, dict) and isinstance(source_row.get('chapter_id'), str):
            trusted_source_hashes.setdefault(source_row['chapter_id'], set()).add(source_row.get('text_sha256'))

    def resolve_existing_name(name):
        exact = [p for p in profiles if isinstance(p, dict) and p.get('name') == name]
        alias_owners = []
        for profile in profiles:
            if not isinstance(profile, dict) or not isinstance(profile.get('name'), str):
                continue
            for alias in profile.get('aliases', []) if isinstance(profile.get('aliases'), list) else []:
                if not isinstance(alias, dict) or alias.get('name') != name:
                    continue
                proven = []
                for ref in alias.get('evidence', []) if isinstance(alias.get('evidence'), list) else []:
                    if (not isinstance(ref, dict) or not isinstance(ref.get('chapter_id'), str)
                            or ref.get('source_sha256') not in trusted_source_hashes.get(ref.get('chapter_id'), set())
                            or not isinstance(ref.get('quote'), str)):
                        continue
                    if _identity_linked(ref['quote'], profile['name'], name):
                        proven.append(ref)
                alias_owners.append({'profile': profile, 'evidence': proven})
        owner_names = {row['profile']['name'] for row in alias_owners}
        if len(exact) > 1 or len(owner_names) > 1 or (exact and owner_names and owner_names != {exact[0]['name']}):
            return 'ambiguous', exact, alias_owners
        if exact:
            return 'exact', exact, alias_owners
        if alias_owners:
            if len(owner_names) != 1:
                return 'ambiguous', exact, alias_owners
            if any(row['evidence'] for row in alias_owners):
                return 'proven_alias', [alias_owners[0]['profile']], alias_owners
            return 'unproven_alias', [], alias_owners
        return 'new', [], []

    for candidate in normalized.get('characters', []):
        profile = dict(candidate)
        profile_sources = [{'chapter_id': c['chapter_id'], 'source_sha256': c['text_sha256']}
                           for c in normalized.get('source', {}).get('chapters', [])]
        profile['profile_sources'] = profile_sources
        name = candidate['name']
        resolution, matches, alias_owners = resolve_existing_name(name)
        if resolution in ('ambiguous', 'unproven_alias'):
            possible = sorted({row['profile']['name'] for row in alias_owners}
                              | {p['name'] for p in matches if isinstance(p, dict) and isinstance(p.get('name'), str)})
            conflict = {'type': 'ambiguous_profile_alias' if resolution == 'ambiguous' else 'unproven_profile_alias',
                'candidate_name': name, 'possible_profiles': possible, 'candidate': candidate,
                'sources': profile_sources}
            conflicts.append(conflict); unresolved_profiles.append(conflict)
            continue
        if resolution == 'proven_alias':
            canonical = matches[0]['name']
            # Keep the historical canonical name. The new surface name is a
            # source-bound variant; it does not rewrite the old profile name.
            profile['name_variants'] = [{'name': name,
                'evidence': profile.get('name_evidence', []), 'sources': profile_sources,
                'identity_resolution': 'matched_existing_explicit_alias'}]
            profile.pop('name_evidence', None)
            profile['name'] = canonical
        if name in by_name:
            profiles[by_name[name]] = _merge_character(profiles[by_name[name]], profile, conflicts)
        elif resolution == 'proven_alias' and profile['name'] in by_name:
            index = by_name[profile['name']]
            merged = _merge_character(profiles[index], profile, conflicts)
            variants = list(profiles[index].get('name_variants', []))
            for variant in profile.get('name_variants', []):
                if _digest(variant) not in {_digest(x) for x in variants if isinstance(x, dict)}:
                    variants.append(variant)
            merged['name_variants'] = variants
            profiles[index] = merged
        else:
            profile['voice_design_candidates'] = ([{'design': profile['voice_design'], 'sources': profile_sources}]
                if isinstance(profile.get('voice_design'), dict) else [])
            profiles.append(profile); by_name[name] = len(profiles)-1
    doc['characters'] = profiles
    def unique_rows(rows):
        result=[];seen=set()
        for row in rows:
            marker=_digest(row)
            if marker not in seen:result.append(row);seen.add(marker)
        return result
    doc['profile_conflicts'] = unique_rows(conflicts)
    doc['unresolved_profiles'] = unique_rows(unresolved_profiles)
    doc['source_conflicts'] = unique_rows(source_conflicts)
    aliases = {}
    for profile in profiles:
        if isinstance(profile, dict) and isinstance(profile.get('name'), str):
            aliases.setdefault(profile['name'], []).append({'name': profile['name'],
                'kind': 'canonical_name', 'evidence': profile.get('name_evidence', [])})
        for alias in profile.get('aliases', []) if isinstance(profile.get('aliases'), list) else []:
            if isinstance(alias, dict) and alias.get('name'):
                aliases.setdefault(alias['name'], []).append({'name': profile.get('name'),
                    'kind': 'alias', 'evidence': alias.get('evidence', [])})
    doc['alias_conflicts'] = [{'alias': alias, 'candidates': rows, 'status': 'ambiguous_no_identity_merge'}
        for alias, rows in sorted(aliases.items()) if len({r['name'] for r in rows}) > 1]
    relations = list(doc.get('relations', [])) if isinstance(doc.get('relations', []), list) else []
    unresolved_relations = list(doc.get('unresolved_relations', [])) if isinstance(doc.get('unresolved_relations', []), list) else []
    for candidate in normalized.get('relations', []):
        if not isinstance(candidate, dict):
            unresolved_relations.append({'candidate': candidate, 'reason': 'invalid_relation_candidate'})
            continue
        row = dict(candidate)
        evidence = row.get('evidence') if isinstance(row.get('evidence'), list) else []
        valid_evidence = [ref for ref in evidence if isinstance(ref, dict)
            and ref.get('chapter_id') in trusted_source_hashes
            and ref.get('source_sha256') in trusted_source_hashes[ref.get('chapter_id')]
            and isinstance(ref.get('quote'), str) and ref.get('quote')]
        if len(valid_evidence) != len(evidence) or not evidence:
            unresolved_relations.append({'candidate': row, 'reason': 'relation_evidence_untrusted_or_missing'})
            continue
        endpoints = []
        for field in ('source', 'target'):
            name = row.get(field)
            resolution, matches, _owners = resolve_existing_name(name) if isinstance(name, str) else ('new', [], [])
            if resolution == 'exact' and len(matches) == 1:
                endpoints.append(matches[0]['name'])
            elif resolution == 'proven_alias' and len(matches) == 1:
                endpoints.append(matches[0]['name'])
            else:
                endpoints.append(None)
        if not endpoints[0] or not endpoints[1] or endpoints[0] == endpoints[1]:
            unresolved_relations.append({'candidate': row, 'reason': 'relation_endpoint_missing_ambiguous_or_same'})
            continue
        if (not isinstance(row.get('label'), str) or not row['label'].strip() or len(row['label']) > 80
                or row.get('status') not in ('explicit', 'inferred')):
            unresolved_relations.append({'candidate': row, 'reason': 'relation_label_or_status_invalid'})
            continue
        row['source'], row['target'] = endpoints
        row['evidence'] = valid_evidence
        prior = next((item for item in relations if isinstance(item, dict)
            and item.get('source') == row['source'] and item.get('target') == row['target']
            and item.get('label') == row['label'] and item.get('status') == row['status']), None)
        if prior is None:
            relations.append(row)
        else:
            prior['evidence'] = _merge_evidence(prior.get('evidence', []), valid_evidence)
    for candidate in normalized.get('unresolved_relations', []):
        if isinstance(candidate, dict):
            unresolved_relations.append(candidate)
    doc['relations'] = unique_rows(relations)
    doc['unresolved_relations'] = unique_rows(unresolved_relations)
    jobs = list(doc.get('profile_jobs', []))
    if _digest(batch_info) not in {_digest(x) for x in jobs if isinstance(x, dict)}:
        jobs.append(batch_info)
    doc['profile_jobs'] = jobs
    return doc


def _commit_registry(root, book_id, document):
    base = Path(root) / 'character-profiles' / book_id
    versions = base / 'versions'; versions.mkdir(parents=True, exist_ok=True)
    raw = workflow._bytes(document); version_sha = _hash(raw)
    version_path = versions / (version_sha + '.json')
    try:
        with version_path.open('xb') as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    except FileExistsError:
        if version_path.read_bytes() != raw:
            raise ValueError('immutable profile version collision')
    current = base / 'current.json'; temp = base / ('.current-' + uuid.uuid4().hex + '.tmp')
    with temp.open('xb') as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    os.replace(temp, current)
    return str(current), str(version_path), version_sha


def _prepare_batches(book_id, chapter_ids, index_sha256, index_request):
    chapters = []; payloads = []
    for chapter_id in chapter_ids:
        response = index_request('prepare', book_id, chapter_id)
        chapter, payload = _source_chapter(response, book_id, chapter_id, index_sha256)
        chapters.append(chapter); payloads.append(payload)
    batches = []; current = []; size = 0
    for item in chapters:
        count = len(item['text'])
        if current and (len(current) >= 3 or size + count > 60000):
            batches.append(current); current = []; size = 0
        if count > 60000:
            raise ValueError('单章超过画像协议60000码点限制')
        current.append(item); size += count
    if current:
        batches.append(current)
    return batches, {p['chapter_id']: p for p in payloads}


def _run_batch(book_id, chapters, root, dispatcher, preflight, *, skip_preflight=False,
               target_names=None):
    targeted_names = sorted(set(target_names or []))
    discovery = not bool(targeted_names)
    last = chapters[-1]['chapter_id']
    request = {'mode': 'character_profiles_v1', 'book_id': book_id, 'chapter_id': last,
        'discover_characters': discovery, 'target_names': [] if discovery else targeted_names,
        'chapters': [{'chapter_id': x['chapter_id'], 'text': x['text'], 'text_sha256': x['text_sha256']} for x in chapters]}
    raw_input = workflow._bytes(request); input_sha = _hash(raw_input)
    source_scope = {'book_id': book_id, 'chapters': [(c['chapter_id'], c['text_sha256']) for c in chapters]}
    # Keep the legacy discovery cache identity byte-for-byte stable. Targeted
    # enrichments have their own purpose and names so they cannot collide.
    source_digest = (_digest(source_scope) if discovery else
        _digest({'purpose': 'targeted_character_profiles_v1', **source_scope, 'target_names': targeted_names}))
    batch_dir = Path(root) / 'profile-tasks' / book_id / source_digest
    if batch_dir.exists():
        try:
            result, usage, receipt = _read_or_recover_success(batch_dir, input_sha)
            return result, usage, receipt, batch_dir, source_digest, True
        except ProfileJobError:
            raise
    source_snapshots={'worker-source.mjs':WORKER.read_bytes(),
                      'protocol-source.mjs':PROFILE_PROTOCOL.read_bytes()}
    if not skip_preflight:
        preflight()
    batch_dir.mkdir(parents=True, exist_ok=False)
    for filename, source in source_snapshots.items():
        with (batch_dir/filename).open('xb') as stream:
            stream.write(source); stream.flush(); os.fsync(stream.fileno())
    input_path = batch_dir / 'input.json'
    with input_path.open('xb') as handle:
        handle.write(raw_input); handle.flush(); os.fsync(handle.fileno())
    receipt_dir = batch_dir / 'receipt'
    jobs_dir = batch_dir / 'jobs'; jobs_dir.mkdir()
    try:
        dispatched = dispatcher(input_path, receipt_dir, jobs_dir)
    except ProfileJobError as exc:
        if exc.status == 'credential_unavailable':
            profile_receipt = receipt_dir / 'character_profiles_v1'
            if (not (profile_receipt/'intent.json').exists() and not (profile_receipt/'response.body').exists()
                    and jobs_dir.is_dir() and not any(jobs_dir.iterdir())
                    and set(p.name for p in batch_dir.iterdir()) <= {'input.json','jobs',*source_snapshots}):
                for filename in source_snapshots:
                    (batch_dir/filename).unlink(missing_ok=True)
                input_path.unlink(missing_ok=True); jobs_dir.rmdir(); batch_dir.rmdir()
        raise
    except Exception as exc:
        raise ProfileJobError('usage_unknown', '画像批次失败或状态未知；不会自动重发', artifact_path=batch_dir) from exc
    if not isinstance(dispatched, dict) or dispatched.get('status') not in ('needs_review', 'complete', 'invalid_or_usage_unknown'):
        raise ProfileJobError('usage_unknown', '画像worker未确认完成；不会自动重发', artifact_path=batch_dir)
    result, usage, receipt = _read_or_recover_success(batch_dir, input_sha)
    normalized = result['result']
    expected = [{'chapter_id': c['chapter_id'], 'text_sha256': c['text_sha256']} for c in chapters]
    actual = normalized.get('source', {}).get('chapters')
    if (normalized.get('book_id') != book_id or not isinstance(actual, list) or len(actual) != len(expected)
            or any(not isinstance(a, dict) or a.get('chapter_id') != e['chapter_id']
                   or a.get('text_sha256') != e['text_sha256'] for a, e in zip(actual, expected))):
        raise ProfileJobError('invalid_result', '画像结果来源不匹配；不会重发', artifact_path=batch_dir)
    return result, usage, receipt, batch_dir, source_digest, False


def _existing_registry(root, book_id):
    path = Path(root) / 'character-profiles' / book_id / 'current.json'
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ProfileJobError('invalid_registry', '当前画像注册表无法读取', artifact_path=path) from exc
    if doc.get('book_id') != book_id or doc.get('mode') != 'character_profiles_v1' or not isinstance(doc.get('characters'), list):
        raise ProfileJobError('invalid_registry', '当前画像注册表身份或schema不匹配', artifact_path=path)
    return doc


def _merge_and_commit(root, book_id, result, usage, receipt, batch_dir, source_digest, reused):
    # Receipt/network work completes before this short book-scoped OS lock.
    lock_root = Path(root) / 'profile-registry-locks'
    with _book_os_lock(lock_root, book_id):
        existing = _existing_registry(root, book_id)
        profile_result = result['result']
        batch_info = {'batch_source_sha256': source_digest, 'input_sha256': _hash((batch_dir/'input.json').read_bytes()),
            'receipt_dir': str(receipt), 'result_path': str(receipt/'result.json'),
            'usage': usage, 'status': 'needs_review', 'human_reviewed': False,
            'chapters': [{'chapter_id': row['chapter_id'], 'text_sha256': row['text_sha256']}
                for row in profile_result.get('source', {}).get('chapters', []) if isinstance(row, dict)]}
        document = _merge_registry(existing, profile_result, batch_info)
        current, version, version_sha = _commit_registry(root, book_id, document)
        return current, version, version_sha


def analyze_profile_range(name, start, end, *, root=None, index_request=None, dispatcher=None, preflight=None):
    """Discover and merge profiles for a selected, verified chapter range.

    Names may be a UI registered book name or a book ID. start/end are chapter
    IDs inclusive in the index order. Each provider call receives at most three
    chapters. A completed same-source batch is reused; any uncertain reservation
    stops without retry.
    """
    book_id = _book_id(name)
    root = Path(root) if root is not None else _runtime_root()
    index_request = index_request or flow.index_request
    use_default = dispatcher is None
    custom_preflight = preflight is not None
    dispatcher = dispatcher or _default_dispatcher
    preflight = preflight or (_default_preflight if use_default else (lambda: None))
    listing = index_request('list', book_id)
    ids = listing.get('chapters') if isinstance(listing, dict) else None
    index_sha = listing.get('index_sha256') if isinstance(listing, dict) else None
    if (listing.get('book_id') != book_id or listing.get('human_reviewed') is not False
            or not isinstance(ids, list) or not all(isinstance(x, str) and CHAPTER.fullmatch(x) for x in ids)
            or len(ids) != len(set(ids)) or not isinstance(index_sha, str) or not HASH.fullmatch(index_sha)):
        raise ValueError('verified chapter list invalid')
    if start not in ids or end not in ids or ids.index(start) > ids.index(end):
        raise ValueError('invalid chapter range')
    chapter_ids = ids[ids.index(start):ids.index(end)+1]
    batches, _payloads = _prepare_batches(book_id, chapter_ids, index_sha, index_request)
    completed = []; total = {'prompt_tokens': 0, 'completion_tokens': 0,
                             'known_usage_batches': 0, 'unknown_batches': 0,
                             'unknown_usage_batches': 0}
    current_path = version_path = None
    for chapters in batches:
        try:
            result, usage, receipt, batch_dir, source_digest, reused = _run_batch(book_id, chapters,
                root, dispatcher, preflight, skip_preflight=(not use_default and not custom_preflight))
            current_path, version_path, version_sha = _merge_and_commit(root, book_id, result,
                usage, receipt, batch_dir, source_digest, reused)
            total['prompt_tokens'] += usage['prompt_tokens']; total['completion_tokens'] += usage['completion_tokens']
            total['known_usage_batches'] += 1
            completed.append({'chapters': [c['chapter_id'] for c in chapters], 'status': 'needs_review',
                'usage': usage, 'result_path': str(receipt/'result.json'), 'batch_source_sha256': source_digest,
                'reused': reused, 'version_path': version_path, 'version_sha256': version_sha})
        except ProfileJobError as exc:
            total['unknown_batches'] += 1
            if isinstance(getattr(exc, 'usage', None), dict):
                total['prompt_tokens'] += exc.usage['prompt_tokens']
                total['completion_tokens'] += exc.usage['completion_tokens']
                total['known_usage_batches'] += 1
            if exc.status in ('usage_unknown', 'unknown'):
                total['unknown_usage_batches'] += 1
            exc.batches = completed
            exc.result = {'status': 'partial' if completed else exc.status, 'book_id': book_id,
                'batches': completed, 'usage': total, 'current_path': current_path,
                'failed_artifact_path': exc.artifact_path, 'failure_status': exc.status,
                'retry_allowed': False}
            raise
    return {'status': 'needs_review', 'book_id': book_id, 'start': start, 'end': end,
        'batches': completed, 'usage': total, 'current_path': current_path,
        'version_path': version_path, 'version_sha256': version_sha, 'human_reviewed': False}


def ensure_profiles_for_chapter(payload, *, root, dispatcher=None, preflight=None):
    """Ensure a verified chapter has a saved rich profile-discovery candidate."""
    if not isinstance(payload, dict) or not BOOK.fullmatch(str(payload.get('book_id', ''))) or not CHAPTER.fullmatch(str(payload.get('chapter_id', ''))):
        raise ValueError('invalid chapter payload identity')
    book_id, chapter_id = payload['book_id'], payload['chapter_id']
    text = payload.get('text')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('chapter text missing')
    required = payload.get('profile_required_names', [])
    if (not isinstance(required, list) or len(required) > 100
            or any(not isinstance(name, str) or not name.strip() or len(name) > 80
                   or name not in text for name in required)):
        raise ValueError('invalid required profile names')
    required = sorted(set(required))
    source_sha = _hash(text.encode('utf-8'))
    registry = _existing_registry(root, book_id)
    missing = _missing_required_profiles(registry, required) if registry and required else list(required)
    if registry:
        sources = registry.get('source', {}).get('chapters', [])
        chapter_current = any(c.get('chapter_id') == chapter_id and c.get('text_sha256') == source_sha
                              for c in sources if isinstance(c, dict))
        if chapter_current and not missing:
            return {'status': 'already_current', 'book_id': book_id,
                    'current_path': str(Path(root)/'character-profiles'/book_id/'current.json'),
                    'human_reviewed': False}
    chapter = {'chapter_id': chapter_id, 'text': text, 'text_sha256': source_sha}
    # Internal one-chapter path avoids rereading the index in AutoBridge; caller
    # has already verified this payload. It still uses the identical receipt flow.
    book_result = _run_payload_batches(book_id, [[chapter]], root, dispatcher, preflight,
                                       target_names=missing if required else None)
    return book_result


def _missing_required_profiles(registry, required_names):
    """Return requested labels absent from canonical or proven unique aliases."""
    if not required_names:
        return []
    sources = registry.get('source', {}).get('chapters', [])
    trusted = {}
    for row in sources if isinstance(sources, list) else []:
        if isinstance(row, dict) and isinstance(row.get('chapter_id'), str):
            trusted.setdefault(row['chapter_id'], set()).add(row.get('text_sha256'))
    profiles = [p for p in registry.get('characters', []) if isinstance(p, dict)
                and isinstance(p.get('name'), str)]
    labels = {}
    for profile in profiles:
        labels.setdefault(profile['name'], []).append(('canonical', profile, True))
        for alias in profile.get('aliases', []) if isinstance(profile.get('aliases'), list) else []:
            if not isinstance(alias, dict) or not isinstance(alias.get('name'), str):
                continue
            proven = any(isinstance(ref, dict) and isinstance(ref.get('chapter_id'), str)
                and ref.get('source_sha256') in trusted.get(ref.get('chapter_id'), set())
                and isinstance(ref.get('quote'), str)
                and _identity_linked(ref['quote'], profile['name'], alias['name'])
                for ref in alias.get('evidence', []) if isinstance(alias.get('evidence'), list))
            labels.setdefault(alias['name'], []).append(('alias', profile, proven))
    missing = []
    for name in required_names:
        owners = labels.get(name, [])
        owner_names = {row[1]['name'] for row in owners}
        canonical = [row for row in owners if row[0] == 'canonical']
        proven_aliases = [row for row in owners if row[0] == 'alias' and row[2]]
        ambiguous = len(owner_names) > 1 or len(canonical) > 1
        if not ambiguous and (canonical or len(proven_aliases) == 1):
            continue
        missing.append(name)
    return missing


def _run_payload_batches(book_id, batches, root, dispatcher=None, preflight=None, *, target_names=None):
    use_default = dispatcher is None
    custom_preflight = preflight is not None
    dispatcher = dispatcher or _default_dispatcher
    preflight = preflight or (_default_preflight if use_default else (lambda: None))
    completed=[]; usage_total={'prompt_tokens':0,'completion_tokens':0,'known_usage_batches':0,
        'unknown_batches':0,'unknown_usage_batches':0}
    current_path=version_path=None
    for chapters in batches:
        try:
            result, usage, receipt, batch_dir, source_digest, reused = _run_batch(book_id, chapters, root,
                dispatcher, preflight, skip_preflight=(not use_default and not custom_preflight),
                target_names=target_names)
            current_path,version_path,version_sha=_merge_and_commit(root,book_id,result,usage,receipt,batch_dir,source_digest,reused)
            usage_total['prompt_tokens']+=usage['prompt_tokens'];usage_total['completion_tokens']+=usage['completion_tokens']
            usage_total['known_usage_batches']+=1
            completed.append({'chapters':[c['chapter_id'] for c in chapters],'status':'needs_review','usage':usage,
                'result_path':str(receipt/'result.json'),'reused':reused,'version_path':version_path,'version_sha256':version_sha})
        except ProfileJobError as exc:
            usage_total['unknown_batches']+=1
            if isinstance(getattr(exc,'usage',None),dict):
                usage_total['prompt_tokens']+=exc.usage['prompt_tokens']
                usage_total['completion_tokens']+=exc.usage['completion_tokens']
                usage_total['known_usage_batches']+=1
            if exc.status in ('usage_unknown','unknown'):usage_total['unknown_usage_batches']+=1
            exc.batches=completed
            exc.result={'status':'partial' if completed else exc.status,'book_id':book_id,'batches':completed,
                'usage':usage_total,'current_path':current_path,'failed_artifact_path':exc.artifact_path,
                'failure_status':exc.status,'retry_allowed':False}
            raise
    return {'status':'needs_review','book_id':book_id,'batches':completed,'usage':usage_total,
        'current_path':current_path,'version_path':version_path,'version_sha256':version_sha,'human_reviewed':False}
