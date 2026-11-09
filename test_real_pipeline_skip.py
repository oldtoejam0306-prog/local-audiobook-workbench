import hashlib,json,tempfile,unittest
from pathlib import Path
import real_pipeline_analysis as a

class SkipTest(unittest.TestCase):
 def sample(self,text,rows):
  tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); path=Path(tmp.name)/'input.json'
  path.write_text(json.dumps({'chapter_id':'c001','text':text},ensure_ascii=False),encoding='utf8')
  pkg={'script_rows':rows,'audit':{'chapter_id':'c001','changes':[]},'provenance':{'input_sha256':a.digest(path.read_bytes()),'response_sha256':'a'*64,'dialogue_evidence':{}}}
  return path,pkg
 def test_punctuation_is_reversible(self):
  p,pkg=self.sample('开头\n……\n结尾',[{'id':'c001','type':'chapter','src':[0,2],'text':'开头'},{'id':'s1','type':'skip','src':[3,5],'text':'……'},{'id':'p1','type':'narration','src':[6,8],'text':'结尾'}])
  chapters,audit=a.to_render_chapters(p,pkg,{})
  self.assertEqual(''.join(s['text'] for s in chapters[0]['segments']),'开头\n……\n结尾')
  self.assertEqual(audit['blocks'][1]['reason'],'punctuation_merged')
  self.assertEqual(audit['blocks'][1]['text_sha256'],hashlib.sha256('……'.encode()).hexdigest())
 def test_nonpunctuation_rejected(self):
  p,pkg=self.sample('标题\n内容',[{'id':'c001','type':'chapter','src':[0,2],'text':'标题'},{'id':'s1','type':'skip','src':[3,5],'text':'内容'}])
  with self.assertRaisesRegex(ValueError,'skip contains'): a.to_render_chapters(p,pkg,{})
if __name__=='__main__': unittest.main()
