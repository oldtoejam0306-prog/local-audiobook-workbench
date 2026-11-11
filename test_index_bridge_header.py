"""Regression tests for the one-shot index bridge response header contract."""
import ast
import contextlib
import hashlib
import json
from pathlib import Path
import types
import unittest

import character_profile_jobs
import real_pipeline_workflow as workflow


BOOK = 'book-a7d5af59d0918b40bfc1'
INDEX_SHA = 'a' * 64
SNAPSHOT = {'book_id': BOOK, 'source_sha256': '1' * 64, 'text_sha256': '2' * 64,
            'script_sha256': '3' * 64, 'cast_sha256': '4' * 64}
CHAPTERS = ['c010', 'c011']


class BridgeHeaderTests(unittest.TestCase):
    @staticmethod
    def _query():
        source = Path(__file__).with_name('workbench_real_index_mac.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'query')
        module = ast.Module(body=[function], type_ignores=[])
        namespace = _fake_namespace()
        exec(compile(module, 'workbench_real_index_mac.py::query', 'exec'), namespace)
        return namespace['query']

    def test_prepare_forwards_verified_chapter_id_and_list_shape_is_unchanged(self):
        query = self._query()
        prepared = query({'action': 'prepare', 'book_id': BOOK, 'chapter_id': 'c011'})
        self.assertEqual('c011', prepared['chapter_id'])
        self.assertEqual('c011', prepared['payload']['chapter_id'])
        self.assertEqual(INDEX_SHA, prepared['index_sha256'])
        self.assertIs(prepared['human_reviewed'], False)

        listing = query({'action': 'list', 'book_id': BOOK})
        self.assertEqual(CHAPTERS, listing['chapters'])
        self.assertNotIn('chapter_id', listing)
        self.assertEqual({'book_id', 'index_sha256', 'human_reviewed', 'chapters'}, set(listing))

    def test_flow_rejects_mismatched_redundant_header_even_with_valid_payload_hash(self):
        payload = {'book_id': BOOK, 'chapter_id': 'c010', 'text': 'fixture only'}
        response = {'book_id': BOOK, 'chapter_id': 'c011', 'human_reviewed': False,
            'index_sha256': INDEX_SHA,
            'payload_sha256': hashlib.sha256(workflow._bytes(payload)).hexdigest(), 'payload': payload}
        with self.assertRaisesRegex(ValueError, 'verified chapter index identity mismatch'):
            character_profile_jobs._source_chapter(response, BOOK, 'c010', INDEX_SHA)


def _fake_namespace():
    manifest = json.dumps({'chapters': [{'chapter_id': cid} for cid in CHAPTERS]}).encode('utf-8')
    persisted = types.SimpleNamespace()
    persisted._process_lock = lambda _path: contextlib.nullcontext()
    persisted._read_regular = lambda _path, **_kwargs: manifest

    def restore_chapter(_index, chapter_id, *, trusted_snapshot, expected_index_sha256):
        if trusted_snapshot != SNAPSHOT or expected_index_sha256 != INDEX_SHA:
            raise AssertionError('fixture received a changed verified identity')
        return {'chapter_id': chapter_id, 'script_rows': [{'header': True}, {'block': True}],
            'chapter_digest': '5' * 64, 'local_text_sha256': '6' * 64, 'index_sha256': INDEX_SHA}

    persisted.restore_chapter = restore_chapter
    workspace = types.SimpleNamespace(_lock_existing=lambda _path: contextlib.nullcontext())

    def prepare(record):
        payload = {'book_id': BOOK, 'chapter_id': record['chapter_id'], 'text': 'fixture only'}
        return {'payload': payload,
            'payload_sha256': hashlib.sha256(workflow._bytes(payload)).hexdigest()}

    adapter = types.SimpleNamespace(prepare=prepare)

    class RpcService:
        def __init__(self, _registry):
            pass

        def dispatch(self, request):
            if request['book_id'] != BOOK:
                raise AssertionError('unexpected book')
            target = request.get('chapter_id')
            summary = rpc._restore_summary(Path('/fixture/index'), target, SNAPSHOT, INDEX_SHA)
            if request['action'] == 'status':
                return {'book_id': BOOK, 'chapter_count': summary['chapter_count'],
                    'index_sha256': INDEX_SHA, 'human_reviewed': False}
            return {'book_id': BOOK, 'chapter_id': summary['chapter_id'],
                'index_sha256': INDEX_SHA, 'human_reviewed': False}

    rpc = types.SimpleNamespace(load_registry=lambda _path: {BOOK: {}}, VerifiedIndexRpc=RpcService,
        _restore_summary=lambda *_args: None)
    release_config = types.SimpleNamespace(section=lambda _name: {'index_registry': 'fixture-registry'})
    import re
    return {'contextlib': contextlib, 'json': json, 'Path': Path, 're': re,
        'release_config': release_config, 'BOOKS': {BOOK},
        'REGISTRY': Path('/fixture/registry.json'), 'rpc': rpc,
        'workspace': workspace, 'persisted': persisted, 'adapter': adapter}


if __name__ == '__main__':
    unittest.main()
