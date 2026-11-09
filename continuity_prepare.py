"""Create a fresh two-chapter synthetic book inside an isolated runtime folder."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

import release_config
release_config.apply_mac_environment()


root = Path(__file__).resolve().parent
driver = Path(__file__).resolve().parent
expected = release_config.mac_stage_root().resolve(strict=True)
if root.parent != expected or not root.name.startswith("continuity-synthetic-"):
    raise RuntimeError("unexpected synthetic experiment root")

sys.path.insert(0, str(driver))
import annotation_job
import voice_console_backend as backend

runtime = root / ".runtime"
runtime.mkdir(exist_ok=False)
huashuo_source = release_config.huashuo_root() / "source"
if not huashuo_source.is_dir():
    raise FileNotFoundError("configured Huashuo source checkout is missing")
runtime.joinpath("source").symlink_to(huashuo_source, target_is_directory=True)
hf_cache = release_config.hf_home()
if not (hf_cache / "hub").is_dir():
    raise FileNotFoundError("configured Hugging Face cache is missing")
runtime.joinpath("hf").symlink_to(hf_cache, target_is_directory=True)
shutil.copyfile(driver / "book_memory.py", runtime / "book_memory.py")
shutil.copyfile(driver / "voice_design_engine.py", runtime / "voice_design_engine.py")

book_id = "book-eeeeeeeeeeeeeeeeeeee"  # Fixed synthetic fixture identity.
folder = runtime / "books" / book_id
folder.mkdir(parents=True)
book = folder / "book.txt"
workdir = folder / "work.huashuo"
book.write_text(
    "第1章 清晨\n清晨的微风吹过山谷。小鸟在树梢轻轻鸣唱。\n"
    "第2章 夜晚\n夜幕落下，山谷中的灯光一盏一盏亮了起来。\n",
    encoding="utf-8",
)
(folder / "book.json").write_text(
    json.dumps({"book_id": book_id, "input_file": book.name, "original_name": "synthetic-continuity-fixture"}),
    encoding="utf-8",
)
backend.ROOT = runtime
backend.PYTHON = Path(sys.executable)
backend.HUASHUO = release_config.mac_huashuo_cli()
backend.select_book(book_id)
backend.run([backend.HUASHUO, "import", book, "--workdir", workdir, "--single-voice"])
backend.run([backend.PYTHON, runtime / "book_memory.py", "init", workdir, "--book-id", book_id])
backend.select_book(book_id)
chapters = [row["id"] for row in annotation_job.rows(workdir / "script.huaben.jsonl") if row.get("type") == "chapter"]
if len(chapters) != 2:
    raise RuntimeError("expected exactly two synthetic chapters")
for chapter in chapters:
    annotation_job.record_chapter_review(
        workdir, {"chapter_id": chapter, "book_id": book_id, "reviewed_by": "synthetic-experiment"}
    )
print(json.dumps({"chapters": chapters}))
