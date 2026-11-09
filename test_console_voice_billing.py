import hashlib, json, os, subprocess, tempfile, unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import Mock, patch
from console_voice_billing import (_saved_receipts, load_records, estimate, price,
                                   register_evidence, imported_records,
                                   _is_reparse_point, usage_totals)
import console_voice_billing as billing

class BillingTest(unittest.TestCase):
    def _junction(self, target, alias):
        try:
            os.symlink(target, alias, target_is_directory=True)
            return
        except OSError:
            if os.name == 'nt':
                quote = lambda value: "'" + str(value).replace("'", "''") + "'"
                script = f"New-Item -ItemType Junction -Path {quote(alias)} -Target {quote(target)} | Out-Null"
                try:
                    subprocess.run(['powershell.exe', '-NoProfile', '-Command', script],
                                   check=True, capture_output=True, text=True, timeout=15)
                    if alias.is_dir(): return
                except (OSError, subprocess.SubprocessError):
                    pass
            self.skipTest('temporary directory junctions are unavailable')

    def test_known_and_unknown_are_separate(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root/'a').mkdir(); (root/'a'/'intent.json').write_text('{}')
            (root/'a'/'usage.json').write_text(json.dumps({'model':'deepseek-flash','usage':{'prompt_tokens':1000000,'completion_tokens':500000,'total_tokens':1500000}}))
            rows = load_records(root, [('known','a','usage.json'), ('missing','b','usage.json')], root/'registry.json')
            self.assertEqual(estimate(rows[0], price('2'), price('8')), Decimal('6'))
            self.assertIsNone(estimate(rows[1], price('2'), price('8')))
            (root/'a'/'usage.json').write_text(json.dumps({'model':'deepseek-flash','usage':{'prompt_tokens':True,'completion_tokens':1}}))
            self.assertIsNone(load_records(root, [('bad','a','usage.json')], root/'registry.json')[0]['input'])
            self.assertIsNone(rows[-1]['input'])

    def test_summary_separates_unknown_usage_from_deduped_responses(self):
        rows = [
            {'input':900,'output':10,'status':'HTTP失败 / 用量已确认；费用仅估算'},
            {'input':None,'output':None,'status':'未知 / 回执不可读，未计入'},
            {'input':None,'output':None,'status':'重复响应 / 未重复计入'},
        ]
        totals = usage_totals(rows, price('2'), price('8'))
        self.assertEqual(len(totals['known_rows']), 1)
        self.assertEqual(totals['unknown_count'], 1)
        self.assertEqual(totals['duplicate_count'], 1)
        self.assertEqual(totals['estimated_cost'], Decimal('0.00188'))

    def _write_auto_body(self, root, response_id, prompt, completion, *, padding=''):
        folder = Path(root) / ('receipt-' + str(prompt) + '-' + str(completion))
        folder.mkdir(parents=True)
        (folder/'intent.json').write_text('{}', encoding='utf-8')
        (folder/'response.http-body').write_text(json.dumps({
            'model':'deepseek-flash','id':response_id,
            'usage':{'prompt_tokens':prompt,'completion_tokens':completion},'extra':padding,
        }), encoding='utf-8')
        return folder

    def test_extra_root_receipts_are_counted_and_keep_their_absolute_evidence_root(self):
        with tempfile.TemporaryDirectory() as td:
            primary=Path(td)/'legacy';extra=Path(td)/'workbench';primary.mkdir();extra.mkdir()
            self._write_auto_body(primary,'legacy-id',5,1)
            self._write_auto_body(extra,'workbench-id',21,3)
            rows=load_records(root=primary,records=(),registry=primary/'registry.json',extra_roots=[extra])
            workbench=next(row for row in rows if row.get('input')==21)
            self.assertEqual(Path(workbench['_root']),extra.resolve())
            self.assertTrue((Path(workbench['_root'])/workbench['source']).is_file())
            totals=usage_totals(rows,price('2'),price('8'))
            self.assertEqual(len(totals['known_rows']),2)
            app=Mock();app.table.selection.return_value=('0',);app.rows=[workbench];app.roots=(primary,)
            app.detail=Mock()
            billing.BillingWindow.selected(app, None)
            self.assertIn(str(extra.resolve()/'receipt-21-3'/'response.http-body'),app.detail.set.call_args.args[0])

    def test_cross_root_same_response_id_is_counted_once(self):
        with tempfile.TemporaryDirectory() as td:
            primary=Path(td)/'legacy';extra=Path(td)/'workbench';primary.mkdir();extra.mkdir()
            self._write_auto_body(primary,'shared-id',13,4)
            self._write_auto_body(extra,'shared-id',13,4)
            rows=load_records(root=primary,records=(),registry=primary/'registry.json',extra_roots=[extra])
            known=[row for row in rows if row.get('input')==13 and row.get('output')==4]
            self.assertEqual(len(known),1)
            self.assertEqual(usage_totals(rows,price('2'),price('8'))['duplicate_count'],0)

    def test_cross_root_same_id_different_usage_invalidates_both(self):
        with tempfile.TemporaryDirectory() as td:
            primary=Path(td)/'legacy';extra=Path(td)/'workbench';primary.mkdir();extra.mkdir()
            self._write_auto_body(primary,'shared-id',13,4)
            self._write_auto_body(extra,'shared-id',99,7)
            rows=load_records(root=primary,records=(),registry=primary/'registry.json',extra_roots=[extra])
            conflicts=[row for row in rows if row.get('status')=='证据冲突 / 同响应ID用量不一致，未计入']
            self.assertEqual(len(conflicts),2)
            self.assertTrue(all(row['input'] is None and row['output'] is None for row in conflicts))

    def test_incomplete_extra_root_is_unknown_without_discarding_primary_root(self):
        with tempfile.TemporaryDirectory() as td:
            primary=Path(td)/'legacy';extra=Path(td)/'workbench';primary.mkdir();extra.mkdir()
            self._write_auto_body(primary,'legacy-id',5,1)
            self._write_auto_body(extra,'new-id',21,3,padding='x'*700)
            with patch.object(billing,'MAX_RECEIPT_BYTES',256):
                rows=load_records(root=primary,records=(),registry=primary/'registry.json',extra_roots=[extra])
            self.assertTrue(any(row.get('input')==5 for row in rows))
            markers=[row for row in rows if row.get('_scan_incomplete')]
            self.assertEqual(len(markers),1)
            self.assertEqual(Path(markers[0]['_root']),extra.resolve())
            self.assertFalse(any(row.get('input')==21 for row in rows))
    def test_registration_deduplicates_and_strips_content(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); registry = root/'ledger.json'
            (root/'a').mkdir(); (root/'a'/'intent.json').write_text('{}')
            (root/'a'/'usage.json').write_text(json.dumps({'model':'deepseek-flash','response_id':'same-id','usage':{'prompt_tokens':5,'completion_tokens':2},'content':'PRIVATE TEXT'}))
            register_evidence(root/'a'/'usage.json', root, registry, ())
            self.assertNotIn('PRIVATE TEXT', registry.read_text())
            with self.assertRaises(ValueError): register_evidence(root/'a'/'usage.json', root, registry, ())
            (root/'b').mkdir(); (root/'b'/'intent.json').write_text('{}')
            (root/'b'/'usage.json').write_text(json.dumps({'model':'deepseek-flash','response_id':'same-id','usage':{'prompt_tokens':5,'completion_tokens':2},'content':'PRIVATE TEXT'}))
            with self.assertRaises(ValueError): register_evidence(root/'b'/'usage.json', root, registry, ())
            rows = load_records(root, (), registry)
            self.assertEqual(rows[-1]['input'], 5)
    def test_saved_private_receipts_are_auto_discovered_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); registry = root/'registry.json'
            for name in ('saved-a', 'saved-b'):
                folder = root/name; folder.mkdir(); (folder/'intent.json').write_text('{}')
                (folder/'response.http-body').write_text(json.dumps({
                    'model':'deepseek-flash', 'id':'same-response',
                    'usage':{'prompt_tokens':13,'completion_tokens':4},
                    'content':'PRIVATE TEXT MUST NOT APPEAR IN BILLING OUTPUT',
                }), encoding='utf-8')
            rows = load_records(root, (), registry)
            automatic = [row for row in rows if row['status'] == '自动收集']
            self.assertEqual(len(automatic), 1)
            self.assertEqual((automatic[0]['input'], automatic[0]['output']), (13, 4))
            self.assertNotIn('PRIVATE TEXT', repr(rows))
            self.assertFalse(registry.exists())

    def test_usage_without_id_can_borrow_only_one_consistent_sibling_identity(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); folder = root/'saved'; folder.mkdir()
            (folder/'intent.json').write_text('{}', encoding='utf-8')
            usage = {'model':'deepseek-flash','usage':{'prompt_tokens':13,'completion_tokens':4}}
            body = {'model':'deepseek-flash','id':'same-response',
                    'usage':{'prompt_tokens':13,'completion_tokens':4}}
            (folder/'usage.json').write_text(json.dumps(usage), encoding='utf-8')
            (folder/'response.body').write_text(json.dumps(body), encoding='utf-8')
            (folder/'response.http-body').write_text(json.dumps(body), encoding='utf-8')
            rows = load_records(root, (), root/'registry.json')
            automatic = [row for row in rows if row['status'] == '自动收集']
            self.assertEqual(len(automatic), 1)
            self.assertEqual((automatic[0]['input'], automatic[0]['output']), (13, 4))
    def test_auto_discovery_does_not_double_count_registered_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); registry = root/'registry.json'; folder = root/'saved'; folder.mkdir()
            (folder/'intent.json').write_text('{}')
            receipt = folder/'usage.json'
            receipt.write_text(json.dumps({'model':'deepseek-flash','response_id':'registered-once',
                                           'usage':{'prompt_tokens':19,'completion_tokens':2}}), encoding='utf-8')
            register_evidence(receipt, root, registry, ())
            rows = load_records(root, (), registry)
            token_rows = [row for row in rows if row['input'] == 19 and row['output'] == 2]
            self.assertEqual(len(token_rows), 1)
            self.assertEqual(token_rows[0]['status'], '已登记')
    def test_static_usage_receipt_uses_sibling_id_for_auto_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); folder = root/'saved'; folder.mkdir(); (folder/'intent.json').write_text('{}')
            (folder/'usage.json').write_text(json.dumps({'model':'deepseek-flash',
                'usage':{'prompt_tokens':23,'completion_tokens':6}}), encoding='utf-8')
            (folder/'response.body').write_text(json.dumps({'model':'deepseek-flash','id':'sibling-id',
                'usage':{'prompt_tokens':23,'completion_tokens':6},'content':'PRIVATE TEXT'}), encoding='utf-8')
            rows = load_records(root, [('fixed','saved','usage.json')], root/'registry.json')
            token_rows = [row for row in rows if row['input'] == 23 and row['output'] == 6]
            self.assertEqual(len(token_rows), 1)
            self.assertNotIn('PRIVATE TEXT', repr(rows))

    def test_sibling_usage_mismatch_is_conflict_and_never_counted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); folder = root/'saved'; folder.mkdir()
            (folder/'intent.json').write_text('{}', encoding='utf-8')
            (folder/'usage.json').write_text(json.dumps({'model':'deepseek-flash',
                'usage':{'prompt_tokens':23,'completion_tokens':6}}), encoding='utf-8')
            (folder/'response.body').write_text(json.dumps({'model':'deepseek-flash',
                'id':'sibling-id','usage':{'prompt_tokens':99,'completion_tokens':7},
                'content':'PRIVATE TEXT'}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '回执冲突'):
                register_evidence(folder/'usage.json', root, root/'registry.json', ())
            rows = load_records(root, (), root/'registry.json')
            conflicts = [row for row in rows if row['status'].startswith('证据冲突 /')]
            self.assertEqual(len(conflicts), 1)
            self.assertIsNone(conflicts[0]['input'])
            self.assertNotIn('PRIVATE TEXT', repr(rows))
            static = load_records(root, [('fixed', 'saved', 'usage.json')], root/'registry.json')
            fixed = next(row for row in static if row['label'] == 'fixed')
            self.assertTrue(fixed['status'].startswith('证据冲突 /'))
            self.assertIsNone(fixed['input'])
            self.assertFalse((root/'registry.json').exists())

    def test_missing_id_auto_receipt_is_visible_as_unknown_not_silent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); folder = root/'saved'; folder.mkdir()
            (folder/'intent.json').write_text('{}', encoding='utf-8')
            (folder/'usage.json').write_text(json.dumps({'model':'deepseek-flash',
                'usage':{'prompt_tokens':23,'completion_tokens':6}}), encoding='utf-8')
            rows = load_records(root, (), root/'registry.json')
            unknown = [row for row in rows if '缺少可去重ID' in row['status']]
            self.assertEqual(len(unknown), 1)
            self.assertIsNone(unknown[0]['input'])

    def test_same_response_id_with_different_usage_marks_both_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name, prompt, completion in (('a', 13, 4), ('b', 99, 7)):
                folder = root/name; folder.mkdir()
                (folder/'intent.json').write_text('{}', encoding='utf-8')
                (folder/'response.http-body').write_text(json.dumps({
                    'model':'deepseek-flash', 'id':'same-id',
                    'usage':{'prompt_tokens':prompt,'completion_tokens':completion},
                }), encoding='utf-8')
            rows = load_records(root, (), root/'registry.json')
            conflicts = [row for row in rows if row['status'].startswith('证据冲突 / 同响应ID')]
            self.assertEqual(len(conflicts), 2)
            self.assertTrue(all(row['input'] is None and row['output'] is None for row in conflicts))
            self.assertFalse((root/'registry.json').exists())

    def test_pair_conflict_invalidates_same_id_from_other_source(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); pair = root/'pair'; other = root/'other'
            pair.mkdir(); other.mkdir()
            for folder in (pair, other):
                (folder/'intent.json').write_text('{}', encoding='utf-8')
            (pair/'usage.json').write_text(json.dumps({'model':'deepseek-flash',
                'usage':{'prompt_tokens':13,'completion_tokens':4}}), encoding='utf-8')
            (pair/'response.body').write_text(json.dumps({'model':'deepseek-flash',
                'id':'shared-id','usage':{'prompt_tokens':99,'completion_tokens':7}}), encoding='utf-8')
            (other/'response.body').write_text(json.dumps({'model':'deepseek-flash',
                'id':'shared-id','usage':{'prompt_tokens':99,'completion_tokens':7}}), encoding='utf-8')
            rows = load_records(root,
                [('pair-conflict', 'pair', 'usage.json'), ('same-id-copy', 'other', 'response.body')],
                root/'registry.json')
            indexed = {row['label']: row for row in rows}
            for label in ('pair-conflict', 'same-id-copy'):
                self.assertEqual(indexed[label]['status'],
                                 '证据冲突 / 同响应ID用量不一致，未计入')
                self.assertIsNone(indexed[label]['input'])
                self.assertIsNone(indexed[label]['output'])

    def test_failed_response_without_usage_is_visible_unknown_and_body_stays_hidden(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); folder = root/'failed'; folder.mkdir()
            (folder/'intent.json').write_text('{}', encoding='utf-8')
            (folder/'response.http-body').write_text(json.dumps({
                'error':{'message':'PRIVATE FAILURE BODY'}, 'status':503,
            }), encoding='utf-8')
            rows = load_records(root, (), root/'registry.json')
            failures = [row for row in rows if row['status'] == '未知 / 失败回执未提供用量，未计入']
            self.assertEqual(len(failures), 1)
            self.assertIsNone(failures[0]['input'])
            self.assertNotIn('PRIVATE FAILURE BODY', repr(rows))

    def test_failed_http_response_with_valid_usage_counts_tokens_but_not_success(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); folder = root/'failed'; folder.mkdir()
            (folder/'intent.json').write_text('{}', encoding='utf-8')
            (folder/'usage.json').write_text(json.dumps({
                'model':'deepseek-flash', 'id':'failed-id', 'status_code':503,
                'error':{'message':'PRIVATE FAILURE BODY'},
                'usage':{'prompt_tokens':900,'completion_tokens':10},
            }), encoding='utf-8')
            rows = load_records(root, (), root/'registry.json')
            failure = next(row for row in rows if row['label'].startswith('自动发现'))
            self.assertEqual((failure['input'], failure['output']), (900, 10))
            self.assertIn('HTTP失败', failure['status'])
            self.assertIn('用量已确认', failure['status'])
            self.assertIsNotNone(estimate(failure, price('2'), price('8')))
            self.assertNotIn('PRIVATE FAILURE BODY', repr(rows))

    def test_failed_http_status_from_bound_meta_keeps_confirmed_usage(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); folder = root/'failed'; folder.mkdir()
            (folder/'intent.json').write_text('{}', encoding='utf-8')
            body = {'model':'deepseek-flash', 'id':'failed-meta-id',
                    'usage':{'prompt_tokens':20,'completion_tokens':4}}
            raw_body = json.dumps(body).encode('utf-8')
            (folder/'response.body').write_bytes(raw_body)
            (folder/'response.meta.json').write_text(json.dumps({
                'http_status':503, 'body_sha256':hashlib.sha256(raw_body).hexdigest(),
            }), encoding='utf-8')
            (folder/'usage.json').write_text(json.dumps({
                'model':'deepseek-flash', 'id':'failed-meta-id', 'usage':body['usage'],
            }), encoding='utf-8')
            rows = load_records(root, (), root/'registry.json')
            known = [row for row in rows if row['input'] == 20 and row['output'] == 4]
            self.assertEqual(len(known), 1)
            self.assertIn('HTTP失败', known[0]['status'])
            self.assertNotIn('PRIVATE FAILURE BODY', repr(rows))
            (folder/'response.meta.json').write_text(json.dumps({
                'http_status':503, 'body_sha256':'0' * 64,
            }), encoding='utf-8')
            unbound = load_records(root, (), root/'registry.json')
            unbound_row = next(row for row in unbound if row['input'] == 20 and row['output'] == 4)
            self.assertNotIn('HTTP失败', unbound_row['status'])

    def test_manual_registration_preserves_failed_status_and_known_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); folder = root/'failed'; folder.mkdir()
            (folder/'intent.json').write_text('{}', encoding='utf-8')
            receipt = folder/'usage.json'
            receipt.write_text(json.dumps({
                'model':'deepseek-flash', 'id':'registered-failed-id', 'status_code':503,
                'error':{'message':'PRIVATE FAILURE BODY'},
                'usage':{'prompt_tokens':44,'completion_tokens':8},
            }), encoding='utf-8')
            registry = root/'registry.json'
            register_evidence(receipt, root, registry, ())
            rows = load_records(root, (), registry)
            known = next(row for row in rows if row['input'] == 44 and row['output'] == 8)
            self.assertIn('HTTP失败', known['status'])
            self.assertNotIn('PRIVATE FAILURE BODY', registry.read_text(encoding='utf-8'))

    def test_incomplete_scan_is_explicit_and_suppresses_partial_auto_totals(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); folder = root/'request'; folder.mkdir()
            (folder/'intent.json').write_text('{}', encoding='utf-8')
            (folder/'response.http-body').write_text(json.dumps({
                'model':'deepseek-flash', 'id':'limited-id',
                'usage':{'prompt_tokens':13,'completion_tokens':4},
            }), encoding='utf-8')
            with patch.object(billing, 'MAX_SCAN_ENTRIES', 1):
                rows = load_records(root, (), root/'registry.json')
            marker = next(row for row in rows if row.get('_scan_incomplete'))
            self.assertIn('扫描不完整', marker['status'])
            self.assertIsNone(marker['input'])
            self.assertFalse(any(row['status'] == '自动收集' for row in rows))

    def test_oversized_receipt_marks_scan_incomplete_without_reading_body(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); folder = root/'request'; folder.mkdir()
            (folder/'intent.json').write_text('{}', encoding='utf-8')
            receipt = folder/'response.http-body'
            receipt.write_text('x' * 64, encoding='utf-8')
            with patch.object(billing, 'MAX_RECEIPT_BYTES', 16):
                original_open = Path.open
                def guarded_open(candidate, *args, **kwargs):
                    if Path(candidate) == receipt:
                        raise AssertionError('oversized receipt was opened')
                    return original_open(candidate, *args, **kwargs)
                with patch.object(Path, 'open', guarded_open):
                    rows = load_records(root, (), root/'registry.json')
            marker = next(row for row in rows if row.get('_scan_incomplete'))
            self.assertIn('single_receipt_size_limit', marker['status'])
            self.assertFalse(any(row['input'] is not None for row in rows))

    def test_registration_refuses_incomplete_history_scan(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); folder = root/'request'; folder.mkdir()
            (folder/'intent.json').write_text('{}', encoding='utf-8')
            selected = folder/'usage.json'
            selected.write_text(json.dumps({'model':'deepseek-flash','id':'new-id',
                'usage':{'prompt_tokens':3,'completion_tokens':1}}), encoding='utf-8')
            registry = root/'registry.json'
            with patch.object(billing, 'MAX_SCAN_ENTRIES', 1):
                with self.assertRaisesRegex(ValueError, '扫描不完整'):
                    register_evidence(selected, root, registry, ())
            self.assertFalse(registry.exists())
            self.assertFalse(registry.with_suffix('.lock').exists())

    def test_reparse_probe_errors_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'receipt.json'
            path.write_text('{}', encoding='utf-8')
            original = Path.lstat
            def fail_for_target(candidate):
                if Path(candidate) == path: raise PermissionError('synthetic stat denial')
                return original(candidate)
            with patch.object(Path, 'lstat', fail_for_target):
                self.assertTrue(_is_reparse_point(path))

    def test_reparse_root_and_child_junctions_are_not_scanned(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); outside = root/'outside'; outside.mkdir()
            external = outside/'request'; external.mkdir()
            (external/'intent.json').write_text('{}', encoding='utf-8')
            (external/'usage.json').write_text(json.dumps({'model':'deepseek-flash',
                'response_id':'external-id','usage':{'prompt_tokens':5,'completion_tokens':1}}), encoding='utf-8')
            root_alias = root/'AudiobookTrials'
            self._junction(outside, root_alias)
            self.assertEqual(list(_saved_receipts(root_alias)), [])
            with self.assertRaises(ValueError):
                register_evidence(external/'usage.json', root_alias,
                                  root_alias/'registry.json', ())

            safe_root = root/'safe'; safe_root.mkdir()
            request = safe_root/'request'; request.mkdir()
            (request/'intent.json').write_text('{}', encoding='utf-8')
            (request/'usage.json').write_text(json.dumps({'model':'deepseek-flash',
                'response_id':'safe-id','usage':{'prompt_tokens':7,'completion_tokens':2}}), encoding='utf-8')
            loop = request/'loop'
            self._junction(request, loop)
            discovered = list(_saved_receipts(safe_root))
            self.assertEqual(len(discovered), 1)
            self.assertEqual(discovered[0].name, 'usage.json')

    def test_receipt_and_registry_file_reparse_points_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); folder = root/'request'; folder.mkdir()
            (folder/'intent.json').write_text('{}', encoding='utf-8')
            receipt = folder/'usage.json'
            receipt.write_text(json.dumps({'model':'deepseek-flash','response_id':'local-id',
                'usage':{'prompt_tokens':3,'completion_tokens':1}}), encoding='utf-8')
            original = _is_reparse_point
            def fake_reparse(path):
                return Path(path) == receipt or original(path)
            with patch('console_voice_billing._is_reparse_point', side_effect=fake_reparse):
                self.assertEqual(list(_saved_receipts(root)), [])
                with self.assertRaises(ValueError):
                    register_evidence(receipt, root, root/'registry.json', ())
            registry = root/'registry.json'
            registry.write_text('[]', encoding='utf-8')
            with patch('console_voice_billing._is_reparse_point',
                       side_effect=lambda path: Path(path) == registry or original(path)):
                with self.assertRaises(ValueError):
                    imported_records(registry, root)
    def test_bad_prices(self):
        for val in ('NaN','-1','abc','Infinity'):
            with self.assertRaises(ValueError): price(val)
if __name__ == '__main__': unittest.main()
