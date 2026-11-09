import hashlib,json,tempfile,unittest,wave
from pathlib import Path
import real_book_renderer as r

class RendererTests(unittest.TestCase):
 def test_synthetic_and_tamper(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); vector=root/'speaker-vector.npy';vector.write_bytes(b'fake vector')
   key='book label\0role label'; record={'directory':str(root),'request':{'stage':'private-stage','vector_sha256':r.sha(vector.read_bytes())},'book_role_key':key}
   bindings={'n':{'console_key':key,'record':record}}
   text='甲'*300+'  '+'乙'*300; digest=r.sha(text.encode())
   stage=root/'stage.json';stage.write_text(json.dumps({'chapters':[{'id':'c001','segments':[{'speaker_id':'n','text':text[:300]},{'speaker_id':'n','text':text[300:]}]}], 'audit':{'source_text_sha256':digest}}),encoding='utf8')
   def synth(voice,text,folder):
    p=folder/'rendered.wav'
    with wave.open(str(p),'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\x00\x00'*10)
    return p
   receipt=r.render(stage,'book-'+'a'*20,'c001',digest,bindings,root,render_segment=synth)
   self.assertTrue(Path(receipt['chapter_wav']).exists())
   doc,assign=r.prepare(stage,'book-'+'a'*20,'c001',digest,bindings)
   self.assertEqual(len(doc['chapters'][0]['segments']),2)
   self.assertEqual(''.join(s['text'] for s in doc['chapters'][0]['segments']),text)
   Path(receipt['chapter_wav']).write_bytes(b'bad')
   with self.assertRaises(ValueError):r.verify(Path(receipt['manifest_path']).parent,doc,assign,digest)
   vector.write_bytes(b'changed')
   with self.assertRaises(ValueError):r.prepare(stage,'book-'+'a'*20,'c001',digest,bindings)
 def test_native_default_uses_injected_renderer_and_verifies_evidence(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);vector=root/'speaker-vector.npy';vector.write_bytes(b'fixed-vector')
   book='book-'+'b'*20;text='甲说：“乙你好。”';source_sha=r.sha(text.encode())
   key='book label\0narrator';record={'directory':str(root),'request':{'stage':'private-stage','vector_sha256':r.sha(vector.read_bytes())},'book_role_key':key}
   bindings={'narrator':{'console_key':key,'record':record}}
   stage=root/'stage.json';stage.write_text(json.dumps({'chapters':[{'id':'c002','segments':[{'speaker_id':'narrator','text':text}]}],
    'audit':{'source_text_sha256':source_sha,'audit_tag':'kept'}},ensure_ascii=False),encoding='utf-8')
   def native(normalized_path,bid,cid,source_hash,roles,*,output_dir):
    normalized=json.loads(Path(normalized_path).read_bytes());segments=normalized['chapters'][0]['segments']
    wav=Path(output_dir)/'chapter.wav'
    with wave.open(str(wav),'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\x01\x00'*32)
    voice_hashes={role:spec['record']['request']['vector_sha256'] for role,spec in roles.items()}
    refs={role:'library:zh/candidate_'+str(i) for i,role in enumerate(roles)}
    coverage=[{'speaker_id':role,'vector_sha256':voice_hashes[role],
      'characters':sum(len(s['text']) for s in segments if s['speaker_id']==role)} for role in refs]
    mapping=[(ch,refs[s['speaker_id']]) for s in segments for ch in s['text'] if not ch.isspace()]
    proof={'status':'verified','source_sha256':source_hash,'page_text_voice_match':True,
      'page_text_voice_sha256':r.sha(json.dumps(mapping,ensure_ascii=False,separators=(',',':')).encode()),
      'nonwhitespace_characters':len(mapping),'units':2}
    reads=[{'ref':refs[role],'sha256':voice_hashes[role],'shape':[2048]} for role in refs]
    receipt={'kind':r.NATIVE_KIND,'book_id':bid,'chapter_id':cid,'source_document_sha256':r.sha(Path(normalized_path).read_bytes()),
      'source_sha256':source_hash,'text_sha256':source_hash,'voice_hashes':voice_hashes,'voice_coverage':coverage,
      'wav_path':str(wav.resolve()),'wav_sha256':r.sha(wav.read_bytes()),'voice_refs':refs,
      'coverage_proof':proof,'coverage_proof_sha256':r.sha(r._canonical(proof)),
      'vector_reads':reads,'vector_reads_sha256':r.sha(r._canonical(reads)),
      'runtime_monitor':{'exit_code':0,'reason':None,'samples':2,'minimum_pressure_free_percent':40,'sampled_peak_rss_bytes':1234},
      'monitor_sha256':'a'*64,'adapter_sha256':r._file_sha(Path(r.__file__).with_name('console_chapter_render_mac.py')),'human_reviewed':False}
    return receipt
   receipt=r.render(stage,book,'c002',source_sha,bindings,root/'output',native_renderer=native)
   self.assertEqual(receipt['renderer_kind'],r.NATIVE_KIND)
   manifest=json.loads(Path(receipt['manifest_path']).read_bytes())
   self.assertEqual(manifest['chapters'][0]['segments'],[])
   self.assertEqual(manifest['chapters'][0]['source_segment_count'],1)
   doc,assign=r.prepare(stage,book,'c002',source_sha,bindings)
   self.assertEqual(r.verify(Path(receipt['manifest_path']).parent,doc,assign,source_sha),receipt)
   native_path=Path(receipt['native_receipt_path'])
   native=json.loads(native_path.read_bytes());native['coverage_proof']['page_text_voice_sha256']='0'*64
   r._atomic_json(native_path,native);manifest['native_receipt_sha256']=r.sha(native_path.read_bytes())
   Path(receipt['manifest_path']).write_bytes(r._canonical(manifest))
   with self.assertRaises(ValueError):r.verify(Path(receipt['manifest_path']).parent,doc,assign,source_sha)
   manifest['chapters'][0]['segments']=[{'speaker_id':'narrator'}]
   Path(receipt['manifest_path']).write_bytes(r._canonical(manifest))
   with self.assertRaises(ValueError):r.verify(Path(receipt['manifest_path']).parent,doc,assign,source_sha)
 def test_whitespace_only_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'s.json';text='甲   乙';p.write_text(json.dumps({'chapters':[{'id':'c1','segments':[{'speaker_id':'a','text':'甲'},{'speaker_id':'b','text':'   '},{'speaker_id':'a','text':'乙'}]}],'audit':{'source_text_sha256':r.sha(text.encode())}}),encoding='utf8')
   with self.assertRaises(ValueError):r.prepare(p,'book','c1',r.sha(text.encode()),{})
if __name__=='__main__':unittest.main()
