"""Build an isolated, review-required chapter package from a saved DeepSeek receipt.

No network, no book modifications, no audio. Output must be a new private file.
Voice references are supplied by the operator; this validates syntax, not assets.
"""
import argparse, hashlib, json, unicodedata
from pathlib import Path
import attribution_staging_adapter as adapter
import staging_render_cast as staging


def digest(raw): return hashlib.sha256(raw).hexdigest()
def read(path): return Path(path).read_bytes()
def build(input_path, receipt_dir, voices):
    from attribution_result_validation import normalize_attribution
    raw=read(input_path); x=json.loads(raw); d=Path(receipt_dir)
    intent=json.loads(read(d/'intent.json')); body=read(d/'response.body'); meta=json.loads(read(d/'response.meta.json'))
    if intent.get('input_sha256')!=digest(raw) or intent.get('book_id')!=x.get('book_id') or intent.get('chapter_id')!=x.get('chapter_id'):
        raise ValueError('receipt input identity mismatch')
    if meta.get('body_sha256')!=digest(body) or meta.get('http_status')!=200: raise ValueError('receipt response mismatch')
    response=json.loads(body); choices=response.get('choices',[]); usage=response.get('usage',{})
    if (not isinstance(response.get('id'),str) or not response['id']
            or not isinstance(response.get('model'),str) or not response['model']
            or len(choices)!=1 or choices[0].get('finish_reason')!='stop'
            or not all(type(usage.get(k)) is int and usage[k]>=0 for k in ('prompt_tokens','completion_tokens'))):
        raise ValueError('incomplete response or usage')
    original_parsed=json.loads(choices[0]['message']['content'])
    parsed,normalization=normalize_attribution(x,original_parsed)
    rows=x['blocks']; text=x['text']
    for r in rows:
        if 'src' in r:
            a,b=r['src']
            if not (isinstance(a,int) and isinstance(b,int) and 0<=a<b<=len(text) and text[a:b]==r.get('text')): raise ValueError('source offset mismatch')
    dialogue={r['id']:r for r in rows if r.get('type')=='dialogue'}
    results=parsed['dialogues']; ids=[r['id'] for r in results]
    if len(ids)!=len(set(ids)) or set(ids)!=set(dialogue): raise ValueError('dialogue coverage mismatch')
    if not isinstance(voices,dict) or not staging._safe_voice(voices.get('narrator')) or not isinstance(voices.get('characters'),dict): raise ValueError('invalid voice mapping')
    cast={'narrator':{'voice':voices['narrator']},'characters':{}}
    for name,voice in voices['characters'].items():
        if not staging._safe_name(name) or name.casefold()=='unknown' or not staging._safe_voice(voice): raise ValueError('invalid voice mapping')
        cast['characters'][name]={'voice':voice,'aliases':[],'description':'未审核的模型人物候选'}
    assignments=[]; unknown=[]
    for r in results:
        if r.get('kind') not in ('dialogue','narration','unknown') or not isinstance(r.get('speaker'),str) or not isinstance(r.get('evidence'),str) or (r['evidence'] and r['evidence'] not in text): raise ValueError('invalid model result')
        name=r['speaker']; usable=r['kind']=='dialogue' and bool(r['evidence']) and name in cast['characters']
        speaker=name if usable else 'unknown'
        assignments.append({'id':r['id'],'speaker':speaker})
        if not usable: unknown.append(r['id'])
    binding=adapter.capture_binding(x['book_id'],x['chapter_id'],rows)
    package=adapter.adapt_attribution(x['book_id'],x['chapter_id'],rows,cast,{'assignments':assignments,'unknown_ids':unknown,'conflicts':[]},binding)
    package['provenance']={'input_sha256':digest(raw),'response_sha256':digest(body),'request_sha256':intent.get('request_sha256'),
      'response_id':response.get('id'),'model':response.get('model'),'usage':usage,
      'local_normalization':normalization,
      'normalized_result_sha256':digest(json.dumps(parsed,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')),
      'dialogue_evidence':{r['id']:{'kind':r['kind'],'speaker':r['speaker'],'evidence':r['evidence']} for r in results},'human_reviewed':False,
      'notice':'模型归属与人物均待审；证据子串不证明语义，声音资产需单独验证。仅隔离实验。'}
    return package

def main():
    p=argparse.ArgumentParser(); p.add_argument('--input',required=True); p.add_argument('--receipt',required=True); p.add_argument('--voices',required=True); p.add_argument('--output',required=True); a=p.parse_args()
    result=build(a.input,a.receipt,json.loads(read(a.voices)))
    with open(a.output,'x',encoding='utf8') as f: json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({'chapter':result['audit']['chapter_id'],'dialogues':len(result['audit']['changes']),'fallback':result['audit']['fallback_count'],'usage':result['provenance']['usage'],'human_reviewed':False},ensure_ascii=False))
if __name__=='__main__': main()


def to_render_chapters(input_path, package, role_ids, *, narrator_id='narrator'):
    """Return (chapters, audit). role_ids maps staged character names to voice speaker IDs.

    All non-dialogue and unresolved dialogue use narrator_id. Inter-block source
    whitespace is attached to the preceding segment to preserve exact text.
    The caller must bind IDs to verified local voice assets before rendering.
    """
    x=json.loads(read(input_path)); text=x['text']; rows=package['script_rows']; audit=package['audit']
    if digest(read(input_path))!=package['provenance']['input_sha256'] or x['chapter_id']!=audit['chapter_id']:
        raise ValueError('source binding mismatch')
    if not isinstance(narrator_id,str) or not narrator_id or not isinstance(role_ids,dict): raise ValueError('invalid role mapping')
    if any(not isinstance(k,str) or not isinstance(v,str) or not v for k,v in role_ids.items()): raise ValueError('invalid role mapping')
    changes={item['id']:item for item in audit['changes']}; segments=[]; details=[]; cursor=0
    for row in rows:
        a,b=row['src']
        if a<cursor or b>len(text) or text[a:b]!=row['text']: raise ValueError('row source mismatch or overlap')
        gap=text[cursor:a]
        if gap:
            if not gap.isspace(): raise ValueError('non-whitespace source gap')
            if segments: segments[-1]['text']+=gap
            else: segments.append({'speaker_id':narrator_id,'text':gap})
        speaker=narrator_id; reason='narration_or_chapter'; change=None
        if row.get('type')=='skip':
            skipped=row['text']
            if not all(c.isspace() or unicodedata.category(c).startswith('P') for c in skipped):
                raise ValueError('skip contains non-punctuation content; review required')
            if segments:
                segments[-1]['text']+=skipped
            else:
                segments.append({'speaker_id':narrator_id,'text':skipped})
            details.append({'block_id':row['id'],'type':'skip','source_range':[a,b],
                            'speaker_id':segments[-1]['speaker_id'],'reason':'punctuation_merged',
                            'merged_segment_index':len(segments)-1,'text_sha256':digest(skipped.encode('utf8')),
                            'model_speaker':None,'model_evidence':None,'identity_status':None})
            cursor=b
            continue
        if row.get('type')=='dialogue':
            change=changes.get(row['id'])
            if not change: raise ValueError('missing dialogue audit')
            staged=row.get('speaker')
            if staged!=staging.FALLBACK_ROLE:
                if staged not in role_ids: raise ValueError('missing model role mapping')
                speaker=role_ids[staged]; reason='unreviewed_model_attribution'
            else: reason='unresolved_narrator_fallback'
        elif row.get('type') not in ('chapter','narration'):
            raise ValueError('unsupported row type')
        segments.append({'speaker_id':speaker,'text':row['text']})
        details.append({'block_id':row['id'],'type':row['type'],'source_range':[a,b],'speaker_id':speaker,'reason':reason,
                        'model_speaker':change.get('model_speaker') if change else None,
                        'model_evidence':package['provenance']['dialogue_evidence'].get(row['id']) if change else None,
                        'identity_status':change.get('identity_status') if change else None})
        cursor=b
    suffix=text[cursor:]
    if suffix and not suffix.isspace(): raise ValueError('non-whitespace source suffix')
    if suffix:
        if segments: segments[-1]['text']+=suffix
        else: segments.append({'speaker_id':narrator_id,'text':suffix})
    if ''.join(s['text'] for s in segments)!=text: raise ValueError('render text mismatch')
    return ([{'id':x['chapter_id'],'segments':segments}], {'source_text_sha256':digest(text.encode('utf8')),
            'response_sha256':package['provenance']['response_sha256'],'human_reviewed':False,'blocks':details})
