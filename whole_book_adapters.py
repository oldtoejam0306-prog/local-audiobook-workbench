"""Adapt evidence-bound character_fusion snapshots to whole-book plan inputs.

Pure offline transformation. The caller supplies only already-processed chapter
texts and a separately verified set of user-locked voices. Fusion or staging
voice hints are never treated as a user lock.
"""
import copy
import hashlib
import re
import character_fusion

_VOICE=re.compile(r'^(?:preset:[A-Za-z0-9_.-]{1,120}|library:[A-Za-z0-9_-]{1,32}/[A-Za-z0-9_.-]{1,120})$')

def _hash(text):return hashlib.sha256(text.encode('utf-8')).hexdigest()
def _name(value):
    return (isinstance(value,str) and value==value.strip() and 0<len(value)<=60 and '..' not in value
            and not any(x in value for x in '/\\<>|?*') and all(x.isprintable() for x in value))
def _quote(text,value):return isinstance(value,str) and bool(value.strip()) and len(value)<=500 and value in text

def fusion_to_executor(snapshot,chapter_texts,*,user_locked_voices=None):
    """Return executor-compatible characters plus evidence and unresolved conflicts.

    chapter_texts maps exactly the processed chapter IDs to their source text.
    user_locked_voices entries must be explicit, externally verified records:
    {'voice_id': str, 'locked_by': 'user', 'status': 'fixed'}. Caller owns the
    authenticity of those records; this function does not infer consent.
    """
    if not isinstance(snapshot,dict) or snapshot.get('version')!=1 or not _name(snapshot.get('book_id')):raise ValueError('invalid fusion snapshot')
    order=snapshot.get('chapter_order');processed=snapshot.get('processed');entities=snapshot.get('entities');conflicts=snapshot.get('conflicts');relations=snapshot.get('relations')
    if not isinstance(order,list) or not order or any(not _name(x) for x in order) or len(set(order))!=len(order):raise ValueError('invalid chapter order')
    if not isinstance(processed,list) or len(processed)>len(order) or not isinstance(entities,list) or not isinstance(conflicts,list) or not isinstance(relations,list) or not isinstance(chapter_texts,dict):raise ValueError('invalid fusion collections')
    ids=[]
    for i,item in enumerate(processed):
        if not isinstance(item,dict) or item.get('chapter_id')!=order[i]:raise ValueError('processed chapters out of order')
        ident=item['chapter_id'];text=chapter_texts.get(ident)
        if not isinstance(text,str) or not text.strip() or item.get('source_sha256')!=_hash(text):raise ValueError('chapter source hash mismatch')
        ids.append(ident)
    if set(chapter_texts)!=set(ids):raise ValueError('chapter texts must match processed chapters exactly')
    seen_ids=set();seen_names=set();alias_owner={};out=[]
    for ent in entities:
        if not isinstance(ent,dict) or not _name(ent.get('id')) or ent['id'] in seen_ids:raise ValueError('invalid or duplicate entity id')
        name=ent.get('name')
        if (not _name(name) or name.casefold()=='unknown' or name=='__STAGING_UNCONFIRMED__'
            or len(name)==1 or character_fusion._generic(name) or name in seen_names):raise ValueError('invalid or duplicate entity name')
        seen_ids.add(ent['id']);seen_names.add(name)
    for ent in entities:
        name=ent['name'];aliases=ent.get('aliases');mentions=ent.get('mentions')
        if not isinstance(aliases,list) or not isinstance(mentions,list) or not mentions:raise ValueError('entity lacks evidence')
        verified_aliases={};alias_records=[]
        for item in aliases:
            if not isinstance(item,dict):raise ValueError('invalid alias evidence')
            alias=item.get('name');chapter=item.get('chapter_id');quote=item.get('evidence')
            if (not _name(alias) or character_fusion._generic(alias) or alias==name or alias in verified_aliases
                or chapter not in chapter_texts or not _quote(chapter_texts[chapter],quote)
                or not character_fusion._linked(quote,name,alias)):
                raise ValueError('alias is not supported by explicit chapter evidence')
            if alias in seen_names or alias in alias_owner:raise ValueError('alias collides across entities')
            alias_owner[alias]=ent['id'];verified_aliases[alias]=item;alias_records.append(copy.deepcopy(item))
        appearances={};evidence=[];uncertainties=[];last_order=-1
        for position,mention in enumerate(mentions):
            if not isinstance(mention,dict):raise ValueError('invalid mention')
            chapter=mention.get('chapter_id');observed=mention.get('name');quote=mention.get('evidence');basis=mention.get('identity_basis')
            if (chapter not in chapter_texts or not _name(observed) or not _quote(chapter_texts[chapter],quote)
                or observed not in quote or not character_fusion._clear_name_occurrence(chapter_texts[chapter],quote,observed)
                or basis not in ('first_occurrence','explicit_link','same_name_assumption')):
                raise ValueError('mention evidence invalid')
            if order.index(chapter)<last_order:raise ValueError('mentions are out of chapter order')
            last_order=order.index(chapter)
            if (position==0 and (basis!='first_occurrence' or observed!=name)) or (position>0 and basis=='first_occurrence'):
                raise ValueError('mention identity basis is inconsistent')
            if basis=='explicit_link':
                alias=verified_aliases.get(observed)
                if not alias or order.index(alias['chapter_id'])>order.index(chapter):raise ValueError('identity link not yet evidenced')
            elif observed!=name:raise ValueError('mention name does not match canonical entity')
            record={'chapter_id':chapter,'name':observed,'evidence':quote,'identity_basis':basis}
            evidence.append(record);appearances.setdefault(chapter,[]).append(record)
            if basis=='same_name_assumption':uncertainties.append({'kind':'same_name_assumption','chapter_id':chapter,'evidence':quote,'status':'needs_review'})
        if ent.get('first_chapter')!=min(appearances,key=order.index):raise ValueError('first appearance inconsistent')
        claims=ent.get('claims')
        if not isinstance(claims,list):raise ValueError('invalid claims')
        for claim in claims:
            if not isinstance(claim,dict):raise ValueError('invalid claim')
            chap=claim.get('chapter_id');quote=claim.get('evidence');claim_text=claim.get('text')
            names_at_time=[name,*[alias for alias,entry in verified_aliases.items() if chap in order and order.index(entry['chapter_id'])<=order.index(chap)]]
            if (chap not in chapter_texts or not _quote(chapter_texts[chap],quote) or not isinstance(claim_text,str) or not claim_text.strip()
                or claim_text not in quote or not any(n in quote for n in names_at_time) or claim.get('status')!='textual_candidate_needs_review'):
                raise ValueError('claim evidence invalid')
        timeline=[{'chapter_id':c,'mentions':copy.deepcopy(appearances[c])} for c in order if c in appearances]
        out.append({'name':name,'entity_id':ent['id'],'appearance_count':len(appearances),
            'fixed_voice':'','fixed_voice_source':None,'appearance_count_basis':'distinct_chapters_with_recorded_mentions_not_confirmed_identity',
            'aliases':alias_records,'evidence':evidence,'claims':copy.deepcopy(claims),
            'timeline':timeline,'uncertainties':uncertainties})
    for c in conflicts:
        if not isinstance(c,dict) or c.get('chapter_id') not in chapter_texts or not isinstance(c.get('kind'),str):raise ValueError('invalid fusion conflict')
        quote=c.get('evidence')
        if quote is not None and not _quote(chapter_texts[c['chapter_id']],quote):raise ValueError('conflict evidence not in chapter')
    by_id={item['entity_id']:item for item in out}
    for relation in relations:
        if not isinstance(relation,dict):raise ValueError('invalid relation candidate')
        source=relation.get('source_id');target=relation.get('target_id');chapter=relation.get('chapter_id');quote=relation.get('evidence');label=relation.get('label')
        if (source not in by_id or target not in by_id or source==target or chapter not in chapter_texts
            or not _name(label) or not _quote(chapter_texts[chapter],quote) or label not in quote
            or relation.get('status')!='textual_candidate_needs_review'):
            raise ValueError('relation evidence invalid')
        for ident in (source,target):
            item=by_id[ident];names=[item['name'],*[x['name'] for x in item['aliases'] if order.index(x['chapter_id'])<=order.index(chapter)]]
            if not any(name in quote for name in names):raise ValueError('relation name missing from evidence')
    locked={} if user_locked_voices is None else user_locked_voices
    if not isinstance(locked,dict):raise ValueError('user locked voices must be a mapping')
    by_name={x['name']:x for x in out}
    for name,record in locked.items():
        if name not in by_name or not isinstance(record,dict) or set(record)!={'voice_id','locked_by','status'} or record['locked_by']!='user' or record['status']!='fixed':
            raise ValueError('voice lock lacks explicit user-verified identity')
        voice=record['voice_id']
        if not isinstance(voice,str) or not _VOICE.fullmatch(voice) or '..' in voice:raise ValueError('invalid locked voice ID')
        by_name[name]['fixed_voice']=voice;by_name[name]['fixed_voice_source']='user_locked'
    return {'book_id':snapshot['book_id'],'characters':out,'relations':copy.deepcopy(relations),'unresolved_conflicts':copy.deepcopy(conflicts),
            'source_chapters':[{'chapter_id':x['chapter_id'],'source_sha256':x['source_sha256']} for x in processed],
            'note':'Counts are distinct chapters with evidence; same-name identity remains an explicitly marked assumption.'}

def fusion_output_for_executor(adapted):
    """Project an adapter result onto the executor fusion output contract.

    Relations and source provenance remain available to their owning review
    flow. Unresolved identity conflicts are carried into the executor because
    attribution validation uses them to require explicit uncertainty.
    """
    if not isinstance(adapted,dict) or not isinstance(adapted.get('characters'),list):
        raise ValueError('invalid adapted fusion result')
    conflicts=adapted.get('unresolved_conflicts',[])
    if not isinstance(conflicts,list):raise ValueError('invalid unresolved conflicts')
    output={'characters':copy.deepcopy(adapted['characters']),
            'unresolved_conflicts':copy.deepcopy(conflicts)}
    if not output['characters']:
        output['no_characters_detected']=True
    return output
