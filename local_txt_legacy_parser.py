"""Offline candidate reconstruction of historical Huashuo 0.2.0 TXT text.

No files are written and no model or provider is invoked. The caller must supply
and independently authenticate the exact Huashuo implementation, its runtime
dependencies, the safety patch and the expected existing-workdir text hash. A
matching SHA proves byte equivalence only; it is not human review or proof of
original source authenticity. Use the official adapter in a dedicated process:
it installs an in-memory section-reading patch in the Huashuo structure module.
"""
from __future__ import annotations
import hashlib,importlib,importlib.util,json,re,sys
from pathlib import Path
from typing import Any,Mapping
import local_txt_parser

PARSER_ID='local-txt-huashuo-legacy-candidate'
PARSER_VERSION='1'
PATCH_ID='long-number-safe-v1'
_HASH=re.compile(r'^[0-9a-f]{64}$')
_TRIM=' \t\u3000\xa0'


def _sha(raw:bytes)->str:return hashlib.sha256(raw).hexdigest()
def _json(value:Any)->bytes:return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
def _file_hash(path:Path)->str:
 h=hashlib.sha256()
 with path.open('rb') as f:
  for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
 return h.hexdigest()

class OfficialHuashuoRuntime:
 """Explicit local 0.2.0 adapter; caller authenticates source pins and environment."""
 def __init__(self,*,source_root:str|Path,guard_path:str|Path,expected_file_sha256:Mapping[str,str],patch_id:str):
  if patch_id!=PATCH_ID:raise ValueError('explicit long-number safety patch required')
  root=Path(source_root).resolve(strict=True);guard=Path(guard_path).resolve(strict=True)
  if not root.is_dir() or not guard.is_file():raise ValueError('local implementation files unavailable')
  modules={name:importlib.import_module(name) for name in ('huashuo','huashuo.structure','huashuo.punct','huashuo.ingest','huashuo.ingest.txt')}
  paths={name:Path(module.__file__).resolve(strict=True) for name,module in modules.items()}
  if any(not path.is_relative_to(root) for path in paths.values()):raise ValueError('Huashuo imports escaped caller-pinned source root')
  paths['safety_patch']=guard
  hashes={name:_file_hash(path) for name,path in paths.items()}
  if not isinstance(expected_file_sha256,Mapping) or hashes!=dict(expected_file_sha256):raise ValueError('Huashuo implementation/patch hashes differ from caller pins')
  if getattr(modules['huashuo'],'__version__',None)!='0.2.0':raise ValueError('unsupported Huashuo version')
  spec=importlib.util.spec_from_file_location('_legacy_safe_import_patch',guard)
  if spec is None or spec.loader is None:raise ValueError('safety patch loader unavailable')
  patch=importlib.util.module_from_spec(spec);spec.loader.exec_module(patch)
  if not callable(getattr(patch,'patch_structure',None)):raise ValueError('safety patch interface unavailable')
  patch.patch_structure(modules['huashuo.structure'])
  self._modules=modules;self._paths=paths;self._hashes=hashes
  self.identity={'huashuo_version':'0.2.0','patch_id':PATCH_ID,'file_sha256':hashes}
 def recheck(self):
  if {name:_file_hash(path) for name,path in self._paths.items()}!=self._hashes:raise ValueError('Huashuo implementation changed during parse')
 def make_book(self,lines,title,author):
  ingest=self._modules['huashuo.ingest'];txt=self._modules['huashuo.ingest.txt'];paras=[ingest.Paragraph(line) for line in lines]
  return ingest.Book(title=title,author=author if author is not None else txt.author_from_opening(paras),language=None,sections=[ingest.Section(paras)],format='txt',encoding='utf-8')
 def unwrap(self,book,language):return self._modules['huashuo.structure'].unwrap(book.sections[0].paragraphs,language)
 def normalized_paragraphs(self,paragraphs,language):return [self._modules['huashuo.punct'].normalize(p.text,language) for p in paragraphs]
 def paragraph_texts(self,paragraphs):return [p.text for p in paragraphs]
 def build_text(self,book,language,read_notes):return self._modules['huashuo.structure'].build(book,language,read_notes,split=False).text


def parse_legacy_txt_bytes(source_bytes:bytes,*,runtime:Any,expected_runtime_identity:Mapping[str,Any],
                           expected_legacy_text_sha256:str|None,language:str='zh',title:str='book',
                           author:str|None=None,read_notes:bool=False,source_name:str='original.txt')->dict[str,Any]:
 """Rebuild candidate; only an independent existing text hash can mark match.

 Runtime identity is a caller-supplied trust input, not an in-process security
 boundary; fake runtime injection is intended solely for tests. Exact behavior
 for arbitrary books is never promised when hash is absent or differs.
 """
 if expected_legacy_text_sha256 is not None and (not isinstance(expected_legacy_text_sha256,str) or not _HASH.fullmatch(expected_legacy_text_sha256)):raise ValueError('invalid expected historical text hash')
 if language not in ('zh','en') or not isinstance(title,str) or not title.strip() or (author is not None and not isinstance(author,str)) or type(read_notes) is not bool:raise ValueError('invalid build options')
 identity=getattr(runtime,'identity',None)
 if not isinstance(identity,dict) or identity!=dict(expected_runtime_identity) or identity.get('huashuo_version')!='0.2.0' or identity.get('patch_id')!=PATCH_ID:raise ValueError('runtime identity or explicit safety patch differs from trusted expectation')
 if not callable(getattr(runtime,'recheck',None)):raise ValueError('runtime recheck unavailable')
 runtime.recheck();parsed=local_txt_parser.parse_local_txt_bytes(source_bytes,source_name=source_name)
 lines=parsed['normalized_text'].split('\n');trimmed=[line.strip(_TRIM) for line in lines];nonempty=[line for line in trimmed if line]
 if not nonempty:raise ValueError('no nonempty lines after legacy trim')
 book=runtime.make_book(nonempty,title,author);unwrapped=runtime.unwrap(book,language);unwrapped_text=runtime.paragraph_texts(unwrapped);punctuated=runtime.normalized_paragraphs(unwrapped,language)
 if not isinstance(unwrapped_text,list) or not all(isinstance(x,str) for x in unwrapped_text) or not isinstance(punctuated,list) or not all(isinstance(x,str) for x in punctuated):raise ValueError('runtime stage output invalid')
 final=runtime.build_text(book,language,read_notes)
 if not isinstance(final,str) or not final or '\r' in final:raise ValueError('runtime output is not normalized text')
 runtime.recheck();final_sha=_sha(final.encode('utf8'));match=None if expected_legacy_text_sha256 is None else final_sha==expected_legacy_text_sha256
 stats={'input_lines':len(lines),'empty_lines_removed':len(lines)-len(nonempty),'boundary_whitespace_removed_characters':sum(len(a)-len(b) for a,b in zip(lines,trimmed)),
        'paragraphs_before_unwrap':len(nonempty),'paragraphs_after_unwrap':len(unwrapped_text),
        'unwrap_removed_paragraph_boundaries':len(nonempty)-len(unwrapped_text),
        'candidate_begins_with_title_line':final.startswith(title+'\n'),
        'punctuation_changed_paragraphs':sum(a!=b for a,b in zip(unwrapped_text,punctuated)),
        'punctuation_net_char_delta':sum(len(b)-len(a) for a,b in zip(unwrapped_text,punctuated)),
        'candidate_characters':len(final)}
 if len(unwrapped_text)!=len(punctuated):raise ValueError('punctuation output count mismatch')
 evidence={'version':1,'parser_id':PARSER_ID,'parser_version':PARSER_VERSION,'runtime_identity':identity,
           'options':{'language':language,'title':title,'author_supplied':author is not None,'author_sha256':_sha(author.encode('utf8')) if author is not None else None,'read_notes':read_notes,'split':False},
           'source_sha256':parsed['source_sha256'],'normalized_input_sha256':parsed['text_sha256'],
           'trimmed_paragraphs_sha256':_sha(_json(nonempty)),'unwrapped_paragraphs_sha256':_sha(_json(unwrapped_text)),
           'punctuated_paragraphs_sha256':_sha(_json(punctuated)),'candidate_text_sha256':final_sha,
           'expected_legacy_text_sha256':expected_legacy_text_sha256,'historical_hash_match':match}
 return {'source_sha256':parsed['source_sha256'],'text_sha256':final_sha,'normalized_text':final,'statistics':stats,'parser_evidence':evidence,
         'historical_compatibility':'unverified' if match is None else 'hash_match' if match else 'hash_mismatch',
         'human_confirmed':False,'limitations':['exact historical compatibility requires independently trusted matching text hash','runtime and source pins must be authenticated by caller','not a model, network or human review']}


def reparse_and_verify(source_bytes:bytes, *, normalized_text:str, parser_evidence:Mapping[str,Any], **kwargs)->dict[str,Any]:
 """Recompute all stages and compare candidate bytes and full evidence."""
 result=parse_legacy_txt_bytes(source_bytes,**kwargs)
 if result['normalized_text']!=normalized_text or not isinstance(parser_evidence,Mapping) or result['parser_evidence']!=dict(parser_evidence):
  raise ValueError('legacy TXT text/evidence differs from fresh reconstruction')
 return result
