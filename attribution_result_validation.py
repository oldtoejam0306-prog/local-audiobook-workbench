"""Offline, source-literal validation for saved per-chapter attribution results."""
import copy
import hashlib
import delivery_policy


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _dialogue_rows(input_data):
    if not isinstance(input_data, dict) or not isinstance(input_data.get('text'), str):
        raise ValueError('invalid chapter input text')
    text=input_data['text']; blocks=input_data.get('blocks')
    if not isinstance(blocks,list):
        raise ValueError('input blocks must be a list')
    rows=[]; seen=set()
    for index,block in enumerate(blocks):
        if not isinstance(block,dict) or not isinstance(block.get('type'),str):
            raise ValueError('invalid source block at index '+str(index))
        if block['type']!='dialogue':
            continue
        block_id=block.get('id'); span=block.get('src'); block_text=block.get('text')
        if not isinstance(block_id,str) or not block_id.strip() or block_id in seen:
            raise ValueError('dialogue block IDs must be nonempty and unique')
        if (not isinstance(span,list) or len(span)!=2
                or any(type(value) is not int for value in span)
                or span[0]<0 or span[0]>=span[1] or span[1]>len(text)):
            raise ValueError('dialogue source offsets are invalid: '+block_id)
        if not isinstance(block_text,str) or not block_text or text[span[0]:span[1]]!=block_text:
            raise ValueError('dialogue source offset does not match text: '+block_id)
        seen.add(block_id);rows.append({'id':block_id,'source_range':span,'text':block_text})
    return text,rows


def normalize_attribution(input_data, parsed):
    """Return a separate normalized copy and a local derivation audit.

    Invalid structures fail closed. A structurally valid row with paraphrased,
    missing, or otherwise nonliteral attribution evidence is downgraded alone
    to unknown; no quote or speaker is invented. The original parsed object is
    never modified and should remain stored as the provider's raw response.
    """
    text,source_rows=_dialogue_rows(input_data)
    if (not isinstance(parsed,dict) or not isinstance(parsed.get('characters'),list)
            or not isinstance(parsed.get('dialogues'),list)):
        raise ValueError('model result must contain character and dialogue lists')
    source_ids={row['id'] for row in source_rows}
    result_ids=[]
    for row in parsed['dialogues']:
        if not isinstance(row,dict):
            raise ValueError('dialogue result must be an object')
        row_id=row.get('id')
        if not isinstance(row_id,str) or not row_id.strip() or row_id in result_ids:
            raise ValueError('dialogue result IDs must be nonempty and unique')
        result_ids.append(row_id)
        if (row.get('kind') not in ('dialogue','narration','unknown')
                or not isinstance(row.get('speaker'),str) or not row['speaker'].strip()
                or not isinstance(row.get('evidence'),str)):
            raise ValueError('dialogue kind, speaker, or evidence has invalid type/taxonomy: '+row_id)
    if set(result_ids)!=source_ids or len(result_ids)!=len(source_ids):
        raise ValueError('dialogue result IDs must exactly cover source dialogue IDs')

    normalized=copy.deepcopy(parsed)
    changes=[]; delivery_changes=[]
    for row in normalized['dialogues']:
        # Legacy saved replies omit delivery. Preserve that omission so old
        # normalized-stage documents and hashes remain byte-for-byte stable;
        # consumers apply the neutral default at the policy boundary.
        if 'delivery' in row:
            try:
                row['delivery']=delivery_policy.validate_delivery(row['delivery'])
            except ValueError:
                delivery_changes.append({'id':row['id'],'reason':'unsupported_or_malformed_delivery',
                                         'original_delivery':row['delivery'],'normalized_delivery':'neutral'})
                row['delivery']='neutral'
        else:
            delivery_changes.append({'id':row['id'],'reason':'legacy_delivery_missing',
                                     'normalized_delivery':'neutral'})
        evidence=row['evidence']
        reason=None
        if evidence and evidence not in text:
            reason='evidence_not_literal_source'
        elif row['kind'] in ('dialogue','narration') and row['speaker']!='unknown' and not evidence:
            reason='missing_literal_evidence'
        if reason:
            changes.append({'type':'dialogue_downgraded','id':row['id'],
                'original_speaker':row['speaker'],'original_kind':row['kind'],
                'original_evidence':evidence,'reason':reason,
                'normalized_speaker':'unknown','normalized_kind':'unknown'})
            row['speaker']='unknown';row['kind']='unknown';row['evidence']=''

    accepted=[]
    for index,candidate in enumerate(normalized['characters']):
        if not isinstance(candidate,dict):
            raise ValueError('character candidate must be an object')
        name=candidate.get('name');aliases=candidate.get('aliases');evidence=candidate.get('evidence')
        if (not isinstance(name,str) or not name.strip() or not isinstance(aliases,list)
                or not all(isinstance(alias,str) for alias in aliases)
                or not isinstance(evidence,str)):
            raise ValueError('character candidate structure invalid at index '+str(index))
        reason=None
        if not evidence or evidence not in text:
            reason='candidate_evidence_not_literal_source'
        elif name not in evidence:
            reason='candidate_name_missing_from_evidence'
        if reason:
            changes.append({'type':'character_candidate_dropped','index':index,
                'name':name,'original_evidence':evidence,'reason':reason})
        else:
            accepted.append(candidate)
    normalized['characters']=accepted
    audit={'status':'derived_locally','execution_kind':'derived_locally',
        'rule_set':'literal_source_evidence_v1','changes':changes,'delivery_audit':delivery_changes,
        'source':{'book_id':input_data.get('book_id'),'chapter_id':input_data.get('chapter_id'),
            'source_sha256':_sha(text)},'human_reviewed':False}
    return normalized,audit
