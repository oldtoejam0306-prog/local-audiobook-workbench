import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import legacy_chapter_state as state


class LegacyChapterStateTests(unittest.TestCase):
    def test_only_configured_legacy_root_and_complete_jobs_are_clear(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); jobs = root / 'jobs'; jobs.mkdir()
            done = jobs / 'old-job'; done.mkdir()
            (done / 'state.json').write_text(json.dumps({'status': 'complete'}), encoding='utf-8')
            payload = {'action': 'check', 'book_id': 'book-' + 'a' * 20,
                       'chapter_id': 'c001', 'legacy_root': str(root)}
            with patch.object(state.release_config, 'remote_legacy_chapter_root', return_value=root):
                self.assertEqual(state.check(payload), {'status': 'clear'})
                payload['chapter_id'] = 'c002'; duplicate = jobs / (payload['book_id'] + '-c002'); duplicate.mkdir()
                self.assertEqual(state.check(payload), {'status': 'blocked'})

    def test_unknown_or_unsafe_state_blocks_and_root_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); jobs = root / 'jobs'; jobs.mkdir()
            (jobs / 'active-job').mkdir()
            payload = {'action': 'check', 'book_id': 'book-' + 'b' * 20,
                       'chapter_id': 'c001', 'legacy_root': str(root)}
            with patch.object(state.release_config, 'remote_legacy_chapter_root', return_value=root):
                self.assertEqual(state.check(payload), {'status': 'blocked'})
            different = root / 'different'; different.mkdir()
            with patch.object(state.release_config, 'remote_legacy_chapter_root', return_value=different):
                with self.assertRaises(ValueError): state.check(payload)


if __name__ == '__main__': unittest.main()
