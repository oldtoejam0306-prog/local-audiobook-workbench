"""Fail-closed process identity checks for an isolated Mac batch experiment.

The experiment controller must create and retain the direct worker Popen handle.
Only descendants observed under that handle, with isolated cwd, may be signalled.
"""
import os, re, signal, subprocess
from pathlib import Path

class UnsafeProcess(RuntimeError): pass

def _table():
 raw=subprocess.check_output(['/bin/ps','-axo','pid=,ppid=,lstart=,command='],text=True)
 table={}
 for line in raw.splitlines():
  p=line.strip().split(None,7)
  if len(p)==8 and p[0].isdigit() and p[1].isdigit():
   table[int(p[0])]={'ppid':int(p[1]),'started':' '.join(p[2:7]),'command':p[7]}
 return table

def _cwd(pid):
 raw=subprocess.check_output(['/usr/sbin/lsof','-a','-p',str(pid),'-d','cwd','-Fn'],text=True)
 paths=[line[1:] for line in raw.splitlines() if line.startswith('n')]
 if len(paths)!=1: raise UnsafeProcess('unable to uniquely verify process working directory')
 return Path(paths[0]).resolve(strict=True)

def snapshot(worker, book_id, experiment_home):
 home=Path(experiment_home).resolve(strict=True)
 if not home.name.startswith('continuity-synthetic-'): raise UnsafeProcess('unexpected experiment home')
 if not re.fullmatch(r'book-[0-9a-f]{20}',book_id): raise UnsafeProcess('invalid book ID')
 if worker.poll() is not None: raise UnsafeProcess('worker already exited')
 table=_table(); record=table.get(worker.pid)
 if (not record or record['ppid']!=os.getpid() or 'continuity_runner.py' not in record['command']
     or '--root' not in record['command']): raise UnsafeProcess('worker is not our direct child')
 children=set(); changed=True
 while changed:
  changed=False
  for pid,row in table.items():
   if pid not in children and (row['ppid']==worker.pid or row['ppid'] in children):
    children.add(pid); changed=True
 selected={pid:table[pid] for pid in [worker.pid,*sorted(children)]}
 for pid,row in selected.items():
  cwd=_cwd(pid)
  if not cwd.is_relative_to(home): raise UnsafeProcess('process cwd outside experiment home')
  row['cwd']=str(cwd)
 return selected

def signal_checked(worker, book_id, home, expected, pid, sig):
 if sig not in (signal.SIGSTOP,signal.SIGTERM,signal.SIGKILL): raise UnsafeProcess('signal disallowed')
 current=snapshot(worker,book_id,home)
 if pid not in expected or current.get(pid)!=expected[pid] or set(current)!=set(expected):
  raise UnsafeProcess('process identity or descendant set changed')
 os.kill(pid,sig)

