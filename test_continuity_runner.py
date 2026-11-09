import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if sys.platform == 'win32':
    sys.modules.setdefault('fcntl', types.ModuleType('fcntl'))
import continuity_runner as continuity
import batch_worker as batch
from render_queue import RenderQueue, chapter_digest


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); wd = root / 'work'; (wd / 'state').mkdir(parents=True)
        rows = [{'type': 'chapter', 'id': 'c007.p001'}, {'type': 'narration', 'text': 'hello'},
                {'type': 'chapter', 'id': 'c2'}, {'type': 'narration', 'text': 'again'}]
        (wd / 'script.huaben.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in rows), encoding='utf8')
        reviews = {c: {'book_id':'b', 'script_digest':chapter_digest(rows,c)} for c in ('c007.p001','c2')}
        (wd / 'state/chapter-reviews.json').write_text(json.dumps(reviews), encoding='utf8')
        (wd / 'state/ingest.json').write_text('{}', encoding='utf8')
        (wd / 'cast.json').write_text('{"narrator":{},"characters":{}}', encoding='utf8')
        self.b = types.SimpleNamespace(WD=wd, ROOT=root, BOOK_ID='b', stamp=lambda:'s',
                    playback_speed=lambda d:float(d['speed']), load=lambda p:json.loads(p.read_text(encoding='utf8')))
        self.q = RenderQueue(wd,'b')
        patcher = mock.patch.object(batch, '_check_lock'); patcher.start(); self.addCleanup(patcher.stop)
        def render(data):
            c=data['chapter']; path=wd/('console-output-'+c+'.wav')
            self.q.enqueue(c,path,batch._config(self.b,data['speed']))
            job=self.q.claim(c); path.write_bytes(('audio'+c).encode()); self.q.complete(c,job['lease'])
        self.b.render=render
        batch.create(self.b,['c007.p001','c2'],1,3); batch.run(self.b,3)

    @mock.patch('continuity_runner.subprocess.run')
    def test_manifest_order_and_tampering(self, run):
        result=continuity.playback_manifest(self.b, 'ffmpeg')
        self.assertEqual([x['chapter_id'] for x in result['chapters']],['c007.p001','c2'])
        self.assertEqual(run.call_count,2)
        (self.b.WD/'console-output-c007.p001.wav').write_bytes(b'changed')
        with self.assertRaises(ValueError): continuity.playback_manifest(self.b, 'ffmpeg')

    @mock.patch('continuity_runner.subprocess.run', side_effect=__import__('subprocess').CalledProcessError(1,'ffmpeg'))
    def test_manifest_rejects_decode_failure(self, run):
        with self.assertRaises(__import__('subprocess').CalledProcessError): continuity.playback_manifest(self.b, 'ffmpeg')

    @mock.patch('continuity_runner.subprocess.run')
    def test_manifest_rejects_changed_review(self, run):
        (self.b.WD/'state/chapter-reviews.json').write_text('{}', encoding='utf8')
        with self.assertRaises(ValueError): continuity.playback_manifest(self.b, 'ffmpeg')


if __name__ == '__main__': unittest.main()

