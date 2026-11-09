import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock
import batch_worker as batch
from render_queue import chapter_digest, RenderQueue

class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name); wd = root/'work'; (wd/'state').mkdir(parents=True)
        self.rows = [{'type':'chapter','id':'c1'}, {'type':'narration','id':'n1','text':'a'},
                     {'type':'chapter','id':'c2'}, {'type':'narration','id':'n2','text':'b'}]
        (wd/'script.huaben.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in self.rows),encoding='utf8')
        (wd/'state/chapter-reviews.json').write_text(json.dumps({c:{'book_id':'b','script_digest':chapter_digest(self.rows,c)} for c in ('c1','c2')}),encoding='utf8')
        (wd/'state/ingest.json').write_text('{}',encoding='utf8')
        (wd/'cast.json').write_text('{"narrator": {}, "characters": {}}',encoding='utf8')
        self.b = types.SimpleNamespace(WD=wd, ROOT=root, BOOK_ID='b', stamp=lambda:'stamp',
            playback_speed=lambda d:float(d['speed']), load=lambda p:json.loads(p.read_text(encoding='utf8')))
        self.lock = mock.patch.object(batch, '_check_lock'); self.lock.start(); self.addCleanup(self.lock.stop)
        self.queue = RenderQueue(wd,'b'); self.calls=[]
        def render(data):
            c=data['chapter']; self.calls.append(c)
            out=wd/(c+'.wav'); fp=batch._config(self.b,data['speed'])
            self.queue.enqueue(c,out,fp); job=self.queue.claim(c); out.write_bytes(b'audio'); self.queue.complete(c,job['lease'])
        self.b.render=render
    def test_stop_at_boundary_and_resume_preserves_complete(self):
        batch.create(self.b,['c1','c2'],1,3)
        render=self.b.render
        def stopped(d):
            render(d); batch.request_stop(self.b)
        self.b.render=stopped
        result=batch.run(self.b,3)
        self.assertEqual(result['status'],'stopped'); self.assertEqual(self.calls,['c1'])
        self.b.render=render
        result=batch.run(self.b,3,resume=True)
        self.assertEqual(result['status'],'complete'); self.assertEqual(self.calls,['c1','c2'])
    def test_unreviewed_rejected(self):
        (self.b.WD/'state/chapter-reviews.json').write_text('{}',encoding='utf8')
        with self.assertRaises(ValueError): batch.create(self.b,['c1'],1,3)
    def test_interrupted_never_infers_success(self):
        batch.create(self.b,['c1'],1,3)
        path,_=batch._paths(self.b); d=batch.status(self.b); d['status']='running'; d['chapters'][0]['status']='running'; batch._save(path,d)
        (self.b.WD/'c1.wav').write_bytes(b'old')
        with self.assertRaises(ValueError): batch.run(self.b,3)
        self.assertEqual(batch.status(self.b)['chapters'][0]['status'],'interrupted')
        self.assertEqual(batch.run(self.b,3,resume=True)['status'],'complete')
        self.assertEqual(self.calls,['c1'])
    def test_config_change_blocks(self):
        batch.create(self.b,['c1'],1,3)
        (self.b.WD/'state/ingest.json').write_text('{"changed":true}',encoding='utf8')
        with self.assertRaises(ValueError): batch.run(self.b,3)
        self.assertEqual(self.calls,[])

if __name__ == '__main__': unittest.main()
