"""Private, local book preview and immutable chapter-index registration.

This module does not invoke Huashuo, a model, a provider, SSH, or a network
service. Preview files and committed source text stay under the configured
private root. Public query responses contain only one selected chapter.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import tempfile
from typing import Any, Mapping

import chapter_attribution_input
import chapter_local_index
import local_docx_parser
import local_txt_parser
import persisted_chapter_index


MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_TEXT_CHARS = 64 * 1024 * 1024
MAX_CHAPTERS = 10_000
BLOCK_CHARS = 8_000
MAX_CHAPTER_CHARS = 18_000
MAX_AUTO_PART_CHARS = 16_000
EXCERPT_CHARS = 160
_BOOK_ID = re.compile(r"^book-[0-9a-f]{20}$")
_CHAPTER = re.compile(
    r"^\s*(?:第[零〇一二三四五六七八九十百千万两0-9]+[章回节卷]"
    r"|chapter\s*[0-9ivxlcdm]+"
    r"|序章|序言|楔子|尾声|后记|终章)",
    re.IGNORECASE,
)
_QUOTE_PAIRS = {"“": "”", "「": "」", "『": "』"}
_UTF16_LE = b"\xff\xfe"
_UTF16_BE = b"\xfe\xff"
_UTF8_BOM = b"\xef\xbb\xbf"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + os.urandom(8).hex())
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_write(path, _canonical(value) + b"\n")


def _safe_filename(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("original_name is required")
    name = PureWindowsPath(value).name
    if not name or name in (".", "..") or len(name) > 240:
        raise ValueError("original_name is invalid")
    return name


def _decode_txt(raw: bytes) -> tuple[str, str]:
    if raw.startswith((_UTF16_LE, _UTF16_BE)):
        return raw.decode("utf-16"), "utf-16"
    if raw.startswith(_UTF8_BOM):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeError:
            continue
    raise ValueError("TXT encoding is not supported")


def _parse_source(raw: bytes, extension: str) -> dict[str, Any]:
    if extension == ".txt":
        decoded, encoding = _decode_txt(raw)
        # The strict shared parser receives a canonical UTF-8 representation;
        # source_sha256 in the catalog remains the hash of the original bytes.
        parsed = local_txt_parser.parse_local_txt_bytes(decoded.encode("utf-8"), source_name="book.txt")
        parsed["encoding"] = encoding
        parsed["parser_id"] = local_txt_parser.PARSER_ID
    elif extension == ".docx":
        parsed = local_docx_parser.parse_local_docx_bytes(raw, source_name="book.docx")
        parsed["encoding"] = "docx"
        parsed["parser_id"] = parsed["parser_evidence"].get("parser_id")
    else:
        raise ValueError("only TXT and DOCX sources are supported")
    text = parsed.get("normalized_text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise ValueError("normalized source is empty or exceeds the supported size")
    if "\r" in text or "\ufeff" in text:
        raise ValueError("parser returned non-normalized text")
    return parsed


def _iter_lines(text: str):
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        body = raw_line.rstrip("\r\n")
        end = offset + len(body)
        yield offset, end, raw_line, body
        offset += len(raw_line)
    if not text or (text and not text.endswith(("\n", "\r"))):
        return


def _first_content_line(text: str) -> tuple[int, int, str] | None:
    for start, end, _raw, body in _iter_lines(text):
        if body.strip():
            return start, end, body
    return None


def _chapter_candidates(text: str) -> list[dict[str, Any]]:
    first = _first_content_line(text)
    if first is None:
        raise ValueError("source contains no readable text")
    front_start, _front_end, front_line = first
    candidates: list[dict[str, Any]] = []
    for start, end, _raw, line in _iter_lines(text):
        title = line.strip()
        if title and len(title) <= 120 and _CHAPTER.match(line):
            candidates.append({"start": start, "end": end, "title": title})
    starts: list[dict[str, Any]] = []
    if not candidates:
        starts.append({"start": front_start, "title": "全文"})
    else:
        if candidates[0]["start"] > front_start:
            starts.append({"start": front_start, "title": "前言"})
        for item in candidates:
            starts.append({"start": item["start"], "title": item["title"]})
    if len(starts) > MAX_CHAPTERS:
        raise ValueError(f"detected more than {MAX_CHAPTERS} chapter starts; narrow the source or split it")
    return starts


def _chapter_ranges(text: str, starts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranges = []
    for index, item in enumerate(starts):
        end = starts[index + 1]["start"] if index + 1 < len(starts) else len(text)
        start = item["start"]
        if not 0 <= start < end <= len(text) or not text[start:end].strip():
            raise ValueError("chapter boundaries leave an empty or invalid chapter")
        ranges.append({
            "id": f"c{index + 1:03d}", "title": item["title"], "start": start, "end": end,
            "characters": end - start,
            "excerpt": text[start:min(end, start + EXCERPT_CHARS)],
        })
    return ranges


def _segment_end(text: str, start: int, end: int) -> int:
    """Choose a source boundary below the 18,000-codepoint chapter limit."""
    limit = min(start + MAX_CHAPTER_CHARS, end)
    if limit == end:
        return end
    paragraph = text.rfind("\n", start + 2_000, limit)
    if paragraph >= start + 2_000:
        return paragraph + 1
    search_end = min(start + MAX_AUTO_PART_CHARS, limit)
    sentence_marks = "。！？!?；;"
    sentence = max((text.rfind(mark, start + 2_000, search_end) for mark in sentence_marks), default=-1)
    if sentence >= start + 2_000:
        return sentence + 1
    return min(start + MAX_AUTO_PART_CHARS, end)


def _partition_chapters(text: str, ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for chapter in ranges:
        boundaries = [chapter["start"]]
        cursor = chapter["start"]
        while chapter["end"] - cursor > MAX_CHAPTER_CHARS:
            stop = _segment_end(text, cursor, chapter["end"])
            if stop <= cursor or stop - cursor > MAX_CHAPTER_CHARS:
                raise ValueError("could not split an oversized chapter safely")
            boundaries.append(stop)
            cursor = stop
        boundaries.append(chapter["end"])
        split = len(boundaries) > 2
        for part_index, (start, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
            if end - start > MAX_CHAPTER_CHARS:
                raise ValueError("committed chapter exceeds 18,000 characters")
            part_title = chapter["title"]
            if split:
                part_title = f"{part_title} · 分段{part_index}"
            chapter_id = f"{chapter['id']}.p{part_index:03d}" if split else chapter["id"]
            result.append({
                "id": chapter_id, "title": part_title, "start": start, "end": end,
                "characters": end - start,
                "excerpt": text[start:min(end, start + EXCERPT_CHARS)],
                "logical_chapter_id": chapter["id"], "part": part_index if split else None,
                "logical_title": chapter["title"],
            })
    if len(result) > MAX_CHAPTERS:
        raise ValueError(f"result exceeds {MAX_CHAPTERS} index chapters")
    return result


def _chunk_ranges(text: str, start: int, end: int):
    """Yield bounded exact source slices, preferring newline boundaries."""
    position = start
    while position < end:
        stop = min(position + BLOCK_CHARS, end)
        if stop < end:
            newline = text.rfind("\n", position, stop)
            if newline > position:
                stop = newline + 1
        if stop <= position:
            stop = min(position + BLOCK_CHARS, end)
        value = text[position:stop]
        if value.strip():
            yield position, stop, value
        position = stop


def _quote_spans(text: str, start: int, end: int) -> tuple[list[tuple[int, int]], bool]:
    """Find balanced same-line Chinese and ASCII double quotes; leave uncertain text narration."""
    spans: list[tuple[int, int]] = []
    uncertain = False
    line_start = start
    while line_start < end:
        line_end = text.find("\n", line_start, end)
        if line_end < 0:
            line_end = end
        stack: tuple[str, int] | None = None
        for position in range(line_start, line_end):
            char = text[position]
            if stack is None:
                if char in _QUOTE_PAIRS:
                    stack = (_QUOTE_PAIRS[char], position)
                elif char == '"':
                    stack = ('"', position)
            elif char == stack[0]:
                spans.append((stack[1], position + 1))
                stack = None
            elif char in _QUOTE_PAIRS or char == '"':
                # Nested or mixed quote syntax is ambiguous; do not infer a speaker.
                uncertain = True
                stack = None
        if stack is not None:
            uncertain = True
        line_start = line_end + 1
    return spans, uncertain


def _append_text_rows(rows: list[dict[str, Any]], text: str, start: int, end: int,
                      serial: list[int], warnings: list[str]) -> None:
    spans, uncertain = _quote_spans(text, start, end)
    if uncertain:
        warnings.append("存在未闭合、跨行或混合引号；不确定片段保留为旁白供复核。")
    cursor = start
    for quote_start, quote_end in spans:
        if quote_start < cursor or quote_end > end:
            continue
        for part_start, part_end, value in _chunk_ranges(text, cursor, quote_start):
            serial[0] += 1
            rows.append({"type": "narration", "id": f"n{serial[0]:07d}",
                         "text": value, "src": [part_start, part_end]})
        for part_start, part_end, value in _chunk_ranges(text, quote_start, quote_end):
            serial[0] += 1
            rows.append({"type": "dialogue", "id": f"d{serial[0]:07d}",
                         "text": value, "speaker": "unknown", "src": [part_start, part_end]})
        cursor = quote_end
    for part_start, part_end, value in _chunk_ranges(text, cursor, end):
        serial[0] += 1
        rows.append({"type": "narration", "id": f"n{serial[0]:07d}",
                     "text": value, "src": [part_start, part_end]})


def _build_script(text: str, text_sha256: str, title: str,
                  ranges: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = [{
        "type": "huaben", "version": 1, "title": title, "language": "zh",
        "text": "text.txt", "text_sha256": text_sha256,
    }]
    serial = [0]
    warnings: list[str] = []
    for chapter in ranges:
        start, end = chapter["start"], chapter["end"]
        block_start = start
        while block_start < end and text[block_start].isspace():
            block_start += 1
        if block_start >= end:
            raise ValueError("chapter part begins without a readable source character")
        line_end = text.find("\n", block_start, end)
        if line_end < 0:
            line_end = end
        chapter_stop = line_end
        if chapter_stop - block_start > BLOCK_CHARS:
            search_end = block_start + BLOCK_CHARS
            sentence_end = max((text.rfind(mark, block_start, search_end) for mark in "。！？!?；;"), default=-1)
            chapter_stop = sentence_end + 1 if sentence_end >= block_start + 100 else search_end
        if chapter_stop <= block_start:
            chapter_stop = min(block_start + BLOCK_CHARS, end)
        if not text[block_start:chapter_stop].strip():
            # A split boundary may begin on layout whitespace. Start at its first
            # real character without dropping the preceding whitespace gap.
            while chapter_stop < end and not text[block_start:chapter_stop].strip():
                chapter_stop += 1
            if chapter_stop > end or not text[block_start:chapter_stop].strip():
                raise ValueError("chapter part begins without a readable source character")
        rows.append({"type": "chapter", "id": chapter["id"],
                     "text": text[block_start:chapter_stop], "src": [block_start, chapter_stop]})
        _append_text_rows(rows, text, chapter_stop, end, serial, warnings)
    return rows, list(dict.fromkeys(warnings))


def _manifest_entry_shape(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("book catalog entry is invalid")
    required = {"book_id", "display_name", "chapters", "source_sha256", "text_sha256",
                "index_sha256", "snapshot", "index_workspace", "import_dir"}
    if set(entry) != required:
        raise ValueError("book catalog entry fields are invalid")
    if not isinstance(entry["book_id"], str) or not _BOOK_ID.fullmatch(entry["book_id"]):
        raise ValueError("book catalog identity is invalid")
    if not isinstance(entry["display_name"], str) or not entry["display_name"].strip():
        raise ValueError("book display name is invalid")
    for key in ("source_sha256", "text_sha256", "index_sha256"):
        if not isinstance(entry[key], str) or not re.fullmatch(r"[0-9a-f]{64}", entry[key]):
            raise ValueError(f"book catalog {key} is invalid")
    if not isinstance(entry["chapters"], list) or not entry["chapters"]:
        raise ValueError("book chapter catalog is invalid")
    ids = []
    for chapter in entry["chapters"]:
        if (not isinstance(chapter, dict) or set(chapter) != {"chapter_id", "title"}
                or not isinstance(chapter["chapter_id"], str)
                or not re.fullmatch(r"c\d{3,}(?:\.p\d{3,})?", chapter["chapter_id"])
                or not isinstance(chapter["title"], str)):
            raise ValueError("book chapter entry is invalid")
        ids.append(chapter["chapter_id"])
    if len(ids) != len(set(ids)):
        raise ValueError("book chapter IDs are duplicated")
    if not isinstance(entry["snapshot"], dict) or set(entry["snapshot"]) != {
        "book_id", "source_sha256", "text_sha256", "script_sha256", "cast_sha256"
    }:
        raise ValueError("book snapshot is invalid")
    if entry["snapshot"].get("book_id") != entry["book_id"]:
        raise ValueError("book snapshot identity mismatch")
    if (entry["snapshot"].get("source_sha256") != entry["source_sha256"]
            or entry["snapshot"].get("text_sha256") != entry["text_sha256"]
            or any(not isinstance(entry["snapshot"].get(field), str)
                   or not re.fullmatch(r"[0-9a-f]{64}", entry["snapshot"][field])
                   for field in ("source_sha256", "text_sha256", "script_sha256", "cast_sha256"))):
        raise ValueError("book snapshot hashes do not match catalog identity")
    expected_import_dir = f"book-imports/{entry['source_sha256'][:32]}"
    if entry["import_dir"] != expected_import_dir or entry["index_workspace"] != expected_import_dir + "/index/workspace":
        raise ValueError("book catalog paths are not canonical relative paths")
    return entry


def merge_book_names(base_mapping: Mapping[str, str], entries: list[dict[str, Any]]) -> dict[str, str]:
    """Merge imported IDs into a display-name map without replacing existing names."""
    if not isinstance(base_mapping, Mapping):
        raise ValueError("base book mapping must be a mapping")
    result = dict(base_mapping)
    if any(not isinstance(name, str) or not isinstance(book_id, str)
           for name, book_id in result.items()):
        raise ValueError("base book mapping contains invalid values")
    used_names = set(result)
    existing_ids = set(result.values())
    for raw_entry in entries:
        entry = _manifest_entry_shape(raw_entry)
        book_id = entry["book_id"]
        if book_id in existing_ids:
            continue
        base_name = entry["display_name"].strip()
        candidate = base_name
        if candidate in used_names:
            suffix = book_id.removeprefix("book-")
            length = 6
            candidate = f"{base_name} · {suffix[:length]}"
            while candidate in used_names and length < len(suffix):
                length = min(len(suffix), length + 2)
                candidate = f"{base_name} · {suffix[:length]}"
            serial = 2
            while candidate in used_names:
                candidate = f"{base_name} · {suffix} ({serial})"
                serial += 1
        result[candidate] = book_id
        used_names.add(candidate)
        existing_ids.add(book_id)
    return result


class BookImporter:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().absolute()
        self.imports = self.root / "book-imports"
        self.catalog_path = self.root / "imported-books.json"
        self.lock_path = self.root / ".book-import.lock"

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("book import root must be a real directory")

    def _lock(self):
        self._ensure_root()
        return persisted_chapter_index._process_lock(self.lock_path)

    def _load_catalog(self) -> list[dict[str, Any]]:
        if not self.catalog_path.exists():
            return []
        if self.catalog_path.is_symlink() or not self.catalog_path.is_file() or self.catalog_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("book catalog is not a regular bounded file")
        data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"version", "books"} or data["version"] != 1 or not isinstance(data["books"], list):
            raise ValueError("book catalog schema is invalid")
        entries = [_manifest_entry_shape(entry) for entry in data["books"]]
        ids = [entry["book_id"] for entry in entries]
        if len(ids) != len(set(ids)):
            raise ValueError("book catalog has duplicate IDs")
        return entries

    def _preview_path(self, preview_id: str) -> Path:
        if not isinstance(preview_id, str) or not re.fullmatch(r"[0-9a-f]{32}", preview_id):
            raise ValueError("preview ID is invalid")
        return self.imports / preview_id

    @staticmethod
    def _read_file(path: Path, limit: int) -> bytes:
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > limit:
            raise ValueError("private book import file is missing or invalid")
        raw = path.read_bytes()
        if len(raw) > limit:
            raise ValueError("private book import file exceeds its size limit")
        return raw

    def preview(self, source_path: str | Path, original_name: str) -> dict[str, Any]:
        name = _safe_filename(original_name)
        extension = Path(name).suffix.lower()
        if extension not in (".txt", ".docx"):
            raise ValueError("only TXT and DOCX sources are supported")
        source = Path(source_path).expanduser().resolve(strict=True)
        if not source.is_file() or source.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("source is not a regular file or exceeds 64 MiB")
        raw = source.read_bytes()
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError("source exceeds 64 MiB")
        raw_sha = _sha(raw)
        book_id = "book-" + raw_sha[:20]
        parsed = _parse_source(raw, extension)
        text = parsed["normalized_text"]
        starts = _chapter_candidates(text)
        chapters = _chapter_ranges(text, starts)
        warnings = []
        if extension == ".docx":
            warnings.append("DOCX仅提取正文段落和简单表格；页眉页脚不参与朗读，复杂对象、脚注批注、修订标记等结构会报错，可另存为纯文本后导入。")
        if len(starts) == 1 and starts[0]["title"] == "全文":
            warnings.append("未识别到章节标题，暂按单章预览；可在确认时手动选择边界。")
        if starts[0]["title"] == "前言":
            warnings.append("首个编号章节前存在正文，已作为前言保留在独立章节。")
        if any(chapter["characters"] > MAX_CHAPTER_CHARS for chapter in chapters):
            warnings.append(f"超长章节将在提交时按段落或句末拆分为不超过 {MAX_CHAPTER_CHARS} 字的章节。")
        preview_id = raw_sha[:32]
        summary = {
            "preview_id": preview_id, "book_id": book_id, "filename": name,
            "title": Path(name).stem[:120] or "未命名书籍",
            "source_sha256": raw_sha, "text_sha256": parsed["text_sha256"],
            "encoding": parsed["encoding"], "character_count": len(text),
            "warnings": warnings, "chapters": chapters,
        }
        private = {
            "version": 1, "summary": summary, "extension": extension,
            "parser_id": parsed["parser_id"], "parser_evidence": parsed["parser_evidence"],
        }
        with self._lock():
            self.imports.mkdir(parents=True, exist_ok=True)
            if self.imports.is_symlink() or not self.imports.is_dir():
                raise ValueError("book import directory must be a real directory")
            target = self._preview_path(preview_id)
            if target.exists():
                if target.is_symlink() or not target.is_dir():
                    raise ValueError("preview path is not a real directory")
                existing_raw = self._read_file(target / ("raw" + extension), MAX_SOURCE_BYTES)
                existing = json.loads(self._read_file(target / "preview.private.json", 16 * 1024 * 1024))
                if (_sha(existing_raw) != raw_sha or existing.get("summary", {}).get("book_id") != book_id
                        or existing.get("summary", {}).get("text_sha256") != parsed["text_sha256"]):
                    raise ValueError("preview ID collision or preview tampering")
                return existing["summary"]
            staging = Path(tempfile.mkdtemp(prefix=".preview-", dir=self.imports))
            try:
                (staging / ("raw" + extension)).write_bytes(raw)
                _atomic_json(staging / "preview.private.json", private)
                os.replace(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return summary

    def find_heading(self, preview_id: str, heading: str) -> list[dict[str, Any]]:
        """Find exact complete-line headings and return bounded preview snippets."""
        if not isinstance(heading, str) or not heading.strip() or len(heading) > 240:
            raise ValueError("heading must be a complete line of at most 240 characters")
        with self._lock():
            _directory, _metadata, raw, summary = self._load_preview(preview_id)
            extension = Path(summary["filename"]).suffix.lower()
            parsed = _parse_source(raw, extension)
            if parsed["text_sha256"] != summary["text_sha256"]:
                raise ValueError("preview text hash mismatch")
            text = parsed["normalized_text"]
            expected = heading.strip()
            matches = []
            for start, end, _raw_line, line in _iter_lines(text):
                if line.strip() == expected:
                    matches.append({
                        "id": f"c{len(matches) + 1:03d}", "title": expected,
                        "start": start, "end": end, "characters": end - start,
                        "excerpt": text[start:min(len(text), start + EXCERPT_CHARS)],
                    })
                    if len(matches) == 100:
                        break
            return matches

    def _load_preview(self, preview_id: str) -> tuple[Path, dict[str, Any], bytes, dict[str, Any]]:
        directory = self._preview_path(preview_id)
        if directory.is_symlink() or not directory.is_dir():
            raise KeyError(preview_id)
        raw_meta = self._read_file(directory / "preview.private.json", 16 * 1024 * 1024)
        metadata = json.loads(raw_meta)
        if not isinstance(metadata, dict) or metadata.get("version") != 1 or not isinstance(metadata.get("summary"), dict):
            raise ValueError("private preview metadata is invalid")
        summary = metadata["summary"]
        extension = metadata.get("extension")
        if extension not in (".txt", ".docx"):
            raise ValueError("private preview format is invalid")
        raw = self._read_file(directory / ("raw" + extension), MAX_SOURCE_BYTES)
        if (_sha(raw) != summary.get("source_sha256") or summary.get("preview_id") != preview_id
                or summary.get("book_id") != "book-" + _sha(raw)[:20]):
            raise ValueError("preview source hash mismatch")
        return directory, metadata, raw, summary

    def _make_starts(self, text: str, default_starts: list[dict[str, Any]],
                     chapter_starts: list[int] | None) -> list[dict[str, Any]]:
        if chapter_starts is None:
            return default_starts
        if not isinstance(chapter_starts, list) or not chapter_starts:
            raise ValueError("chapter_starts must be a nonempty list of normalized-text line offsets")
        line_map = {start: (end, body) for start, end, _raw, body in _iter_lines(text)}
        values = []
        for start in chapter_starts:
            if type(start) is not int or start not in line_map or not line_map[start][1].strip():
                raise ValueError("each chapter start must be a nonempty normalized-text line start")
            values.append(start)
        if values != sorted(values) or len(values) != len(set(values)):
            raise ValueError("chapter_starts must be strictly increasing and unique")
        first = _first_content_line(text)
        assert first is not None
        first_start = first[0]
        # Any leading/front-matter text remains represented as the first chapter.
        starts = list(values)
        if first_start not in starts:
            starts.insert(0, first_start)
        if len(starts) > MAX_CHAPTERS:
            raise ValueError(f"chapter_starts exceeds {MAX_CHAPTERS}")
        result = []
        for start in starts:
            line = line_map[start][1].strip()
            match = _CHAPTER.match(line)
            title = line if match else ("前言" if start == first_start else line)
            result.append({"start": start, "title": title[:240]})
        return result

    def commit(self, preview_id: str, title: str,
               chapter_starts: list[int] | None = None) -> dict[str, Any]:
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
            raise ValueError("book title must contain 1 to 120 characters")
        display_name = title.strip()
        with self._lock():
            directory, preview, raw, summary = self._load_preview(preview_id)
            entries = self._load_catalog()
            book_id = summary["book_id"]
            existing = next((entry for entry in entries if entry["book_id"] == book_id), None)
            if existing:
                if existing["source_sha256"] != _sha(raw):
                    raise ValueError("book ID prefix collision")
                return existing
            extension = preview["extension"]
            parsed = _parse_source(raw, extension)
            if parsed["text_sha256"] != summary["text_sha256"]:
                raise ValueError("source changed after preview")
            text = parsed["normalized_text"]
            starts = self._make_starts(text, summary["chapters"], chapter_starts)
            logical_ranges = _chapter_ranges(text, starts)
            ranges = _partition_chapters(text, logical_ranges)
            script_rows, script_warnings = _build_script(text, parsed["text_sha256"], display_name, ranges)
            cast = {"version": 1, "narrator": {"voice": "preset:Vivian"}, "characters": {}}
            snapshot = {
                "book_id": book_id, "source_sha256": _sha(raw),
                "text_sha256": parsed["text_sha256"],
                "script_sha256": chapter_local_index.object_sha256(script_rows),
                "cast_sha256": chapter_local_index.object_sha256(cast),
            }
            index = chapter_local_index.build_index(
                book_id=book_id, source_bytes=raw, normalized_text=text,
                script_rows=script_rows, cast=cast, trusted_snapshot=snapshot)
            index_dir = directory / "index"
            index_dir.mkdir(exist_ok=True)
            if index_dir.is_symlink() or not index_dir.is_dir():
                raise ValueError("index destination must be a real directory")
            index_path = index_dir / "workspace"
            try:
                persisted_chapter_index.store_chapter_index(
                    index, index_path, trusted_snapshot=snapshot,
                    expected_index_sha256=index.index_sha256)
            except persisted_chapter_index.ChapterIndexStoreError:
                # A completed index from an interrupted commit may be resumed;
                # partial store residues remain an explicit manual-reconcile case.
                if not index_path.is_dir():
                    raise
                for chapter in index.chapters:
                    persisted_chapter_index.restore_chapter(
                        index_path, chapter.chapter_id, trusted_snapshot=snapshot,
                        expected_index_sha256=index.index_sha256)
            script_path = directory / "script.private.json"
            cast_path = directory / "cast.private.json"
            if not script_path.exists():
                _atomic_json(script_path, script_rows)
            elif json.loads(self._read_file(script_path, 256 * 1024 * 1024)) != script_rows:
                raise ValueError("immutable script already exists with different chapter rules")
            if not cast_path.exists():
                _atomic_json(cast_path, cast)
            manifest = {
                "version": 1, "preview_id": preview_id, "book_id": book_id,
                "display_name": display_name, "extension": extension,
                "source_sha256": _sha(raw), "text_sha256": parsed["text_sha256"],
                "encoding": parsed["encoding"], "parser_id": parsed["parser_id"],
                "parser_evidence": parsed["parser_evidence"], "chapters": [
                    {"chapter_id": item["id"], "title": item["title"],
                     "start": item["start"], "end": item["end"],
                     "characters": item["characters"]} for item in ranges
                ],
                "warnings": script_warnings,
                "script_sha256": snapshot["script_sha256"], "cast_sha256": snapshot["cast_sha256"],
                "snapshot": snapshot, "index_sha256": index.index_sha256,
                "index_workspace": f"book-imports/{preview_id}/index/workspace",
            }
            manifest_path = directory / "manifest.private.json"
            if manifest_path.exists():
                if json.loads(self._read_file(manifest_path, 16 * 1024 * 1024)) != manifest:
                    raise ValueError("immutable import manifest already exists with different commit data")
            else:
                _atomic_json(manifest_path, manifest)
            entry = {
                "book_id": book_id, "display_name": display_name,
                "chapters": [{"chapter_id": item["id"], "title": item["title"]} for item in ranges],
                "source_sha256": snapshot["source_sha256"], "text_sha256": snapshot["text_sha256"],
                "index_sha256": index.index_sha256, "snapshot": snapshot,
                "index_workspace": manifest["index_workspace"],
                "import_dir": f"book-imports/{preview_id}",
            }
            _manifest_entry_shape(entry)
            entries.append(entry)
            _atomic_json(self.catalog_path, {"version": 1, "books": entries})
            return entry

    def list_books(self) -> list[dict[str, Any]]:
        # The selection module may enumerate books during import. Keep that
        # read-only path free of root/lock creation before the first commit.
        if not self.catalog_path.exists():
            return []
        with self._lock():
            # A committed catalog identity remains visible even if its backing
            # files later disappear; query() reports that integrity failure.
            return self._load_catalog()

    def _read_committed(self, book_id: str):
        if not isinstance(book_id, str) or not _BOOK_ID.fullmatch(book_id):
            raise KeyError(book_id)
        entries = self._load_catalog()
        entry = next((item for item in entries if item["book_id"] == book_id), None)
        if entry is None:
            raise KeyError(book_id)
        directory = self.root / entry["import_dir"]
        manifest = json.loads(self._read_file(directory / "manifest.private.json", 16 * 1024 * 1024))
        if not isinstance(manifest, dict) or manifest.get("version") != 1:
            raise ValueError("committed book manifest is invalid")
        raw_path = directory / ("raw" + manifest.get("extension", ""))
        raw = self._read_file(raw_path, MAX_SOURCE_BYTES)
        if _sha(raw) != manifest.get("source_sha256") or manifest.get("source_sha256") != entry["source_sha256"]:
            raise ValueError("committed source hash mismatch")
        if book_id != "book-" + _sha(raw)[:20]:
            raise ValueError("committed book identity does not match source hash")
        parsed = _parse_source(raw, manifest.get("extension"))
        if (parsed["text_sha256"] != manifest.get("text_sha256")
                or parsed["text_sha256"] != entry["text_sha256"]
                or parsed["parser_id"] != manifest.get("parser_id")
                or parsed["parser_evidence"] != manifest.get("parser_evidence")):
            raise ValueError("committed normalized text or parser evidence changed")
        text = parsed["normalized_text"]
        script_rows = json.loads(self._read_file(directory / "script.private.json", 256 * 1024 * 1024))
        cast = json.loads(self._read_file(directory / "cast.private.json", 16 * 1024 * 1024))
        snapshot = {
            "book_id": book_id, "source_sha256": _sha(raw), "text_sha256": parsed["text_sha256"],
            "script_sha256": chapter_local_index.object_sha256(script_rows),
            "cast_sha256": chapter_local_index.object_sha256(cast),
        }
        if snapshot != manifest.get("snapshot") or snapshot != entry["snapshot"]:
            raise ValueError("committed source/script/cast snapshot mismatch")
        index = chapter_local_index.build_index(
            book_id=book_id, source_bytes=raw, normalized_text=text,
            script_rows=script_rows, cast=cast, trusted_snapshot=snapshot)
        if index.index_sha256 != manifest.get("index_sha256") or index.index_sha256 != entry["index_sha256"]:
            raise ValueError("committed index digest mismatch")
        if (manifest.get("index_workspace") != entry.get("index_workspace")
                or entry.get("import_dir") != f"book-imports/{manifest.get('preview_id')}"
                or manifest.get("display_name") != entry.get("display_name")):
            raise ValueError("committed relative path binding mismatch")
        manifest_chapters = manifest.get("chapters")
        if not isinstance(manifest_chapters, list) or len(manifest_chapters) != len(index.chapters):
            raise ValueError("committed chapter catalog does not match rebuilt index")
        for indexed, stored in zip(index.chapters, manifest_chapters):
            if (not isinstance(stored, dict)
                    or stored != {"chapter_id": indexed.chapter_id, "title": stored.get("title"),
                                  "start": indexed.original_start, "end": indexed.original_end,
                                  "characters": len(indexed.text)}
                    or not isinstance(stored.get("title"), str)):
                raise ValueError("committed chapter catalog does not match rebuilt index")
        if entry["chapters"] != [{"chapter_id": item["chapter_id"], "title": item["title"]}
                                  for item in manifest_chapters]:
            raise ValueError("catalog chapter titles differ from committed manifest")
        return entry, manifest, snapshot, index, directory

    def query(self, action: str, book_id: str, chapter: str | None = None) -> dict[str, Any]:
        if action not in ("list", "prepare"):
            raise ValueError("unsupported book query action")
        if (action == "list" and chapter is not None) or (action == "prepare" and not isinstance(chapter, str)):
            raise ValueError("invalid chapter query")
        with self._lock():
            entry, manifest, snapshot, index, directory = self._read_committed(book_id)
            index_workspace = self.root / entry["index_workspace"]
            chosen = index.chapters[0].chapter_id if action == "list" else chapter
            if action == "prepare" and chapter not in {item.chapter_id for item in index.chapters}:
                raise ValueError("chapter is not registered for this book")
            restored = persisted_chapter_index.restore_chapter(
                index_workspace, chosen, trusted_snapshot=snapshot,
                expected_index_sha256=index.index_sha256)
            if action == "list":
                chapters = [{"chapter_id": item.chapter_id, "title": metadata["title"]}
                            for item, metadata in zip(index.chapters, manifest["chapters"])]
                return {"book_id": book_id, "chapters": [item["chapter_id"] for item in chapters],
                        "index_sha256": index.index_sha256, "human_reviewed": False}
            prepared = chapter_attribution_input.prepare(restored)
            return {
                "book_id": book_id, "chapter_id": chapter,
                "payload": prepared["payload"], "payload_sha256": prepared["payload_sha256"],
                "chapter_digest": restored["chapter_digest"],
                "local_text_sha256": restored["local_text_sha256"],
                "index_sha256": index.index_sha256, "human_reviewed": False,
            }


def query(root: str | Path, action: str, book_id: str, chapter: str | None = None) -> dict[str, Any]:
    """Convenience wrapper for service routing."""
    return BookImporter(root).query(action, book_id, chapter)
