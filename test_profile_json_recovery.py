import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import character_profile_jobs
import real_pipeline_workflow as workflow


BOOK = 'book-' + '1' * 20
USAGE = {'prompt_tokens': 6216, 'completion_tokens': 3882, 'total_tokens': 10098}
REPO = Path(__file__).parent


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return workflow._bytes(value)


def _profile(name, quote):
    evidence = [{'chapter_id': 'c001', 'quote': quote}]
    unknown = {'value': '', 'status': 'unknown', 'evidence': []}
    return {'name': name, 'name_evidence': evidence, 'aliases': [], 'identity': unknown,
        'gender': unknown, 'age': unknown, 'personality': [], 'behavior': [], 'speech_style': [],
        'voice_design': {'mode': 'character_voice', 'description': '', 'rationale': '', 'creative_choices': []},
        'uncertainties': []}


def _malformed_content():
    quote = '甲向乙点头。'
    profiles = [_profile('甲', quote), _profile('乙', quote)]
    parsed = {'characters': profiles, 'relations': [], 'narration_design': None,
        'scope_summary': 'synthetic fixture'}
    content = json.dumps(parsed, ensure_ascii=False, separators=(',', ':'))
    search_from = 0
    for profile in profiles:
        needle = '"name_evidence":' + json.dumps(profile['name_evidence'], ensure_ascii=False, separators=(',', ':'))
        start = content.index(needle, search_from)
        end = start + len(needle)
        content = content[:end] + '}]' + content[end:]
        search_from = end + 2
    return content


class ProfileJsonRecoveryTests(unittest.TestCase):
    def test_offline_recovery_binds_all_artifacts_and_keeps_originals(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is unavailable')
        with tempfile.TemporaryDirectory() as td:
            batch = Path(td) / 'batch'; receipt = batch / 'receipt' / 'character_profiles_v1'
            receipt.mkdir(parents=True)
            text = '甲向乙点头。'
            input_doc = {'mode': 'character_profiles_v1', 'book_id': BOOK, 'chapter_id': 'c001',
                'discover_characters': True, 'target_names': [],
                'chapters': [{'chapter_id': 'c001', 'text': text, 'text_sha256': sha(text.encode('utf-8'))}]}
            input_raw = canonical(input_doc)
            (batch / 'input.json').write_bytes(input_raw)
            worker_raw = b'fixture worker bytes'
            protocol_raw = (REPO / 'character_profile_protocol.mjs').read_bytes()
            (batch / 'worker-source.mjs').write_bytes(worker_raw)
            (batch / 'protocol-source.mjs').write_bytes(protocol_raw)
            request_raw = b'{"fixture":"request"}'
            (receipt / 'request.body.json').write_bytes(request_raw)
            body_raw = canonical({'id': 'fixture-response', 'usage': USAGE,
                'choices': [{'finish_reason': 'stop', 'message': {'content': _malformed_content()}}]})
            (receipt / 'response.body').write_bytes(body_raw)
            meta_raw = canonical({'http_status': 200, 'body_sha256': sha(body_raw)})
            (receipt / 'response.meta.json').write_bytes(meta_raw)
            (receipt / 'usage.json').write_bytes(canonical({'usage': USAGE}))
            original_result = b'{"status":"invalid_or_usage_unknown","retry_allowed":false}'
            (receipt / 'result.json').write_bytes(original_result)
            intent = {'purpose': 'character_profiles_v1', 'input_sha256': sha(input_raw),
                'request_sha256': sha(request_raw), 'worker_source_sha256': sha(worker_raw),
                'protocol_source_sha256': sha(protocol_raw)}
            (receipt / 'intent.json').write_bytes(canonical(intent))

            completed = subprocess.run([node, str(REPO / 'profile_json_recovery.mjs'), '--recover', str(batch)],
                capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(0, completed.returncode, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(2, len(report['repair']['removals']))
            self.assertEqual(sorted(row['character_offset'] for row in report['repair']['removals']),
                [row['character_offset'] for row in report['repair']['removals']])
            self.assertEqual(original_result, (receipt / 'result.json').read_bytes())
            self.assertEqual(body_raw, (receipt / 'response.body').read_bytes())

            recovered, usage, _receipt = character_profile_jobs._read_success(batch, sha(input_raw))
            self.assertEqual(USAGE, usage)
            self.assertEqual('fixture-response', recovered['response_id'])
            self.assertEqual(['甲', '乙'], [row['name'] for row in recovered['result']['characters']])
            self.assertFalse(recovered['result']['human_reviewed'])

    def test_mismatched_recovery_binding_is_rejected_without_reusing_original_result(self):
        with tempfile.TemporaryDirectory() as td:
            batch = Path(td) / 'batch'; receipt = batch / 'receipt' / 'character_profiles_v1'
            receipt.mkdir(parents=True)
            input_raw = b'{}'; body = b'{}'; meta_raw = b'{}'; result_raw = b'{}'
            (batch / 'input.json').write_bytes(input_raw)
            (batch / 'worker-source.mjs').write_bytes(b'worker')
            (batch / 'protocol-source.mjs').write_bytes(b'protocol')
            (receipt / 'intent.json').write_bytes(canonical({'input_sha256': sha(input_raw),
                'purpose': 'character_profiles_v1', 'request_sha256': sha(b'{}'),
                'worker_source_sha256': sha(b'worker'), 'protocol_source_sha256': sha(b'protocol')}))
            (receipt / 'request.body.json').write_bytes(b'{}')
            (receipt / 'response.body').write_bytes(canonical({'id': 'fixture-response', 'usage': USAGE}))
            (receipt / 'response.meta.json').write_bytes(canonical({'http_status': 200,
                'body_sha256': sha((receipt / 'response.body').read_bytes())}))
            (receipt / 'usage.json').write_bytes(canonical({'usage': USAGE}))
            (receipt / 'result.json').write_bytes(result_raw)
            (batch / 'profile-json-recovery-source.mjs').write_bytes(b'helper')
            (receipt / 'result.local-format-recovered.json').write_bytes(b'{}')
            sidecar = {'version': 1, 'kind': 'offline_profile_json_boundary_recovery',
                'input_sha256': sha(input_raw), 'response_body_sha256': sha((receipt / 'response.body').read_bytes()),
                'response_meta_sha256': sha((receipt / 'response.meta.json').read_bytes()),
                'original_result_sha256': sha(result_raw), 'protocol_source_sha256': sha(b'protocol'),
                'helper_source_sha256': sha(b'helper'), 'recovered_result_sha256': sha(b'{}'),
                'response_id': 'fixture-response', 'usage': USAGE,
                'repair': {'kind': 'remove_premature_name_evidence_closers', 'removals': [
                    {'character_offset': 1, 'removed': '}]', 'next_field': 'aliases'}]}}
            sidecar['input_sha256'] = '0' * 64
            (receipt / 'local-format-recovery.json').write_bytes(canonical(sidecar))
            with self.assertRaises(character_profile_jobs.ProfileJobError) as raised:
                character_profile_jobs._read_success(batch, sha(input_raw))
            self.assertEqual('invalid_result', raised.exception.status)
            self.assertFalse(raised.exception.retry_allowed)


if __name__ == '__main__':
    unittest.main()
