import hashlib
import json
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest

from docx import Document

import workbench_book_import as importer


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class BookImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="book-import-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "private"
        self.source_dir = Path(self.temp.name) / "source"
        self.source_dir.mkdir()
        self.service = importer.BookImporter(self.root)

    def source(self, name: str, raw: bytes) -> Path:
        path = self.source_dir / name
        path.write_bytes(raw)
        return path

    def test_empty_list_books_does_not_create_root_or_lock(self):
        self.assertEqual(self.service.list_books(), [])
        self.assertFalse(self.root.exists())

    def test_default_preview_title_fits_commit_limit(self):
        name = "A" * 130 + ".txt"
        preview = self.service.preview(self.source(name, "第一章\n正文。\n".encode()), name)
        self.assertEqual(len(preview["title"]), 120)
        entry = self.service.commit(preview["preview_id"], preview["title"])
        self.assertEqual(entry["display_name"], preview["title"])

    def test_preview_commit_query_dialogue_and_duplicate_reuses_original_rules(self):
        text = "前言内容。\n第一章 夜行\n林舟说：“别怕。”她回答：「我在。」\n第二章 天明\n天亮了。\n"
        source = self.source("Book.txt", text.encode("utf-8"))
        preview = self.service.preview(source, "Book.txt")
        self.assertEqual(preview["source_sha256"], sha(text.encode("utf-8")))
        self.assertEqual([row["title"] for row in preview["chapters"]], ["前言", "第一章 夜行", "第二章 天明"])
        self.assertTrue(all(len(row["excerpt"]) <= 160 for row in preview["chapters"]))

        entry = self.service.commit(preview["preview_id"], "My Book")
        self.assertIn("index_sha256", entry)
        self.assertEqual([row["chapter_id"] for row in entry["chapters"]], ["c001", "c002", "c003"])
        listing = self.service.query("list", entry["book_id"])
        self.assertEqual(listing["chapters"], [row["chapter_id"] for row in entry["chapters"]])
        first = self.service.query("prepare", entry["book_id"], "c002")
        self.assertEqual(first["payload"]["chapter_id"], "c002")
        self.assertEqual(first["payload"]["blocks"][0]["type"], "chapter")
        self.assertEqual(first["payload"]["blocks"][0]["text"], "第一章 夜行")
        self.assertGreaterEqual(sum(row["type"] == "dialogue" for row in first["payload"]["blocks"]), 2)
        self.assertTrue(all(row.get("speaker") == "unknown" for row in first["payload"]["blocks"] if row["type"] == "dialogue"))
        self.assertEqual(first["payload_sha256"], sha(json.dumps(first["payload"], ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")))
        self.assertIs(first["human_reviewed"], False)

        reused = self.service.commit(preview["preview_id"], "Different title", [0])
        self.assertEqual(reused, entry)
        self.assertEqual(len(self.service.list_books()), 1)

    def test_tight_chinese_headings_are_recognized_and_emoji_offsets_round_trip(self):
        text = "第一章雨夜\n星⭐来了。“你好吗？”\n第二章雨停\n雨后🌧️安静。\n"
        preview = self.service.preview(self.source("Tight.txt", text.encode()), "Tight.txt")
        self.assertEqual([row["title"] for row in preview["chapters"]], ["第一章雨夜", "第二章雨停"])
        entry = self.service.commit(preview["preview_id"], "Tight")
        first = self.service.query("prepare", entry["book_id"], "c001")
        second = self.service.query("prepare", entry["book_id"], "c002")
        self.assertEqual(first["payload"]["blocks"][0]["text"], "第一章雨夜")
        self.assertEqual(second["payload"]["blocks"][0]["text"], "第二章雨停")
        self.assertIn("⭐", first["payload"]["text"])
        self.assertIn("🌧️", second["payload"]["text"])
        self.assertEqual("".join(self.service.query("prepare", entry["book_id"], chapter["chapter_id"])
            ["payload"]["text"] for chapter in entry["chapters"]), text)

    def test_manual_boundaries_require_unique_line_starts_and_preserve_prefix(self):
        text = "前言一⭐\n补充小节\n正文🌧️\n最后一节\n结束\n"
        preview = self.service.preview(self.source("Manual.txt", text.encode()), "Manual.txt")
        matches = self.service.find_heading(preview["preview_id"], "补充小节")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["title"], "补充小节")
        with self.assertRaises(ValueError):
            self.service.commit(preview["preview_id"], "Manual", [2, 2])
        entry = self.service.commit(preview["preview_id"], "Manual", [matches[0]["start"]])
        self.assertEqual([chapter["title"] for chapter in entry["chapters"]], ["前言", "补充小节"])

    def test_utf16_and_gb18030_keep_original_raw_source_hash(self):
        cases = [
            ("utf16.txt", "第一章\n你好。\n".encode("utf-16"), "utf-16"),
            ("gb.txt", "第一章\n你好。\n".encode("gb18030"), "gb18030"),
        ]
        for name, raw, expected_encoding in cases:
            with self.subTest(name=name):
                preview = self.service.preview(self.source(name, raw), name)
                self.assertEqual(preview["encoding"], expected_encoding)
                self.assertEqual(preview["source_sha256"], sha(raw))
                entry = self.service.commit(preview["preview_id"], Path(name).stem)
                self.assertEqual(entry["source_sha256"], sha(raw))
                self.assertEqual(self.service.query("list", entry["book_id"])["book_id"], entry["book_id"])

    def test_docx_is_parsed_and_query_rejects_changed_archived_source(self):
        path = self.source_dir / "Novel.docx"
        document = Document()
        document.add_paragraph("第一章 雨夜")
        document.add_paragraph("“快走。”")
        document.add_paragraph("第二章 天明")
        document.add_paragraph("天亮了。")
        document.save(path)
        preview = self.service.preview(path, path.name)
        self.assertEqual(len(preview["chapters"]), 2)
        self.assertTrue(any("页眉页脚" in warning for warning in preview["warnings"]))
        entry = self.service.commit(preview["preview_id"], "Novel")
        prepared = self.service.query("prepare", entry["book_id"], "c001")
        self.assertEqual(sum(row["type"] == "dialogue" for row in prepared["payload"]["blocks"]), 1)
        archived = self.root / entry["import_dir"] / "raw.docx"
        archived.write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            self.service.query("list", entry["book_id"])

    def test_missing_archived_raw_fails_closed(self):
        raw = "第一章\n正文。\n".encode()
        preview = self.service.preview(self.source("Missing.txt", raw), "Missing.txt")
        entry = self.service.commit(preview["preview_id"], "Missing")
        archived = self.root / entry["import_dir"] / "raw.txt"
        archived.unlink()
        with self.assertRaises(ValueError):
            self.service.query("list", entry["book_id"])

    def test_long_chapter_is_split_under_limit_without_losing_text(self):
        body = "春风吹过田野。" * 3_000
        raw = ("第一章\n" + body + "\n").encode("utf-8")
        preview = self.service.preview(self.source("Long.txt", raw), "Long.txt")
        self.assertTrue(any("拆分" in warning for warning in preview["warnings"]))
        entry = self.service.commit(preview["preview_id"], "Long")
        self.assertGreaterEqual(len(entry["chapters"]), 2)
        for chapter in entry["chapters"]:
            prepared = self.service.query("prepare", entry["book_id"], chapter["chapter_id"])
            self.assertLessEqual(len(prepared["payload"]["text"]), importer.MAX_CHAPTER_CHARS)
        self.assertEqual("".join(self.service.query("prepare", entry["book_id"], chapter["chapter_id"])
            ["payload"]["text"] for chapter in entry["chapters"]), "第一章\n" + body + "\n")

    def test_two_importer_instances_concurrently_register_same_source_once(self):
        raw = ("第一章\n“同一份原文。”\n" * 20).encode("utf-8")
        source = self.source("Concurrent.txt", raw)
        one = importer.BookImporter(self.root)
        two = importer.BookImporter(self.root)

        def import_once(service):
            preview = service.preview(source, "Concurrent.txt")
            return service.commit(preview["preview_id"], "Concurrent")

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(import_once, (one, two)))
        self.assertEqual(first, second)
        self.assertEqual(len(one.list_books()), 1)

    def test_merge_names_reuses_id_and_suffixes_collisions_without_overwrite(self):
        base = {"Same": "book-" + "1" * 20}
        def catalog_entry(number):
            book_id = "book-" + number * 20
            source_hash = number * 64
            import_dir = f"book-imports/{source_hash[:32]}"
            return {
                "book_id": book_id, "display_name": "Renamed" if number == "1" else "Same",
                "chapters": [{"chapter_id": "c001", "title": "x"}],
                "source_sha256": source_hash, "text_sha256": "b" * 64,
                "index_sha256": "c" * 64,
                "snapshot": {"book_id": book_id, "source_sha256": source_hash,
                    "text_sha256": "b" * 64, "script_sha256": "d" * 64, "cast_sha256": "e" * 64},
                "index_workspace": import_dir + "/index/workspace", "import_dir": import_dir,
            }
        entries = [
            catalog_entry("1"), catalog_entry("2"),
        ]
        merged = importer.merge_book_names(base, entries)
        self.assertEqual(merged["Same"], base["Same"])
        self.assertIn("Same · 222222", merged)
        self.assertEqual(merged["Same · 222222"], entries[1]["book_id"])


if __name__ == "__main__":
    unittest.main()
