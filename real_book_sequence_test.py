import json
import tempfile
import unittest
from pathlib import Path

import real_book_sequence as seq


class SequenceBindingTests(unittest.TestCase):
    def test_two_books_two_chapters_and_tampered_prior(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chapters = [{'id': 'c001', 'source_sha256': '1' * 64},
                        {'id': 'c002', 'source_sha256': '2' * 64}]
            seen = []
            def callback(chapter, prior, artifacts):
                seen.append((chapter, prior, artifacts))
                return {'status': 'ok', 'artifact': {'chapter': chapter['id']}}
            callbacks = {s: callback for s in seq.STAGES}
            a = seq.create_plan(root, 'book-' + 'a' * 20, 'f' * 64, chapters, {'role': 'a'})
            b = seq.create_plan(root, 'book-' + 'b' * 20, 'e' * 64, chapters, {'role': 'b'})
            self.assertEqual(seq.run(root, a, callbacks)['status'], 'complete')
            self.assertEqual(seq.run(root, b, callbacks)['status'], 'complete')
            self.assertEqual(seen[4][1][0]['chapter']['id'], 'c001')
            self.assertEqual(seen[8][1], [])
            self.assertEqual(seen[8][0]['book_id'], b['book_id'])
            self.assertEqual(seen[8][0]['voice_bindings_sha256'], b['voice_bindings_sha256'])
            done = root / a['book_id'] / 'c001' / 'analyze.done.json'
            record = json.loads(done.read_text(encoding='utf-8'))
            record['artifact']['chapter'] = 'changed'
            done.write_text(json.dumps(record), encoding='utf-8')
            with self.assertRaises(ValueError):
                seq.run(root, a, callbacks)
            self.assertEqual(len(seen), 16)

    def test_unknown_does_not_repeat_after_new_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = seq.create_plan(tmp, 'book-' + 'c' * 20, 'd' * 64,
                                   [{'id': 'c001', 'source_sha256': 'a' * 64}], {})
            calls = []
            def uncertain(*args):
                calls.append(1)
                raise RuntimeError('unknown outcome')
            callbacks = {s: uncertain for s in seq.STAGES}
            self.assertEqual(seq.run(tmp, plan, callbacks)['status'], 'unknown')
            self.assertEqual(seq.run(tmp, plan, callbacks)['status'], 'unknown')
            self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
