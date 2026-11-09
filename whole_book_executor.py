"""Fail-closed scheduler for whole_book_plan using injected worker callbacks.

The executor itself performs no model, network, file synthesis, or book mutation.
Every operation is delegated to a caller-provided callback. A callback returns
``{'output': {...}, 'receipt': {...}}`` with hashes, billing state, and cache
validation. The executor checkpoints dispatch before calling it and checkpoints
the validated output before committing the task, enabling conservative recovery.
Receipt structure and request binding are validated here, but those checks do
not authenticate a receipt's origin. Real worker integration must first verify
the raw billing receipt and its hash against the trusted ledger adapter.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import whole_book_plan as plan_api
import attribution_staging_adapter as attribution_staging


class ExecutorBlocked(RuntimeError):
    pass


class StaleInputs(ExecutorBlocked):
    pass


Worker = Callable[[dict[str, Any]], dict[str, Any]]


def payload_sha256(value: Any) -> str:
    """Hash JSON payloads with the same canonical encoding used by plan inputs."""
    return plan_api._digest(value)


def _checkpoint_path(plan_path: Path) -> Path:
    return plan_path.with_name(plan_path.name + '.executor-checkpoints.json')


def _read_checkpoints(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {'version': 1, 'attempts': {}}
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _checkpoint_hash(record: dict[str, Any]) -> str:
    value = copy.deepcopy(record)
    value.pop('checkpoint_sha256', None)
    return payload_sha256(value)


_OUTPUT_FIELDS = {
    'chapter_analysis': {'profile', 'summary'},
    'whole_book_cast_fusion': {'characters', 'no_characters_detected', 'unresolved_conflicts'},
    'dialogue_attribution': {'assignments', 'unknown_ids', 'conflicts'},
    'voice_design_text': {'design_text', 'uses_real_person_reference'},
    'local_voice_synthesis': {'voice_id', 'fixed', 'local_only', 'uses_real_person_reference'},
    'chapter_render': {'artifact_sha256', 'artifact_ref', 'staging_only', 'human_reviewed',
                       'staging_audit', 'staging_audit_sha256', 'staging_package_sha256',
                       'render_input_sha256', 'attribution_task_id',
                       'attribution_receipt_sha256', 'attribution_output_sha256'},
    'output_verification': {'artifact_sha256', 'artifact_ref', 'verified', 'exists',
                            'staging_audit_sha256', 'render_input_sha256'},
}
_RECEIPT_FIELDS = {
    'receipt_id', 'task_id', 'idempotency_key', 'input_fingerprint', 'output_sha256',
    'cache_status', 'cache_sha256', 'billing_status', 'billing_receipt', 'request_id', 'response_id',
}
_BILLING_FIELDS = {'receipt_id', 'task_id', 'idempotency_key', 'request_id', 'response_id', 'usage'}
_USAGE_FIELDS = {'input_tokens', 'output_tokens'}
_SECRET_KEY = re.compile(
    r'(?:api.?key|(?:api|access|refresh|auth|bearer|session|client).?token|'
    r'authorization|secret|password|credential|cookie|private.?key)', re.IGNORECASE)


def _scrub_expected(value: Any, *, allowed_keys: set[str] | None = None, depth: int = 0) -> Any:
    """JSON-safe recursive copy, dropping credential-like keys and excess depth."""
    if depth > 32:
        return '[truncated-depth]'
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or _SECRET_KEY.search(key):
                continue
            if allowed_keys is not None and key not in allowed_keys:
                continue
            child_keys = _USAGE_FIELDS if depth > 0 and key == 'usage' else None
            result[key] = _scrub_expected(item, allowed_keys=child_keys, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_scrub_expected(item, depth=depth + 1) for item in value[:10000]]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError('worker response contains a non-JSON value')


def _safe_worker_response(response: Any, task: dict[str, Any]) -> dict[str, Any]:
    """Capture only the declared output and receipt contract before validation."""
    if not isinstance(response, dict):
        return {'response_type': type(response).__name__}
    output = response.get('output')
    receipt = response.get('receipt')
    if isinstance(output, dict):
        output = _scrub_expected(output, allowed_keys=_OUTPUT_FIELDS.get(task.get('stage'), set()))
    else:
        output = _scrub_expected(output)
    if isinstance(receipt, dict):
        receipt = _scrub_expected(receipt, allowed_keys=_RECEIPT_FIELDS)
        billing = receipt.get('billing_receipt')
        if isinstance(billing, dict):
            receipt['billing_receipt'] = _scrub_expected(billing, allowed_keys=_BILLING_FIELDS)
    else:
        receipt = _scrub_expected(receipt)
    return {'output': output, 'receipt': receipt}


def _find_attempt(state: dict[str, Any], task_id: str, attempt: int) -> dict[str, Any] | None:
    values = state.get('attempts', {}).get(task_id, [])
    return next((item for item in reversed(values) if item.get('attempt') == attempt), None)


class WholeBookExecutor:
    """Execute ready plan tasks through a stage -> callback mapping.

    ``snapshot_provider`` returns the same keyword arguments accepted by
    ``create_or_update_plan``. The executor compares its input fingerprint
    before and after every worker call. Workers never receive paths to source
    books from this module; context contains plan metadata and prior task results.
    """

    def __init__(self, plan_path: str | Path, *, workers: dict[str, Worker],
                 snapshot_provider: Callable[[], dict[str, Any]],
                 render_input_provider: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
                 artifact_reader: Callable[[str], bytes] | None = None):
        self.plan_path = Path(plan_path)
        self.workers = dict(workers)
        self.snapshot_provider = snapshot_provider
        self.render_input_provider = render_input_provider
        self.artifact_reader = artifact_reader
        self.checkpoint_path = _checkpoint_path(self.plan_path)
        self.run_lock_path = self.plan_path.with_name(self.plan_path.name + '.executor-run.lock')

    def _current_snapshot(self) -> dict[str, Any]:
        snapshot = self.snapshot_provider()
        if not isinstance(snapshot, dict):
            raise StaleInputs('snapshot_provider 必须返回计划输入对象。')
        return snapshot

    def _sync_snapshot(self) -> dict[str, Any]:
        snapshot = self._current_snapshot()
        current = plan_api.create_or_update_plan(self.plan_path, **snapshot)
        if current.get('stale'):
            raise StaleInputs(current.get('stale_reason') or '计划输入已变化。')
        return current

    def _persist_checkpoint(self, record: dict[str, Any]) -> dict[str, Any]:
        lock = self.checkpoint_path.with_suffix(self.checkpoint_path.suffix + '.lock')
        with plan_api._file_lock(lock):
            state = _read_checkpoints(self.checkpoint_path)
            attempts = state.setdefault('attempts', {}).setdefault(record['task_id'], [])
            prior = next((i for i, item in enumerate(attempts)
                          if item.get('attempt') == record.get('attempt')), None)
            saved = copy.deepcopy(record)
            saved['checkpoint_sha256'] = _checkpoint_hash(saved)
            if prior is None:
                attempts.append(saved)
            else:
                attempts[prior] = saved
            plan_api._atomic_json(self.checkpoint_path, state)
            return copy.deepcopy(saved)

    def _verify_checkpoint(self, record: dict[str, Any], task: dict[str, Any], plan: dict[str, Any],
                           render_evidence: dict[str, Any] | None = None) -> bool:
        if not record or record.get('checkpoint_sha256') != _checkpoint_hash(record):
            return False
        if (record.get('task_id') != task.get('id')
                or record.get('attempt') != task.get('attempt')
                or record.get('idempotency_key') != task.get('idempotency_key')
                or record.get('input_fingerprint') != plan.get('input_fingerprint')):
            return False
        if task.get('stage') == 'chapter_render' and (
                render_evidence is None
                or record.get('render_input_sha256') != render_evidence.get('render_input_sha256')
                or record.get('staging_audit_sha256') != render_evidence.get('staging_audit_sha256')):
            return False
        if record.get('state') != 'receipt_ready':
            return False
        output = record.get('output')
        receipt = record.get('receipt')
        if not isinstance(output, dict) or not isinstance(receipt, dict):
            return False
        if receipt.get('output_sha256') != payload_sha256(output):
            return False
        if record.get('receipt_sha256') != payload_sha256(receipt):
            return False
        if receipt.get('task_id') != task.get('id') or receipt.get('idempotency_key') != task.get('idempotency_key'):
            return False
        if receipt.get('input_fingerprint') != record.get('input_fingerprint'):
            return False
        try:
            self._validate_billing(task, receipt)
        except ValueError:
            return False
        if receipt.get('cache_status') not in ('fresh', 'verified'):
            return False
        if receipt.get('cache_status') == 'verified' and receipt.get('cache_sha256') != receipt.get('output_sha256'):
            return False
        try:
            self._validate_output(task, output, self._dependencies(plan, task), plan,
                                  render_evidence, self.artifact_reader)
        except (ValueError, TypeError):
            return False
        return True

    def _validate_worker_response(self, response: Any, task: dict[str, Any],
                                  plan: dict[str, Any],
                                  render_evidence: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        if (not isinstance(response, dict) or set(response) != {'output', 'receipt'}
                or not isinstance(response.get('output'), dict)):
            raise ValueError('worker 必须返回 output 对象和 receipt 对象。')
        output = copy.deepcopy(response['output'])
        receipt = response.get('receipt')
        if not isinstance(receipt, dict):
            raise ValueError('worker 缺少执行收据。')
        safe = _safe_worker_response(response, task)
        if (payload_sha256(safe['output']) != payload_sha256(output)
                or payload_sha256(safe['receipt']) != payload_sha256(receipt)):
            raise ValueError('worker response 含协议外字段或敏感凭证字段。')
        if not isinstance(receipt.get('receipt_id'), str) or not receipt['receipt_id'].strip():
            raise ValueError('执行收据缺少 receipt_id。')
        if receipt.get('task_id') != task['id'] or receipt.get('idempotency_key') != task['idempotency_key']:
            raise ValueError('执行收据任务编号或幂等键不匹配。')
        if receipt.get('input_fingerprint') != plan['input_fingerprint']:
            raise ValueError('执行收据输入快照不匹配。')
        out_hash = payload_sha256(output)
        if receipt.get('output_sha256') != out_hash:
            raise ValueError('执行结果哈希不匹配。')
        if receipt.get('cache_status') not in ('fresh', 'verified'):
            raise ValueError('缓存状态未验证，不能复用结果。')
        if receipt.get('cache_status') == 'verified' and receipt.get('cache_sha256') != out_hash:
            raise ValueError('缓存内容哈希校验失败。')
        self._validate_billing(task, receipt)
        self._validate_output(task, output, self._dependencies(plan, task), plan,
                              render_evidence, self.artifact_reader)
        receipt = copy.deepcopy(receipt)
        return output, receipt

    @staticmethod
    def _validate_billing(task: dict[str, Any], receipt: dict[str, Any]) -> None:
        mode = (task.get('details') or {}).get('billing_mode')
        status = receipt.get('billing_status')
        if mode == 'external':
            if status != 'known':
                raise ValueError('外部模型阶段必须提供已知计费凭证。')
            billing = receipt.get('billing_receipt')
            if not isinstance(billing, dict):
                raise ValueError('计费凭证必须是结构化对象。')
            request_id = receipt.get('request_id')
            response_id = receipt.get('response_id')
            usage = billing.get('usage')
            if (not isinstance(request_id, str) or not request_id.strip()
                    or not isinstance(response_id, str) or not response_id.strip()
                    or billing.get('request_id') != request_id
                    or billing.get('response_id') != response_id
                    or billing.get('task_id') != task.get('id')
                    or billing.get('idempotency_key') != receipt.get('idempotency_key')
                    or not isinstance(billing.get('receipt_id'), str) or not billing['receipt_id'].strip()
                    or not isinstance(usage, dict)):
                raise ValueError('计费凭证与本次请求/响应/任务未绑定。')
            input_tokens, output_tokens = usage.get('input_tokens'), usage.get('output_tokens')
            if (type(input_tokens) is not int or input_tokens < 0
                    or type(output_tokens) is not int or output_tokens < 0
                    or input_tokens + output_tokens < 1):
                raise ValueError('计费凭证缺少有效的输入/输出用量。')
        elif mode == 'local':
            if status != 'not_applicable':
                raise ValueError('本地合成/渲染阶段必须明确标记 not_applicable。')
        else:
            raise ValueError('任务缺少明确的 billing_mode 策略。')

    @staticmethod
    def _validate_output(task: dict[str, Any], output: dict[str, Any],
                         dependencies: list[dict[str, Any]] | None = None,
                         plan: dict[str, Any] | None = None,
                         render_evidence: dict[str, Any] | None = None,
                         artifact_reader: Callable[[str], bytes] | None = None) -> None:
        stage = task['stage']
        if stage == 'chapter_analysis':
            profile = output.get('profile')
            summary = output.get('summary')
            if not isinstance(profile, dict):
                raise ValueError('章节画像必须是结构化 profile 对象。')
            profiled_characters = profile.get('characters')
            if not isinstance(profiled_characters, list):
                raise ValueError('章节画像必须包含 characters 列表；空列表需显式标记无人物。')
            if not profiled_characters and profile.get('no_characters_detected') is not True:
                raise ValueError('空人物列表必须显式标记 no_characters_detected=true。')
            if profiled_characters and profile.get('no_characters_detected') is True:
                raise ValueError('人物列表非空时不能标记 no_characters_detected=true。')
            profile_names = []
            for item in profiled_characters:
                if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name'].strip():
                    raise ValueError('章节画像含无效人物条目。')
                profile_names.append(item['name'].strip())
            if len(profile_names) != len(set(profile_names)):
                raise ValueError('章节画像含重复人物。')
            valid_summary = ((isinstance(summary, str) and bool(summary.strip()))
                             or (isinstance(summary, dict) and bool(summary))
                             or (isinstance(summary, list) and bool(summary)))
            if not valid_summary:
                raise ValueError('章节摘要必须是非空文本、对象或列表。')
        elif stage == 'whole_book_cast_fusion':
            characters = output.get('characters')
            if not isinstance(characters, list):
                raise ValueError('全书融合结果必须包含 characters 列表。')
            if not characters and output.get('no_characters_detected') is not True:
                raise ValueError('空融合清单必须显式标记 no_characters_detected=true。')
            names = [x.get('name') for x in characters if isinstance(x, dict)]
            if len(names) != len(characters) or len(names) != len(set(names)):
                raise ValueError('全书人物融合结果含重复或无效人物。')
            for character in characters:
                count = character.get('appearance_count', 0)
                if (not isinstance(character.get('name'), str) or not character['name'].strip()
                        or type(count) is not int or count < 0
                        or character.get('fixed_voice') is not None
                        and not isinstance(character.get('fixed_voice'), str)):
                    raise ValueError('融合人物缺少有效名称、出现次数或固定声线信息。')
            conflicts = output.get('unresolved_conflicts', [])
            if not isinstance(conflicts, list):
                raise ValueError('全书融合 unresolved_conflicts 必须是列表。')
            entity_ids = {item.get('entity_id') for item in characters
                          if isinstance(item.get('entity_id'), str)}
            chapter_ids = {item.get('id') for item in (plan or {}).get('inputs', {}).get('chapters', [])
                           if isinstance(item, dict)}
            allowed_conflict_fields = {
                'kind', 'chapter_id', 'index', 'name', 'evidence', 'link_to',
                'possible_entities', 'entity_id', 'claim', 'alias',
                'source_index', 'target_index', 'label',
            }
            for conflict in conflicts:
                if (not isinstance(conflict, dict) or set(conflict) - allowed_conflict_fields
                        or not isinstance(conflict.get('kind'), str) or not conflict['kind'].strip()
                        or conflict.get('chapter_id') not in chapter_ids):
                    raise ValueError('全书融合冲突记录格式无效或章节不属于当前计划。')
                if 'name' in conflict and conflict['name'] is not None and not isinstance(conflict['name'], str):
                    raise ValueError('融合冲突 name 字段无效。')
                if 'evidence' in conflict and conflict['evidence'] is not None and not isinstance(conflict['evidence'], str):
                    raise ValueError('融合冲突 evidence 字段无效。')
                if ('index' in conflict and
                        (type(conflict['index']) is not int or conflict['index'] < 0)):
                    raise ValueError('融合冲突 index 字段无效。')
                for field in ('source_index', 'target_index'):
                    if field in conflict and conflict[field] is not None and (
                            type(conflict[field]) is not int or conflict[field] < 0):
                        raise ValueError(f'融合冲突 {field} 字段无效。')
                for field in ('claim', 'alias', 'label'):
                    if field in conflict and conflict[field] is not None and not isinstance(conflict[field], str):
                        raise ValueError(f'融合冲突 {field} 字段无效。')
                if 'entity_id' in conflict and conflict['entity_id'] is not None and conflict['entity_id'] not in entity_ids:
                    raise ValueError('融合冲突引用未知 entity_id。')
                # link_to is a model-proposed target, not a resolved identity
                # reference. identity_not_proven conflicts intentionally retain
                # bad/nonexistent targets so they remain reviewable; never use
                # this field to establish an entity relationship.
                if ('link_to' in conflict and conflict['link_to'] is not None
                        and (not isinstance(conflict['link_to'], str) or not conflict['link_to'].strip())):
                    raise ValueError('融合冲突 link_to 候选无效。')
                possible = conflict.get('possible_entities', [])
                if (not isinstance(possible, list)
                        or any(not isinstance(ident, str) or ident not in entity_ids for ident in possible)):
                    raise ValueError('融合冲突 possible_entities 引用无效。')
            if characters and output.get('no_characters_detected') is True:
                raise ValueError('融合人物非空时不能标记 no_characters_detected=true。')
            if plan is not None:
                known = {item['name'] for item in plan.get('inputs', {}).get('initial_character_hints', [])}
                missing = known - set(names)
                if missing:
                    raise ValueError('全书融合遗漏初始已知人物：' + '、'.join(sorted(missing)))
        elif stage == 'dialogue_attribution':
            assignments = output.get('assignments')
            unknown_ids = output.get('unknown_ids')
            conflicts = output.get('conflicts')
            if not isinstance(assignments, list) or not isinstance(unknown_ids, list) or not isinstance(conflicts, list):
                raise ValueError('对白归属必须显式返回 assignments、unknown_ids 和 conflicts。')
            by_id = {}
            uncertain_names = set()
            fusion = next((dep for dep in dependencies or []
                           if dep.get('stage') == 'whole_book_cast_fusion'), None)
            if fusion:
                fusion_output = (fusion.get('result') or {}).get('output') or {}
                entities = fusion_output.get('characters', [])
                entity_names = {item.get('entity_id'): item.get('name') for item in entities
                                if isinstance(item, dict) and isinstance(item.get('entity_id'), str)}
                for character in entities:
                    if isinstance(character, dict) and character.get('uncertainties'):
                        uncertain_names.add(character.get('name'))
                for conflict in fusion_output.get('unresolved_conflicts', []):
                    if isinstance(conflict.get('name'), str):
                        uncertain_names.add(conflict['name'])
                    entity_id = conflict.get('entity_id')
                    if entity_id in entity_names:
                        uncertain_names.add(entity_names[entity_id])
                    # A valid link_to is still only a proposed identity target.
                    # Keep that existing entity uncertain too; unknown targets
                    # remain unresolved candidates and do not taint any entity.
                    linked_name = entity_names.get(conflict.get('link_to'))
                    if linked_name:
                        uncertain_names.add(linked_name)
                    for entity_id in conflict.get('possible_entities', []):
                        if entity_id in entity_names:
                            uncertain_names.add(entity_names[entity_id])
            conflict_ids = set()
            for conflict in conflicts:
                if not isinstance(conflict, dict) or not isinstance(conflict.get('id'), str):
                    raise ValueError('对白归属冲突缺少对白编号。')
                if conflict['id'] in conflict_ids:
                    raise ValueError('对白归属冲突编号重复。')
                conflict_ids.add(conflict['id'])
            for item in assignments:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item['id'] in by_id:
                    raise ValueError('对白归属含无效或重复编号。')
                speaker = item.get('speaker')
                if not isinstance(speaker, str) or not speaker.strip():
                    raise ValueError('对白归属 speaker 必须为非空字符串。')
                status = item.get('identity_status')
                uncertainty = item.get('identity_uncertainty')
                if status is not None and status not in (
                        'confirmed', 'unconfirmed', 'unknown', 'ambiguous', 'conflict', 'model_inference',
                        'same_name_assumption'):
                    raise ValueError('对白归属 identity_status 无效。')
                if uncertainty is not None and (not isinstance(uncertainty, str) or not uncertainty.strip()
                                                or len(uncertainty) > 500):
                    raise ValueError('对白归属 identity_uncertainty 无效。')
                if speaker in uncertain_names:
                    if (status not in ('unconfirmed', 'unknown', 'ambiguous', 'conflict', 'model_inference',
                                       'same_name_assumption') or not uncertainty):
                        raise ValueError('融合身份不确定的人物必须在逐句归属中显式保留不确定性。')
                by_id[item['id']] = item
            for ident in unknown_ids:
                row = by_id.get(ident)
                if not isinstance(ident, str) or not row or row.get('speaker') not in (None, '', 'unknown'):
                    raise ValueError('未知对白必须保留为 unknown，不能自动分配人物。')
            if set(unknown_ids) != {ident for ident, item in by_id.items() if item['speaker'] == 'unknown'}:
                raise ValueError('unknown_ids 必须完整列出所有未知对白。')
        elif stage == 'voice_design_text':
            if not isinstance(output.get('design_text'), str) or not output['design_text'].strip():
                raise ValueError('角色声线设计结果缺少 design_text。')
            if output.get('uses_real_person_reference') is True:
                raise ValueError('角色声线设计不能使用真人参考声音。')
        elif stage == 'local_voice_synthesis':
            if not isinstance(output.get('voice_id'), str) or not output['voice_id'] or output.get('fixed') is not True:
                raise ValueError('本地合成结果必须给出已固定的 voice_id。')
            if output.get('local_only') is not True or output.get('uses_real_person_reference') is not False:
                raise ValueError('声线必须为本地文字合成，且不使用真人参考。')
        elif stage == 'chapter_render':
            if not plan_api._valid_hash(output.get('artifact_sha256')):
                raise ValueError('渲染结果缺少有效的输出 SHA-256。')
            if render_evidence is None or not callable(artifact_reader):
                raise ValueError('缺少独立渲染输入快照或 artifact reader。')
            if (output.get('staging_only') is not True or output.get('human_reviewed') is not False
                    or output.get('staging_audit') != render_evidence['staging_audit']
                    or output.get('staging_audit_sha256') != render_evidence['staging_audit_sha256']
                    or output.get('staging_package_sha256') != render_evidence['staging_package_sha256']
                    or output.get('render_input_sha256') != render_evidence['render_input_sha256']
                    or output.get('attribution_task_id') != render_evidence['attribution_task_id']
                    or output.get('attribution_receipt_sha256') != render_evidence['attribution_receipt_sha256']
                    or output.get('attribution_output_sha256') != render_evidence['attribution_output_sha256']):
                raise ValueError('渲染产物没有绑定执行器生成的暂存审计、章节归属与 cast 快照。')
            artifact_ref = output.get('artifact_ref')
            if not isinstance(artifact_ref, str) or not artifact_ref:
                raise ValueError('渲染结果缺少可由独立 reader 校验的 artifact_ref。')
            artifact_bytes = artifact_reader(artifact_ref)
            if (not isinstance(artifact_bytes, bytes) or not artifact_bytes
                    or hashlib.sha256(artifact_bytes).hexdigest() != output['artifact_sha256']):
                raise ValueError('artifact reader 返回的实际文件字节与渲染 SHA-256 不匹配。')
        elif stage == 'output_verification':
            if output.get('verified') is not True or not plan_api._valid_hash(output.get('artifact_sha256')):
                raise ValueError('输出验证未通过或缺少 artifact_sha256。')
            if output.get('exists') is not True:
                raise ValueError('已验证输出不存在。')
            renders = [dep for dep in dependencies or [] if dep.get('stage') == 'chapter_render']
            if len(renders) != 1:
                raise ValueError('输出验证必须对应唯一的章节渲染任务。')
            expected = (renders[0].get('result') or {}).get('output', {}).get('artifact_sha256')
            if not expected or output.get('artifact_sha256') != expected:
                raise ValueError('验证的 artifact 哈希与本章渲染结果不一致。')
            render_output = renders[0].get('result', {}).get('output', {})
            if (output.get('artifact_ref') != render_output.get('artifact_ref')
                    or output.get('staging_audit_sha256') != render_output.get('staging_audit_sha256')
                    or output.get('render_input_sha256') != render_output.get('render_input_sha256')):
                raise ValueError('输出验证没有继承本章 artifact/audit/render-input 绑定。')
            if not callable(artifact_reader):
                raise ValueError('缺少独立 artifact reader。')
            verified_bytes = artifact_reader(output['artifact_ref'])
            if (not isinstance(verified_bytes, bytes) or not verified_bytes
                    or hashlib.sha256(verified_bytes).hexdigest() != output['artifact_sha256']):
                raise ValueError('输出验证读取的实际 artifact 不匹配。')

    def _dependencies(self, plan: dict[str, Any], task: dict[str, Any]) -> list[dict[str, Any]]:
        indexed = {item['id']: item for item in plan['tasks']}
        values = []
        for ident in task.get('dependencies', []):
            dep = indexed.get(ident)
            if not dep or dep.get('status') != 'complete':
                raise ExecutorBlocked('任务依赖尚未完成。')
            dep_result = dep.get('result') or {}
            if (dep_result.get('checkpoint_receipt_validated') is not True
                    or not plan_api._valid_hash(dep_result.get('checkpoint_receipt_sha256'))):
                raise ExecutorBlocked(f"依赖任务 {dep.get('key')} 缺少已验证执行收据；拒绝继续。")
            values.append({'task_id': ident, 'key': dep['key'], 'stage': dep['stage'],
                           'chapter_id': dep.get('chapter_id'),
                           'result': copy.deepcopy(dep_result)})
        return values

    def _mark_render_binding_stale(self, chapter_id: str, binding: dict[str, Any]) -> None:
        snapshot = self._current_snapshot()
        bindings = dict(snapshot.get('attribution_bindings') or {})
        bindings[chapter_id] = binding
        snapshot['attribution_bindings'] = bindings
        plan_api.create_or_update_plan(self.plan_path, **snapshot)

    def _prepare_render_evidence(self, plan: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        """Build the isolated staging package from independently supplied live rows/cast."""
        if not callable(self.render_input_provider) or not callable(self.artifact_reader):
            raise ExecutorBlocked('真实渲染已关闭：缺少可信 render_input_provider 或 artifact_reader。')
        supplied = self.render_input_provider(copy.deepcopy(plan), copy.deepcopy(task))
        if (not isinstance(supplied, dict) or set(supplied) != {'script_rows', 'cast'}
                or not isinstance(supplied.get('script_rows'), list) or not isinstance(supplied.get('cast'), dict)):
            raise ExecutorBlocked('render_input_provider 必须返回 script_rows 与 cast。')
        script_rows = copy.deepcopy(supplied['script_rows'])
        cast = copy.deepcopy(supplied['cast'])
        try:
            binding = attribution_staging.capture_binding(plan['book_id'], task['chapter_id'], script_rows)
        except Exception as exc:
            raise ExecutorBlocked('当前话本无法生成安全章节绑定；渲染已封锁。') from exc
        expected_binding = plan.get('inputs', {}).get('attribution_bindings', {}).get(task['chapter_id'])
        if binding != expected_binding:
            self._mark_render_binding_stale(task['chapter_id'], binding)
            raise StaleInputs('当前对白与预先捕获的归属绑定不一致；计划已标 stale。')
        dependencies = self._dependencies(plan, task)
        attributions = [dep for dep in dependencies
                        if dep['stage'] == 'dialogue_attribution' and dep.get('chapter_id') == task['chapter_id']]
        if len(attributions) != 1:
            raise ExecutorBlocked('渲染必须依赖唯一的本章对白归属任务。')
        attribution = attributions[0]
        output = (attribution.get('result') or {}).get('output')
        if not isinstance(output, dict):
            raise ExecutorBlocked('本章归属结果缺失。')
        expansion = plan.get('voice_expansion') or {}
        characters = expansion.get('characters')
        if not isinstance(characters, list):
            raise ExecutorBlocked('缺少融合后角色清单。')
        cast_characters = cast.get('characters')
        narrator = cast.get('narrator')
        if not isinstance(cast_characters, dict) or not isinstance(narrator, dict):
            raise ExecutorBlocked('渲染 cast 结构无效。')
        expected_names = {item.get('name') for item in characters if isinstance(item, dict)}
        if len(expected_names) != len(characters) or set(cast_characters) != expected_names:
            raise ExecutorBlocked('渲染 cast 人物集合与已验证的融合角色集合不一致。')
        synthesized = {}
        for dep in dependencies:
            if dep['stage'] == 'local_voice_synthesis':
                name = (next((item for item in plan['tasks'] if item.get('id') == dep['task_id']), {})
                        .get('details', {}).get('character'))
                voice = (dep.get('result') or {}).get('output', {}).get('voice_id')
                if not isinstance(name, str) or not isinstance(voice, str) or not voice:
                    raise ExecutorBlocked('角色声音合成依赖缺少人物或固定音色编号。')
                synthesized[name] = voice
        for character in characters:
            name = character['name']
            expected_voice = character.get('fixed_voice') or synthesized.get(name)
            entry = cast_characters.get(name)
            if not expected_voice or not isinstance(entry, dict) or entry.get('voice') != expected_voice:
                raise ExecutorBlocked(f'{name} 的渲染音色与融合锁定/合成结果不一致。')
        try:
            package = attribution_staging.adapt_attribution(
                plan['book_id'], task['chapter_id'], script_rows, cast, output, binding)
        except Exception as exc:
            raise ExecutorBlocked('无法根据当前归属和捕获的话本构建安全暂存输入。') from exc
        audit = package.get('audit')
        if not isinstance(audit, dict) or audit.get('human_reviewed') is not False:
            raise ExecutorBlocked('暂存审计缺失或误标为人工审核。')
        audit_sha = payload_sha256({k: v for k, v in audit.items() if k != 'audit_sha256'})
        if audit.get('audit_sha256') != audit_sha or audit.get('binding') != binding:
            raise ExecutorBlocked('暂存审计哈希或章节绑定无效。')
        attribution_output_sha = payload_sha256(output)
        attribution_receipt_sha = (attribution.get('result') or {}).get('checkpoint_receipt_sha256')
        if not plan_api._valid_hash(attribution_receipt_sha):
            raise ExecutorBlocked('本章归属缺少有效收据哈希。')
        voice_receipts = sorted(
            ({'task_id': dep['task_id'],
              'receipt_sha256': (dep.get('result') or {}).get('checkpoint_receipt_sha256'),
              'voice_id': (dep.get('result') or {}).get('output', {}).get('voice_id')}
             for dep in dependencies if dep['stage'] == 'local_voice_synthesis'),
            key=lambda item: item['task_id'])
        if any(not plan_api._valid_hash(item['receipt_sha256']) for item in voice_receipts):
            raise ExecutorBlocked('本章角色声线缺少有效合成收据。')
        script_sha = payload_sha256(script_rows)
        cast_sha = payload_sha256(cast)
        staging_package_sha = payload_sha256({'script_rows': package['script_rows'], 'cast': package['cast']})
        render_input = {
            'book_id': plan['book_id'], 'chapter_id': task['chapter_id'],
            'binding': binding, 'script_sha256': script_sha, 'cast_sha256': cast_sha,
            'attribution_task_id': attribution['task_id'],
            'attribution_receipt_sha256': attribution_receipt_sha,
            'attribution_output_sha256': attribution_output_sha,
            'staging_audit_sha256': audit_sha, 'staging_package_sha256': staging_package_sha,
            'voice_receipts': voice_receipts,
        }
        return {
            'binding': binding, 'script_sha256': script_sha, 'cast_sha256': cast_sha,
            'source_cast': cast,
            'attribution_task_id': attribution['task_id'],
            'attribution_receipt_sha256': attribution_receipt_sha,
            'attribution_output_sha256': attribution_output_sha,
            'staging_package': package, 'staging_audit': audit,
            'staging_audit_sha256': audit_sha, 'staging_package_sha256': staging_package_sha,
            'voice_receipts': voice_receipts, 'render_input_sha256': payload_sha256(render_input),
        }

    def _worker_context(self, plan: dict[str, Any], task: dict[str, Any],
                        render_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        context = {
            'plan_id': plan['plan_id'], 'revision': plan['revision'],
            'book_id': plan['book_id'], 'input_fingerprint': plan['input_fingerprint'],
            'inputs': copy.deepcopy(plan['inputs']), 'task': copy.deepcopy(task),
            'idempotency_key': task['idempotency_key'],
            'dependencies': self._dependencies(plan, task),
        }
        if render_evidence is not None:
            context['render_evidence'] = copy.deepcopy(render_evidence)
        return context

    def _reconcile_running_checkpoint(self, plan: dict[str, Any]) -> str | None:
        running = next((task for task in plan['tasks'] if task.get('status') == 'running'), None)
        if not running:
            return None
        with plan_api._file_lock(self.checkpoint_path.with_suffix(self.checkpoint_path.suffix + '.lock')):
            state = _read_checkpoints(self.checkpoint_path)
            record = _find_attempt(state, running['id'], running['attempt'])
        current_plan = plan_api.load_plan(self.plan_path)
        render_evidence = None
        if running.get('stage') == 'chapter_render':
            try:
                render_evidence = self._prepare_render_evidence(current_plan, running)
            except StaleInputs:
                raise
            except Exception:
                plan_api.fail_task(self.plan_path, running['id'],
                                   error='渲染输入/暂存证据无法恢复验证；为避免重复而封锁。',
                                   billing_unknown=True)
                return 'blocked_unknown'
        if self._verify_checkpoint(record, running, current_plan, render_evidence):
            result = {'output': copy.deepcopy(record['output']),
                      'receipt': copy.deepcopy(record['receipt']),
                      'checkpoint_receipt_sha256': record['receipt_sha256'],
                      'checkpoint_receipt_validated': True}
            plan_api.complete_task(self.plan_path, running['id'], result=result,
                                   idempotency_key=running['idempotency_key'])
            self._mark_checkpoint_completed(running['id'], running['attempt'])
            return 'recovered_complete'
        plan_api.fail_task(self.plan_path, running['id'],
                           error='中断时没有完整、可验证的执行收据；为防重复请求标记计费状态未知。',
                           billing_unknown=True)
        return 'blocked_unknown'

    def _mark_checkpoint_completed(self, task_id: str, attempt: int) -> None:
        with plan_api._file_lock(self.checkpoint_path.with_suffix(self.checkpoint_path.suffix + '.lock')):
            state = _read_checkpoints(self.checkpoint_path)
            record = _find_attempt(state, task_id, attempt)
            if record:
                record['state'] = 'committed'
                record['committed_at'] = time.time()
                record['checkpoint_sha256'] = _checkpoint_hash(record)
                plan_api._atomic_json(self.checkpoint_path, state)

    def recover(self) -> dict[str, Any]:
        """Conservatively recover the one running plan task after interruption."""
        with plan_api._file_lock(self.run_lock_path):
            return self._recover_locked()

    def _recover_locked(self) -> dict[str, Any]:
        plan = self._sync_snapshot()
        outcome = self._reconcile_running_checkpoint(plan)
        plan = plan_api.load_plan(self.plan_path)
        self._expand_completed_fusion(plan)
        return {'outcome': outcome or 'nothing_to_recover', 'plan': plan}

    def _expand_completed_fusion(self, plan: dict[str, Any]) -> None:
        if plan.get('stop_requested') or plan.get('stale'):
            return
        fusion = next((task for task in plan['tasks']
                       if task.get('stage') == 'whole_book_cast_fusion' and task.get('status') == 'complete'), None)
        if not fusion:
            return
        result = fusion.get('result') or {}
        receipt_sha = result.get('checkpoint_receipt_sha256')
        if result.get('checkpoint_receipt_validated') is not True or not plan_api._valid_hash(receipt_sha):
            raise ExecutorBlocked('角色融合已结束但缺少已验证的 checkpoint receipt；拒绝继续渲染。')
        if plan.get('voice_expansion', {}).get('fusion_redo_generation') != fusion.get('redo_generation', 0):
            plan_api.expand_voice_tasks_from_fusion(self.plan_path, fusion['id'],
                                                    checkpoint_receipt_sha256=receipt_sha)

    def execute_next(self) -> dict[str, Any]:
        with plan_api._file_lock(self.run_lock_path):
            return self._execute_next_locked()

    def _execute_next_locked(self) -> dict[str, Any]:
        recovery = self._recover_locked()
        if recovery['outcome'] == 'blocked_unknown':
            raise ExecutorBlocked('中断任务没有有效收据，计费状态未知；需要核实后才能继续。')
        plan = self._sync_snapshot()
        if plan.get('stop_requested'):
            return {'status': 'stopped'}
        if plan.get('status') in ('blocked', 'stale') or plan.get('stale'):
            raise ExecutorBlocked(f"计划状态为 {plan.get('status')}，停止执行。")
        for task in plan['tasks']:
            if task.get('status') == 'complete':
                result = task.get('result') or {}
                if (result.get('checkpoint_receipt_validated') is not True
                        or not plan_api._valid_hash(result.get('checkpoint_receipt_sha256'))):
                    raise ExecutorBlocked(f"已完成任务 {task.get('key')} 缺少可信 checkpoint receipt。")
        indexed = {task['id']: task for task in plan['tasks']}
        completed = {task['id'] for task in plan['tasks'] if task.get('status') == 'complete'}
        selected = next((task for task in plan['tasks']
                         if task.get('status') == 'queued'
                         and all(dep in completed for dep in task.get('dependencies', []))
                         and all((indexed[dep].get('result') or {}).get('checkpoint_receipt_validated') is True
                                 and plan_api._valid_hash((indexed[dep].get('result') or {}).get(
                                     'checkpoint_receipt_sha256')) for dep in task.get('dependencies', []))), None)
        if not selected:
            for task in plan['tasks']:
                if task.get('status') != 'queued':
                    continue
                for dep_id in task.get('dependencies', []):
                    dep = indexed.get(dep_id)
                    if dep and dep.get('status') == 'complete':
                        result = dep.get('result') or {}
                        if (result.get('checkpoint_receipt_validated') is not True
                                or not plan_api._valid_hash(result.get('checkpoint_receipt_sha256'))):
                            raise ExecutorBlocked(f"依赖任务 {dep.get('key')} 缺少已验证执行收据；拒绝继续。")
            return {'status': plan.get('status'), 'remaining': sum(t.get('status') == 'queued' for t in plan['tasks'])}
        worker = self.workers.get(selected['stage'])
        if not callable(worker):
            raise ExecutorBlocked(f"没有为阶段 {selected['stage']} 注入 worker callback。")
        render_evidence = self._prepare_render_evidence(plan, selected) if selected['stage'] == 'chapter_render' else None
        claimed = plan_api.claim_task(self.plan_path, selected['id'])
        context = self._worker_context(plan_api.load_plan(self.plan_path), claimed, render_evidence)
        record = {'task_id': claimed['id'], 'task_key': claimed['key'], 'attempt': claimed['attempt'],
                  'idempotency_key': claimed['idempotency_key'],
                  'input_fingerprint': plan['input_fingerprint'], 'state': 'dispatching',
                  'request_context_sha256': payload_sha256(context), 'started_at': time.time()}
        if render_evidence is not None:
            record['render_input_sha256'] = render_evidence['render_input_sha256']
            record['staging_audit_sha256'] = render_evidence['staging_audit_sha256']
        self._persist_checkpoint(record)
        receipt_durable = False
        unverified_checkpoint = None
        try:
            response = worker(context)
            try:
                safe_response = _safe_worker_response(response, claimed)
            except Exception as sanitize_error:
                safe_response = {'response_type': type(response).__name__,
                                 'sanitization_error_type': type(sanitize_error).__name__}
            unverified_checkpoint = {**record, 'state': 'response_unverified',
                                     'raw_response': safe_response,
                                     'response_captured_at': time.time()}
            self._persist_checkpoint(unverified_checkpoint)
            output, receipt = self._validate_worker_response(response, claimed, plan, render_evidence)
            receipt_sha = payload_sha256(receipt)
            ready = {**record, 'state': 'receipt_ready', 'output': output, 'receipt': receipt,
                     'receipt_sha256': receipt_sha, 'output_sha256': receipt['output_sha256'],
                     'billing_status': receipt['billing_status'], 'cache_status': receipt['cache_status'],
                     'received_at': time.time()}
            self._persist_checkpoint(ready)
            receipt_durable = True
            try:
                latest = self._sync_snapshot()
            except StaleInputs:
                stale_record = {**ready, 'state': 'stale_after_worker',
                                'error': '输入在 worker 执行期间变化。'}
                self._persist_checkpoint(stale_record)
                raise StaleInputs('输入在 worker 执行期间变化；已标 stale，完整收据已保留供核查。')
            if render_evidence is not None:
                try:
                    latest_render_evidence = self._prepare_render_evidence(
                        plan_api.load_plan(self.plan_path), claimed)
                except StaleInputs:
                    stale_record = {**ready, 'state': 'stale_after_worker',
                                    'error': '渲染输入在worker执行期间变化。'}
                    self._persist_checkpoint(stale_record)
                    raise
                if latest_render_evidence['render_input_sha256'] != render_evidence['render_input_sha256']:
                    plan_api.mark_plan_stale(self.plan_path,
                                             reason='渲染期间章节/归属/cast/staging evidence变化',
                                             observed_evidence_sha256=latest_render_evidence['render_input_sha256'])
                    stale_record = {**ready, 'state': 'stale_after_worker',
                                    'error': '渲染输入在worker执行期间变化。'}
                    self._persist_checkpoint(stale_record)
                    raise StaleInputs('渲染输入在worker执行期间变化；计划已标 stale。')
            if latest['input_fingerprint'] != plan['input_fingerprint']:
                stale_record = {**ready, 'state': 'stale_after_worker',
                                'error': '输入在 worker 执行期间变化。'}
                self._persist_checkpoint(stale_record)
                raise StaleInputs('输入在 worker 执行期间变化；已标 stale，完整收据已保留供核查。')
            committed_result = {'output': output, 'receipt': receipt,
                                'checkpoint_receipt_sha256': receipt_sha,
                                'checkpoint_receipt_validated': True}
            plan_api.complete_task(self.plan_path, claimed['id'], result=committed_result,
                                   idempotency_key=claimed['idempotency_key'])
            self._mark_checkpoint_completed(claimed['id'], claimed['attempt'])
            new_plan = plan_api.load_plan(self.plan_path)
            self._expand_completed_fusion(new_plan)
            return {'status': 'completed_task', 'task_id': claimed['id'], 'task_key': claimed['key'],
                    'stage': claimed['stage'], 'receipt_sha256': receipt_sha,
                    'plan': plan_api.load_plan(self.plan_path)}
        except StaleInputs:
            raise
        except Exception as exc:
            if receipt_durable:
                raise ExecutorBlocked(f"{claimed['key']} 的有效收据已持久化；恢复时将提交该结果，不会重发任务。") from exc
            safe_error = f'{type(exc).__name__}: worker response failed validation or execution; details omitted.'
            failure = {**(unverified_checkpoint or record), 'state': 'validation_failed' if unverified_checkpoint
                       else 'worker_failure', 'error': safe_error, 'failed_at': time.time()}
            self._persist_checkpoint(failure)
            if unverified_checkpoint:
                try:
                    self._sync_snapshot()
                except StaleInputs:
                    failure['state'] = 'stale_after_worker_validation_failed'
                    self._persist_checkpoint(failure)
                    raise StaleInputs('worker response is preserved; plan inputs changed during execution') from exc
            latest = plan_api.load_plan(self.plan_path)
            current = next(t for t in latest['tasks'] if t['id'] == claimed['id'])
            if current.get('status') == 'running':
                try:
                    plan_api.fail_task(self.plan_path, claimed['id'], error=safe_error, billing_unknown=True)
                except ValueError:
                    # A concurrent input refresh can turn a running task into an
                    # interrupted/stale task. Preserve the response and stop here.
                    latest = plan_api.load_plan(self.plan_path)
                    if latest.get('stale'):
                        failure['state'] = 'stale_after_worker_validation_failed'
                        failure['checkpoint_sha256'] = _checkpoint_hash(failure)
                        self._persist_checkpoint(failure)
                        raise StaleInputs('worker response is preserved; plan inputs changed during execution') from exc
            raise ExecutorBlocked(f"{claimed['key']} 未能取得可信收据，已按计费状态未知封锁后续任务。") from exc

    def run_until_blocked(self, *, max_steps: int | None = None) -> dict[str, Any]:
        steps = 0
        while max_steps is None or steps < max_steps:
            result = self.execute_next()
            if result.get('status') != 'completed_task':
                return {'steps': steps, **result}
            steps += 1
        return {'steps': steps, 'status': 'step_limit', 'plan': plan_api.load_plan(self.plan_path)}

