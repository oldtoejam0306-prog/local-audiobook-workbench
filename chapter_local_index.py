"""Immutable in-memory chapter index for offline research; no file access.

All source/script/cast hashes must be authenticated by the caller. This module
binds but does not prove raw-source-to-normalized-text derivation. Build once
from a trusted snapshot; subsequent extraction uses only retained immutable
chapter records and a caller-supplied current snapshot identity. It does not
observe external files or replace a lock/TOCTOU check, and does not render audio.
"""
from __future__ import annotations
import hashlib,json,re
from dataclasses import dataclass
from typing import Any,Mapping

_HASH=re.compile(r'^[0-9a-f]{64}$')

def _json(value: Any)->bytes:
    try:return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
    except (TypeError,ValueError,UnicodeError) as exc:raise ValueError('invalid JSON data') from exc

def _sha(value:bytes)->str:return hashlib.sha256(value).hexdigest()
def object_sha256(value:Any)->str:return _sha(_json(value))

@dataclass(frozen=True)
class ChapterEntry:
    chapter_id:str
    original_start:int
    original_end:int
    text:str
    original_rows_json:bytes
    chapter_digest:str
    text_sha256:str

@dataclass(frozen=True)
class ChapterIndex:
    book_id:str
    source_sha256:str
    text_sha256:str
    script_sha256:str
    cast_sha256:str
    index_sha256:str
    header_json:bytes
    cast_json:bytes
    chapters:tuple[ChapterEntry,...]


def build_index(*,book_id:str,source_bytes:bytes,normalized_text:str,script_rows:list,cast:dict,
                trusted_snapshot:Mapping[str,Any])->ChapterIndex:
    """Validate whole snapshot once; hashes are byte/source and canonical JSON.

    trusted_snapshot exactly {book_id,source_sha256,text_sha256,script_sha256,
    cast_sha256}; caller authenticates this snapshot and parser provenance.
    Unknown dialogue remains unknown. Unlocated/overlapping blocks fail closed.
    """
    fields={'book_id','source_sha256','text_sha256','script_sha256','cast_sha256'}
    if not isinstance(book_id,str) or not book_id.strip() or not isinstance(trusted_snapshot,Mapping) or set(trusted_snapshot)!=fields or trusted_snapshot.get('book_id')!=book_id:raise ValueError('invalid trusted snapshot')
    if any(not isinstance(trusted_snapshot[k],str) or not _HASH.fullmatch(trusted_snapshot[k]) for k in fields-{'book_id'}):raise ValueError('invalid snapshot hash')
    if not isinstance(source_bytes,bytes) or not source_bytes or _sha(source_bytes)!=trusted_snapshot['source_sha256']:raise ValueError('source hash mismatch')
    if not isinstance(normalized_text,str) or not normalized_text or '\r' in normalized_text:raise ValueError('invalid normalized text')
    text_bytes=normalized_text.encode('utf8',errors='strict')
    if _sha(text_bytes)!=trusted_snapshot['text_sha256'] or object_sha256(script_rows)!=trusted_snapshot['script_sha256'] or object_sha256(cast)!=trusted_snapshot['cast_sha256']:raise ValueError('text/script/cast snapshot mismatch')
    if not isinstance(script_rows,list) or len(script_rows)<2 or not isinstance(script_rows[0],dict) or script_rows[0].get('type')!='huaben' or script_rows[0].get('text_sha256')!=trusted_snapshot['text_sha256']:raise ValueError('script header mismatch')
    if not isinstance(cast,dict) or not isinstance(cast.get('narrator'),dict) or not isinstance(cast['narrator'].get('voice'),str) or not cast['narrator']['voice'] or not isinstance(cast.get('characters'),dict):raise ValueError('invalid cast')
    seen=set();end=0;groups=[];active=None
    for row in script_rows[1:]:
        if not isinstance(row,dict):raise ValueError('invalid script row')
        ident=row.get('id');kind=row.get('type');src=row.get('src');body=row.get('text')
        if not isinstance(ident,str) or not ident or ident in seen:raise ValueError('duplicate or invalid block ID')
        seen.add(ident)
        if kind not in ('chapter','heading','narration','dialogue','skip','break','equation','figure','table','footnote'):raise ValueError('unsupported block type')
        if not isinstance(src,list) or len(src)!=2 or any(type(x) is not int for x in src):raise ValueError('missing source range')
        start,stop=src
        if not 0<=end<=start<stop<=len(normalized_text) or normalized_text[end:start].strip() or not isinstance(body,str) or normalized_text[start:stop]!=body:raise ValueError('source range, ordering, text or coverage mismatch')
        end=stop
        if kind=='chapter':
            active={'id':ident,'rows':[],'start':start};groups.append(active)
        if active is None:raise ValueError('first block must be a chapter')
        if kind=='dialogue':
            who=row.get('speaker')
            if not isinstance(who,str) or not who or (who!='unknown' and who not in cast['characters']):raise ValueError('dialogue speaker missing from cast')
        active['rows'].append(row)
    if normalized_text[end:].strip() or not groups:raise ValueError('uncovered source text')
    if normalized_text[:groups[0]['start']].strip():raise ValueError('uncovered prefix')
    records=[]
    for i,group in enumerate(groups):
        start=0 if i==0 else group['start'];stop=groups[i+1]['start'] if i+1<len(groups) else len(normalized_text)
        raw=_json(group['rows']);chapter_text=normalized_text[start:stop];records.append(ChapterEntry(group['id'],start,stop,chapter_text,raw,_sha(raw),_sha(chapter_text.encode('utf8'))))
    summary={'book_id':book_id,'source_sha256':trusted_snapshot['source_sha256'],'text_sha256':trusted_snapshot['text_sha256'],
             'script_sha256':trusted_snapshot['script_sha256'],'cast_sha256':trusted_snapshot['cast_sha256'],
             'header_sha256':_sha(_json(script_rows[0])),'chapters':[{'id':r.chapter_id,'start':r.original_start,'end':r.original_end,'chapter_digest':r.chapter_digest,'text_sha256':r.text_sha256} for r in records]}
    return ChapterIndex(book_id, trusted_snapshot['source_sha256'],trusted_snapshot['text_sha256'],trusted_snapshot['script_sha256'],trusted_snapshot['cast_sha256'],object_sha256(summary),_json(script_rows[0]),_json(cast),tuple(records))


def extract_chapter(index:ChapterIndex,chapter_id:str,*,current_snapshot:Mapping[str,Any],expected_index_sha256:str)->dict:
    """Extract chapter-local copies; caller must supply independently current identity.

    This function does not reread external files; detecting external drift is the
    caller's responsibility. Source offsets are Unicode codepoints, not bytes.
    """
    if not isinstance(index,ChapterIndex) or not isinstance(current_snapshot,Mapping):raise ValueError('invalid index or snapshot')
    expected={'book_id':index.book_id,'source_sha256':index.source_sha256,'text_sha256':index.text_sha256,'script_sha256':index.script_sha256,'cast_sha256':index.cast_sha256}
    summary={**expected,'header_sha256':_sha(index.header_json),'chapters':[{'id':r.chapter_id,'start':r.original_start,'end':r.original_end,'chapter_digest':r.chapter_digest,'text_sha256':r.text_sha256} for r in index.chapters]}
    if (dict(current_snapshot)!=expected or expected_index_sha256!=index.index_sha256
            or object_sha256(summary)!=index.index_sha256 or _sha(index.cast_json)!=index.cast_sha256):raise ValueError('source snapshot or index identity changed')
    matches=[entry for entry in index.chapters if entry.chapter_id==chapter_id]
    if len(matches)!=1:raise ValueError('chapter is missing or duplicated')
    entry=matches[0]
    if _sha(entry.original_rows_json)!=entry.chapter_digest or _sha(entry.text.encode('utf8'))!=entry.text_sha256:raise ValueError('chapter index corrupted')
    rows=json.loads(entry.original_rows_json);cast=json.loads(index.cast_json);original_header=json.loads(index.header_json)
    local_text_sha=_sha(entry.text.encode('utf8'));local_header=dict(original_header)
    local_header['text_sha256']=local_text_sha;local_header['text']='text.txt'
    if 'source' in local_header:local_header['source']={'format':'TXT','path':'text.txt','sha256':local_text_sha}
    mapping=[];local=[];speakers=set();uncertainty=[]
    for row in rows:
        original=list(row['src']);relative=[original[0]-entry.original_start,original[1]-entry.original_start]
        if not 0<=relative[0]<relative[1]<=len(entry.text) or entry.text[relative[0]:relative[1]]!=row['text']:raise ValueError('chapter range mapping invalid')
        row['src']=relative;local.append(row);mapping.append({'id':row['id'],'original_src':original,'local_src':relative})
        if row.get('type')=='dialogue':
            speaker=row['speaker']
            if speaker=='unknown':uncertainty.append({'id':row['id'],'kind':'unknown_speaker','status':'unconfirmed'})
            else:speakers.add(speaker)
            for key in ('identity_status','identity_uncertainty'):
                if key in row:uncertainty.append({'id':row['id'],'kind':key,'value':row[key]})
    local_cast={'version':cast.get('version',1),'narrator':cast['narrator'],'characters':{name:cast['characters'][name] for name in sorted(speakers)}}
    return {'book_id':index.book_id,'chapter_id':chapter_id,'source_sha256':index.source_sha256,'text_sha256':index.text_sha256,
            'script_sha256':index.script_sha256,'cast_sha256':index.cast_sha256,'index_sha256':index.index_sha256,
            'chapter_digest':entry.chapter_digest,'chapter_original_range':[entry.original_start,entry.original_end],
            'local_text_sha256':local_text_sha,'normalized_text':entry.text,'script_rows':[local_header,*local],
            'cast':local_cast,'offset_mapping':mapping,'uncertainty':uncertainty,'human_reviewed':False,
            'limitations':['caller must detect external source/index changes under a trusted lock',
                           'chapter slicing equivalence to full Huashuo render is not yet verified']}
