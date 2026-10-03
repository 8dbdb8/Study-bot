import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from studybot.sg_glossary import (
    GLOSSARY_PATH,
    GlossaryDataError,
    GlossaryEntry,
    glossary_entry_key,
    init_sg_glossary_rating_table,
    get_sg_glossary_ratings,
    load_glossary,
    save_sg_glossary_rating,
    search_glossary,
    split_text,
)


class GlossaryDataTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "sg_glossary.json"

    def write_json(self, payload):
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_accepts_metadata_wrapped_and_terms_only_data(self):
        self.write_json({
            "metadata": {"source": "example"},
            "entries": [
                {"term": "認証", "meaning": "本人の確認", "category": "安全"},
                {"term": "暗号", "meaning": "", "source_url": "https://example.com"},
            ],
        })
        entries = load_glossary(self.path)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].meaning, "本人の確認")
        self.assertEqual(entries[1].meaning, "")
        self.assertEqual(entries[1].category, "")

    def test_accepts_plain_list_and_rejects_missing_term(self):
        self.write_json([{"term": " 用語 ", "meaning": " 意味 "}])
        self.assertEqual(load_glossary(self.path)[0].term, "用語")
        self.write_json([{"term": ""}])
        with self.assertRaises(GlossaryDataError):
            load_glossary(self.path)

    def test_searches_full_width_case_and_definition(self):
        entries = [
            GlossaryEntry("Access Control", "アクセスを制限する", "安全"),
            GlossaryEntry("暗号", "秘密を守る", "技術"),
        ]
        self.assertEqual(search_glossary(entries, "ＡＣＣＥＳＳ")[0], entries[0])
        self.assertEqual(search_glossary(entries, "秘密 技術"), [entries[1]])
        self.assertEqual(search_glossary(entries, ""), entries)

    def test_searches_syllabus_subcategory(self):
        entry = GlossaryEntry(
            "ISMS", "組織の情報を守る仕組み", "セキュリティ",
            "https://example.com", "情報セキュリティ管理",
        )
        self.assertEqual(search_glossary([entry], "情報セキュリティ管理"), [entry])

    def test_splitting_preserves_long_meanings(self):
        text = "あ" * 301 + "\n" + "い" * 210
        self.assertEqual("".join(split_text(text, 100)), text)

    def test_bundled_syllabus_glossary_has_complete_cards(self):
        entries = load_glossary(GLOSSARY_PATH)
        self.assertEqual(len(entries), 829)
        self.assertEqual(len({entry.term for entry in entries}), 810)
        self.assertEqual(len({entry.category for entry in entries}), 11)
        self.assertTrue(all(entry.meaning and entry.source_url for entry in entries))
        self.assertTrue(all("�" not in entry.term + entry.meaning for entry in entries))

    def test_self_ratings_persist_per_user_and_distinguish_same_term(self):
        db_path = Path(self.temp_dir.name) / "ratings.db"
        with closing(sqlite3.connect(db_path)) as conn:
            init_sg_glossary_rating_table(conn.cursor())
            init_sg_glossary_rating_table(conn.cursor())
            conn.commit()

        first = GlossaryEntry("共通用語", "意味A", "セキュリティ")
        second = GlossaryEntry("共通用語", "意味B", "法務")
        entries = [first, second]
        self.assertNotEqual(glossary_entry_key(first), glossary_entry_key(second))
        self.assertEqual(get_sg_glossary_ratings(db_path, 123, entries), {})

        save_sg_glossary_rating(db_path, 123, first, "できた")
        save_sg_glossary_rating(db_path, 123, second, "微妙")
        save_sg_glossary_rating(db_path, 456, first, "できなかった")
        self.assertEqual(get_sg_glossary_ratings(db_path, 123, entries), {
            glossary_entry_key(first): "できた",
            glossary_entry_key(second): "微妙",
        })
        self.assertEqual(get_sg_glossary_ratings(db_path, 456, entries), {
            glossary_entry_key(first): "できなかった",
        })

        save_sg_glossary_rating(db_path, 123, first, "まだ要復習")
        self.assertEqual(get_sg_glossary_ratings(db_path, 123, entries), {
            glossary_entry_key(first): "まだ要復習",
            glossary_entry_key(second): "微妙",
        })



if __name__ == "__main__":
    unittest.main()
