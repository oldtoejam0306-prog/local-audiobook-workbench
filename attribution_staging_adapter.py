"""Convert one chapter's machine attribution into isolated reversible render input.

Pure in-memory only; never writes a workdir, memory, cast or audio. The caller
must first verify the attribution receipt belongs to the captured book/chapter
and input snapshot. A model-proposed name remains an unconfirmed attribution.
"""
import copy
import hashlib
import json
import re

import annotation_job
import staging_render_cast as staging
import delivery_policy

_SHA=re.compile(r'^[0-9a-f]{64}$')

def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()
def _text_hash(value):return hashlib.sha256(value.encode('utf-8')).hexdigest()

def _chapter(rows,chapter_id):
    if not isinstance(rows,list) or not isinstance(chapter_id,str) or not chapter_id:raise ValueError('invalid chapter input')
    seen=set();chapters=0;active=None;indices=[]
    for index,row in enumerate(rows):
        if not isinstance(row,dict):raise ValueError('invalid script row')
        ident=row.get('id')
        if ident is not None:
            if not isinstance(ident,str) or not ident or ident in seen:raise ValueError('invalid or duplicate script block ID')
            seen.add(ident)
        if row.get('type')=='chapter':
            active=ident
            if ident==chapter_id:chapters+=1
        if active==chapter_id:indices.append(index)
    if chapters!=1 or not indices:raise ValueError('chapter not uniquely present')
    return indices

def capture_binding(book_id,chapter_id,script_rows):
    """Capture before dispatch; store separately with trusted task provenance."""
    if not isinstance(book_id,str) or not book_id.strip():raise ValueError('book ID required')
    indices=_chapter(script_rows,chapter_id);selected=[script_rows[i] for i in indices];texts={}
    for row in selected:
        if row.get('type')=='dialogue':
            if not isinstance(row.get('id'),str) or not isinstance(row.get('text'),str) or not row['text'].strip():raise ValueError('invalid dialogue')
            texts[row['id']]=_text_hash(row['text'])
    return {'version':1,'book_id':book_id,'chapter_id':chapter_id,
            'chapter_sha256':annotation_job.chapter_digest(script_rows,chapter_id),'dialogue_text_sha256':texts}

def adapt_attribution(book_id,chapter_id,script_rows,cast,attribution,binding,*,fallback_voice=None):
    """Validate complete attribution against a prior binding and build staging data."""
    actual=capture_binding(book_id,chapter_id,script_rows)
    if not isinstance(binding,dict) or binding!=actual:raise ValueError('captured book/chapter/text binding mismatch')
    if not isinstance(attribution,dict) or set(attribution)!={'assignments','unknown_ids','conflicts'}:
        raise ValueError('invalid attribution output fields')
    assignments=attribution['assignments'];unknown=attribution['unknown_ids'];conflicts=attribution['conflicts']
    if not isinstance(assignments,list) or not isinstance(unknown,list) or not isinstance(conflicts,list):raise ValueError('invalid attribution lists')
    by_id={}
    for item in assignments:
        if not isinstance(item,dict) or not {'id','speaker'}<=set(item) or set(item)-{'id','speaker','identity_status','identity_uncertainty','delivery'}:raise ValueError('invalid assignment fields')
        ident=item['id'];speaker=item['speaker']
        if not isinstance(ident,str) or not ident or ident in by_id or not isinstance(speaker,str) or not speaker:
            raise ValueError('invalid or duplicate assignment')
        status=item.get('identity_status')
        if status is not None and status not in ('confirmed','unconfirmed','unknown','ambiguous','conflict','model_inference','same_name_assumption'):
            raise ValueError('invalid identity status')
        uncertainty=item.get('identity_uncertainty')
        if uncertainty is not None and (not isinstance(uncertainty,str) or not uncertainty.strip() or len(uncertainty)>500 or not all(c.isprintable() for c in uncertainty)):
            raise ValueError('invalid identity uncertainty')
        if 'delivery' in item:
            delivery_policy.validate_delivery(item['delivery'])
        by_id[ident]=copy.deepcopy(item)
    expected=set(actual['dialogue_text_sha256'])
    if set(by_id)!=expected:raise ValueError('assignment IDs do not match chapter dialogue')
    if any(not isinstance(x,str) or not x for x in unknown) or len(set(unknown))!=len(unknown):raise ValueError('invalid unknown IDs')
    if set(unknown)!={ident for ident,item in by_id.items() if item['speaker']=='unknown'}:raise ValueError('unknown IDs do not match assignments')
    conflict_ids=set()
    for conflict in conflicts:
        if not isinstance(conflict,dict):raise ValueError('invalid conflict')
        ident=conflict.get('id')
        if not isinstance(ident,str) or ident not in expected or ident in conflict_ids:raise ValueError('invalid or duplicate conflict ID')
        conflict_ids.add(ident)
    if not isinstance(cast,dict) or not isinstance(cast.get('characters'),dict):raise ValueError('invalid cast')
    characters=cast['characters'];indices=_chapter(script_rows,chapter_id);selected=copy.deepcopy([script_rows[i] for i in indices]);changes=[];unresolved=[]
    for row in selected:
        if row.get('type')!='dialogue':continue
        ident=row['id'];original=row.get('speaker');assignment=by_id[ident];proposed=assignment['speaker']
        if (not staging._safe_name(original) or not staging._safe_name(proposed)
            or proposed!='unknown' and (proposed.casefold()=='unknown' or proposed not in characters and ident not in conflict_ids)
            or original!='unknown' and (original.casefold()=='unknown' or original not in characters)):
            raise ValueError('invalid or unregistered speaker')
        mismatch=original!='unknown' and proposed!=original
        stated_uncertainty=(assignment.get('identity_uncertainty') is not None
                            or assignment.get('identity_status') not in (None,'confirmed'))
        fallback=proposed=='unknown' or ident in conflict_ids or mismatch or stated_uncertainty
        reasons=[]
        if proposed=='unknown':reasons.append('speaker_unknown')
        if ident in conflict_ids:reasons.append('model_conflict')
        if mismatch:reasons.append('conflicts_with_existing_speaker')
        if stated_uncertainty:reasons.append('model_identity_uncertain')
        if fallback:
            # Feed staging a safe known speaker when available, and explicitly
            # mark it unresolved. Its fallback role never becomes an identity.
            row['speaker']=original if original!='unknown' else 'unknown'
            unresolved.append({'id':ident,'reason':';'.join(reasons)})
            status='unconfirmed_fallback'
        else:
            row['speaker']=proposed
            status='existing_speaker_preserved' if original==proposed and original!='unknown' else 'model_attribution_not_confirmed'
        changes.append({'id':ident,'original_speaker':original,'model_speaker':proposed,'staging_speaker':staging.FALLBACK_ROLE if fallback else proposed,
            'text_sha256':actual['dialogue_text_sha256'][ident],'identity_status':status,'reasons':reasons,
            'model_reported_identity_status':assignment.get('identity_status'),
            'model_reported_identity_uncertainty':assignment.get('identity_uncertainty'),
            'needs_correction':fallback,'requires_identity_review':status!='existing_speaker_preserved',
            'regeneration_required_after_correction':status!='existing_speaker_preserved'})
    stage=staging.build_staging_render(selected,cast,unresolved,fallback_voice=fallback_voice)
    rows=copy.deepcopy(script_rows)
    for index,row in zip(indices,stage['script_rows']):rows[index]=row
    delivery={ident:item['delivery'] for ident,item in by_id.items() if 'delivery' in item}
    audit={'version':1,'scope':'isolated_staging_only','book_id':book_id,'chapter_id':chapter_id,
        'binding':copy.deepcopy(actual),'source_script_sha256':_hash(script_rows),'source_cast_sha256':_hash(cast),
        'attribution_sha256':_hash(attribution),'staging_script_sha256':_hash(rows),'staging_cast_sha256':_hash(stage['cast']),
        'staging_manifest':stage['manifest'],'changes':changes,'delivery':delivery,'fallback_count':len(unresolved),
        'needs_correction':bool(unresolved),'needs_identity_review':any(x['requires_identity_review'] for x in changes),'human_reviewed':False,
        'note':'Attribution is a machine proposal; model/receipt provenance must be verified by caller. Voice assets require separate validation.'}
    audit['audit_sha256']=_hash(audit)
    return {'script_rows':rows,'cast':stage['cast'],'audit':audit}

def restore_original(package):
    """Verify hashes and reconstruct exactly the input script and cast."""
    if not isinstance(package,dict) or set(package)!={'script_rows','cast','audit'}:raise ValueError('invalid package')
    rows=copy.deepcopy(package['script_rows']);cast=copy.deepcopy(package['cast']);audit=package['audit']
    if (not isinstance(audit,dict) or audit.get('version')!=1 or audit.get('scope')!='isolated_staging_only'
        or _hash({k:v for k,v in audit.items() if k!='audit_sha256'})!=audit.get('audit_sha256')
        or _hash(rows)!=audit.get('staging_script_sha256') or _hash(cast)!=audit.get('staging_cast_sha256')):
        raise ValueError('staging package hash mismatch')
    indices=_chapter(rows,audit['chapter_id']);selected=[rows[i] for i in indices]
    restored=staging.restore_original({'script_rows':selected,'cast':cast,'manifest':audit['staging_manifest']})
    changes=audit.get('changes')
    if not isinstance(changes,list):raise ValueError('invalid audit changes')
    lookup={row.get('id'):row for row in restored['script_rows'] if row.get('type')=='dialogue'};seen=set()
    for item in changes:
        if not isinstance(item,dict):raise ValueError('invalid audit change')
        ident=item.get('id');row=lookup.get(ident)
        if (not isinstance(ident,str) or ident in seen or not row or not isinstance(item.get('original_speaker'),str)
            or _text_hash(row['text'])!=item.get('text_sha256')):raise ValueError('invalid reversible mapping')
        seen.add(ident);row['speaker']=item['original_speaker']
    if seen!=set(lookup):raise ValueError('incomplete reversible mapping')
    for index,row in zip(indices,restored['script_rows']):rows[index]=row
    if _hash(rows)!=audit.get('source_script_sha256') or _hash(restored['cast'])!=audit.get('source_cast_sha256'):
        raise ValueError('original snapshot integrity mismatch')
    if capture_binding(audit['book_id'],audit['chapter_id'],rows)!=audit.get('binding'):raise ValueError('original binding mismatch')
    return {'script_rows':rows,'cast':restored['cast']}
