import concurrent.futures, importlib, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
import controlled_real_voice as c

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.old=c.ROOT; c.ROOT=Path(self.tmp.name)
        self.previous=os.environ.get('AUDIOBOOK_CONTROLLED_REAL_ROOT'); os.environ['AUDIOBOOK_CONTROLLED_REAL_ROOT']=self.tmp.name
    def tearDown(self):
        c.ROOT=self.old
        if self.previous is None: os.environ.pop('AUDIOBOOK_CONTROLLED_REAL_ROOT',None)
        else: os.environ['AUDIOBOOK_CONTROLLED_REAL_ROOT']=self.previous
        self.tmp.cleanup()
    def finish(self,job):
        # The worker starts a fresh Python process; allow modest suite load
        # while keeping a bounded deadline for a stuck child.
        for _ in range(300):
            v=c.state(job)
            if v['status'] in ('complete','interrupted','failed') and not (c.ROOT/'active.lock').exists(): return v
            time.sleep(.05)
        self.fail('worker did not finish')
    def test_concurrent_duplicate_and_completion(self):
        req={'mode':'synthetic','text':'自写并发测试'}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(c.start,[req,req]))
        self.assertEqual(sum(x['status']=='reserved' for x in results),1)
        job=c.identity(req); s=self.finish(job);self.assertEqual(s['status'],'complete')
        self.assertEqual(c.digest(Path(s['audio_path']).read_bytes()),s['audio_sha256'])
        self.assertEqual(c.start(req)['status'],'already_reserved')
    def test_two_independent_start_processes(self):
        payload=json.dumps({'action':'start','input':{'mode':'synthetic','text':'独立进程并发'}})
        def launch():
            return json.loads(subprocess.run([sys.executable,str(Path(c.__file__))],input=payload,text=True,encoding='utf8',capture_output=True,check=True).stdout)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _:launch(),range(2)))
        self.assertEqual(sum(x['status']=='reserved' for x in results),1)
        job=c.identity({'mode':'synthetic','text':'独立进程并发'})
        self.assertEqual(self.finish(job)['status'],'complete')
        self.assertEqual(c.start({'mode':'synthetic','text':'独立进程并发'})['status'],'already_reserved')
    def test_stop_and_busy(self):
        req={'mode':'synthetic','text':'自写中断测试'}; r=c.start(req)
        self.assertEqual(c.start({'mode':'synthetic','text':'另一个输入'})['status'],'busy_or_uncertain')
        self.assertEqual(c.stop(r['job'])['status'],'stop_requested')
        self.assertEqual(self.finish(r['job'])['status'],'interrupted')
        self.assertEqual(c.start(req)['status'],'already_reserved')
    def test_worker_exception_persists_failure(self):
        req={'mode':'voice','text':'自写失败测试'}; job=c.identity(req); d=c.jobpath(job);d.mkdir(parents=True); (c.ROOT/'active.lock').mkdir()
        (d/'input.json').write_bytes(c.raw(req))
        original=c.render_voice
        try:
            def fail(*args): raise RuntimeError('synthetic test')
            c.render_voice=fail;c.worker(job)
        finally: c.render_voice=original
        result=c.state(job)
        self.assertEqual(result['status'],'failed'); self.assertEqual(result['error_type'],'RuntimeError'); self.assertTrue((c.ROOT/'active.lock').exists()); self.assertTrue(result['lock_retained'])
        self.assertEqual(c.start(req)['status'],'busy_or_uncertain')
    def test_invalid_and_uncertain_lock(self):
        with self.assertRaises(ValueError): c.start({'mode':'real','text':'x'})
        c.ROOT.mkdir(exist_ok=True); (c.ROOT/'active.lock').mkdir()
        self.assertEqual(c.start({'mode':'synthetic','text':'x'})['status'],'busy_or_uncertain')

if __name__=='__main__': unittest.main()
