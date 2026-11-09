import hashlib,json,subprocess,tempfile,unittest
from pathlib import Path

MODULE=Path(__file__).with_name('chapter_map_worker.mjs')
RUNNER=r"""
import {pathToFileURL} from 'node:url';
const mod=await import(pathToFileURL(process.argv[2]));
const chunks=[];for await(const c of process.stdin)chunks.push(c);
const data=JSON.parse(Buffer.concat(chunks).toString('utf8'));
let calls=0;globalThis.fetch=()=>{calls++;throw Error('network must never be reached');};
try {
 const bytes=Buffer.from(JSON.stringify(data.input));
 if(data.mode==='dispatch'){try{mod.dispatch();}catch(e){console.log(JSON.stringify({error:e.message,calls}));}}
 else {const x=mod.buildRequest(bytes,data.budget);console.log(JSON.stringify({...x,parsed:JSON.parse(x.bodyText),calls}));}
}catch(e){console.log(JSON.stringify({error:e.message,calls}));}
"""
class OfflineChapterMapWorkerTests(unittest.TestCase):
 def setUp(self):self.data={'book_id':'book-123','chapter_id':'c001','chapter_text':'合成章节：林舟走进房间。','source_sha256':'a'*64,'characters':{},'approved_summaries':[]}
 def call(self,mode='draft',data=None,budget=.5):
  p=subprocess.run(['node','--input-type=module','-e',RUNNER,'mock-entry',str(MODULE)],input=json.dumps({'mode':mode,'input':self.data if data is None else data,'budget':budget},ensure_ascii=False),text=True,encoding='utf-8',capture_output=True,timeout=20);self.assertEqual(p.returncode,0,p.stderr);return json.loads(p.stdout)
 def test_builds_chapter_map_request_and_hashes(self):
  x=self.call();body=x['parsed'];self.assertEqual(body['model'],'deepseek-flash');self.assertEqual(body['response_format'],{'type':'json_object'});self.assertIn('chapter-map-v1',body['messages'][1]['content']);self.assertIn('model_inference',body['messages'][1]['content']);self.assertIn('不要计算数字位置',body['messages'][1]['content']);self.assertEqual(x['request_body_sha256'],hashlib.sha256(x['bodyText'].encode()).hexdigest());self.assertEqual(x['dispatch_status'],'blocked_manifest_protocol_not_implemented');self.assertFalse(x['budget_reserved']);self.assertEqual(x['calls'],0)
 def test_dispatch_is_always_blocked_even_with_mock_fetch(self):
  x=self.call('dispatch');self.assertIn('dispatch_disabled',x['error']);self.assertEqual(x['calls'],0)
 def test_invalid_budget_and_unapproved_context_fail_before_dispatch(self):
  for budget in (0,-1,10.1,1e-7):
   with self.subTest(budget=budget):self.assertIn('error',self.call(budget=budget))
  data=dict(self.data,approved_summaries=[{'status':'draft','summary':'x'}]);self.assertEqual(self.call(data=data)['error'],'unapproved_context')
 def test_missing_source_or_oversized_chapter_rejected(self):
  for data in (dict(self.data,source_sha256='wrong'),dict(self.data,chapter_text=''),dict(self.data,chapter_text='字'*18001)):
   with self.subTest(data_length=len(data['chapter_text'])):self.assertIn('error',self.call(data=data))
 def test_cli_only_outputs_hashes_and_never_dispatches(self):
  with tempfile.TemporaryDirectory() as d:
   inp=Path(d)/'export.json';inp.write_text(json.dumps(self.data,ensure_ascii=False),encoding='utf-8');p=subprocess.run(['node',str(MODULE),'--draft',str(inp),'0.5'],text=True,encoding='utf-8',capture_output=True,timeout=20)
   self.assertEqual(p.returncode,0,p.stderr);self.assertNotIn(self.data['chapter_text'],p.stdout);self.assertEqual(json.loads(p.stdout)['dispatch_status'],'blocked_manifest_protocol_not_implemented')
   blocked=subprocess.run(['node',str(MODULE),'--run',str(inp),'0.5'],text=True,encoding='utf-8',capture_output=True,timeout=20);self.assertNotEqual(blocked.returncode,0)
if __name__=='__main__':unittest.main()
