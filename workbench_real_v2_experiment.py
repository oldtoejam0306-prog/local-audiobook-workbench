"""Explicit experimental sequential runner; nothing runs on import.

Single-user coordination required: legacy workers do not honor v2 locks.
Calls the real one-shot dispatch and fixed renderer unless injected for tests.
The operator must supply a verifier based on trusted fixed/temporary voice records.
"""
import hashlib,json
from pathlib import Path
import controlled_book_chapter_v2 as v2
import real_book_sequence as seq
import real_book_sequence_callbacks as callbacks
import real_book_renderer as renderer
import real_book_auto_callbacks as auto_callbacks
import real_book_auto_voice as auto_voice
import real_pipeline_workflow as workflow
import workbench_real_flow as index_bridge
import workbench_real_v2_client as client

def run_experiment(root,legacy_root,book,chapter_ids,voices,role_bindings,verify_voice_records,*,single_user_ack=False,index_request=index_bridge.index_request,dispatch=workflow.dispatch_chapter,render_segment=None,auto_narrator=None,generator=auto_voice.generate_local_fictional,shared_root=None,stop_requested=None):
 if single_user_ack is not True:raise ValueError('single-user coordination required')
 if book not in index_bridge.ui.BOOKS.values() or not callable(verify_voice_records) or verify_voice_records(book,voices,role_bindings) is not True:raise ValueError('untrusted book or voice records')
 if not isinstance(chapter_ids,list) or not chapter_ids or len(set(chapter_ids))!=len(chapter_ids):raise ValueError('invalid chapters')
 listing=index_request('list',book); ids=listing.get('chapters');digest=listing.get('index_sha256')
 if listing.get('book_id')!=book or listing.get('human_reviewed') is not False or not isinstance(ids,list) or not all(isinstance(x,str) for x in ids) or len(set(ids))!=len(ids):raise ValueError('invalid index')
 positions=[ids.index(cid) for cid in chapter_ids] if all(cid in ids for cid in chapter_ids) else []
 if len(positions)!=len(chapter_ids) or positions!=sorted(positions):raise ValueError('chapters not in index order')
 payloads={}; specs=[]
 for cid in chapter_ids:
  item=index_request('prepare',book,cid);payload=item.get('payload')
  if item.get('book_id')!=book or item.get('index_sha256')!=digest or item.get('human_reviewed') is not False or not isinstance(payload,dict) or payload.get('book_id')!=book or payload.get('chapter_id')!=cid or not isinstance(payload.get('text'),str):raise ValueError('index identity changed')
  raw=workflow._bytes(payload)
  if hashlib.sha256(raw).hexdigest()!=item.get('payload_sha256'):raise ValueError('payload hash mismatch')
  identity=client.definition(book,cid,raw,digest,voices)
  if v2.canonical(voices)!=seq._bytes(voices) or identity['voice_mapping_sha256']!=seq.fingerprint(voices):raise ValueError('voice fingerprint mismatch')
  payloads[cid]=json.loads(raw);specs.append({'id':cid,'source_sha256':hashlib.sha256(payload['text'].encode('utf8')).hexdigest()})
  result=v2.Protocol(Path(root)/'gate',legacy_root).reserve(identity,raw,voices)
  if result['status'] not in ('reserved','already_reserved'):return result
 base=Path(root).resolve();plan=seq.create_plan(base/'sequence',book,digest,specs,voices)
 def restore(index,cid,snapshot,expected):
  fresh=index_request('prepare',book,cid);payload=fresh.get('payload')
  if fresh.get('book_id')!=book or fresh.get('human_reviewed') is not False or fresh.get('index_sha256')!=expected or not isinstance(payload,dict) or payload.get('book_id')!=book or payload.get('chapter_id')!=cid or not isinstance(payload.get('text'),str) or (cid in payloads and workflow._bytes(payload)!=workflow._bytes(payloads[cid])) or hashlib.sha256(workflow._bytes(payload)).hexdigest()!=fresh.get('payload_sha256'):raise ValueError('index source changed')
  return json.loads(workflow._bytes(payload))
 def render(stage_path,binding):
  if seq.fingerprint(binding)!=plan['voice_bindings_sha256'] or verify_voice_records(book,binding,role_bindings) is not True:raise ValueError('voice records changed')
  stage=json.loads(Path(stage_path).read_bytes());cid=stage['chapters'][0]['id'];spec=next(c for c in specs if c['id']==cid)
  return renderer.render(stage_path,book,cid,spec['source_sha256'],role_bindings,base/'render-output'/book/cid,render_segment=render_segment)
 def verify(receipt,stage_path,binding):
  if verify_voice_records(book,binding,role_bindings) is not True:return False
  stage=json.loads(Path(stage_path).read_bytes());cid=stage['chapters'][0]['id'];spec=next(c for c in specs if c['id']==cid)
  document,assignments=renderer.prepare(stage_path,book,cid,spec['source_sha256'],role_bindings)
  actual=renderer.verify(Path(receipt['manifest_path']).parent,document,assignments,spec['source_sha256'])
  return actual==receipt
 if auto_narrator is None:
  bridge=callbacks.Bridge(base/'artifacts',None,None,voices,render,verify,dispatch=dispatch,restore=restore)
 else:
  auto_voice._validate_record(auto_narrator['record'])
  if auto_narrator['record'].get('book_role_key')!=auto_narrator['console_key'] or voices.get('auto_narrator_sha256')!=seq.fingerprint(auto_narrator):raise ValueError('narrator identity not pinned')
  def auto_render(path,bindings,chapter):
   if verify_voice_records(book,voices,{'narrator':auto_narrator}) is not True:raise ValueError('narrator changed')
   return renderer.render(path,book,chapter['id'],chapter['source_sha256'],bindings,base/'render-output'/book/chapter['id'],render_segment=render_segment)
  def auto_verify(receipt,path,bindings,chapter):
   if verify_voice_records(book,voices,{'narrator':auto_narrator}) is not True:return False
   doc,assignments=renderer.prepare(path,book,chapter['id'],chapter['source_sha256'],bindings)
   return renderer.verify(Path(receipt['manifest_path']).parent,doc,assignments,chapter['source_sha256'])==receipt
  voice_base=Path(shared_root).resolve() if shared_root is not None else base
  bridge=auto_callbacks.AutoBridge(base/'artifacts',None,None,voices,voice_root=voice_base/'auto-voices',narrator=auto_narrator,auto_renderer=auto_render,auto_verify=auto_verify,generator=generator,dispatch=dispatch,restore=restore)
 stage_callbacks=bridge.callbacks()
 if shared_root is not None:
  import workbench_job_store as store
  shared=Path(shared_root).resolve()
  bridge.correction_root=shared
  def expanded_prior(chapter,prior):
   merged={item['chapter']['id']:item for item in store.prior_memories(shared,book,digest,chapter['id'])}
   for item in prior:
    cid=item['chapter']['id']
    if cid in merged and merged[cid]['chapter']['source_sha256']!=item['chapter']['source_sha256']:
     raise ValueError('shared chapter memory source differs from current plan')
    merged[cid]=item
   return [merged[cid] for cid in sorted(merged,key=workflow._chapter_key)]
  original_analyze,original_memory=stage_callbacks['analyze'],stage_callbacks['memory']
  def shared_analyze(chapter,prior,artifacts):
   return original_analyze(chapter,expanded_prior(chapter,prior),artifacts)
  def shared_memory(chapter,prior,artifacts):
   result=original_memory(chapter,expanded_prior(chapter,prior),artifacts)
   if result.get('status')=='ok':store.publish_memory(shared,book,digest,
     {'id':chapter['id'],'source_sha256':chapter['source_sha256']},result['artifact'])
   return result
  stage_callbacks.update(analyze=shared_analyze,memory=shared_memory)
 return seq.run(base/'sequence',plan,stage_callbacks,stop_requested=stop_requested,
   resumable_render=render_segment is None)

def fixed_record_verifier(assignments_path,book_names,role_names):
 """Build a verifier pinned to the current workbench assignments file.

 book_names maps registered book IDs to the exact console book name; role_names
 maps speaker IDs to exact console role names, explicitly chosen by the operator.
 A correct mapping does not establish model character attribution.
 """
 path=Path(assignments_path)
 def verify(book,voices,bindings):
  try:
   name=book_names[book];db=json.loads(path.read_bytes())
   if not isinstance(db,dict) or not isinstance(bindings,dict) or not bindings:return False
   expected_roles={voices['narrator']['id']} | {v['id'] for v in voices['characters'].values()}
   if set(bindings)!=expected_roles or set(role_names)!=expected_roles:return False
   for role,spec in bindings.items():
    key=name+'\0'+role_names[role];record=spec['record']
    if spec['console_key']!=key or db.get(key)!=record or record.get('book_role_key')!=key:return False
    request=record['request'];vector=Path(record['directory'])/'speaker-vector.npy'
    if not request.get('stage') or hashlib.sha256(vector.read_bytes()).hexdigest()!=request.get('vector_sha256'):return False
   return True
  except (OSError,KeyError,TypeError,ValueError):return False
 return verify

def make_auto_executor(root,legacy_root,narrator,verify_narrator,*,single_user_ack=False,index_request=index_bridge.index_request,dispatch=workflow.dispatch_chapter,render_segment=None,generator=auto_voice.generate_local_fictional):
 """Bind a GUI executor to one explicitly configured, verified narrator."""
 voices={'narrator':{'id':'narrator','voice':'preset:test'},'characters':{},'auto_narrator_sha256':seq.fingerprint(narrator)}
 def execute(identity,payload,selected_voices):
  if selected_voices!=voices:raise ValueError('voice configuration differs')
  fresh=index_request('prepare',identity['book_id'],identity['chapter_id'])
  if fresh.get('index_sha256')!=identity['index_sha256'] or fresh.get('book_id')!=identity['book_id'] or fresh.get('human_reviewed') is not False or fresh.get('payload_sha256')!=identity['source_sha256'] or workflow._bytes(fresh.get('payload'))!=workflow._bytes(payload):raise ValueError('GUI identity no longer current')
  return run_experiment(root,legacy_root,identity['book_id'],[identity['chapter_id']],voices,{'narrator':narrator},verify_narrator,single_user_ack=single_user_ack,index_request=index_request,dispatch=dispatch,render_segment=render_segment,auto_narrator=narrator,generator=generator)
 return voices,execute
