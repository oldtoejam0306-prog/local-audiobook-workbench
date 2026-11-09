import tempfile
import unittest

import real_book_sequence as seq


class SequencePauseTests(unittest.TestCase):
    def test_pause_after_current_stage_resumes_without_repeating_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = seq.create_plan(tmp, 'book-' + 'a' * 20, 'b' * 64,
                                   [{'id': 'c001', 'source_sha256': 'c' * 64}], {})
            calls, paused = [], [False]
            def callback(stage):
                def run(*args):
                    calls.append(stage)
                    if stage == 'analyze':
                        paused[0] = True
                    return {'status': 'ok', 'artifact': {'stage': stage}}
                return run
            callbacks = {stage: callback(stage) for stage in seq.STAGES}
            result = seq.run(tmp, plan, callbacks, stop_requested=lambda: paused[0])
            self.assertEqual(result['status'], 'paused')
            self.assertEqual(result['next_stage'], 'memory')
            paused[0] = False
            result = seq.run(tmp, plan, callbacks)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(calls, list(seq.STAGES))

    def test_only_explicitly_resumable_render_can_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = seq.create_plan(tmp, 'book-' + 'a' * 20, 'b' * 64,
                                   [{'id': 'c001', 'source_sha256': 'c' * 64}], {})
            calls = []
            def ordinary(*args):
                return {'status': 'ok', 'artifact': {}}
            def render(*args):
                calls.append(1)
                if len(calls) == 1:
                    raise OSError()
                return ordinary()
            callbacks = {stage: ordinary for stage in seq.STAGES}
            callbacks['render'] = render
            self.assertEqual(seq.run(tmp, plan, callbacks)['status'], 'unknown')
            self.assertEqual(seq.run(tmp, plan, callbacks)['status'], 'unknown')
            self.assertEqual(calls, [1])
            self.assertEqual(seq.run(tmp, plan, callbacks, resumable_render=True)['status'], 'complete')
            self.assertEqual(calls, [1, 1])


if __name__ == '__main__':
    unittest.main()
