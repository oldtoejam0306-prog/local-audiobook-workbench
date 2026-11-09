"""Read-only, static quality report for a Huashuo book work directory.

Machine checks validate stored metadata, chapter review digests, and audio file
presence/digests. They cannot judge pronunciation, prosody, or audio quality;
those remain explicitly pending listening validation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from collections import Counter

from render_queue import chapter_digest


def _load(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _rows(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()]


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate_report(workdir, book_id=None, voice_library_path=None, chapter_id=None,
                    detail_limit=100):
    """Return a per-chapter report without writing to the book or its workspace."""
    wd = Path(workdir).resolve()
    if type(detail_limit) is not int or detail_limit < 0:
        raise ValueError("detail_limit 必须是非负整数。")
    rows = _rows(wd / "script.huaben.jsonl")
    import_record = _load(wd.parent / "book.json", {})
    memory = _load(wd / "book-memory.json", {})
    characters = memory.get("characters", {})
    chapters = []
    current = None
    for row in rows:
        if row.get("type") == "chapter":
            current = {"id": row.get("id"), "title": row.get("text", ""), "rows": []}
            chapters.append(current)
        if current is not None:
            current["rows"].append(row)

    if book_id is None:
        book_id = memory.get("book_id")
    if chapter_id is not None and chapter_id not in {c["id"] for c in chapters}:
        raise ValueError("指定章节不存在。")
    voice_library = Path(voice_library_path).resolve() if voice_library_path is not None else None
    detected_chapter_count = len(chapters)
    candidate_count = import_record.get("chapter_count_candidates")
    structure_review = _load(wd / "state/chapter-review.json", {})
    front_matter_id = structure_review.get("front_matter_chapter")
    has_marked_front_matter = (structure_review.get("requires_review") is True and
                               isinstance(front_matter_id, str) and
                               front_matter_id in {chapter["id"] for chapter in chapters})
    matches_after_front_matter = (type(candidate_count) is int and
                                  has_marked_front_matter and
                                  candidate_count == detected_chapter_count - 1)
    count_differs = (type(candidate_count) is int and
                     candidate_count != detected_chapter_count and
                     not matches_after_front_matter)
    proofs = _load(wd / "state/chapter-reviews.json", {})
    queue = _load(wd / "state/render-queue.json", {"jobs": {}}).get("jobs", {})
    annotation = _load(wd / "annotation-review.json", {})
    profile = _load(wd / "profile-review.json", {})
    summaries = {item.get("chapter_id"): item for item in memory.get("chapter_summaries", [])
                 if isinstance(item, dict)}

    # Alias ambiguity is book-wide, so every affected chapter can surface it.
    alias_owners = {}
    for name, person in characters.items():
        for alias in [name, *person.get("aliases", [])]:
            if isinstance(alias, str) and alias.strip():
                alias_owners.setdefault(alias.strip().casefold(), set()).add(name)
    conflicting_names = set()
    for owners in alias_owners.values():
        if len(owners) > 1:
            conflicting_names.update(owners)

    output = []
    overall_counts = Counter()
    details_remaining = detail_limit
    for chapter in chapters:
        ident = chapter["id"]
        if chapter_id is not None and ident != chapter_id:
            continue
        issue_counts = Counter()
        issues = []

        def issue(code, message):
            nonlocal details_remaining
            issue_counts[code] += 1
            overall_counts[code] += 1
            if details_remaining > 0:
                issues.append({"code": code, "message": message})
                details_remaining -= 1

        expected_digest = chapter_digest(chapter["rows"], ident)
        proof = proofs.get(ident, {})
        reviewed = (proof.get("book_id") == book_id and
                    proof.get("script_digest") == expected_digest)
        if not reviewed:
            issue("chapter_not_reviewed", "缺少有效的本章审核凭证。")
        if count_differs:
            issue("chapter_count_candidate_mismatch",
                  f"话本识别到 {detected_chapter_count} 章，导入记录候选数为 {candidate_count}；两者均需人工核对，不能据此判定哪一个正确。")

        used_speakers = set()
        for row in chapter["rows"]:
            if row.get("type") == "dialogue":
                speaker = row.get("speaker")
                if not isinstance(speaker, str) or not speaker.strip():
                    issue("speaker_missing", f"对白 {row.get('id', '?')} 未填写有效说话人。")
                elif speaker.strip().casefold() == "unknown":
                    issue("unknown_speaker", f"对白 {row.get('id', '?')} 的说话人仍为 unknown。")
                elif speaker not in characters:
                    issue("speaker_not_in_cast", f"对白 {row.get('id', '?')} 的说话人“{speaker}”不在人物档案中。")
                else:
                    used_speakers.add(speaker)
        if annotation.get("status") == "needs_review" and annotation.get("chapter_id") in (None, ident):
            approved = annotation.get("approved", {})
            if set(approved) != set(annotation.get("decisions", {})):
                issue("annotation_pending", "存在尚未完成的机器标注审核。")
        if profile.get("status") == "needs_review" and profile.get("chapter_id") == ident:
            issue("profile_pending", "存在尚未完成的人物画像或摘要审核。")

        required = {"旁白", *used_speakers}
        for name in sorted(conflicting_names):
            issue("alias_conflict", f"人物“{name}”的名称或别名与其他人物冲突。")
        for name in sorted(required):
            person = memory.get("narrator", {}) if name == "旁白" else characters.get(name, {})
            voice = person.get("voice")
            if not voice:
                issue("missing_voice", f"人物“{name}”未配置音色。")
            elif voice_library is None:
                issue("voice_asset_unverified", f"人物“{name}”已配置音色 {voice}，但未提供音色库路径，无法验证对应文件。")
            elif not isinstance(voice, str) or not voice.startswith("library:zh/"):
                issue("voice_identifier_invalid", f"人物“{name}”的音色标识格式无法识别：{voice!r}。")
            else:
                voice_id = voice.removeprefix("library:zh/")
                voice_file = voice_library / (voice_id + ".json") if re.fullmatch(r"[A-Za-z0-9_-]+", voice_id) else None
                if voice_file is None or not voice_file.is_file():
                    issue("voice_asset_missing", f"人物“{name}”配置的音色文件不存在或标识无效：{voice}。")
        summary = summaries.get(ident)
        if not summary or summary.get("status") != "approved":
            issue("summary_unapproved", "本章缺少已批准的摘要。")

        job = queue.get(ident, {})
        audio_path = Path(job["output_path"]) if job.get("output_path") else None
        audio_missing = (job.get("status") != "complete" or audio_path is None or
                         not audio_path.is_file() or audio_path.stat().st_size == 0)
        if audio_missing:
            issue("audio_missing", "缺少已完成且非空的本章音频文件。")
        elif _sha(audio_path) != job.get("output_sha256"):
            issue("audio_hash_mismatch", "音频文件 SHA-256 与渲染记录不一致。")

        output.append({
            "chapter_id": ident,
            "title": chapter["title"],
            "machine_format_checks": {
                "status": "issues" if issue_counts else "clear",
                "issues": issues,
                "issue_counts": dict(sorted(issue_counts.items())),
                "issue_detail_truncated": sum(issue_counts.values()) > len(issues),
                "audio_file_present": not audio_missing,
                "chapter_review_digest_valid": reviewed,
            },
            "listening_validation": {
                "status": "pending",
                "required": ["发音准确性", "角色音色辨识度", "语速与停顿", "情绪自然度", "剪切与杂音"],
                "note": "静态机器检查不能替代人工试听；本报告不会把试听标记为通过。",
            },
        })

    return {"version": 1, "book_id": book_id, "workdir": str(wd),
            "scope": "read_only_static_checks",
            "overview": {
                "chapters_total": len(chapters),
                "chapters_returned": len(output),
                "issue_counts": dict(sorted(overall_counts.items())),
                "issue_total": sum(overall_counts.values()),
                "details_limit": detail_limit,
                "details_truncated": sum(overall_counts.values()) > len(
                    [issue for item in output for issue in item["machine_format_checks"]["issues"]]),
            },
            "chapter_count_comparison": {
                "script_detected": detected_chapter_count,
                "import_candidate": candidate_count,
                "front_matter_chapter": front_matter_id if has_marked_front_matter else None,
                "status": ("mismatch" if count_differs else
                           "match_with_front_matter_review" if matches_after_front_matter else
                           "match_or_unavailable"),
                "note": ("候选数与话本识别数不同；报告仅提示差异，不判定哪一个正确。"
                         if count_differs else
                         "候选数扣除已标记的一个前置章节后与话本章节数相符；前置章节仍待核对。"
                         if matches_after_front_matter else
                         "未发现数量差异；候选数缺失时不作比较。"),
            },
            "chapters": output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--book-id")
    parser.add_argument("--voice-library", type=Path)
    parser.add_argument("--chapter-id")
    parser.add_argument("--detail-limit", type=int, default=100)
    parser.add_argument("--include-private-details", action="store_true",
                        help="本机排障时显式输出路径、标识、章节标题和问题详情；请勿公开分享。")
    args = parser.parse_args()
    report = generate_report(args.workdir, args.book_id, args.voice_library,
                             args.chapter_id, args.detail_limit)
    if not args.include_private_details:
        report = {"version": report["version"], "scope": report["scope"],
                  "overview": report["overview"],
                  "chapter_count_comparison": {k: v for k, v in report["chapter_count_comparison"].items()
                                               if k != "front_matter_chapter"}}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
