"""Standalone experimental character voice console; never edits existing books."""
import hashlib, json, os, secrets, subprocess, threading, time, tkinter as tk, uuid, wave, winsound
from pathlib import Path, PurePosixPath
from tkinter import ttk, messagebox
import release_config

ROOT = release_config.local_data_root() / 'voice-console'

def ssh_python(worker, payload, timeout):
    command = ['ssh', *release_config.ssh_options(), release_config.ssh_target(), release_config.remote_command(worker)]
    return subprocess.run(command, input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                          text=True, encoding='utf-8', timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)

def scp_from_mac(remote_path, local_path, timeout=120):
    command = ['scp', *release_config.ssh_options(), release_config.scp_source(remote_path), str(local_path)]
    return subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW)

def suggestion(description):
    return '虚构人物的中文声音，' + description.strip() + '。请保持自然、清晰、稳定，不模仿现实人物。'

def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def invoke(description, text, directory):
    req = {'description':description, 'text':text, 'seed':secrets.randbits(32)}
    p = ssh_python('console_voice_mac.py', req, 1000)
    try: result = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError,IndexError): raise RuntimeError('Mac 连接或执行失败：'+(p.stderr or p.stdout)[-1000:])
    if not result.get('ok'): raise RuntimeError(result.get('error','生成失败'))
    stage = result['stage']
    stage_root = release_config.remote_voice_root()
    if not release_config.remote_path_is_within(stage, stage_root) or PurePosixPath(stage).parent != stage_root or not PurePosixPath(stage).name.startswith('voice-experiment-'):
        raise RuntimeError('返回路径无效')
    for filename, key in [('generated.wav','wav_sha256'),('speaker-vector.npy','vector_sha256')]:
        path=directory/filename
        p=scp_from_mac(stage+'/'+filename,path)
        if p.returncode or digest(path)!=result[key]: raise RuntimeError('下载或校验失败：'+filename)
    with wave.open(str(directory/'generated.wav'),'rb') as wav:
        if wav.getnchannels()!=1 or wav.getsampwidth()!=2 or wav.getframerate()!=24000 or not wav.getnframes(): raise RuntimeError('试听文件格式无效')
        if len(wav.readframes(wav.getnframes())) != wav.getnframes()*2: raise RuntimeError('试听文件不完整')
    req.update(stage=stage, wav_sha256=result['wav_sha256'], vector_sha256=result['vector_sha256'])
    return req

def render_fixed(voice, text, directory):
    req=voice['request']; vector=Path(voice['directory'])/'speaker-vector.npy'
    if digest(vector)!=req.get('vector_sha256'): raise RuntimeError('固定声音向量已改变，请重新生成。')
    payload={'action':'render','stage':req['stage'],'vector_sha256':req['vector_sha256'],'text':text}
    p=ssh_python('console_voice_render_mac.py',payload,700)
    try: result=json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError,IndexError): raise RuntimeError('Mac 渲染连接失败：'+(p.stderr or p.stdout)[-1000:])
    if not result.get('ok'): raise RuntimeError(result.get('error','渲染失败'))
    if result.get('text_sha256')!=hashlib.sha256(text.encode('utf-8')).hexdigest() or result.get('vector_sha256')!=req['vector_sha256']: raise RuntimeError('文字或声音身份不一致')
    remote=result['wav']
    if not release_config.remote_path_is_within(remote,release_config.remote_voice_root()) or PurePosixPath(remote).name!='rendered.wav': raise RuntimeError('渲染返回路径无效')
    path=directory/'rendered.wav';p=scp_from_mac(remote,path)
    if p.returncode or digest(path)!=result['wav_sha256']: raise RuntimeError('渲染音频下载或校验失败')
    with wave.open(str(path),'rb') as wav:
        if wav.getnchannels()!=1 or wav.getsampwidth()!=2 or wav.getframerate()!=24000 or not wav.getnframes(): raise RuntimeError('渲染音频格式无效')
        if len(wav.readframes(wav.getnframes()))!=wav.getnframes()*2: raise RuntimeError('渲染音频不完整')
    return path

def chapter_rpc(payload):
    p=ssh_python('continuity_workbench.py',payload,90)
    try: result=json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError,IndexError): raise RuntimeError('连续批次连接失败：'+(p.stderr or p.stdout)[-1000:])
    if not result.get('ok'): raise RuntimeError(result.get('error','连续批次失败'))
    return result

def render_chapters(voice,directory):
    req=voice['request'];vector=Path(voice['directory'])/'speaker-vector.npy'
    if digest(vector)!=req.get('vector_sha256'): raise RuntimeError('固定声音向量已改变，请重新生成。')
    result=chapter_rpc({'action':'start','stage':req['stage'],'vector_sha256':req['vector_sha256']})
    if result.get('vector_sha256')!=req['vector_sha256']: raise RuntimeError('连续批次使用的声音身份不一致')
    run=result['run_id']
    if not release_config.remote_path_is_within(run,release_config.remote_stage_root()): raise RuntimeError('返回的实验目录无效')
    deadline=time.monotonic()+1800
    while time.monotonic()<deadline:
        result=chapter_rpc({'action':'status','run_id':run})
        if result.get('run_id')!=run or result.get('vector_sha256')!=req['vector_sha256']: raise RuntimeError('连续批次身份不一致')
        if result.get('manifest') is not None: break
        if result.get('status') in ('failed','interrupted','unknown'): raise RuntimeError('连续批次状态：'+result['status'])
        time.sleep(3)
    else: raise RuntimeError('等待超时；实验未自动重试，保留 Mac 端结果。')
    if result.get('vector_load_verified') is not True: raise RuntimeError('未验证连续批次实际读取固定声音向量')
    manifest=result['manifest'];entries=manifest['chapters']
    if len(entries)!=2: raise RuntimeError('实验未返回两章')
    paths=[]
    for index,entry in enumerate(entries):
        remote=entry['path']
        if not remote.startswith(run+'/') or '..' in remote: raise RuntimeError('章节文件路径无效')
        path=directory/f'chapter-{index+1}.wav'
        p=subprocess.run(['scp',*OPTIONS,HOST+':'+remote,str(path)],capture_output=True,text=True,encoding='utf-8',timeout=180,creationflags=subprocess.CREATE_NO_WINDOW)
        if p.returncode or digest(path)!=entry['sha256'] or path.stat().st_size!=entry['bytes']: raise RuntimeError('章节下载或校验失败')
        with wave.open(str(path),'rb') as wav:
            if not wav.getnframes() or wav.getnchannels()<1: raise RuntimeError('章节 WAV 无效')
        paths.append(path)
    return paths

class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title('角色声音工作台（独立试验）'); self.geometry('800x800')
        ROOT.mkdir(parents=True,exist_ok=True); self.book=tk.StringVar(); self.role=tk.StringVar(); self.status=tk.StringVar(value='填写书名与人物，生成声音。')
        self.dbpath=ROOT/'assignments.json'; self.db=json.loads(self.dbpath.read_text(encoding='utf-8')) if self.dbpath.exists() else {}; self.current=None
        self.runspath=ROOT/'chapter-runs.json';self.runs=json.loads(self.runspath.read_text(encoding='utf-8')) if self.runspath.exists() else {}
        box=ttk.Frame(self,padding=14); box.pack(fill='both',expand=True)
        ttk.Label(box,text='角色声音工作台',font=('Microsoft YaHei UI',16,'bold')).pack(anchor='w')
        ttk.Label(box,text='独立试验声库 · 仅文字生成虚构声音 · 不修改原有书籍或声库').pack(anchor='w',pady=(0,12))
        row=ttk.Frame(box);row.pack(fill='x');ttk.Label(row,text='书名').pack(side='left');ttk.Entry(row,textvariable=self.book,width=24).pack(side='left',padx=6);ttk.Label(row,text='人物').pack(side='left');ttk.Entry(row,textvariable=self.role,width=24).pack(side='left',padx=6);ttk.Button(row,text='读取已固定声音',command=self.load).pack(side='left')
        ttk.Label(box,text='人物文字描述（默认作为声音建议，可直接修改）').pack(anchor='w',pady=(12,2));self.description=tk.Text(box,height=4,wrap='word');self.description.pack(fill='x')
        ttk.Label(box,text='试听台词').pack(anchor='w',pady=(10,2));self.sample=tk.Text(box,height=3,wrap='word');self.sample.pack(fill='x');self.sample.insert('1.0','天亮了，我们一起出发吧。')
        row=ttk.Frame(box);row.pack(fill='x',pady=12);self.generate=ttk.Button(row,text='生成并试听',command=self.start);self.generate.pack(side='left');ttk.Button(row,text='播放当前试听',command=self.play).pack(side='left',padx=8);ttk.Button(row,text='固定为该人物声音',command=self.save).pack(side='left')
        ttk.Label(box,textvariable=self.status,wraplength=700).pack(anchor='w',pady=6)
        row=ttk.Frame(box);row.pack(fill='x',pady=6);self.render_button=ttk.Button(row,text='用固定声音朗读试听台词',command=self.render);self.render_button.pack(side='left');ttk.Button(row,text='播放朗读结果',command=self.play_rendered).pack(side='left',padx=8)
        self.rendered=None
        ttk.Label(box,text='固定记录保存在本工作台中，可按书和人物重新读取、更换。朗读使用固定向量并输出隔离文件；每次最多 500 字，尚未接入整书配音。',wraplength=700).pack(anchor='w',pady=10)
        ttk.Separator(box).pack(fill='x',pady=8)
        ttk.Label(box,text='自写双章连续批次（独立实验）',font=('Microsoft YaHei UI',10,'bold')).pack(anchor='w')
        ttk.Label(box,text='以当前书籍、人物的固定声音朗读两章自写样本，音频保存在独立目录。',wraplength=700).pack(anchor='w',pady=3)
        row=ttk.Frame(box);row.pack(fill='x',pady=5);self.batch_button=ttk.Button(row,text='生成自写双章',command=self.start_chapters);self.batch_button.pack(side='left')
        self.chapters=[];self.chapter_buttons=[]
        for index in range(2):
            button=ttk.Button(row,text=f'播放第 {index+1} 章',state='disabled',command=lambda n=index:self.play_chapter(n));button.pack(side='left',padx=8);self.chapter_buttons.append(button)
        ttk.Separator(box).pack(fill='x',pady=8)
        ttk.Button(box,text='查看 DeepSeek 用量与费用估算',command=self.open_billing).pack(anchor='w')
        self.book.trace_add('write',self.identity_changed);self.role.trace_add('write',self.identity_changed)
    def open_billing(self):
        from console_voice_billing import BillingWindow
        BillingWindow(self)
    def key(self):
        book=self.book.get().strip();role=self.role.get().strip()
        if not book or not role: raise ValueError('请填写书名和人物。')
        return book+'\u0000'+role
    def start(self):
        try:
            key=self.key();desc=self.description.get('1.0','end').strip();sample=self.sample.get('1.0','end').strip()
            if not desc or not sample or len(desc)>1500 or len(sample)>500: raise ValueError('请输入不超过 1500 字的人物描述和不超过 500 字的试听台词。')
        except ValueError as e: messagebox.showerror('无法生成',str(e));return
        prior_hash=self.db.get(key,{}).get('request',{}).get('vector_sha256')
        directory=ROOT/'candidates'/uuid.uuid4().hex;directory.mkdir(parents=True)
        self.generate.config(state='disabled');self.status.set('正在 Mac 本机生成和编码，请稍候……')
        def worker():
            try:
                req=invoke(suggestion(desc),sample,directory)
                self.after(0,lambda:self.completed(directory,req,key,prior_hash))
            except Exception as exc:
                error=str(exc);self.after(0,lambda:self.failed(error))
        threading.Thread(target=worker,daemon=True).start()
    def completed(self,directory,req,key,prior_hash):
        candidate={'directory':str(directory),'request':req,'book_role_key':key};self.generate.config(state='normal')
        (directory/'candidate.json').write_text(json.dumps(candidate,ensure_ascii=False,indent=2),encoding='utf-8')
        if self.db.get(key,{}).get('request',{}).get('vector_sha256')!=prior_hash:
            self.status.set('原人物的候选声音已保留；任务期间固定声音发生变化，未替换或播放。');return
        if key not in self.db:
            try: self.persist(key,candidate); notice='已作为该人物的默认建议固定。'
            except OSError: notice='自动保存失败，请手动固定。'
        else: notice='已有固定声音未被替换；满意后点击固定以更换。'
        if self.selected_key()!=key:
            self.status.set('原人物的候选声音已生成并保存在独立目录。'+notice+' 当前选择未改变。');return
        self.current=candidate
        self.status.set('生成完成，正在播放。'+notice);self.play()
    def failed(self,error): self.generate.config(state='normal');self.status.set('生成失败，未自动重试。');messagebox.showerror('生成失败',error)
    def play(self):
        if not self.current: messagebox.showinfo('试听','请先生成或读取声音。');return
        try: winsound.PlaySound(str(Path(self.current['directory'])/'generated.wav'),winsound.SND_FILENAME|winsound.SND_ASYNC)
        except RuntimeError as e: messagebox.showerror('播放失败',str(e))
    def save(self):
        try:
            key=self.key()
            if not self.current: raise ValueError('请先生成或读取声音。')
            if self.current.get('book_role_key') != key: raise ValueError('当前声音属于其他书或人物，请先重新生成或读取。')
            self.persist(key,self.current);self.clear_chapters();self.rendered=None;winsound.PlaySound(None,0);self.status.set('已固定到独立声库，可随时重新生成并更换。')
        except (ValueError,OSError) as e: messagebox.showerror('保存失败',str(e))
    def load(self):
        try:
            value=self.db.get(self.key())
            if not value or not (Path(value['directory'])/'generated.wav').is_file(): raise ValueError('该书和人物没有可读取的声音。')
            self.current=value;self.rendered=None;winsound.PlaySound(None,0);self.status.set('已读取固定声音，可播放或重新生成。')
            record=self.runs.get(self.key(),{});paths=[Path(p) for p in record.get('paths',[])] if record.get('vector_sha256')==value['request'].get('vector_sha256') else []
            self.chapters=paths if len(paths)==2 and all(p.is_file() for p in paths) else []
            for button in self.chapter_buttons:button.config(state='normal' if self.chapters else 'disabled')
        except ValueError as e: messagebox.showinfo('未找到',str(e))
    def persist(self,key,value):
        updated=dict(self.db);updated[key]=value;temp=self.dbpath.with_suffix('.tmp');temp.write_text(json.dumps(updated,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,self.dbpath);self.db=updated
    def render(self):
        try:
            key=self.key();voice=self.db.get(key);text=self.sample.get('1.0','end').strip()
            if not voice or 'stage' not in voice.get('request',{}): raise ValueError('请先生成并固定新声音。')
            if not text or len(text)>500: raise ValueError('请输入不超过 500 字的朗读文字。')
        except ValueError as e: messagebox.showerror('无法朗读',str(e));return
        directory=ROOT/'renders'/uuid.uuid4().hex;directory.mkdir(parents=True)
        self.render_button.config(state='disabled');self.status.set('正在使用固定声音朗读……')
        def worker():
            try:
                path=render_fixed(voice,text,directory);self.after(0,lambda:self.render_done(path,key,voice['request']['vector_sha256']))
            except Exception as exc:
                error=str(exc);self.after(0,lambda:self.render_failed(error))
        threading.Thread(target=worker,daemon=True).start()
    def render_done(self,path,key,vector_sha256):
        self.render_button.config(state='normal')
        if self.selected_key()!=key or self.db.get(key,{}).get('request',{}).get('vector_sha256')!=vector_sha256:
            self.status.set('原人物或原固定声音的朗读已生成并保留；当前选择或固定声音已改变，未自动播放。');return
        self.rendered=path;self.status.set('固定声音朗读完成，正在播放。');self.play_rendered()
    def render_failed(self,error): self.render_button.config(state='normal');self.status.set('朗读失败，未自动重试。');messagebox.showerror('朗读失败',error)
    def play_rendered(self):
        if not self.rendered: messagebox.showinfo('播放','请先生成朗读结果。');return
        try: winsound.PlaySound(str(self.rendered),winsound.SND_FILENAME|winsound.SND_ASYNC)
        except RuntimeError as e: messagebox.showerror('播放失败',str(e))
    def start_chapters(self):
        try:
            key=self.key();voice=self.db.get(key)
            if not voice or 'stage' not in voice.get('request',{}): raise ValueError('请先生成并固定新声音。')
        except ValueError as e: messagebox.showerror('无法生成',str(e));return
        directory=ROOT/'chapter-runs'/uuid.uuid4().hex;directory.mkdir(parents=True)
        self.batch_button.config(state='disabled');self.status.set('正在用固定声音生成自写双章，请稍候……')
        def worker():
            try:
                chapters=render_chapters(voice,directory);self.after(0,lambda:self.chapters_done(chapters,key,voice['request']['vector_sha256']))
            except Exception as exc:
                error=str(exc);self.after(0,lambda:self.chapters_failed(error))
        threading.Thread(target=worker,daemon=True).start()
    def chapters_done(self,chapters,key,vector_sha256):
        self.runs[key]={'paths':[str(p) for p in chapters],'vector_sha256':vector_sha256};tmp=self.runspath.with_suffix('.tmp');tmp.write_text(json.dumps(self.runs,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(tmp,self.runspath)
        self.batch_button.config(state='normal');self.chapters=[]
        if self.book.get().strip()+'\u0000'+self.role.get().strip()==key and self.db.get(key,{}).get('request',{}).get('vector_sha256')==vector_sha256:
            self.chapters=chapters
        for button in self.chapter_buttons:button.config(state='normal' if self.chapters else 'disabled')
        self.status.set('自写双章已生成并校验，可分别播放。归属：'+key.replace('\u0000',' / '))
    def chapters_failed(self,error): self.batch_button.config(state='normal');self.status.set('双章实验失败，未自动重试。');messagebox.showerror('双章实验失败',error)
    def play_chapter(self,index):
        try: winsound.PlaySound(str(self.chapters[index]),winsound.SND_FILENAME|winsound.SND_ASYNC)
        except (RuntimeError,IndexError) as e: messagebox.showerror('播放失败',str(e))
    def clear_chapters(self,*_):
        self.chapters=[]
        for button in self.chapter_buttons:button.config(state='disabled')
    def identity_changed(self,*_):
        self.clear_chapters();self.current=None;self.rendered=None;winsound.PlaySound(None,0)
    def selected_key(self):
        return self.book.get().strip()+'\u0000'+self.role.get().strip()

if __name__=='__main__': App().mainloop()
