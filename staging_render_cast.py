"""Build reversible temporary render inputs for unknown dialogue speakers.

Pure in-memory transformation: never writes the book, cast, memory, or audio.
The generated role is an audio placeholder and is not a character identity.
"""
import copy
import hashlib
import json
import re

FALLBACK_ROLE='__STAGING_UNCONFIRMED__'
_VOICE=re.compile(r'^(?:preset:[A-Za-z0-9_.-]{1,120}|library:[A-Za-z0-9_-]{1,32}/[A-Za-z0-9_.-]{1,120})$')
_FORBIDDEN=re.compile(r'[\\/<>:|?*]')


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()

def _text_hash(text):return hashlib.sha256(text.encode('utf-8')).hexdigest()

def _safe_name(name):
    return (isinstance(name,str) and 0<len(name)<=60 and name==name.strip()
            and name not in (FALLBACK_ROLE,'旁白') and '..' not in name
            and not _FORBIDDEN.search(name) and all(c.isprintable() for c in name))

def _safe_voice(voice):
    return isinstance(voice,str) and bool(_VOICE.fullmatch(voice)) and '..' not in voice

def build_staging_render(script_rows,cast,unresolved=None,*,fallback_voice=None):
    """Return isolated rows/cast and a reversible audit manifest.

    Rows already carrying a known speaker retain it, unless their ID is also
    explicitly listed as unresolved; in that case the temporary role is used.
    Each unresolved entry is {'id': block_id, 'reason': nonempty reason}.
    Voice syntax only is checked: the caller must verify the preset/library asset
    exists, and operate on an isolated rendering workspace.
    """
    if not isinstance(script_rows,list) or not isinstance(cast,dict):raise ValueError('invalid script or cast')
    characters=cast.get('characters');narrator=cast.get('narrator')
    if not isinstance(characters,dict) or not isinstance(narrator,dict):raise ValueError('invalid cast')
    if FALLBACK_ROLE in characters or 'unknown' in characters:raise ValueError('reserved role already exists in cast')
    for name,profile in characters.items():
        if not _safe_name(name) or name.casefold()=='unknown' or not isinstance(profile,dict) or not _safe_voice(profile.get('voice')):
            raise ValueError('invalid cast character or voice')
    voice=narrator.get('voice') if fallback_voice is None else fallback_voice
    if not _safe_voice(voice):raise ValueError('fallback voice is invalid')
    entries=[] if unresolved is None else unresolved
    if not isinstance(entries,list):raise ValueError('unresolved must be a list')
    reasons={}
    for item in entries:
        if (not isinstance(item,dict) or set(item)!={'id','reason'} or not isinstance(item['id'],str)
            or not item['id'] or item['id'] in reasons or not isinstance(item['reason'],str)
            or not item['reason'].strip() or len(item['reason'])>500 or not all(c.isprintable() for c in item['reason'])):
            raise ValueError('invalid or duplicate unresolved entry')
        reasons[item['id']]=item['reason']
    staged_rows=copy.deepcopy(script_rows);staged_cast=copy.deepcopy(cast);seen=set();dialogue_ids=set();changes=[]
    for row in staged_rows:
        if not isinstance(row,dict):raise ValueError('invalid script row')
        ident=row.get('id')
        if ident is not None:
            if not isinstance(ident,str) or not ident or ident in seen:raise ValueError('invalid or duplicate block ID')
            seen.add(ident)
        if row.get('type')!='dialogue':continue
        if not isinstance(ident,str) or not isinstance(row.get('text'),str) or not row['text'].strip():raise ValueError('invalid dialogue text or ID')
        dialogue_ids.add(ident);speaker=row.get('speaker')
        if not _safe_name(speaker):raise ValueError('invalid dialogue speaker')
        if speaker!='unknown' and (speaker.casefold()=='unknown' or speaker not in characters):raise ValueError('speaker is not a known character')
        if speaker=='unknown' or ident in reasons:
            why=reasons.get(ident,'speaker_unknown')
            changes.append({'id':ident,'original_speaker':speaker,'staging_speaker':FALLBACK_ROLE,
                'reason':why,'text_sha256':_text_hash(row['text']),'identity_status':'unconfirmed',
                'needs_correction':True,'regeneration_required_after_correction':True})
            row['speaker']=FALLBACK_ROLE
    if set(reasons)-dialogue_ids:raise ValueError('unresolved entry does not refer to dialogue')
    if changes:
        staged_cast['characters'][FALLBACK_ROLE]={'voice':voice,'aliases':[],
            'description':'临时未确认说话人；仅用于隔离渲染，不代表真实人物身份。'}
    manifest={'version':1,'scope':'isolated_staging_only','fallback_role':FALLBACK_ROLE if changes else None,
        'fallback_voice':voice if changes else None,'source_script_sha256':_hash(script_rows),
        'source_cast_sha256':_hash(cast),'staging_script_sha256':_hash(staged_rows),
        'staging_cast_sha256':_hash(staged_cast),'fallback_count':len(changes),
        'needs_correction':bool(changes),'regeneration_required_after_correction':bool(changes),'changes':changes}
    manifest['manifest_sha256']=_hash(manifest)
    return {'script_rows':staged_rows,'cast':staged_cast,'manifest':manifest}

def restore_original(staged):
    """Verify integrity and reconstruct the exact pre-staging rows and cast."""
    if not isinstance(staged,dict) or set(staged)!={'script_rows','cast','manifest'}:raise ValueError('invalid staging package')
    rows=copy.deepcopy(staged['script_rows']);cast=copy.deepcopy(staged['cast']);manifest=staged['manifest']
    if (not isinstance(rows,list) or not isinstance(cast,dict) or not isinstance(manifest,dict)
        or manifest.get('version')!=1 or manifest.get('scope')!='isolated_staging_only'
        or _hash({k:v for k,v in manifest.items() if k!='manifest_sha256'})!=manifest.get('manifest_sha256')
        or _hash(rows)!=manifest.get('staging_script_sha256') or _hash(cast)!=manifest.get('staging_cast_sha256')):
        raise ValueError('staging integrity check failed')
    changes=manifest.get('changes')
    if not isinstance(changes,list) or manifest.get('fallback_count')!=len(changes):raise ValueError('invalid staging changes')
    seen=set();lookup={r.get('id'):r for r in rows if isinstance(r,dict) and r.get('id') is not None}
    for change in changes:
        if not isinstance(change,dict):raise ValueError('invalid staging change')
        ident=change.get('id');row=lookup.get(ident)
        if (not isinstance(ident,str) or ident in seen or not row or row.get('type')!='dialogue'
            or row.get('speaker')!=FALLBACK_ROLE or change.get('staging_speaker')!=FALLBACK_ROLE
            or _text_hash(row.get('text',''))!=change.get('text_sha256')
            or not _safe_name(change.get('original_speaker'))):raise ValueError('invalid fallback mapping')
        seen.add(ident);row['speaker']=change['original_speaker']
    if changes:
        if manifest.get('fallback_role')!=FALLBACK_ROLE or not isinstance(cast.get('characters'),dict):raise ValueError('invalid fallback role')
        cast['characters'].pop(FALLBACK_ROLE,None)
    if _hash(rows)!=manifest.get('source_script_sha256') or _hash(cast)!=manifest.get('source_cast_sha256'):
        raise ValueError('original integrity check failed')
    return {'script_rows':rows,'cast':cast}
