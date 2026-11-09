"""Deterministic local TXT decoding and newline normalization for synthetic use.

This module performs no filesystem, network, model, or service operations. It
accepts raw UTF-8 TXT bytes only; its evidence is a reproducible parser result,
not a signature or external authentication claim.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping


PARSER_ID = 'local-txt-utf8-newlines'
PARSER_VERSION = '1'
NORMALIZATION_ID = 'lf-newlines-utf8-v1'
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_EVIDENCE_FIELDS = {
    'version', 'parser_id', 'parser_version', 'import_mode', 'normalization',
    'source_sha256', 'text_sha256',
}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _validate_source_name(source_name: Any) -> None:
    if (not isinstance(source_name, str) or not source_name or source_name != source_name.strip()
            or PurePosixPath(source_name).name != source_name
            or PureWindowsPath(source_name).name != source_name
            or not source_name.lower().endswith('.txt')):
        raise ValueError('local TXT parser accepts a plain .txt filename only; DOCX and EPUB are unsupported')


def parse_local_txt_bytes(source_bytes: bytes, *, source_name: str = 'book.txt') -> dict[str, Any]:
    """Strictly decode UTF-8/UTF-8 BOM and normalize CRLF and CR to LF.

    The raw-source SHA-256 hashes the original bytes. The text SHA-256 hashes
    the normalized body's UTF-8 bytes. Both hashes are carried independently
    in the parser evidence schema used by ``huashuo_seed_builder``.
    """
    _validate_source_name(source_name)
    if not isinstance(source_bytes, bytes) or not source_bytes:
        raise ValueError('source_bytes must be nonempty raw UTF-8 bytes')
    try:
        decoded = source_bytes.decode('utf-8-sig' if source_bytes.startswith(b'\xef\xbb\xbf') else 'utf-8',
                                      errors='strict')
    except UnicodeDecodeError as exc:
        raise ValueError('source is not strict UTF-8 or UTF-8 BOM text') from exc
    if '\ufeff' in decoded:
        raise ValueError('embedded BOM is unsupported')
    normalized = decoded.replace('\r\n', '\n').replace('\r', '\n')
    normalized_bytes = normalized.encode('utf-8', errors='strict')
    if not normalized or not normalized.strip():
        raise ValueError('normalized TXT body is empty')
    source_sha256 = _sha256(source_bytes)
    text_sha256 = _sha256(normalized_bytes)
    evidence = {
        'version': 1,
        'parser_id': PARSER_ID,
        'parser_version': PARSER_VERSION,
        'import_mode': 'txt_decode',
        'normalization': NORMALIZATION_ID,
        'source_sha256': source_sha256,
        'text_sha256': text_sha256,
    }
    return {
        'source_sha256': source_sha256,
        'text_sha256': text_sha256,
        'normalized_text': normalized,
        'parser_evidence': evidence,
    }


def reparse_and_verify(source_bytes: bytes, *, normalized_text: str,
                       parser_evidence: Mapping[str, Any],
                       source_name: str = 'book.txt') -> dict[str, Any]:
    """Reparse original bytes and reject any claimed text/evidence mismatch."""
    if not isinstance(parser_evidence, Mapping) or set(parser_evidence) != _EVIDENCE_FIELDS:
        raise ValueError('parser_evidence fields do not match the local TXT parser contract')
    parsed = parse_local_txt_bytes(source_bytes, source_name=source_name)
    if normalized_text != parsed['normalized_text']:
        raise ValueError('normalized text differs from a fresh parse of source bytes')
    if dict(parser_evidence) != parsed['parser_evidence']:
        raise ValueError('parser evidence differs from a fresh parse of source bytes')
    if (not isinstance(parser_evidence.get('source_sha256'), str)
            or not _SHA256.fullmatch(parser_evidence['source_sha256'])
            or not isinstance(parser_evidence.get('text_sha256'), str)
            or not _SHA256.fullmatch(parser_evidence['text_sha256'])):
        raise ValueError('parser evidence hashes must be lowercase SHA-256')
    return parsed
