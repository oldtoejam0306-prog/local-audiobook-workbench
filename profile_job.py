"""Prepare and review one-chapter character profile jobs without writing memory."""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import annotation_job


@contextmanager
def _book_lock(wd):
    """Reuse the backend's already-held lock, or acquire it for direct callers."""
    wd = Path(wd).resolve()
    lock_path = wd / ".console.lock"
    backend = sys.modules.get("voice_console_backend") or sys.modules.get("__main__")
    held_fd = getattr(backend, "BOOK_LOCK_FD", None) if backend is not None else None
    if held_fd is not None:
        try:
            held_stat = os.fstat(held_fd)
            path_stat = lock_path.stat()
        except OSError:
            held_fd = None
        else:
            if (held_stat.st_dev, held_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino):
                yield
                return
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0"); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _manifest_binding(wd, chapter_id, export_path=None, state=None, state_sha256=None,
                      require_enabled=False):
    """Return exact manifest/receipt binding for enabled mode, else None."""
    manifest_path = Path(wd) / "state" / "analysis-manifest.json"
    if not manifest_path.is_file():
        if require_enabled:
            raise ValueError("待审画像已绑定画像账本，但账本文件缺失。")
        return None
    import analysis_manifest as ledger
    manifest = ledger.load_manifest(manifest_path)
    if manifest.get("profile_manifest_mode_enabled") is not True:
        if require_enabled:
            raise ValueError("待审画像已绑定画像账本，但账本模式已关闭。")
        return None
    chapter = next((row for row in manifest["chapters"] if row["chapter_id"] == chapter_id), None)
    if not chapter or chapter.get("status") != "awaiting_review":
        raise ValueError("画像账本当前章节未处于匹配的待审核状态。")
    provenance = chapter.get("request_provenance")
    receipt_provenance = chapter.get("receipt_provenance")
    if not isinstance(provenance, dict) or not isinstance(receipt_provenance, dict):
        raise ValueError("画像账本缺少请求或收据绑定。")
    raw_export = Path(provenance.get("export_path", "")).resolve()
    receipt_path = Path(receipt_provenance.get("receipt_path", "")).resolve()
    wd_resolved = Path(wd).resolve()
    if not raw_export.is_relative_to(wd_resolved) or not receipt_path.is_relative_to(wd_resolved):
        raise ValueError("画像账本任务或收据路径超出书籍工作区。")
    try:
        raw_export_bytes = raw_export.read_bytes()
        receipt_bytes = receipt_path.read_bytes()
        raw_export_hash = hashlib.sha256(raw_export_bytes).hexdigest()
        receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
        receipt = json.loads(receipt_bytes.decode("utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("画像账本原始任务或收据不可读取。") from exc
    expected_job = provenance.get("job_hash")
    expected_attempt = ledger._canonical_hash({
        "book_id": manifest.get("book_id"), "chapter_id": chapter_id,
        "job_hash": expected_job,
        "request_body_sha256": provenance.get("request_body_sha256"),
        "mjs_source_sha256": provenance.get("mjs_source_sha256"),
    })
    if (raw_export_hash != expected_job or raw_export_hash != provenance.get("export_sha256")
            or provenance.get("attempt_id") != expected_attempt
            or receipt_hash != receipt_provenance.get("receipt_sha256")
            or (state_sha256 is not None and state_sha256 != receipt_hash)
            or receipt.get("status") != "complete_needs_review"
            or receipt.get("job_hash") != expected_job
            or receipt.get("book_id") != manifest.get("book_id")
            or receipt.get("chapter_id") != chapter_id
            or receipt.get("request_body_sha256") != provenance.get("request_body_sha256")
            or receipt.get("mjs_source_sha256") != provenance.get("mjs_source_sha256")):
        raise ValueError("画像收据与账本预留任务不匹配或已改变。")
    usage = receipt.get("usage")
    recorded_usage = chapter.get("usage")
    result = receipt.get("result")
    if (not isinstance(usage, dict) or not isinstance(recorded_usage, dict)
            or not isinstance(result, dict) or not isinstance(result.get("summary"), str)
            or result["summary"] != chapter.get("draft_summary")
            or receipt.get("response_id") != chapter.get("provider_response_id")
            or usage.get("prompt_tokens") != recorded_usage.get("input_tokens")
            or usage.get("completion_tokens") != recorded_usage.get("output_tokens")
            or usage.get("total_tokens", usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)) != recorded_usage.get("total_tokens")):
        raise ValueError("画像收据用量或草稿摘要与账本记录不匹配。")
    estimate = receipt.get("estimated_peak_cost_cny")
    if (not _same_money_amount(estimate, recorded_usage.get("cost"))
            or recorded_usage.get("currency") != "CNY"
            or recorded_usage.get("cost_kind") != "estimated"
            or recorded_usage.get("billing_status") != "unverified"):
        raise ValueError("画像收据费用记录与账本用量不匹配。")
    if export_path is not None and _sha(export_path) != raw_export_hash:
        raise ValueError("画像任务导出与账本预留的原始字节不匹配。")
    if state is not None and (state.get("job_hash") != raw_export_hash
                              or state.get("book_id") != manifest.get("book_id")
                              or state.get("chapter_id") != chapter_id):
        raise ValueError("画像任务状态与账本收据不匹配。")
    return {
        "manifest_path": str(manifest_path), "attempt_id": provenance.get("attempt_id"),
        "job_hash": raw_export_hash, "request_body_sha256": provenance.get("request_body_sha256"),
        "receipt_path": str(receipt_path), "receipt_sha256": receipt_hash,
    }


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _same_money_amount(left, right):
    """Compare JSON decimal spellings numerically without binary float math."""
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
    except (InvalidOperation, ValueError):
        return False
    return a.is_finite() and b.is_finite() and a >= 0 and b >= 0 and a == b


def _dump(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def recover(wd):
    """Restore the previous profile files after an interrupted commit."""
    wd = Path(wd).resolve()
    txn_path = wd / "state" / "profile-transaction.json"
    if not txn_path.exists():
        return False
    txn = _load(txn_path)
    if txn.get("status") != "applying":
        return False
    backup = Path(txn.get("backup", "")).resolve()
    backup_root = (wd / "profile-backups").resolve()
    if not backup.is_relative_to(backup_root) or not backup.is_dir():
        raise ValueError("画像事务备份路径无效，无法安全恢复。")
    for name in ("book-memory.json", "cast.json", "profile-review.json"):
        src, dst = backup / name, wd / name
        if not src.is_file():
            raise ValueError("画像事务备份不完整，无法恢复。")
        shutil.copy2(src, dst)
    txn["status"] = "restored_after_interruption"
    _dump(txn_path, txn)
    return True


def export(wd, out, chapter_id):
    """Export exactly one chapter through annotation_job.export's range logic."""
    wd, out = Path(wd).resolve(), Path(out).resolve()
    recover(wd)
    memory_path = wd / "book-memory.json"
    memory = _load(memory_path)
    if not isinstance(chapter_id, str) or not chapter_id:
        raise ValueError("章节 ID 必须指定。")

    # Reuse chapter boundary validation without duplicating the whole book.
    with tempfile.TemporaryDirectory(prefix="profile-export-") as temp:
        raw = Path(temp) / "annotation-export.json"
        annotation_job.export(wd, raw, chapter_id, allow_empty_cast=True)
        data = _load(raw)
    data["memory_sha256"] = _sha(memory_path)
    payload = {
        "version": 1,
        "book_id": data["book_id"],
        "chapter_id": chapter_id,
        "chapter_text": data.pop("text"),
        "source_sha256": data["source_sha256"],
        "script_sha256": data["script_sha256"],
        "memory_sha256": data["memory_sha256"],
        "characters": data["characters"],
        "approved_summaries": data["chapter_summaries"],
        "blocks": data["blocks"],
    }
    _dump(out, payload)
    return {"exported": str(out), "book_id": payload["book_id"],
            "chapter_id": chapter_id, "status": "ready"}


def proposal(wd, source, state_path):
    wd = Path(wd).resolve()
    with _book_lock(wd):
        return _proposal_locked(wd, source, state_path)


def _proposal_locked(wd, source, state_path):
    """Validate deepseek_profile.mjs output and store an isolated review record."""
    wd, source, state_path = Path(wd).resolve(), Path(source).resolve(), Path(state_path).resolve()
    recover(wd)
    source_bytes = source.read_bytes()
    state_bytes = state_path.read_bytes()
    data = json.loads(source_bytes.decode("utf-8-sig"))
    state = json.loads(state_bytes.decode("utf-8-sig"))
    state_sha256 = hashlib.sha256(state_bytes).hexdigest()
    if hashlib.sha256(source_bytes).hexdigest() != state.get("job_hash"):
        raise ValueError("结果不属于此画像任务输入。")
    if state.get("status") != "complete_needs_review":
        raise ValueError("画像任务尚未完成审核准备状态。")
    memory = _load(wd / "book-memory.json")
    if (data.get("source_sha256") != _sha(wd / "text.txt")
            or data.get("script_sha256") != _sha(wd / "script.huaben.jsonl")
            or data.get("memory_sha256") != _sha(wd / "book-memory.json")):
        raise ValueError("书稿、话本或人物档案已改变，请重新导出任务。")
    if data.get("book_id") != memory.get("book_id") or state.get("book_id") != memory.get("book_id"):
        raise ValueError("人物档案不属于此书。")
    if not data.get("chapter_id") or state.get("chapter_id") != data["chapter_id"]:
        raise ValueError("画像结果章节 ID 不匹配。")
    state_claims_manifest = "manifest_attempt_id" in state or "manifest_mode" in state
    binding = _manifest_binding(wd, data["chapter_id"], source, state, state_sha256,
                                require_enabled=state_claims_manifest)
    if state_claims_manifest and (
            state.get("manifest_mode") is not True
            or not isinstance(state.get("manifest_attempt_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", state["manifest_attempt_id"])
            or binding is None
            or state["manifest_attempt_id"] != binding.get("attempt_id")):
        raise ValueError("画像任务的 manifest 声明缺失、格式无效或与账本 attempt 不匹配。")
    result = state.get("result")
    if (not isinstance(result, dict) or not isinstance(result.get("characters"), list)
            or not isinstance(result.get("summary"), str) or len(result["summary"]) > 300):
        raise ValueError("画像结果格式无效。")
    for item in result["characters"]:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                or not isinstance(item.get("aliases"), list)
                or not isinstance(item.get("description"), str)
                or not isinstance(item.get("evidence"), str)):
            raise ValueError("人物画像格式无效。")

    out = wd / "profile-review.json"
    if out.exists() and _load(out).get("status") == "needs_review":
        raise ValueError("已有未完成的画像审核，请先处理。")
    review = {
        "version": 1, "book_id": data["book_id"], "chapter_id": data["chapter_id"],
        "input_sha256": _sha(source), "source_sha256": data["source_sha256"],
        "script_sha256": data["script_sha256"], "memory_sha256": data["memory_sha256"],
        "status": "needs_review", "characters": result["characters"],
        "summary": result["summary"], "summary_exceeds_requested_limit": len(result["summary"]) > 150, "approved": {},
        "note": "机器画像仅供审核；不会自动写入人物档案或章节记忆。",
    }
    if binding is not None:
        if result["summary"] != _load(Path(binding["receipt_path"])).get("result", {}).get("summary"):
            raise ValueError("画像草稿摘要与账本收据不匹配。")
        review["manifest_binding"] = binding
    _dump(out, review)
    return {"review_file": str(out), "characters": len(result["characters"]), "status": "needs_review"}


def commit(wd, book, chapter_id, review_sha256, decisions):
    wd = Path(wd).resolve()
    with _book_lock(wd):
        return _commit_locked(wd, book, chapter_id, review_sha256, decisions)


def _commit_locked(wd, book, chapter_id, review_sha256, decisions):
    """Commit only explicitly approved character fields and the chapter summary.

    ``decisions`` must contain one decision per proposal index and a summary
    decision. Rejected candidates are recorded as decisions but never saved.
    """
    wd = Path(wd).resolve()
    recovered = recover(wd)
    memory_path, cast_path = wd / "book-memory.json", wd / "cast.json"
    review_path, txn_path = wd / "profile-review.json", wd / "state" / "profile-transaction.json"
    if not review_path.is_file() or _sha(review_path) != review_sha256:
        raise ValueError("待审画像已改变，请重新载入后审核。")
    review = _load(review_path)
    if review.get("status") != "needs_review":
        raise ValueError("画像审核已处理或状态无效。")
    if review.get("chapter_id") != chapter_id:
        raise ValueError("画像审核章节不匹配。")
    if book != review.get("book_id"):
        raise ValueError("画像审核书籍不匹配。")
    has_historical_binding = "manifest_binding" in review
    if has_historical_binding and not isinstance(review.get("manifest_binding"), dict):
        raise ValueError("待审画像的历史账本绑定格式无效，拒绝降级提交。")
    binding = _manifest_binding(wd, chapter_id, require_enabled=has_historical_binding)
    if binding is not None:
        if review.get("manifest_binding") != binding:
            raise ValueError("待审画像与当前账本收据绑定不匹配。")
        summary_decision = decisions.get("summary") if isinstance(decisions, dict) else None
        if not isinstance(summary_decision, dict) or summary_decision.get("accept") is not True:
            raise ValueError("画像账本需要明确接受摘要后才能提交。")
    if (not memory_path.is_file() or not (wd / "text.txt").is_file()
            or not (wd / "script.huaben.jsonl").is_file()
            or _sha(memory_path) != review.get("memory_sha256")
            or _sha(wd / "text.txt") != review.get("source_sha256")
            or _sha(wd / "script.huaben.jsonl") != review.get("script_sha256")):
        raise ValueError("书稿、话本或人物档案已改变，请重新导出任务。")
    memory = _load(memory_path)
    if memory.get("book_id") != review.get("book_id"):
        raise ValueError("画像审核不属于此书。")
    if not isinstance(decisions, dict) or not isinstance(decisions.get("characters"), list):
        raise ValueError("画像审核决策格式无效。")
    candidates = review.get("characters", [])
    submitted = decisions["characters"]
    if len(submitted) != len(candidates):
        raise ValueError("每个人物候选都必须明确接受或拒绝。")
    by_index = {}
    for item in submitted:
        if not isinstance(item, dict) or type(item.get("index")) is not int:
            raise ValueError("人物审核项格式无效。")
        index = item["index"]
        if index in by_index or not 0 <= index < len(candidates):
            raise ValueError("人物审核序号重复或无效。")
        if type(item.get("accept")) is not bool:
            raise ValueError("每个人物候选都必须明确接受或拒绝。")
        by_index[index] = item
    if set(by_index) != set(range(len(candidates))):
        raise ValueError("每个人物候选都必须明确接受或拒绝。")
    summary = decisions.get("summary")
    if not isinstance(summary, dict) or type(summary.get("accept")) is not bool:
        raise ValueError("章节摘要必须明确接受或拒绝。")

    import book_memory
    new_memory = json.loads(json.dumps(memory, ensure_ascii=False))
    voice_root = (annotation_job.ROOT / "source" / "src" / "huashuo" / "voices" / "zh")
    applied = 0
    for index in range(len(candidates)):
        item = by_index[index]
        if not item["accept"]:
            continue
        name = item.get("name")
        target_name = item.get("target_name")
        aliases = item.get("aliases")
        description = item.get("description")
        voice = item.get("voice", "")
        if (not isinstance(name, str) or not name.strip() or len(name.strip()) > 60
                or (target_name is not None and (not isinstance(target_name, str) or len(target_name) > 60))
                or not isinstance(aliases, list) or len(aliases) > 30
                or any(not isinstance(alias, str) or not alias.strip() or len(alias.strip()) > 60 for alias in aliases)
                or not isinstance(description, str) or len(description) > 300
                or not isinstance(voice, str) or len(voice) > 200):
            raise ValueError("已接受人物的姓名、别名、描述或声音字段无效。")
        name = name.strip()
        if target_name is None:
            # Older callers used an exact candidate-name match to update an
            # existing entry. Preserve that behavior; a different name must
            # now be mapped explicitly with target_name.
            target_name = name if name in new_memory["characters"] else ""
        target_name = target_name.strip()
        aliases = [alias.strip() for alias in aliases]
        if target_name:
            if target_name not in new_memory["characters"]:
                raise ValueError(f"目标人物不存在：{target_name}")
            canonical_name = target_name
            existing = new_memory["characters"][canonical_name]
        else:
            if name in new_memory["characters"]:
                raise ValueError(f"人物“{name}”已存在；请选择该人物作为目标或使用新名称。")
            canonical_name = name
            existing = None
        if voice:
            voice_id = voice.removeprefix("library:zh/")
            if (not re.fullmatch(r"[A-Za-z0-9_-]+", voice_id)
                    or not (voice_root / (voice_id + ".json")).is_file()):
                raise ValueError(f"声音不存在或格式无效：{voice}")
        elif existing:
            voice = existing.get("voice", "")
        if not voice:
            raise ValueError(f"人物“{canonical_name}”必须明确选择有效声音。")
        profile = dict(existing or {})
        profile.update({"aliases": aliases, "description": description, "voice": voice})
        new_memory["characters"][canonical_name] = profile
        applied += 1
    book_memory.validate(new_memory)
    if summary["accept"]:
        text = summary.get("text")
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > 300:
            raise ValueError("已接受摘要必须为 1–300 字。")
        new_memory.setdefault("chapter_summaries", [])
        if any(row.get("chapter_id") == chapter_id for row in new_memory["chapter_summaries"]):
            raise ValueError("该章节已有摘要，请先处理现有摘要。")
        new_memory["chapter_summaries"].append({"chapter_id": chapter_id, "summary": text.strip(),
                                                "source": "manual", "status": "approved"})

    cast = _load(cast_path)
    new_cast = json.loads(json.dumps(cast, ensure_ascii=False))
    new_cast["characters"] = json.loads(json.dumps(new_memory["characters"], ensure_ascii=False))
    backup = wd / "profile-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup.mkdir(parents=True, exist_ok=False)
    for path in (memory_path, cast_path, review_path):
        shutil.copy2(path, backup / path.name)
    txn_path.parent.mkdir(parents=True, exist_ok=True)
    _dump(txn_path, {"status": "applying", "backup": str(backup), "review_sha256": review_sha256})
    try:
        _dump(memory_path, new_memory)
        _dump(cast_path, new_cast)
        review["status"] = "committed"
        review["original_review_sha256"] = review_sha256
        review["committed_memory_sha256"] = _sha(memory_path)
        if binding is not None:
            review["committed_summary_sha256"] = hashlib.sha256(
                next((row["summary"] for row in new_memory.get("chapter_summaries", [])
                      if row.get("chapter_id") == chapter_id), "").encode("utf-8")
            ).hexdigest()
            review["committed_manifest_binding"] = binding
        _dump(review_path, review)
        committed_review_hash = _sha(review_path)
        _dump(txn_path, {"status": "committed", "backup": str(backup), "review_sha256": review_sha256,
                         "committed_review_sha256": committed_review_hash,
                         "memory_sha256": _sha(memory_path), "cast_sha256": _sha(cast_path),
                         "committed_summary_sha256": review.get("committed_summary_sha256"),
                         "manifest_binding": binding})
    except Exception:
        recover(wd)
        raise
    return {"committed_characters": applied, "summary_approved": bool(summary["accept"]),
            "backup": str(backup), "recovered_previous_transaction": recovered}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("export")
    p.add_argument("workdir", type=Path); p.add_argument("output", type=Path); p.add_argument("chapter_id")
    p = sub.add_parser("proposal")
    p.add_argument("workdir", type=Path); p.add_argument("input", type=Path); p.add_argument("state", type=Path)
    p = sub.add_parser("recover")
    p.add_argument("workdir", type=Path)
    args = parser.parse_args()
    try:
        result = (export(args.workdir, args.output, args.chapter_id) if args.cmd == "export"
                  else proposal(args.workdir, args.input, args.state) if args.cmd == "proposal"
                  else {"recovered": recover(args.workdir)})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(2, "error: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
