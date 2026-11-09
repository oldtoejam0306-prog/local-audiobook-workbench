import copy
import hashlib
import unittest

import local_txt_parser as parser
import huashuo_seed_builder


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class LocalTxtParserTests(unittest.TestCase):
    def test_utf8_crlf_cr_and_unicode_normalize_to_lf_with_separate_hashes(self):
        raw = '第一章\r\n春风吹过。\r第二章\n雨停了。\r\n'.encode('utf-8')
        parsed = parser.parse_local_txt_bytes(raw)
        expected = '第一章\n春风吹过。\n第二章\n雨停了。\n'
        self.assertEqual(parsed['normalized_text'], expected)
        self.assertEqual(parsed['source_sha256'], sha(raw))
        self.assertEqual(parsed['text_sha256'], sha(expected.encode('utf-8')))
        self.assertNotEqual(parsed['source_sha256'], parsed['text_sha256'])
        self.assertEqual(parsed['parser_evidence'], {
            'version': 1, 'parser_id': parser.PARSER_ID, 'parser_version': parser.PARSER_VERSION,
            'import_mode': 'txt_decode', 'normalization': parser.NORMALIZATION_ID,
            'source_sha256': parsed['source_sha256'], 'text_sha256': parsed['text_sha256'],
        })

    def test_utf8_bom_is_removed_and_reparse_verification_succeeds(self):
        raw = b'\xef\xbb\xbf' + '第一章\r\n雨后。'.encode('utf-8')
        parsed = parser.parse_local_txt_bytes(raw)
        self.assertEqual(parsed['normalized_text'], '第一章\n雨后。')
        reparsed = parser.reparse_and_verify(
            raw, normalized_text=parsed['normalized_text'], parser_evidence=parsed['parser_evidence'])
        self.assertEqual(reparsed, parsed)

    def test_parser_output_evidence_is_consumable_by_seed_builder(self):
        raw = '第一章\r\n旁白。\r\n'.encode('utf-8')
        parsed = parser.parse_local_txt_bytes(raw)
        text = parsed['normalized_text']
        rows = [
            {'type': 'huaben', 'version': 1, 'title': 'synthetic', 'language': 'zh',
             'text': 'text.txt', 'text_sha256': parsed['text_sha256']},
            {'type': 'chapter', 'id': 'c001', 'text': '第一章', 'src': [0, 3]},
            {'type': 'narration', 'id': 'n001', 'text': '旁白。', 'src': [4, 7]},
        ]
        cast = {'version': 1, 'narrator': {'voice': 'preset:Vivian'}, 'characters': {}}
        files = huashuo_seed_builder.build_seed_files(
            source_bytes=raw, expected_source_sha256=parsed['source_sha256'],
            normalized_text=text, expected_text_sha256=parsed['text_sha256'],
            parser_evidence=parsed['parser_evidence'], script_rows=rows, cast=cast)
        self.assertEqual(set(files), {'text.txt', 'script.huaben.jsonl', 'cast.json'})
        self.assertEqual(files['text.txt'], text.encode('utf-8'))

    def test_verifier_reparses_raw_bytes_instead_of_trusting_self_reported_hashes(self):
        raw = '第一章\r\n原始正文。\n'.encode('utf-8')
        parsed = parser.parse_local_txt_bytes(raw)
        with self.assertRaisesRegex(ValueError, 'fresh parse'):
            parser.reparse_and_verify(raw, normalized_text='第一章\n伪造正文。\n',
                                      parser_evidence=parsed['parser_evidence'])
        false_evidence = copy.deepcopy(parsed['parser_evidence'])
        false_text = '第一章\n伪造正文。\n'
        false_evidence['text_sha256'] = sha(false_text.encode('utf-8'))
        with self.assertRaisesRegex(ValueError, 'fresh parse'):
            parser.reparse_and_verify(raw, normalized_text=false_text, parser_evidence=false_evidence)

    def test_verifier_rejects_tampered_parser_identity_or_source_hash(self):
        raw = b'hello\r\nworld\n'
        parsed = parser.parse_local_txt_bytes(raw)
        for field, value in (('parser_id', 'caller-claimed-parser'), ('source_sha256', '0' * 64),
                             ('text_sha256', '1' * 64)):
            evidence = copy.deepcopy(parsed['parser_evidence'])
            evidence[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'fresh parse'):
                parser.reparse_and_verify(raw, normalized_text=parsed['normalized_text'],
                                          parser_evidence=evidence)

    def test_invalid_utf8_empty_text_and_embedded_bom_are_rejected(self):
        for raw in (b'', b'\xff\xfe', b' \r\n ', b'good\xef\xbb\xbfbad'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parser.parse_local_txt_bytes(raw)

    def test_docx_epub_paths_and_path_injection_are_explicitly_rejected(self):
        raw = b'synthetic only'
        for name in ('book.docx', 'book.epub', '../book.txt', 'C:\\books\\book.txt', '/tmp/book.txt'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'only; DOCX and EPUB'):
                parser.parse_local_txt_bytes(raw, source_name=name)


if __name__ == '__main__':
    unittest.main()
