#!/usr/bin/env python3
"""Local-only helper invoked through the existing authenticated SSH connection."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid
import re
import fcntl
import signal
from datetime import datetime, timezone
import release_config

ROOT = release_config.mac_data_root()
WD = ROOT/'books'/'_no_book_selected'/'work.huashuo'
BOOK = ROOT/'books'/'_no_book_selected.txt'
BOOK_ID=''
BOOK_LOCK_FD=None
CHAPTER_ID_RE=re.compile(r'c[0-9]+(?:\.p[0-9]+(?:\.[0-9]+)?)?\Z')
ASR_MODEL_REPO='mlx-community/Qwen3-ASR-1.7B-8bit'
PYTHON = Path(os.environ.get('AUDIOBOOK_PYTHON', sys.executable))
HUASHUO = Path(os.environ.get('AUDIOBOOK_HUASHUO_CLI', shutil.which('huashuo') or 'huashuo'))

def select_book(book_id):
    global WD,BOOK,BOOK_ID
    if not re.fullmatch(r'book-[0-9a-f]{20}',book_id):raise ValueError('书籍编号无效。')
    root=(ROOT/'books'/book_id).resolve()
    if not root.is_relative_to((ROOT/'books').resolve()) or root.name!=book_id:raise ValueError('书籍路径无效。')
    record=load(root/'book.json')
    if record.get('book_id')!=book_id:raise ValueError('书籍档案编号不匹配。')
    book=(root/record['input_file']).resolve()
    if not book.is_relative_to(root):raise ValueError('书籍文件路径无效。')
    work=root/'work.huashuo'
    if (work/'book-memory.json').is_file() and load(work/'book-memory.json').get('book_id')!=book_id:
        raise ValueError('人物档案不属于所选书籍。')
    WD=work;BOOK=book;BOOK_ID=book_id

def list_books():
    books=[]
    base=ROOT/'books'
    if base.is_dir():
        for folder in sorted(base.glob('book-*')):
            if not re.fullmatch(r'book-[0-9a-f]{20}',folder.name):continue
            try:
                if not folder.resolve().is_relative_to(base.resolve()):continue
                info=load(folder/'book.json')
                books.append({'id':folder.name,'name':info.get('original_name',folder.name),
                    'ready':(folder/'work.huashuo/book-memory.json').is_file(),
                    'chapter_candidates':info.get('chapter_count_candidates'),'warnings':info.get('warnings',[])})
            except (OSError,ValueError,KeyError):continue
    return {'books':books}

def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def chapter_exists(chapter):
    if not isinstance(chapter,str) or not CHAPTER_ID_RE.fullmatch(chapter):return False
    script=WD/'script.huaben.jsonl'
    if not script.is_file():return False
    with script.open(encoding='utf-8-sig') as f:
        for line in f:
            if not line.strip():continue
            row=json.loads(line)
            if row.get('type')=='chapter' and row.get('id')==chapter:return True
    return False

def safe_chapter_id(chapter):
    return isinstance(chapter,str) and CHAPTER_ID_RE.fullmatch(chapter) is not None

def require_chapter(chapter):
    if not isinstance(chapter,str) or not CHAPTER_ID_RE.fullmatch(chapter):
        raise ValueError('章节编号无效。')
    if not chapter_exists(chapter):raise ValueError('章节不存在。')
    return chapter

def atomic_json(path,data):
    tmp=path.with_name(path.name+'.tmp')
    with tmp.open('w',encoding='utf-8') as f:
        json.dump(data,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def _profile_manifest_readiness():
    reasons=[];worker_hash=None
    ledger_path=Path(__file__).with_name('analysis_manifest.py')
    rpc_path=Path(__file__).with_name('profile_manifest_rpc.py')
    worker_path=Path(__file__).resolve().parent/'approved-workers'/'deepseek_profile.mjs'
    if not ledger_path.is_file():reasons.append('Mac 端 analysis_manifest.py 未部署。')
    if not rpc_path.is_file():reasons.append('Mac 端 profile_manifest_rpc.py 未部署。')
    if not worker_path.is_file():reasons.append('画像 worker 未部署。')
    if reasons:return {'ready':False,'worker_sha256':None,'reasons':reasons}
    try:
        import analysis_manifest as ledger
        import profile_manifest_rpc as rpc
        pinned=rpc.DEFAULT_WORKER_COPY
        if not pinned.is_file() or pinned.is_symlink():
            reasons.append('Mac 固定画像 worker 副本缺失或不是普通文件。')
        else:
            worker_hash=hashlib.sha256(pinned.read_bytes()).hexdigest()
            if worker_hash not in rpc.ALLOWED_WORKER_SHA256:
                reasons.append('Mac 固定画像 worker hash 不在 RPC allowlist。')
            if hashlib.sha256(worker_path.read_bytes()).hexdigest()!=worker_hash:
                reasons.append('console-v2 worker 与 Mac 固定 allowlist worker 不一致。')
        if not callable(getattr(ledger,'create_manifest',None)) or not callable(getattr(ledger,'approve_chapter',None)):
            reasons.append('账本创建或审批接口不可用。')
        if not all(callable(getattr(rpc,name,None)) for name in ('reserve','finalize')):
            reasons.append('画像 RPC reserve/finalize 接口不可用。')
        rpc_text=rpc_path.read_text(encoding='utf-8')
        worker_text=worker_path.read_text(encoding='utf-8')
        if 'reserve_profile_request' not in rpc_text or 'finalize_profile_request' not in rpc_text:
            reasons.append('画像 RPC action 不完整。')
        if ('HUASHUO_PROFILE_MANIFEST_MODE' not in worker_text or
                'reserve_profile_request' not in worker_text or 'finalize_profile_request' not in worker_text):
            reasons.append('画像 worker 未包含 manifest 模式 reserve/finalize。')
    except (OSError,ImportError,AttributeError,ValueError) as exc:
        reasons.append('Mac 端账本组件校验失败：'+str(exc))
    return {'ready':not reasons,'worker_sha256':worker_hash,'reasons':reasons}

def _legacy_profile_blockers(chapter_ids,manifest=None):
    blockers=[]
    review_path=WD/'profile-review.json'
    if review_path.is_file():
        try:
            review=load(review_path)
            if review.get('status')=='needs_review':
                ledger_row=next((item for item in (manifest or {}).get('chapters',[])
                                 if item.get('chapter_id')==review.get('chapter_id')),None)
                provenance=(ledger_row or {}).get('request_provenance') or {}
                legacy=(ledger_row or {}).get('legacy_provenance') or {}
                matching=(ledger_row and ledger_row.get('status')=='awaiting_review'
                          and ((provenance.get('kind')=='manifest_reserved'
                                and provenance.get('job_hash')==review.get('input_sha256'))
                               or (legacy.get('kind')=='legacy_unproven'
                                   and legacy.get('job_hash')==review.get('input_sha256'))))
                if not matching:blockers.append('存在尚未迁移的旧画像审核。')
        except (OSError,ValueError):blockers.append('旧画像审核文件无法核验。')
    for chapter in chapter_ids:
        receipt_path=WD/'state'/('profile-state-'+chapter+'.json')
        if not receipt_path.is_file():continue
        try:receipt=load(receipt_path)
        except (OSError,ValueError):
            blockers.append(f'{chapter} 的旧画像收据无法核验。');continue
        attempt=receipt.get('manifest_attempt_id')
        ledger_row=next((item for item in (manifest or {}).get('chapters',[])
                         if item.get('chapter_id')==chapter),None)
        provenance=(ledger_row or {}).get('request_provenance') or {}
        legacy=(ledger_row or {}).get('legacy_provenance') or {}
        if attempt:
            if not ledger_row or provenance.get('attempt_id')!=attempt:
                blockers.append(f'{chapter} 存在未匹配账本的画像收据。')
        elif receipt.get('status') in ('inflight','failed','billing_unknown','complete_needs_review'):
            adopted=(ledger_row and ledger_row.get('status') in ('awaiting_review','approved')
                     and legacy.get('kind')=='legacy_unproven' and legacy.get('job_hash')==receipt.get('job_hash'))
            if not adopted:blockers.append(f'{chapter} 存在未迁移的旧画像收据。')
    return blockers

def analysis_manifest_status(chapter_ids=None,current_chapter=None):
    chapter_ids=list(chapter_ids or [])
    path=WD/'state'/'analysis-manifest.json'
    readiness=_profile_manifest_readiness()
    enabled=False
    review_path=WD/'profile-review.json'
    try:profile_review_status=load(review_path).get('status') if review_path.is_file() else None
    except (OSError,ValueError):profile_review_status='invalid'
    blockers=list(readiness['reasons'])
    if not path.is_file():
        legacy_blockers=_legacy_profile_blockers(chapter_ids)
        blockers.extend(legacy_blockers)
        return {'exists':False,'book_id':BOOK_ID,'profile_mode_enabled':False,
                'components_ready':readiness['ready'],'worker_sha256':readiness['worker_sha256'],
                'can_enable':False,'blockers':blockers,'legacy_blockers':legacy_blockers,
                'profile_review_status':profile_review_status,'chapters':[]}
    try:
        import analysis_manifest as ledger
        manifest=ledger.load_manifest(path)
        enabled=manifest.get('profile_manifest_mode_enabled') is True
        legacy_blockers=_legacy_profile_blockers(chapter_ids,manifest)
        blockers.extend(legacy_blockers)
        if manifest.get('book_id')!=BOOK_ID:raise ValueError('manifest book_id 与当前书籍不符。')
        manifest_ids=[item['chapter_id'] for item in manifest['chapters']]
        if manifest_ids!=chapter_ids:blockers.append('账本章节清单与当前话本不一致。')
        if manifest.get('currency')!='CNY':blockers.append('当前画像 RPC 仅接受 CNY 账本。')
        item_states=[item['status'] for item in manifest['chapters']]
        if not enabled and any(status not in ('pending','approved') for status in item_states):
            blockers.append('账本存在待恢复、待审核或计费未知章节，不能启用新画像请求。')
        if manifest.get('status')!='active':blockers.append('账本未处于 active 状态。')
        limit=Decimal(str(manifest['estimated_budget_limit']))
        reserved=ledger._reserved(manifest)
        chapters=[{'chapter_id':item['chapter_id'],'status':item['status'],
                   'estimated_cost':item.get('estimated_cost'),'usage':item.get('usage'),
                   'billing_unknown':item.get('billing_unknown')}
                  for item in manifest['chapters']]
        index=next((i for i,item in enumerate(manifest['chapters'])
                    if item['chapter_id']==current_chapter),None)
        start_allowed=(enabled and readiness['ready'] and manifest.get('status')=='active' and index is not None
                       and manifest['chapters'][index]['status']=='pending'
                       and all(item['status']=='approved' for item in manifest['chapters'][:index])
                       and not legacy_blockers
                       and not any(item['status'] in ('in_progress','awaiting_review','billing_unknown')
                                   for item in manifest['chapters']))
        can_enable=(readiness['ready'] and not legacy_blockers
                    and manifest.get('status')=='active' and manifest_ids==chapter_ids
                    and manifest.get('currency')=='CNY' and all(s in ('pending','approved') for s in item_states))
        return {'exists':True,'book_id':BOOK_ID,'status':manifest['status'],'currency':manifest['currency'],
                'estimated_budget_limit':str(limit),'reserved_estimate':str(reserved),
                'remaining_estimate':str(limit-reserved),'profile_mode_enabled':enabled,
                'components_ready':readiness['ready'],'worker_sha256':readiness['worker_sha256'],
                'can_enable':can_enable,'can_start_profile':start_allowed,'blockers':blockers,
                'legacy_blockers':legacy_blockers,'profile_review_status':profile_review_status,
                'chapters':chapters,'events':manifest.get('events',[])[-10:]}
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return {'exists':True,'book_id':BOOK_ID,'profile_mode_enabled':enabled,
                'components_ready':readiness['ready'],'worker_sha256':readiness['worker_sha256'],
                'can_enable':False,'blockers':[ *blockers,'账本无法核验：'+str(exc)],
                'legacy_blockers':[],'profile_review_status':profile_review_status,'chapters':[]}

def recover_console_transaction():
    journal=WD/'state/console-transaction.json'
    if not journal.exists():return False
    record=load(journal)
    if record.get('status')!='applying':return False
    backup=Path(record['backup']).resolve()
    if not backup.is_relative_to((WD/'console-backups').resolve()):raise ValueError('控制台备份路径无效。')
    allowed={'book-memory.json','cast.json','script.huaben.jsonl','memory-checkpoint.json','profile-review.json','state/chapter-reviews.json'}
    for item in record['files']:
        name=item['name']
        if name not in allowed:raise ValueError('控制台事务包含无效文件。')
        dst=WD/name;src=backup/name
        if item['existed']:
            if not src.is_file():raise ValueError('控制台事务备份不完整。')
            shutil.copy2(src,dst)
        elif dst.exists():dst.unlink()
    record['status']='restored_after_interruption';atomic_json(journal,record)
    return True

def begin_console_transaction(names):
    backup=WD/'console-backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');backup.mkdir(parents=True)
    files=[]
    for name in names:
        src=WD/name;exists=src.is_file()
        if exists:
            (backup/name).parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(src,backup/name)
        files.append({'name':name,'existed':exists})
    record={'status':'applying','backup':str(backup.resolve()),'files':files}
    atomic_json(WD/'state/console-transaction.json',record)
    return record

def finish_console_transaction(record):
    record['status']='committed';atomic_json(WD/'state/console-transaction.json',record)

def stamp():
    h=hashlib.sha256()
    for name in ('book-memory.json','cast.json','script.huaben.jsonl'):
        h.update((WD/name).read_bytes())
    return h.hexdigest()

def voices():
    out=[]
    for f in sorted((ROOT/'source/src/huashuo/voices/zh').glob('*.json')):
        v=load(f);out.append({'id':'library:zh/'+v['id'],'label':v.get('role',v['id'])})
    return out

def state(data=None):
    data=data or {}
    if not (WD/'book-memory.json').is_file():
        return {'book_id':BOOK_ID,'ready':False,'voices':voices(),'characters':{},'dialogue':[],'chapters':[]}
    memory=load(WD/'book-memory.json')
    rows=[json.loads(x) for x in (WD/'script.huaben.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]
    from annotation_job import chapter_digest
    proof_path=WD/'state/chapter-reviews.json'
    proofs=load(proof_path) if proof_path.exists() else {}
    grouped={};current_chapter=None
    for row in rows:
        if row.get('type')=='chapter':current_chapter=row['id'];grouped[current_chapter]=[]
        if current_chapter is not None:grouped[current_chapter].append(row)
    chapters=[]
    for r in rows:
        if r.get('type')!='chapter':continue
        ident=r['id'];proof=proofs.get(ident,{})
        valid=(proof.get('book_id')==BOOK_ID and proof.get('script_digest')==chapter_digest(grouped[ident],ident))
        chapter={'id':ident,'title':r['text'],'reviewed':valid}
        if valid:
            reviewer=proof.get('reviewed_by','unknown')
            chapter['reviewed_by']=reviewer if isinstance(reviewer,str) and reviewer else 'unknown'
        chapters.append(chapter)
    chapter_ids={item['id'] for item in chapters}
    chapter_structure={'needs_review':False,'requires_review':False,'review_recorded':False,'candidate_count':None,
                       'chapter_count':len(chapters),'missing_candidates':[],'unexpected_chapters':[]}
    metadata_path=WD.parent/'book.json'
    structure_path=WD/'state'/'chapter-review.json'
    if metadata_path.is_file():
        metadata=load(metadata_path)
        candidates=metadata.get('chapter_candidates')
        if isinstance(candidates,list) and all(isinstance(x,str) for x in candidates):
            titles=[x['title'] for x in chapters]
            chapter_structure['candidate_count']=len(candidates)
            chapter_structure['missing_candidates']=[x for x in candidates if x not in titles]
            chapter_structure['unexpected_chapters']=[x for x in titles if x not in candidates]
    if structure_path.is_file():
        marker=load(structure_path)
        chapter_structure['review_recorded']=True
        chapter_structure['requires_review']=marker.get('requires_review') is True
    chapter_structure['needs_review']=bool(chapter_structure['requires_review'] or
        chapter_structure['missing_candidates'] or chapter_structure['unexpected_chapters'])
    selected=data.get('chapter') or (chapters[0]['id'] if chapters else None)
    if selected is not None and selected not in {x['id'] for x in chapters}:raise ValueError('章节不存在。')
    result=[];current=None
    for r in rows:
        if r.get('type')=='chapter':current=r['id']
        if r.get('type')=='dialogue' and current==selected:
            result.append({'id':r['id'],'text':r['text'],'speaker':r.get('speaker','unknown'),'emotion':r.get('emotion','')})
    review_path=WD/'annotation-review.json';review=None
    if review_path.exists():
        r=load(review_path)
        if r.get('status')=='needs_review' and r.get('chapter_id') in (selected,None):
            try:
                from annotation_job import review_queue
                queue=review_queue(WD)
                review={'decisions':r['decisions'],'approved':r.get('approved',{}),
                        'sha256':queue['review_sha256'],'items':queue['items'],'note':queue['note']}
            except ValueError as exc:
                review={'decisions':r.get('decisions',{}),'approved':r.get('approved',{}),
                        'sha256':hashlib.sha256(review_path.read_bytes()).hexdigest(),
                        'stale':True,'note':str(exc)}
    profile_path=WD/'profile-review.json';profile_review=None
    if profile_path.exists():
        r=load(profile_path)
        if r.get('status')=='needs_review':
            if r.get('book_id')!=BOOK_ID:raise ValueError('画像审核所属书籍不匹配。')
            profile_review={**r,'sha256':hashlib.sha256(profile_path.read_bytes()).hexdigest()}
            chapter=r.get('chapter_id')
            if safe_chapter_id(chapter) and chapter in chapter_ids:
                job_path=WD/'state'/('profile-state-'+chapter+'.json')
                if job_path.is_file():
                    job=load(job_path)
                    if job.get('book_id')==BOOK_ID and job.get('chapter_id')==chapter and job.get('job_hash')==r.get('input_sha256'):
                        profile_review['usage']=job.get('usage');profile_review['estimated_peak_cost_cny']=job.get('estimated_peak_cost_cny')
    settings=load(WD/'console-settings.json') if (WD/'console-settings.json').exists() else {}
    return {'book_id':BOOK_ID,'ready':True,'stamp':stamp(),'voices':voices(),'characters':memory['characters'],
            'narrator':memory['narrator'],'chapters':chapters,'chapter':selected,'dialogue':result,'review':review,
            'speed':settings.get('speed',1.0),'profile_review':profile_review,
            'chapter_structure':chapter_structure,
            'analysis_manifest':analysis_manifest_status([item['id'] for item in chapters],selected)}

def run(args, timeout=1200):
    env=os.environ.copy();path_items=[str(Path(PYTHON).parent),str(release_config.mac_ffmpeg().parent),env.get('PATH','')]
    if Path(HUASHUO).is_absolute():path_items.insert(1,str(Path(HUASHUO).parent))
    env.update({'PATH':os.pathsep.join(x for x in path_items if x),
         'HF_HOME':str(ROOT/'hf'),'HF_HUB_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1'})
    # Keep the same book lock open in the child. If SSH drops while synthesis
    # is still running, another console request must not start concurrently.
    pass_fds=(BOOK_LOCK_FD,) if BOOK_LOCK_FD is not None else ()
    command=[str(x) for x in args]
    if len(command)>1 and command[0]==str(HUASHUO) and command[1]=='import':
        command=[str(PYTHON),str(Path(__file__).with_name('safe_huashuo_import.py')),*command[1:]]
    p=subprocess.Popen(command,cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                       text=True,pass_fds=pass_fds,start_new_session=True)
    try:
        stdout,stderr=p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:os.killpg(p.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:os.killpg(p.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            p.communicate()
        raise RuntimeError('本地生成超时，已请求停止该任务的进程组。')
    if p.returncode:
        raise RuntimeError((stderr+'\n'+stdout)[-3000:])
    return stdout[-3000:]

def save(data):
    if data.get('stamp') != stamp(): raise ValueError('资料已被其他操作修改，请重新载入后再保存。')
    memory=load(WD/'book-memory.json'); name=data['character']; voice=data['voice']
    valid={v['id'] for v in voices()}
    if name not in memory['characters'] and name != '旁白': raise ValueError('角色不存在。')
    if voice not in valid: raise ValueError('所选音色不存在。')
    if name=='旁白': memory['narrator']['voice']=voice
    else: memory['characters'][name]['voice']=voice
    rows=[json.loads(x) for x in (WD/'script.huaben.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]
    line=data.get('line')
    proof_path=WD/'state/chapter-reviews.json';proofs=load(proof_path) if proof_path.exists() else {}
    chapter=data.get('chapter') or next((r['id'] for r in rows if r.get('type')=='chapter'),None)
    before_chapter=[];active=None
    for r in rows:
        if r.get('type')=='chapter':active=r.get('id')
        if active==chapter:before_chapter.append(json.loads(json.dumps(r,ensure_ascii=False)))
    from annotation_job import chapter_digest
    existing_proof=proofs.get(chapter,{})
    can_preserve=bool(before_chapter and existing_proof.get('book_id')==BOOK_ID and
                      existing_proof.get('script_digest')==chapter_digest(before_chapter,chapter))
    if line:
        row=next((r for r in rows if r.get('id')==line['id'] and r.get('type')=='dialogue'),None)
        if not row or not any(r.get('id')==line['id'] for r in before_chapter): raise ValueError('当前章节对白不存在。')
        kind=line.get('kind','dialogue');speaker=line.get('speaker')
        if kind not in ('dialogue','narration','skip'):raise ValueError('内容类型无效。')
        if kind=='dialogue' and speaker not in memory['characters']: raise ValueError('对白说话人不存在。')
        row['type']=kind
        if kind=='dialogue':row['speaker']=speaker
        else:
            row.pop('speaker',None)
            if kind=='skip':row['reason']='user edit'
        emotion=line.get('emotion','').strip()
        if len(emotion)>200: raise ValueError('语气说明请控制在200字以内。')
        if emotion and kind=='dialogue': row['emotion']=emotion
        else: row.pop('emotion',None)
        corrections=[c for c in memory.get('corrections',[]) if c['block_id'] != row['id']]
        if kind=='dialogue':
            corrections.append({'block_id':row['id'],'text_sha256':hashlib.sha256(row['text'].encode()).hexdigest(),
                                'speaker':speaker,'reason':'控制台人工修订'})
        memory['corrections']=corrections
    transaction=begin_console_transaction(('book-memory.json','cast.json','script.huaben.jsonl','memory-checkpoint.json','state/chapter-reviews.json'))
    try:
        atomic_json(WD/'book-memory.json',memory)
        from annotation_job import write_rows
        write_rows(WD/'script.huaben.jsonl',rows)
        run([PYTHON,ROOT/'book_memory.py','apply',WD])
        run([HUASHUO,'check',BOOK,'--workdir',WD])
        if line and can_preserve:
            after=[];active=None
            for r in [json.loads(x) for x in (WD/'script.huaben.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]:
                if r.get('type')=='chapter':active=r.get('id')
                if active==chapter:after.append(r)
            before_other=[r for r in before_chapter if r.get('id')!=line['id']]
            after_other=[r for r in after if r.get('id')!=line['id']]
            if before_other==after_other:
                proofs[chapter]={'book_id':BOOK_ID,'script_digest':chapter_digest(after,chapter),
                                 'reviewed_by':existing_proof.get('reviewed_by','unknown'),
                                 'last_line_edited_by':'user','last_line_edited':line['id']}
                atomic_json(proof_path,proofs)
        finish_console_transaction(transaction)
    except Exception:
        recover_console_transaction()
        raise
    return state({'chapter':data.get('chapter')})

def playback_speed(data):
    try:value=float(data.get('speed',1.0))
    except (TypeError,ValueError):raise ValueError('语速数值无效。')
    if not 0.8<=value<=1.3:raise ValueError('语速必须在 0.8 到 1.3 之间。')
    return value

def render_config_fingerprint(speed):
    h=hashlib.sha256((stamp()+'|'+str(speed)+'|console-render-v1').encode())
    h.update((WD/'state/ingest.json').read_bytes())
    cast=load(WD/'cast.json')
    for voice in sorted({x.get('voice','') for x in [cast.get('narrator',{}),*cast.get('characters',{}).values()]}):
        if not voice.startswith('library:zh/'):continue
        ident=voice.removeprefix('library:zh/')
        if not re.fullmatch(r'[A-Za-z0-9_-]+',ident):raise ValueError('音色编号无效。')
        for ext in ('.json','.npy'):
            path=ROOT/'source/src/huashuo/voices/zh'/(ident+ext)
            if path.is_file():h.update(path.read_bytes())
    return h.hexdigest()

def preview(name,data):
    if name not in load(WD/'book-memory.json')['characters']: raise ValueError('请选择角色试听。')
    speed=playback_speed(data)
    output=WD/'console-preview.m4b'
    run([HUASHUO,'audition',BOOK,'--workdir',WD,'--character',name,'-o',output])
    wav=WD/'console-preview.wav'
    run([release_config.mac_ffmpeg(),'-hide_banner','-loglevel','error','-y','-i',output,'-vn','-af',f'atempo={speed}', '-ac','1','-ar','24000','-c:a','pcm_s16le',wav])
    return {'remote_file':str(wav)}

def render(data):
    current=state(data);chapter=current.get('chapter')
    speed=playback_speed(data)
    if not chapter:raise ValueError('没有可生成的章节。')
    from annotation_job import chapter_digest, rows
    proof=WD/'state'/'chapter-reviews.json'
    records=load(proof) if proof.exists() else {}
    reviewed=records.get(chapter,{})
    if reviewed.get('book_id')!=BOOK_ID or reviewed.get('script_digest')!=chapter_digest(rows(WD/'script.huaben.jsonl'),chapter):
        raise ValueError('本章尚未完成审核，或审核后内容已修改；请重新审核后生成。')
    if any(r['speaker']=='unknown' for r in current['dialogue']):raise ValueError('本章有未确认说话人的对白，请先审核。')
    options=load(WD/'state/ingest.json').get('options',{})
    if options.get('voices')=='single' and current['dialogue']:raise ValueError('这本书尚未完成多角色标注；请先建立人物并审核。')
    from render_queue import RenderQueue
    queue=RenderQueue(WD,BOOK_ID)
    # The caller holds .console.lock, so a running record here is from an interrupted request.
    queue.recover(book_lock_held=True)
    fingerprint=render_config_fingerprint(speed);wav=WD/('console-output-'+chapter+'.wav')
    existing=queue.get().get(chapter)
    if existing and existing.get('status') in ('complete','failed','interrupted','stale'):
        queue.restart(chapter,fingerprint)
    else:queue.enqueue(chapter,wav,fingerprint)
    job=queue.claim(chapter)
    if not job:raise ValueError('章节任务无法开始，请检查队列状态。')
    try:
        index=next(i for i,c in enumerate(current['chapters'],1) if c['id']==chapter)
        run([HUASHUO,'synth',BOOK,'--workdir',WD,'--chapters',str(index),'--no-asr','--no-opening','--no-closing'],timeout=10800)
        base=WD/('console-output-'+chapter+'-base.mp3')
        log=run([HUASHUO,'package',BOOK,'--workdir',WD,'--chapters',str(index),'--no-opening','--no-closing','--format','mp3','-o',base])
        output=WD/('console-output-'+chapter+'.mp3')
        if speed==1.0:shutil.copy2(base,output)
        else:run([release_config.mac_ffmpeg(),'-hide_banner','-loglevel','error','-y','-i',base,'-af',f'atempo={speed}',
                  '-map_metadata','0','-map_chapters','-1','-codec:a','libmp3lame','-q:a','3',output])
        run([release_config.mac_ffmpeg(),'-hide_banner','-loglevel','error','-y','-i',output,'-ac','1','-ar','24000','-c:a','pcm_s16le',wav])
        (WD/'console-settings.json').write_text(json.dumps({'speed':speed},indent=2)+'\n',encoding='utf-8')
        queue.complete(chapter,job['lease'])
        return {'remote_file':str(wav),'log':log}
    except Exception as exc:
        queue.fail(chapter,job['lease'],str(exc))
        raise

def chapter_audio(data):
    chapter=require_chapter(data.get('chapter'))
    from render_queue import RenderQueue
    queue=RenderQueue(WD,BOOK_ID)
    job=queue.get(chapter)
    if not job or job.get('status')!='complete':raise ValueError('当前章节没有已完成的音频任务。')
    if job.get('book_id')!=BOOK_ID or job.get('chapter_id')!=chapter:
        raise ValueError('音频任务书籍或章节不匹配。')
    if queue._proof(chapter)!=job.get('script_digest'):
        raise ValueError('章节审核凭证或话本已改变，不能播放该音频。')
    settings=load(WD/'console-settings.json') if (WD/'console-settings.json').is_file() else {}
    fingerprint=render_config_fingerprint(playback_speed({'speed':settings.get('speed',1.0)}))
    if not isinstance(job.get('config_fingerprint'),str) or job['config_fingerprint']!=fingerprint:
        raise ValueError('生成配置已改变，不能复用该章节音频。')
    expected=(WD/('console-output-'+chapter+'.wav')).resolve()
    output=Path(job.get('output_path','')).resolve()
    if output!=expected or not output.is_relative_to(WD.resolve()):
        raise ValueError('音频路径不属于当前章节的固定工作区输出。')
    if not queue.verify_output(chapter):raise ValueError('已生成音频缺失或哈希校验失败。')
    return {'remote_file':str(output)}

def asr_cache_file_is_allowed(path,allowed_roots):
    try:
        resolved=path.resolve(strict=True)
        roots=[root.resolve(strict=True) for root in allowed_roots]
    except OSError:
        return False
    return any(resolved.is_relative_to(root) for root in roots)

def resolve_cached_asr_model():
    repo=ROOT/'hf/hub/models--mlx-community--Qwen3-ASR-1.7B-8bit'
    try:
        revision=(repo/'refs/main').read_text(encoding='utf-8').strip()
        if not re.fullmatch(r'[0-9a-f]{40}',revision):raise ValueError('本地 ASR 模型版本标记无效。')
        manifest=load(repo/'trees'/(revision+'.json'))
        files=manifest.get('files')
        if not isinstance(files,dict) or not files or 'model.safetensors' not in files:
            raise ValueError('本地 ASR 模型文件清单缺失。')
        repo_root=repo.resolve(strict=True)
        shared_blobs=ROOT/'hf/hub/blobs'
        allowed_roots=(repo_root,shared_blobs.resolve(strict=True)) if shared_blobs.exists() else (repo_root,)
        snapshot=repo/'snapshots'/revision
        resolved_snapshot=snapshot.resolve(strict=True)
        if not resolved_snapshot.is_relative_to(repo_root):raise ValueError('本地 ASR 模型路径无效。')
        for relative,record in files.items():
            name=Path(relative)
            if name.is_absolute() or '..' in name.parts or not isinstance(record,dict):
                raise ValueError('本地 ASR 模型文件清单无效。')
            path=(snapshot/name).resolve(strict=True)
            size=record.get('size')
            if (not asr_cache_file_is_allowed(path,allowed_roots) or not path.is_file() or type(size) is not int
                    or size<=0 or path.stat().st_size!=size):
                raise ValueError('本地 ASR 模型快照不完整或大小不匹配。')
        return resolved_snapshot
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise ValueError(f'本地 ASR 模型 {ASR_MODEL_REPO} 缓存缺失或不完整；检查已停止，不会下载。') from exc

def asr_spot_check_action(data):
    chapter=require_chapter(data.get('chapter'))
    sample_count=data.get('samples',3)
    if type(sample_count) is not int or not 1<=sample_count<=8:
        raise ValueError('抽检样本数必须是 1 到 8 的整数。')
    model_dir=resolve_cached_asr_model()
    script=Path(__file__).with_name('asr_spot_check.py')
    if not script.is_file():raise ValueError('找不到本地 ASR 抽检程序，未启动模型。')
    output_dir=ROOT/'asr-spot-checks'/BOOK_ID;output_dir.mkdir(parents=True,exist_ok=True)
    output=output_dir/(chapter+'-'+uuid.uuid4().hex+'.json')
    if output.resolve().is_relative_to(WD.resolve()):raise ValueError('抽检报告路径不能位于书籍工作区。')
    run([PYTHON,script,WD,chapter,output,'--model-dir',model_dir,'--samples',str(sample_count),'--run-asr'],timeout=1800)
    report=load(output)
    if (report.get('chapter_id')!=chapter or Path(report.get('workdir','')).resolve()!=WD.resolve()
        or report.get('sample_count')!=len(report.get('samples',[]))):
        raise ValueError('ASR 抽检报告校验失败。')
    return {'report_path':str(output),'report':report}

def batch_allows_chapter_audio_read(batch,book_id,chapter):
    return (isinstance(batch,dict) and batch.get('book_id')==book_id and batch.get('status')=='running'
            and any(isinstance(item,dict) and item.get('id')==chapter and item.get('status')=='complete'
                    for item in batch.get('chapters',[])))

def confirm_narration_only(data):
    chapter=data.get('chapter')
    if data.get('confirmed') is not True:raise ValueError('需要明确确认本章已人工核对。')
    current=state({'chapter':chapter})
    if not chapter:raise ValueError('请选择章节。')
    if current.get('review'):raise ValueError('本章有待审核标注，请先完成审核。')
    from annotation_job import chapter_digest, rows
    script=rows(WD/'script.huaben.jsonl')
    selected=[];active=None
    for row in script:
        if row.get('type')=='chapter':active=row.get('id')
        if active==chapter:selected.append(row)
    if not selected:raise ValueError('审核章节不存在。')
    if any(row.get('type')=='dialogue' for row in selected):
        raise ValueError('本章话本中仍有对白条目，不能按纯旁白章节确认。')
    proof=WD/'state'/'chapter-reviews.json'
    records=load(proof) if proof.exists() else {}
    records[chapter]={'book_id':BOOK_ID,'script_digest':chapter_digest(script,chapter),'reviewed_by':'user'}
    proof.parent.mkdir(parents=True,exist_ok=True)
    tmp=proof.with_name(proof.name+'.tmp')
    tmp.write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.replace(tmp,proof)
    return state({'chapter':chapter})

def batch_view():
    from batch_worker import status
    batch=status(sys.modules[__name__])
    if not batch:return {'batch':None}
    items=batch.get('chapters',[])
    current=next((x['id'] for x in items if x.get('status')=='running'),None)
    # A status query never changes recovery state or blocks on the book lock.
    active=False
    with (WD/'.console.lock').open('a') as lock:
        try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:active=True
        else:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)
    message=batch.get('error','')
    if batch.get('stop_requested') and batch.get('status') in ('queued','running'):
        message='已请求在当前章节结束后停止。'
    elif not active and batch.get('status') in ('running','queued'):
        message='后台进程未持锁；请检查并明确恢复。'
    return {'batch':{'id':batch['id'],'status':batch['status'],'completed':sum(x.get('status')=='complete' for x in items),
                     'total':len(items),'current_chapter':current,'chapters':[x['id'] for x in items],
                     'worker_active':active,'message':message}}

def batch_stop():
    from batch_worker import request_stop
    request_stop(sys.modules[__name__])
    return batch_view()

def batch_start(data,resume=False):
    from batch_worker import create,status
    backend=sys.modules[__name__]
    if resume:
        prior=status(backend)
        if not prior or prior.get('status') not in ('queued','running','interrupted','stopped'):
            raise ValueError('没有可恢复的批次。')
    else:
        create(backend,data.get('chapters'),data.get('speed',1.0),BOOK_LOCK_FD)
    log_dir=WD/'logs';log_dir.mkdir(parents=True,exist_ok=True)
    command=[sys.executable,str(Path(__file__).with_name('batch_worker.py')),'--book',BOOK_ID,
             '--lock-fd',str(BOOK_LOCK_FD)]
    if resume:command.append('--resume')
    with (log_dir/'batch-worker.log').open('ab') as log:
        subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                         start_new_session=True,pass_fds=(BOOK_LOCK_FD,),cwd=ROOT)
    # The inherited descriptor keeps the lock while the worker starts and runs.
    return {'batch':{'status':'starting','completed':0,'total':len(data.get('chapters',[])) if not resume else len(prior['chapters']),
                     'chapters':data.get('chapters',[]) if not resume else [x['id'] for x in prior['chapters']],
                     'current_chapter':None,'message':'后台任务正在启动，请刷新状态。'}}

def import_book(data):
    name=data['staged_file']
    if not re.fullmatch(r'[0-9a-f]{32}\.(txt|docx|epub)',name):raise ValueError('导入文件名无效。')
    stage=(ROOT/'import-source'/name).resolve()
    if not stage.is_relative_to((ROOT/'import-source').resolve()) or not stage.is_file():raise ValueError('导入文件不存在。')
    from book_import import prepare,repair
    result=prepare(stage)
    record=result['record'];book_id=result['book_id'];select_book(book_id)
    # Preserve the user's display name without changing the verified source hash.
    meta_path=ROOT/'books'/book_id/'book.json';meta=load(meta_path)
    display=str(data.get('display_name','')).strip()
    if display:meta['original_name']=Path(display).name[:240]
    meta_path.write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if (WD/'book-memory.json').exists():return {'book_id':book_id,'reused':True,'warnings':meta.get('warnings',[])}
    return initialize_book({'book_id':book_id})

def initialize_book(data):
    select_book(data.get('book_id'))
    meta=load(ROOT/'books'/BOOK_ID/'book.json');record=meta;book_id=BOOK_ID
    with (ROOT/'books'/BOOK_ID/'.initialize.lock').open('a') as lock:
        try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('该书正在建立工作区，请稍候。')
        if (WD/'book-memory.json').exists():return {'book_id':book_id,'reused':True,'warnings':meta.get('warnings',[])}
        if WD.exists():raise ValueError('检测到未完成的工作目录，未覆盖；请先恢复或检查。')
        run([HUASHUO,'import',BOOK,'--workdir',WD,'--single-voice'])
        warnings=list(meta.get('warnings',[]))
        if record['format'] in ('.txt','.docx'):
            try:
                from book_import import repair
                chapter=repair(WD)
                if chapter.get('requires_review'):warnings.append('已检测并修正可疑章节，仍需核对章节结构。')
            except (ValueError,OSError,KeyError):
                warnings.append('无法安全自动核对章节，原章节结构保留，请人工检查。')
        run([PYTHON,ROOT/'book_memory.py','init',WD,'--book-id',book_id])
        return {'book_id':book_id,'reused':False,'warnings':warnings}

def add_character(data):
    if data.get('stamp')!=stamp():raise ValueError('资料已被其他操作修改，请重新载入。')
    memory=load(WD/'book-memory.json');name=str(data.get('name','')).strip();voice=data.get('voice')
    if not name or len(name)>60 or name in ('unknown','旁白') or name in memory['characters']:raise ValueError('人物名称无效或已存在。')
    if voice not in {x['id'] for x in voices()}:raise ValueError('音色不存在。')
    aliases=[x.strip() for x in str(data.get('aliases','')).split('，') if x.strip()]
    memory['characters'][name]={'aliases':aliases,'voice':voice,'description':str(data.get('description','')).strip()[:300]}
    from book_memory import validate
    validate(memory)
    transaction=begin_console_transaction(('book-memory.json','cast.json'))
    try:
        cast=load(WD/'cast.json');cast.setdefault('characters',{})[name]=memory['characters'][name]
        atomic_json(WD/'book-memory.json',memory);atomic_json(WD/'cast.json',cast)
        finish_console_transaction(transaction)
    except Exception:
        recover_console_transaction()
        raise
    return state({'chapter':data.get('chapter')})

def export_annotations(data):
    chapter=require_chapter(data.get('chapter'))
    review=WD/'annotation-review.json'
    if review.exists() and load(review).get('status')=='needs_review':raise ValueError('已有未完成审核，请先处理。')
    from annotation_job import export
    output=WD/'state'/('annotation-input-'+chapter+'.json')
    result=export(WD,output,chapter)
    if result['quotes']==0:raise ValueError('本章没有待标注的引号内容。')
    return {'remote_file':str(output),'quotes':result['quotes'],'chapter':chapter}

def receive_annotations(data):
    chapter=require_chapter(data.get('chapter'))
    from annotation_job import proposal
    source=WD/'state'/('annotation-input-'+chapter+'.json')
    result=WD/'state'/('deepseek-state-'+chapter+'.json')
    proposal(WD,source,result)
    return state({'chapter':chapter})

def validate_profile_export_protocol(data,chapter):
    manifest_path=WD/'state'/'analysis-manifest.json'
    protocol=data.get('manifest_protocol')
    if not manifest_path.is_file():
        if protocol is not None:raise ValueError('profile manifest protocol requested but no Mac book ledger exists.')
        return
    manifest=load(manifest_path)
    enabled=manifest.get('profile_manifest_mode_enabled') is True
    if not enabled:
        if protocol is not None:raise ValueError('Mac ledger mode is not enabled; no profile manifest export was made.')
        return
    if type(protocol) is not int or protocol!=1:
        raise ValueError('Mac book ledger is enabled; this client lacks manifest protocol v1. Export stopped before request.')
    from annotation_job import rows
    chapters=[row.get('id') for row in rows(WD/'script.huaben.jsonl') if row.get('type')=='chapter']
    info=analysis_manifest_status(chapters,chapter)
    if not info.get('can_start_profile'):
        raise ValueError('Mac book ledger blocks this profile request: '+'; '.join(info.get('blockers',[])[:4]))

def export_profile(data):
    chapter=require_chapter(data.get('chapter'))
    review=WD/'profile-review.json'
    if review.exists() and load(review).get('status')=='needs_review':raise ValueError('已有未完成的画像审核，请先处理。')
    # The dispatcher holds the book lock around this action. This is the final
    # server-side gate; stale Windows state may not authorize a legacy export.
    validate_profile_export_protocol(data,chapter)
    from profile_job import export
    output=WD/'state'/('profile-input-'+chapter+'.json')
    export(WD,output,chapter)
    return {'remote_file':str(output),'chapter':chapter}

def receive_profile(data):
    chapter=require_chapter(data.get('chapter'))
    manifest_path=WD/'state'/'analysis-manifest.json'
    if manifest_path.is_file() and load(manifest_path).get('profile_manifest_mode_enabled') is True:
        return recover_profile_manifest_review({'chapter':chapter})
    from profile_job import proposal
    source=WD/'state'/('profile-input-'+chapter+'.json')
    result=WD/'state'/('profile-state-'+chapter+'.json')
    proposal(WD,source,result)
    return state({'chapter':chapter})

def recover_profile_manifest_review(data):
    chapter=require_chapter(data.get('chapter'))
    manifest_path=WD/'state'/'analysis-manifest.json'
    if not manifest_path.is_file():raise ValueError('Mac 书级账本不存在，不能恢复画像审核。')
    import analysis_manifest as ledger
    manifest=ledger.load_manifest(manifest_path)
    if manifest.get('book_id')!=BOOK_ID or manifest.get('profile_manifest_mode_enabled') is not True:
        raise ValueError('账本书籍或画像 manifest 模式不匹配。')
    _,item=ledger._chapter(manifest,chapter)
    provenance=item.get('request_provenance') or {};receipt_binding=item.get('receipt_provenance') or {}
    if item.get('status')!='awaiting_review' or provenance.get('kind')!='manifest_reserved':
        raise ValueError('账本没有可恢复的已完成画像审核。')
    receipt_path=(WD/'state'/('profile-state-'+chapter+'.json')).resolve()
    export_path=(WD/'state'/('profile-input-'+chapter+'.json')).resolve()
    if (not receipt_path.is_relative_to(WD.resolve()) or not export_path.is_relative_to(WD.resolve())
            or not receipt_path.is_file() or not export_path.is_file()):
        raise ValueError('账本绑定的画像收据或输入导出缺失。')
    receipt_bytes=receipt_path.read_bytes();receipt_hash=hashlib.sha256(receipt_bytes).hexdigest()
    if (receipt_hash!=receipt_binding.get('receipt_sha256')
            or hashlib.sha256(export_path.read_bytes()).hexdigest()!=provenance.get('job_hash')):
        raise ValueError('画像收据 SHA 或导出 job_hash 与已完成账本 attempt 不匹配。')
    receipt=load(receipt_path)
    if (receipt.get('status')!='complete_needs_review' or receipt.get('manifest_attempt_id')!=provenance.get('attempt_id')
            or receipt.get('book_id')!=BOOK_ID or receipt.get('chapter_id')!=chapter
            or receipt.get('job_hash')!=provenance.get('job_hash')
            or receipt.get('request_body_sha256')!=provenance.get('request_body_sha256')
            or receipt.get('mjs_source_sha256')!=provenance.get('mjs_source_sha256')
            or receipt.get('response_id')!=receipt_binding.get('provider_response_id')):
        raise ValueError('画像收据内容与账本 attempt/response binding 不匹配。')
    review_path=WD/'profile-review.json'
    if review_path.is_file():
        review=load(review_path)
        if (review.get('status')=='needs_review' and review.get('book_id')==BOOK_ID
                and review.get('chapter_id')==chapter and review.get('input_sha256')==provenance.get('job_hash')
                and review.get('summary')==(receipt.get('result') or {}).get('summary')
                and review.get('characters')==(receipt.get('result') or {}).get('characters')):
            return state({'chapter':chapter})
        raise ValueError('现有画像审核不匹配该账本收据，拒绝覆盖。')
    from profile_job import proposal
    proposal(WD,export_path,receipt_path)
    return state({'chapter':chapter})

def quality_report(data):
    chapter=data.get('chapter')
    if chapter is not None:chapter=require_chapter(chapter)
    from quality_report import generate_report
    voice_library=ROOT/'source/src/huashuo/voices/zh'
    book_report=generate_report(WD,book_id=BOOK_ID,voice_library_path=voice_library,detail_limit=50)
    chapter_report=(generate_report(WD,book_id=BOOK_ID,voice_library_path=voice_library,
                                    chapter_id=chapter,detail_limit=50) if chapter is not None else None)
    return {'book':{'overview':book_report['overview'],
                    'chapter_count_comparison':book_report['chapter_count_comparison']},
            'chapter':chapter_report['chapters'][0] if chapter_report and chapter_report['chapters'] else None,
            'note':'机器静态检查仅核对元数据、凭证和文件；不代表已试听或音频质量通过。'}

def dismiss_profile(data):
    path=WD/'profile-review.json'
    if not path.is_file():raise ValueError('没有待审核画像。')
    review=load(path)
    if review.get('status')!='needs_review':raise ValueError('该画像审核已处理。')
    if review.get('book_id')!=BOOK_ID or review.get('chapter_id')!=data.get('chapter'):
        raise ValueError('画像审核不属于当前书籍或章节。')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=data.get('review_sha256'):
        raise ValueError('审核内容已改变，请重新载入。')
    manifest_path=WD/'state'/'analysis-manifest.json'
    if manifest_path.is_file():
        manifest=load(manifest_path)
        ledger_item=next((x for x in manifest.get('chapters',[]) if x.get('chapter_id')==data.get('chapter')),None)
        if manifest.get('profile_manifest_mode_enabled') is True and ledger_item and ledger_item.get('status')=='awaiting_review':
            raise ValueError('画像账本没有丢弃/拒绝转移；待审核章节保持阻塞，未丢弃收据。')
    review['status']='dismissed'
    review['dismissed_at']=datetime.now(timezone.utc).isoformat()
    atomic_json(path,review)
    return state({'chapter':data.get('chapter')})

def commit_profile(data):
    chapter=data.get('chapter');review_path=WD/'profile-review.json'
    review=load(review_path)
    if review.get('book_id')!=BOOK_ID or review.get('chapter_id')!=chapter:raise ValueError('画像审核不属于当前书籍或章节。')
    decisions=data.get('decisions')
    if not isinstance(decisions,dict) or not isinstance(decisions.get('characters'),list):raise ValueError('审核内容无效。')
    manifest_path=WD/'state'/'analysis-manifest.json'
    manifest=load(manifest_path) if manifest_path.is_file() else {}
    if manifest.get('profile_manifest_mode_enabled') is True:
        summary=decisions.get('summary')
        if not isinstance(summary,dict) or summary.get('accept') is not True:
            raise ValueError('画像账本没有拒绝摘要的推进转移；保留当前审核，不提交为拒绝状态。')
    valid={v['id'] for v in voices()}
    for item in decisions['characters']:
        if not isinstance(item,dict):raise ValueError('人物审核内容无效。')
        if item.get('accept') is True and item.get('voice') not in valid:raise ValueError('所选音色不存在。')
    from profile_job import commit
    commit(WD,BOOK_ID,chapter,data.get('review_sha256'),decisions)
    return state({'chapter':chapter})

def create_analysis_manifest(data):
    if data.get('book_id') not in (None,BOOK_ID):raise ValueError('账本书籍编号与当前书籍不符。')
    from annotation_job import rows
    chapters=[row.get('id') for row in rows(WD/'script.huaben.jsonl') if row.get('type')=='chapter']
    legacy=_legacy_profile_blockers(chapters)
    if legacy:raise ValueError('检测到未处理的旧画像任务，账本未创建：'+'；'.join(legacy[:3]))
    from analysis_manifest import create_manifest
    create_manifest(WD/'state'/'analysis-manifest.json',book_id=BOOK_ID,chapters=chapters,
                    estimated_budget_limit=data.get('estimated_budget_limit_cny'),currency='CNY')
    return state({'chapter':data.get('chapter')})

def enable_profile_manifest_mode(data):
    if data.get('confirmed') is not True:raise ValueError('需要明确确认启用画像账本保护。')
    from annotation_job import rows
    chapters=[row.get('id') for row in rows(WD/'script.huaben.jsonl') if row.get('type')=='chapter']
    info=analysis_manifest_status(chapters,data.get('chapter'))
    if not info.get('exists'):raise ValueError('请先创建 Mac 书级预算账本。')
    if not info.get('can_enable'):
        raise ValueError('画像账本模式未通过安全检查：'+'；'.join(info.get('blockers',[])[:4]))
    if data.get('client_ready') is not True or data.get('client_worker_sha256')!=info.get('worker_sha256'):
        raise ValueError('Windows RPC 主机校验或固定 worker hash 未通过，未启用账本模式。')
    from analysis_manifest import _locked_update,_record
    manifest_path=WD/'state'/'analysis-manifest.json'
    def enable(manifest):
        if manifest.get('book_id')!=BOOK_ID or manifest.get('status')!='active':
            raise ValueError('账本书籍或状态不允许启用画像模式。')
        manifest['profile_manifest_mode_enabled']=True
        manifest['profile_manifest_worker_sha256']=info.get('worker_sha256')
        _record(manifest,'profile_manifest_mode_enabled',worker_sha256=info.get('worker_sha256'))
        return manifest
    _locked_update(manifest_path,enable)
    return state({'chapter':data.get('chapter')})

def resume_analysis_manifest(data):
    if data.get('book_id') not in (None,BOOK_ID):raise ValueError('账本书籍编号与当前书籍不符。')
    from analysis_manifest import resume
    resume(WD/'state'/'analysis-manifest.json')
    return state({'chapter':data.get('chapter')})

def advance_analysis_manifest(data):
    chapter=data.get('chapter');manifest_path=WD/'state'/'analysis-manifest.json'
    if not manifest_path.is_file() or load(manifest_path).get('profile_manifest_mode_enabled') is not True:
        raise ValueError('画像账本模式尚未启用。')
    from analysis_manifest import approve_chapter
    approve_chapter(manifest_path,WD,chapter)
    return state({'chapter':chapter})

def approve_line(data):
    path=WD/'annotation-review.json';review=load(path)
    if (review.get('status')!='needs_review' or review.get('book_id')!=BOOK_ID
        or review.get('chapter_id') not in (data.get('chapter'),None)):
        raise ValueError('当前章节没有待审核结果。')
    from annotation_job import review_queue
    queue=review_queue(WD)
    if queue['review_sha256']!=data.get('review_sha256'):
        raise ValueError('审核内容已改变，请重新载入。')
    ident=data.get('id');kind=data.get('kind');speaker=data.get('speaker')
    if ident not in review['decisions'] or kind not in ('dialogue','narration','skip'):raise ValueError('审核内容无效。')
    memory=load(WD/'book-memory.json')
    if kind=='dialogue' and speaker not in memory['characters']:
        raise ValueError('请指定已建立的人物，或将该内容作为叙述审核。')
    edit={'kind':kind,'speaker':speaker if kind=='dialogue' else None,'emotion':str(data.get('emotion',''))[:80]}
    review.setdefault('approved',{})[ident]=edit
    from annotation_job import dump
    dump(path,review)
    return state({'chapter':data['chapter']})

def batch_accept_suggestions(data):
    path=WD/'annotation-review.json'
    if not path.is_file():raise ValueError('没有待审核标注。')
    review=load(path)
    if review.get('status')!='needs_review' or review.get('book_id')!=BOOK_ID:
        raise ValueError('审核不属于当前书籍或已处理。')
    if review.get('chapter_id') not in (data.get('chapter'),None):
        raise ValueError('审核不属于当前章节。')
    from annotation_job import accept_suggestions
    accept_suggestions(WD,data.get('ids'),data.get('review_sha256'))
    return state({'chapter':data.get('chapter')})

def commit_annotations(data):
    from annotation_job import commit
    commit(WD,BOOK)
    return state({'chapter':data.get('chapter')})

def engine(request):
    env=os.environ.copy();path_items=[str(Path(PYTHON).parent),str(release_config.mac_ffmpeg().parent),env.get('PATH','')]
    if Path(HUASHUO).is_absolute():path_items.insert(1,str(Path(HUASHUO).parent))
    env.update({'PATH':os.pathsep.join(x for x in path_items if x),
        'HF_HOME':str(ROOT/'hf'),'HF_HUB_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1'})
    p=subprocess.run([str(PYTHON),str(ROOT/'voice_design_engine.py')],input=json.dumps(request,ensure_ascii=False),
                     cwd=ROOT,env=env,capture_output=True,text=True,timeout=1200)
    if p.returncode: raise RuntimeError((p.stderr+'\n'+p.stdout)[-2500:])

def design(data):
    character=data['character']; description=data['description'].strip(); text=data['text'].strip()
    if character not in load(WD/'book-memory.json')['characters']: raise ValueError('请选择一个角色。')
    if not (2<=len(description)<=300 and 4<=len(text)<=100): raise ValueError('声音描述需 2–300 字，试听台词需 4–100 字。')
    candidate=uuid.uuid4().hex
    folder=ROOT/'designed-voices';folder.mkdir(exist_ok=True)
    wav=folder/(candidate+'.wav')
    seed=int.from_bytes(os.urandom(4),'big')
    engine({'action':'design','description':description,'text':text,'wav':str(wav),'seed':seed})
    (folder/(candidate+'.json')).write_text(json.dumps({'book_id':BOOK_ID,'character':character,'description':description,'text':text,'seed':seed},ensure_ascii=False),encoding='utf-8')
    return {'candidate':candidate,'remote_file':str(wav),'character':character}

def lock_voice(data):
    candidate=data['candidate'];character=data['character']
    if len(candidate)!=32 or any(c not in '0123456789abcdef' for c in candidate): raise ValueError('候选编号无效。')
    folder=ROOT/'designed-voices';meta=load(folder/(candidate+'.json'));wav=folder/(candidate+'.wav')
    if meta.get('book_id')!=BOOK_ID or meta['character']!=character:raise ValueError('该候选属于其他书籍或角色，请重新生成。')
    if data['stamp']!=stamp():raise ValueError('资料已被其他操作修改，请重新载入后再固定声音。')
    name='custom_'+candidate[:16];library=ROOT/'source/src/huashuo/voices/zh'
    vector=library/(name+'.npy')
    engine({'action':'encode','wav':str(wav),'vector':str(vector)})
    metadata={'id':name,'role':'专属音色：'+character,'label':'custom: '+character,'gender':'unknown','age':'unknown',
              'traits':['自设计'],'description':meta['description'],'ref_text':meta['text'],'seed':meta['seed'],
              'made_with':{'voice_design':'local VoiceDesign','speaker_encoder':'mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit','for_model':'mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit'}}
    (library/(name+'.json')).write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    result=save({'stamp':data['stamp'],'character':character,'voice':'library:zh/'+name,'line':None})
    return result

if __name__ == '__main__':
    try:
        action=sys.argv[1]
        data=json.loads(sys.stdin.read() or '{}')
        if action not in ('list_books','import_book'):select_book(data.get('book_id'))
        actions={'state':lambda:state(data),'list_books':list_books,'import_book':lambda:import_book(data),
          'initialize_book':lambda:initialize_book(data),
          'save':lambda:save(data),'preview':lambda:preview(data['character'],data),'render':lambda:render(data),
          'chapter_audio':lambda:chapter_audio(data),
          'asr_spot_check':lambda:asr_spot_check_action(data),
          'confirm_narration_only':lambda:confirm_narration_only(data),
          'design':lambda:design(data),'lock':lambda:lock_voice(data),'add_character':lambda:add_character(data),
          'batch_start':lambda:batch_start(data),'batch_resume':lambda:batch_start(data,True),
          'batch_status':batch_view,'batch_stop':batch_stop,
          'export_annotations':lambda:export_annotations(data),'receive_annotations':lambda:receive_annotations(data),
          'quality_report':lambda:quality_report(data),
          'analysis_manifest_create':lambda:create_analysis_manifest(data),
          'profile_manifest_enable':lambda:enable_profile_manifest_mode(data),
          'analysis_manifest_resume':lambda:resume_analysis_manifest(data),
          'analysis_manifest_advance':lambda:advance_analysis_manifest(data),
          'export_profile':lambda:export_profile(data),'receive_profile':lambda:receive_profile(data),
          'recover_profile_manifest_review':lambda:recover_profile_manifest_review(data),
          'commit_profile':lambda:commit_profile(data),'dismiss_profile':lambda:dismiss_profile(data),
          'approve_line':lambda:approve_line(data),'batch_accept_suggestions':lambda:batch_accept_suggestions(data),
          'commit_annotations':lambda:commit_annotations(data)}
        if action not in ('list_books','import_book','initialize_book','batch_status','batch_stop') and WD.is_dir():
            with (WD/'.console.lock').open('a') as lock:
                try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    if action=='state':
                        from batch_worker import status as batch_status_read
                        batch=batch_status_read(sys.modules[__name__])
                        if batch and batch.get('status') in ('queued','running'):
                            print(json.dumps({'ok':True,'data':state(data)},ensure_ascii=False));sys.exit(0)
                    if action=='chapter_audio':
                        from batch_worker import status as batch_status_read
                        batch=batch_status_read(sys.modules[__name__])
                        completed=batch_allows_chapter_audio_read(batch,BOOK_ID,data.get('chapter'))
                        if completed:
                            print(json.dumps({'ok':True,'data':actions[action]()},ensure_ascii=False));sys.exit(0)
                    raise ValueError('该书正在处理其他操作，请稍候。')
                BOOK_LOCK_FD=lock.fileno()
                recover_console_transaction()
                from annotation_job import recover
                recover(WD)
                from profile_job import recover as recover_profile
                recover_profile(WD)
                result=actions[action]() if action in actions else None
        else:result=actions[action]() if action in actions else None
        if result is None: raise ValueError('未知操作。')
        print(json.dumps({'ok':True,'data':result},ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False))
        sys.exit(1)
