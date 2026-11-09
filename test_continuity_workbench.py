import contextlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

fake_library=types.ModuleType('experimental_isolated_voice_library')
with mock.patch.dict(sys.modules,{'experimental_isolated_voice_library':fake_library}):
    import continuity_workbench as workbench
workbench.continuity=types.SimpleNamespace(configure=None,inspect=None,book_lock=None,playback_manifest=None)

class WorkbenchTests(unittest.TestCase):
    def test_fresh_run_stages_dynamic_helpers_and_exports_project_pythonpath(self):
        with tempfile.TemporaryDirectory(prefix='fresh-workbench-run-') as td:
            run_root=Path(td)
            workbench.stage_worker_bundle(run_root)
            for name in ('continuity_prepare.py','continuity_runner.py','book_memory.py','voice_design_engine.py'):
                self.assertTrue((run_root/name).is_file())
            env=workbench.worker_environment()
            paths=env['PYTHONPATH'].split(__import__('os').pathsep)
            self.assertIn(str(workbench.DRIVER),paths)
            self.assertTrue(env['AUDIOBOOK_PROJECT_ROOT'])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'workbench-binding.json').write_text(json.dumps({'vector_sha256':'a'*64,'load_evidence_required':True}))
        self.backend=types.SimpleNamespace(ROOT=self.root)
        self.stack=__import__('contextlib').ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(workbench,'run_dir',return_value=self.root))
        self.stack.enter_context(mock.patch.object(workbench.continuity,'configure',return_value=self.backend))
        self.stack.enter_context(mock.patch.object(workbench.continuity,'inspect',return_value={'batch':{'status':'complete'},'lock_available':True}))
        self.stack.enter_context(mock.patch.object(workbench.continuity,'book_lock',return_value=contextlib.nullcontext()))
        self.stack.enter_context(mock.patch.object(workbench.continuity,'playback_manifest',return_value={'chapters':[]}))
        self.stack.enter_context(mock.patch.object(workbench.release_config,'mac_ffmpeg',return_value=Path('synthetic-ffmpeg')))
    def test_completed_batch_requires_observed_matching_synth_load(self):
        req={'action':'status','run_id':str(self.root)}
        with self.assertRaises(ValueError): workbench.query(req)
        (self.root/'vector-loads.jsonl').write_text(json.dumps({'action':'synth','vector_sha256':'b'*64})+'\n')
        with self.assertRaises(ValueError): workbench.query(req)
        (self.root/'vector-loads.jsonl').write_text(json.dumps({'action':'synth','vector_sha256':'a'*64})+'\n')
        self.assertTrue(workbench.query(req)['vector_load_verified'])

if __name__=='__main__': unittest.main()
