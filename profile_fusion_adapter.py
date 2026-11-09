"""Adapt completed DeepSeek profile receipts into conservative chapter fusion.

Offline only. The model's descriptions and proposed aliases remain unverified
and never become character facts. Existing output files are never replaced.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re

import character_fusion

_SHA=re.compile(r'^[0-9a-f]{64}$')

def _sha(data):return hashlib.sha256(data).hexdigest()
def _snapshot_hash(snapshot):
    return _sha(json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8'))
def _decode(raw,label):
    if not isinstance(raw,bytes):raise ValueError(label+' must be raw bytes')
    try:value=json.loads(raw.decode('utf-8-sig'))
    except (UnicodeError,json.JSONDecodeError) as exc:raise ValueError(label+' invalid JSON') from exc
    if not isinstance(value,dict):raise ValueError(label+' must be an object')
    return value

def _evidence_reasons(candidate,text):
    evidence=candidate['evidence'];name=candidate['name'];reasons=[]
    if not evidence.strip():reasons.append('evidence_empty')
    if len(evidence)>100:reasons.append('evidence_too_long')
    if name not in evidence:reasons.append('evidence_missing_name')
    if evidence not in text:reasons.append('evidence_not_in_chapter')
    return reasons

def _validated_candidates(receipt,result,text):
    """Partition raw candidates; never trust an excluded record without its raw index."""
    audit=receipt.get('recovery_audit')
    if audit is not None and not isinstance(audit,dict):raise ValueError('invalid recovery audit')
    has_new='excluded_candidates' in receipt;has_old=isinstance(audit,dict) and 'excluded_candidates' in audit
    if not has_new and not has_old:return [(c,'result',None) for c in result['characters']]
    new=receipt.get('excluded_candidates',[]);old=audit.get('excluded_candidates',[]) if isinstance(audit,dict) else []
    if not isinstance(new,list) or not isinstance(old,list) or (has_new and has_old and (new or old)):raise ValueError('ambiguous excluded candidate records')
    records=new if new else old;kind='new' if new else 'recovery' if old else ('new' if has_new else 'recovery')
    raw=receipt.get('raw_response')
    if not isinstance(raw,str):raise ValueError('excluded candidates require raw response')
    raw_result=_decode(raw.encode('utf-8'),'raw response');raw_chars=raw_result.get('characters')
    if not isinstance(raw_chars,list) or len(raw_chars)>100 or raw_result.get('summary')!=result.get('summary'):
        raise ValueError('raw response does not match result')
    expected_excluded=set()
    for i,candidate in enumerate(raw_chars):
        if not isinstance(candidate,dict) or set(candidate)!={'name','aliases','description','evidence'}:raise ValueError('raw candidate structure invalid')
        name=candidate['name'];aliases=candidate['aliases'];description=candidate['description'];evidence=candidate['evidence']
        if (not isinstance(name,str) or not name.strip() or len(name)>60 or not isinstance(aliases,list) or len(aliases)>30
            or any(not isinstance(x,str) or len(x)>60 for x in aliases) or not isinstance(description,str) or len(description)>80 or not isinstance(evidence,str)):
            raise ValueError('raw candidate structure invalid')
        if _evidence_reasons(candidate,text):expected_excluded.add(i)
    excluded={}
    old_reasons={'empty_evidence':'evidence_empty','evidence_over_100_characters':'evidence_too_long',
                 'evidence_missing_character_name':'evidence_missing_name','evidence_not_in_chapter':'evidence_not_in_chapter'}
    for item in records:
        if not isinstance(item,dict) or item.get('status')!='unverified':raise ValueError('excluded candidate status invalid')
        index=item.get('original_index') if kind=='new' else item.get('index')
        if type(index)!=int or not 0<=index<len(raw_chars) or index in excluded:raise ValueError('excluded candidate index invalid or repeated')
        candidate=item.get('candidate')
        if candidate!=raw_chars[index] or not isinstance(candidate,dict) or set(candidate)!={'name','aliases','description','evidence'}:
            raise ValueError('excluded candidate differs from raw response')
        name=candidate['name'];aliases=candidate['aliases'];description=candidate['description'];evidence=candidate['evidence']
        if (not isinstance(name,str) or not name.strip() or len(name)>60 or not isinstance(aliases,list) or len(aliases)>30
            or any(not isinstance(x,str) or len(x)>60 for x in aliases) or not isinstance(description,str) or len(description)>80 or not isinstance(evidence,str)):
            raise ValueError('excluded candidate structure invalid')
        reasons=_evidence_reasons(candidate,text)
        if not reasons:raise ValueError('excluded candidate evidence actually passes')
        if kind=='new':
            if item.get('reasons')!=reasons:raise ValueError('excluded candidate reasons do not match')
        else:
            reason=item.get('reason')
            if not isinstance(reason,str) or old_reasons.get(reason)!=reasons[0]:raise ValueError('recovery exclusion reason does not match')
        excluded[index]=candidate
    if set(excluded)!=expected_excluded:raise ValueError('excluded candidate partition is incomplete or incorrect')
    if kind=='recovery' and 'excluded_candidate_count' in audit and (type(audit['excluded_candidate_count'])!=int or audit['excluded_candidate_count']!=len(excluded)):
        raise ValueError('recovery exclusion count is inconsistent')
    verified=[c for i,c in enumerate(raw_chars) if i not in excluded]
    if verified!=result['characters']:raise ValueError('raw candidates do not partition into result and exclusions')
    return [(candidate,'excluded' if i in excluded else 'result',i) for i,candidate in enumerate(raw_chars)]

def adapt(snapshot,export_bytes,receipt_bytes,source_bytes,context_chars=35):
    if not isinstance(snapshot,dict) or snapshot.get('version')!=1:raise ValueError('invalid fusion snapshot')
    if type(context_chars)!=int or not 0<=context_chars<=200:raise ValueError('invalid context length')
    export=_decode(export_bytes,'export');receipt=_decode(receipt_bytes,'receipt')
    book=export.get('book_id');chapter=export.get('chapter_id');text=export.get('chapter_text');source_sha=export.get('source_sha256')
    if (book!=snapshot.get('book_id') or not isinstance(chapter,str) or not chapter
        or not isinstance(text,str) or not text.strip() or not isinstance(source_sha,str) or not _SHA.fullmatch(source_sha)):
        raise ValueError('export book/chapter/text/source hash invalid')
    if not isinstance(source_bytes,bytes) or _sha(source_bytes)!=source_sha:raise ValueError('source bytes do not match export hash')
    try:source_text=source_bytes.decode('utf-8').replace('\r\n','\n').replace('\r','\n')
    except UnicodeError as exc:raise ValueError('source is not UTF-8') from exc
    start=source_text.find(text)
    if start<0 or source_text.find(text,start+1)>=0:raise ValueError('chapter text is missing or ambiguous in source')
    job_sha=_sha(export_bytes)
    if (receipt.get('status')!='complete_needs_review' or receipt.get('job_hash')!=job_sha
        or receipt.get('book_id')!=book or receipt.get('chapter_id')!=chapter):
        raise ValueError('receipt does not match the exported job')
    result=receipt.get('result')
    if not isinstance(result,dict) or not isinstance(result.get('characters'),list):raise ValueError('receipt has no profile candidates')
    processed=snapshot.get('processed');order=snapshot.get('chapter_order')
    if not isinstance(processed,list) or not isinstance(order,list) or len(processed)>=len(order) or order[len(processed)]!=chapter:
        raise ValueError('chapter must follow snapshot order')
    meta=snapshot.get('profile_adapter')
    if meta is None:
        if processed:raise ValueError('cannot attach an unbound pre-existing snapshot')
        history=[]
    else:
        if not isinstance(meta,dict) or meta.get('version')!=1 or meta.get('source_sha256')!=source_sha or not isinstance(meta.get('chapters'),list):
            raise ValueError('source book or adapter metadata changed')
        history=meta['chapters']
        if len(history)!=len(processed):raise ValueError('adapter history and snapshot disagree')
        for index,(entry,prior) in enumerate(zip(history,processed)):
            if (not isinstance(entry,dict) or not isinstance(prior,dict) or prior.get('chapter_id')!=order[index]
                or entry.get('chapter_id')!=prior.get('chapter_id') or entry.get('source_sha256')!=source_sha
                or entry.get('chapter_text_sha256')!=prior.get('source_sha256')):
                raise ValueError('processed chapter provenance changed')
    source_candidates=_validated_candidates(receipt,result,text)
    relations=result.get('relations',[])
    if not isinstance(relations,list) or any(not isinstance(r,dict) for r in relations):raise ValueError('invalid relation candidates')
    candidates=[];quarantine=[];stats={'candidates':len(source_candidates),'result_candidates':len(result['characters']),
         'excluded_candidates':sum(kind=='excluded' for _,kind,_ in source_candidates),
         'excluded_name_occurrence_fallback':0,'excluded_name_absent':0,'exact_evidence':0,'name_occurrence_fallback':0,
         'name_absent':0,'aliases_proposed':0,'descriptions_unverified':0}
    for index,(item,candidate_source,original_index) in enumerate(source_candidates):
        if not isinstance(item,dict):raise ValueError('invalid character candidate')
        name=item.get('name');evidence=item.get('evidence');description=item.get('description');aliases=item.get('aliases')
        if (not isinstance(name,str) or not name.strip() or len(name.strip())>60 or not isinstance(evidence,str)
            or not isinstance(description,str) or not isinstance(aliases,list) or any(not isinstance(x,str) for x in aliases)):
            raise ValueError('profile candidate fields invalid')
        name=name.strip();stats['aliases_proposed']+=len(aliases)
        if description.strip():stats['descriptions_unverified']+=1
        if candidate_source=='result' and evidence.strip() and len(evidence)<=500 and evidence in text and name in evidence:
            quote=evidence;basis='model_evidence_exact';stats['exact_evidence']+=1
        else:
            pos=text.find(name)
            if pos<0:
                quote='';basis='name_absent';stats['name_absent']+=1
                if candidate_source=='excluded':stats['excluded_name_absent']+=1
            else:
                quote=text[max(0,pos-context_chars):min(len(text),pos+len(name)+context_chars)]
                basis='name_occurrence_only';stats['name_occurrence_fallback']+=1
                if candidate_source=='excluded':stats['excluded_name_occurrence_fallback']+=1
        candidates.append({'name':name,'evidence':quote,'aliases':[] if candidate_source=='excluded' else [{'name':alias,'evidence':evidence} for alias in aliases]})
        quarantine.append({'candidate_index':index,'original_index':original_index,'candidate_source':candidate_source,'name':name,'evidence_basis':basis,
            'occurrence_evidence':quote or None,'description':{'text':description,'status':'model_unverified'},
            'aliases':[{'name':x,'status':'model_unverified_until_explicit_source_link'} for x in aliases],
            'model_evidence_exact':bool(evidence and evidence in text),
            'note':'Occurrence supports only presence of these characters in this chapter; identity, description and aliases require separate evidence.'})
    # Relation indices refer to result.characters, while source_candidates may
    # interleave excluded raw candidates. Remap without ever pointing at one.
    result_positions=[i for i,(_,kind,_) in enumerate(source_candidates) if kind=='result']
    fusion_relations=[]
    for relation in relations:
        mapped=copy.deepcopy(relation)
        for field in ('source_index','target_index'):
            index=relation.get(field)
            mapped[field]=result_positions[index] if type(index)==int and 0<=index<len(result_positions) else None
        fusion_relations.append(mapped)
    before_conflicts=len(snapshot.get('conflicts',[]))
    fused=character_fusion.fuse_chapter(snapshot,chapter,text,candidates,relations=fusion_relations)
    new_conflicts=fused['conflicts'][before_conflicts:]
    stats['fusion_conflicts']=len(new_conflicts)
    stats['relations_proposed']=len(relations)
    stats['relations_unresolved']=sum(c.get('kind')=='relation_unresolved' for c in new_conflicts)
    stats['unresolved_aliases']=sum(c.get('kind') in ('generic_or_invalid_alias','alias_not_proven','alias_collision') for c in new_conflicts)
    entry={'chapter_id':chapter,'source_sha256':source_sha,'chapter_text_sha256':_sha(text.encode('utf-8')),
           'input_snapshot_sha256':_snapshot_hash(snapshot),'export_sha256':job_sha,
           'receipt_sha256':_sha(receipt_bytes),'stats':stats,'model_unverified':quarantine,
           'model_unverified_relations':[{'candidate':copy.deepcopy(r),'status':'model_unverified'} for r in relations]}
    fused['profile_adapter']={'version':1,'source_sha256':source_sha,'chapters':copy.deepcopy(history)+[entry]}
    return fused

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot',type=Path);parser.add_argument('export',type=Path);parser.add_argument('receipt',type=Path);parser.add_argument('source',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args()
    try:
        if args.output.resolve() in {args.snapshot.resolve(),args.export.resolve(),args.receipt.resolve(),args.source.resolve()}:raise ValueError('output must be a new file')
        snapshot=json.loads(args.snapshot.read_text(encoding='utf-8-sig'));out=adapt(snapshot,args.export.read_bytes(),args.receipt.read_bytes(),args.source.read_bytes())
        with args.output.open('x',encoding='utf-8') as f:json.dump(out,f,ensure_ascii=False,indent=2);f.write('\n')
    except (OSError,ValueError,KeyError,TypeError) as exc:parser.exit(2,'error: '+str(exc)+'\n')
if __name__=='__main__':main()
