"""Inspect a local TXT or DOCX without displaying or uploading its contents."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import local_txt_parser

MAX_INPUT_BYTES = 50 * 1024 * 1024


def inspect_file(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("input must be a regular local file")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("input exceeds the 50 MiB size limit")
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".txt":
        parsed = local_txt_parser.parse_local_txt_bytes(raw, source_name=path.name)
    elif suffix == ".docx":
        import local_docx_parser

        parsed = local_docx_parser.parse_local_docx_bytes(raw, source_name=path.name)
    else:
        raise ValueError("supported input formats are .txt and .docx")
    evidence = parsed["parser_evidence"]
    return {
        "format": suffix[1:],
        "source_bytes": len(raw),
        "normalized_characters": len(parsed["normalized_text"]),
        "source_sha256": parsed["source_sha256"],
        "text_sha256": parsed["text_sha256"],
        "parser_id": evidence["parser_id"],
        "parser_version": evidence["parser_version"],
        "network_transfer": False,
        "content_printed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Locally inspect a TXT or DOCX file and print metadata only."
    )
    parser.add_argument("input", type=Path, help="local .txt or .docx file")
    args = parser.parse_args(argv)
    try:
        report = inspect_file(args.input)
    except (OSError, ValueError, ImportError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
