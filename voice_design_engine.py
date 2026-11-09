"""Generate a synthetic voice from text, or encode that generated voice. No reference uploads."""
import json
import sys
import time
import wave
from pathlib import Path
import release_config

import mlx.core as mx
import numpy as np
from mlx_audio.tts.utils import load_model

root=release_config.mac_data_root()
request=json.load(sys.stdin)
action=request['action']
if action=='design':
    model=load_model(str(release_config.mac_voice_design_model_root()))
    mx.random.seed(int(request['seed']))
    result=model.generate_voice_design(text=request['text'],instruct=request['description'],language='Chinese',temperature=.85,top_p=.95,top_k=50,repetition_penalty=1.05,max_tokens=700)
    chunks=[];rate=None
    for part in result:
        mx.eval(part.audio);chunks.append(np.asarray(part.audio,dtype=np.float32).reshape(-1));rate=int(part.sample_rate)
    if not chunks: raise RuntimeError('未生成音频')
    samples=np.concatenate(chunks)
    with wave.open(request['wav'],'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate);w.writeframes((np.clip(samples,-1,1)*32767).astype('<i2').tobytes())
    print('designed')
elif action=='encode':
    with wave.open(request['wav'],'rb') as w:
        if w.getnchannels()!=1 or w.getsampwidth()!=2 or w.getframerate()!=24000:raise ValueError('候选声音必须是本程序生成的 24 kHz 单声道音频')
        samples=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(np.float32)/32767
    model=load_model('mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit')
    vector=model.extract_speaker_embedding(mx.array(samples));mx.eval(vector)
    a=np.asarray(vector,dtype=np.float32).reshape(-1)
    if a.size!=2048 or not np.isfinite(a).all():raise ValueError('生成的声音向量无效')
    np.save(request['vector'],a,allow_pickle=False)
    print('encoded')
else: raise ValueError('unknown action')
