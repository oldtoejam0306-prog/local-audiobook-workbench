"""Run selected Huashuo commands with a process-local isolated voice library.

Use in a dedicated subprocess: changing huashuo.library.ROOT is process-global,
not thread-local. Paths are constrained to the supplied staging root. This does
not sandbox Huashuo or authenticate assets, and does not verify model output.
"""
import argparse
import importlib
import json
import os
from pathlib import Path
import re
import threading
import tempfile

_LOCK=threading.RLock()
_CREATED_STAGES={}

def create_staging_root(parent):
    """Create and register a fresh private directory for this process only.

    Populate it with copies, never links to existing assets. This is a misuse
    guard, not an adversarial sandbox; real calls also require an OS sandbox.
    """
    parent=Path(parent).expanduser().resolve(strict=True)
    if not parent.is_dir():raise ValueError("staging parent must be a directory")
    stage=Path(tempfile.mkdtemp(prefix="huashuo-isolated-",dir=parent)).resolve(strict=True)
    stat=stage.stat()
    _CREATED_STAGES[stage]=(stat.st_dev,stat.st_ino)
    return stage

def _registered_stage(path):
    original=Path(path).expanduser()
    if original.is_symlink():raise ValueError("staging root may not be a symlink")
    stage=original.resolve(strict=True)
    stat=stage.stat()
    if not stage.is_dir() or _CREATED_STAGES.get(stage)!=(stat.st_dev,stat.st_ino):
        raise ValueError("staging root must be freshly created by create_staging_root in this process")
    return stage
_SAFE_NAME=re.compile(r'^[A-Za-z0-9_.-]{1,120}$')
_SAFE_LANG=re.compile(r'^[A-Za-z0-9_-]{1,32}$')
_CHAPTERS=re.compile(r'^[1-9][0-9]*(?:-[1-9][0-9]*)?(?:,[1-9][0-9]*(?:-[1-9][0-9]*)?)*$')
_ALLOWED_COMMANDS={'check','synth','package'}

def _within(path,root,*,must_exist=True):
    p=Path(path).expanduser()
    if must_exist:
        resolved=p.resolve(strict=True)
    else:
        if p.is_symlink() or p.exists():raise ValueError('output must be a new file')
        resolved=p.parent.resolve(strict=True)/p.name
    if not resolved.is_relative_to(root) or resolved==root:raise ValueError('path escapes or equals staging root')
    return resolved

def _object_file(path,label):
    try:
        data=json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError,UnicodeError,json.JSONDecodeError) as exc:raise ValueError(label+' invalid') from exc
    if not isinstance(data,dict):raise ValueError(label+' must be an object')
    return data

def validate_library(root,*,trusted_presets):
    """Check all discoverable voice metadata and NPY partners without loading models."""
    root=Path(root).resolve(strict=True)
    if not root.is_dir():raise ValueError('library root must be a directory')
    presets_path=_within(root/'presets.json',root)
    presets=_object_file(presets_path,'presets')
    if not presets or not isinstance(trusted_presets,dict) or not trusted_presets:raise ValueError('no trusted presets available')
    if any(name not in trusted_presets or info!=trusted_presets[name] for name,info in presets.items()):
        raise ValueError('isolated presets do not match installed Huashuo metadata')
    for name,info in presets.items():
        if (not isinstance(name,str) or not _SAFE_NAME.fullmatch(name) or '..' in name or not isinstance(info,dict)
            or any(not isinstance(info.get(k),str) or not info[k].strip() for k in ('language','gender','age'))):
            raise ValueError('invalid preset metadata')
    if not any(info.get('language')=='zh' for info in presets.values()):raise ValueError('no registered zh preset available')
    zh=root/'zh'
    if not zh.is_dir() or not zh.resolve(strict=True).is_relative_to(root):raise ValueError('isolated zh library missing or unsafe')
    count=0
    # Huashuo loads every */*.json entry, so validate every language it sees.
    for folder in root.iterdir():
        if not folder.is_dir():continue
        real=folder.resolve(strict=True)
        if not real.is_relative_to(root) or not _SAFE_LANG.fullmatch(folder.name):raise ValueError('unsafe voice language directory')
        metas={x.stem:x for x in folder.glob('*.json')};vectors={x.stem:x for x in folder.glob('*.npy')}
        if set(metas)!=set(vectors):raise ValueError('voice JSON and NPY files must be paired')
        for stem,meta_path in metas.items():
            if not _SAFE_NAME.fullmatch(stem) or '..' in stem:raise ValueError('invalid voice id')
            meta_path=_within(meta_path,root);vector=_within(vectors[stem],root)
            if not meta_path.is_file() or not vector.is_file() or vector.stat().st_size==0:raise ValueError('missing or empty voice asset')
            info=_object_file(meta_path,'voice metadata')
            if any(not isinstance(info.get(k),str) or not info[k].strip() for k in ('gender','age')):raise ValueError('voice metadata lacks gender/age')
            if 'id' in info and info['id']!=stem:raise ValueError('voice metadata ID differs from filename')
            if folder.name=='zh':count+=1
    if not count:raise ValueError('no paired zh voices available')
    return {'presets':len(presets),'zh_voices':count}

def _reject_links(workdir):
    # Huashuo reads/writes fixed descendants (script, state, cache). A symlink
    # there could otherwise redirect an apparently isolated operation.
    def walk_error(error):raise ValueError('cannot fully inspect staging workdir') from error
    for folder,dirs,files in os.walk(workdir,followlinks=False,onerror=walk_error):
        for name in dirs+files:
            entry=Path(folder)/name
            if entry.is_symlink():raise ValueError('stage contains a symlink; isolation cannot be guaranteed')
            if entry.is_file() and entry.stat().st_nlink > 1:raise ValueError('stage contains a hard-linked file')

def run_isolated(staging_root,library_root,book,workdir,command,*,chapters=None,output=None,output_format='mp3'):
    """Run a bounded Huashuo invocation and always restore ROOT/environment.

    Inputs/assets and any output must reside under staging_root. The caller is
    responsible for using a separate process and for offline model availability.
    """
    stage=_registered_stage(staging_root)
    library=_within(library_root,stage);source=_within(book,stage);wd=_within(workdir,stage)
    if not source.is_file() or not wd.is_dir():raise ValueError('isolated source/workdir missing')
    _reject_links(stage)
    if command not in _ALLOWED_COMMANDS:raise ValueError('command not allowed')
    if chapters is not None and (not isinstance(chapters,str) or not _CHAPTERS.fullmatch(chapters)):
        raise ValueError('invalid chapter selection')
    if command=='check' and (chapters is not None or output is not None):raise ValueError('check does not support chapter/output options')
    if command=='synth' and output is not None:raise ValueError('synth output option is not allowed')
    if command=='package':
        if output is None or output_format not in ('mp3','m4b'):raise ValueError('package requires a supported output format')
        target=_within(output,stage,must_exist=False)
    else:target=None
    args=[command,str(source),'--workdir',str(wd)]
    if chapters is not None:args.extend(['--chapters',chapters])
    if command in ('synth','package'):args.extend(['--no-opening','--no-closing'])
    if command=='synth':args.append('--no-asr')
    if command=='package':args.extend(['--format',output_format,'-o',str(target)])
    with _LOCK:
        library_module=importlib.import_module('huashuo.library')
        old_root=library_module.ROOT
        env_names=('HF_HUB_OFFLINE','HF_HOME','HUGGINGFACE_HUB_CACHE','HF_HUB_CACHE','TRANSFORMERS_CACHE','TMPDIR','TMP','TEMP')
        old_env={name:os.environ.get(name) for name in env_names}
        old_tempdir=tempfile.tempdir
        tmp=stage/'.tmp';hf=stage/'.hf'
        tmp.mkdir(exist_ok=True);hf.mkdir(exist_ok=True)
        if tmp.is_symlink() or hf.is_symlink():raise ValueError('unsafe cache directory')
        trusted_presets=_object_file(Path(old_root)/'presets.json','installed presets')
        validate_library(library,trusted_presets=trusted_presets)
        try:
            library_module.ROOT=library
            cache=getattr(library_module,'_load',None)
            if callable(getattr(cache,'cache_clear',None)):cache.cache_clear()
            os.environ.update({'HF_HUB_OFFLINE':'1','HF_HOME':str(hf),
                'HUGGINGFACE_HUB_CACHE':str(hf/'hub'),'HF_HUB_CACHE':str(hf/'hub'),
                'TRANSFORMERS_CACHE':str(hf/'transformers'),'TMPDIR':str(tmp),'TMP':str(tmp),'TEMP':str(tmp)})
            tempfile.tempdir=str(tmp)
            cli=importlib.import_module('huashuo.cli')
            return cli.main(args)
        finally:
            library_module.ROOT=old_root
            cache=getattr(library_module,'_load',None)
            if callable(getattr(cache,'cache_clear',None)):cache.cache_clear()
            tempfile.tempdir=old_tempdir
            for name,value in old_env.items():
                if value is None:os.environ.pop(name,None)
                else:os.environ[name]=value

def main():
    p=argparse.ArgumentParser(description=__doc__ + ' Use the Python create_staging_root API in the same process; existing directories are rejected.');p.add_argument('staging_root',type=Path);p.add_argument('library_root',type=Path)
    p.add_argument('command',choices=sorted(_ALLOWED_COMMANDS));p.add_argument('book',type=Path);p.add_argument('workdir',type=Path)
    p.add_argument('--chapters');p.add_argument('--output',type=Path);p.add_argument('--format',choices=['mp3','m4b'],default='mp3');a=p.parse_args()
    try:return run_isolated(a.staging_root,a.library_root,a.book,a.workdir,a.command,chapters=a.chapters,output=a.output,output_format=a.format)
    except (OSError,ValueError,ImportError) as exc:p.exit(2,'error: '+str(exc)+'\n')
if __name__=='__main__':raise SystemExit(main())
