import hashlib,json,tempfile,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import real_book_auto_voice as a

def vector(path):
 path.mkdir(parents=True,exist_ok=True); (path/'speaker-vector.npy').write_bytes(b'fictional')
 return {'directory':str(path),'request':{'stage':'local-test','vector_sha256':hashlib.sha256(b'fictional').hexdigest()}}

def profile(id,name,aliases=[],design='温和沉稳的表演节奏'):
 return {'entity_id':id,'name':name,'evidence':name+'出现','personality':'温和','aliases':aliases,
  'design_text':'为中文有声书设计虚构角色表演声音。声音描述：'+design+' 创作依据（不是人物事实）：画像中有温和线索。 创作选择：温和、沉稳。以下声线与表演细节均为创作选择，不作为人物事实；不模仿真人。',
  'profile_sha256':hashlib.sha256((name+design).encode()).hexdigest(),
  'spec_id':hashlib.sha256((name+design).encode()).hexdigest()[:16],
  'source_evidence':[{'chapter_id':'c001','quote':name+'出现','source_sha256':'a'*64}],
  }

class Tests(unittest.TestCase):
 def test_stability_separation_conflict_and_no_retry(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); n=vector(root/'narrator');n['book_role_key']='test\0n';narrator={'record':n,'console_key':'test\0n'}; calls=[]
   def gen(desc,directory): calls.append(desc);return vector(directory/'asset')
   book='book-'+'a'*20; p=profile('one','甲',[{'name':'小甲','evidence':'甲又称小甲'}])
   first=a.allocate(root/'library',book,[p],narrator,gen)
   self.assertEqual(first['status'],'complete');self.assertEqual(len(calls),1)
   self.assertEqual(a.allocate(root/'library',book,[p],narrator,gen)['status'],'complete');self.assertEqual(len(calls),1)
   conflict=a.allocate(root/'library',book,[p,profile('two','乙',[{'name':'小甲','evidence':'乙叫小甲'}])],narrator,gen)
   self.assertEqual(len(conflict['unresolved']),2);self.assertEqual(len(calls),1)
   self.assertEqual(a.allocate(root/'library','book-'+'b'*20,[p],narrator,gen)['status'],'complete');self.assertEqual(len(calls),2)
   def fail(*args): calls.append('failed');raise RuntimeError('unknown')
   bad=profile('bad','丙');self.assertEqual(a.allocate(root/'library',book,[bad],narrator,fail)['status'],'needs_review')
   self.assertEqual(a.allocate(root/'library',book,[bad],narrator,fail)['status'],'needs_review');self.assertEqual(calls.count('failed'),1)
   Path(first['bindings']['one']['record']['directory'],'speaker-vector.npy').write_bytes(b'changed')
   with self.assertRaises(ValueError):a.allocate(root/'library',book,[p],narrator,gen)
 def test_rejects_unsupported_alias(self):
  with self.assertRaises(ValueError):a._validate_profile(profile('a','甲',[{'name':'甲甲','evidence':''}]))
 def test_prompt_requires_planner_marker_and_rejects_real_person_imitation(self):
  description=profile('e','甲')['design_text']
  self.assertEqual(a.creative_description(description),description)
  with self.assertRaises(ValueError):a.generate_local_fictional('模仿真人',Path('unused'))
 def test_generation_failure_persists_bounded_transfer_diagnostics_without_retry(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);n=vector(root/'narrator');n['book_role_key']='test\0n'
   narrator={'record':n,'console_key':'test\0n'};book='book-'+'9'*20;item=profile('e','甲');calls=[]
   def failed_transfer(description,directory):
    calls.append(1);voice=directory/'voice';voice.mkdir()
    (voice/'remote-completion.json').write_text('{"status":"remote_complete"}',encoding='utf-8')
    raise RuntimeError('SCP failed for generated.wav (exit 1): fixture failure')
   result=a.allocate(root/'auto-voices',book,[item],narrator,failed_transfer)
   row=result['unresolved'][0]
   self.assertEqual('RuntimeError',row['error_type'])
   self.assertIn('SCP failed',row['error_message'])
   self.assertTrue(row['remote_completion_present'])
   failure=Path(row['failure_record']);self.assertTrue(failure.is_file())
   saved=json.loads(failure.read_bytes())
   self.assertEqual(row['remote_completion_marker'],saved['remote_completion_marker'])
   self.assertTrue((failure.parent/'generation.reserved.json').is_file())
   retry=a.allocate(root/'auto-voices',book,[item],narrator,failed_transfer)
   self.assertEqual(1,len(calls))
   self.assertEqual(row['remote_completion_marker'],retry['unresolved'][0]['remote_completion_marker'])
 def test_missing_source_bound_design_is_rejected_before_generation(self):
  p=profile('e','甲');p.pop('design_text')
  with self.assertRaisesRegex(ValueError,'source-bound'):
   a._validate_profile(p)
 def test_profile_change_creates_new_candidate_and_preserves_old(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);n=vector(root/'n');n['book_role_key']='n';narr={'record':n,'console_key':'n'}
   book='book-'+'f'*20;calls=[]
   def gen(desc,directory):calls.append((desc,directory));return vector(directory/'asset')
   p=profile('same','甲');a.allocate(root/'voices',book,[p],narr,gen)
   old=Path(root/'voices'/book/'same'/'candidates'/p['spec_id']/'voice.json').read_bytes()
   newer=profile('same','甲',design='外向活跃的表演节奏')
   stable=a.allocate(root/'voices',book,[newer],narr,gen)
   self.assertEqual(Path(root/'voices'/book/'same'/'candidates'/p['spec_id']/'voice.json').read_bytes(),old)
   self.assertEqual(stable['bindings']['same']['record']['directory'],str(calls[0][1]/'asset'))
   self.assertEqual(stable['selection_status']['same'],'default_recommendation')
   self.assertFalse((root/'voices'/book/'same'/'candidates'/newer['spec_id']/'voice.json').exists())
   count=len(calls);a.allocate(root/'voices',book,[newer],narr,gen);self.assertEqual(len(calls),count)
 def test_explicit_selection_overrides_recommendation_and_is_book_scoped(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);n=vector(root/'n');n['book_role_key']='n';narr={'record':n,'console_key':'n'}
   book='book-'+'e'*20;p=profile('e','甲');calls=[]
   def gen(desc,directory):calls.append(1);return vector(directory/('asset'+str(len(calls))))
   first=a.allocate(root/'voices',book,[p],narr,gen)['bindings']['e']
   other_book='book-'+'d'*20
   other=a.allocate(root/'voices',other_book,[p],narr,gen)['bindings']['e']
   other_book_binding=other
   other_record=dict(other['record'],book_role_key=book+'\0e');other={'console_key':book+'\0e','record':other_record}
   a.select_voice(root/'voices',book,'e',other,basis='用户在工作台明确更换',design='voice design',spec_id='b'*16)
   changed=a.allocate(root/'voices',book,[profile('e','甲',design='画像更新后的设计')],narr,gen)
   self.assertEqual(changed['bindings']['e'],other)
   self.assertNotEqual(first,other)
   selected=a._read_selections(root/'voices',book)['voices']['e']
   self.assertEqual(selected['status'],'explicitly_selected')
   self.assertEqual(a._read_selections(root/'voices',other_book)['voices']['e']['binding'],other_book_binding)
 def test_corrupt_selected_vector_is_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);book='book-'+'c'*20;n=vector(root/'n');n['book_role_key']=book+'\0e';binding={'record':n,'console_key':book+'\0e'}
   a.select_voice(root,book,'e',binding,basis='test')
   self.assertIsNone(a.get_selected_voice(root,book,'missing'))
   Path(n['directory'],'speaker-vector.npy').write_bytes(b'corrupt')
   with self.assertRaises(ValueError):a._read_selections(root,book)
   repaired=vector(root/'repair');repaired['book_role_key']=book+'\0e'
   replacement={'record':repaired,'console_key':book+'\0e'}
   a.select_voice(root,book,'e',replacement,basis='repair the corrupted selected vector')
   self.assertEqual(a.get_selected_voice(root,book,'e'),replacement)
   self.assertIsNone(a.get_selected_voice(root,book,'missing'))
 def test_concurrent_selection_updates_do_not_drop_entities(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);book='book-'+'b'*20
   def choose(i):
    key=book+'\0e'+str(i);rec=vector(root/('v'+str(i)));rec['book_role_key']=key
    a.select_voice(root,book,'e'+str(i),{'console_key':key,'record':rec},basis='parallel fixture')
   with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(choose,range(16)))
   doc=a._read_selections(root,book)
   self.assertEqual(set(doc['voices']),{'e'+str(i) for i in range(16)})
   self.assertTrue((root/book/'selected-voices.lock').exists())
if __name__=='__main__':unittest.main()
