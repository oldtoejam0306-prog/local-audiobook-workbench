import copy
import hashlib
import json
import unittest

from auto_voice_planner import plan_voice_designs, plans_from_character_profiles


def character_profile_document(alias_evidence, text='阿舟站在一旁。'):
    source_sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
    anchor = {'chapter_id': 'c001', 'quote': text, 'source_range': [0, len(text)],
              'source_sha256': source_sha}
    profile = {'name': '林舟', 'name_evidence': [anchor], 'aliases': [],
        'identity': {'value': '', 'status': 'unknown', 'evidence': []},
        'gender': {'value': '', 'status': 'unknown', 'evidence': []},
        'age': {'value': '', 'status': 'unknown', 'evidence': []},
        'personality': [], 'behavior': [], 'speech_style': [],
        'voice_design': {'description': '中性、清晰的虚构声音', 'rationale': '仅为声音创作选择',
            'creative_choices': ['保持清楚咬字'], 'status': 'candidate_needs_review',
            'mode': 'character_voice'}, 'uncertainties': []}
    if alias_evidence is not None:
        profile['aliases'] = [{'name': '阿舟', 'evidence': alias_evidence}]
    return {'version': 1, 'mode': 'character_profiles_v1', 'book_id': 'book-demo',
        'source': {'chapters': [{'chapter_id': 'c001', 'text_sha256': source_sha,
                                 'codepoint_length': len(text)}]},
        'characters': [profile], 'scope_summary': 'test', 'issues': [],
        'human_reviewed': False}


def profile_digest(profile):
    return hashlib.sha256(json.dumps(profile, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


class AutoVoicePlannerTests(unittest.TestCase):
    def setUp(self):
        self.memory = {
            'book_id': 'book-demo',
            'characters': {
                '林舟': {'aliases': ['阿舟'], 'voice_description': '中低音域，略沙哑，语速偏慢',
                         'personality': '冷静、谨慎', 'voice': ''},
                '小满': {'aliases': [], 'description': '活泼又可爱', 'voice': ''},
                '掌柜': {'aliases': ['老板'], 'voice_description': '清亮偏高的中音',
                         'voice': 'library:zh/pinned_01'},
            },
        }
        self.rows = [
            {'id': '1', 'type': 'dialogue', 'speaker': '阿舟', 'text': '一'},
            {'id': '2', 'type': 'dialogue', 'speaker': '林舟', 'text': '二'},
            {'id': '3', 'type': 'dialogue', 'speaker': '老板', 'text': '三'},
            {'id': '4', 'type': 'dialogue', 'speaker': 'unknown', 'text': '四'},
            {'id': '5', 'type': 'narration', 'text': '旁白'},
        ]

    def test_ranks_by_appearances_and_resolves_aliases(self):
        result = plan_voice_designs(self.memory, self.rows)
        self.assertEqual(result['main_character_order'], ['林舟', '掌柜', '小满'])
        self.assertEqual(result['plans'][0]['appearance_count'], 2)
        self.assertEqual(result['plans'][1]['appearance_count'], 1)

    def test_personality_drives_labeled_performance_inference_not_factual_acoustics(self):
        result = plan_voice_designs(self.memory, self.rows)
        plan = next(x for x in result['plans'] if x['character'] == '小满')
        self.assertEqual(plan['personality_audit_only'], '活泼又可爱')
        self.assertIn('活力', plan['design_text'])
        self.assertIn('创作性表演推断', plan['design_text'])
        self.assertTrue(any(x['rule'] == 'lively_to_energetic_delivery' for x in plan['performance_inference']))
        self.assertIn('不补充画像未提及的年龄或性别', plan['design_text'])
        self.assertTrue(any('不是原文声学事实' in warning for warning in plan['warnings']))

    def test_talkative_fast_comedy_profile_maps_to_brisk_comic_delivery_without_identity_guess(self):
        memory = {'book_id': 'goat-book', 'characters': {
            '山羊头': {'description': '话多、快语速的喜剧山羊头', 'voice': ''},
        }}
        plan = plan_voice_designs(memory, [])['plans'][0]
        self.assertIn('语句衔接更紧、停顿较少', plan['design_text'])
        self.assertIn('采用较快语速', plan['design_text'])
        self.assertIn('喜剧表演节奏', plan['design_text'])
        self.assertIn('不做动物叫声模仿', plan['design_text'])
        self.assertNotIn('男声', plan['design_text'])
        self.assertNotIn('成年', plan['design_text'])
        self.assertEqual({x['rule'] for x in plan['performance_inference']}, {
            'talkative_to_dense_rhythm', 'explicit_fast_speech', 'comedic_to_playful_timing'})

    def test_fixed_voice_is_preserved_and_not_scheduled_for_generation(self):
        result = plan_voice_designs(self.memory, self.rows, pinned_voices={'林舟': 'custom:locked'})
        plan = next(x for x in result['plans'] if x['character'] == '掌柜')
        self.assertEqual(plan['fixed_voice'], 'library:zh/pinned_01')
        self.assertTrue(plan['preserve_fixed_voice'])
        self.assertEqual(plan['status'], 'fixed_voice_preserved')
        self.assertIsNone(plan['design_text'])
        main = result['plans'][0]
        self.assertEqual(main['fixed_voice'], 'custom:locked')
        self.assertIsNone(main['design_text'])

    def test_assumptions_conflicts_and_duplicate_specs_are_auditable(self):
        memory = copy.deepcopy(self.memory)
        memory['characters']['掌柜']['voice'] = ''
        memory['characters']['掌柜']['voice_description'] = ''
        memory['characters']['小满'].pop('description', None)
        memory['characters']['掌柜'].pop('description', None)
        assumptions = {'小满': '中性、清晰', '掌柜': '中性、清晰',
                       '林舟': '高音域且低音域；语速快又语速慢'}
        result = plan_voice_designs(memory, self.rows, assumptions=assumptions)
        small = next(x for x in result['plans'] if x['character'] == '小满')
        shop = next(x for x in result['plans'] if x['character'] == '掌柜')
        lead = next(x for x in result['plans'] if x['character'] == '林舟')
        self.assertEqual(small['evidence_kind'], 'assumption')
        self.assertTrue(any('假设' in warning for warning in small['warnings']))
        self.assertTrue(any('冲突' in warning for warning in lead['warnings']))
        self.assertEqual(result['duplicate_specs'][small['spec_id']], ['掌柜', '小满'])

    def test_input_objects_are_not_mutated_and_duplicate_ids_fail(self):
        original = copy.deepcopy((self.memory, self.rows))
        plan_voice_designs(self.memory, self.rows)
        self.assertEqual((self.memory, self.rows), original)
        with self.assertRaises(ValueError):
            plan_voice_designs(self.memory, self.rows + [self.rows[0]])

    def test_character_profile_plan_audits_alias_quote_without_both_names(self):
        document = character_profile_document([{'chapter_id': 'c001',
            'quote': '阿舟站在一旁。', 'source_range': [0, 7],
            'source_sha256': hashlib.sha256('阿舟站在一旁。'.encode()).hexdigest()}])
        original = copy.deepcopy(document)
        profile = document['characters'][0]
        result = plans_from_character_profiles(document)
        self.assertEqual(document, original)
        self.assertEqual(1, len(result['plans']))
        self.assertEqual([], result['plans'][0]['aliases'])
        self.assertEqual(profile_digest(profile), result['plans'][0]['profile_sha256'])
        self.assertEqual([{'canonical': '林舟', 'candidate': '阿舟',
            'evidence': profile['aliases'][0]['evidence'],
            'reason': 'canonical_and_alias_not_co_located'}], result['unresolved_aliases'])

    def test_character_profile_plan_audits_alias_with_untrusted_source_hash(self):
        text = '林舟又名阿舟。'
        document = character_profile_document([{'chapter_id': 'c001', 'quote': text,
            'source_range': [0, len(text)], 'source_sha256': 'f' * 64}], text=text)
        original = copy.deepcopy(document)
        result = plans_from_character_profiles(document)
        self.assertEqual(document, original)
        self.assertEqual(1, len(result['plans']))
        self.assertEqual([], result['plans'][0]['aliases'])
        self.assertEqual(profile_digest(document['characters'][0]), result['plans'][0]['profile_sha256'])
        self.assertEqual('invalid_or_untrusted_source_evidence', result['unresolved_aliases'][0]['reason'])
        self.assertEqual('阿舟', result['unresolved_aliases'][0]['candidate'])

    def test_character_profile_plan_keeps_valid_source_bound_alias(self):
        text = '林舟又名阿舟。'
        document = character_profile_document([{'chapter_id': 'c001', 'quote': text,
            'source_range': [0, len(text)],
            'source_sha256': hashlib.sha256(text.encode()).hexdigest()}], text=text)
        original = copy.deepcopy(document)
        result = plans_from_character_profiles(document)
        self.assertEqual(document, original)
        self.assertEqual([], result['unresolved_aliases'])
        self.assertEqual(['阿舟'], [x['name'] for x in result['plans'][0]['aliases']])


if __name__ == '__main__': unittest.main()
