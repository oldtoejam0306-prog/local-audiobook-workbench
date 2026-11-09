"""Recompute a narrowly scoped candidate for the historical DOCX/TXT path.

The candidate reproduces the old importer's paragraph extraction, blank-paragraph
filter, intermediate double-LF join, TXT line trimming, and the fixed `book`
front-matter title. Historical Huashuo also has paragraph-unwrapping and
punctuation-normalization rules; this module does not reimplement them. A
candidate MUST NOT be treated as equivalent to an existing Huashuo workdir
unless its hash matches a separately obtained, trusted historical text hash.
The hardened local DOCX parser validates supported structures first. No network,
model, file mutation, or claim of retaining the complete source formatting.
"""
from __future__ import annotations

import hashlib
import io
import re
from typing import Any

import docx
from docx import Document
from docx.oxml.ns import qn
import local_docx_parser

PARSER_ID='local-docx-legacy-huashuo-candidate'
PARSER_VERSION='1'
NORMALIZATION_ID='docx-nonempty-double-lf-txt-trim-book-header-v1'
_SHA=re.compile(r'^[0-9a-f]{64}$')
_TRIM=' \t\u3000\xa0'


def _sha(raw: bytes) -> str:return hashlib.sha256(raw).hexdigest()


def parse_legacy_docx_bytes(source_bytes: bytes, *, source_name: str='book.docx',
                            expected_legacy_text_sha256: str | None=None) -> dict[str, Any]:
    """Compute a deterministic candidate and optional historical hash match.

    Without `expected_legacy_text_sha256`, candidate status is unverified; a
    mismatch likewise never claims compatibility. The expected hash must come
    from the caller's independently trusted existing workdir, not this output.
    DOCX limitations from local_docx_parser apply unchanged.
    """
    if expected_legacy_text_sha256 is not None and (
            not isinstance(expected_legacy_text_sha256,str) or not _SHA.fullmatch(expected_legacy_text_sha256)):
        raise ValueError('expected legacy text hash must be lowercase SHA-256')
    validated=local_docx_parser.parse_local_docx_bytes(source_bytes,source_name=source_name)
    document=Document(io.BytesIO(source_bytes))
    paragraphs=[];empty_paragraphs=0;paragraph_count=0
    for node in document.element.body.iter(qn('w:p')):
        paragraph_count+=1;pieces=[]
        for element in node.iter():
            if element.tag==qn('w:t') and element.text:pieces.append(element.text)
            elif element.tag==qn('w:tab'):pieces.append('\t')
            elif element.tag in (qn('w:br'),qn('w:cr')):pieces.append('\n')
        text=''.join(pieces)
        if text.strip():paragraphs.append(text)
        else:empty_paragraphs+=1
    if not paragraphs:raise ValueError('legacy extraction contains no nonempty paragraphs')
    intermediate='\n\n'.join(paragraphs)+'\n'
    normalized=intermediate.lstrip('\ufeff').replace('\r\n','\n').replace('\r','\n')
    lines=normalized.split('\n');trimmed=[line.strip(_TRIM) for line in lines]
    removed_chars=sum(len(before)-len(after) for before,after in zip(lines,trimmed))
    nonempty=[line for line in trimmed if line]
    if not nonempty:raise ValueError('legacy TXT normalization yields no nonempty lines')
    text='book\n'+'\n'.join(nonempty)+'\n'
    text_sha=_sha(text.encode('utf-8'))
    match=None if expected_legacy_text_sha256 is None else text_sha==expected_legacy_text_sha256
    stats={'source_paragraphs':paragraph_count,'retained_nonempty_paragraphs':len(paragraphs),
           'discarded_empty_or_whitespace_paragraphs':empty_paragraphs,
           'intermediate_characters':len(intermediate),'txt_input_lines':len(lines),
           'retained_txt_lines':len(nonempty),'discarded_empty_txt_lines':len(lines)-len(nonempty),
           'trimmed_boundary_whitespace_characters':removed_chars,
           'inserted_placeholder_title':'book','inserted_placeholder_characters':5,
           'candidate_characters':len(text)}
    evidence={'version':1,'parser_id':PARSER_ID,'parser_version':PARSER_VERSION,
              'normalization':NORMALIZATION_ID,'source_sha256':validated['source_sha256'],
              'strict_docx_text_sha256':validated['text_sha256'],
              'strict_docx_parser_id':validated['parser_evidence']['parser_id'],
              'strict_docx_parser_version':validated['parser_evidence']['parser_version'],
              'legacy_intermediate_sha256':_sha(intermediate.encode('utf-8')),
              'candidate_text_sha256':text_sha,'expected_legacy_text_sha256':expected_legacy_text_sha256,
              'python_docx_version':docx.__version__,'historical_hash_match':match}
    return {'source_sha256':validated['source_sha256'],'text_sha256':text_sha,
            'normalized_text':text,'parser_evidence':evidence,'statistics':stats,
            'historical_compatibility':('unverified' if match is None else 'hash_match' if match else 'hash_mismatch'),
            'limitations':['candidate omits historical Huashuo unwrap and punctuation normalization',
                           'whitespace-only paragraphs and boundary whitespace are discarded',
                           'book placeholder is added; original formatting and headers/footers are not preserved',
                           'local_docx_parser unsupported-structure rejections apply']}


def reparse_and_verify(source_bytes: bytes, *, normalized_text: str, parser_evidence: dict,
                       source_name: str='book.docx', expected_legacy_text_sha256: str | None=None) -> dict:
    parsed=parse_legacy_docx_bytes(source_bytes,source_name=source_name,
                                   expected_legacy_text_sha256=expected_legacy_text_sha256)
    if normalized_text!=parsed['normalized_text'] or parser_evidence!=parsed['parser_evidence']:
        raise ValueError('legacy candidate or parser evidence differs from fresh extraction')
    return parsed
