"""Private per-chapter DeepSeek orchestration for verified index snapshots.

The caller supplies trusted snapshot/index hashes, private directories, and
voice bindings. No automatic retry; any existing task directory requires review.
Not a global billing lock; other machines/older workers do not share it.
"""
import hashlib, json, os, subprocess
from pathlib import Path
import chapter_attribution_input
import persisted_chapter_index
import real_pipeline_analysis as analysis
from context_compaction import compact_prior_context

ROOT=Path(__file__).resolve().parent


class CredentialUnavailableError(RuntimeError):
    status='credential_unavailable'
    operation_not_started=True
    retry_allowed=False

def _bytes(x): return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
def _write_new(path, value):
    with Path(path).open('xb') as f:
        f.write(_bytes(value)); f.flush(); os.fsync(f.fileno())

def _chapter_key(chapter):
    import re
    match=re.fullmatch(r'c(\d+)(?:\.p(\d+)(?:\.(\d+))?)?',chapter)
    if not match: raise ValueError('invalid chapter identity')
    return (int(match[1]), int(match[2] or 0), int(match[3] or 0))

def prepare_index_chapter(index_path, chapter_id, trusted_snapshot, expected_index_sha256):
    """Restore independently pinned index and derive bounded one-chapter payload.

    Pins authenticate only to the degree the caller independently trusts them;
    index restoration does not lock the originating book continuously.
    """
    c=persisted_chapter_index.restore_chapter(Path(index_path),chapter_id,
        trusted_snapshot=trusted_snapshot,expected_index_sha256=expected_index_sha256)
    return chapter_attribution_input.prepare(c)['payload']

def _preflight_worker(node, runner=subprocess.run):
    """Check credentials in the worker before reserving a local dispatch task."""
    try:
        proc=runner([node,str(ROOT/'real_pipeline_context_worker.mjs'),'--preflight'],
            capture_output=True,text=True,timeout=30,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        report=json.loads(proc.stdout)
    except Exception as exc:
        raise CredentialUnavailableError('无法完成模型凭据预检，尚未调用模型。') from exc
    if proc.returncode or not isinstance(report,dict) or report.get('status')!='credential_available':
        raise CredentialUnavailableError('credential_unavailable；未配置可用模型凭据，尚未调用模型。')

def dispatch_chapter(payload, private_root, jobs_root, *, node='node', runner=subprocess.run, preflight=None):
    """Dispatch exactly once for a new chapter task directory, never retry.

    Returns a receipt path on success. Any failure leaves the task occupied;
    inspect intent/response/usage and external billing before taking further action.
    Callers must serialize book/chapter use across processes, machines and legacy UI.
    """
    book=payload.get('book_id',''); chapter=payload.get('chapter_id','')
    import re
    if not re.fullmatch(r'book-[0-9a-f]{20}',book) or not re.fullmatch(r'c\d+(?:\.p\d+(?:\.\d+)?)?',chapter): raise ValueError('invalid identity')
    root=Path(private_root).resolve(); jobs=Path(jobs_root).resolve()
    if root==jobs or root in jobs.parents or jobs in root.parents: raise ValueError('separate private and job roots required')
    # Fail closed before mkdir, task reservation, or intent persistence. A missing
    # credential is a configuration issue, not an uncertain paid attempt.
    (preflight or (lambda: _preflight_worker(node,runner)))()
    root.mkdir(parents=True,exist_ok=True); jobs.mkdir(parents=True,exist_ok=True)
    task=root/(book+'-'+chapter); task.mkdir() # atomic reservation, never reused
    input_path=task/'input.json'; _write_new(input_path,payload)
    _write_new(task/'dispatch-state.json',{'status':'dispatch_may_have_occurred','book_id':book,'chapter_id':chapter,'input_sha256':hashlib.sha256(input_path.read_bytes()).hexdigest(),'retry_allowed':False})
    proc=runner([node,str(ROOT/'real_pipeline_context_worker.mjs'),'--run',str(input_path),str(task/'receipt'),str(jobs)],capture_output=True,text=True,timeout=120)
    _write_new(task/'worker-exit.json',{'exit_code':proc.returncode})
    if proc.returncode!=0: raise RuntimeError('dispatch failed or unknown; inspect private task, never retry automatically')
    return {'input_path':str(input_path),'receipt_dir':str(task/'receipt'),'status':'needs_review'}

def stage_receipt(input_path,receipt_dir,voices,role_ids,output_dir,*,narrator_id='narrator'):
    """Create one isolated review-required render package in a new directory."""
    package=analysis.build(input_path,receipt_dir,voices)
    chapters,audit=analysis.to_render_chapters(input_path,package,role_ids,narrator_id=narrator_id)
    out=Path(output_dir); out.mkdir(parents=True,exist_ok=False)
    _write_new(out/'render.json',{'chapters':chapters,'audit':audit,'provenance':package['provenance']})
    _write_new(out/'staging.json',package)
    return {'path':str(out/'render.json'),'chapter_id':audit['blocks'][0]['block_id'] if False else chapters[0]['id'],
            'segments':len(chapters[0]['segments']),'human_reviewed':False,'production_eligible':False}

def candidate_memory(input_path,receipt_dir):
    """Extract chapter-scoped unreviewed character/alias candidates with evidence.

    No automatic identity merging, approval, or claim that evidence proves identity.
    """
    raw=Path(input_path).read_bytes(); x=json.loads(raw); d=Path(receipt_dir)
    intent=json.loads((d/'intent.json').read_bytes()); body=(d/'response.body').read_bytes(); meta=json.loads((d/'response.meta.json').read_bytes())
    if intent.get('input_sha256')!=hashlib.sha256(raw).hexdigest() or meta.get('body_sha256')!=hashlib.sha256(body).hexdigest() or meta.get('http_status')!=200: raise ValueError('receipt mismatch')
    response=json.loads(body); choice=response['choices'][0]
    if choice.get('finish_reason')!='stop': raise ValueError('incomplete response')
    result=json.loads(choice['message']['content']); candidates=[]
    for c in result['characters']:
        name=c.get('name'); aliases=c.get('aliases'); evidence=c.get('evidence')
        if not isinstance(name,str) or not name or not isinstance(aliases,list) or not all(isinstance(a,str) for a in aliases) or not isinstance(evidence,str) or not evidence or evidence not in x['text'] or name not in evidence: raise ValueError('invalid candidate evidence')
        candidates.append({'name':name,'aliases':aliases,'evidence':evidence,'chapter_id':x['chapter_id'],'identity_status':'unreviewed_model_candidate'})
    summary=result.get('chapter_summary')
    if summary is not None and (not isinstance(summary,str) or len(summary)>1500): raise ValueError('invalid summary')
    return {'book_id':x['book_id'],'chapter_id':x['chapter_id'],'response_sha256':hashlib.sha256(body).hexdigest(),
            'source_sha256':hashlib.sha256(x['text'].encode('utf8')).hexdigest(),
            'characters':candidates,'chapter_summary':summary,'human_reviewed':False}

def persist_candidate_memory(input_path,receipt_dir,memory_root):
    """Create immutable chapter candidate record; duplicate requires inspection."""
    record=candidate_memory(input_path,receipt_dir)
    root=Path(memory_root); root.mkdir(parents=True,exist_ok=True)
    _write_new(root/(record['book_id']+'-'+record['chapter_id']+'.json'),record)
    return record

def run_index_chapter(index_path,chapter_id,trusted_snapshot,expected_index_sha256,private_root,jobs_root,memory_root,stage_root,voice_bindings,*,narrator_id='narrator',node='node',corrections=()):
    """Dispatch and stage one chapter; invoke per chapter, never automatic retry.

    voice_bindings: {'narrator': {'id':..., 'voice':...}, 'characters':
    {exact_candidate_name: {'id':..., 'voice':...}}}. Exact names only;
    aliases are stored for review, not silently merged. Missing voices fallback.
    """
    payload=prepare_index_chapter(index_path,chapter_id,trusted_snapshot,expected_index_sha256)
    payload['prior_context']=freeze_prior_context(payload['book_id'],chapter_id,memory_root,corrections=corrections)
    receipt=dispatch_chapter(payload,private_root,jobs_root,node=node)
    memory=persist_candidate_memory(receipt['input_path'],receipt['receipt_dir'],memory_root)
    narrator=voice_bindings['narrator']; supplied=voice_bindings['characters']; names={c['name'] for c in memory['characters']}
    chosen={name:entry for name,entry in supplied.items() if name in names}
    voices={'narrator':narrator['voice'],'characters':{n:e['voice'] for n,e in chosen.items()}}
    role_ids={n:e['id'] for n,e in chosen.items()}
    out=Path(stage_root)/(payload['book_id']+'-'+chapter_id)
    stage=stage_receipt(receipt['input_path'],receipt['receipt_dir'],voices,role_ids,out,narrator_id=narrator['id'])
    return {'receipt':receipt,'memory_chapter':memory['chapter_id'],'stage':stage,'retry_allowed':False}

def freeze_prior_context(book_id,target_chapter_id,memory_root,*,corrections=()):
    """Validate all earlier same-book records, then compact to the 12KB budget.

    Corrections are explicitly supplied operator notes, not review approvals.
    Numeric chapter IDs determine order; callers must verify chapter numbering.
    """
    import re
    if not re.fullmatch(r'book-[0-9a-f]{20}',book_id) or not re.fullmatch(r'c\d+(?:\.p\d+(?:\.\d+)?)?',target_chapter_id): raise ValueError('invalid identity')
    target=_chapter_key(target_chapter_id); root=Path(memory_root); records=[]; seen=set()
    if root.exists():
        for path in sorted(root.glob(book_id+'-c*.json')):
            raw=path.read_bytes(); record=json.loads(raw); cid=record.get('chapter_id','')
            if record.get('book_id')!=book_id or not re.fullmatch(r'c\d+(?:\.p\d+(?:\.\d+)?)?',cid) or cid in seen: raise ValueError('invalid memory identity')
            seen.add(cid)
            if _chapter_key(cid)>=target: continue
            if record.get('human_reviewed') is not False or not isinstance(record.get('characters'),list): raise ValueError('invalid memory status')
            records.append({'book_id':book_id,'chapter_id':cid,'record_sha256':hashlib.sha256(raw).hexdigest(),
                            'response_sha256':record['response_sha256'],'characters':record['characters'],
                            'source_sha256':record.get('source_sha256'),
                            'chapter_summary':record.get('chapter_summary'),'unresolved':record.get('unresolved',[]),
                            'human_reviewed':False})
    notes=[]
    for n in corrections:
        if not isinstance(n,dict) or set(n)!={'chapter_id','note'} or not re.fullmatch(r'c\d+(?:\.p\d+(?:\.\d+)?)?',n['chapter_id']) or _chapter_key(n['chapter_id'])>=target or not isinstance(n['note'],str) or not n['note'].strip() or len(n['note'])>500: raise ValueError('invalid correction note')
        source = next((r.get('source_sha256') for r in records if r['chapter_id']==n['chapter_id']), None)
        notes.append({'chapter_id':n['chapter_id'],'note':n['note'],'source_sha256':source})
    return compact_prior_context(book_id,target_chapter_id,records,corrections=notes,max_bytes=12000)
