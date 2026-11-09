import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import isolated_real_batch_controller as controller
import isolated_real_multivoice_mac as multivoice
import release_config


class MultiVoiceControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multi-controller-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.controlled = self.root / "controlled"
        self.input = self.data / "request.json"
        self.input.write_text(json.dumps({"synthetic": True}), encoding="utf-8")
        self.voices = [{"role": "narrator", "stage": self.root / "voice", "vector_sha256": "a" * 64}]
        self.patches = [
            mock.patch.object(release_config, "mac_data_root", return_value=self.data),
            mock.patch.object(release_config, "mac_controlled_root", return_value=self.controlled),
            mock.patch.object(release_config, "mac_multivoice_voices", return_value=self.voices),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fake_worker(self, statuses):
        calls = {"count": 0}
        def run(args, timeout):
            calls["count"] += 1
            status, before, after, model_hash = statuses.pop(0)
            run_root = self.controlled / "multivoice"
            run = run_root / f"c001-run-{calls['count']}"
            stage = run / "huashuo-isolated-fixture"
            stage.mkdir(parents=True)
            if before is not None:
                (stage / "batch-before.json").write_text(json.dumps(before), encoding="utf-8")
            if after is not None:
                (stage / "batch-after.json").write_text(json.dumps(after), encoding="utf-8")
            response = {"run": str(run), "status": status, "model_inventory_sha256": model_hash}
            return 0, json.dumps(response), "", True
        return calls, run

    def test_completed_same_input_voice_is_idempotent_and_explicit_redo_is_new(self):
        calls, fake = self._fake_worker([("complete", None, None, "b" * 64), ("complete", None, None, "b" * 64)])
        with mock.patch.object(controller, "_run_worker", side_effect=fake):
            first = controller.run_controller(str(self.input))
            repeated = controller.run_controller(str(self.input))
            redo = controller.run_controller(str(self.input), redo_nonce="operator-approved-20261106")
        self.assertEqual(first["status"], "complete")
        self.assertEqual(repeated["status"], "already_complete")
        self.assertEqual(redo["status"], "complete")
        self.assertEqual(calls["count"], 2)
        self.assertEqual(len(list((self.controlled / "multivoice" / "jobs").glob("*/controller.json"))), 2)

    def test_no_cache_progress_requires_review_and_does_not_restart(self):
        before = {"cached": 2, "total": 5}
        after = {"cached": 2, "total": 5, "remaining": 3}
        calls, fake = self._fake_worker([("batch_pending", before, after, "b" * 64)])
        with mock.patch.object(controller, "_run_worker", side_effect=fake):
            first = controller.run_controller(str(self.input))
            second = controller.run_controller(str(self.input))
        self.assertEqual(first["status"], "requires_review")
        self.assertEqual(second["status"], "requires_review")
        self.assertEqual(calls["count"], 1)
        self.assertFalse((self.controlled / "active.lock").exists())

    def test_uncertain_descendant_keeps_shared_lock_and_blocks_replay(self):
        calls, fake = self._fake_worker([])
        with mock.patch.object(controller, "_run_worker", return_value=(None, "", "", False)) as worker:
            first = controller.run_controller(str(self.input))
            second = controller.run_controller(str(self.input))
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(second["status"], "requires_review")
        self.assertTrue((self.controlled / "active.lock").is_dir())
        worker.assert_called_once()

    def test_model_inventory_cannot_change_between_batches(self):
        before = {"cached": 0, "total": 3}
        after = {"cached": 1, "total": 3, "remaining": 2}
        calls, fake = self._fake_worker([
            ("batch_pending", before, after, "b" * 64),
            ("complete", None, None, "c" * 64),
        ])
        with mock.patch.object(controller, "_run_worker", side_effect=fake):
            result = controller.run_controller(str(self.input))
        self.assertEqual(result["status"], "requires_review")
        self.assertEqual(calls["count"], 2)
        self.assertFalse((self.controlled / "active.lock").exists())

    def test_prior_publications_reuse_voice_refs_and_reject_changed_vectors(self):
        parent = self.root / "experiment-stage"
        parent.mkdir()
        libdir = parent / "published"
        libdir.mkdir()
        voice = {"role": "narrator", "stage": self.root / "voice", "vector_sha256": "a" * 64}
        publication = [{"role": "narrator", "voice_ref": "library:zh/stable", "library_dir": str(libdir),
                        "metadata": {"vector_sha256": "a" * 64}}]
        first = multivoice.validate_publications([voice], publication, parent)
        later = multivoice.validate_publications([voice], publication, parent)
        self.assertEqual(first, later)
        changed = [{**voice, "vector_sha256": "c" * 64}]
        with self.assertRaises(ValueError):
            multivoice.validate_publications(changed, publication, parent)

    def test_process_group_is_terminated_and_confirmed_gone(self):
        with mock.patch.object(controller.os, "name", "posix"), \
             mock.patch.object(controller.os, "killpg", side_effect=[None, ProcessLookupError()], create=True) as kill:
            self.assertTrue(controller._stop_group(123, grace_seconds=0.01))
        self.assertEqual(kill.call_args_list[0].args, (123, signal.SIGTERM))
        self.assertEqual(kill.call_args_list[1].args, (123, 0))

    def test_process_group_that_survives_kill_remains_unknown(self):
        with mock.patch.object(controller.os, "name", "posix"), \
             mock.patch.object(controller.os, "killpg", return_value=None, create=True), \
             mock.patch.object(controller.time, "monotonic", side_effect=[0.0, 10.0, 0.0, 10.0]):
            self.assertFalse(controller._stop_group(123, grace_seconds=0))


if __name__ == "__main__":
    unittest.main()
