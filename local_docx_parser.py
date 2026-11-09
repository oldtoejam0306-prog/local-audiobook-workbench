"""Deterministic offline DOCX-body extraction; independent of Huashuo seed.

The main document body is read in XML order. A paragraph is emitted as one
block; a table emits rows in order, with cells separated by TAB and paragraphs
within each cell separated by LF. Every block/row ends in LF. A parallel span
map records boundaries in Unicode code points so embedded newlines/tabs do not
have to be interpreted as structural delimiters.

Footnotes, endnotes, comments, all tracked revision variants, macros, unknown
content controls, drawings/text boxes, nested or merged tables and external
relationships are unsupported and cause rejection; headers/footers are not
included in the body. No file, network or model access.
A content hash/evidence is reproducible, not a signature of document origin.
"""
from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping

try:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph
except ImportError as exc:
    raise ImportError('local_docx_parser requires the already-installed python-docx package') from exc

PARSER_ID = 'local-docx-body-paragraph-table'
PARSER_VERSION = '2'
NORMALIZATION_ID = 'docx-body-paragraph-table-lf-v1'
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_UNPACKED_BYTES = 256 * 1024 * 1024
_MAX_MEMBERS = 4096
_W_NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_tracked_revision(tag: str) -> bool:
    if not isinstance(tag, str) or not tag.startswith(_W_NS):
        return False
    local = tag[len(_W_NS):]
    folded = local.lower()
    if folded.endswith('change'):
        return True
    if folded.startswith(('movefrom', 'moveto')):
        return True
    if folded.startswith('customxml') and any(marker in folded for marker in (
            'insrange', 'delrange', 'movefromrange', 'movetorange')):
        return True
    return folded in {'cellins', 'celldel', 'cellmerge', 'numberingchange', 'conflictins',
                      'conflictdel', 'deltext', 'fldchar'}


def _assert_metadata_has_no_body_text(element, label: str) -> None:
    text_bearing = {
        qn('w:p'), qn('w:tbl'), qn('w:tr'), qn('w:tc'), qn('w:r'), qn('w:hyperlink'),
        qn('w:sdt'), qn('w:customXml'), qn('w:t'), qn('w:delText'), qn('w:instrText'),
    }
    if any(descendant is not element and descendant.tag in text_bearing for descendant in element.iter()):
        raise ValueError(f'{label} contains hidden body text')


def _validate_paragraph_structure(paragraph) -> None:
    p_allowed = {
        qn('w:pPr'), qn('w:r'), qn('w:hyperlink'), qn('w:bookmarkStart'),
        qn('w:bookmarkEnd'), qn('w:proofErr'), qn('w:permStart'), qn('w:permEnd'),
    }
    run_allowed = {
        qn('w:rPr'), qn('w:t'), qn('w:tab'), qn('w:br'), qn('w:cr'),
        qn('w:noBreakHyphen'), qn('w:softHyphen'), qn('w:sym'), qn('w:lastRenderedPageBreak'),
    }
    meta = {qn('w:bookmarkStart'), qn('w:bookmarkEnd'), qn('w:proofErr'), qn('w:permStart'), qn('w:permEnd')}
    for child in paragraph.iterchildren():
        if child.tag not in p_allowed:
            raise ValueError('paragraph contains an unsupported or text-hiding structure')
        if child.tag == qn('w:pPr'):
            _assert_metadata_has_no_body_text(child, 'paragraph properties')
        if child.tag == qn('w:r'):
            if any(run_child.tag not in run_allowed for run_child in child.iterchildren()):
                raise ValueError('run contains an unsupported or text-hiding structure')
            for run_child in child.iterchildren():
                if run_child.tag == qn('w:rPr'):
                    _assert_metadata_has_no_body_text(run_child, 'run properties')
        elif child.tag == qn('w:hyperlink'):
            if any(link_child.tag != qn('w:r') and link_child.tag not in meta
                   for link_child in child.iterchildren()):
                raise ValueError('hyperlink contains an unsupported or text-hiding structure')
            for run in child.iterchildren():
                if run.tag in meta:
                    _assert_metadata_has_no_body_text(run, 'hyperlink metadata')
                if run.tag == qn('w:r') and any(run_child.tag not in run_allowed
                                                for run_child in run.iterchildren()):
                    raise ValueError('hyperlink run contains an unsupported or text-hiding structure')
                if run.tag == qn('w:r'):
                    for run_child in run.iterchildren():
                        if run_child.tag == qn('w:rPr'):
                            _assert_metadata_has_no_body_text(run_child, 'hyperlink run properties')


def _validate_table_structure(table_element) -> None:
    for child in table_element.iterchildren():
        if child.tag not in {qn('w:tblPr'), qn('w:tblGrid'), qn('w:tr')}:
            raise ValueError('table contains an unsupported or text-hiding structure')
        if child.tag != qn('w:tr'):
            _assert_metadata_has_no_body_text(child, 'table properties')
            continue
        for row_child in child.iterchildren():
            if row_child.tag not in {qn('w:trPr'), qn('w:tc')}:
                raise ValueError('table row contains an unsupported or text-hiding structure')
            if row_child.tag != qn('w:tc'):
                _assert_metadata_has_no_body_text(row_child, 'table row properties')
                continue
            for cell_child in row_child.iterchildren():
                if cell_child.tag == qn('w:tbl'):
                    raise ValueError('nested tables are unsupported')
                if cell_child.tag not in {qn('w:tcPr'), qn('w:p')}:
                    raise ValueError('table cell contains an unsupported or text-hiding structure')
                if cell_child.tag == qn('w:tcPr'):
                    _assert_metadata_has_no_body_text(cell_child, 'table cell properties')
                if cell_child.tag == qn('w:p'):
                    _validate_paragraph_structure(cell_child)


def _source_name(value: Any) -> None:
    if (not isinstance(value, str) or not value or value != value.strip()
            or PurePosixPath(value).name != value or PureWindowsPath(value).name != value
            or not value.lower().endswith('.docx')):
        raise ValueError('DOCX parser requires a plain .docx filename')


def _validate_archive(raw: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            items = archive.infolist()
            if not items or len(items) > _MAX_MEMBERS:
                raise ValueError('DOCX archive contains too many or no parts')
            names = [item.filename for item in items]
            if len(names) != len(set(names)):
                raise ValueError('DOCX archive contains duplicate part names')
            if sum(item.file_size for item in items) > _MAX_UNPACKED_BYTES:
                raise ValueError('DOCX archive exceeds expanded size limit')
            if any(item.flag_bits & 1 for item in items):
                raise ValueError('encrypted DOCX parts are unsupported')
            for name in names:
                path = PurePosixPath(name)
                if path.is_absolute() or '\\' in name or any(part == '..' for part in path.parts):
                    raise ValueError('DOCX archive contains an unsafe part name')
            if '[Content_Types].xml' not in names or 'word/document.xml' not in names:
                raise ValueError('DOCX archive lacks required document parts')
            lowered_names = [name.lower() for name in names]
            if any('vbaproject' in name or 'vbadata' in name or 'macroenabled' in name
                   for name in lowered_names):
                raise ValueError('VBA or macro-enabled DOCX packages are unsupported')
            content_types = archive.read('[Content_Types].xml').lower()
            if b'vbaproject' in content_types or b'macroenabled' in content_types:
                raise ValueError('VBA or macro-enabled DOCX content types are unsupported')
            for name in names:
                if name.lower().endswith('.rels') and b'vbaproject' in archive.read(name).lower():
                    raise ValueError('VBA project relationships are unsupported')
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError('source is not a readable DOCX archive') from exc


def parse_local_docx_bytes(source_bytes: bytes, *, source_name: str = 'book.docx') -> dict[str, Any]:
    """Extract the body and return raw/text hashes, versioned evidence and spans.

    Only `.docx` bytes are accepted. Empty paragraphs/cells are retained as
    boundaries; an empty or whitespace-only resulting document is rejected.
    Source data is never interpreted as an instruction or sent to a service.
    """
    _source_name(source_name)
    if not isinstance(source_bytes, bytes) or not source_bytes or len(source_bytes) > _MAX_SOURCE_BYTES:
        raise ValueError('source must be nonempty DOCX bytes within the size limit')
    _validate_archive(source_bytes)
    try:
        document = Document(io.BytesIO(source_bytes))
    except Exception as exc:
        raise ValueError('DOCX package cannot be parsed') from exc
    for part in document.part.package.parts:
        for relation in part.rels.values():
            if relation.is_external:
                raise ValueError('external relationships such as linked content are unsupported')
        name = str(part.partname).lower()
        if name in ('/word/footnotes.xml','/word/endnotes.xml','/word/comments.xml'):
            raise ValueError('footnotes, endnotes and comments are unsupported')
    body = document.element.body
    forbidden = {
        qn('w:footnoteReference'), qn('w:endnoteReference'), qn('w:commentReference'),
        qn('w:commentRangeStart'), qn('w:commentRangeEnd'), qn('w:ins'), qn('w:del'),
        qn('w:drawing'), qn('w:pict'), qn('w:object'), qn('w:altChunk'),
        qn('w:gridSpan'), qn('w:vMerge'), qn('w:gridBefore'), qn('w:gridAfter'),
        qn('w:fldSimple'), qn('w:instrText'), qn('w:sdt'), qn('w:customXml'),
    }
    if any(_is_tracked_revision(element.tag) for element in body.iter()):
        raise ValueError('DOCX body contains an unsupported tracked revision')
    if any(element.tag in forbidden for element in body.iter()):
        raise ValueError('DOCX body contains unsupported notes, comments, changes, objects, fields or merged cells')
    chunks: list[str] = []
    spans: list[dict[str, Any]] = []
    length = 0
    def append(value: str) -> None:
        nonlocal length
        if '\r' in value or '\x00' in value:
            raise ValueError('DOCX body contains unsupported control characters')
        chunks.append(value);length += len(value)
    for element in body.iterchildren():
        if element.tag == qn('w:sectPr'):
            _assert_metadata_has_no_body_text(element, 'section properties')
            continue
        if element.tag == qn('w:p'):
            _validate_paragraph_structure(element)
            start=length;append(Paragraph(element,document).text);end=length;append('\n')
            spans.append({'type':'paragraph','start':start,'end':end})
        elif element.tag == qn('w:tbl'):
            _validate_table_structure(element)
            table=Table(element,document)
            for row_index,row in enumerate(table.rows):
                start=length;cells=[]
                for column,cell in enumerate(row.cells):
                    if column:append('\t')
                    cell_start=length
                    for paragraph_index,paragraph in enumerate(cell.paragraphs):
                        if paragraph_index:append('\n')
                        append(paragraph.text)
                    cells.append({'start':cell_start,'end':length})
                end=length;append('\n')
                spans.append({'type':'table_row','table_row_index':row_index,'start':start,'end':end,'cells':cells})
        else:
            raise ValueError('DOCX body contains an unsupported top-level element')
    text=''.join(chunks)
    if not text.strip():raise ValueError('DOCX document body is empty')
    source_sha,text_sha=_sha(source_bytes),_sha(text.encode('utf-8'))
    evidence={'version':1,'parser_id':PARSER_ID,'parser_version':PARSER_VERSION,
              'import_mode':'docx_extract','normalization':NORMALIZATION_ID,
              'source_sha256':source_sha,'text_sha256':text_sha}
    return {'source_sha256':source_sha,'text_sha256':text_sha,'normalized_text':text,
            'parser_evidence':evidence,'body_spans':spans,
            'limitations':['main document body only; headers and footers omitted',
                           'notes, comments, tracked changes, drawings, fields, nested/merged tables and external links rejected']}


def reparse_and_verify(source_bytes: bytes, *, normalized_text: str,
                       parser_evidence: Mapping[str, Any], source_name: str = 'book.docx') -> dict[str, Any]:
    """Recompute extraction from original bytes and reject text/evidence mismatch."""
    parsed=parse_local_docx_bytes(source_bytes,source_name=source_name)
    if normalized_text != parsed['normalized_text'] or not isinstance(parser_evidence,Mapping) or dict(parser_evidence)!=parsed['parser_evidence']:
        raise ValueError('claimed DOCX text or parser evidence differs from fresh extraction')
    return parsed
