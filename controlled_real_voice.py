"""Isolated single-use experiment controller; no book or network access.

Each canonical input is reserved forever; an existing reservation, including a
crashed/uncertain one, can never be automatically restarted. The global lock is
held until the worker exits; a crash leaves it occupied for manual inspection.
"""
import hashlib, json, math, os, shutil, signal, struct, subprocess, sys, time, wave
from pathlib import Path
import release_config

ROOT=release_config.mac_controlled_root().resolve()
def raw(x): return json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
def digest(b): return hashlib.sha256(b).hexdigest()
def atomic(path,value):
    tmp=path.with_name(path.name+'.tmp-'+str(os.getpid())); tmp.write_bytes(raw(value)); os.replace(tmp,path)
def identity(req):
    if set(req)!={'text','mode'} or req['mode'] not in ('synthetic','voice') or not isinstance(req['text'],str) or not 1<=len(req['text'])<=80: raise ValueError('invalid synthetic request')
    return digest(raw(req))
def jobpath(job):
    if len(job)!=64 or any(c not in '0123456789abcdef' for c in job): raise ValueError('invalid job')
    return ROOT/'jobs'/job
def state(job):
    p=jobpath(job)/'state.json'
    return json.loads(p.read_text(encoding='utf8'))
def start(req):
    job=identity(req); ROOT.mkdir(parents=True,exist_ok=True); (ROOT/'jobs').mkdir(exist_ok=True)
    lock=ROOT/'active.lock'
    try: lock.mkdir()
    except FileExistsError: return {'status':'busy_or_uncertain','job':job}
    created=False
    try:
        d=jobpath(job)
        try: d.mkdir(); created=True
        except FileExistsError:
            lock.rmdir(); return {'status':'already_reserved','job':job,'existing':state(job) if (d/'state.json').exists() else {'status':'uncertain'}}
        (d/'input.json').write_bytes(raw(req))
        atomic(d/'state.json',{'version':1,'job':job,'status':'reserved','input_sha256':job,'mode':req['mode'],'note':'unreviewed isolated experiment; cooperative protocol only'})
        subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'worker',job],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,start_new_session=(os.name!='nt'))
        return {'status':'reserved','job':job}
    except BaseException:
        if not created:
            try: lock.rmdir()
            except OSError: pass
        raise

def stop(job):
    d=jobpath(job); s=state(job)
    if s['status'] not in ('reserved','running'): return {'status':'not_running','job':job}
    try:
        with (d/'stop.request').open('x',encoding='utf8') as f: f.write('stop')
    except FileExistsError: pass
    return {'status':'stop_requested','job':job}

def render_voice(d, req):
    """Bounded fictional voice helper, invoked as a separate process group."""
    helper=Path(__file__).resolve().with_name('console_voice_render_mac.py')
    config=release_config.mac_controlled_voice();stage=config['stage'];vector_hash=config['vector_sha256']
    if not helper.is_file() or digest((stage/'speaker-vector.npy').read_bytes())!=vector_hash: raise ValueError('configured voice helper or vector is missing or changed')
    payload={'action':'render','stage':str(stage),'vector_sha256':vector_hash,'text':req['text']}
    out=d/'helper.stdout'; err=d/'helper.stderr'; input_path=d/'helper-input.json'; input_path.write_bytes(raw(payload))
    with input_path.open('rb') as stdin, out.open('wb') as stdout, err.open('wb') as stderr:
        proc=subprocess.Popen([sys.executable,str(helper)],stdin=stdin,stdout=stdout,stderr=stderr,start_new_session=True)
        deadline=time.monotonic()+700
        while proc.poll() is None:
            if (d/'stop.request').exists():
                os.killpg(proc.pid,signal.SIGTERM)
                try: proc.wait(timeout=30)
                except subprocess.TimeoutExpired: os.killpg(proc.pid,signal.SIGKILL); proc.wait()
                try: os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                raise InterruptedError('stop requested')
            if time.monotonic()>deadline:
                os.killpg(proc.pid,signal.SIGTERM)
                try: proc.wait(timeout=30)
                except subprocess.TimeoutExpired: os.killpg(proc.pid,signal.SIGKILL); proc.wait()
                try: os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                raise TimeoutError('helper timed out')
            time.sleep(.25)
    if proc.returncode: raise RuntimeError('voice helper failed')
    result=json.loads(out.read_text(encoding='utf8').strip().splitlines()[-1]); audio=Path(result['wav'])
    allowed=release_config.mac_voice_root().resolve()
    if result.get('ok') is not True or result.get('text_sha256')!=digest(req['text'].encode()) or result.get('vector_sha256')!=vector_hash or audio.parent.parent.resolve()!=allowed or not audio.parent.name.startswith('render-') or audio.name!='rendered.wav' or audio.is_symlink() or digest(audio.read_bytes())!=result.get('wav_sha256'): raise ValueError('voice output verification failed')
    shutil.copyfile(audio,d/'audio.wav')

def worker(job):
    d=jobpath(job); req=json.loads((d/'input.json').read_bytes()); lock=ROOT/'active.lock'
    if identity(req)!=job or not lock.is_dir(): raise ValueError('invalid reservation')
    status={'version':1,'job':job,'status':'running','input_sha256':job,'mode':req['mode'],'note':'unreviewed isolated experiment; cooperative protocol only'}
    atomic(d/'state.json',status)
    try:
        target=d/'audio.wav'
        if req['mode']=='voice':
            if (d/'stop.request').exists(): raise InterruptedError('stop requested')
            render_voice(d,req)
        else:
            # Long enough to exercise process-level cancellation in tests.
            with wave.open(str(target),'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
                for i in range(50):
                    if (d/'stop.request').exists(): raise InterruptedError('stop requested')
                    frames=b''.join(struct.pack('<h',int(1000*math.sin(2*math.pi*440*(i*320+j)/16000))) for j in range(320))
                    wav.writeframes(frames); time.sleep(.02)
        with wave.open(str(target),'rb') as wav:
            if wav.getnframes()<=0 or wav.getcomptype()!='NONE': raise ValueError('invalid audio')
        data=target.read_bytes(); status.update(status='complete',audio_sha256=digest(data),audio_bytes=len(data),audio_path=str(target.resolve()))
    except InterruptedError: status.update(status='interrupted')
    except BaseException as exc: status.update(status='failed',error_type=type(exc).__name__,lock_retained=(req['mode']=='voice'))
    finally:
        atomic(d/'state.json',status)
        if not status.get('lock_retained'):
            try: lock.rmdir()
            except OSError: pass

def main():
    if len(sys.argv)==3 and sys.argv[1]=='worker': worker(sys.argv[2]);return
    req=json.load(sys.stdin); action=req.get('action')
    if action=='start' and set(req)=={'action','input'}: result=start(req['input'])
    elif action=='status' and set(req)=={'action','job'}: result=state(req['job'])
    elif action=='stop' and set(req)=={'action','job'}: result=stop(req['job'])
    else: raise ValueError('invalid action')
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':
    try: main()
    except Exception as e: print(json.dumps({'status':'error','error':str(e)},ensure_ascii=False));sys.exit(1)
