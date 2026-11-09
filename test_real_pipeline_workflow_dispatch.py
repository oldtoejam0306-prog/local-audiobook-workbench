import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import real_pipeline_workflow as workflow


BOOK = 'book-' + 'a' * 20


class DispatchPreflightTests(unittest.TestCase):
    def payload(self):
        return {'book_id': BOOK, 'chapter_id': 'c001', 'text': '合成测试文本。'}

    def test_missing_credential_stops_before_task_directories_and_run(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'private'; jobs=Path(td)/'jobs'; calls=[]
            def runner(args, **kwargs):
                calls.append(args)
                return SimpleNamespace(returncode=2,stdout='{"status":"credential_unavailable"}',stderr='')
            with self.assertRaisesRegex(RuntimeError,'credential_unavailable'):
                workflow.dispatch_chapter(self.payload(),root,jobs,node='node',runner=runner)
            self.assertEqual(1,len(calls))
            self.assertEqual('--preflight',calls[0][-1])
            self.assertFalse(root.exists());self.assertFalse(jobs.exists())

    def test_available_credential_preflights_before_reserving_then_runs_once(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'private'; jobs=Path(td)/'jobs'; calls=[]
            def runner(args, **kwargs):
                calls.append(args)
                if args[-1]=='--preflight':
                    return SimpleNamespace(returncode=0,stdout='{"status":"credential_available"}',stderr='')
                return SimpleNamespace(returncode=0,stdout='{"status":"needs_review"}',stderr='')
            result=workflow.dispatch_chapter(self.payload(),root,jobs,node='node',runner=runner)
            self.assertEqual('needs_review',result['status'])
            self.assertEqual('--preflight',calls[0][-1]);self.assertEqual('--run',calls[1][2])
            task=root/(BOOK+'-c001')
            self.assertTrue((task/'dispatch-state.json').is_file())
            self.assertEqual(2,len(calls))

    def test_injected_preflight_can_block_without_touching_worker_or_filesystem(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'private';jobs=Path(td)/'jobs';calls=[]
            def preflight():raise RuntimeError('credential_unavailable')
            def runner(args,**kwargs):calls.append(args);return SimpleNamespace(returncode=0,stdout='{}',stderr='')
            with self.assertRaisesRegex(RuntimeError,'credential_unavailable'):
                workflow.dispatch_chapter(self.payload(),root,jobs,runner=runner,preflight=preflight)
            self.assertEqual([],calls);self.assertFalse(root.exists());self.assertFalse(jobs.exists())


if __name__=='__main__':
    unittest.main()
