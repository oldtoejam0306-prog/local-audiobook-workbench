import hashlib
import multiprocessing
import tempfile
import unittest
from pathlib import Path

import attribution_staging_adapter as staging_binding
from whole_book_plan import (
    claim_task, complete_task, create_or_update_plan, fail_task, load_plan,
    expand_voice_tasks_from_fusion, queue_redo, rebuild_stale_plan, reconcile_task,
    resume_plan, stop_plan,
)


def _claim_in_child(path, task_id, output):
    try:
        task = claim_task(path, task_id)
        output.put(('claimed', task['id']))
    except Exception as exc:
        output.put(('rejected', str(exc)))


class WholeBookPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'work-plan.json'
        self.hash_a = hashlib.sha256(b'source-a').hexdigest()
        self.hash_b = hashlib.sha256(b'source-b').hexdigest()
        self.chapters = [
            {'id': 'c001', 'title': '第一章', 'source_sha256': self.hash_a},
            {'id': 'c002', 'title': '第二章', 'source_sha256': self.hash_b},
        ]
        self.characters = [
            {'name': '主角', 'appearance_count': 40},
            {'name': '固定声线角色', 'appearance_count': 10, 'fixed_voice': 'library:zh/locked'},
        ]
        self.plan = create_or_update_plan(self.path, book_id='book-x', chapters=self.chapters,
                                          source_hashes={'book.txt': self.hash_a}, characters=self.characters)

    def tearDown(self):
        self.tmp.cleanup()

    def by_key(self, key):
        plan = load_plan(self.path)
        return next(task for task in plan['tasks'] if task['key'] == key)

    def test_two_pass_tasks_dependencies_and_fixed_voice_preservation(self):
        plan = self.plan
        keys = {task['key'] for task in plan['tasks']}
        self.assertIn('chapter_analysis:c001', keys)
        self.assertIn('chapter_analysis:c002', keys)
        self.assertEqual(sum(task['stage'] == 'chapter_analysis' for task in plan['tasks']), 2)
        self.assertFalse(any(task['stage'] in ('chapter_profile', 'chapter_summary') for task in plan['tasks']))
        self.assertIn('cast_fusion', keys)
        self.assertIn('dialogue_attribution:c001', keys)
        self.assertFalse(any(task['stage'] in ('voice_design_text', 'local_voice_synthesis')
                             for task in plan['tasks']))
        attribution = next(x for x in plan['tasks'] if x['key'] == 'dialogue_attribution:c001')
        self.assertTrue(attribution['details']['unknown_and_conflicts_preserved'])
        self.assertEqual(attribution['details']['generation'], 'automatic')
        self.assertFalse(attribution['details']['human_reviewed'])
        fusion = next(x for x in plan['tasks'] if x['key'] == 'cast_fusion')
        self.assertEqual(len(fusion['dependencies']), 2)
        self.assertFalse(any(task['stage'] == 'voice_design_text' for task in plan['tasks']))

    def test_idempotent_creation_and_changed_input_marks_stale_until_explicit_rebuild(self):
        again = create_or_update_plan(self.path, book_id='book-x', chapters=self.chapters,
                                      source_hashes={'book.txt': self.hash_a}, characters=self.characters)
        self.assertEqual(again['plan_id'], self.plan['plan_id'])
        self.assertEqual([x['id'] for x in again['tasks']], [x['id'] for x in self.plan['tasks']])
        changed = create_or_update_plan(self.path, book_id='book-x', chapters=self.chapters,
                                        source_hashes={'book.txt': self.hash_b}, characters=self.characters)
        self.assertTrue(changed['stale'])
        self.assertTrue(all(x['status'] == 'stale' for x in changed['tasks']))
        with self.assertRaises(ValueError):
            claim_task(self.path, changed['tasks'][0]['id'])
        fresh = rebuild_stale_plan(self.path, book_id='book-x', chapters=self.chapters,
                                   source_hashes={'book.txt': self.hash_b}, characters=self.characters)
        self.assertEqual(fresh['revision'], 2)
        self.assertFalse(fresh['stale'])
        self.assertEqual(len(fresh['archived_revisions']), 1)

    def test_fused_character_identity_is_dynamic_but_initial_fixed_voice_config_is_snapshotted(self):
        path = self.path.parent / 'identity-snapshot-plan.json'
        profile_hash = hashlib.sha256(b'manual-profile-v1').hexdigest()
        initial = create_or_update_plan(path, book_id='identity-book', chapters=self.chapters,
                                        source_hashes={'book.txt': self.hash_a}, characters=[],
                                        fixed_voice_snapshot={'narrator': 'voice:locked'},
                                        external_profile_hash=profile_hash)
        fused = create_or_update_plan(path, book_id='identity-book', chapters=self.chapters,
                                      source_hashes={'book.txt': self.hash_a},
                                      characters=[{'name': 'fused-hero', 'appearance_count': 18}],
                                      fixed_voice_snapshot={'narrator': 'voice:locked'},
                                      external_profile_hash=profile_hash)
        self.assertFalse(fused['stale'])
        self.assertEqual(fused['input_fingerprint'], initial['input_fingerprint'])
        changed_lock = create_or_update_plan(path, book_id='identity-book', chapters=self.chapters,
                                             source_hashes={'book.txt': self.hash_a}, characters=[
                                                 {'name': 'fused-hero', 'appearance_count': 18}],
                                             fixed_voice_snapshot={'narrator': 'voice:other'},
                                             external_profile_hash=profile_hash)
        self.assertTrue(changed_lock['stale'])
        new_path = self.path.parent / 'profile-hash-plan.json'
        create_or_update_plan(new_path, book_id='profile-hash-book', chapters=self.chapters,
                              source_hashes={'book.txt': self.hash_a},
                              external_profile_hash=profile_hash)
        changed_profile = create_or_update_plan(new_path, book_id='profile-hash-book', chapters=self.chapters,
                                                source_hashes={'book.txt': self.hash_a},
                                                external_profile_hash=hashlib.sha256(b'manual-profile-v2').hexdigest())
        self.assertTrue(changed_profile['stale'])

    def test_dependency_claim_stop_resume_and_idempotency_key(self):
        fusion = self.by_key('cast_fusion')
        with self.assertRaises(ValueError): claim_task(self.path, fusion['id'])
        profile = self.by_key('chapter_analysis:c001')
        first = claim_task(self.path, profile['id'])
        self.assertEqual(first['attempt'], 1)
        self.assertEqual(first['idempotency_key'], f"{self.plan['plan_id']}:1:chapter_analysis:c001:r0")
        stop_plan(self.path, reason='user_stop')
        with self.assertRaises(ValueError): claim_task(self.path, self.by_key('chapter_analysis:c002')['id'])
        complete_task(self.path, profile['id'], idempotency_key=first['idempotency_key'], result={'ok': True})
        self.assertEqual(load_plan(self.path)['status'], 'stopped')
        resume_plan(self.path)
        second = claim_task(self.path, self.by_key('chapter_analysis:c002')['id'])
        self.assertEqual(second['attempt'], 1)

    def test_billing_unknown_blocks_retry_until_explicit_reconciliation(self):
        task = self.by_key('chapter_analysis:c001')
        claimed = claim_task(self.path, task['id'])
        fail_task(self.path, task['id'], error='request outcome unavailable', billing_unknown=True)
        with self.assertRaises(ValueError): resume_plan(self.path)
        with self.assertRaises(ValueError): claim_task(self.path, task['id'])
        queued = reconcile_task(self.path, task['id'], outcome='confirmed_not_executed',
                                evidence='synthetic test: executor was not launched')
        self.assertEqual(queued['status'], 'queued')
        resume_plan(self.path)
        retried = claim_task(self.path, task['id'])
        self.assertEqual(retried['attempt'], 2)
        self.assertEqual(retried['idempotency_key'], claimed['idempotency_key'])

    def test_cross_process_claim_is_exclusive(self):
        task = self.by_key('chapter_analysis:c001')
        ctx = multiprocessing.get_context('spawn')
        output = ctx.Queue()
        children = [ctx.Process(target=_claim_in_child, args=(str(self.path), task['id'], output))
                    for _ in range(2)]
        for child in children: child.start()
        results = [output.get(timeout=15) for _ in children]
        for child in children: child.join(timeout=15)
        self.assertEqual(sum(result[0] == 'claimed' for result in results), 1)
        self.assertEqual(sum(result[0] == 'rejected' for result in results), 1)

    def test_completed_task_can_be_explicitly_redone_with_descendants_invalidated(self):
        first = self.by_key('chapter_analysis:c001')
        active = claim_task(self.path, first['id'])
        complete_task(self.path, first['id'], result={'profile': 'synthetic'})
        self.assertFalse(self.by_key('chapter_analysis:c001')['result']['human_reviewed'])
        queued = queue_redo(self.path, first['id'], reason='synthetic reviewer requested another pass',
                            confirm_redo=True)
        profile = next(x for x in queued['tasks'] if x['id'] == first['id'])
        self.assertEqual(profile['status'], 'queued')
        self.assertEqual(profile['redo_generation'], 1)
        self.assertEqual(profile['history'][0]['result']['profile'], 'synthetic')
        self.assertFalse(profile['history'][0]['result']['human_reviewed'])
        self.assertIn(self.by_key('cast_fusion')['id'], queued['redo_events'][-1]['affected_task_ids'])
        repeated = claim_task(self.path, first['id'])
        self.assertEqual(repeated['attempt'], 2)
        self.assertNotEqual(repeated['idempotency_key'], active['idempotency_key'])

    def test_empty_start_fusion_expands_voice_tasks_before_render(self):
        path = self.path.parent / 'empty-cast-plan.json'
        script_rows = [{'type': 'chapter', 'id': 'c001', 'src': [0, 0]},
                       {'type': 'dialogue', 'id': 'd001', 'text': 'synthetic line', 'speaker': 'synthetic-lead'}]
        plan = create_or_update_plan(path, book_id='empty-cast-book',
                                     chapters=[{'id': 'c001', 'source_sha256': self.hash_a}],
                                     source_hashes={'book.txt': self.hash_a}, characters=[],
                                     attribution_bindings={'c001': staging_binding.capture_binding(
                                         'empty-cast-book', 'c001', script_rows)})
        analysis = next(x for x in plan['tasks'] if x['stage'] == 'chapter_analysis')
        self.assertTrue(analysis['details']['single_request'])
        self.assertEqual(sum(x['stage'] == 'chapter_analysis' for x in plan['tasks']), 1)
        self.assertFalse(any(x['stage'] in ('chapter_profile', 'chapter_summary') for x in plan['tasks']))
        active = claim_task(path, analysis['id'])
        complete_task(path, analysis['id'], idempotency_key=active['idempotency_key'],
                      result={'profile': {'characters': []}, 'summary': 'synthetic'})

        plan = load_plan(path)
        fusion = next(x for x in plan['tasks'] if x['stage'] == 'whole_book_cast_fusion')
        active = claim_task(path, fusion['id'])
        receipt_hash = hashlib.sha256(b'validated-fake-receipt').hexdigest()
        characters = [
            {'name': 'synthetic-lead', 'appearance_count': 12, 'fixed_voice': ''},
            {'name': 'synthetic-fixed', 'appearance_count': 3, 'fixed_voice': 'voice:locked'},
        ]
        complete_task(path, fusion['id'], idempotency_key=active['idempotency_key'], result={
            'output': {'characters': characters},
            'checkpoint_receipt_sha256': receipt_hash,
            'checkpoint_receipt_validated': True,
        })

        plan = load_plan(path)
        attribution = next(x for x in plan['tasks'] if x['stage'] == 'dialogue_attribution')
        active = claim_task(path, attribution['id'])
        complete_task(path, attribution['id'], idempotency_key=active['idempotency_key'],
                      result={'assignments': [], 'unknown_ids': ['q-unknown'], 'conflicts': ['q-conflict']})
        plan = expand_voice_tasks_from_fusion(path, fusion['id'], checkpoint_receipt_sha256=receipt_hash)
        design = next(x for x in plan['tasks'] if x['stage'] == 'voice_design_text')
        synth = next(x for x in plan['tasks'] if x['stage'] == 'local_voice_synthesis')
        self.assertEqual(design['details']['character'], 'synthetic-lead')
        self.assertFalse(any(x.get('details', {}).get('character') == 'synthetic-fixed'
                             for x in plan['tasks'] if x['stage'] in ('voice_design_text', 'local_voice_synthesis')))
        render = next(x for x in plan['tasks'] if x['stage'] == 'chapter_render')
        self.assertIn(synth['id'], render['dependencies'])
        with self.assertRaises(ValueError): claim_task(path, render['id'])

        for task in (design, synth):
            active = claim_task(path, task['id'])
            complete_task(path, task['id'], idempotency_key=active['idempotency_key'], result={'synthetic': True})
        active = claim_task(path, render['id'])
        complete_task(path, render['id'], idempotency_key=active['idempotency_key'], result={'audio_hash': self.hash_b})
        verify = next(x for x in load_plan(path)['tasks'] if x['stage'] == 'output_verification')
        self.assertEqual(verify['dependencies'], [render['id']])
        self.assertEqual(claim_task(path, verify['id'])['stage'], 'output_verification')


if __name__ == '__main__': unittest.main()
