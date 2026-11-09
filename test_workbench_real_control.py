import unittest
from unittest import mock

import workbench_real_control as control


class WorkbenchControlTests(unittest.TestCase):
    def test_job_identity_changes_with_mode(self):
        self.assertNotEqual(control.identity("sample", "synthetic"), control.identity("sample", "voice"))

    def test_start_selects_only_the_configured_worker_name(self):
        job = control.identity("sample", "synthetic")
        with mock.patch.object(control, "call", return_value={"job": job, "status": "reserved"}) as call:
            result = control.start("sample", "synthetic")
        self.assertEqual(result["job"], job)
        self.assertEqual(call.call_args.args[1], "controlled_real_synthetic.py")

    def test_voice_mode_uses_voice_worker(self):
        job = control.identity("sample", "voice")
        with mock.patch.object(control, "call", return_value={"job": job, "status": "reserved"}) as call:
            control.start("sample", "voice")
        self.assertEqual(call.call_args.args[1], "controlled_real_voice.py")


if __name__ == "__main__":
    unittest.main()
