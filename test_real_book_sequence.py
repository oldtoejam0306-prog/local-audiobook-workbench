import tempfile
import unittest
from pathlib import Path
import real_book_sequence as seq


class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.book = 'book-' + 'a' * 20
        self.chapters = [{'id': 'c008', 'source_sha256': '1'*64}, {'id': 'c008.p0038.01', 'source_sha256': '2'*64}]
        self.plan = seq.create_plan(self.root, self.book, 'f'*64, self.chapters, {'narrator': 'v1'})
        self.calls = []

    def callbacks(self, fail=None, conflict=False):
        def callback(stage):
            def execute(chapter, prior, artifacts):
                self.calls.append((stage, chapter['id'], prior))
                if stage == fail:
                    raise RuntimeError('uncertain')
                if stage == 'memory' and conflict:
                    return {'status': 'alias_conflict'}
                return {'status': 'ok', 'artifact': {'stage': stage, 'chapter': chapter['id']}}
            return execute
        return {s: callback(s) for s in seq.STAGES}

    def test_order_and_idempotence(self):
        cb = self.callbacks()
        self.assertEqual(seq.run(self.root, self.plan, cb)['status'], 'complete')
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(self.calls[4][2][0]['chapter']['id'], 'c008')
        self.assertEqual(seq.run(self.root, self.plan, cb)['status'], 'complete')
        self.assertEqual(len(self.calls), 8)

    def test_unknown_never_retries(self):
        cb = self.callbacks(fail='analyze')
        self.assertEqual(seq.run(self.root, self.plan, cb)['status'], 'unknown')
        self.assertEqual(seq.run(self.root, self.plan, cb)['status'], 'unknown')
        self.assertEqual(len(self.calls), 1)

    def test_conflict_stops_and_does_not_retry(self):
        cb = self.callbacks(conflict=True)
        self.assertEqual(seq.run(self.root, self.plan, cb)['stage'], 'memory')
        self.assertEqual(seq.run(self.root, self.plan, cb)['status'], 'unknown')
        self.assertEqual([x[0] for x in self.calls], ['analyze', 'memory'])

    def test_book_isolation_and_plan_immutable(self):
        cb = self.callbacks()
        seq.run(self.root, self.plan, cb)
        other = seq.create_plan(self.root, 'book-'+'b'*20, 'f'*64, self.chapters, {'narrator':'v1'})
        seq.run(self.root, other, cb)
        self.assertEqual(self.calls[8][2], [])
        with self.assertRaises(ValueError):
            seq.create_plan(self.root, self.book, 'f'*64, list(reversed(self.chapters)), {'narrator':'v1'})
        with self.assertRaises(ValueError):
            seq.create_plan(self.root, self.book, 'f'*64, self.chapters, {'narrator':'changed'})

    def test_lock_stops_same_book(self):
        (self.root/self.book/'active.lock').write_text('{}')
        self.assertEqual(seq.run(self.root, self.plan, self.callbacks())['status'], 'busy_or_uncertain')
        self.assertEqual(self.calls, [])

if __name__ == '__main__':
    unittest.main()
