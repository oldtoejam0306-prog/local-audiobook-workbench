"""Offline-only, explicit ASR spot check of current Huashuo chapter cache units.

This reports machine mismatch candidates, never listening quality or audio provenance.
It never modifies the Huashuo workdir or its caches.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


def file_hash(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def model_hash(path):
    path=path.resolve(strict=True)
    if not path.is_dir():raise ValueError('ASR model must be an existing local directory')
    files=sorted(p for p in path.rglob('*') if p.is_file())
    if not files or not (path/'config.json').is_file():raise ValueError('local ASR model is incomplete')
    h=hashlib.sha256()
    for p in files:
        name=p.relative_to(path).as_posix().encode('utf-8');h.update(len(name).to_bytes(8,'big'));h.update(name);h.update(bytes.fromhex(file_hash(p)))
    return h.hexdigest()

def load_json(path):return json.loads(path.read_text(encoding='utf-8-sig'))

def prepare(workdir,chapter):
    from huashuo.pipeline import load_project,check_project,make_plan
    from huashuo.workdir import Workdir
    from huashuo.synth import unit_key
    from huashuo.engines.qwen3 import library_voice_identity
    wd=Workdir(workdir);project=load_project(wd)
    if check_project(project):raise ValueError('current Huashuo project check failed')
    options=load_json(workdir/'state/run.json')
    required=('titles','emotions','opening','closing','credit','simplify')
    if any(x not in options for x in required):raise ValueError('saved synthesis options are incomplete')
    if any(type(options[x]) is not bool for x in required[:-1]) or options['simplify'] not in (True,False,None):
        raise ValueError('invalid saved synthesis options')
    plan=make_plan(project,read_titles=options['titles'],emotions=options['emotions'],opening=options['opening'],
                   closing=options['closing'],credit=options['credit'],simplify=options['simplify'])
    matches=[i for i,c in enumerate(plan.chapters) if c.block_id==chapter]
    if len(matches)!=1:raise ValueError('chapter not uniquely present in current plan')
    units=[u for u in plan.units if u.chapter==matches[0]]
    if not units:raise ValueError('chapter contains no planned units')
    return project,units,unit_key,library_voice_identity

def map_units(units,cache,language,key_fn,voice_fn):
    identities={}
    for path in sorted(cache.glob('*.json')):
        try:
            meta=load_json(path);engine=meta['engine']
            if not isinstance(engine,dict):continue
            identities[json.dumps(engine,sort_keys=True,ensure_ascii=False)]=engine
        except (OSError,ValueError,KeyError,TypeError):continue
    mapped=[]
    for unit in units:
        matches=[]
        for engine in identities.values():
            key=key_fn(unit,engine,language,voice_fn(unit.voice));side=cache/(key+'.json');wav=cache/(key+'.wav')
            if not side.is_file() or not wav.is_file():continue
            try:
                meta=load_json(side)
                if (meta.get('key')!=key or meta.get('engine')!=engine or meta.get('text')!=unit.text
                    or meta.get('voice')!=unit.voice or meta.get('instruct')!=unit.instruct):continue
            except (OSError,ValueError,AttributeError):continue
            matches.append((key,side,wav))
        mapped.append(matches[0] if len(matches)==1 else ('ambiguous' if len(matches)>1 else None))
    return mapped

def spot_check(workdir,chapter,output,model_dir,sample_count,run_asr=False):
    if not run_asr:raise ValueError('explicit --run-asr is required for local transcription')
    workdir=Path(workdir).resolve(strict=True);output=Path(output).resolve();model_dir=Path(model_dir).resolve(strict=True)
    if output.is_relative_to(workdir) or output.exists():raise ValueError('report must be a new file outside the workdir')
    if not isinstance(sample_count,int) or isinstance(sample_count,bool) or sample_count<1:raise ValueError('sample count must be positive')
    source_names=('script.huaben.jsonl','text.txt','cast.json','pron.txt','state/run.json')
    before={name:(None if name=='pron.txt' and not (workdir/name).exists() else file_hash(workdir/name))
            for name in source_names}
    fingerprint=model_hash(model_dir)
    project,units,key_fn,voice_fn=prepare(workdir,chapter)
    mapped=map_units(units,workdir/'cache/units',project.language,key_fn,voice_fn)
    eligible=[i for i,m in enumerate(mapped) if isinstance(m,tuple)]
    if not eligible:raise ValueError('no units can be uniquely mapped to current cache')
    n=min(sample_count,len(eligible));chosen=[eligible[(i*len(eligible))//n] for i in range(n)]
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1';os.environ['HF_DATASETS_OFFLINE']='1'
    from huashuo.asr import AsrChecker,judge
    from huashuo.audio import read_wav
    from importlib.metadata import version
    try:backend_version=version('mlx-audio')
    except Exception as exc:raise ValueError('local ASR runtime version unavailable') from exc
    asr=AsrChecker(model=str(model_dir));samples=[]
    try:
        # Huashuo's lazy quiet_loading expects a Hub repo ID, not a local path.
        # Load the validated local directory directly before transcribe.
        from mlx_audio.stt import load as load_local_asr
        asr._model=load_local_asr(model_dir)
        for i in chosen:
            key,side,wav=mapped[i];meta_hash=file_hash(side);wav_hash=file_hash(wav)
            audio,rate=read_wav(wav);transcript=asr.transcribe(audio,rate,project.language)
            if not isinstance(transcript,str):raise ValueError('ASR returned invalid transcript')
            cer,problem=judge(units[i].page_text,transcript,project.language,asr.max_cer)
            if file_hash(wav)!=wav_hash or file_hash(side)!=meta_hash:raise ValueError('cache changed during transcription')
            samples.append({'unit_index':i,'key':key,'blocks':units[i].block_ids,'reference':units[i].page_text,
                'wav_sha256':wav_hash,'metadata_sha256':meta_hash,'transcript':transcript,
                'transcript_sha256':hashlib.sha256(transcript.encode()).hexdigest(),'cer':cer,'candidate':problem})
    finally:asr.close()
    after={name:(None if name=='pron.txt' and not (workdir/name).exists() else file_hash(workdir/name))
           for name in source_names}
    if before!=after or model_hash(model_dir)!=fingerprint:
        raise ValueError('source or local model changed during transcription')
    report={'version':1,'chapter_id':chapter,'workdir':str(workdir),'input_sha256':before,
        'model':{'local_path':str(model_dir),'sha256':fingerprint,'runtime':'mlx-audio','runtime_version':backend_version},
        'planned_units':len(units),'mapped_units':len(eligible),'sample_count':len(samples),
        'sample_coverage_planned':len(samples)/len(units),'sample_coverage_mapped':len(samples)/len(eligible),
        'skipped':{'unmapped':sum(x is None for x in mapped),'ambiguous':sum(x=='ambiguous' for x in mapped),
                   'not_selected':len(eligible)-len(samples)},'samples':samples,
        'error_candidates':[{'unit_index':x['unit_index'],'key':x['key'],'reason':x['candidate']} for x in samples if x['candidate']],
        'note':'Machine ASR mismatch candidates only. Listening quality not assessed. Hashes describe files observed now; historical provenance is not established. Native cached transcripts were not used.'}
    output.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive creation ensures an existing report is never replaced.
    with output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2);f.write('\n')
    return report

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('workdir',type=Path);p.add_argument('chapter');p.add_argument('output',type=Path);p.add_argument('--model-dir',type=Path,required=True);p.add_argument('--samples',type=int,default=5);p.add_argument('--run-asr',action='store_true');a=p.parse_args()
    try:report=spot_check(a.workdir,a.chapter,a.output,a.model_dir,a.samples,a.run_asr)
    except (OSError,ValueError,ImportError,KeyError) as exc:p.exit(2,'error: '+str(exc)+'\n')
    print(json.dumps({'report':str(a.output),'sample_count':report['sample_count'],'mapped_units':report['mapped_units']},ensure_ascii=False))
if __name__=='__main__':main()
