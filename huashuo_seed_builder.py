"""Build a minimal Huashuo seed from reparsed local TXT bytes.

TXT inputs are decoded and newline-normalized again by ``local_txt_parser``.
DOCX and EPUB remain unsupported until a trusted local parser can rederive their
normalized text. Parser evidence is reproducible, not signed authentication.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

import local_txt_parser


_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_VOICE = re.compile(r'^(?:preset:[A-Za-z0-9_.-]{1,120}|library:[A-Za-z0-9_-]{1,32}/[A-Za-z0-9_.-]{1,120})$')
_FALLBACK_ROLE = '__STAGING_UNCONFIRMED__'
_EVIDENCE_FIELDS = {
    'version', 'parser_id', 'parser_version', 'import_mode', 'normalization',
    'source_sha256', 'text_sha256',
}
_HEADER_REQUIRED_FIELDS = {'type', 'version', 'title', 'language', 'text', 'text_sha256'}
_HEADER_OPTIONAL_FIELDS = {'author', 'source'}
_ROW_FIELDS = {'type', 'id', 'text', 'src', 'speaker', 'level', 'emotion', 'reason'}
_SCRIPT_TYPES = {'chapter', 'narration', 'dialogue', 'skip'}
_INGEST = b'{"options":{"voices":"multi"}}\n'


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _nonempty_string(value: Any, label: str, *, limit: int = 500) -> str:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > limit or not all(char.isprintable() for char in value)):
        raise ValueError(f'{label} must be a bounded printable nonempty string')
    return value


def _script_text(value: Any) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 2_000_000
            or any((not char.isprintable()) and char not in '\n\t' for char in value)):
        raise ValueError('script block text must be bounded readable text')
    return value


def _validate_header_source(value: Any, *, source_sha256: str | None) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {'format', 'path', 'sha256'}:
        raise ValueError('Huashuo header source must have exactly format, path, and sha256')
    if value.get('format') != 'TXT':
        raise ValueError('only TXT header source metadata is supported by this builder')
    path = _nonempty_string(value.get('path'), 'header source path', limit=2000)
    if ('\\' in path or '://' in path or PurePosixPath(path).is_absolute()
            or PureWindowsPath(path).drive or PureWindowsPath(path).is_absolute()):
        raise ValueError('header source path must be a relative inert provenance string')
    source_hash = value.get('sha256')
    if not isinstance(source_hash, str) or not _SHA256.fullmatch(source_hash):
        raise ValueError('header source sha256 must be lowercase SHA-256')
    if source_sha256 is not None and source_hash != source_sha256:
        raise ValueError('original header source hash differs from raw TXT source bytes')
    return {'format': 'TXT', 'path': path, 'sha256': source_hash}


def _stage_header_source(rows: list[dict[str, Any]], text_sha256: str) -> list[dict[str, Any]]:
    staged = [dict(row) for row in rows]
    if 'source' in staged[0]:
        staged[0]['source'] = {'format': 'TXT', 'path': 'text.txt', 'sha256': text_sha256}
    return staged


def _validate_parser_evidence(evidence: Any, source_bytes: bytes, source_name: str,
                              normalized_text: str, source_sha: str, text_sha: str) -> None:
    if not isinstance(evidence, Mapping) or set(evidence) != _EVIDENCE_FIELDS:
        raise ValueError('parser_evidence fields do not match the seed contract')
    if evidence.get('import_mode') != 'txt_decode':
        raise ValueError('DOCX/EPUB parser modes are unsupported until trusted parsers are connected')
    reparsed = local_txt_parser.reparse_and_verify(
        source_bytes, normalized_text=normalized_text, parser_evidence=evidence,
        source_name=source_name)
    if reparsed['source_sha256'] != source_sha or reparsed['text_sha256'] != text_sha:
        raise ValueError('fresh TXT parse hashes differ from the expected source/text hashes')


def _validate_cast(cast: Any) -> set[str]:
    if not isinstance(cast, Mapping) or set(cast) != {'version', 'narrator', 'characters'}:
        raise ValueError('cast must contain only version, narrator, and characters')
    if type(cast.get('version')) is not int or cast['version'] != 1:
        raise ValueError('unsupported cast version')
    narrator = cast.get('narrator')
    if not isinstance(narrator, Mapping) or set(narrator) != {'voice'}:
        raise ValueError('narrator must contain only a voice')
    voice = narrator.get('voice')
    if not isinstance(voice, str) or not _VOICE.fullmatch(voice) or '..' in voice:
        raise ValueError('narrator voice reference is invalid')
    characters = cast.get('characters')
    if not isinstance(characters, Mapping):
        raise ValueError('cast characters must be a map')
    folded: set[str] = set()
    for name, profile in characters.items():
        if (not isinstance(name, str) or not name or name != name.strip() or len(name) > 60
                or not all(char.isprintable() for char in name) or name.casefold() in {'unknown', '旁白'}
                or '..' in name or any(char in name for char in '/\\<>:|?*')):
            if name != _FALLBACK_ROLE:
                raise ValueError('cast contains an invalid or reserved character name')
        if name.casefold() in folded:
            raise ValueError('cast character names collide case-insensitively')
        folded.add(name.casefold())
        if not isinstance(profile, Mapping) or set(profile) - {'voice', 'aliases', 'description'} or 'voice' not in profile:
            raise ValueError('cast character profile has unsupported fields')
        voice = profile.get('voice')
        if not isinstance(voice, str) or not _VOICE.fullmatch(voice) or '..' in voice:
            raise ValueError('cast character voice reference is invalid')
        aliases = profile.get('aliases', [])
        if (not isinstance(aliases, list) or any(not isinstance(alias, str) or not alias.strip()
                                                 or not all(char.isprintable() for char in alias)
                                                 for alias in aliases)
                or len(set(aliases)) != len(aliases)):
            raise ValueError('cast aliases must be unique printable strings')
        if 'description' in profile:
            _nonempty_string(profile['description'], 'character description', limit=1000)
    return set(characters)


def _validate_script(text: str, text_sha: str, rows: Any, cast: Mapping[str, Any],
                     *, source_sha256: str | None = None) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError('script must contain a header and at least one block')
    header = rows[0]
    if (not isinstance(header, Mapping) or not _HEADER_REQUIRED_FIELDS <= set(header)
            or set(header) - (_HEADER_REQUIRED_FIELDS | _HEADER_OPTIONAL_FIELDS)
            or header.get('type') != 'huaben' or type(header.get('version')) is not int
            or header.get('version') != 1 or header.get('language') != 'zh'
            or header.get('text') != 'text.txt' or header.get('text_sha256') != text_sha):
        raise ValueError('Huashuo script header is missing or inconsistent')
    _nonempty_string(header.get('title'), 'script title', limit=200)
    if 'author' in header:
        _nonempty_string(header['author'], 'script author', limit=200)
    if 'source' in header:
        _validate_header_source(header['source'], source_sha256=source_sha256)

    names = _validate_cast(cast)
    seen_ids: set[str] = set()
    previous_end = 0
    first_block = True
    chapters = 0
    validated: list[dict[str, Any]] = [dict(header)]
    for row in rows[1:]:
        if not isinstance(row, Mapping) or set(row) - _ROW_FIELDS:
            raise ValueError('script block has unsupported fields')
        kind = row.get('type')
        if kind not in _SCRIPT_TYPES:
            raise ValueError('script block type is unsupported')
        ident = _nonempty_string(row.get('id'), 'script block ID', limit=160)
        if any(char.isspace() for char in ident) or ident in seen_ids:
            raise ValueError('script block IDs must be unique and contain no whitespace')
        seen_ids.add(ident)
        body = _script_text(row.get('text'))
        src = row.get('src')
        if (not isinstance(src, list) or len(src) != 2
                or any(type(point) is not int for point in src)):
            raise ValueError('each script block requires a Unicode-codepoint src range')
        start, end = src
        if not 0 <= previous_end <= start < end <= len(text):
            raise ValueError('script src ranges overlap, are unordered, or are out of bounds')
        if text[previous_end:start].strip():
            raise ValueError('script src ranges leave uncovered non-whitespace text')
        if text[start:end] != body:
            raise ValueError('script block text differs from its normalized source slice')
        previous_end = end

        if first_block and kind != 'chapter':
            raise ValueError('first readable script block must be a chapter')
        first_block = False
        if kind == 'chapter':
            chapters += 1
            if 'speaker' in row or 'reason' in row:
                raise ValueError('chapter block contains incompatible annotations')
            if 'level' in row and (type(row['level']) is not int or row['level'] not in (1, 2)):
                raise ValueError('chapter level must be 1 or 2')
        elif kind == 'dialogue':
            speaker = _nonempty_string(row.get('speaker'), 'dialogue speaker', limit=60)
            if speaker not in names:
                raise ValueError('dialogue speaker is missing from cast characters')
            if 'level' in row or 'reason' in row:
                raise ValueError('dialogue block contains incompatible annotations')
        elif kind == 'narration':
            if 'speaker' in row or 'level' in row or 'reason' in row:
                raise ValueError('narration block contains incompatible annotations')
        else:  # skip
            if 'speaker' in row or 'level' in row:
                raise ValueError('skip block contains incompatible annotations')
            if 'reason' in row:
                _nonempty_string(row['reason'], 'skip reason', limit=300)

        if 'emotion' in row:
            _nonempty_string(row['emotion'], 'emotion', limit=80)
        validated.append(dict(row))
    if chapters == 0 or first_block:
        raise ValueError('script contains no chapter blocks')
    if text[previous_end:].strip():
        raise ValueError('script src ranges do not cover the normalized text')
    return validated


def build_seed_files(*, source_bytes: bytes, expected_source_sha256: str,
                     normalized_text: str, expected_text_sha256: str,
                     parser_evidence: Mapping[str, Any], script_rows: list[dict[str, Any]],
                     cast: Mapping[str, Any], include_multi_voice_ingest: bool = False,
                     source_name: str = 'book.txt') -> dict[str, bytes]:
    """Return only the fixed Huashuo seed files after hash and structure checks.

    The source bytes are reparsed locally and the normalized body and complete
    parser evidence are compared with that fresh result. A source/text hash pair
    or caller-asserted DOCX/EPUB parser record is insufficient.
    Every script row must carry a valid source range; unlocated insertion blocks
    and unbound ``say`` overrides are rejected. Whitespace-only gaps are allowed.
    """
    if not isinstance(source_bytes, bytes) or not source_bytes:
        raise ValueError('source_bytes must be nonempty raw source bytes')
    if not isinstance(expected_source_sha256, str) or not _SHA256.fullmatch(expected_source_sha256):
        raise ValueError('expected_source_sha256 must be lowercase SHA-256')
    actual_source_sha = _sha256(source_bytes)
    if actual_source_sha != expected_source_sha256:
        raise ValueError('raw source bytes differ from expected_source_sha256')
    if (not isinstance(normalized_text, str) or not normalized_text
            or '\r' in normalized_text or '\ufeff' in normalized_text
            or any((not char.isprintable()) and char not in '\n\t' for char in normalized_text)):
        raise ValueError('normalized_text must be nonempty LF-only UTF-8 text without a BOM')
    try:
        text_bytes = normalized_text.encode('utf-8', errors='strict')
    except UnicodeEncodeError as exc:
        raise ValueError('normalized_text contains invalid Unicode scalar values') from exc
    actual_text_sha = _sha256(text_bytes)
    if not isinstance(expected_text_sha256, str) or not _SHA256.fullmatch(expected_text_sha256):
        raise ValueError('expected_text_sha256 must be lowercase SHA-256')
    if actual_text_sha != expected_text_sha256:
        raise ValueError('normalized UTF-8 body differs from expected_text_sha256')
    _validate_parser_evidence(parser_evidence, source_bytes, source_name, normalized_text,
                              actual_source_sha, actual_text_sha)
    validated_rows = _validate_script(normalized_text, actual_text_sha, script_rows, cast,
                                      source_sha256=actual_source_sha)
    staged_rows = _stage_header_source(validated_rows, actual_text_sha)
    # Validate once more as a detached JSON-compatible tree; no caller object is mutated.
    script_bytes = ''.join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':'), allow_nan=False) + '\n'
                           for row in staged_rows).encode('utf-8')
    cast_bytes = (json.dumps(dict(cast), ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    files = {'text.txt': text_bytes, 'script.huaben.jsonl': script_bytes, 'cast.json': cast_bytes}
    if type(include_multi_voice_ingest) is not bool:
        raise ValueError('include_multi_voice_ingest must be boolean')
    if include_multi_voice_ingest:
        files['state/ingest.json'] = _INGEST
    return files
