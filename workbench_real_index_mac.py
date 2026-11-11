"""One-shot private index bridge; no model calls, no original writes.

Uses the existing trusted registry and workspace/index lock/check workflow.
The prepare response contains private chapter text and must never be logged.
"""
import contextlib,json,sys,re
from pathlib import Path
import release_config
import verified_index_rpc as rpc
import book_workspace_snapshot as workspace
import persisted_chapter_index as persisted
import chapter_attribution_input as adapter
def query(req):
 registry_path=release_config.section('mac').get('index_registry')
 if not isinstance(registry_path,str) or not registry_path:raise ValueError('index registry not configured')
 registry=rpc.load_registry(registry_path)
 if not isinstance(req,dict) or req.get('action') not in ('list','prepare') or req.get('book_id') not in registry:raise ValueError('invalid request')
 if set(req)!=({'action','book_id'} if req['action']=='list' else {'action','book_id','chapter_id'}):raise ValueError('invalid request fields')
 chapter=req.get('chapter_id')
 if chapter is not None and (not isinstance(chapter,str) or not re.fullmatch(r'c\d+(?:\.p\d+(?:\.\d+)?)?',chapter) or len(chapter)>100):raise ValueError('invalid chapter')
 service=rpc.VerifiedIndexRpc(registry);result={}
 def restore(index,target,snapshot,digest):
  lock=index.parent/('.'+index.name+'.lock')
  with workspace._lock_existing(lock):
   before=persisted._read_regular(index/'manifest.json',max_bytes=64*1024*1024)
   m=json.loads(before);entries=m['chapters']
   if not isinstance(entries,list) or not entries or len(entries)>100000:raise ValueError('bad manifest')
   chosen=target if target is not None else entries[0]['chapter_id']
   old=persisted._process_lock;persisted._process_lock=lambda _:contextlib.nullcontext()
   try:record=persisted.restore_chapter(index,chosen,trusted_snapshot=snapshot,expected_index_sha256=digest)
   finally:persisted._process_lock=old
   if persisted._read_regular(index/'manifest.json',max_bytes=64*1024*1024)!=before:raise ValueError('manifest changed')
   if req['action']=='list':
    ids=[x['chapter_id'] for x in entries]
    if not all(isinstance(x,str) and x for x in ids) or len(set(ids))!=len(ids):raise ValueError('invalid chapter list')
    result['chapters']=ids
   else:
    prepared=adapter.prepare(record)
    result.update(payload=prepared['payload'],payload_sha256=prepared['payload_sha256'],chapter_digest=record['chapter_digest'],local_text_sha256=record['local_text_sha256'])
   summary={'chapter_id':record['chapter_id'],'block_count':len(record['script_rows'])-1,'chapter_digest':record['chapter_digest'],'local_text_sha256':record['local_text_sha256'],'index_sha256':record['index_sha256'],'human_reviewed':False}
   if target is None:summary['chapter_count']=len(entries)
   return summary
 original=rpc._restore_summary;rpc._restore_summary=restore
 try:
  q={'action':'status' if chapter is None else 'verify_chapter','book_id':req['book_id']}
  if chapter is not None:q['chapter_id']=chapter
  verified=service.dispatch(q)
 finally:rpc._restore_summary=original
 result.update(book_id=req['book_id'],index_sha256=verified['index_sha256'],human_reviewed=False)
 if chapter is not None: result['chapter_id']=verified['chapter_id']
 return result

def main():
 if hasattr(sys.stdout, 'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
 data=sys.stdin.buffer.read(65537)
 if len(data)>65536:raise ValueError('request too large')
 print(json.dumps({'ok':True,'data':query(json.loads(data))},ensure_ascii=False,separators=(',',':')))
if __name__=='__main__':
 try:main()
 except Exception as e:print(json.dumps({'ok':False,'error':type(e).__name__}));sys.exit(1)
