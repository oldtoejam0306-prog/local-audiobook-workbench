"""Safely prepare local books for Huashuo; no model calls and no network."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import release_config
import re
import shutil
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

ROOT=release_config.mac_data_root()/'books'
CHAPTER=re.compile(r'^\s*(?:第[零〇一二三四五六七八九十百千万两0-9]+[章回节卷](?:\s|[：:、.．　]|$)|(?:chapter|chapter\s+)\s*[0-9ivxlcdm]+(?:\s|[.:]|$)|(?:序章|序言|楔子|尾声|后记|终章)\s*$)',re.I)
SCENE=re.compile(r'^[\s※*＊·•—–=_~～◇◆]+$')
W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'

def atomic_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.replace(tmp,path)

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def decode_txt(path):
    raw=path.read_bytes()
    if raw.startswith((b'\xff\xfe',b'\xfe\xff')):return raw.decode('utf-16'),'utf-16'
    if raw.startswith(b'\xef\xbb\xbf'):return raw.decode('utf-8-sig'),'utf-8-sig'
    for enc in ('utf-8','gb18030'):
        try:return raw.decode(enc),enc
        except UnicodeError:pass
    raise ValueError('无法识别文本编码，请转换为 UTF-8 后重试。')

def docx_paragraphs(path):
    with zipfile.ZipFile(path) as z:
        info=z.getinfo('word/document.xml')
        if info.file_size>200*1024*1024:raise ValueError('DOCX 主文档过大，停止处理。')
        with z.open(info) as f:
            for event,elem in ET.iterparse(f,events=('end',)):
                if elem.tag!=W+'p':continue
                parts=[]
                for node in elem.iter():
                    if node.tag==W+'t' and node.text:parts.append(node.text)
                    elif node.tag==W+'tab':parts.append('\t')
                    elif node.tag in (W+'br',W+'cr'):parts.append('\n')
                text=''.join(parts)
                if text.strip():yield text
                elem.clear()

def prepare(source):
    source=source.resolve(strict=True)
    if not source.is_file():raise ValueError('源路径不是文件。')
    if source.stat().st_size>200*1024*1024:raise ValueError('源文件超过 200 MiB，停止处理。')
    ext=source.suffix.lower()
    if ext not in ('.txt','.docx','.epub'):raise ValueError('只支持 TXT、DOCX 和 EPUB。')
    sha=digest(source);book_id='book-'+sha[:20]
    root=ROOT/book_id
    if root.exists():
        record=json.loads((root/'book.json').read_text(encoding='utf-8'))
        if record['source_sha256']!=sha:raise ValueError('书籍标识冲突，停止写入。')
        return {'book_id':book_id,'path':str(root),'reused':True,'record':record}
    ROOT.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT,prefix='.prepare-') as t:
        staging=Path(t)/book_id;staging.mkdir()
        raw=staging/('original'+ext);shutil.copy2(source,raw)
        if digest(raw)!=sha:raise ValueError('源文件复制后校验失败。')
        warnings=[];encoding=None;count=None;headings=None
        if ext=='.txt':text,encoding=decode_txt(raw)
        elif ext=='.docx':
            paragraphs=list(docx_paragraphs(raw));text='\n\n'.join(paragraphs)+'\n'
            warnings.append('DOCX 提取正文段落和表格文字；图片、脚注、批注、复杂格式及修订标记尚未完整覆盖，需核对。')
        else:text=None
        if text is not None:
            normalized=staging/'book.txt';normalized.write_text(text,encoding='utf-8')
            headings=[line.strip() for line in text.splitlines() if CHAPTER.match(line) and len(line.strip())<=120]
            count=len(text)
            if not headings:warnings.append('未识别到明确章节标题，需手动核对章节。')
            normalized_name='book.txt'
        else:
            normalized_name=raw.name;warnings.append('EPUB 章节、顺序和非正文过滤需导入后核对。')
        record={'version':1,'book_id':book_id,'original_name':source.name,'source_sha256':sha,
                'format':ext,'encoding':encoding,'characters':count,'chapter_count_candidates':None if headings is None else len(headings),
                'chapter_candidates':headings,'input_file':normalized_name,'warnings':warnings}
        atomic_json(staging/'book.json',record)
        os.replace(staging,root)
    return {'book_id':book_id,'path':str(root),'reused':False,'record':record}

def _verified_front_matter(workdir,rows,first_explicit):
    # Keep the container chapter: downstream chapter grouping discards ungrouped rows.
    if not rows or rows[0].get('type')!='huaben' or rows[0].get('title')!='book':return None
    if first_explicit<4 or rows[1].get('type')!='chapter' or rows[1].get('text')!='book':return None
    prefix=rows[1:first_explicit]
    if any(r.get('type')=='chapter' for r in prefix[1:]):return None
    titles=[];authors=[]
    for row in prefix[1:]:
        value=row.get('text')
        if not isinstance(value,str):return None
        title_match=re.match(r'^书名[：:](.*)$',value)
        author_match=re.match(r'^作者[：:](.*)$',value)
        if title_match:titles.append(title_match.group(1).strip())
        if author_match:authors.append(author_match.group(1).strip())
    if len(authors)!=1 or not authors[0]:return None
    if titles:
        if len(titles)!=1 or not titles[0]:return None
    else:
        metadata=workdir.parent/'book.json'
        if not metadata.is_file():return None
        name=json.loads(metadata.read_text(encoding='utf-8-sig')).get('original_name')
        if not isinstance(name,str) or Path(name).name!=name:return None
        def clean_title(value):
            value=value.strip()
            if value.startswith('《') and value.endswith('》'):value=value[1:-1].strip()
            return value
        expected=clean_title(Path(name).stem)
        matches=[r for r in prefix[1:] if clean_title(r.get('text',''))==expected]
        if not expected or len(matches)!=1:return None
    if authors[0]!=rows[0].get('author','').strip():return None
    text=(workdir/'text.txt').read_text(encoding='utf-8')
    end=0
    for row in prefix:
        src=row.get('src'); value=row.get('text')
        if (not isinstance(src,list) or len(src)!=2 or any(type(x)!=int for x in src)
            or not isinstance(value,str)):return None
        a,b=src
        if not 0<=end<=a<b<=len(text) or text[end:a].strip() or text[a:b]!=value:return None
        end=b
    src=rows[first_explicit].get('src')
    if (not isinstance(src,list) or len(src)!=2 or any(type(x)!=int for x in src)
        or not end<=src[0]<src[1]<=len(text) or text[end:src[0]].strip()
        or text[src[0]:src[1]]!=rows[first_explicit].get('text')):return None
    return rows[1]['id']

def _candidate_promotions(workdir,rows):
    metadata=workdir.parent/'book.json'
    if not metadata.exists():return []
    record=json.loads(metadata.read_text(encoding='utf-8-sig'))
    candidates=record.get('chapter_candidates');count=record.get('chapter_count_candidates')
    if (not isinstance(candidates,list) or not candidates or type(count)!=int or count!=len(candidates)
        or any(not isinstance(x,str) or not CHAPTER.match(x) for x in candidates)):
        raise ValueError('章节候选清单无效，未修改话本。')
    text=(workdir/'text.txt').read_text(encoding='utf-8')
    source_name=record.get('input_file')
    if (not isinstance(source_name,str) or Path(source_name).name!=source_name
        or not rows or rows[0].get('type')!='huaben'
        or record.get('format') not in ('.txt','.docx','.epub')
        or digest(workdir.parent/('original'+record['format']))!=record.get('source_sha256')
        or rows[0].get('source',{}).get('sha256')!=digest(workdir.parent/source_name)
        or rows[0].get('text_sha256')!=digest(workdir/'text.txt')):
        raise ValueError('章节候选来源或文本哈希不匹配。')
    extracted=[];offset=0
    for line in text.splitlines(keepends=True):
        value=line.strip()
        if CHAPTER.match(line) and len(value)<=120:
            start=offset+len(line)-len(line.lstrip());end=start+len(value)
            extracted.append((value,start,end))
        offset+=len(line)
    source_text=(workdir.parent/source_name).read_text(encoding='utf-8-sig')
    source_candidates=[line.strip() for line in source_text.splitlines() if CHAPTER.match(line) and len(line.strip())<=120]
    if source_candidates!=candidates:raise ValueError('章节候选与来源清单不一致。')
    if len(extracted)!=len(candidates) or any(
        actual!=original and actual!=unicodedata.normalize('NFKC',original)
        for original,(actual,_,_) in zip(candidates,extracted)):
        raise ValueError('章节候选与原文清单不一致。')
    last_end=0
    for row in rows[1:]:
        src=row.get('src')
        if isinstance(src,list) and len(src)==2 and all(type(x)==int for x in src):
            if not 0<=last_end<=src[0]<=src[1]<=len(text):raise ValueError('话本原文位置无效或重叠。')
            last_end=src[1]
    found=[];last_index=-1;cursor=1
    for value,a,b in extracted:
        while cursor<len(rows):
            src=rows[cursor].get('src')
            if not isinstance(src,list) or len(src)!=2 or any(type(x)!=int for x in src):
                cursor+=1;continue
            if src[1]>a:break
            cursor+=1
        start=cursor;parts=[];end=a
        while cursor<len(rows):
            row=rows[cursor];src=row.get('src')
            if not isinstance(src,list) or len(src)!=2 or any(type(x)!=int for x in src):break
            x,y=src
            if x>=b:break
            if (not a<=x<y<=b or x<end or text[end:x].strip()
                or row.get('type') not in ('chapter','narration','dialogue','skip')
                or row.get('text')!=text[x:y]):raise ValueError('章节候选块无法安全拼合。')
            if row.get('type')=='chapter' and 'level' in row and (type(row['level'])!=int or row['level'] not in (1,2)):
                raise ValueError('章节层级无效。')
            parts.append(row);end=y;cursor+=1
        if not parts or end!=b or text[a:b]!=value or start<=last_index:
            raise ValueError('章节候选与话本未完全匹配。')
        if any(r.get('type')=='skip' for r in parts):
            if (len(parts)!=1 or parts[0].get('reason')!='notes'
                or set(parts[0])-{'id','type','text','src','reason'}):
                raise ValueError('跳过块不能安全提升为章节。')
        if len(parts)==1 and parts[0].get('type')=='dialogue':raise ValueError('完整对白块不能自动提升为章节。')
        if len(parts)>1:
            if any(r.get('type')=='chapter' and (type(r.get('level'))!=int or r['level'] not in (1,2)) for r in parts):raise ValueError('拆分标题的章节层级无效。')
            allowed={'id','type','text','src','speaker','level'}
            if any(set(r)-allowed for r in parts) or any(r.get('type')=='dialogue' and r.get('speaker')!='unknown' for r in parts):
                raise ValueError('拆分标题含有额外标注，不能自动合并。')
            if any(r.get('type')=='chapter' for r in parts[1:]):raise ValueError('拆分标题含有多个章节块。')
        last_index=cursor-1;found.append((value,a,b,parts))
    return found

def _require_pristine_for_merge(workdir):
    state=workdir/'state';script=workdir/'script.huaben.jsonl';auto=state/'script.auto.jsonl'
    if not auto.is_file() or digest(auto)!=digest(script):raise ValueError('话本并非原始导入状态，不能合并标题。')
    allowed_root={'script.huaben.jsonl','text.txt','cast.json','pron.txt','book-memory.json','state','logs','.console.lock'}
    allowed_state={'script.auto.jsonl','cast.auto.json','ingest.json'}
    if any(p.name not in allowed_root for p in workdir.iterdir()) or any(p.name not in allowed_state for p in state.iterdir()):
        raise ValueError('存在审核、生成记录或未知文件，不能合并标题。')
    memory=workdir/'book-memory.json'
    if memory.exists():
        data=json.loads(memory.read_text(encoding='utf-8-sig'))
        if any(data.get(k) for k in ('characters','chapter_summaries','corrections')):
            raise ValueError('人物档案含审核或纠错记录，不能合并标题。')
    logs=workdir/'logs'
    if logs.exists():
        for log in logs.iterdir():
            if not log.is_file() or log.suffix!='.log' or any(not ('huashuo import ' in line or 'safe_huashuo_import.py import ' in line) for line in log.read_text(encoding='utf-8').splitlines() if line.strip()):
                raise ValueError('存在非导入日志，不能合并标题。')

def repair(workdir):
    path=workdir/'script.huaben.jsonl'
    rows=[json.loads(s) for s in path.read_text(encoding='utf-8-sig').splitlines() if s.strip()]
    chapter_rows=[r for r in rows if r.get('type')=='chapter']
    explicit=[r for r in chapter_rows if CHAPTER.match(r.get('text',''))]
    if not explicit:raise ValueError('未发现明确的编号章节，未修改话本；请人工确认章节。')
    front_id=None
    if chapter_rows[0]['id'] != explicit[0]['id']:
        first_explicit=next(i for i,r in enumerate(rows) if r is explicit[0])
        front_id=_verified_front_matter(workdir,rows,first_explicit)
        if front_id is None:raise ValueError('首个章节不是明确的编号标题，需人工确认前言后再修正。')
    candidates=_candidate_promotions(workdir,rows)
    if any(len(parts)>1 or parts[0].get('type')=='skip' for _,_,_,parts in candidates):
        _require_pristine_for_merge(workdir)
        ids=[r.get('id') for r in rows if r.get('type')!='huaben']
        if any(not isinstance(i,str) or not i for i in ids) or len(set(ids))!=len(ids):raise ValueError('话本段落编号无效或重复。')
        last_end=0
        for r in rows[1:]:
            src=r.get('src')
            if not isinstance(src,list) or len(src)!=2 or any(type(x)!=int for x in src) or not 0<=last_end<=src[0]<=src[1]:raise ValueError('话本原文位置无效或重叠。')
            last_end=src[1]
    changes=[];removed_ids=set()
    for value,a,b,parts in candidates:
        row=parts[0]
        if len(parts)>1:
            removed=parts[1:];removed_ids.update(r['id'] for r in removed)
            changes.append({'id':row['id'],'original':row['text'],'new_type':'chapter',
                            'merged_text':value,'src':[a,b],'removed_blocks':copy.deepcopy(removed)})
            row['text']=value;row['src']=[a,b];row.pop('speaker',None)
        elif row['type']=='skip':
            changes.append({'id':row['id'],'original':row['text'],'new_type':'chapter','original_block':copy.deepcopy(row)})
            row.pop('reason',None)
        elif row['type']!='chapter':
            changes.append({'id':row['id'],'original':row['text'],'new_type':'chapter'})
        else:continue
        row['type']='chapter';row['level']=row.get('level',1)
    if removed_ids:rows=[r for r in rows if r.get('id') not in removed_ids]

    for row in chapter_rows:
        if row.get('id')==front_id or CHAPTER.match(row.get('text','')):continue
        original=row['text'];kind='skip' if SCENE.fullmatch(original.strip()) else 'narration'
        row['type']=kind;row.pop('level',None)
        if kind=='skip':row['reason']='scene separator'
        changes.append({'id':row['id'],'original':original,'new_type':kind})
    if changes or front_id:
        backup=workdir/'state'/'script.before-strict-chapters.jsonl'
        if backup.exists():raise ValueError('章节修正备份已存在，拒绝覆盖；请先检查工作目录。')
        shutil.copy2(path,backup)
        tmp=path.with_name(path.name+'.tmp')
        tmp.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
        os.replace(tmp,path)
    result={'explicit_chapters':len(explicit)+sum(x['new_type']=='chapter' for x in changes),'changes':changes,'front_matter_chapter':front_id,
            'requires_review':bool(changes or front_id)}
    atomic_json(workdir/'state'/'chapter-review.json',result)
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('prepare');p.add_argument('source',type=Path)
    p=sub.add_parser('repair-chapters');p.add_argument('workdir',type=Path)
    args=parser.parse_args()
    try:result=prepare(args.source) if args.cmd=='prepare' else repair(args.workdir)
    except (OSError,ValueError,KeyError,zipfile.BadZipFile,ET.ParseError) as e:parser.exit(2,'error: '+str(e)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
