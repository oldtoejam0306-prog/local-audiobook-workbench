"""Isolated v2 chapter reservation protocol. No real renderer or network.

Persistent directory locks are cooperative, not an OS sandbox or legacy global lock.
Unknown and failed outcomes remain reserved and are never automatically retried.
"""
import hashlib,json,os,re
from pathlib import Path

BOOK=re.compile(r'book-[0-9a-f]{20}\Z')
CHAPTER=re.compile(r'c\d+(?:\.p\d+(?:\.\d+)?)?\Z')
DIGEST=re.compile(r'[0-9a-f]{64}\Z')
FIELDS=('book_id','chapter_id','source_sha256','index_sha256','voice_mapping_sha256')

def canonical(x):
 return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf8')

def validate(identity):
 if not isinstance(identity,dict) or set(identity)!=set(FIELDS):raise ValueError('invalid identity fields')
 if not isinstance(identity['book_id'],str) or not BOOK.fullmatch(identity['book_id']):raise ValueError('invalid book')
 if not isinstance(identity['chapter_id'],str) or not CHAPTER.fullmatch(identity['chapter_id']):raise ValueError('invalid chapter')
 for key in FIELDS[2:]:
  if not isinstance(identity[key],str) or not DIGEST.fullmatch(identity[key]):raise ValueError('invalid digest')
 return hashlib.sha256(canonical(identity)).hexdigest()

def _write_new(path,value):
 with path.open('xb') as f:
  f.write(canonical(value));f.flush();os.fsync(f.fileno())

def _read(path):
 if path.is_symlink() or not path.is_file():raise ValueError('invalid record')
 return json.loads(path.read_bytes())

class Protocol:
 def __init__(self,root,legacy_root):
  self.root=Path(root).resolve();self.legacy_root=Path(legacy_root).resolve()
  if self.root==self.legacy_root or self.root in self.legacy_root.parents or self.legacy_root in self.root.parents:raise ValueError('roots must be separate')
 def reserve(self,identity,source_bytes=None,voice_mapping=None):
  job=validate(identity); self.root.mkdir(parents=True,exist_ok=True)
  if source_bytes is not None and (not isinstance(source_bytes,bytes) or hashlib.sha256(source_bytes).hexdigest()!=identity['source_sha256'] or hashlib.sha256(canonical(voice_mapping)).hexdigest()!=identity['voice_mapping_sha256']):raise ValueError('stage content mismatch')
  lock=self.root/'reservation.lock'
  try:lock.mkdir()
  except FileExistsError:return {'status':'busy_or_uncertain','job':job}
  try:
   # Legacy records lack book identity; any incomplete or unreadable record blocks.
   jobs=self.legacy_root/'jobs'
   if jobs.exists():
    if jobs.is_symlink() or not jobs.is_dir():return {'status':'legacy_uncertain','job':job}
    for d in jobs.iterdir():
     try:
      if d.is_symlink() or not d.is_dir() or _read(d/'state.json').get('status')!='complete':return {'status':'legacy_uncertain','job':job}
     except (OSError,ValueError,TypeError,json.JSONDecodeError):return {'status':'legacy_uncertain','job':job}
   book=self.root/'books'/identity['book_id'];book.mkdir(parents=True,exist_ok=True)
   chapter=book/identity['chapter_id']
   try:chapter.mkdir()
   except FileExistsError:
    if chapter.is_symlink() or not chapter.is_dir():return {'status':'conflict_or_uncertain','job':job}
    try:existing=_read(chapter/'identity.json');state=_read(chapter/'state.json')
    except (OSError,ValueError,json.JSONDecodeError):return {'status':'conflict_or_uncertain','job':job}
    if existing!=identity or state.get('job')!=job:return {'status':'conflict_or_uncertain','job':job}
    outcome=chapter/'outcome.json'
    if outcome.exists():
     try:
      event=_read(outcome)
      if event.get('job')!=job or event.get('status') not in ('complete','failed','unknown'):raise ValueError('invalid outcome')
      state=event
     except (OSError,ValueError,json.JSONDecodeError):return {'status':'conflict_or_uncertain','job':job}
    return {'status':'already_reserved','job':job,'state':state.get('status')}
   _write_new(chapter/'identity.json',identity)
   if source_bytes is not None:
    with (chapter/'source.bin').open('xb') as f:f.write(source_bytes);f.flush();os.fsync(f.fileno())
    _write_new(chapter/'voice-mapping.json',voice_mapping)
   _write_new(chapter/'state.json',{'version':2,'job':job,'status':'reserved','retry_allowed':False})
   return {'status':'reserved','job':job}
  finally:lock.rmdir()
 def run_mock(self,identity,worker):
  """Test seam only: caller-provided mock; no renderer is available here."""
  result=self.reserve(identity)
  if result['status']!='reserved':return result
  chapter=self.root/'books'/identity['book_id']/identity['chapter_id']
  try:
   output=worker(dict(identity))
   if output not in ('complete','failed','unknown'):output='unknown'
  except BaseException:
   output='unknown'
  # Immutable terminal event. Missing event means uncertain, never retry.
  _write_new(chapter/'outcome.json',{'job':result['job'],'status':output,'retry_allowed':False})
  return {'job':result['job'],'status':output}
