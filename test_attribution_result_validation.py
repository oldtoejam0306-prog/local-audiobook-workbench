import copy
import hashlib
import unittest

from attribution_result_validation import normalize_attribution


def fixture():
    text='😀甲说：“我到了。”乙回答：“收到。”'
    chars=list(text)
    first='甲说：“我到了。”';second='乙回答：“收到。”'
    start1=''.join(chars).index(first);start2=''.join(chars).index(second)
    payload={'book_id':'book-'+'a'*20,'chapter_id':'c003','text':text,'blocks':[
        {'id':'d1','type':'dialogue','text':first,'src':[start1,start1+len(first)]},
        {'id':'d2','type':'dialogue','text':second,'src':[start2,start2+len(second)]}]}
    parsed={'characters':[
        {'name':'甲','aliases':['阿甲'],'evidence':'甲说：“我到了。”','extra':'preserved'},
        {'name':'乙','aliases':[],'evidence':'乙回了一句。'},
        {'name':'丙','aliases':[],'evidence':'甲说：“我到了。”'}],
        'dialogues':[
            {'id':'d1','kind':'dialogue','speaker':'甲','evidence':'我到了。','confidence':0.91},
            {'id':'d2','kind':'dialogue','speaker':'乙','evidence':'乙回答说她收到了。'}],
        'chapter_summary':'原始模型摘要','provider_extra':{'keep':True}}
    return payload,parsed


class AttributionResultValidationTests(unittest.TestCase):
    def test_paraphrased_line_is_downgraded_and_valid_results_preserved(self):
        payload,parsed=fixture();before=copy.deepcopy(parsed);input_before=copy.deepcopy(payload)
        normalized,audit=normalize_attribution(payload,parsed)
        self.assertEqual(parsed,before);self.assertEqual(payload,input_before)
        self.assertEqual({'keep':True},normalized['provider_extra'])
        self.assertEqual('甲',normalized['dialogues'][0]['speaker'])
        self.assertEqual('我到了。',normalized['dialogues'][0]['evidence'])
        self.assertEqual('unknown',normalized['dialogues'][1]['speaker'])
        self.assertEqual('unknown',normalized['dialogues'][1]['kind'])
        self.assertEqual('',normalized['dialogues'][1]['evidence'])
        self.assertEqual([{'name':'甲','aliases':['阿甲'],'evidence':'甲说：“我到了。”','extra':'preserved'}],
                         normalized['characters'])
        self.assertEqual('derived_locally',audit['status'])
        self.assertFalse(audit['human_reviewed'])
        self.assertEqual('d2',audit['changes'][0]['id'])
        self.assertEqual('乙',audit['changes'][0]['original_speaker'])
        self.assertEqual('乙回答说她收到了。',audit['changes'][0]['original_evidence'])
        self.assertEqual('evidence_not_literal_source',audit['changes'][0]['reason'])
        self.assertEqual('candidate_evidence_not_literal_source',audit['changes'][1]['reason'])
        self.assertEqual('candidate_name_missing_from_evidence',audit['changes'][2]['reason'])
        self.assertEqual(hashlib.sha256(payload['text'].encode()).hexdigest(),audit['source']['source_sha256'])

    def test_model_dialogue_ids_must_cover_source_exactly_once(self):
        payload,parsed=fixture()
        for rows in (parsed['dialogues'][:1],
                     [parsed['dialogues'][0],dict(parsed['dialogues'][1],id='other')]):
            with self.subTest(rows=rows),self.assertRaisesRegex(ValueError,'exactly cover'):
                normalize_attribution(payload,{**parsed,'dialogues':rows})
        with self.assertRaisesRegex(ValueError,'unique'):
            normalize_attribution(payload,{**parsed,'dialogues':parsed['dialogues']+[parsed['dialogues'][0]]})

    def test_invalid_dialogue_schema_and_source_offsets_are_rejected(self):
        payload,parsed=fixture()
        for update in ({'kind':'speaker'}, {'speaker':[]}, {'evidence':None}, {'evidence':4}):
            bad=copy.deepcopy(parsed);bad['dialogues'][0].update(update)
            with self.subTest(update=update),self.assertRaises(ValueError):
                normalize_attribution(payload,bad)
        bad_payload=copy.deepcopy(payload);bad_payload['blocks'][0]['src']=[0,1]
        with self.assertRaisesRegex(ValueError,'offset'):
            normalize_attribution(bad_payload,parsed)
        bad_payload=copy.deepcopy(payload);bad_payload['blocks'][1]['id']='d1'
        with self.assertRaisesRegex(ValueError,'unique'):
            normalize_attribution(bad_payload,parsed)

    def test_character_structure_errors_but_bad_quote_candidates_are_audited(self):
        payload,parsed=fixture()
        with self.assertRaisesRegex(ValueError,'character candidate structure'):
            normalize_attribution(payload,{**parsed,'characters':[{'name':'甲','evidence':'甲'}]})
        with self.assertRaisesRegex(ValueError,'character'):
            normalize_attribution(payload,{**parsed,'characters':None})
        normalized,audit=normalize_attribution(payload,parsed)
        self.assertEqual(['甲'],[row['name'] for row in normalized['characters']])
        self.assertEqual([1,2],[row['index'] for row in audit['changes'] if row['type']=='character_candidate_dropped'])

    def test_empty_dialogue_evidence_cannot_keep_a_named_attribution(self):
        payload,parsed=fixture();parsed['dialogues'][0]['evidence']=''
        normalized,audit=normalize_attribution(payload,parsed)
        self.assertEqual('unknown',normalized['dialogues'][0]['speaker'])
        self.assertEqual('missing_literal_evidence',audit['changes'][0]['reason'])

    def test_legacy_delivery_defaults_semantically_without_changing_rows(self):
        payload,parsed=fixture()
        normalized,audit=normalize_attribution(payload,parsed)
        self.assertNotIn('delivery',normalized['dialogues'][0])
        self.assertEqual('neutral',normalized['dialogues'][0].get('delivery','neutral'))
        self.assertEqual('legacy_delivery_missing',audit['delivery_audit'][0]['reason'])
        self.assertNotIn('delivery', [entry['type'] for entry in audit['changes']])

    def test_valid_delivery_is_preserved_and_bad_value_is_separately_audited(self):
        payload,parsed=fixture()
        parsed['dialogues'][0]['delivery']='tense'
        parsed['dialogues'][1]['delivery']={'mood':'sad'}
        normalized,audit=normalize_attribution(payload,parsed)
        self.assertEqual('tense',normalized['dialogues'][0]['delivery'])
        self.assertEqual('neutral',normalized['dialogues'][1]['delivery'])
        self.assertEqual('unsupported_or_malformed_delivery',audit['delivery_audit'][0]['reason'])
        self.assertTrue(all(entry['type'] in ('dialogue_downgraded','character_candidate_dropped')
                            for entry in audit['changes']))


if __name__=='__main__':
    unittest.main()
