import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path

import natural_pacing as np
import real_book_renderer as renderer
import isolated_chapter_voice_bridge as bridge


class NaturalPacingTests(unittest.TestCase):
    def test_lossless_quotes_digits_and_natural_boundaries(self):
        text = '他说：“等3分钟，A-10到了。”\n下一段！'
        chunks = np.split_text(text, 12)
        self.assertEqual(''.join(x.text for x in chunks), text)
        self.assertTrue(all(len(x.text) <= 12 for x in chunks))
        self.assertIn('sentence', {x.boundary for x in chunks})

    def test_long_unpunctuated_text_audits_hard_cut(self):
        chunks = np.split_text('字' * 1001)
        self.assertEqual([500, 500, 1], [len(x.text) for x in chunks])
        self.assertEqual(['hard', 'hard', 'final'], [x.boundary for x in chunks])

    def test_quotes_are_attached_decimal_period_is_not_a_boundary(self):
        quoted = np.split_text('abcd。” next words', 7)
        self.assertEqual('abcd。” ', quoted[0].text)
        self.assertNotIn(quoted[0].text[-1], '。！？!?')
        decimal = np.split_text('3.14公里。后文', 4)
        self.assertEqual('3.14公里。后文', ''.join(c.text for c in decimal))
        self.assertEqual('hard', decimal[0].boundary)
        self.assertEqual('3.14', decimal[0].text)

    def test_trailing_quote_cannot_become_a_punctuation_only_chunk(self):
        chunks = np.split_text('甲' * 500 + '”')
        self.assertEqual('甲' * 500 + '”', ''.join(c.text for c in chunks))
        self.assertEqual([499, 2], [len(c.text) for c in chunks])
        self.assertEqual('hard', chunks[0].boundary)
        self.assertTrue(chunks[1].text.startswith('甲'))

    def test_gap_uses_boundary_newline_and_strips_closing_quote(self):
        self.assertEqual((4800, 'sentence'), np.gap_seconds('段落中\n还有。”  ', '下一段', 'a', 'a'))
        self.assertEqual((8400, 'paragraph'), np.gap_seconds('前文\n  ', '  后文', 'a', 'a'))
        self.assertEqual((6000, 'speaker_change'), np.gap_seconds('前文。”  ', '后文', 'a', 'b'))

    def test_pcm_gap_frames_are_written_and_manifested(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vector = root / 'speaker-vector.npy'; vector.write_bytes(b'vector')
            key_a, key_b = 'book\0a', 'book\0b'
            bindings = {}
            for role, key in [('a', key_a), ('b', key_b)]:
                bindings[role] = {'console_key': key, 'record': {
                    'directory': str(root), 'book_role_key': key,
                    'request': {'stage': 'test', 'vector_sha256': hashlib.sha256(b'vector').hexdigest()}}}
            source = '他说：“3号到了。”' + '她回答，收到。'
            source_sha = hashlib.sha256(source.encode()).hexdigest()
            stage = root / 'stage.json'
            stage.write_text(json.dumps({'chapters': [{'id': 'c1', 'segments': [
                {'speaker_id': 'a', 'text': source[:len('他说：“3号到了。”')]},
                {'speaker_id': 'b', 'text': source[len('他说：“3号到了。”'):]}]}],
                'audit': {'source_text_sha256': source_sha}}, ensure_ascii=False), encoding='utf-8')

            def synth(voice, text, folder):
                p = folder / 'rendered.wav'
                with wave.open(str(p), 'wb') as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
                    w.writeframes(b'\x01\x00' * 10)
                return p

            receipt = renderer.render(stage, 'book', 'c1', source_sha, bindings, root, render_segment=synth)
            doc, _ = renderer.prepare(stage, 'book', 'c1', source_sha, bindings)
            gaps = [item['gap_frames'] for item in doc['pacing']]
            self.assertEqual([0, 6000], gaps)
            with wave.open(receipt['chapter_wav'], 'rb') as w:
                self.assertEqual(20 + sum(gaps), w.getnframes())
            manifest = json.loads(Path(receipt['manifest_path']).read_text(encoding='utf-8'))
            self.assertEqual(gaps, [s['gap_frames'] for s in manifest['chapters'][0]['segments']])
            self.assertTrue(renderer.verify(Path(receipt['manifest_path']).parent, doc,
                                            renderer.prepare(stage, 'book', 'c1', source_sha, bindings)[1], source_sha))

    def test_multichapter_first_gaps_are_zero_and_pcm_is_fixed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vector = root / 'speaker-vector.npy'; vector.write_bytes(b'vector')
            key = 'book\0a'
            voice = {'directory': str(root), 'book_role_key': key,
                     'request': {'stage': 'test', 'vector_sha256': hashlib.sha256(b'vector').hexdigest()}}
            doc = {'book': 'book', 'chapters': [
                {'id': 'c1', 'segments': [{'speaker_id': 'a', 'text': '第一章。'}]},
                {'id': 'c2', 'segments': [{'speaker_id': 'a', 'text': '第二章。'}]}],
                'pacing': [{'gap_frames': 0, 'reason': 'start', 'boundary': 'final'},
                           {'gap_frames': 0, 'reason': 'start', 'boundary': 'final'}]}

            def synth(voice, text, folder):
                p = folder / 'rendered.wav'
                with wave.open(str(p), 'wb') as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
                    w.writeframes(b'\x01\x00' * 10)
                return p

            out, manifest = bridge.render_document(doc, {'book\0a': voice}, root, synth)
            self.assertEqual('complete', manifest['status'])
            for index in range(2):
                with wave.open(str(out / ('chapter-%04d' % index) / 'chapter.wav'), 'rb') as w:
                    self.assertEqual((1, 2, 24000), (w.getnchannels(), w.getsampwidth(), w.getframerate()))
                    self.assertEqual(10, w.getnframes())

            invalid = json.loads(json.dumps(doc))
            invalid['pacing'][1]['gap_frames'] = 240
            with self.assertRaisesRegex(ValueError, 'each chapter'):
                bridge.render_document(invalid, {'book\0a': voice}, root, synth)


if __name__ == '__main__':
    unittest.main()
