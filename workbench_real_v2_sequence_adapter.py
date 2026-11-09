"""Single chapter v2-to-sequence adapter. External dependencies must be supplied.

No default dispatcher, asset verifier, or renderer; cannot invoke a real service by
constructing this module. Operator voice JSON is not proof of valid voice assets.
"""
import hashlib,json
from pathlib import Path
import controlled_book_chapter_v2 as v2
import real_book_sequence as seq
import real_book_sequence_callbacks as callbacks
import real_pipeline_workflow as workflow

def run_one(root,identity,payload,voices,*,legacy_root,restore,dispatch,renderer,verify_renderer,verify_voice_assets):
 job=v2.validate(identity)
 if not all(callable(x) for x in (restore,dispatch,renderer,verify_renderer,verify_voice_assets)):raise ValueError('trusted adapters required')
 if not isinstance(payload,dict) or payload.get('book_id')!=identity['book_id'] or payload.get('chapter_id')!=identity['chapter_id'] or not isinstance(payload.get('text'),str):raise ValueError('source identity mismatch')
 if hashlib.sha256(workflow._bytes(payload)).hexdigest()!=identity['source_sha256']:raise ValueError('payload changed')
 # Both implementations use the same UTF-8 canonical JSON; verify, do not assume.
 if v2.canonical(voices)!=seq._bytes(voices) or hashlib.sha256(v2.canonical(voices)).hexdigest()!=identity['voice_mapping_sha256'] or seq.fingerprint(voices)!=identity['voice_mapping_sha256']:raise ValueError('voice identity mismatch')
 if verify_voice_assets(voices) is not True:raise ValueError('voice assets unverified')
 reservation=v2.Protocol(Path(root)/'gate',legacy_root).reserve(identity,workflow._bytes(payload),voices)
 if reservation['status'] not in ('reserved','already_reserved'):return reservation
 source_hash=hashlib.sha256(payload['text'].encode('utf8')).hexdigest()
 # Dedicated plan directory prevents one chapter plan overwriting another.
 base=Path(root).resolve()/identity['book_id']/identity['chapter_id']/job
 def pinned_restore(index,chapter,snapshot,digest):
  fresh=restore(index,chapter,snapshot,digest)
  if workflow._bytes(fresh)!=workflow._bytes(payload):raise ValueError('verified source changed')
  return json.loads(workflow._bytes(fresh))
 def checked_renderer(path,binding):
  if verify_voice_assets(binding) is not True:raise ValueError('voice assets changed')
  return renderer(path,binding)
 plan=seq.create_plan(base/'sequence',identity['book_id'],identity['index_sha256'],[{'id':identity['chapter_id'],'source_sha256':source_hash}],voices)
 if plan['voice_bindings_sha256']!=identity['voice_mapping_sha256']:raise ValueError('voice hash mismatch')
 bridge=callbacks.Bridge(base/'artifacts',None,None,voices,checked_renderer,verify_renderer,dispatch=dispatch,restore=pinned_restore)
 return seq.run(base/'sequence',plan,bridge.callbacks())
