"""Configurable local viewer for explicitly registered chapter artifacts.

The repository contains no book registry or receipts. Users keep those in an
external workbench configuration and a private data directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shlex
import subprocess
import threading
import tkinter as tk
import wave
import winsound
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from tkinter import ttk, messagebox

import release_config
import workbench_real_control as control


BASE = release_config.local_workbench_root()
_BOOK_ID = re.compile(r"book-[0-9a-f]{20}\Z")
_CHAPTER_ID = re.compile(r"c[0-9]+(?:\.p[0-9]+(?:\.[0-9]+)?)?\Z")


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def registry() -> list[dict]:
    books = release_config.section("workbench").get("books", [])
    if not isinstance(books, list):
        raise ValueError("workbench.books must be a list")
    checked = []
    seen = set()
    for book in books:
        if not isinstance(book, dict) or set(book) - {"book_id", "display_name", "chapters"}:
            raise ValueError("invalid workbench book entry")
        book_id = book.get("book_id")
        title = book.get("display_name")
        chapters = book.get("chapters")
        if not isinstance(book_id, str) or not _BOOK_ID.fullmatch(book_id) or book_id in seen:
            raise ValueError("invalid or duplicate workbench book id")
        if not isinstance(title, str) or not title.strip() or len(title) > 120 or not isinstance(chapters, list):
            raise ValueError("invalid workbench book metadata")
        seen.add(book_id)
        chapter_ids = set()
        for chapter in chapters:
            if not isinstance(chapter, dict):
                raise ValueError("invalid workbench chapter entry")
            chapter_id = chapter.get("chapter_id")
            if not isinstance(chapter_id, str) or not _CHAPTER_ID.fullmatch(chapter_id) or chapter_id in chapter_ids:
                raise ValueError("invalid or duplicate workbench chapter id")
            chapter_ids.add(chapter_id)
        checked.append(book)
    return checked


# Compatibility map for the book-scoped experimental UI. Its contents come
# only from the user's external configuration; the distributed default is empty.
BOOKS = {book["display_name"]: book["book_id"] for book in registry()}


def chapter_spec(book_id: str, chapter_id: str) -> dict:
    for book in registry():
        if book["book_id"] == book_id:
            for chapter in book["chapters"]:
                if chapter["chapter_id"] == chapter_id:
                    return chapter
    raise ValueError("chapter is not registered in the local workbench config")


def _data_path(value: str | None, *, required: bool = True) -> Path | None:
    if value is None:
        if required:
            raise ValueError("required artifact path is missing from config")
        return None
    if not isinstance(value, str) or not value.strip():
        if required:
            raise ValueError("invalid artifact path in config")
        return None
    root = BASE.resolve(strict=False)
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root) or path.is_symlink():
        raise ValueError("configured artifact path escapes the workbench data root")
    return resolved


def _usage_record(book_id: str, chapter_id: str, spec: dict):
    folder = _data_path(spec.get("receipt_dir"), required=False)
    usage_path = _data_path(spec.get("usage_path"), required=False)
    if usage_path is None and folder is not None:
        usage_path = folder / "usage.json"
    if usage_path is None or not usage_path.is_file():
        return None
    data = load(usage_path)
    usage = data.get("usage")
    response_id = data.get("response_id") or data.get("id")
    if not isinstance(usage, dict):
        raise ValueError("usage evidence is malformed")
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    if type(input_tokens) is not int or type(output_tokens) is not int or min(input_tokens, output_tokens) < 0:
        raise ValueError("usage token counts are invalid")
    input_path = _data_path(spec.get("input_path"), required=False)
    if input_path is not None and input_path.is_file() and folder is not None:
        intent_path = folder / "intent.json"
        if not intent_path.is_file():
            raise ValueError("usage receipt has no request intent")
        intent = load(intent_path)
        if intent.get("book_id") != book_id or intent.get("chapter_id") != chapter_id or intent.get("input_sha256") != sha(input_path):
            raise ValueError("usage receipt does not match the configured input")
        body_path = folder / "response.body"
        meta_path = folder / "response.meta.json"
        if body_path.is_file() and meta_path.is_file():
            meta = load(meta_path)
            if sha(body_path) != meta.get("body_sha256") or meta.get("http_status") != 200:
                raise ValueError("response receipt integrity check failed")
            response = load(body_path)
            if response.get("id") != response_id or response.get("usage") != usage:
                raise ValueError("usage receipt does not match the saved response")
    return {"response_id": response_id, "input": input_tokens, "output": output_tokens}


def usage_info(book_id: str, chapter_id: str) -> str:
    try:
        record = _usage_record(book_id, chapter_id, chapter_spec(book_id, chapter_id))
        if record is None:
            return "用量证据未知；未知不等于零。"
        return f"输入 {record['input']} / 输出 {record['output']} tokens；按当前手动单价估算。"
    except (OSError, ValueError, KeyError, TypeError):
        return "用量证据未知；未知不等于零。"


def book_usage() -> dict:
    unique = {}
    unknown = []
    duplicates = []
    for book in registry():
        for chapter in book["chapters"]:
            cid = chapter["chapter_id"]
            try:
                record = _usage_record(book["book_id"], cid, chapter)
                if record is None or not record.get("response_id"):
                    unknown.append(f"{book['book_id']}:{cid}")
                    continue
                rid = record["response_id"]
                counts = (record["input"], record["output"])
                if rid in unique:
                    if unique[rid] != counts:
                        unknown.append(f"{book['book_id']}:{cid}")
                    else:
                        duplicates.append(f"{book['book_id']}:{cid}")
                else:
                    unique[rid] = counts
            except (OSError, ValueError, KeyError, TypeError):
                unknown.append(f"{book['book_id']}:{cid}")
    input_tokens = sum(item[0] for item in unique.values())
    output_tokens = sum(item[1] for item in unique.values())
    return {"input": input_tokens, "output": output_tokens, "known_responses": len(unique),
            "unknown": unknown, "duplicates": duplicates}


def usage_text() -> str:
    usage = book_usage()
    return (f"已核对响应 {usage['known_responses']} 条；输入 {usage['input']} / 输出 {usage['output']} tokens。"
            f"用量未知：{len(usage['unknown'])}；重复响应：{len(usage['duplicates'])}。费用需按当前价卡另行估算。")


def verify_saved_analysis(book_id: str, chapter_id: str) -> int:
    import real_pipeline_analysis as analysis
    spec = chapter_spec(book_id, chapter_id)
    source = _data_path(spec.get("input_path"))
    receipt = _data_path(spec.get("receipt_dir"))
    voices_path = _data_path(spec.get("voice_mapping_path"))
    rebuilt = analysis.build(source, receipt, load(voices_path))
    if spec.get("render_manifest"):
        render_path = _data_path(spec["render_manifest"])
        render = load(render_path)
        role_ids = spec.get("role_ids", {})
        if not isinstance(role_ids, dict):
            raise ValueError("role_ids must be a mapping in the private workbench config")
        chapters, audit = analysis.to_render_chapters(source, rebuilt, role_ids)
        if chapters != render.get("chapters") or audit != render.get("audit") or rebuilt.get("provenance") != render.get("provenance"):
            raise ValueError("saved render differs from the verified analysis receipt")
        return sum(len(chapter.get("segments", [])) for chapter in chapters)
    return sum(1 for row in rebuilt.get("script_rows", []) if row.get("type") == "dialogue")


def artifacts() -> list[dict]:
    rows = []
    for book in registry():
        for spec in book["chapters"]:
            manifest_value = spec.get("manifest_path")
            if not manifest_value:
                continue
            try:
                manifest_path = _data_path(manifest_value)
                manifest = load(manifest_path)
                chapters = manifest.get("chapters")
                chapter_entry = None
                if isinstance(chapters, list):
                    chapter_entry = next((row for row in chapters if row.get("id") == spec["chapter_id"]), None)
                status = (chapter_entry or manifest).get("status", "unknown")
                segments = (chapter_entry or manifest).get("segments", (chapter_entry or manifest).get("units", 0))
                if not isinstance(segments, int):
                    segments = len(segments) if isinstance(segments, list) else 0
                rows.append({"book": book["display_name"], "book_id": book["book_id"],
                             "chapter": spec["chapter_id"], "status": status,
                             "segments": segments, "manifest": manifest_path, "kind": spec.get("kind", "generic")})
            except (OSError, ValueError, KeyError, TypeError):
                rows.append({"book": book["display_name"], "book_id": book["book_id"],
                             "chapter": spec["chapter_id"], "status": "artifact unavailable", "segments": 0,
                             "manifest": None, "kind": spec.get("kind", "generic")})
    return rows


def verified_audio(row: dict) -> tuple[Path, float]:
    if row.get("status") != "complete" or not row.get("manifest"):
        raise ValueError("artifact is not complete")
    spec = chapter_spec(row["book_id"], row["chapter"])
    manifest_path = _data_path(spec.get("manifest_path"))
    manifest = load(manifest_path)
    chapter_entry = None
    entries = manifest.get("chapters")
    if isinstance(entries, list):
        chapter_entry = next((item for item in entries if item.get("id") == row["chapter"]), None)
    record = chapter_entry or manifest
    audio_value = spec.get("audio_path") or record.get("audio_path") or record.get("path")
    audio = _data_path(audio_value)
    expected_size = record.get("audio_bytes", record.get("bytes"))
    expected_hash = record.get("audio_sha256", record.get("sha256"))
    if audio is None or not audio.is_file() or type(expected_size) is not int or audio.stat().st_size != expected_size:
        raise ValueError("audio file size does not match the manifest")
    if not isinstance(expected_hash, str) or sha(audio) != expected_hash:
        raise ValueError("audio hash does not match the manifest")
    source_value = spec.get("source_path")
    expected_source = record.get("source_sha256")
    if source_value and expected_source:
        source = _data_path(source_value)
        if sha(source) != expected_source:
            raise ValueError("source artifact hash does not match the manifest")
    with wave.open(str(audio), "rb") as wav:
        if wav.getnframes() <= 0 or wav.getcomptype() != "NONE":
            raise ValueError("audio is empty or compressed")
        duration = wav.getnframes() / wav.getframerate()
    return audio, duration


def read_experiment_progress() -> list[str]:
    remote = release_config.section("workbench").get("remote_progress_files", [])
    if not isinstance(remote, list):
        return []
    results = []
    for item in remote:
        if not isinstance(item, dict) or not isinstance(item.get("label"), str) or not isinstance(item.get("path"), str):
            continue
        path = PurePosixPath(item["path"])
        if not path.is_absolute() or ".." in path.parts:
            continue
        command = ["ssh", *release_config.ssh_options(), release_config.ssh_target(), f"cat {shlex.quote(str(path))}"]
        try:
            proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=15,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            data = json.loads(proc.stdout) if proc.returncode == 0 and len(proc.stdout) <= 100000 else {}
            history = data.get("history", [])
            last = next((entry for entry in reversed(history) if isinstance(entry, dict) and "cached" in entry and "total" in entry), None)
            if last and type(last["cached"]) is int and type(last["total"]) is int and last["total"] > 0:
                results.append(f"{item['label']}: {last['cached']}/{last['total']}")
            else:
                results.append(f"{item['label']}: unknown")
        except Exception:
            results.append(f"{item['label']}: unknown")
    return results


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("本地章节工作台")
        self.geometry("980x720")
        self.rows = []
        self.messages = queue.Queue()
        self.control_job = ""
        self.control_busy = False
        self.control_job_file = BASE / "control" / "last-job.txt"
        try:
            self.control_job = self.control_job_file.read_text(encoding="ascii").strip()
        except OSError:
            pass
        panel = ttk.Frame(self, padding=14)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="本地章节工作台", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(panel, text="只读取外部配置登记的本机材料；默认工作台不登记任何书籍或任务。", wraplength=900).pack(anchor="w", pady=6)
        self.table = ttk.Treeview(panel, columns=("book", "chapter", "status", "segments"), show="headings", height=12)
        for column, label, width in [("book", "书籍标签", 180), ("chapter", "章节", 100), ("status", "状态", 390), ("segments", "片段", 100)]:
            self.table.heading(column, text=label)
            self.table.column(column, width=width)
        self.table.pack(fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", self.selected)
        bar = ttk.Frame(panel)
        bar.pack(fill="x", pady=8)
        ttk.Button(bar, text="刷新", command=self.refresh).pack(side="left")
        ttk.Button(bar, text="核对登记结果", command=self.verify).pack(side="left", padx=6)
        ttk.Button(bar, text="播放已核验音频", command=self.play).pack(side="left", padx=6)
        ttk.Button(bar, text="停止播放", command=lambda: winsound.PlaySound(None, winsound.SND_PURGE)).pack(side="left", padx=6)
        self.status = tk.StringVar(value="等待配置")
        ttk.Label(panel, textvariable=self.status, wraplength=900).pack(anchor="w")
        self.usage = tk.StringVar(value=usage_text())
        ttk.Label(panel, textvariable=self.usage, wraplength=900).pack(anchor="w", pady=4)
        self.progress = tk.StringVar(value="")
        ttk.Label(panel, textvariable=self.progress, wraplength=900).pack(anchor="w", pady=4)
        ttk.Separator(panel).pack(fill="x", pady=8)
        controls = ttk.Frame(panel)
        controls.pack(fill="x")
        self.synthetic_text = tk.StringVar()
        ttk.Entry(controls, textvariable=self.synthetic_text, width=32).pack(side="left")
        ttk.Button(controls, text="启动合成样例", command=self.control_start).pack(side="left", padx=4)
        ttk.Button(controls, text="启动语音实验", command=self.control_voice).pack(side="left", padx=4)
        ttk.Button(controls, text="任务状态", command=self.control_status).pack(side="left", padx=4)
        ttk.Button(controls, text="停止任务", command=self.control_stop).pack(side="left", padx=4)
        ttk.Button(controls, text="试听实验音频", command=self.control_play).pack(side="left", padx=4)
        self.control_note = tk.StringVar(value="实验控制只提交明确输入；状态不证明真实书或完整工作流可用。")
        ttk.Label(panel, textvariable=self.control_note, wraplength=900).pack(anchor="w", pady=4)
        self.refresh()
        self.after(100, self.poll_messages)
        self.after(5000, self.auto_refresh)
        self.after(20000, self.poll_progress)
        self.after(10000, self.poll_control)

    def selected(self, event=None):
        selection = self.table.selection()
        if selection:
            row = self.rows[int(selection[0])]
            self.usage.set(usage_info(row["book_id"], row["chapter"]))

    def refresh(self, quiet=False):
        selected = self.table.selection()
        old = self.rows[int(selected[0])] if selected else None
        old_key = (old.get("book_id"), old.get("chapter")) if old else None
        self.rows = artifacts()
        self.table.delete(*self.table.get_children())
        for index, row in enumerate(self.rows):
            self.table.insert("", "end", iid=str(index), values=(row["book"], row["chapter"], row["status"], row["segments"]))
            if old_key == (row.get("book_id"), row.get("chapter")):
                self.table.selection_set(str(index))
        self.usage.set(usage_text())
        if not self.rows and not quiet:
            self.status.set("本机配置没有登记任何书籍或章节。")

    def auto_refresh(self):
        self.refresh(quiet=True)
        self.after(5000, self.auto_refresh)

    def poll_messages(self):
        while True:
            try:
                item = self.messages.get_nowait()
                if isinstance(item, tuple) and item[0] == "control":
                    self.control_busy = False
                    self.control_note.set(item[1])
                    if len(item) > 2 and item[2]:
                        self.control_job = item[2]
                        self.control_job_file.parent.mkdir(parents=True, exist_ok=True)
                        temp = self.control_job_file.with_suffix(".tmp")
                        temp.write_text(self.control_job, encoding="ascii")
                        os.replace(temp, self.control_job_file)
                elif isinstance(item, tuple) and item[0] == "progress":
                    self.progress.set(item[1])
                else:
                    self.status.set(str(item))
            except queue.Empty:
                break
        self.after(100, self.poll_messages)

    def poll_progress(self):
        def worker():
            values = read_experiment_progress()
            self.messages.put(("progress", "；".join(values)))
        if release_config.section("workbench").get("remote_progress_files"):
            threading.Thread(target=worker, daemon=True).start()
        self.after(20000, self.poll_progress)

    def verify(self):
        selection = self.table.selection()
        if not selection:
            self.status.set("请先选择登记的章节。")
            return
        row = self.rows[int(selection[0])]
        self.status.set("正在核对本机材料…")
        def worker():
            try:
                count = verify_saved_analysis(row["book_id"], row["chapter"])
                message = f"{row['chapter']}：本机登记材料重建一致；片段 {count}。"
            except Exception:
                message = "核对失败；请检查私有配置和证据文件。"
            self.messages.put(message)
        threading.Thread(target=worker, daemon=True).start()

    def play(self):
        selection = self.table.selection()
        if not selection:
            self.status.set("请先选择登记的章节。")
            return
        row = self.rows[int(selection[0])]
        self.status.set("正在核验音频…")
        def worker():
            try:
                audio, duration = verified_audio(row)
                winsound.PlaySound(str(audio), winsound.SND_FILENAME | winsound.SND_ASYNC)
                message = f"{row['chapter']} 已播放；时长约 {duration:.1f} 秒。听感仍需人工确认。"
            except Exception:
                message = "音频核验失败；不会播放未匹配登记记录的文件。"
            self.messages.put(message)
        threading.Thread(target=worker, daemon=True).start()

    def control_run(self, operation):
        if self.control_busy:
            return
        text = self.synthetic_text.get().strip()
        job = self.control_job
        if operation in ("start", "voice") and not text:
            self.control_note.set("请输入自写短句。")
            return
        if operation not in ("start", "voice") and not job:
            self.control_note.set("没有已登记的本机任务。")
            return
        self.control_busy = True
        self.control_note.set("正在访问配置的受控 worker…")
        def worker():
            try:
                new_job = ""
                if operation in ("start", "voice"):
                    result = control.start(text, "voice" if operation == "voice" else "synthetic")
                    new_job = result.get("job", "") if result.get("status") in ("reserved", "already_reserved") else ""
                    message = f"任务状态：{result.get('status', 'unknown')}。"
                elif operation == "status":
                    result = control.status(job)
                    message = f"任务状态：{result.get('status', 'unknown')}；模式：{result.get('mode', 'unknown')}。"
                elif operation == "stop":
                    result = control.stop(job)
                    message = f"停止状态：{result.get('status', 'unknown')}。"
                else:
                    result = control.status(job)
                    audio = control.fetch(job)
                    winsound.PlaySound(str(audio), winsound.SND_FILENAME | winsound.SND_ASYNC)
                    message = f"实验音频已下载并播放；任务状态：{result.get('status', 'unknown')}。"
                self.messages.put(("control", message, new_job))
            except Exception:
                self.messages.put(("control", "操作失败或状态不确定；请在 worker 主机核对，不会自动重试。", ""))
        threading.Thread(target=worker, daemon=True).start()

    def control_start(self): self.control_run("start")
    def control_voice(self): self.control_run("voice")
    def control_status(self): self.control_run("status")
    def control_stop(self): self.control_run("stop")
    def control_play(self): self.control_run("play")

    def poll_control(self):
        if self.control_job and not self.control_busy:
            self.control_status()
        self.after(10000, self.poll_control)


if __name__ == "__main__":
    App().mainloop()
