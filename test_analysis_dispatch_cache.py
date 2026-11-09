import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analysis_dispatch_cache import CachedAnalysisDispatcher, AnalysisCacheError
import real_pipeline_workflow as workflow


class AnalysisDispatchCacheTests(unittest.TestCase):
    def payload(self):
        return {'book_id': 'book-' + 'a' * 20, 'chapter_id': 'c001', 'text': '你好。'}

    def test_identical_payload_reuses_receipt_after_voice_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def dispatch(payload, root, jobs):
                calls.append(payload)
                task = root / (payload['book_id'] + '-' + payload['chapter_id'])
                receipt = task / 'receipt'; receipt.mkdir(parents=True)
                (task / 'input.json').write_bytes(workflow._bytes(payload))
                (receipt / 'result.json').write_text(json.dumps(
                    {'status': 'needs_review', 'retry_allowed': False}))
            cache = CachedAnalysisDispatcher(tmp, dispatch=dispatch, worker_sha256='b' * 64)
            with patch('analysis_dispatch_cache.analysis.build', return_value={}):
                first = cache(self.payload())
                second = cache(self.payload())
            self.assertFalse(first['reused_analysis'])
            self.assertTrue(second['reused_analysis'])
            self.assertEqual(len(calls), 1)
            self.assertEqual(first['receipt_dir'], second['receipt_dir'])

    def test_uncertain_call_is_never_dispatched_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def fail(*args):
                calls.append(1)
                raise TimeoutError()
            cache = CachedAnalysisDispatcher(tmp, dispatch=fail)
            for _ in range(2):
                with self.assertRaises(AnalysisCacheError):
                    cache(self.payload())
            self.assertEqual(calls, [1])

    def test_missing_credential_reserves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            def unavailable():
                raise RuntimeError('credential_unavailable')
            cache = CachedAnalysisDispatcher(tmp, preflight=unavailable)
            with self.assertRaises(RuntimeError):
                cache(self.payload())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_source_or_worker_change_produces_distinct_request_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def fail(*args):
                calls.append(1)
                raise TimeoutError()
            payload = self.payload()
            for sha, text in [('b' * 64, '你好。'), ('c' * 64, '你好。'), ('b' * 64, '您好。')]:
                cache = CachedAnalysisDispatcher(tmp, dispatch=fail, worker_sha256=sha)
                with self.assertRaises(AnalysisCacheError):
                    cache(dict(payload, text=text))
            self.assertEqual(len(calls), 3)

    def test_verified_provider_body_reuses_once_and_tamper_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {'book_id':'book-' + 'd' * 20, 'chapter_id':'c001', 'text':'c001',
                       'blocks':[{'type':'chapter','id':'c001','text':'c001','src':[0,4]}],
                       'prior_context':{'version':1,'book_id':'book-' + 'd' * 20,
                                        'target_chapter_id':'c001','records':[]}}
            calls = []

            def dispatch(value, task_root, jobs_root):
                calls.append(value)
                task = task_root / (value['book_id'] + '-' + value['chapter_id'])
                receipt = task / 'receipt'
                receipt.mkdir(parents=True)
                input_path = task / 'input.json'
                raw_input = workflow._bytes(value)
                input_path.write_bytes(raw_input)
                intent = {'book_id':value['book_id'],'chapter_id':value['chapter_id'],
                          'input_sha256':hashlib.sha256(raw_input).hexdigest()}
                (receipt / 'intent.json').write_text(json.dumps(intent), encoding='utf-8')
                body = json.dumps({'id':'response-1','model':'deepseek-flash',
                    'usage':{'prompt_tokens':11,'completion_tokens':2},
                    'choices':[{'finish_reason':'stop','message':{'content':json.dumps(
                        {'characters':[],'dialogues':[],'chapter_summary':'summary'})}}]},
                    separators=(',',':')).encode('utf-8')
                (receipt / 'response.body').write_bytes(body)
                (receipt / 'response.meta.json').write_text(json.dumps({
                    'http_status':200,'body_sha256':hashlib.sha256(body).hexdigest()}), encoding='utf-8')
                (receipt / 'result.json').write_text(json.dumps(
                    {'status':'needs_review','retry_allowed':False}), encoding='utf-8')

            cache = CachedAnalysisDispatcher(tmp, dispatch=dispatch, worker_sha256='b' * 64)
            first = cache(payload)
            second = cache(payload)
            self.assertFalse(first['reused_analysis'])
            self.assertTrue(second['reused_analysis'])
            self.assertEqual(len(calls), 1)
            body_path = Path(second['receipt_dir']) / 'response.body'
            body_path.write_bytes(body_path.read_bytes() + b' ')
            with self.assertRaises(AnalysisCacheError):
                cache(payload)
            self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
