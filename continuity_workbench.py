"""One-shot JSON bridge for isolated, synthetic continuity experiments."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import release_config

BASE=release_config.mac_data_root()
CHECK=release_config.mac_stage_root()
VOICE_ROOT=release_config.mac_voice_root()
DRIVER=Path(__file__).resolve().parent
sys.path.insert(0,str(DRIVER))
import experimental_isolated_voice_library as library


class _ContinuityProxy:
    def __getattr__(self, name):
        module = importlib.import_module("continuity_runner")
        value = getattr(module, name)
        setattr(self, name, value)
        return value


continuity = _ContinuityProxy()


def sha(raw): return hashlib.sha256(raw).hexdigest()

WORKER_BUNDLE=('continuity_prepare.py','continuity_runner.py','book_memory.py','voice_design_engine.py')

def stage_worker_bundle(root):
    root=Path(root).resolve(strict=True)
    for name in WORKER_BUNDLE:
        raw_source=DRIVER/name
        if raw_source.is_symlink() or not raw_source.is_file():raise ValueError('required worker source is missing')
        source=raw_source.resolve(strict=True)
        shutil.copyfile(source,root/name)
    return root

def worker_environment():
    env=os.environ.copy()
    huashuo_source=release_config.huashuo_root()/'source'/'src'
    env.update({'PYTHONPATH':os.pathsep.join((str(DRIVER),str(huashuo_source))),
                'AUDIOBOOK_PROJECT_ROOT':str(DRIVER),'HF_HUB_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1'})
    return env
def run_dir(value):
    p=Path(value)
    if not p.is_absolute() or p.is_symlink(): raise ValueError('invalid experiment directory')
    p=p.resolve(strict=True)
    if p.parent != CHECK.resolve() or not re.fullmatch(r'continuity-synthetic-[A-Za-z0-9_-]+',p.name):
        raise ValueError('invalid experiment directory')
    if not (p/'workbench-binding.json').is_file(): raise ValueError('not a workbench experiment')
    return p

def launch(root, action):
    profile='(version 1)(allow default)(deny network*)(deny file-write*)(allow file-write* (subpath "'+str(root)+'"))'
    env=worker_environment()
    python=str(Path(os.path.abspath(sys.executable)))
    sandbox=str(release_config.sandbox_exec())
    with (root/'workbench-worker.log').open('ab') as log:
        p=subprocess.Popen([sandbox,'-p',profile,python,
            str(root/'continuity_runner.py'),'--root',str(root),action],cwd=root,env=env,
            stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,close_fds=True)
    return p.pid

def start(req):
    release_config.apply_mac_environment()
    if set(req)!={'action','stage','vector_sha256'} or not isinstance(req['vector_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',req['vector_sha256']):
        raise ValueError('invalid start request')
    stage=Path(req['stage'])
    if not stage.is_absolute() or stage.is_symlink() or stage.parent!=VOICE_ROOT or not re.fullmatch(r'voice-experiment-[A-Za-z0-9_-]+',stage.name):
        raise ValueError('invalid voice stage')
    stage=stage.resolve(strict=True)
    if sha((stage/'speaker-vector.npy').read_bytes())!=req['vector_sha256']: raise ValueError('vector hash mismatch')
    preset=release_config.mac_presets_path()
    snapshot={'version':1,'book_id':'isolated-book','locked':{}}
    raw=json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    published=library.publish_experiment(task_dir=stage,experiment_parent=CHECK,book_id='isolated-book',character_id='isolated-character',
       locked_snapshot=snapshot,locked_snapshot_sha256=sha(raw),trusted_presets_path=preset,trusted_presets_sha256=sha(preset.read_bytes()))
    if published['metadata']['vector_sha256']!=req['vector_sha256']: raise ValueError('published vector mismatch')
    root=Path(tempfile.mkdtemp(prefix='continuity-synthetic-workbench-',dir=CHECK)).resolve()
    stage_worker_bundle(root)
    p=subprocess.run([sys.executable,str(root/'continuity_prepare.py')],cwd=root,env=worker_environment(),capture_output=True,text=True,timeout=120)
    if p.returncode: raise RuntimeError('synthetic preparation failed: '+p.stderr[-500:])
    fake=root/'.runtime'; src=fake/'source'
    if not src.is_symlink(): raise RuntimeError('unexpected synthetic source')
    src.unlink()
    voices=src/'src/huashuo/voices'; voices.parent.mkdir(parents=True)
    shutil.copytree(published['library_dir'],voices,symlinks=False)
    wd=fake/'books'/continuity.BOOK/'work.huashuo'; cast=wd/'cast.json'
    data=json.loads(cast.read_text());data['narrator']['voice']=published['voice_ref']
    cast.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    executable=root/'isolated-huashuo'
    vector_path=voices/'zh'/(published['voice_ref'].split('/')[-1]+'.npy')
    wrapper = '''#!{python}
import hashlib, json, os, sys
from pathlib import Path
import numpy as np
import huashuo.library as lib
lib.ROOT=Path({voices})
target=Path({vector}).resolve()
expected={digest}
original=np.load
def observed_load(path, *args, **kwargs):
    if isinstance(path,(str,os.PathLike)) and Path(path).resolve()==target:
        actual=hashlib.sha256(target.read_bytes()).hexdigest()
        if actual!=expected: raise ValueError('voice vector changed before load')
        result=original(path,*args,**kwargs)
        with open({evidence},'a',encoding='utf8') as stream:
            stream.write(json.dumps({{'action':sys.argv[1], 'vector_sha256':actual}})+'\\n')
            stream.flush(); os.fsync(stream.fileno())
        return result
    return original(path,*args,**kwargs)
np.load=observed_load
from huashuo.cli import main
sys.exit(main())
'''.format(python=Path(os.path.abspath(sys.executable)),voices=repr(str(voices)),vector=repr(str(vector_path)),
           digest=repr(req['vector_sha256']),evidence=repr(str(root/'vector-loads.jsonl')))
    executable.write_text(wrapper,encoding='utf8')
    executable.chmod(0o700)
    binding={'vector_sha256':req['vector_sha256'],'voice_ref':published['voice_ref'],
        'library_receipt_sha256':published['receipt_sha256'],'stage':str(stage),'production_eligible':False,
        'load_evidence_required':True}
    (root/'workbench-binding.json').write_text(json.dumps(binding,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    backend=continuity.configure(root)
    with continuity.book_lock(backend) as lock:
        continuity.batch.create(backend,['c000','c001'],1.0,lock.fileno())
    launch(root,'run')
    return {'ok':True,'run_id':str(root),'status':'queued','vector_sha256':req['vector_sha256']}

def query(req):
    if set(req)!={'action','run_id'}: raise ValueError('invalid request')
    root=run_dir(req['run_id']); backend=continuity.configure(root)
    if req['action']=='resume':
        state=continuity.batch.status(backend)
        if not state or state['status'] not in ('queued','running','interrupted','stopped'):
            raise ValueError('batch cannot be resumed')
        with continuity.book_lock(backend): pass
        launch(root,'resume')
        return {'ok':True,'run_id':str(root),'status':'starting'}
    state=continuity.inspect(backend)
    manifest=None
    vector_load_verified=False
    if state['batch'] and state['batch']['status']=='complete':
        with continuity.book_lock(backend): manifest=continuity.playback_manifest(backend,release_config.mac_ffmpeg())
        binding=json.loads((root/'workbench-binding.json').read_text())
        evidence=root/'vector-loads.jsonl'
        loads=[json.loads(line) for line in evidence.read_text().splitlines()] if evidence.is_file() else []
        vector_load_verified=any(row.get('action')=='synth' and row.get('vector_sha256')==binding['vector_sha256'] for row in loads)
        if binding.get('load_evidence_required') and not vector_load_verified:
            raise ValueError('no verified synth vector load evidence')
    return {'ok':True,'run_id':str(root),'status':state['batch']['status'] if state['batch'] else 'missing','batch':state['batch'],'lock_available':state['lock_available'],
            'manifest':manifest,'vector_load_verified':vector_load_verified,
            'vector_sha256':json.loads((root/'workbench-binding.json').read_text())['vector_sha256']}

def main():
    req=json.load(sys.stdin)
    if not isinstance(req,dict): raise ValueError('JSON object required')
    if req.get('action')=='start': result=start(req)
    elif req.get('action') in ('status','resume'): result=query(req)
    else: raise ValueError('invalid action')
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False));sys.exit(1)


