import os,socket,json,hashlib,shutil,time,contextlib,sys
from pathlib import Path
root=Path(__file__).resolve().parent
s=socket.socket();s.settimeout(1)
try:
 try:s.connect(('127.0.0.1',9))
 except PermissionError:pass
 else:raise RuntimeError('network probe failed')
finally:s.close()
try:fd=os.open('/dev/null',os.O_WRONLY)
except PermissionError:pass
else:os.close(fd);raise RuntimeError('outside write probe failed')
from mac_render_resources import configure_mlx_limits
from chapter_model_cache import clone_model_tree, model_tree_inventory
import mlx.core as mx
resource_limits=configure_mlx_limits()
import huashuo.post as post
post.LEAD_IN=0.15
post.PAUSES={**post.PAUSES,"end":0.5,"chapter_end":0.5,"sentence":0.18,"turn":0.25,"paragraph":0.35}
import isolated_huashuo_library as wrapper
import huashuo.library as library
published=json.loads((root/'publish-result.json').read_text());refs=published['voice_refs'];expected={refs[s]:h for s,h in published['vectors'].items()}
stage=wrapper.create_staging_root(root)
def persist(name,obj):
 tmp=stage/(name+'.tmp')
 with tmp.open('w',encoding='utf8') as f:json.dump(obj,f);f.flush();os.fsync(f.fileno())
 os.replace(tmp,stage/name)
 fd=os.open(stage,os.O_RDONLY);os.fsync(fd);os.close(fd)
try:
 shutil.copytree(published['library_dir'],stage/'voices')
 source_model=Path(os.environ['AUDIOBOOK_CUSTOMVOICE_HF_HOME']).resolve(strict=True)
 before=model_tree_inventory(source_model,allowed_file_link_root=source_model)
 clone_model_tree(source_model,stage/'.hf',allowed_file_link_root=source_model)
 after=model_tree_inventory(stage/'.hf')
 if not before or before!=after:raise RuntimeError('local model copy inventory mismatch')
 model_hash=hashlib.sha256(json.dumps(before,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 data=json.loads((root/'request.json').read_text(encoding='utf-8'));segments=data['segments'];text=''.join(x['text'] for x in segments)
 lines=[x['text'] for x in segments];roles=[x['speaker_id'] for x in segments]
 if hashlib.sha256(text.encode()).hexdigest()!=data['source_sha256']:raise RuntimeError('chapter text hash mismatch')
 chapter_prefix=data['chapter_id']+'\n';metadata_text=chapter_prefix+text
 wd=stage/'work';wd.mkdir(exist_ok=True);(stage/'book.txt').write_text(metadata_text,encoding='utf-8');(wd/'text.txt').write_text(metadata_text,encoding='utf-8')
 header={'type':'huaben','version':1,'title':data['chapter_id'],'language':'zh','text':'text.txt','text_sha256':hashlib.sha256(metadata_text.encode()).hexdigest()}
 rows=[header,{'type':'chapter','id':'c001','text':data['chapter_id'],'level':1,'src':[0,len(data['chapter_id'])]}]
 offset=len(chapter_prefix)
 for i,(line,role) in enumerate(zip(lines,roles)):
  rows.append({'type':'dialogue','id':'c001.p'+str(i+1),'text':line,'say':line,'speaker':role,'src':[offset,offset+len(line)]});offset+=len(line)
 (wd/'script.huaben.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows),encoding='utf-8')
 narrator_ref=refs.get('narrator',next(iter(refs.values())))
 (wd/'cast.json').write_text(json.dumps({'narrator':{'voice':narrator_ref},'characters':{r:{'voice':v} for r,v in refs.items()}},ensure_ascii=False),encoding='utf-8')
 import huashuo.synth as synthmod
 import native_unit_cache
 original_synthesize=synthmod.synthesize
 def verified_synthesize(units,engine,workdir,language,*args,**kwargs):
  expected=[(c,refs[role]) for seg,role in zip(lines,roles) for c in seg if not c.isspace()]
  actual=[(c,u.voice) for u in units for c in u.page_text if not c.isspace()]
  if expected!=actual:raise RuntimeError('planned page_text/voice mapping differs from source')
  mapping_sha=hashlib.sha256(json.dumps(actual,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
  proof={'status':'verified','source_sha256':data['source_sha256'],'nonwhitespace_characters':len(expected),'units':len(units),'page_text_voice_match':True,'page_text_voice_sha256':mapping_sha}
  (root/'coverage-proof.json').write_text(json.dumps(proof,ensure_ascii=False,sort_keys=True,separators=(',',':')),encoding='utf-8')
  settings=json.loads((root/'cache-settings.json').read_text())
  binding={'stage':str(stage),'model_sha256':model_hash,'adapter_sha256':settings['adapter_sha256']}
  (root/'cache-binding.json').write_text(json.dumps(binding),encoding='utf-8')
  keys=synthmod.unit_keys(units,engine,language)
  expected_cache={k:{'text':u.text,'voice':u.voice,'instruct':u.instruct,'engine':engine.identity()} for k,u in zip(keys,units)}
  reuse=native_unit_cache.restore_units(settings['root'],workdir.units,expected_cache,model_sha256=model_hash,adapter_sha256=settings['adapter_sha256'])
  (root/'cache-reuse.json').write_text(json.dumps(reuse),encoding='utf-8')
  statistics=original_synthesize(units,engine,workdir,language,*args,**kwargs)
  (root/'synthesis-statistics.json').write_text(json.dumps(vars(statistics)),encoding='utf-8')
  return statistics
 synthmod.synthesize=verified_synthesize
 from natural_pacing import adjust_narration_dialogue_pauses
 original_layout=post.layout
 def handoff_layout(plan,keys,workdir,target_lufs=post.TARGET_LUFS,pauses=None):
  adjusted,changes=(adjust_narration_dialogue_pauses(plan,refs['narrator'],{**post.PAUSES,**(pauses or {})})
                    if 'narrator' in refs else (plan,[]))
  (root/'handoff-pauses.json').write_text(json.dumps({'source_sha256':data['source_sha256'],
   'full_handoff_seconds':0.50,'continuation_cue_seconds':0.35,'changes':changes}),encoding='utf-8')
  return original_layout(adjusted,keys,workdir,target_lufs,pauses)
 post.layout=handoff_layout
 reads=[];orig=library.Voice.vector
 def trace(voice):
  path=voice.vector_path;digest=hashlib.sha256(path.read_bytes()).hexdigest();arr=orig(voice)
  reads.append({'ref':voice.ref,'sha256':digest,'shape':list(arr.shape),'path':str(path)})
  persist('vector-reads.json',reads)
  if voice.ref in expected and digest!=expected[voice.ref]:raise RuntimeError('unexpected candidate vector')
  return arr
 library.Voice.vector=trace
 times={};results={}
 with (stage/'huashuo.log').open('w',encoding='utf8') as log,contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
  for command in ('check','synth','package'):
   persist('phase-state.json',{'phase':command,'status':'running'})
   t=time.monotonic();kwargs={'output':stage/'synthetic-result.mp3'} if command=='package' else {}
   result=wrapper.run_isolated(stage,stage/'voices',stage/'book.txt',wd,command,**kwargs)
   times[command]=round(time.monotonic()-t,3);results[command]=result
   if result not in (None,0):
    persist('phase-state.json',{'phase':command,'status':'failed','return':result});raise RuntimeError(command+' failed')
   cache={}
   if command=='synth':
    for item in sorted((wd/'cache/units').glob('*')):
     if item.is_file():cache[item.name]=hashlib.sha256(item.read_bytes()).hexdigest()
    persist('synth-cache-hashes.json',cache)
   persist('phase-'+command+'.json',{'phase':command,'status':'complete','return':result,'seconds':times[command]})
   persist('phase-state.json',{'phase':command,'status':'complete'})
 if not all(any(row['ref']==ref and row['sha256']==expected[ref] and row['shape']==[2048] for row in reads) for ref in set(refs.values())):raise RuntimeError('candidate vector not actually read')
 audio=stage/'synthetic-result.mp3';digest=hashlib.sha256(audio.read_bytes()).hexdigest()
 out={'vector_reads':reads,'resource_limits':resource_limits,'mlx_peak_allocated_bytes':mx.get_peak_memory(),'mlx_active_allocated_bytes':mx.get_active_memory(),'mlx_cache_bytes':mx.get_cache_memory(),'pacing':{'lead_in_seconds':post.LEAD_IN,'pauses':post.PAUSES},'status':'complete','stage':str(stage),'voice_refs':refs,'vectors':expected,'vector_reads':reads,'audio_sha256':digest,'audio_bytes':audio.stat().st_size,'times':times,'returns':results,'model_inventory_sha256':model_hash,'network':'sandbox_denied','outside_writes':'sandbox_denied','human_listening_reviewed':False}
 (root/'render-result.json').write_text(json.dumps(out));print(json.dumps(out),flush=True)
except BaseException as e:
 (root/'render-unknown.json').write_text(json.dumps({'stage':str(stage),'error':str(e),'no_retry':True}));raise
