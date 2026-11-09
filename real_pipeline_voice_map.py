"""Private, immutable per-book experimental mapping of candidates to voice IDs.

No claim of human confirmation. Exact same-name continuity is an assumption;
aliases require a shared literal evidence quote, and collisions fail closed.
"""
import hashlib,json,re
from pathlib import Path
import real_pipeline_workflow as w


def evolve(book_id,chapter_id,text,characters,prior,voice_ids):
    if not re.fullmatch(r'book-[0-9a-f]{20}',book_id) or not re.fullmatch(r'c\d+',chapter_id): raise ValueError('invalid identity')
    if prior and (prior['book_id']!=book_id or int(prior['chapter_id'][1:])>=int(chapter_id[1:])): raise ValueError('invalid prior book/order')
    if len(set(voice_ids))!=len(voice_ids) or not all(isinstance(x,str) and x for x in voice_ids): raise ValueError('invalid voices')
    roles=json.loads(json.dumps(prior['roles'],ensure_ascii=False)) if prior else []
    collisions=set(prior.get('conflicts',[])) if prior else set()
    for c in characters:
        name=c.get('name'); aliases=c.get('aliases'); evidence=c.get('evidence')
        if not isinstance(name,str) or not name or not isinstance(aliases,list) or not all(isinstance(x,str) and x for x in aliases) or not isinstance(evidence,str) or not evidence or evidence not in text or name not in evidence: continue
        owners={r['name']:r for r in roles}; alias_owners={}
        for r in roles:
            for alias in r['aliases']: alias_owners.setdefault(alias,set()).add(r['name'])
        if name in alias_owners and name not in owners:
            # A previously established alias is not automatically a distinct person.
            collisions.add(name); continue
        if name not in owners:
            used={r['voice_id'] for r in roles}; available=[v for v in voice_ids if v not in used]
            if not available: continue
            role={'name':name,'voice_id':available[0],'aliases':[],'evidence':[],'identity_status':'unreviewed_model_candidate'}
            roles.append(role);owners[name]=role
        role=owners[name]; role['evidence'].append({'chapter_id':chapter_id,'quote':evidence})
        for alias in aliases:
            # Co-occurrence is a conservative string test, not semantic proof.
            if alias==name or alias not in evidence: continue
            if not re.search(re.escape(name)+r'.{0,8}(?:又叫|也叫|又名|别名|称作|名为)'+re.escape(alias), evidence): continue
            other=alias_owners.get(alias,set())-{name}
            if alias in owners and alias!=name: other.add(alias)
            if other: collisions.add(alias);continue
            if alias not in role['aliases']:role['aliases'].append(alias)
    # Resolve ambiguity across prior aliases and names; never silently select.
    owners={}
    for r in roles:
        for label in [r['name'],*r['aliases']]:owners.setdefault(label,set()).add(r['name'])
    collisions.update(label for label,names in owners.items() if len(names)>1)
    return {'version':1,'book_id':book_id,'chapter_id':chapter_id,'roles':roles,'conflicts':sorted(collisions),
            'human_reviewed':False,'note':'same-name continuity and evidence are unreviewed model proposals; voice identity is fictional'}

def label_map(record):
    out={}
    for r in record['roles']:
        for label in [r['name'],*r['aliases']]:
            if label not in record['conflicts']:out[label]=r['voice_id']
    return out

def create_snapshot(root,record,*,source_sha256,response_sha256):
    out=dict(record,source_sha256=source_sha256,response_sha256=response_sha256)
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    path=root/(record['book_id']+'-'+record['chapter_id']+'.json');w._write_new(path,out)
    return path
