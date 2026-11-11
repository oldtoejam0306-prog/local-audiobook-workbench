import hashlib
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import character_profile_jobs as jobs
import real_pipeline_workflow as workflow


BOOK = 'book-' + '1' * 20
INDEX_SHA = 'a' * 64
USAGE = {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return workflow._bytes(value)


def claim(value='', status='unknown', evidence=None):
    return {'value': value, 'status': status, 'evidence': evidence or []}


class IndexStub:
    def __init__(self, count=4):
        self.ids = [f'c{i:03d}' for i in range(1, count + 1)]
        self.text = {cid: '林舟是律师。' for cid in self.ids}
        self.calls = []

    def __call__(self, action, book, chapter=None):
        self.calls.append((action, book, chapter))
        if action == 'list':
            return {'book_id': book, 'chapters': self.ids, 'index_sha256': INDEX_SHA, 'human_reviewed': False}
        payload = {'book_id': book, 'chapter_id': chapter, 'text': self.text[chapter], 'blocks': []}
        return {'book_id': book, 'chapter_id': chapter, 'payload': payload,
                'payload_sha256': sha(canonical(payload)), 'index_sha256': INDEX_SHA, 'human_reviewed': False}


class DispatcherStub:
    def __init__(self):
        self.calls = []

    def __call__(self, input_path, receipt_dir, jobs_dir):
        input_path, receipt_dir, jobs_dir = Path(input_path), Path(receipt_dir), Path(jobs_dir)
        self.calls.append(json.loads(input_path.read_bytes()))
        request = self.calls[-1]
        receipt = receipt_dir / 'character_profiles_v1'; receipt.mkdir(parents=True)
        request_body = canonical({'stub': True, 'input_sha256': sha(input_path.read_bytes())})
        provider_body = canonical({'id': f'response-{len(self.calls)}', 'model': 'deepseek-flash',
                                   'usage': USAGE, 'choices': [{'finish_reason': 'stop'}]})
        intent = {'purpose': 'character_profiles_v1', 'book_id': BOOK,
            'chapter_id': request['chapter_id'], 'input_sha256': sha(input_path.read_bytes()),
            'request_sha256': sha(request_body)}
        intent['worker_source_sha256']=sha((input_path.parent/'worker-source.mjs').read_bytes())
        intent['protocol_source_sha256']=sha((input_path.parent/'protocol-source.mjs').read_bytes())
        (receipt / 'intent.json').write_bytes(canonical(intent))
        (receipt / 'request.body.json').write_bytes(request_body)
        (receipt / 'response.body').write_bytes(provider_body)
        (receipt / 'response.meta.json').write_bytes(canonical({'http_status': 200, 'body_sha256': sha(provider_body)}))
        (receipt / 'usage.json').write_bytes(canonical({'usage': USAGE}))
        source_chapters = [{'chapter_id': c['chapter_id'], 'text_sha256': c['text_sha256'],
                            'codepoint_length': len(c['text'])} for c in request['chapters']]
        evidence = [{'chapter_id': c['chapter_id'], 'quote': c['text'],
                     'source_range': [0, len(c['text'])], 'source_sha256': c['text_sha256']} for c in request['chapters']]
        if len(self.calls) == 1:
            identity = claim('律师', 'explicit', evidence[:1])
            design = {'description': '第一版虚构音色', 'rationale': '依据原文证据',
                      'creative_choices': ['中音域'], 'status': 'candidate_needs_review'}
        else:
            identity = claim()
            design = {'description': '第二版虚构音色', 'rationale': '依据新章节',
                      'creative_choices': ['语速平稳'], 'status': 'candidate_needs_review'}
        target_names = request['target_names'] if not request['discover_characters'] else ['林舟']
        profiles = [{'name': name, 'name_evidence': evidence[:1], 'aliases': [],
            'identity': identity, 'gender': claim(), 'age': claim(), 'personality': [],
            'behavior': [], 'speech_style': [], 'voice_design': design, 'uncertainties': []}
            for name in target_names]
        normalized = {'version': 1, 'mode': 'character_profiles_v1', 'book_id': BOOK,
            'chapter_id': request['chapter_id'], 'status': 'needs_review', 'source': {'chapters': source_chapters},
            'characters': profiles, 'scope_summary': 'stub', 'narration_design': None,
            'issues': [], 'human_reviewed': False}
        result = {'status': 'needs_review', 'retry_allowed': False, 'response_id': f'response-{len(self.calls)}',
            'usage': USAGE, 'result': normalized}
        (receipt / 'result.json').write_bytes(canonical(result))
        return {'status': 'needs_review', 'usage': USAGE}


class CharacterProfileJobTests(unittest.TestCase):
    def test_known_json_syntax_failure_recovers_locally_and_reuses_one_provider_reply(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index = IndexStub(1)
            original_dispatch = DispatcherStub()
            originals = {}

            def dispatch(input_path, receipt_dir, jobs_dir):
                reply = original_dispatch(input_path, receipt_dir, jobs_dir)
                receipt = Path(receipt_dir) / 'character_profiles_v1'
                normalized = json.loads((receipt / 'result.json').read_bytes())['result']
                content = json.dumps({'characters': normalized['characters'], 'relations': [],
                    'narration_design': None, 'scope_summary': 'synthetic fixture'},
                    ensure_ascii=False, separators=(',', ':'))
                content = content.replace(',"scope_summary":', '},"scope_summary":')
                body = json.loads((receipt / 'response.body').read_bytes())
                body['choices'][0]['message'] = {'content': content}
                body_raw = canonical(body)
                (receipt / 'response.body').write_bytes(body_raw)
                (receipt / 'response.meta.json').write_bytes(canonical({
                    'http_status': 200, 'body_sha256': sha(body_raw)}))
                (receipt / 'result.json').write_bytes(canonical({
                    'status': 'invalid_or_usage_unknown', 'retry_allowed': False}))
                for name in ('input.json', 'worker-source.mjs', 'protocol-source.mjs'):
                    path = Path(input_path).parent / name
                    originals[path] = sha(path.read_bytes())
                for name in ('response.body', 'response.meta.json', 'result.json'):
                    path = receipt / name
                    originals[path] = sha(path.read_bytes())
                return reply

            first = jobs.analyze_profile_range(BOOK, 'c001', 'c001', root=root,
                index_request=index, dispatcher=dispatch)
            second = jobs.analyze_profile_range(BOOK, 'c001', 'c001', root=root,
                index_request=index, dispatcher=dispatch)
            self.assertEqual('needs_review', first['status'])
            self.assertTrue(second['batches'][0]['reused'])
            self.assertEqual(1, len(original_dispatch.calls))
            self.assertTrue(all(sha(path.read_bytes()) == digest for path, digest in originals.items()))
            batch = next((root / 'profile-tasks' / BOOK).iterdir())
            repair = json.loads((batch / 'receipt' / 'character_profiles_v1'
                / 'local-format-recovery.json').read_bytes())
            self.assertEqual('remove_premature_scope_summary_root_closer', repair['repair']['kind'])

    def test_worker_archive_tamper_or_missing_never_redispatches_and_retains_usage(self):
        for filename, missing in (('worker-source.mjs', False), ('worker-source.mjs', True),
                                   ('protocol-source.mjs', False), ('protocol-source.mjs', True)):
            with self.subTest(filename=filename, missing=missing), tempfile.TemporaryDirectory() as td:
                root=Path(td); index=IndexStub(1); dispatch=DispatcherStub()
                with patch('subprocess.run', side_effect=AssertionError('SSH/API must not be called')):
                    first=jobs.analyze_profile_range(BOOK,'c001','c001',root=root,
                        index_request=index,dispatcher=dispatch)
                    batch_dir=next((root/'profile-tasks'/BOOK).iterdir())
                    archive=batch_dir/filename
                    if missing: archive.unlink()
                    else: archive.write_bytes(b'tampered source archive')
                    with self.assertRaises(jobs.ProfileJobError) as raised:
                        jobs.analyze_profile_range(BOOK,'c001','c001',root=root,
                            index_request=index,dispatcher=dispatch)
                self.assertEqual('source_pin_mismatch',raised.exception.status)
                self.assertEqual(USAGE,raised.exception.usage)
                self.assertEqual(1,len(dispatch.calls),'corrupt/missing archive must not redispatch')
                self.assertEqual(4,len(index.calls))
                self.assertEqual(USAGE,first['batches'][0]['usage'])

    def test_valid_historical_archive_is_reused_when_current_worker_path_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); index=IndexStub(1); dispatch=DispatcherStub()
            with patch('subprocess.run', side_effect=AssertionError('SSH/API must not be called')):
                first=jobs.analyze_profile_range(BOOK,'c001','c001',root=root,
                    index_request=index,dispatcher=dispatch)
                replacement=root/'new-worker.mjs'; replacement.write_bytes(b'new current worker path')
                with patch.object(jobs,'WORKER',replacement):
                    second=jobs.analyze_profile_range(BOOK,'c001','c001',root=root,
                        index_request=index,dispatcher=dispatch)
            self.assertEqual(1,len(dispatch.calls))
            self.assertTrue(second['batches'][0]['reused'])
            self.assertEqual(first['batches'][0]['batch_source_sha256'],second['batches'][0]['batch_source_sha256'])
            self.assertEqual(4,len(index.calls))

    def test_missing_worker_for_new_job_stops_before_placeholder_and_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); index=IndexStub(1); dispatch=DispatcherStub()
            missing=root/'missing-worker.mjs'
            with patch('subprocess.run', side_effect=AssertionError('SSH/API must not be called')):
                with patch.object(jobs,'WORKER',missing):
                    with self.assertRaises(FileNotFoundError):
                        jobs.analyze_profile_range(BOOK,'c001','c001',root=root,
                            index_request=index,dispatcher=dispatch)
            self.assertFalse((root/'profile-tasks').exists())
            self.assertEqual([],dispatch.calls)
            self.assertEqual(2,len(index.calls))

    def test_range_batches_three_chapters_reuses_success_and_preserves_voice_selection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); selected = root / 'narrators' / BOOK / 'selected-voices.json'
            selected.parent.mkdir(parents=True); selected.write_bytes(b'{"selected":"untouched"}')
            original_selected = selected.read_bytes()
            index, dispatch = IndexStub(4), DispatcherStub()
            result = jobs.analyze_profile_range(BOOK, 'c001', 'c004', root=root,
                index_request=index, dispatcher=dispatch)
            self.assertEqual([3, 1], [len(batch['chapters']) for batch in result['batches']])
            self.assertEqual(2, len(dispatch.calls))
            self.assertEqual((200, 100, 0), (result['usage']['prompt_tokens'],
                result['usage']['completion_tokens'], result['usage']['unknown_usage_batches']))
            self.assertTrue(Path(result['current_path']).is_file())
            document = json.loads(Path(result['current_path']).read_bytes())
            profile = document['characters'][0]
            self.assertEqual([['c001', 'c002', 'c003'], ['c004']], [
                [row['chapter_id'] for row in batch['chapters']] for batch in document['profile_jobs']])
            self.assertEqual('律师', profile['identity']['value'])
            self.assertEqual('第一版虚构音色', profile['voice_design']['description'])
            self.assertEqual(2, len(profile['voice_design_candidates']))
            self.assertEqual(original_selected, selected.read_bytes())
            before = Path(result['current_path']).read_bytes()
            again = jobs.analyze_profile_range(BOOK, 'c001', 'c004', root=root,
                index_request=index, dispatcher=dispatch)
            self.assertEqual(2, len(dispatch.calls))
            self.assertTrue(all(batch['reused'] for batch in again['batches']))
            self.assertEqual(before, Path(again['current_path']).read_bytes())
            self.assertGreaterEqual(len(list((root/'character-profiles'/BOOK/'versions').glob('*.json'))), 1)

    def test_preflight_failure_happens_before_task_reservation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); index = IndexStub(1); calls=[]
            def dispatch(*args): calls.append(1); self.fail('dispatch must not run')
            def preflight(): raise jobs.ProfileJobError('credential_unavailable', 'missing key')
            with self.assertRaises(jobs.ProfileJobError) as raised:
                jobs.analyze_profile_range(BOOK, 'c001', 'c001', root=root,
                    index_request=index, dispatcher=dispatch, preflight=preflight)
            self.assertEqual('credential_unavailable', raised.exception.status)
            self.assertEqual([], calls)
            self.assertFalse((root/'profile-tasks'/BOOK).exists())

    def test_unknown_result_is_not_redispatched(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); index = IndexStub(1); calls=[]
            def dispatch(*args):
                calls.append(1)
                raise RuntimeError('stub unknown')
            for _ in range(2):
                with self.assertRaises(jobs.ProfileJobError) as raised:
                    jobs.analyze_profile_range(BOOK, 'c001', 'c001', root=root,
                        index_request=index, dispatcher=dispatch)
                self.assertFalse(raised.exception.retry_allowed)
            self.assertEqual([1], calls)

    def test_merge_keeps_conflicting_values_and_never_upgrades_alias_identity(self):
        evidence_a = [{'chapter_id':'c001','quote':'林舟是律师。','source_sha256':'1'*64}]
        evidence_b = [{'chapter_id':'c002','quote':'林舟是司机。','source_sha256':'2'*64}]
        current = {'version':1,'mode':'character_profiles_v1','book_id':BOOK,'human_reviewed':False,
            'characters':[{'name':'林舟','identity':claim('律师','explicit',evidence_a),
                'gender':claim(),'age':claim(),'aliases':[{'name':'阿舟','evidence':evidence_a}],
                'voice_design':{'description':'旧候选','rationale':'旧依据','creative_choices':[]},
                'selected_voice_id':'must-not-change','custom_fact':'preserve'}]}
        new = {'book_id':BOOK,'mode':'character_profiles_v1','source':{'chapters':[]},
            'characters':[{'name':'林舟','identity':claim('司机','explicit',evidence_b),
                'gender':claim(),'age':claim(),'aliases':[{'name':'阿舟','evidence':evidence_b}],
                'voice_design':{'description':'新候选','rationale':'新依据','creative_choices':[]}},
                {'name':'周林','identity':claim(),'gender':claim(),'age':claim(),
                 'aliases':[{'name':'阿舟','evidence':evidence_b}],
                 'voice_design':{'description':'独立候选','rationale':'独立人物证据','creative_choices':[]}}],
            'narration_design':None}
        merged = jobs._merge_registry(current, new, {'batch_source_sha256':'3'*64})
        profile = merged['characters'][0]
        self.assertEqual('律师', profile['identity']['value'])
        self.assertEqual('preserve', profile['custom_fact'])
        self.assertEqual('must-not-change', profile['selected_voice_id'])
        self.assertEqual(2, len(profile['aliases'][0]['evidence']))
        self.assertTrue(any(c['field']=='identity' for c in merged['profile_conflicts']))
        self.assertEqual('新候选', profile['voice_design_candidates'][-1]['design']['description'])
        self.assertEqual({'林舟','周林'}, {p['name'] for p in merged['characters']})
        self.assertEqual({'林舟','周林'}, {p['name'] for p in merged['alias_conflicts'][0]['candidates']})

    def test_merge_deduplicates_source_bound_relations_and_preserves_unresolved(self):
        source_sha = sha('林舟是阿舟，周林是同伴。'.encode('utf-8'))
        quote = '林舟是阿舟，周林是同伴。'
        evidence = [{'chapter_id': 'c001', 'quote': quote, 'source_sha256': source_sha}]
        existing = {'version': 1, 'mode': 'character_profiles_v1', 'book_id': BOOK,
            'source': {'chapters': [{'chapter_id': 'c001', 'text_sha256': source_sha}]},
            'characters': [
                {'name': '林舟', 'aliases': [{'name': '阿舟', 'evidence': evidence}]},
                {'name': '周林', 'aliases': []}],
            'relations': [], 'unresolved_relations': []}
        normalized = {'book_id': BOOK, 'mode': 'character_profiles_v1',
            'source': {'chapters': [{'chapter_id': 'c001', 'text_sha256': source_sha}]},
            'characters': [], 'narration_design': None,
            'relations': [
                {'source': '林舟', 'target': '周林', 'label': '同伴', 'status': 'explicit', 'evidence': evidence},
                {'source': '未知姓名', 'target': '周林', 'label': '亲属', 'status': 'explicit', 'evidence': evidence}],
            'unresolved_relations': [{'source': None, 'target': '周林', 'reason': 'missing_endpoint'}]}
        merged = jobs._merge_registry(existing, normalized,
            {'batch_source_sha256': 'b' * 64, 'chapters': [{'chapter_id': 'c001'}]})
        merged = jobs._merge_registry(merged, normalized,
            {'batch_source_sha256': 'c' * 64, 'chapters': [{'chapter_id': 'c001'}]})
        self.assertEqual(1, len(merged['relations']))
        self.assertEqual(('林舟', '周林'), (merged['relations'][0]['source'], merged['relations'][0]['target']))
        self.assertGreaterEqual(len(merged['unresolved_relations']), 2)
        legacy = jobs._merge_registry({'characters': []},
            {'book_id': BOOK, 'mode': 'character_profiles_v1', 'source': {'chapters': []},
             'characters': [], 'narration_design': None}, {})
        self.assertEqual([], legacy['relations'])

    def _alias_merge_fixture(self, old_quote):
        old_source='c001 source';new_source='c002 source'
        old_sha=sha(old_source.encode());new_sha=sha(new_source.encode())
        old_profile={'name':'林舟','aliases':[{'name':'阿野','evidence':[{'chapter_id':'c001',
            'quote':old_quote,'source_range':[0,len(old_quote)],'source_sha256':old_sha}]}],
            'identity':claim(),'gender':claim(),'age':claim(),'personality':[],'behavior':[],'speech_style':[],
            'voice_design':{'description':'旧设计','rationale':'旧来源','creative_choices':['旧选择'],'status':'candidate_needs_review'}}
        existing={'version':1,'mode':'character_profiles_v1','book_id':BOOK,'human_reviewed':False,
            'source':{'chapters':[{'chapter_id':'c001','text_sha256':old_sha,'codepoint_length':len(old_source)}]},
            'characters':[old_profile]}
        quote='阿野走进房间。'
        evidence=[{'chapter_id':'c002','quote':quote,'source_range':[0,len(quote)],'source_sha256':new_sha}]
        normalized={'book_id':BOOK,'mode':'character_profiles_v1','source':{'chapters':[
            {'chapter_id':'c002','text_sha256':new_sha,'codepoint_length':len(new_source)}]},
            'characters':[{'name':'阿野','name_evidence':evidence,'aliases':[],
                'identity':claim(),'gender':claim(),'age':claim(),'personality':[],'behavior':[],'speech_style':[],
                'voice_design':{'description':'新设计','rationale':'新来源','creative_choices':['新选择'],'status':'candidate_needs_review'}}],
            'narration_design':None}
        return existing,normalized

    def test_new_name_matching_proven_existing_alias_canonicalizes_without_renaming(self):
        existing,normalized=self._alias_merge_fixture('林舟又名阿野。')
        merged=jobs._merge_registry(existing,normalized,{'batch_source_sha256':'b'*64})
        self.assertEqual(['林舟'],[p['name'] for p in merged['characters']])
        profile=merged['characters'][0]
        self.assertEqual('林舟',profile['name'])
        self.assertIn('阿野',[a['name'] for a in profile['aliases']])
        self.assertIn('阿野',[v['name'] for v in profile['name_variants']])
        self.assertEqual('matched_existing_explicit_alias',profile['name_variants'][0]['identity_resolution'])
        self.assertIn('c002',{s['chapter_id'] for s in profile['profile_sources']})
        self.assertEqual([],merged.get('unresolved_profiles',[]))

    def test_cooccurrence_without_identity_evidence_stays_blocked_unmerged(self):
        existing,normalized=self._alias_merge_fixture('林舟在门口遇见阿野。')
        merged=jobs._merge_registry(existing,normalized,{'batch_source_sha256':'c'*64})
        self.assertEqual(['林舟'],[p['name'] for p in merged['characters']])
        self.assertEqual('unproven_profile_alias',merged['profile_conflicts'][-1]['type'])
        self.assertEqual('阿野',merged['unresolved_profiles'][-1]['candidate_name'])

    def test_concurrent_book_merges_keep_both_profiles_and_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);sha_a='1'*64;sha_b='2'*64;results=[]
            for cid,name,source_sha in [('c001','甲',sha_a),('c002','乙',sha_b)]:
                batch=root/('batch-'+cid);batch.mkdir();(batch/'input.json').write_bytes(canonical({'chapter':cid}))
                receipt=root/('receipt-'+cid);receipt.mkdir()
                evidence=[{'chapter_id':cid,'quote':name+'在场。','source_range':[0,4],'source_sha256':source_sha}]
                normalized={'book_id':BOOK,'mode':'character_profiles_v1','source':{'chapters':[
                    {'chapter_id':cid,'text_sha256':source_sha,'codepoint_length':4}]},
                    'characters':[{'name':name,'name_evidence':evidence,'aliases':[],
                        'identity':claim(),'gender':claim(),'age':claim(),'personality':[],'behavior':[],
                        'speech_style':[],'voice_design':{'description':'候选','rationale':'依据','creative_choices':['中音']}}],
                    'narration_design':None}
                result={'result':normalized}
                results.append((result,USAGE,receipt,batch,'c'*64 if cid=='c001' else 'd'*64,False))
            original=jobs._merge_registry
            def slow_merge(*args,**kwargs):
                time.sleep(.04)
                return original(*args,**kwargs)
            with patch.object(jobs,'_merge_registry',side_effect=slow_merge):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[pool.submit(jobs._merge_and_commit,root,BOOK,*args) for args in results]
                    [f.result() for f in futures]
            current=json.loads((root/'character-profiles'/BOOK/'current.json').read_bytes())
            self.assertEqual({'甲','乙'},{p['name'] for p in current['characters']})
            self.assertEqual({'c001','c002'},{x['chapter_id'] for x in current['source']['chapters']})
            self.assertEqual(2,len(current['profile_jobs']))

    def test_ensure_profiles_reuses_chapter_source(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); index=IndexStub(1); dispatch=DispatcherStub()
            response=index('prepare',BOOK,'c001'); payload=response['payload']
            first=jobs.ensure_profiles_for_chapter(payload,root=root,dispatcher=dispatch)
            second=jobs.ensure_profiles_for_chapter(payload,root=root,dispatcher=dispatch)
            self.assertEqual('needs_review',first['status'])
            self.assertEqual('already_current',second['status'])
            self.assertEqual(1,len(dispatch.calls))

    def test_required_names_trigger_targeted_profiles_when_source_is_current(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); dispatch=DispatcherStub(); text='林舟遇见看门大妈，又问柳恺。'
            source_sha=sha(text.encode()); chapter='c001'
            registry=root/'character-profiles'/BOOK/'current.json'; registry.parent.mkdir(parents=True)
            registry.write_bytes(canonical({'version':1,'mode':'character_profiles_v1','book_id':BOOK,
                'source':{'chapters':[{'chapter_id':chapter,'text_sha256':source_sha}]},
                'characters':[{'name':'林舟','aliases':[]}]}))
            payload={'book_id':BOOK,'chapter_id':chapter,'text':text,'profile_required_names':['柳恺','看门大妈']}
            result=jobs.ensure_profiles_for_chapter(payload,root=root,dispatcher=dispatch)
            request=dispatch.calls[0]
            self.assertFalse(request['discover_characters'])
            self.assertEqual(sorted(['看门大妈','柳恺']),request['target_names'])
            self.assertEqual({'林舟','看门大妈','柳恺'},
                {p['name'] for p in json.loads(registry.read_bytes())['characters']})
            self.assertEqual('needs_review',result['status'])

    def test_targeted_and_discovery_cache_scopes_are_separate_and_targeted_reuses(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); index=IndexStub(1); dispatch=DispatcherStub()
            payload=index('prepare',BOOK,'c001')['payload']
            payload['text']='林舟是律师。看门大妈在门口。'
            jobs.ensure_profiles_for_chapter(payload,root=root,dispatcher=dispatch)
            current=json.loads((root/'character-profiles'/BOOK/'current.json').read_bytes())
            current['characters']=[]
            (root/'character-profiles'/BOOK/'current.json').write_bytes(canonical(current))
            target_payload=dict(payload,profile_required_names=['看门大妈'])
            jobs.ensure_profiles_for_chapter(target_payload,root=root,dispatcher=dispatch)
            self.assertEqual(2,len(dispatch.calls))
            # Remove the profile while retaining the source so the same targeted
            # request reaches its own immutable cache entry on the next call.
            current=json.loads((root/'character-profiles'/BOOK/'current.json').read_bytes())
            current['characters']=[]
            (root/'character-profiles'/BOOK/'current.json').write_bytes(canonical(current))
            result=jobs.ensure_profiles_for_chapter(target_payload,root=root,dispatcher=dispatch)
            self.assertEqual(2,len(dispatch.calls))
            self.assertTrue(result['batches'][0]['reused'])

    def test_required_names_accept_only_unique_literal_bounded_names(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); index=IndexStub(1); dispatch=DispatcherStub()
            payload=index('prepare',BOOK,'c001')['payload']
            for names in ([''], ['missing'], ['林舟'*81], ['林舟',None]):
                with self.subTest(names=names), self.assertRaises(ValueError):
                    jobs.ensure_profiles_for_chapter(dict(payload,profile_required_names=names),
                        root=root,dispatcher=dispatch)
            jobs.ensure_profiles_for_chapter(dict(payload,profile_required_names=['林舟','林舟']),
                root=root,dispatcher=dispatch)
            self.assertEqual(['林舟'],dispatch.calls[0]['target_names'])

    def test_required_name_protocol_length_boundary_is_80(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); dispatch=DispatcherStub(); name='林'*80
            payload={'book_id':BOOK,'chapter_id':'c001','text':name}
            result=jobs.ensure_profiles_for_chapter(dict(payload,profile_required_names=[name]),
                root=root,dispatcher=dispatch)
            self.assertEqual([name],dispatch.calls[0]['target_names'])
            self.assertEqual('needs_review',result['status'])
            self.assertEqual([],dispatch.calls[0]['chapters'][0].get('profile_required_names',[]))

    def test_required_names_accept_only_source_supported_unambiguous_aliases(self):
        text='林舟又名阿野。'; source_sha=sha(text.encode())
        registry={'source':{'chapters':[{'chapter_id':'c001','text_sha256':source_sha}]},
            'characters':[{'name':'林舟','aliases':[{'name':'阿野','evidence':[
                {'chapter_id':'c001','source_sha256':source_sha,'quote':text}]}]}]}
        self.assertEqual([],jobs._missing_required_profiles(registry,['阿野']))
        registry['characters'].append({'name':'阿野','aliases':[]})
        self.assertEqual(['阿野'],jobs._missing_required_profiles(registry,['阿野']))
        registry['characters'].pop()
        registry['characters'][0]['aliases'][0]['evidence'][0]['source_sha256']='f'*64
        self.assertEqual(['阿野'],jobs._missing_required_profiles(registry,['阿野']))


if __name__ == '__main__':
    unittest.main()
