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

    def test_native_plan_narration_dialogue_pause_floors_are_pure_and_bounded(self):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Unit:
            kind: str
            text: str
            voice: str
            instruct: str | None
            block_ids: tuple[str, ...]
            chapter: int
            after: str
            pause_override: float | None = None
            reference: str | None = None

            @property
            def page_text(self):
                return self.reference if self.reference is not None else self.text

        @dataclass(frozen=True)
        class Chapter:
            index: int

        @dataclass(frozen=True)
        class Plan:
            units: list[Unit]
            chapters: list[Chapter]

        units = [
            Unit('body', '他说完了。”  ', 'narrator-ref', None, ('a',), 0, 'turn'),
            Unit('body', '人物接话。', 'character-ref', 'warm', ('b',), 0, 'sentence'),
            Unit('body', '他低声说道：“', 'narrator-ref', None, ('c',), 0, 'sentence'),
            Unit('body', '“明白。”', 'character-ref', 'warm', ('d',), 0, 'paragraph'),
            Unit('body', '旁白继续。', 'narrator-ref', None, ('e',), 0, 'paragraph'),
            Unit('body', '人物回答。', 'character-ref', 'warm', ('f',), 0, 'turn'),
            Unit('body', '他补充说道：”  ', 'narrator-ref', None, ('g',), 0, 'sentence'),
            Unit('body', '另一个声音。', 'character-ref', 'cool', ('h',), 0, 'sentence'),
            Unit('body', '对话继续。', 'character-ref', 'cool', ('i',), 0, 'turn'),
            Unit('body', '新段旁白。', 'narrator-ref', None, ('j',), 0, 'paragraph'),
            Unit('body', '保留长停顿。', 'narrator-ref', None, ('k',), 0, 'long'),
            Unit('body', '角色发言。', 'character-ref', 'warm', ('l',), 0, 'turn'),
            Unit('body', '显式零停顿。', 'narrator-ref', None, ('m',), 0, 'turn', 0.0),
            Unit('body', '不能覆盖。', 'character-ref', 'warm', ('n',), 0, 'end'),
            Unit('heading', '小标题', 'narrator-ref', None, ('o',), 0, 'turn'),
            Unit('body', '章节边界', 'character-ref', 'warm', ('p',), 1, 'turn'),
            Unit('body', '同一角色', 'character-ref', 'warm', ('q',), 1, 'turn'),
        ]
        plan = Plan(units, [Chapter(0), Chapter(1)])
        original_units = tuple(plan.units)
        original_content = [(u.text, u.voice, u.instruct, u.block_ids, u.chapter) for u in units]
        changed, rows = np.adjust_narration_dialogue_pauses(
            plan, 'narrator-ref', {'turn': 0.25, 'sentence': 0.18,
                                   'paragraph': 0.35, 'long': 0.90, 'end': 0.5})

        self.assertIsNot(changed, plan)
        self.assertEqual(original_units, tuple(plan.units))
        self.assertEqual([0.50, 0.50, 0.35, 0.50, 0.50, 0.50, 0.35, None,
                          0.50, None, None, 0.50, 0.0, None, None, None, None],
                         [u.pause_override for u in changed.units])
        self.assertEqual(original_content,
                         [(u.text, u.voice, u.instruct, u.block_ids, u.chapter) for u in changed.units])
        self.assertEqual([
            {'unit_index': 0, 'chapter_index': 0, 'before_seconds': 0.25,
             'after_seconds': 0.50, 'type': 'full_handoff', 'direction': 'narration_to_dialogue'},
            {'unit_index': 1, 'chapter_index': 0, 'before_seconds': 0.18,
             'after_seconds': 0.50, 'type': 'full_handoff', 'direction': 'dialogue_to_narration'},
            {'unit_index': 2, 'chapter_index': 0, 'before_seconds': 0.18,
             'after_seconds': 0.35, 'type': 'continuation_cue', 'direction': 'narration_to_dialogue'},
            {'unit_index': 3, 'chapter_index': 0, 'before_seconds': 0.35,
             'after_seconds': 0.50, 'type': 'full_handoff', 'direction': 'dialogue_to_narration'},
            {'unit_index': 4, 'chapter_index': 0, 'before_seconds': 0.35,
             'after_seconds': 0.50, 'type': 'full_handoff', 'direction': 'narration_to_dialogue'},
            {'unit_index': 5, 'chapter_index': 0, 'before_seconds': 0.25,
             'after_seconds': 0.50, 'type': 'full_handoff', 'direction': 'dialogue_to_narration'},
            {'unit_index': 6, 'chapter_index': 0, 'before_seconds': 0.18,
             'after_seconds': 0.35, 'type': 'continuation_cue', 'direction': 'narration_to_dialogue'},
            {'unit_index': 8, 'chapter_index': 0, 'before_seconds': 0.25,
             'after_seconds': 0.50, 'type': 'full_handoff', 'direction': 'dialogue_to_narration'},
            {'unit_index': 11, 'chapter_index': 0, 'before_seconds': 0.25,
             'after_seconds': 0.50, 'type': 'full_handoff', 'direction': 'dialogue_to_narration'},
        ], rows)
        self.assertTrue(all(not ({'text', 'voice', 'instruct'} & row.keys()) for row in rows))

    def test_native_plan_keeps_existing_long_pause_and_recognizes_fullwidth_cue(self):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Unit:
            kind: str
            text: str
            voice: str
            instruct: str | None
            block_ids: tuple[str, ...]
            chapter: int
            after: str
            pause_override: float | None = None
            reference: str | None = None

            @property
            def page_text(self):
                return self.reference if self.reference is not None else self.text

        @dataclass(frozen=True)
        class Plan:
            units: list[Unit]
            chapters: list[object]

        plan = Plan([
            Unit('body', '引用里，」  ', 'narrator-ref', None, ('a',), 0, 'sentence'),
            Unit('body', '对话。', 'voice-2', None, ('b',), 0, 'turn'),
            Unit('body', '较长停顿。', 'narrator-ref', None, ('c',), 0, 'long'),
            Unit('body', '新对话。', 'voice-2', None, ('d',), 0, 'turn'),
        ], [])
        changed, rows = np.adjust_narration_dialogue_pauses(
            plan, 'narrator-ref', {'sentence': 0.18, 'turn': 0.25, 'long': 0.9})
        self.assertEqual([0.35, 0.50, None, None], [u.pause_override for u in changed.units])
        self.assertEqual([0.35, 0.50], [row['after_seconds'] for row in rows])

    def test_native_plan_does_not_stack_cue_over_paragraph_pause(self):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Unit:
            kind: str
            text: str
            voice: str
            instruct: str | None
            block_ids: tuple[str, ...]
            chapter: int
            after: str
            pause_override: float | None = None
            reference: str | None = None

            @property
            def page_text(self):
                return self.reference if self.reference is not None else self.text

        @dataclass(frozen=True)
        class Plan:
            units: list[Unit]
            chapters: list[object]

        plan = Plan([
            Unit('body', '他说，', 'narrator-ref', None, ('a',), 0, 'paragraph'),
            Unit('body', '“是。”', 'dialogue-ref', None, ('b',), 0, 'turn'),
        ], [])
        changed, rows = np.adjust_narration_dialogue_pauses(
            plan, 'narrator-ref', {'paragraph': 0.35, 'turn': 0.25})
        self.assertIsNot(changed, plan)
        self.assertIsNone(changed.units[0].pause_override)
        self.assertEqual([], rows)
if __name__ == '__main__':
    unittest.main()







