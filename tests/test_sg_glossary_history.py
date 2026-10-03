import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path

from sg_glossary import GlossaryEntry
from sg_glossary_history import (
    get_glossary_session,
    init_glossary_history_db,
    record_glossary_card,
    set_glossary_summary_message_id,
)


class GlossaryHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "data" / "sg_glossary_history.db"
        self.first = GlossaryEntry("認証", "本人を確かめる", "セキュリティ")
        self.second = GlossaryEntry("暗号", "情報を守る", "セキュリティ")

    def record(self, session_id, entry, rating, *, user_id=123, channel_id=20):
        return record_glossary_card(
            self.db_path, session_id, 1, channel_id, user_id, entry, rating,
        )

    def test_creates_separate_database_and_counts_distinct_cards(self):
        self.assertFalse(self.db_path.exists())
        init_glossary_history_db(self.db_path)
        init_glossary_history_db(self.db_path)
        self.assertTrue(self.db_path.exists())

        first = self.record("one", self.first, "できた")
        self.assertEqual(first["total"], 1)
        self.assertEqual(first["counts"], {
            "できた": 1,
            "できなかった": 0,
            "まだ要復習": 0,
            "微妙": 0,
        })
        self.assertEqual(first["session_id"], "one")
        self.assertEqual(first["user_id"], 123)
        self.assertIsNone(first["summary_message_id"])
        self.assertIsNotNone(datetime.fromisoformat(first["started_at"]).tzinfo)

        second = self.record("one", self.second, "できなかった")
        self.assertEqual(second["total"], 2)
        self.assertEqual(second["counts"]["できた"], 1)
        self.assertEqual(second["counts"]["できなかった"], 1)

        corrected = self.record("one", self.first, "微妙")
        self.assertEqual(corrected["total"], 2)
        self.assertEqual(corrected["counts"], {
            "できた": 0,
            "できなかった": 1,
            "まだ要復習": 0,
            "微妙": 1,
        })
        self.assertEqual(get_glossary_session(self.db_path, "one"), corrected)

        with closing(sqlite3.connect(self.db_path)) as conn:
            events = conn.execute(
                "SELECT term, rating FROM glossary_rating_events "
                "WHERE session_id = ? ORDER BY id", ("one",),
            ).fetchall()
            cards = conn.execute(
                "SELECT COUNT(*) FROM glossary_card_ratings WHERE session_id = ?",
                ("one",),
            ).fetchone()[0]
        self.assertEqual(events, [
            ("認証", "できた"),
            ("暗号", "できなかった"),
            ("認証", "微妙"),
        ])
        self.assertEqual(cards, 2)

    def test_sessions_and_users_are_independent_and_identity_is_checked(self):
        self.record("first", self.first, "できた")
        self.record("second", self.first, "まだ要復習")
        self.record("third", self.first, "できなかった", user_id=456)

        self.assertEqual(get_glossary_session(self.db_path, "first")["counts"]["できた"], 1)
        self.assertEqual(get_glossary_session(self.db_path, "second")["counts"]["まだ要復習"], 1)
        self.assertEqual(get_glossary_session(self.db_path, "third")["counts"]["できなかった"], 1)
        with self.assertRaisesRegex(ValueError, "利用者またはチャンネル"):
            self.record("first", self.second, "微妙", user_id=456)
        with self.assertRaisesRegex(ValueError, "利用者またはチャンネル"):
            self.record("first", self.second, "微妙", channel_id=21)
        self.assertEqual(get_glossary_session(self.db_path, "first")["total"], 1)

    def test_summary_message_id_persists_and_missing_session_is_rejected(self):
        self.record("one", self.first, "できた")
        set_glossary_summary_message_id(self.db_path, "one", 98765)
        self.assertEqual(
            get_glossary_session(self.db_path, "one")["summary_message_id"],
            98765,
        )
        with self.assertRaisesRegex(ValueError, "見つかりません"):
            set_glossary_summary_message_id(self.db_path, "missing", 123)
        with self.assertRaisesRegex(ValueError, "見つかりません"):
            get_glossary_session(self.db_path, "missing")

    def test_invalid_rating_has_no_record(self):
        with self.assertRaises(ValueError):
            self.record("one", self.first, "未定")
        self.assertFalse(self.db_path.exists())


if __name__ == "__main__":
    unittest.main()
