"""Export book annotation jobs, validate proposals and commit reviewed decisions."""
import argparse,copy,hashlib,json,os,shutil,subprocess,sys
from datetime import datetime,timezone
from pathlib import Path
import release_config

ROOT=release_config.mac_data_root()
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def load(path):return json.loads(path.read_text(encoding='utf-8-sig'))
def dump(path,obj):
    tmp=path.with_name(path.name+'.tmp')
    with tmp.open('w',encoding='utf-8') as f:
        json.dump(obj,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
def rows(path):return [json.loads(s) for s in path.read_text(encoding='utf-8-sig').splitlines() if s.strip()]
def write_rows(path,data):
    tmp=path.with_name(path.name+'.tmp')
    with tmp.open('w',encoding='utf-8') as f:
        for row in data:f.write(json.dumps(row,ensure_ascii=False)+'\n')
        f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def chapter_digest(data, chapter_id):
    selected=[]; current=None
    for row in data:
        if row.get('type')=='chapter':current=row.get('id')
        if current==chapter_id:selected.append(row)
    if not selected:raise ValueError('审核章节不存在。')
    return hashlib.sha256(json.dumps(selected,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def record_chapter_review(wd,review):
    chapter=review.get('chapter_id')
    script_rows=rows(wd/'script.huaben.jsonl')
    chapters=[chapter] if chapter else [r['id'] for r in script_rows if r.get('type')=='chapter']
    if not chapters:return
    path=wd/'state'/'chapter-reviews.json'
    records=load(path) if path.exists() else {}
    wanted=set(chapters);grouped={};current=None
    for row in script_rows:
        if row.get('type')=='chapter':current=row.get('id')
        if current in wanted:grouped.setdefault(current,[]).append(row)
    for ident in chapters:
        records[ident]={'script_digest':chapter_digest(grouped.get(ident,[]),ident),'book_id':review['book_id'],
                        'reviewed_by':review.get('reviewed_by','user')}
    dump(path,records)

def export(wd,out,chapter_id=None,allow_empty_cast=False):
    script=wd/'script.huaben.jsonl';text=wd/'text.txt';memory=load(wd/'book-memory.json')
    chars=memory.get('characters',{})
    if not chars and not allow_empty_cast:raise ValueError('请先确认人物档案；不会自动将空人物表送去标注。')
    all_rows=rows(script)
    chapter_ids=[r['id'] for r in all_rows if r.get('type')=='chapter']
    if chapter_id is not None and chapter_id not in chapter_ids:raise ValueError('章节不存在。')
    selected=[];current=None
    for row in all_rows:
        if row.get('type')=='chapter':current=row['id']
        if chapter_id is None or current==chapter_id:selected.append(row)
    prior=set(chapter_ids[:chapter_ids.index(chapter_id)]) if chapter_id is not None else set(chapter_ids)
    source_text=text.read_text(encoding='utf-8')
    if chapter_id is not None:
        chapters=[r for r in all_rows if r.get('type')=='chapter']
        idx=chapter_ids.index(chapter_id)
        start=chapters[idx].get('src',[None])[0]
        end=chapters[idx+1].get('src',[None])[0] if idx+1<len(chapters) else len(source_text)
        if not isinstance(start,int) or not isinstance(end,int) or not 0<=start<end<=len(source_text):
            raise ValueError('章节位置无效，无法安全限制上传范围。')
        selected=copy.deepcopy(selected)
        for row in selected:
            if 'src' in row:
                a,b=row['src']
                if not start<=a<=b<=end:raise ValueError('段落超出章节范围，停止导出。')
                row['src']=[a-start,b-start]
        source_text=source_text[start:end]
    data={'version':1,'book_id':memory['book_id'],'chapter_id':chapter_id,'text':source_text,
      'source_sha256':sha(text),'script_sha256':sha(script),'memory_sha256':sha(wd/'book-memory.json'),'blocks':selected,
      'characters':{name:{'aliases':c.get('aliases',[]),'description':c.get('description','')} for name,c in chars.items()},
      'chapter_summaries':[x for x in memory.get('chapter_summaries',[]) if x.get('status')=='approved' and x.get('chapter_id') in prior]}
    dump(out,data)
    return {'exported':str(out),'book_id':data['book_id'],'quotes':sum(x.get('type')=='dialogue' for x in data['blocks']),
            'approved_summaries':len(data['chapter_summaries'])}

def proposal(wd,source,state_path):
    data=load(source);state=load(state_path)
    if sha(source)!=state.get('job_hash'):raise ValueError('结果不属于此任务输入。')
    if state.get('status')!='complete_needs_review':raise ValueError('标注任务尚未完成。')
    if (sha(wd/'text.txt')!=data['source_sha256'] or sha(wd/'script.huaben.jsonl')!=data['script_sha256']
        or sha(wd/'book-memory.json')!=data['memory_sha256']):
        raise ValueError('书稿、话本或人物档案已改变，请重新导出任务。')
    if load(wd/'book-memory.json').get('book_id')!=data['book_id']:raise ValueError('人物档案不属于此书。')
    decisions={}
    for batch in state['batches'].values():
        if batch.get('status')!='done':raise ValueError('存在未完成批次。')
        for k,v in batch.get('results',{}).items():
            if k in decisions:raise ValueError('重复的段落编号。')
            decisions[k]=v
    expected={r['id'] for r in data['blocks'] if r.get('type')=='dialogue'}
    if set(decisions)!=expected:raise ValueError('标注结果和对白编号不一致。')
    existing=wd/'annotation-review.json'
    if existing.exists() and load(existing).get('status')=='needs_review':
        raise ValueError('已有未完成的审核，请先处理。')
    review={'version':1,'book_id':data['book_id'],'chapter_id':data.get('chapter_id'),'input_sha256':sha(source),'script_sha256':data['script_sha256'],
            'source_sha256':data['source_sha256'],'memory_sha256':data['memory_sha256'],
            'status':'needs_review','decisions':decisions,'approved':{},'note':'所有机器标注需审核；unknown 默认不自动分给人物。'}
    out=wd/'annotation-review.json';dump(out,review)
    return {'review_file':str(out),'count':len(decisions),'unknown':sum(v.get('speaker')=='unknown' and v.get('kind')!='narration' for v in decisions.values()),'status':'needs_review'}

def _current_review(wd):
    path=wd/'annotation-review.json'; review=load(path); memory=load(wd/'book-memory.json')
    if (review.get('status')!='needs_review' or sha(wd/'script.huaben.jsonl')!=review['script_sha256']
        or sha(wd/'text.txt')!=review['source_sha256'] or sha(wd/'book-memory.json')!=review['memory_sha256']
        or memory.get('book_id')!=review['book_id']):
        raise ValueError('待审结果已过期或书籍不匹配。')
    return path,review,memory

def _suggestion(decision,characters):
    if not isinstance(decision,dict):return None
    kind=decision.get('kind'); speaker=decision.get('speaker')
    if kind=='dialogue' and isinstance(speaker,str) and speaker!='unknown' and speaker in characters:
        result={'kind':'dialogue','speaker':speaker}
        if decision.get('emotion'):result['emotion']=str(decision['emotion'])[:80]
        return result
    if kind=='narration':return {'kind':'narration'}
    return None

def review_queue(wd):
    path,review,memory=_current_review(wd)
    script={r.get('id'):r for r in rows(wd/'script.huaben.jsonl') if r.get('id')}
    queue=[]
    for ident,decision in review['decisions'].items():
        row=script.get(ident)
        if not row or row.get('type')!='dialogue':raise ValueError('审核段落无效。')
        suggestion=_suggestion(decision,memory.get('characters',{}))
        queue.append({'id':ident,'text':row.get('text',''),'decision':decision,
                      'approved':ident in review.get('approved',{}),
                      'requires_individual_review':suggestion is None})
    queue.sort(key=lambda x:(x['approved'],not x['requires_individual_review']))
    return {'review_sha256':sha(path),'items':queue,
            'note':'未提供可靠置信度；排序仅优先显示 unknown 或无效建议，所有建议仍需用户确认。'}

def accept_suggestions(wd,ids,expected_review_sha256):
    path,review,memory=_current_review(wd)
    if not isinstance(expected_review_sha256,str) or sha(path)!=expected_review_sha256:
        raise ValueError('审核快照已改变，请重新查看。')
    if not isinstance(ids,list) or not ids or any(not isinstance(i,str) or not i for i in ids) or len(set(ids))!=len(ids):
        raise ValueError('请明确选择互不重复的段落编号。')
    decisions=review['decisions'];approved=review.get('approved',{})
    script={r.get('id'):r for r in rows(wd/'script.huaben.jsonl') if r.get('id')}
    updates={}
    for ident in ids:
        if ident not in decisions or ident in approved or script.get(ident,{}).get('type')!='dialogue':
            raise ValueError(f'{ident}: 段落不存在或已审核。')
        suggestion=_suggestion(decisions[ident],memory.get('characters',{}))
        if suggestion is None:raise ValueError(f'{ident}: 建议需单独审核，不能批量接受。')
        updates[ident]=suggestion
    review['approved']={**approved,**updates};dump(path,review)
    return {'accepted':len(updates),'review_sha256':sha(path),'remaining':len(decisions)-len(review['approved'])}

def recover(wd):
    txn=wd/'state'/'annotation-transaction.json'
    if not txn.exists():return False
    t=load(txn)
    if t.get('status')!='applying':return False
    backup=Path(t['backup'])
    if not backup.is_dir() or not backup.is_relative_to(wd/'annotation-backups'):raise ValueError('恢复备份路径无效。')
    for item in ('script.huaben.jsonl','cast.json','ingest.json'):
        src=backup/item;dst=wd/'state'/'ingest.json' if item=='ingest.json' else wd/item
        if not src.exists():raise ValueError('恢复备份不完整，停止处理。')
        shutil.copy2(src,dst)
    t['status']='restored_after_interruption';dump(txn,t)
    return True

def commit(wd,book):
    recovered=recover(wd)
    review_path=wd/'annotation-review.json';review=load(review_path);memory=load(wd/'book-memory.json')
    script=wd/'script.huaben.jsonl';cast=wd/'cast.json';ingest=wd/'state'/'ingest.json'
    txn=wd/'state'/'annotation-transaction.json'
    if txn.exists():
        old=load(txn)
        if (old.get('status')=='committed' and review.get('status')=='needs_review'
            and old.get('review_sha256')==sha(review_path) and old.get('script_sha256')==sha(script)
            and old.get('cast_sha256')==sha(cast) and old.get('ingest_sha256')==sha(ingest)):
            record_chapter_review(wd,review)
            review['status']='committed';review['committed_script_sha256']=sha(script);dump(review_path,review)
            return {'committed':len(review.get('approved',{})),'recovered_completion':True,'backup':old['backup']}
    if (review.get('status')!='needs_review' or sha(script)!=review['script_sha256']
        or sha(wd/'text.txt')!=review['source_sha256'] or sha(wd/'book-memory.json')!=review['memory_sha256']
        or memory.get('book_id')!=review['book_id']):
        raise ValueError('待审结果已过期或书籍不匹配。')
    decisions=review['decisions'];approved=review.get('approved',{})
    if set(approved)!=set(decisions):raise ValueError('仍有段落未审核，未更改话本。')
    data=rows(script);lookup={r.get('id'):r for r in data if r.get('id')}
    for ident,edit in approved.items():
        if ident not in lookup or lookup[ident].get('type')!='dialogue' or not isinstance(edit,dict):raise ValueError('审核段落无效。')
        kind=edit.get('kind');speaker=edit.get('speaker');row=lookup[ident]
        if kind not in ('dialogue','narration','skip'):raise ValueError(f'{ident}: 类型无效。')
        if kind=='dialogue' and speaker not in memory['characters'] and not (speaker=='unknown' and edit.get('allow_unknown') is True):
            raise ValueError(f'{ident}: 说话人未确认。')
        row['type']=kind
        if kind=='dialogue':
            row['speaker']=speaker
            if edit.get('emotion'):row['emotion']=str(edit['emotion'])[:80]
            else:row.pop('emotion',None)
        else:
            row.pop('speaker',None);row.pop('emotion',None)
            if kind=='skip':row['reason']='reviewed skip'
    backup=wd/'annotation-backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');backup.mkdir(parents=True)
    for src in (script,cast,ingest):shutil.copy2(src,backup/src.name)
    review_hash=sha(review_path)
    dump(txn,{'status':'applying','backup':str(backup),'review_sha256':review_hash})
    try:
        oldcast=load(cast);oldcast['narrator']=memory['narrator'];oldcast['characters']=memory['characters'];dump(cast,oldcast)
        opt=load(ingest);opt.setdefault('options',{})['voices']='multi';dump(ingest,opt)
        write_rows(script,data)
        p=subprocess.run([str(ROOT/'.venv/bin/huashuo'),'check',str(book),'--workdir',str(wd)],capture_output=True,text=True)
        if p.returncode:raise ValueError('话本检查失败：'+(p.stderr or p.stdout)[-1200:])
        dump(txn,{'status':'committed','backup':str(backup),'script_sha256':sha(script),'cast_sha256':sha(cast),
                  'ingest_sha256':sha(ingest),'review_sha256':review_hash})
    except Exception:
        recover(wd);raise
    record_chapter_review(wd,review)
    review['status']='committed';review['committed_script_sha256']=sha(script);dump(review_path,review)
    return {'committed':len(approved),'backup':str(backup),'recovered_previous_transaction':recovered}

def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='cmd',required=True)
    x=sub.add_parser('export');x.add_argument('workdir',type=Path);x.add_argument('output',type=Path);x.add_argument('--chapter-id')
    x=sub.add_parser('proposal');x.add_argument('workdir',type=Path);x.add_argument('input',type=Path);x.add_argument('state',type=Path)
    x=sub.add_parser('commit');x.add_argument('workdir',type=Path);x.add_argument('book',type=Path)
    x=sub.add_parser('recover');x.add_argument('workdir',type=Path)
    x=sub.add_parser('review-queue');x.add_argument('workdir',type=Path)
    x=sub.add_parser('accept-suggestions');x.add_argument('workdir',type=Path);x.add_argument('--review-sha256',required=True);x.add_argument('ids',nargs='+')
    args=p.parse_args()
    args.workdir=args.workdir.resolve()
    try:
        result=export(args.workdir,args.output,args.chapter_id) if args.cmd=='export' else proposal(args.workdir,args.input,args.state) if args.cmd=='proposal' else commit(args.workdir,args.book) if args.cmd=='commit' else review_queue(args.workdir) if args.cmd=='review-queue' else accept_suggestions(args.workdir,args.ids,args.review_sha256) if args.cmd=='accept-suggestions' else {'recovered':recover(args.workdir)}
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (OSError,ValueError,KeyError,json.JSONDecodeError) as e:p.exit(2,'error: '+str(e)+'\n')
if __name__=='__main__':main()
