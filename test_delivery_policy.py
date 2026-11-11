import unittest
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import delivery_policy as policy
import real_pipeline_analysis as analysis


class DeliveryPolicyTests(unittest.TestCase):
    @dataclass(frozen=True)
    class Unit:
        kind: str
        text: str
        voice: str
        instruct: str | None
        block_ids: list[str]
        chapter: int
        after: str = 'turn'
        pause_override: float | None = None
        reference: str | None = None

        @property
        def page_text(self):
            return self.reference if self.reference is not None else self.text

    @dataclass(frozen=True)
    class Plan:
        units: list
        chapters: list

    def test_terminal_cadence_only_marks_eligible_narration_units(self):
        U=self.Unit
        units=[
            U('body','旁白完整结束。”  ','narrator',None,['ok'],0),
            U('body','人物回答。','character',None,['answer'],0),
            U('body','同一旁白结束。','narrator',None,['same'],0),
            U('body','仍是旁白','narrator',None,['same2'],0),
            U('body','人物说完。','character',None,['reverse'],0),
            U('body','随后旁白。','narrator',None,['narrator2'],0),
            U('body','问题？','narrator',None,['question'],0),
            U('body','人物答。','character',None,['answer2'],0),
            U('body','后面有引出冒号：','narrator',None,['colon'],0),
            U('body','人物接话。','character',None,['answer3'],0),
            U('body','章节末句。','narrator',None,['chapter-end'],0),
            U('body','下一章人物。','character',None,['next-chapter'],1),
            U('heading','旁白标题。','narrator',None,['heading'],0),
            U('body','人物继续。','character',None,['answer4'],0),
            U('body','人物陈述。','character',None,['not-narrator'],0),
            U('body','另一角色。','other-character',None,['answer5'],0),
            U('body','用户已经手动设定。','narrator','用户自己的朗读方式',['explicit'],0),
            U('body','人物回应。','character',None,['answer6'],0),
            U('body','数字句3.14.」','narrator','平静可信提示',['numeric'],0),
            U('body','人物答复。','character',None,['answer7'],0),
        ]
        plan=self.Plan(units,[])
        changed,audit=policy.apply_terminal_cadence(plan,'narrator',explicit_block_ids={'explicit'})
        self.assertIsNot(changed,plan)
        self.assertEqual(units,plan.units)
        eligible={'ok','numeric'}
        self.assertEqual(eligible,{ident for row in audit for ident in row['block_ids']})
        self.assertTrue(all(row['instruction'].startswith('自然平稳的中文旁白。') for row in audit))
        numeric=next(row for row in audit if 'numeric' in row['block_ids'])
        self.assertIn('平静可信提示',numeric['instruction'])
        self.assertEqual('用户自己的朗读方式',changed.units[16].instruct)
        self.assertEqual([u.pause_override for u in units],[u.pause_override for u in changed.units])
        self.assertEqual([u.text for u in units],[u.text for u in changed.units])
        self.assertEqual([u.voice for u in units],[u.voice for u in changed.units])
        self.assertTrue(all(set(row)=={'unit_index','chapter_index','block_ids','instruction'} for row in audit))
        self.assertNotIn('旁白完整结束',json.dumps(audit,ensure_ascii=False))

    def test_terminal_cadence_is_idempotent_and_legacy_none_is_supported(self):
        plan=self.Plan([self.Unit('body','正文。','narrator',None,['legacy'],0),
                        self.Unit('body','对话。','character',None,['next'],0)],[])
        once,first=policy.apply_terminal_cadence(plan,'narrator')
        twice,second=policy.apply_terminal_cadence(once,'narrator')
        self.assertEqual(1,len(first));self.assertEqual(1,len(second))
        self.assertEqual(once.units[0].instruct,twice.units[0].instruct)

    def test_neutral_adds_no_hint(self):
        self.assertIsNone(policy.instruction_for('neutral'))

    def test_closed_delivery_maps_to_bounded_chinese_hints(self):
        values = ('neutral', 'calm', 'tense', 'happy', 'angry', 'sad', 'afraid')
        for value in values:
            with self.subTest(value=value):
                result = policy.instruction_for(value)
                if value == 'neutral':
                    self.assertIsNone(result)
                else:
                    self.assertIsInstance(result, str)
                    self.assertLessEqual(len(result), 100)
                    self.assertTrue(all(ch.isprintable() for ch in result))

    def test_invalid_delivery_rejected(self):
        for value in ('', 'excited', None, [], 'sad\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.instruction_for(value)

    def test_terminal_and_override_precedence(self):
        self.assertIn('最后一个字的发音完整、清晰', policy.instruction_for(terminal_narration=True))
        self.assertEqual('用户明确指令', policy.instruction_for('sad', '用户明确指令', terminal_narration=True))
        for value in ('', 'x' * 501, '非法\n指令'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.instruction_for(override=value)

    def test_pipeline_emits_closed_delivery_and_only_explicit_user_instruct(self):
        text='旁白。 “你怕吗？”'
        rows=[{'id':'n1','type':'narration','text':'旁白。','src':[0,3]},
              {'id':'d1','type':'dialogue','text':'“你怕吗？”','src':[4,len(text)],'speaker':'小甲'}]
        raw=json.dumps({'text':text,'chapter_id':'c1'},ensure_ascii=False).encode()
        package={'script_rows':rows,'audit':{'chapter_id':'c1','changes':[{'id':'d1','model_speaker':'小甲',
                    'identity_status':'model_attribution_not_confirmed'}], 'delivery':{'d1':'afraid'}},
                 'provenance':{'input_sha256':hashlib.sha256(raw).hexdigest(),'response_sha256':'a'*64,
                               'dialogue_evidence':{'d1':{'kind':'dialogue','speaker':'小甲','evidence':'你怕吗？'}}}}
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'input.json';source.write_bytes(raw)
            chapters,render_audit=analysis.to_render_chapters(source,package,{'小甲':'voice-a'},
                                                               delivery_overrides={'n1':'用户明确段落指令'})
        self.assertEqual('用户明确段落指令',chapters[0]['segments'][0]['instruct'])
        self.assertEqual('afraid',chapters[0]['segments'][1]['delivery'])
        self.assertEqual('model_inference_unreviewed',render_audit['blocks'][1]['delivery_origin'])
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'input.json';source.write_bytes(raw)
            with self.assertRaisesRegex(ValueError,'limited to narration'):
                analysis.to_render_chapters(source,package,{'小甲':'voice-a'},delivery_overrides={'d1':'不要用模型指令'})


if __name__ == '__main__':
    unittest.main()
