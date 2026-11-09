"""Conservative, offline chapter-by-chapter character evidence fusion.

No model or network calls. Each invocation takes only the current chapter text;
all output is a new JSON file. Unresolved identities are never silently merged.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re

GENERIC=re.compile(r'^(?:船长|大副|二副|队长|局长|医生|护士|老师|教授|先生|女士|小姐|夫人|老板|掌柜|师兄|师姐|师父|父亲|母亲|爸爸|妈妈|哥哥|姐姐|弟弟|妹妹|爷爷|奶奶|那人|男人|女人|老人|少年|女孩|男孩|他|她|它|我|你|我们|他们)$')
TITLE_SUFFIX=re.compile(r'(?:船长|大副|二副|队长|舰长|局长|组长|团长|将军|警官|先生|女士|小姐|夫人|大人|医生|护士|老师|教授|老板|掌柜|师兄|师姐|师父|父亲|母亲|哥哥|姐姐|弟弟|妹妹|爷爷|奶奶|陌生人|黑衣人|白衣人|老人|少年|姑娘|小孩)$')
def _generic(name):return bool(GENERIC.fullmatch(name) or TITLE_SUFFIX.search(name))
LINK_WORDS=('又名','也叫','原名','别名是','别名为','就是','即是','就是以前的','就是之前的','同一人')

def _sha(text):return hashlib.sha256(text.encode('utf-8')).hexdigest()
def _name(value):
    if not isinstance(value,str) or not value.strip() or len(value.strip())>60:return None
    return value.strip()
def _quoted(text,quote):return isinstance(quote,str) and bool(quote.strip()) and len(quote)<=500 and quote in text
def _clear_name_occurrence(text,quote,name):
    """Reject unlocalized occurrences embedded after an unrecognized word prefix.

    This is deliberately conservative: Chinese has no universal word boundary.
    Every possible occurrence of this quoted name must have a clear left context.
    """
    contexts=('名叫','叫做','叫作','称为','认识','看见','遇见','听见','见到','找到','看着','望着','告诉','叫住','喊','问','对','与','和','向','跟','是','叫')
    found=False;quote_start=0
    while True:
        qpos=text.find(quote,quote_start)
        if qpos<0:break
        name_start=0
        while True:
            local=quote.find(name,name_start)
            if local<0:break
            absolute=qpos+local;found=True
            if absolute>0 and (text[absolute-1].isalnum() or text[absolute-1]=='_'):
                if not any(absolute>=len(prefix) and text.startswith(prefix,absolute-len(prefix),absolute) for prefix in contexts):return False
            name_start=local+1
        quote_start=qpos+1
    return found
def _linked(quote,a,b):
    # Strong local syntactic identity only. A free-floating “same person” token is insufficient.
    if not a or not b or a==b or _generic(a) or _generic(b):return False
    # Alternatives, denials, speculation, and reported mistaken beliefs are
    # not identity assertions even if a substring resembles “A就是B”.
    ambiguous=('不是','并非','并不','并未','未必','不一定','否认','没有证据','无证据','未证实','无法确定',
               '或者','或是','还是','也许','或许','可能','疑似','似乎','以为','误认','误会','冒充','难道','究竟','据说','传闻','如果','假如','倘若','倘使','要是','假设','假定','是否','若是','的话','?','？')
    if any(token in quote for token in ambiguous):return False
    words='|'.join(map(re.escape,LINK_WORDS))
    end=r'(?=$|[，。！？；、：,;!?.\s）)\]》\"”’])'
    start=r'(?<![\w])'
    return bool(re.search(start+r'(?:'+re.escape(a)+r')\s*(?:'+words+r')\s*(?:'+re.escape(b)+r')'+end,quote) or
                re.search(start+r'(?:'+re.escape(b)+r')\s*(?:'+words+r')\s*(?:'+re.escape(a)+r')'+end,quote))
def _conflict(out,kind,chapter,**details):out['conflicts'].append({'kind':kind,'chapter_id':chapter,**details})
def new_book(book_id,chapters):
    if not isinstance(book_id,str) or not book_id.strip():raise ValueError('book_id required')
    if not isinstance(chapters,list) or not chapters or any(not isinstance(c,str) or not c for c in chapters) or len(set(chapters))!=len(chapters):raise ValueError('ordered chapter IDs must be unique')
    return {'version':1,'book_id':book_id,'chapter_order':chapters[:],'processed':[],
            'entities':[],'relations':[],'conflicts':[]}
def fuse_chapter(snapshot,chapter_id,text,candidates,relations=None):
    out=copy.deepcopy(snapshot)
    if not isinstance(out,dict) or out.get('version')!=1:raise ValueError('invalid snapshot')
    chapters=out.get('chapter_order');processed=out.get('processed')
    if not isinstance(chapters,list) or not isinstance(processed,list) or len(processed)>=len(chapters) or chapters[len(processed)]!=chapter_id:
        raise ValueError('chapter must be the next unprocessed chapter')
    if not isinstance(text,str) or not text.strip() or not isinstance(candidates,list) or not isinstance(relations or [],list):raise ValueError('invalid chapter input')
    if any(not isinstance(out.get(k),list) for k in ('entities','relations','conflicts')):raise ValueError('invalid snapshot collections')
    entities=out['entities'];names={}
    for ent in entities:
        for n in [ent['name'],*[x['name'] for x in ent['aliases']]]:names.setdefault(n,[]).append(ent)
    current={}
    for index,c in enumerate(candidates):
        if not isinstance(c,dict):raise ValueError('candidate must be object')
        name=_name(c.get('name'));quote=c.get('evidence')
        if not name or not _quoted(text,quote) or name not in quote:
            _conflict(out,'invalid_name_evidence',chapter_id,index=index);continue
        if _generic(name):
            _conflict(out,'generic_name',chapter_id,index=index,name=name,evidence=quote);continue
        # One-character tokens may occur inside ordinary words or other names.
        # A substring alone cannot establish that they denote a person.
        if len(name)==1:
            _conflict(out,'short_name_ambiguous',chapter_id,index=index,name=name,evidence=quote);continue
        if not _clear_name_occurrence(text,quote,name):
            _conflict(out,'name_boundary_unresolved',chapter_id,index=index,name=name,evidence=quote);continue
        link=c.get('link_to');link_evidence=c.get('identity_evidence')
        ent=None;identity_basis='first_occurrence'
        if link is not None:
            found=[e for e in entities if e['id']==link]
            if len(found)!=1 or not _quoted(text,link_evidence) or not _linked(link_evidence,name,found[0]['name']):
                _conflict(out,'identity_not_proven',chapter_id,index=index,name=name,link_to=link,evidence=link_evidence if _quoted(text,link_evidence) else None);continue
            existing=names.get(name,[])
            if existing and any(e['id']!=link for e in existing):
                _conflict(out,'name_collision',chapter_id,index=index,name=name,link_to=link,evidence=link_evidence);continue
            ent=found[0];identity_basis='explicit_link'
            if name!=ent['name'] and not any(a['name']==name for a in ent['aliases']):
                ent['aliases'].append({'name':name,'chapter_id':chapter_id,'evidence':link_evidence});names.setdefault(name,[]).append(ent)
        elif names.get(name):
            owners=names[name]
            if len(owners)!=1 or owners[0]['name']!=name:
                _conflict(out,'same_name_unconfirmed',chapter_id,index=index,name=name,possible_entities=[e['id'] for e in owners],evidence=quote);continue
            ent=owners[0];identity_basis='same_name_assumption'
            _conflict(out,'same_name_assumption_needs_review',chapter_id,index=index,name=name,entity_id=ent['id'],evidence=quote)
        else:
            ent={'id':f'e{len(entities)+1:06d}','name':name,'first_chapter':chapter_id,'aliases':[],
                 'mentions':[],'timeline':[],'claims':[]};entities.append(ent);names.setdefault(name,[]).append(ent)
        ent['mentions'].append({'chapter_id':chapter_id,'name':name,'evidence':quote,'identity_basis':identity_basis})
        ent['timeline'].append({'chapter_id':chapter_id,'evidence':quote})
        current[index]=ent
        for claim in c.get('claims',[]):
            if not isinstance(claim,dict):raise ValueError('claim must be object')
            statement=claim.get('text');evidence=claim.get('evidence')
            if not isinstance(statement,str) or not statement.strip() or len(statement)>300 or not _quoted(text,evidence) or name not in evidence:
                _conflict(out,'claim_without_evidence',chapter_id,index=index,entity_id=ent['id']);continue
            # The exact claim must appear in the evidence; no inference from a co-occurrence.
            if statement.strip() not in evidence:
                _conflict(out,'claim_inference_unverified',chapter_id,index=index,entity_id=ent['id'],claim=statement if isinstance(statement,str) and statement in text else None,evidence=evidence);continue
            ent['claims'].append({'text':statement.strip(),'chapter_id':chapter_id,'evidence':evidence,'status':'textual_candidate_needs_review'})
        for alias in c.get('aliases',[]):
            if not isinstance(alias,dict):raise ValueError('alias must be object')
            alt=_name(alias.get('name'));evidence=alias.get('evidence')
            if not alt or _generic(alt):
                _conflict(out,'generic_or_invalid_alias',chapter_id,index=index,entity_id=ent['id'],alias=alt if alt and alt in text else None);continue
            if not _quoted(text,evidence) or not _linked(evidence,name,alt):
                _conflict(out,'alias_not_proven',chapter_id,index=index,entity_id=ent['id'],alias=alt if alt in text else None,evidence=evidence if _quoted(text,evidence) else None);continue
            owners=names.get(alt,[])
            if any(e['id']!=ent['id'] for e in owners):
                _conflict(out,'alias_collision',chapter_id,index=index,entity_id=ent['id'],alias=alt,evidence=evidence);continue
            if alt!=ent['name'] and not any(a['name']==alt for a in ent['aliases']):
                ent['aliases'].append({'name':alt,'chapter_id':chapter_id,'evidence':evidence});names.setdefault(alt,[]).append(ent)
    for relation in relations or []:
        if not isinstance(relation,dict):raise ValueError('relation must be object')
        a,b=relation.get('source_index'),relation.get('target_index');label=_name(relation.get('label'));quote=relation.get('evidence')
        if type(a)!=int or type(b)!=int or a not in current or b not in current or a==b or not label or not _quoted(text,quote):
            _conflict(out,'relation_unresolved',chapter_id,source_index=a if type(a)==int else None,target_index=b if type(b)==int else None,label=label if label and label in text else None,evidence=quote if _quoted(text,quote) else None);continue
        name_a=_name(candidates[a].get('name'));name_b=_name(candidates[b].get('name'))
        if name_a not in quote or name_b not in quote or label not in quote:
            _conflict(out,'relation_unresolved',chapter_id,source_index=a if type(a)==int else None,target_index=b if type(b)==int else None,label=label if label and label in text else None,evidence=quote if _quoted(text,quote) else None);continue
        # Records a reported textual relation, not a verified interpretation or enduring fact.
        out['relations'].append({'source_id':current[a]['id'],'target_id':current[b]['id'],'label':label,
                                 'chapter_id':chapter_id,'evidence':quote,'status':'textual_candidate_needs_review'})
    out['processed'].append({'chapter_id':chapter_id,'source_sha256':_sha(text),'candidate_count':len(candidates)})
    return out

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('snapshot',type=Path);p.add_argument('chapter',type=Path);p.add_argument('output',type=Path);a=p.parse_args()
    try:
        snapshot=json.loads(a.snapshot.read_text(encoding='utf-8-sig'));item=json.loads(a.chapter.read_text(encoding='utf-8-sig'))
        if a.output.resolve() in (a.snapshot.resolve(),a.chapter.resolve()):raise ValueError('output must be a new file')
        result=fuse_chapter(snapshot,item['chapter_id'],item['text'],item['candidates'],item.get('relations'))
        with a.output.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2);f.write('\n')
    except (OSError,ValueError,KeyError,TypeError) as exc:p.exit(2,'error: '+str(exc)+'\n')
if __name__=='__main__':main()
