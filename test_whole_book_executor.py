import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import whole_book_plan as plan_api
import whole_book_executor as executor_api
import attribution_staging_adapter as attribution_adapter
import character_fusion
import whole_book_adapters
import staging_render_cast as staging
from whole_book_executor import ExecutorBlocked, StaleInputs, WholeBookExecutor, payload_sha256


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class FakeWorkers:
    def __init__(self, script_rows=None):
        self.calls = []
        self.script_rows = script_rows or [
            {'type': 'chapter', 'id': 'c001', 'src': [0, 0]},
            {'type': 'dialogue', 'id': 'q-c001', 'text': 'synthetic line one', 'speaker': 'synthetic-hero'},
            {'type': 'chapter', 'id': 'c002', 'src': [0, 0]},
            {'type': 'dialogue', 'id': 'q-c002', 'text': 'synthetic line two', 'speaker': 'unknown'},
        ]
        self.artifacts = {}

    def callback(self, context):
        task = context['task']
        stage = task['stage']
        key = context['idempotency_key']
        self.calls.append((stage, key))
        if stage == 'chapter_analysis':
            output = {'profile': {'characters': [{'name': 'synthetic-hero'}]}, 'summary': 'synthetic summary'}
        elif stage == 'whole_book_cast_fusion':
            output = {'characters': [
                {'name': 'synthetic-hero', 'appearance_count': 8, 'fixed_voice': '',
                 'uncertainties': [{'kind': 'same_name_assumption', 'status': 'needs_review'}]},
                {'name': 'locked-cast-member', 'appearance_count': 2, 'fixed_voice': 'library:zh/other'},
            ]}
        elif stage == 'dialogue_attribution':
            chapter = task['chapter_id']
            row = next(row for row in self.script_rows if row.get('type') == 'dialogue'
                       and row.get('id') == ('q-' + chapter))
            assignment = {'id': row['id'], 'speaker': row['speaker']}
            if row['speaker'] == 'synthetic-hero':
                assignment.update({'identity_status': 'same_name_assumption',
                                   'identity_uncertainty': 'synthetic repeated-name uncertainty'})
            unknown_ids = [row['id']] if row['speaker'] == 'unknown' else []
            conflicts = [{'id': row['id'], 'candidates': ['synthetic-hero', 'other']}] if unknown_ids else []
            output = {'assignments': [assignment], 'unknown_ids': unknown_ids, 'conflicts': conflicts}
        elif stage == 'voice_design_text':
            output = {'design_text': 'synthetic fictional voice style'}
        elif stage == 'local_voice_synthesis':
            output = {'voice_id': 'library:zh/synthetic-voice-01', 'fixed': True, 'local_only': True,
                      'uses_real_person_reference': False}
        elif stage == 'chapter_render':
            evidence = context['render_evidence']
            package = evidence['staging_package']
            assert attribution_adapter.restore_original(package) == {
                'script_rows': self.script_rows, 'cast': evidence['source_cast']}
            artifact_ref = 'synthetic-artifact:' + task['chapter_id']
            artifact_bytes = ('synthetic-audio:' + evidence['render_input_sha256']).encode()
            self.artifacts[artifact_ref] = artifact_bytes
            output = {'artifact_ref': artifact_ref,
                      'artifact_sha256': hashlib.sha256(artifact_bytes).hexdigest(),
                      'staging_only': True, 'human_reviewed': False,
                      'staging_audit': evidence['staging_audit'],
                      'staging_audit_sha256': evidence['staging_audit_sha256'],
                      'staging_package_sha256': evidence['staging_package_sha256'],
                      'render_input_sha256': evidence['render_input_sha256'],
                      'attribution_task_id': evidence['attribution_task_id'],
                      'attribution_receipt_sha256': evidence['attribution_receipt_sha256'],
                      'attribution_output_sha256': evidence['attribution_output_sha256']}
        elif stage == 'output_verification':
            render = next(dep for dep in context['dependencies'] if dep['stage'] == 'chapter_render')
            rendered = render['result']['output']
            output = {'artifact_sha256': rendered['artifact_sha256'], 'artifact_ref': rendered['artifact_ref'],
                      'verified': True, 'exists': True,
                      'staging_audit_sha256': rendered['staging_audit_sha256'],
                      'render_input_sha256': rendered['render_input_sha256']}
        else:
            raise AssertionError(f'unexpected stage: {stage}')
        out_hash = payload_sha256(output)
        receipt = {'receipt_id': 'synthetic-receipt-' + key,
                   'task_id': task['id'], 'idempotency_key': key,
                   'input_fingerprint': context['input_fingerprint'],
                   'output_sha256': out_hash, 'cache_status': 'fresh',
                   'billing_status': 'not_applicable'}
        if task['details'].get('billing_mode') == 'external':
            billing = {'receipt_id': 'synthetic-billing-' + key,
                       'task_id': task['id'], 'idempotency_key': key,
                       'request_id': 'synthetic-request-' + key,
                       'response_id': 'synthetic-response-' + key,
                       'usage': {'input_tokens': 3, 'output_tokens': 2}}
            receipt.update({'billing_status': 'known',
                            'request_id': billing['request_id'],
                            'response_id': billing['response_id'],
                            'billing_receipt': billing})
        return {'output': output, 'receipt': receipt}

    def mapping(self, *, external_billing=False):
        def callback(context):
            response = self.callback(context)
            if not external_billing and context['task']['details'].get('billing_mode') == 'external':
                response['receipt'].update({'billing_status': 'not_applicable'})
                response['receipt'].pop('request_id', None)
                response['receipt'].pop('response_id', None)
                response['receipt'].pop('billing_receipt', None)
            return response
        return {stage: callback for stage in (
            'chapter_analysis', 'whole_book_cast_fusion', 'dialogue_attribution',
            'voice_design_text', 'local_voice_synthesis', 'chapter_render', 'output_verification')}

    def render_input_provider(self, plan, task):
        voices = {}
        task_by_id = {item['id']: item for item in plan['tasks']}
        synthesized = {task_by_id[dep['id']]['details']['character']: dep['result']['output']['voice_id']
                      for dep in plan['tasks'] if dep.get('stage') == 'local_voice_synthesis'
                      and dep.get('status') == 'complete'}
        for character in plan['voice_expansion']['characters']:
            name = character['name']
            voice = character.get('fixed_voice') or synthesized.get(name)
            if not voice:
                raise AssertionError('missing synthetic voice for ' + name)
            voices[name] = {'voice': voice, 'aliases': []}
        return {'script_rows': copy.deepcopy(self.script_rows),
                'cast': {'narrator': {'voice': 'library:zh/narrator'}, 'characters': voices}}

    def artifact_reader(self, artifact_ref):
        if artifact_ref not in self.artifacts:
            raise FileNotFoundError(artifact_ref)
        return self.artifacts[artifact_ref]


class WholeBookExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.path = self.directory / 'plan.json'
        self.snapshot = {'book_id': 'synthetic-book',
                         'chapters': [{'id': 'c001', 'source_sha256': sha('chapter 1')},
                                      {'id': 'c002', 'source_sha256': sha('chapter 2')}],
                         'source_hashes': {'book.txt': sha('synthetic source')},
                         'characters': [], 'fixed_voice_snapshot': {'locked-cast-member': 'library:zh/locked'},
                         'external_profile_hash': sha('manual profile v1')}
        fake = FakeWorkers()
        self.snapshot['attribution_bindings'] = {
            chapter['id']: attribution_adapter.capture_binding(
                self.snapshot['book_id'], chapter['id'], fake.script_rows)
            for chapter in self.snapshot['chapters']
        }
        plan_api.create_or_update_plan(self.path, **self.snapshot)

    def tearDown(self):
        self.tmp.cleanup()

    def make_executor(self, fake, workers=None, *, path=None, snapshot_provider=None):
        return WholeBookExecutor(path or self.path, workers=workers or fake.mapping(external_billing=True),
                                 snapshot_provider=snapshot_provider or (lambda: self.snapshot),
                                 render_input_provider=fake.render_input_provider,
                                 artifact_reader=fake.artifact_reader)

    def test_fake_workers_complete_full_pipeline_without_chapter_prompts_or_duplicate_requests(self):
        fake = FakeWorkers()
        workers = fake.mapping(external_billing=True)
        base_fusion = workers['whole_book_cast_fusion']
        def fusion_updates_dynamic_identity(context):
            result = base_fusion(context)
            self.snapshot['characters'] = result['output']['characters']
            return result
        workers['whole_book_cast_fusion'] = fusion_updates_dynamic_identity
        executor = self.make_executor(fake, workers)
        result = executor.run_until_blocked(max_steps=30)
        self.assertEqual(result['status'], 'complete')
        plan = plan_api.load_plan(self.path)
        self.assertTrue(all(task['status'] == 'complete' for task in plan['tasks']))
        self.assertEqual([stage for stage, _ in fake.calls].count('chapter_analysis'), 2)
        self.assertNotIn('chapter_profile', [stage for stage, _ in fake.calls])
        self.assertNotIn('chapter_summary', [stage for stage, _ in fake.calls])
        self.assertEqual([stage for stage, _ in fake.calls].count('whole_book_cast_fusion'), 1)
        self.assertEqual([stage for stage, _ in fake.calls].count('dialogue_attribution'), 2)
        self.assertEqual([stage for stage, _ in fake.calls].count('voice_design_text'), 1)
        self.assertEqual([stage for stage, _ in fake.calls].count('local_voice_synthesis'), 1)
        self.assertFalse(any(task['details'].get('character') == 'locked-cast-member'
                             for task in plan['tasks']
                             if task['stage'] in ('voice_design_text', 'local_voice_synthesis')))
        self.assertEqual([stage for stage, _ in fake.calls].count('chapter_render'), 2)
        self.assertEqual([stage for stage, _ in fake.calls].count('output_verification'), 2)
        self.assertFalse(plan['stale'])
        self.assertEqual(len({key for _, key in fake.calls}), len(fake.calls))
        self.assertTrue(all(task['result']['human_reviewed'] is False for task in plan['tasks']))
        unknown = next(task for task in plan['tasks']
                       if task['stage'] == 'dialogue_attribution' and task['chapter_id'] == 'c002')
        self.assertEqual(unknown['result']['output']['unknown_ids'], ['q-c002'])
        self.assertEqual(unknown['result']['output']['conflicts'][0]['id'], 'q-c002')
        expansion = next(task for task in plan['tasks'] if task['stage'] == 'voice_task_expansion')
        self.assertEqual(plan['voice_expansion']['fixed_voice_conflicts'], [{
            'character': 'locked-cast-member', 'locked_voice': 'library:zh/locked',
            'fusion_suggestion': 'library:zh/other'}])
        render = next(task for task in plan['tasks'] if task['stage'] == 'chapter_render')
        self.assertIn(expansion['id'], render['dependencies'])
        repeated = executor.run_until_blocked(max_steps=10)
        self.assertEqual(repeated['status'], 'complete')
        self.assertEqual(len(fake.calls), 11)
        analysis = next(task for task in plan['tasks'] if task['stage'] == 'chapter_analysis')
        with self.assertRaises(ValueError):
            plan_api.queue_redo(self.path, analysis['id'], reason='synthetic upstream change',
                                confirm_redo=True, cascade=False)
        self.assertEqual(plan_api.load_plan(self.path)['status'], 'complete')
        self.snapshot['external_profile_hash'] = sha('manual profile v2')
        stale = plan_api.create_or_update_plan(self.path, **self.snapshot)
        self.assertTrue(stale['stale'])
        rendered = [task for task in stale['tasks'] if task['stage'] == 'chapter_render']
        self.assertTrue(all(task['status'] == 'stale' for task in rendered))
        self.assertTrue(all(task['history'][-1]['invalidated_by'] == 'input_snapshot_changed'
                            for task in rendered))

    def test_render_fails_closed_without_live_input_and_artifact_readers(self):
        fake = FakeWorkers()
        executor = WholeBookExecutor(self.path, workers=fake.mapping(external_billing=True),
                                     snapshot_provider=lambda: self.snapshot)
        with self.assertRaises(ExecutorBlocked): executor.run_until_blocked(max_steps=30)
        self.assertNotIn('chapter_render', [stage for stage, _ in fake.calls])
        render = next(task for task in plan_api.load_plan(self.path)['tasks']
                      if task['stage'] == 'chapter_render')
        self.assertEqual(render['status'], 'queued')

    def test_fusion_uncertainty_cannot_be_dropped_by_attribution_worker(self):
        fake = FakeWorkers()
        workers = fake.mapping(external_billing=True)
        base = workers['dialogue_attribution']
        def omit_uncertainty(context):
            response = base(context)
            for assignment in response['output']['assignments']:
                assignment.pop('identity_status', None)
                assignment.pop('identity_uncertainty', None)
            response['receipt']['output_sha256'] = payload_sha256(response['output'])
            return response
        workers['dialogue_attribution'] = omit_uncertainty
        executor = self.make_executor(fake, workers)
        with self.assertRaises(ExecutorBlocked): executor.run_until_blocked(max_steps=20)
        plan = plan_api.load_plan(self.path)
        attribution = next(task for task in plan['tasks'] if task['stage'] == 'dialogue_attribution'
                           and task['chapter_id'] == 'c001')
        self.assertEqual(attribution['status'], 'billing_unknown')
        self.assertNotIn('chapter_render', [stage for stage, _ in fake.calls])

    def test_fusion_conflicts_survive_safe_checkpoint_and_dependency_and_require_uncertainty(self):
        fake = FakeWorkers()
        workers = fake.mapping(external_billing=True)
        base_fusion = workers['whole_book_cast_fusion']
        def fusion_with_conflict(context):
            response = base_fusion(context)
            # Remove the separate per-character uncertainty so this assertion
            # proves the top-level fusion conflict survives the whole contract.
            for character in response['output']['characters']:
                character.pop('uncertainties', None)
            response['output']['unresolved_conflicts'] = [{
                'kind': 'same_name_unconfirmed', 'chapter_id': 'c001',
                'name': 'synthetic-hero', 'possible_entities': [],
            }]
            response['receipt']['output_sha256'] = payload_sha256(response['output'])
            return response
        workers['whole_book_cast_fusion'] = fusion_with_conflict
        base_attribution = workers['dialogue_attribution']
        def omit_conflict_uncertainty(context):
            response = base_attribution(context)
            for assignment in response['output']['assignments']:
                assignment.pop('identity_status', None)
                assignment.pop('identity_uncertainty', None)
            response['receipt']['output_sha256'] = payload_sha256(response['output'])
            return response
        workers['dialogue_attribution'] = omit_conflict_uncertainty
        executor = self.make_executor(fake, workers)
        # Advance through the two analyses and fusion, all with fake callbacks.
        for _ in range(3):
            executor.execute_next()
        plan = plan_api.load_plan(self.path)
        fusion_task = next(t for t in plan['tasks'] if t['stage'] == 'whole_book_cast_fusion')
        self.assertEqual(fusion_task['status'], 'complete')
        self.assertEqual(fusion_task['result']['output']['unresolved_conflicts'][0]['name'], 'synthetic-hero')
        attribution_task = next(t for t in plan['tasks'] if t['stage'] == 'dialogue_attribution'
                                and t['chapter_id'] == 'c001')
        dependency = next(d for d in executor._dependencies(plan, attribution_task)
                          if d['stage'] == 'whole_book_cast_fusion')
        self.assertEqual(dependency['result']['output']['unresolved_conflicts'],
                         fusion_task['result']['output']['unresolved_conflicts'])
        with self.assertRaises(ExecutorBlocked):
            executor.execute_next()
        plan = plan_api.load_plan(self.path)
        attribution_task = next(t for t in plan['tasks'] if t['stage'] == 'dialogue_attribution'
                                and t['chapter_id'] == 'c001')
        self.assertEqual(attribution_task['status'], 'billing_unknown')
        self.assertNotIn('chapter_render', [stage for stage, _ in fake.calls])

    def test_fusion_conflict_rejects_unknown_entity_reference(self):
        fake = FakeWorkers()
        workers = fake.mapping(external_billing=True)
        base_fusion = workers['whole_book_cast_fusion']
        def malformed(context):
            response = base_fusion(context)
            response['output']['unresolved_conflicts'] = [{
                'kind': 'same_name_unconfirmed', 'chapter_id': 'c001',
                'possible_entities': ['nonexistent-entity'],
            }]
            response['receipt']['output_sha256'] = payload_sha256(response['output'])
            return response
        workers['whole_book_cast_fusion'] = malformed
        executor = self.make_executor(fake, workers)
        with self.assertRaises(ExecutorBlocked):
            executor.run_until_blocked(max_steps=20)
        self.assertNotIn('dialogue_attribution', [stage for stage, _ in fake.calls])

    def test_real_fusion_adapter_preserves_invalid_link_candidate_as_unresolved(self):
        texts = {'c001': '林舟来了。', 'c002': '林舟和顾晴见面。'}
        snapshot = character_fusion.new_book('synthetic-book', ['c001', 'c002'])
        snapshot = character_fusion.fuse_chapter(snapshot, 'c001', texts['c001'], [
            {'name': '林舟', 'evidence': '林舟来了。'}])
        snapshot = character_fusion.fuse_chapter(snapshot, 'c002', texts['c002'], [
            {'name': '顾晴', 'evidence': '林舟和顾晴见面。',
             'link_to': 'e999999', 'identity_evidence': '林舟和顾晴见面。'}])
        adapted = whole_book_adapters.fusion_to_executor(snapshot, texts)
        output = whole_book_adapters.fusion_output_for_executor(adapted)
        conflict = output['unresolved_conflicts'][0]
        self.assertEqual(conflict['kind'], 'identity_not_proven')
        self.assertEqual(conflict['link_to'], 'e999999')

        fusion_task = {'stage': 'whole_book_cast_fusion'}
        plan = {'inputs': {'chapters': [{'id': 'c001'}, {'id': 'c002'}]}}
        safe_response = executor_api._safe_worker_response({'output': output, 'receipt': {}}, fusion_task)
        self.assertEqual(safe_response['output']['unresolved_conflicts'], output['unresolved_conflicts'])
        WholeBookExecutor._validate_output(fusion_task, safe_response['output'], plan=plan)

        dependency = {'stage': 'whole_book_cast_fusion', 'result': {'output': safe_response['output']}}
        attribution_task = {'stage': 'dialogue_attribution'}
        attribution_output = {'assignments': [
            {'id': 'q-c002', 'speaker': '顾晴', 'identity_status': 'confirmed'}],
            'unknown_ids': [], 'conflicts': []}
        with self.assertRaisesRegex(ValueError, '显式保留不确定性'):
            WholeBookExecutor._validate_output(attribution_task, attribution_output,
                                               dependencies=[dependency])
        # A nonexistent candidate target cannot taint the unrelated existing
        # entity; the candidate name itself remains uncertain (above).
        unrelated_target = {'assignments': [
            {'id': 'q-c002', 'speaker': '林舟', 'identity_status': 'confirmed'}],
            'unknown_ids': [], 'conflicts': []}
        WholeBookExecutor._validate_output(attribution_task, unrelated_target,
                                           dependencies=[dependency])

    def test_existing_link_candidate_makes_target_entity_uncertain(self):
        cases = [
            ('identity_not_proven', '林舟和顾晴见面。',
             {'name': '顾晴', 'evidence': '林舟和顾晴见面。',
              'link_to': 'e000001', 'identity_evidence': '林舟和顾晴见面。'}, '林舟'),
            ('name_collision', '林舟也叫顾晴。',
             {'name': '林舟', 'evidence': '林舟也叫顾晴。',
              'link_to': 'e000002', 'identity_evidence': '林舟也叫顾晴。'}, '顾晴'),
        ]
        for expected_kind, chapter_text, candidate, target_name in cases:
            with self.subTest(kind=expected_kind):
                texts = {'c001': '林舟来了。顾晴来了。', 'c002': chapter_text}
                snapshot = character_fusion.new_book('synthetic-book', ['c001', 'c002'])
                snapshot = character_fusion.fuse_chapter(snapshot, 'c001', texts['c001'], [
                    {'name': '林舟', 'evidence': '林舟来了。'},
                    {'name': '顾晴', 'evidence': '顾晴来了。'}])
                snapshot = character_fusion.fuse_chapter(snapshot, 'c002', chapter_text, [candidate])
                adapted = whole_book_adapters.fusion_to_executor(snapshot, texts)
                output = whole_book_adapters.fusion_output_for_executor(adapted)
                conflict = next(item for item in output['unresolved_conflicts']
                                if item['kind'] == expected_kind)
                self.assertIn(conflict['link_to'], {item['entity_id'] for item in output['characters']})
                dependency = {'stage': 'whole_book_cast_fusion',
                              'result': {'output': output}}
                with self.assertRaisesRegex(ValueError, '显式保留不确定性'):
                    WholeBookExecutor._validate_output(
                        {'stage': 'dialogue_attribution'},
                        {'assignments': [{'id': 'q-c002', 'speaker': target_name,
                                          'identity_status': 'confirmed'}],
                         'unknown_ids': [], 'conflicts': []},
                        dependencies=[dependency])

    def test_render_rejects_forged_staging_audit_even_with_recomputed_worker_output_hash(self):
        fake = FakeWorkers()
        workers = fake.mapping(external_billing=True)
        base = workers['chapter_render']
        def forge_audit(context):
            response = base(context)
            response['output']['staging_audit']['human_reviewed'] = True
            response['receipt']['output_sha256'] = payload_sha256(response['output'])
            return response
        workers['chapter_render'] = forge_audit
        executor = self.make_executor(fake, workers)
        with self.assertRaises(ExecutorBlocked): executor.run_until_blocked(max_steps=30)
        plan = plan_api.load_plan(self.path)
        render = next(task for task in plan['tasks'] if task['stage'] == 'chapter_render')
        self.assertEqual(render['status'], 'billing_unknown')
        self.assertNotIn('output_verification', [stage for stage, _ in fake.calls])

    def test_render_rejects_artifact_bytes_that_do_not_match_receipt_hash(self):
        fake = FakeWorkers()
        executor = self.make_executor(fake)
        executor.artifact_reader = lambda _ref: b'wrong synthetic artifact bytes'
        with self.assertRaises(ExecutorBlocked): executor.run_until_blocked(max_steps=30)
        plan = plan_api.load_plan(self.path)
        render = next(task for task in plan['tasks'] if task['stage'] == 'chapter_render')
        self.assertEqual(render['status'], 'billing_unknown')
        self.assertNotIn('output_verification', [stage for stage, _ in fake.calls])

    def test_invalid_cache_receipt_blocks_and_does_not_repeat_worker(self):
        calls = []
        fake = FakeWorkers()
        base_workers = fake.mapping(external_billing=True)
        def bad_cache(context):
            response = base_workers['chapter_analysis'](context)
            response['receipt']['cache_status'] = 'hit_unverified'
            calls.append(context['idempotency_key'])
            return response
        workers = fake.mapping(external_billing=True); workers['chapter_analysis'] = bad_cache
        executor = WholeBookExecutor(self.path, workers=workers, snapshot_provider=lambda: self.snapshot)
        with self.assertRaises(ExecutorBlocked): executor.execute_next()
        with self.assertRaises(ExecutorBlocked): executor.execute_next()
        plan = plan_api.load_plan(self.path)
        self.assertEqual(plan['status'], 'blocked')
        self.assertEqual(plan['tasks'][0]['status'], 'billing_unknown')
        self.assertEqual(len(calls), 1)

    def test_chapter_analysis_rejects_null_profile_and_empty_summary_with_valid_billing(self):
        fake = FakeWorkers()
        workers = fake.mapping(external_billing=True)
        base = workers['chapter_analysis']
        def invalid(context):
            response = base(context)
            response['output'] = {'profile': None, 'summary': None}
            response['receipt']['output_sha256'] = payload_sha256(response['output'])
            return response
        workers['chapter_analysis'] = invalid
        executor = WholeBookExecutor(self.path, workers=workers, snapshot_provider=lambda: self.snapshot)
        with self.assertRaises(ExecutorBlocked): executor.execute_next()
        self.assertEqual(plan_api.load_plan(self.path)['tasks'][0]['status'], 'billing_unknown')

    def test_known_billing_receipt_requires_structured_request_bound_usage(self):
        fake = FakeWorkers()
        workers = fake.mapping(external_billing=True)
        base = workers['chapter_analysis']
        def fabricated(context):
            response = base(context)
            response['receipt']['billing_receipt'] = 'fabricated'
            return response
        workers['chapter_analysis'] = fabricated
        executor = WholeBookExecutor(self.path, workers=workers, snapshot_provider=lambda: self.snapshot)
        with self.assertRaises(ExecutorBlocked): executor.execute_next()
        self.assertEqual(plan_api.load_plan(self.path)['tasks'][0]['status'], 'billing_unknown')

    def test_invalid_response_is_checkpointed_without_unexpected_secret_fields(self):
        fake = FakeWorkers()
        workers = fake.mapping(external_billing=True)
        base = workers['chapter_analysis']
        def polluted(context):
            response = base(context)
            response['api_key'] = 'synthetic-secret'
            response['output']['access_token'] = 'synthetic-secret'
            response['receipt']['credential'] = 'synthetic-secret'
            return response
        workers['chapter_analysis'] = polluted
        executor = WholeBookExecutor(self.path, workers=workers, snapshot_provider=lambda: self.snapshot)
        with self.assertRaises(ExecutorBlocked): executor.execute_next()
        checkpoint = executor_api._read_checkpoints(executor.checkpoint_path)
        record = checkpoint['attempts'][plan_api.load_plan(self.path)['tasks'][0]['id']][0]
        self.assertEqual(record['state'], 'validation_failed')
        raw = record['raw_response']
        self.assertNotIn('api_key', raw)
        self.assertNotIn('access_token', raw['output'])
        self.assertNotIn('credential', raw['receipt'])

    def test_unknown_billing_receipt_blocks_and_does_not_repeat_worker(self):
        fake = FakeWorkers()
        calls = []
        base_workers = fake.mapping(external_billing=True)
        def unknown_billing(context):
            response = base_workers['chapter_analysis'](context)
            response['receipt']['billing_status'] = 'unknown'
            calls.append(context['idempotency_key'])
            return response
        workers = fake.mapping(external_billing=True); workers['chapter_analysis'] = unknown_billing
        executor = WholeBookExecutor(self.path, workers=workers, snapshot_provider=lambda: self.snapshot)
        with self.assertRaises(ExecutorBlocked): executor.execute_next()
        with self.assertRaises(ExecutorBlocked): executor.execute_next()
        self.assertEqual(plan_api.load_plan(self.path)['tasks'][0]['status'], 'billing_unknown')
        self.assertEqual(len(calls), 1)

    def test_empty_fusion_cannot_drop_initial_unvoiced_character(self):
        path = self.directory / 'known-cast-plan.json'
        snapshot = dict(self.snapshot)
        snapshot['characters'] = [{'name': 'KnownName', 'appearance_count': 1, 'fixed_voice': ''}]
        plan_api.create_or_update_plan(path, **snapshot)
        fake = FakeWorkers()
        workers = fake.mapping(external_billing=True)
        base_fusion = workers['whole_book_cast_fusion']
        def omit_known_character(context):
            response = base_fusion(context)
            response['output'] = {'characters': [], 'no_characters_detected': True}
            response['receipt']['output_sha256'] = payload_sha256(response['output'])
            return response
        workers['whole_book_cast_fusion'] = omit_known_character
        executor = WholeBookExecutor(path, workers=workers, snapshot_provider=lambda: snapshot)
        with self.assertRaises(ExecutorBlocked): executor.run_until_blocked(max_steps=20)
        stages = [stage for stage, _ in fake.calls]
        self.assertIn('whole_book_cast_fusion', stages)
        self.assertNotIn('chapter_render', stages)

    def test_fusion_character_list_must_preserve_initial_profile_hints(self):
        path = self.directory / 'missing-known-character-plan.json'
        snapshot = dict(self.snapshot)
        snapshot['characters'] = [{'name': 'KnownName', 'appearance_count': 1, 'fixed_voice': ''}]
        plan_api.create_or_update_plan(path, **snapshot)
        fake = FakeWorkers()
        workers = fake.mapping(external_billing=True)
        base = workers['whole_book_cast_fusion']
        def omit(context):
            response = base(context)
            response['output'] = {'characters': [], 'no_characters_detected': True}
            response['receipt']['output_sha256'] = payload_sha256(response['output'])
            return response
        workers['whole_book_cast_fusion'] = omit
        executor = WholeBookExecutor(path, workers=workers, snapshot_provider=lambda: snapshot)
        with self.assertRaises(ExecutorBlocked): executor.run_until_blocked(max_steps=20)
        self.assertEqual([stage for stage, _ in fake.calls].count('whole_book_cast_fusion'), 1)
        self.assertNotIn('chapter_render', [stage for stage, _ in fake.calls])

    def test_snapshot_change_during_callback_marks_stale_and_stops_later_calls(self):
        fake = FakeWorkers()
        base_workers = fake.mapping(external_billing=True)
        def changing_snapshot():
            return self.snapshot
        def mutate_after_first(context):
            result = base_workers['chapter_analysis'](context)
            self.snapshot = dict(self.snapshot)
            self.snapshot['source_hashes'] = {'book.txt': sha('changed source')}
            return result
        workers = fake.mapping(external_billing=True); workers['chapter_analysis'] = mutate_after_first
        executor = WholeBookExecutor(self.path, workers=workers, snapshot_provider=changing_snapshot)
        with self.assertRaises(StaleInputs): executor.execute_next()
        with self.assertRaises(StaleInputs): executor.execute_next()
        stale_plan = plan_api.load_plan(self.path)
        self.assertTrue(stale_plan['stale'])
        interrupted = next(task for task in stale_plan['tasks'] if task['status'] == 'interrupted')
        with self.assertRaises(ValueError):
            plan_api.rebuild_stale_plan(self.path, **self.snapshot)
        plan_api.reconcile_task(self.path, interrupted['id'], outcome='confirmed_not_executed',
                                evidence='synthetic test confirms callback receipt is not present')
        rebuilt = plan_api.rebuild_stale_plan(self.path, **self.snapshot)
        self.assertEqual(rebuilt['revision'], 2)
        self.assertEqual(len(fake.calls), 1)

    def test_interrupted_dispatch_without_receipt_becomes_billing_unknown(self):
        calls = []
        def crash(context):
            calls.append(context['idempotency_key'])
            raise KeyboardInterrupt('synthetic process interruption')
        workers = FakeWorkers().mapping(external_billing=True); workers['chapter_analysis'] = crash
        executor = WholeBookExecutor(self.path, workers=workers, snapshot_provider=lambda: self.snapshot)
        with self.assertRaises(KeyboardInterrupt): executor.execute_next()
        restarted = WholeBookExecutor(self.path, workers=FakeWorkers().mapping(external_billing=True),
                                      snapshot_provider=lambda: self.snapshot)
        outcome = restarted.recover()
        self.assertEqual(outcome['outcome'], 'blocked_unknown')
        with self.assertRaises(ExecutorBlocked): restarted.execute_next()
        self.assertEqual(len(calls), 1)
        self.assertEqual(plan_api.load_plan(self.path)['tasks'][0]['status'], 'billing_unknown')

    def test_receipt_checkpoint_recovers_complete_without_recalling_worker(self):
        fake = FakeWorkers()
        executor = self.make_executor(fake)
        with patch.object(plan_api, 'complete_task', side_effect=OSError('synthetic checkpoint commit failure')):
            with self.assertRaises(ExecutorBlocked): executor.execute_next()
        self.assertEqual(plan_api.load_plan(self.path)['tasks'][0]['status'], 'running')
        self.assertEqual(len(fake.calls), 1)
        restarted = self.make_executor(fake, fake.mapping(external_billing=True))
        recovered = restarted.recover()
        self.assertEqual(recovered['outcome'], 'recovered_complete')
        self.assertEqual(plan_api.load_plan(self.path)['tasks'][0]['status'], 'complete')
        self.assertEqual(len(fake.calls), 1)


if __name__ == '__main__': unittest.main()
