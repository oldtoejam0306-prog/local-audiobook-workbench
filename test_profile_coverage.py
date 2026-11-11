import unittest

from profile_coverage import build_coverage_report


class ProfileCoverageTests(unittest.TestCase):
    def test_distinguishes_covered_reused_failed_unknown_deferred_and_hash_conflict(self):
        registry = {'book_id': 'book-' + '1' * 20,
            'source': {'chapters': [
                {'chapter_id': 'c001', 'text_sha256': 'a' * 64},
                {'chapter_id': 'c002', 'text_sha256': 'b' * 64},
                {'chapter_id': 'c003', 'text_sha256': 'c' * 64},
                {'chapter_id': 'c004', 'text_sha256': 'd' * 64},
                {'chapter_id': 'c005', 'text_sha256': 'e' * 64},
                {'chapter_id': 'c005', 'text_sha256': 'f' * 64}]},
            'profile_jobs': [
                {'chapters': [{'chapter_id': 'c001'}], 'status': 'needs_review'},
                {'chapters': [{'chapter_id': 'c002'}], 'status': 'needs_review', 'reused': True},
                {'chapters': [{'chapter_id': 'c003'}], 'status': 'failed'},
                {'chapters': [{'chapter_id': 'c004'}], 'status': 'usage_unknown'}],
            'unresolved_relations': [{'reason': 'test'}]}
        report = build_coverage_report(['c001', 'c002', 'c003', 'c004', 'c005', 'c006'], registry)
        self.assertEqual(['covered', 'covered_reused', 'failed', 'unknown', 'source_hash_conflict', 'deferred'],
                         [row['status'] for row in report['chapters']])
        self.assertEqual(6, report['counts']['requested'])
        self.assertEqual(1, report['counts']['covered_reused'])
        self.assertEqual(1, report['counts']['source_hash_conflict'])
        self.assertEqual('not_assessed', report['semantic_accuracy'])
        self.assertEqual(1, report['unresolved_counts']['unresolved_relations'])

    def test_supplied_receipt_status_and_source_reference_only_are_not_misreported(self):
        registry = {'source': {'chapters': [{'chapter_id': 'c001', 'text_sha256': 'a' * 64}]}}
        report = build_coverage_report(['c001', 'c002'], registry, [
            {'chapters': ['c002'], 'status': 'invalid_or_usage_unknown'}])
        self.assertEqual('source_referenced_status_unconfirmed', report['chapters'][0]['status'])
        self.assertEqual('unknown', report['chapters'][1]['status'])

    def test_rejects_duplicate_or_empty_requested_scope(self):
        with self.assertRaises(ValueError):
            build_coverage_report([], {})
        with self.assertRaises(ValueError):
            build_coverage_report(['c001', 'c001'], {})


if __name__ == '__main__':
    unittest.main()
