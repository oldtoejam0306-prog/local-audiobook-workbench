"""Offline validation and conservative fusion for the chapter-map v1 protocol.

This module does not dispatch requests, recover charges, or authorize retries.
A provider receipt with uncertain billing must be reconciled outside this module.
Offsets in evidence are Unicode code-point indices into the exported chapter text.
"""
import copy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

import character_fusion

def _strict_object(properties):
    return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
_OFFSET_EVIDENCE_SCHEMA=_strict_object({'start':{'type':'integer','minimum':0},'end':{'type':'integer','minimum':1},
                                 'quote':{'type':'string','minLength':1,'maxLength':500}})
_QUOTE_EVIDENCE_SCHEMA={'type':'object','properties':{'quote':{'type':'string','minLength':1,'maxLength':500},
    'context':{'type':'string','minLength':1,'maxLength':1500},'occurrence':{'type':'integer','minimum':0}},
    'required':['quote'],'additionalProperties':False}
_EVIDENCE_SCHEMA={'anyOf':[_OFFSET_EVIDENCE_SCHEMA,_QUOTE_EVIDENCE_SCHEMA]}
_ALIAS_SCHEMA=_strict_object({'name':{'type':'string','minLength':1,'maxLength':60},'evidence':_EVIDENCE_SCHEMA})
_VOICE_SCHEMA=_strict_object({'kind':{'enum':['source_explicit','model_inference']},
    'description':{'type':'string','minLength':1,'maxLength':200},'evidence':{'anyOf':[_EVIDENCE_SCHEMA,{'type':'null'}]}})
_CHARACTER_SCHEMA=_strict_object({'name':{'type':'string','minLength':1,'maxLength':60},'evidence':_EVIDENCE_SCHEMA,
    'aliases':{'type':'array','maxItems':30,'items':_ALIAS_SCHEMA},'voice':{'type':'array','maxItems':20,'items':_VOICE_SCHEMA}})
_RELATION_SCHEMA=_strict_object({'source_index':{'type':'integer','minimum':0},'target_index':{'type':'integer','minimum':0},
    'label':{'type':'string','minLength':1,'maxLength':60},'evidence':_EVIDENCE_SCHEMA})
CHAPTER_MAP_SCHEMA=_strict_object({'schema_version':{'const':'chapter-map-v1'},
    'characters':{'type':'array','maxItems':100,'items':_CHARACTER_SCHEMA},
    'relations':{'type':'array','maxItems':300,'items':_RELATION_SCHEMA},
    'summary':_strict_object({'text':{'type':'string','minLength':1,'maxLength':300},'evidence':_EVIDENCE_SCHEMA})})

_SHA=re.compile(r'^[0-9a-f]{64}$')
class InvalidChapterMap(ValueError):pass
class BillingUnknown(InvalidChapterMap):pass
class BudgetExceeded(InvalidChapterMap):pass

def _hash(b):return hashlib.sha256(b).hexdigest()
def _unique(pairs):
    out={}
    for key,value in pairs:
        if key in out:raise InvalidChapterMap('duplicate JSON key')
        out[key]=value
    return out
def _json(raw,label):
    if not isinstance(raw,bytes):raise InvalidChapterMap(label+' must be bytes')
    try:v=json.loads(raw.decode('utf-8-sig'),object_pairs_hook=_unique)
    except (UnicodeError,json.JSONDecodeError) as e:raise InvalidChapterMap(label+' is invalid JSON') from e
    if not isinstance(v,dict):raise InvalidChapterMap(label+' must be an object')
    return v
def _obj(value,keys,label):
    if not isinstance(value,dict) or set(value)!=set(keys):raise InvalidChapterMap(label+' has invalid fields')
    return value
def _text(value,label,limit):
    if not isinstance(value,str) or not value.strip() or len(value)>limit:raise InvalidChapterMap(label+' invalid')
    return value.strip()
def _money(v,label):
    if isinstance(v,bool) or not isinstance(v,(str,int,float)):raise BillingUnknown(label+' invalid')
    try:x=Decimal(str(v))
    except InvalidOperation as e:raise BillingUnknown(label+' invalid') from e
    if not x.is_finite() or x<0:raise BillingUnknown(label+' invalid')
    return x
def _positions(source,quote):
    found=[];start=0
    while True:
        index=source.find(quote,start)
        if index<0:return found
        found.append(index);start=index+1

def _evidence(value,source,label):
    if not isinstance(value,dict) or 'quote' not in value:raise InvalidChapterMap(label+' has invalid fields')
    q=value['quote']
    if not isinstance(q,str) or not q.strip() or len(q)>500:raise InvalidChapterMap(label+' invalid quote')
    keys=set(value)
    if keys=={'start','end','quote'}:
        a,b=value['start'],value['end']
        if type(a)!=int or type(b)!=int or not 0<=a<b<=len(source) or source[a:b]!=q:
            raise InvalidChapterMap(label+' is not an exact contiguous source quote')
        return {'quote':q,'start':a,'end':b,'status':'resolved','method':'provided_offsets'}
    if not keys<={'quote','context','occurrence'}:raise InvalidChapterMap(label+' has invalid fields')
    matches=_positions(source,q)
    if not matches:raise InvalidChapterMap(label+' quote not present in source')
    context=value.get('context');occurrence=value.get('occurrence')
    if 'context' in value:
        if not isinstance(context,str) or not context.strip() or len(context)>1500 or q not in context:
            raise InvalidChapterMap(label+' invalid context')
        contexts=_positions(source,context)
        if not contexts:raise InvalidChapterMap(label+' context not present in source')
    else:contexts=None
    if 'occurrence' in value and (type(occurrence)!=int or not 0<=occurrence<len(matches)):
        raise InvalidChapterMap(label+' occurrence out of range')
    selected=None;method=None
    if contexts is not None:
        inside=_positions(context,q)
        if len(contexts)==1 and len(inside)==1:
            selected=contexts[0]+inside[0];method='unique_context'
        elif occurrence is not None:
            pick=matches[occurrence]
            if not any(c<=pick and pick+len(q)<=c+len(context) for c in contexts):
                raise InvalidChapterMap(label+' occurrence does not match context')
            selected=pick;method='context_and_occurrence'
    elif occurrence is not None:selected=matches[occurrence];method='model_selected_occurrence'
    elif len(matches)==1:selected=matches[0];method='unique_quote'
    if selected is not None and occurrence is not None and matches[occurrence]!=selected:
        raise InvalidChapterMap(label+' occurrence contradicts context')
    if selected is None:return {'quote':q,'start':None,'end':None,'status':'unresolved_ambiguous','occurrences':len(matches)}
    return {'quote':q,'start':selected,'end':selected+len(q),'status':'resolved','method':method}

def validate_receipt(export_bytes,receipt_bytes):
    export=_json(export_bytes,'export');receipt=_json(receipt_bytes,'receipt')
    if receipt.get('status')!='complete_needs_review':raise BillingUnknown('request not complete; do not resend automatically')
    if (receipt.get('job_hash')!=_hash(export_bytes) or receipt.get('book_id')!=export.get('book_id')
        or receipt.get('chapter_id')!=export.get('chapter_id')):raise InvalidChapterMap('receipt/export identity mismatch')
    if not isinstance(receipt.get('response_id'),str) or not receipt['response_id'].strip():raise BillingUnknown('provider response ID missing')
    usage=receipt.get('usage')
    if not isinstance(usage,dict):raise BillingUnknown('provider usage missing')
    i,o=usage.get('prompt_tokens'),usage.get('completion_tokens')
    if type(i)!=int or type(o)!=int or i<0 or o<0:raise BillingUnknown('provider token usage invalid')
    if 'total_tokens' in usage and (type(usage['total_tokens'])!=int or usage['total_tokens']!=i+o):raise BillingUnknown('provider total tokens inconsistent')
    reservation=receipt.get('budget_reservation')
    if not isinstance(reservation,dict) or reservation.get('kind')!='estimated_peak' or reservation.get('currency')!='CNY' or reservation.get('scope')!='single_request' or reservation.get('billing_status')!='unverified':
        raise BillingUnknown('request reservation is missing or invalid')
    reserved=_money(reservation.get('amount_cny'),'reservation');budget=_money(receipt.get('budget_cny'),'budget')
    estimate=Decimal(i*2+o*8)/Decimal(1000000)
    if _money(receipt.get('estimated_peak_cost_cny'),'estimate')!=estimate:raise BillingUnknown('provider usage and recorded estimate disagree')
    if reserved<=0 or budget<=0 or reserved>budget or estimate>budget:raise BudgetExceeded('reservation or observed estimate exceeds budget; no automatic retry')
    requested_model=receipt.get('requested_model')
    if not isinstance(requested_model,str) or not requested_model.strip() or receipt.get('model')!=requested_model:
        raise InvalidChapterMap('requested model identity missing or inconsistent')
    if receipt.get('finish_reason')=='length':raise InvalidChapterMap('provider response truncated; no automatic retry')
    request_hash=receipt.get('request_body_sha256')
    if not isinstance(request_hash,str) or not _SHA.fullmatch(request_hash):raise InvalidChapterMap('request body hash missing')
    raw=receipt.get('raw_response')
    if not isinstance(raw,str):raise InvalidChapterMap('raw response missing')
    response=_json(raw.encode('utf-8'),'response')
    if response!=receipt.get('result'):raise InvalidChapterMap('parsed result differs from raw provider response')
    return export,receipt,response,{'reservation_cny':str(reserved),'budget_cny':str(budget),
        'observed_estimate_cny':str(estimate),'billing_status':'unverified','provider_response_id':receipt['response_id'],
        'input_tokens':i,'output_tokens':o,'requested_model':requested_model,'actual_model':receipt.get('actual_model'),
        'finish_reason':receipt.get('finish_reason'),'request_body_sha256':request_hash,
        'request_body_hash_verification':'not_verified_by_offline_adapter'}

def validate_map(result,text):
    _obj(result,('schema_version','characters','relations','summary'),'chapter map')
    if result['schema_version']!='chapter-map-v1':raise InvalidChapterMap('unsupported chapter-map version')
    chars=result['characters'];relations=result['relations']
    if not isinstance(chars,list) or len(chars)>100 or not isinstance(relations,list) or len(relations)>300:raise InvalidChapterMap('invalid candidate lists')
    fused=[];voice=[];character_evidence=[];relation_evidence=[];unresolved=[]
    def resolve(evidence,label):
        result=_evidence(evidence,text,label)
        if result['status']!='resolved':unresolved.append({'field':label,'evidence':result})
        return result
    for i,c in enumerate(chars):
        _obj(c,('name','evidence','aliases','voice'),'character')
        name=_text(c['name'],'name',60);ev=resolve(c['evidence'],f'characters[{i}].evidence');quote=ev['quote']
        if name not in quote:raise InvalidChapterMap('name evidence does not include name')
        if not isinstance(c['aliases'],list) or len(c['aliases'])>30 or not isinstance(c['voice'],list) or len(c['voice'])>20:raise InvalidChapterMap('invalid alias or voice lists')
        aliases=[];alias_evidence=[]
        for j,item in enumerate(c['aliases']):
            _obj(item,('name','evidence'),'alias');alias=_text(item['name'],'alias',60);ae=resolve(item['evidence'],f'characters[{i}].aliases[{j}].evidence');q=ae['quote']
            if alias not in q or name not in q:raise InvalidChapterMap('alias evidence must include both names')
            aliases.append({'name':alias,'evidence':q if ae['status']=='resolved' else ''});alias_evidence.append({'name':alias,'evidence':ae})
        for j,item in enumerate(c['voice']):
            _obj(item,('kind','description','evidence'),'voice')
            desc=_text(item['description'],'voice description',200);kind=item['kind'];raw=item['evidence']
            if kind not in ('source_explicit','model_inference'):raise InvalidChapterMap('invalid voice kind')
            ve=None if raw is None else resolve(raw,f'characters[{i}].voice[{j}].evidence');q=None if ve is None else ve['quote']
            if kind=='source_explicit' and (not q or name not in q or desc not in q):raise InvalidChapterMap('explicit acoustic description must occur with name in source quote')
            if q is not None and name not in q:raise InvalidChapterMap('voice evidence must contain name')
            status=('model_inference' if kind=='model_inference' else 'evidence_unresolved' if ve['status']!='resolved' else 'source_text_candidate_needs_review')
            voice.append({'candidate_index':i,'name':name,'kind':kind,'description':desc,'evidence':ve,'status':status})
        fused.append({'name':name,'evidence':quote if ev['status']=='resolved' else '', 'aliases':aliases})
        character_evidence.append({'name':name,'evidence':ev,'aliases':alias_evidence})
    rels=[]
    for i,r in enumerate(relations):
        _obj(r,('source_index','target_index','label','evidence'),'relation')
        a,b=r['source_index'],r['target_index'];label=_text(r['label'],'relation label',60);ev=resolve(r['evidence'],f'relations[{i}].evidence');quote=ev['quote']
        if (type(a)!=int or type(b)!=int or not 0<=a<len(chars) or not 0<=b<len(chars) or a==b
            or chars[a]['name'].strip() not in quote or chars[b]['name'].strip() not in quote or label not in quote):raise InvalidChapterMap('relation evidence or endpoints invalid')
        rels.append({'source_index':a,'target_index':b,'label':label,'evidence':quote if ev['status']=='resolved' else ''})
        relation_evidence.append({'source_index':a,'target_index':b,'label':label,'evidence':ev})
    summary=_obj(result['summary'],('text','evidence'),'summary');summary_text=_text(summary['text'],'summary',300)
    se=resolve(summary['evidence'],'summary.evidence')
    return fused,rels,voice,{'text':summary_text,'evidence':se,'status':'model_unverified' if se['status']=='resolved' else 'evidence_unresolved'}, {'characters':character_evidence,'relations':relation_evidence},unresolved

def validate_and_fuse(snapshot,export_bytes,receipt_bytes,source_bytes):
    export,receipt,result,accounting=validate_receipt(export_bytes,receipt_bytes)
    text=export.get('chapter_text');chapter=export.get('chapter_id');source_sha=export.get('source_sha256')
    if (not isinstance(snapshot,dict) or export.get('book_id')!=snapshot.get('book_id')
        or not isinstance(chapter,str) or not chapter or not isinstance(text,str) or not text.strip()
        or not isinstance(source_sha,str) or not _SHA.fullmatch(source_sha) or not isinstance(source_bytes,bytes)
        or _hash(source_bytes)!=source_sha):raise InvalidChapterMap('book, chapter or source hash mismatch')
    try:source=source_bytes.decode('utf-8').replace('\r\n','\n').replace('\r','\n')
    except UnicodeError as exc:raise InvalidChapterMap('source is not UTF-8') from exc
    start=source.find(text)
    if start<0 or source.find(text,start+1)>=0:raise InvalidChapterMap('chapter text not unique in source')
    meta=snapshot.get('chapter_map_v1');processed=snapshot.get('processed')
    if not isinstance(processed,list):raise InvalidChapterMap('invalid snapshot history')
    if meta is None:
        if processed:raise InvalidChapterMap('snapshot has no chapter-map source binding')
        history=[]
    else:
        if not isinstance(meta,dict) or meta.get('source_sha256')!=source_sha or not isinstance(meta.get('chapters'),list):raise InvalidChapterMap('chapter-map source changed')
        history=meta['chapters']
        if len(history)!=len(processed):raise InvalidChapterMap('chapter-map history mismatch')
        for entry,old in zip(history,processed):
            if entry.get('chapter_id')!=old.get('chapter_id') or entry.get('chapter_sha256')!=old.get('source_sha256'):raise InvalidChapterMap('chapter-map prior hash mismatch')
    candidates,relations,voices,summary,evidence,unresolved=validate_map(result,text)
    fused=character_fusion.fuse_chapter(snapshot,chapter,text,candidates,relations)
    snap_hash=_hash(json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
    chapter_record={'chapter_id':chapter,'chapter_sha256':_hash(text.encode()),'input_snapshot_sha256':snap_hash,
        'export_sha256':_hash(export_bytes),'receipt_sha256':_hash(receipt_bytes),'accounting':accounting,
        'source_evidence':evidence,'unresolved_evidence':unresolved,
        'voice_candidates':voices,'summary_candidate':summary,'status':'needs_review'}
    fused['chapter_map_v1']={'source_sha256':source_sha,'chapters':copy.deepcopy(history)+[chapter_record]}
    return fused
