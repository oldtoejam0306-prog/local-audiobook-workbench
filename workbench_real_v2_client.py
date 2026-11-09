"""Local preparation client for v2. No real dispatch is exposed."""
import hashlib
from pathlib import Path
import controlled_book_chapter_v2 as protocol

def definition(book_id,chapter_id,source_bytes,index_sha256,voice_mapping):
 if not isinstance(source_bytes,bytes):raise ValueError('source must be bytes')
 identity={'book_id':book_id,'chapter_id':chapter_id,'source_sha256':hashlib.sha256(source_bytes).hexdigest(),'index_sha256':index_sha256,'voice_mapping_sha256':hashlib.sha256(protocol.canonical(voice_mapping)).hexdigest()}
 protocol.validate(identity)
 return identity

def reserve(root,legacy_root,identity,source_bytes,voice_mapping):
 return protocol.Protocol(Path(root),Path(legacy_root)).reserve(identity,source_bytes,voice_mapping)
