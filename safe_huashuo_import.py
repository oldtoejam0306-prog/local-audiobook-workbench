"""Run the official Huashuo CLI with a narrow long-number import guard.

This wrapper leaves the Huashuo source tree untouched. It patches only
``huashuo.structure.section_reading`` before importing the official CLI.
"""

from __future__ import annotations

import functools
import importlib
import os
from pathlib import Path
import re
import sys
from types import ModuleType
from typing import Callable
import release_config


_PURE_DIGITS = re.compile(r"[0-9０-９]+\Z")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_PATCH_MARKER = "_safe_huashuo_import_original"


def _guarded_section_reading(
    original: Callable[[str, str], str | None],
    section_number_pattern: re.Pattern[str],
):
    """Build a wrapper that skips unsupported long Chinese numeric headings."""

    @functools.wraps(original)
    def section_reading(text: str, language: str) -> str | None:
        match = section_number_pattern.match(text.strip())
        number = match.group(1) if match else ""
        digits = number.translate(_FULLWIDTH_DIGITS)
        if language == "zh" and _PURE_DIGITS.fullmatch(number):
            significant = digits.lstrip("0") or "0"
            if len(significant) > 4:
                return None
        return original(text, language)

    setattr(section_reading, _PATCH_MARKER, original)
    return section_reading


def patch_structure(structure: ModuleType) -> Callable[[str, str], str | None]:
    """Patch one imported Huashuo structure module; return its original function."""

    current = structure.section_reading
    original = getattr(current, _PATCH_MARKER, current)
    if not hasattr(current, _PATCH_MARKER):
        structure.section_reading = _guarded_section_reading(original, structure._SECTION_NUMBER)
    return original


def _huashuo_root() -> Path:
    return release_config.huashuo_root()


def main() -> int | None:
    """Install the guard and pass the current arguments to the official CLI."""

    source_root = _huashuo_root() / "source" / "src"
    if not source_root.is_dir():
        raise FileNotFoundError(
            f"Huashuo source directory not found: {source_root}; set HUASHUO_ROOT to its checkout"
        )

    source_string = str(source_root)
    if source_string not in sys.path:
        sys.path.insert(0, source_string)

    structure = importlib.import_module("huashuo.structure")
    patch_structure(structure)
    cli = importlib.import_module("huashuo.cli")
    return cli.main()


if __name__ == "__main__":
    main()
