"""Build an isolated seed for a hash-matched historical DOCX text snapshot.

This pure builder does not read user files or authenticate the caller-provided
snapshot. A trusted caller must obtain that snapshot independently from the
existing book's protected manifest/workdir. Hash consistency is not origin or
approval authentication. This module does not enable rendering.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

import huashuo_seed_builder
import local_docx_legacy_parser

_SHA=re.compile(r'^[0-9a-f]{64}$')
_FIELDS={'version','book_id','source_sha256','legacy_text_sha256','script_sha256','cast_sha256'}


def _canonical(value: Any) -> bytes:
    try:return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    except (TypeError,ValueError,UnicodeError) as exc:raise ValueError('snapshot input is not canonical JSON') from exc


def _sha(raw: bytes) -> str:return hashlib.sha256(raw).hexdigest()


def snapshot_object_sha256(value: Any) -> str:
    """Hash a detached JSON object with deterministic UTF-8 encoding."""
    return _sha(_canonical(value))


def build_legacy_docx_seed(*, source_bytes: bytes, source_name: str, trusted_snapshot: Mapping[str, Any],
                           book_id: str, normalized_text: str, script_rows: list[dict[str, Any]],
                           cast: Mapping[str, Any], include_multi_voice_ingest: bool=False) -> dict[str, Any]:
    """Reparse original DOCX, verify independent snapshot, return fixed seed bytes.

    `trusted_snapshot` has exactly version=1, book_id, source_sha256,
    legacy_text_sha256, script_sha256 and cast_sha256. Script/cast hashes are
    canonical JSON object hashes using `snapshot_object_sha256`; source and text
    hashes are SHA-256 of original bytes and UTF-8 text respectively. Caller
    owns authentication and independent snapshot provenance.
    """
    if not isinstance(trusted_snapshot,Mapping) or set(trusted_snapshot)!=_FIELDS:
        raise ValueError('independent snapshot fields do not match contract')
    snapshot=copy.deepcopy(dict(trusted_snapshot))
    if type(snapshot['version']) is not int or snapshot['version']!=1:
        raise ValueError('unsupported snapshot version')
    if (not isinstance(book_id,str) or not book_id.strip() or book_id!=book_id.strip()
            or snapshot['book_id']!=book_id):raise ValueError('book identity does not match independent snapshot')
    for field in ('source_sha256','legacy_text_sha256','script_sha256','cast_sha256'):
        if not isinstance(snapshot[field],str) or not _SHA.fullmatch(snapshot[field]):raise ValueError('invalid independent snapshot hash')
    if not isinstance(source_bytes,bytes) or not source_bytes or _sha(source_bytes)!=snapshot['source_sha256']:
        raise ValueError('source bytes differ from independent snapshot')
    if not isinstance(normalized_text,str):raise ValueError('normalized text must be a string')
    try:text_hash=_sha(normalized_text.encode('utf-8',errors='strict'))
    except UnicodeError as exc:raise ValueError('normalized text is invalid UTF-8') from exc
    if text_hash!=snapshot['legacy_text_sha256']:
        raise ValueError('normalized text differs from independent historical hash')
    if snapshot_object_sha256(script_rows)!=snapshot['script_sha256']:
        raise ValueError('script differs from independent snapshot')
    if snapshot_object_sha256(cast)!=snapshot['cast_sha256']:
        raise ValueError('cast differs from independent snapshot')
    parsed=local_docx_legacy_parser.parse_legacy_docx_bytes(
        source_bytes,source_name=source_name,expected_legacy_text_sha256=snapshot['legacy_text_sha256'])
    if (parsed.get('historical_compatibility')!='hash_match'
            or parsed.get('parser_evidence',{}).get('historical_hash_match') is not True
            or parsed.get('source_sha256')!=snapshot['source_sha256']
            or parsed.get('text_sha256')!=snapshot['legacy_text_sha256']
            or parsed.get('normalized_text')!=normalized_text):
        raise ValueError('fresh DOCX parse does not match independent historical text')
    validated=huashuo_seed_builder._validate_script(normalized_text,text_hash,script_rows,cast)
    staged_rows=huashuo_seed_builder._stage_header_source(validated,text_hash)
    source_header=dict(script_rows[0]);staged_header=dict(staged_rows[0])
    intermediate_source=source_header.get('source')
    source_provenance=None
    if isinstance(intermediate_source,dict):
        source_provenance={
            'format':intermediate_source['format'],
            'intermediate_source_sha256':intermediate_source['sha256'],
            'intermediate_source_path_sha256':_sha(intermediate_source['path'].encode('utf-8')),
            'metadata_sha256':snapshot_object_sha256(intermediate_source),
            'staged_source':staged_header.get('source'),
        }
    if type(include_multi_voice_ingest) is not bool:raise ValueError('include_multi_voice_ingest must be boolean')
    script=''.join(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n' for row in staged_rows).encode('utf-8')
    cast_bytes=_canonical(dict(cast))+b'\n'
    files={'text.txt':normalized_text.encode('utf-8'),'script.huaben.jsonl':script,'cast.json':cast_bytes}
    if include_multi_voice_ingest:files['state/ingest.json']=b'{"options":{"voices":"multi"}}\n'
    file_hashes={name:_sha(value) for name,value in sorted(files.items())}
    binding={'version':1,'book_id':book_id,'snapshot_sha256':snapshot_object_sha256(snapshot),
             'source_sha256':snapshot['source_sha256'],'legacy_text_sha256':snapshot['legacy_text_sha256'],
             'script_sha256':snapshot['script_sha256'],'cast_sha256':snapshot['cast_sha256'],
             'source_header_sha256':snapshot_object_sha256(source_header),
             'staged_header_sha256':snapshot_object_sha256(staged_header),
             'intermediate_source_provenance':source_provenance,
             'parser_evidence_sha256':snapshot_object_sha256(parsed['parser_evidence']),
             'seed_file_hashes':file_hashes,'historical_hash_match':True,
             'trust':'caller authenticates independent snapshot; hash match is not source authentication'}
    return {'files':files,'binding':binding,'parser_evidence':copy.deepcopy(parsed['parser_evidence']),
            'statistics':copy.deepcopy(parsed['statistics']),'limitations':list(parsed['limitations'])}
