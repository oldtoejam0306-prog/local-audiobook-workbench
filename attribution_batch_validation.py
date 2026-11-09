"""Conservative local model response validation; no filesystem or network access."""
import json


def validate(raw, targets, candidate_names, finish_reason, *, audit=None):
    """Evidence matches adjacent text and names the proposed speaker; still inference.

    Exact text does not prove the speaker spoke it. No model proposal is confirmed.
    Any malformed/truncated response makes the entire batch unknown.
    """
    fallback=[{'id':x['id'],'speaker':'unknown','identity_status':'model_inference'} for x in targets]
    try:
        if finish_reason != 'stop': raise ValueError('not_stopped')
        data=json.loads(raw)
        if not isinstance(data,list) or len(data)>len(targets): raise ValueError('invalid_count')
        expected={x['id']:x for x in targets}; seen=set(); result={}; outcomes={}; rejected=0
        for item in data:
            if not isinstance(item,dict) or set(item)!={'id','speaker','evidence'}: raise ValueError('invalid_fields')
            ident=item['id']; speaker=item['speaker']; evidence=item['evidence']
            if not isinstance(ident,str) or ident not in expected or ident in seen or not isinstance(speaker,str) or not isinstance(evidence,str): raise ValueError('invalid_entry')
            seen.add(ident); context=expected[ident]['context']
            valid=(speaker in candidate_names and 2<=len(evidence)<=300 and evidence in context and speaker in evidence)
            if speaker!='unknown' and not valid:rejected+=1
            result[ident]={'id':ident,'speaker':speaker if valid else 'unknown','identity_status':'model_inference'}
            outcomes[ident]='model_unknown' if speaker=='unknown' else ('evidence_matched_unreviewed' if valid else 'evidence_or_candidate_rejected')
        missing=len(expected)-len(seen)
        errors=[]
        if missing:errors.append('missing_ids:'+str(missing))
        if rejected:errors.append('evidence_rejected:'+str(rejected))
        if audit is not None:audit.extend({'id':x['id'],'outcome':outcomes.get(x['id'],'missing_id')} for x in targets)
        return [result.get(x['id'],{'id':x['id'],'speaker':'unknown','identity_status':'model_inference'}) for x in targets], errors
    except (ValueError,TypeError,KeyError):
        if audit is not None:audit.extend({'id':x['id'],'outcome':'invalid_batch'} for x in targets)
        return fallback, ['invalid_or_incomplete_batch']


def for_staging(assignments, expected_ids, registered_names):
    """Convert proposals to staging's complete schema; never confirm identity."""
    if not isinstance(assignments,list) or not isinstance(expected_ids,set) or not isinstance(registered_names,set):
        raise ValueError('invalid inputs')
    converted=[];unknown=[];conflicts=[];seen=set()
    for item in assignments:
        if not isinstance(item,dict) or set(item)!={'id','speaker','identity_status'} or item['identity_status']!='model_inference':
            raise ValueError('invalid assignment')
        ident=item['id']; speaker=item['speaker']
        if not isinstance(ident,str) or ident not in expected_ids or ident in seen or not isinstance(speaker,str) or not speaker:
            raise ValueError('invalid or duplicate ID')
        seen.add(ident)
        converted.append({'id':ident,'speaker':speaker,'identity_status':'model_inference',
                          'identity_uncertainty':'本地模型推断，身份未经人工审核'})
        if speaker=='unknown':unknown.append(ident)
        elif speaker not in registered_names:conflicts.append({'id':ident,'reason':'unregistered_model_candidate'})
    if seen!=expected_ids:raise ValueError('incomplete assignments')
    return {'assignments':converted,'unknown_ids':unknown,'conflicts':conflicts}
