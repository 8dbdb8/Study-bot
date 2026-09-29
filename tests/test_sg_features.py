import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot
from sg_features import (
    add_sg_mistake,
    get_sg_b_summary,
    get_sg_category_progress,
    get_sg_mistakes,
    get_sg_plan_status,
    parse_correct_count,
    record_sg_mistake_attempt,
    save_sg_b_practice,
    save_sg_plan,
    score_from_counts,
)


class SGFeatureDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = str(Path(self.temp_dir.name) / "study.db")
        self.db_patch = patch.object(bot, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        bot.init_db()
        self.next_message_id = 100

    def save_log(self, content, analysis, study_date):
        self.next_message_id += 1
        created_at = datetime.combine(
            study_date, datetime.min.time(), bot.JST
        )
        message = SimpleNamespace(
            id=self.next_message_id,
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2),
            author=SimpleNamespace(id=123, display_name="test"),
            created_at=created_at,
            content=content,
        )
        bot.save_study_log(message)
        bot.save_study_analysis(message, analysis)
        return message

    def test_mistake_review_schedule_and_user_isolation(self):
        start = date(2026, 9, 29)
        mistake_id, due = add_sg_mistake(
            self.db_path, 123, "情報セキュリティ",
            "https://example.com/q/1", "選択肢を読み違えた",
            "条件を確認", today=start,
        )
        self.assertEqual(due, start + timedelta(days=1))
        self.assertEqual(get_sg_mistakes(self.db_path, 123, start), [])
        self.assertEqual(
            len(get_sg_mistakes(self.db_path, 123, due)), 1
        )
        with self.assertRaises(ValueError):
            record_sg_mistake_attempt(
                self.db_path, 999, mistake_id, "correct", due
            )

        first = record_sg_mistake_attempt(
            self.db_path, 123, mistake_id, "correct", due
        )
        self.assertEqual(
            first["next_review_on"], due + timedelta(days=3)
        )
        second = record_sg_mistake_attempt(
            self.db_path, 123, mistake_id, "wrong",
            first["next_review_on"],
        )
        self.assertEqual(second["streak"], 0)
        self.assertEqual(
            second["next_review_on"],
            first["next_review_on"] + timedelta(days=1),
        )
        third = record_sg_mistake_attempt(
            self.db_path, 123, mistake_id, "correct",
            second["next_review_on"],
        )
        fourth = record_sg_mistake_attempt(
            self.db_path, 123, mistake_id, "correct",
            third["next_review_on"],
        )
        self.assertEqual(
            fourth["next_review_on"],
            third["next_review_on"] + timedelta(days=7),
        )
        final = record_sg_mistake_attempt(
            self.db_path, 123, mistake_id, "correct",
            fourth["next_review_on"],
        )
        self.assertTrue(final["completed"])
        self.assertEqual(
            get_sg_mistakes(self.db_path, 123, due_only=False), []
        )

    def test_fourteen_category_progress_and_separate_b_results(self):
        first_day = date(2026, 9, 28)
        second_day = date(2026, 9, 29)
        self.save_log(
            "A 10問", bot.build_structured_sg_analysis(
                "情報セキュリティ", 10, 40.0
            ), first_day,
        )
        self.save_log(
            "A 5問", bot.build_structured_sg_analysis(
                "情報セキュリティ", 5, 60.0
            ), second_day,
        )
        self.save_log(
            "旧ログ", {
                "qualification": "SG", "activity": "過去問道場",
                "questions": 4, "correct_answers": 2,
                "score_percent": 50.0,
                "category_results": [{
                    "major_category": "テクノロジ系",
                    "category": "セキュリティ", "questions": 4,
                    "correct_answers": 2, "score_percent": 50.0,
                }],
            }, first_day,
        )
        b_analysis = bot.build_structured_sg_b_analysis(
            "リスクアセスメント", 3, 2,
            "残余リスクを見落とした", "表を見直す",
        )
        b_message = self.save_log("B 3問", b_analysis, second_day)
        save_sg_b_practice(
            self.db_path, b_message.id, 123, "リスクアセスメント",
            3, 2, "残余リスクを見落とした", "表を見直す",
            second_day.isoformat(),
        )

        progress, unclassified = get_sg_category_progress(
            self.db_path, 123
        )
        self.assertEqual(len(progress), 14)
        self.assertEqual(progress[0]["questions"], 15)
        self.assertEqual(progress[0]["latest_score"], 60.0)
        self.assertEqual(
            progress[0]["last_study_date"], second_day.isoformat()
        )
        self.assertEqual(progress[1]["questions"], 0)
        self.assertEqual(unclassified, 4)

        b_summary = get_sg_b_summary(self.db_path, 123)
        self.assertEqual(b_summary["questions"], 3)
        self.assertEqual(b_summary["correct_answers"], 2)
        self.assertEqual(
            b_summary["recent_sessions"][0][3],
            "残余リスクを見落とした",
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            section = conn.execute(
                "SELECT exam_section FROM study_log_analysis "
                "WHERE message_id = ?", (b_message.id,)
            ).fetchone()[0]
        self.assertEqual(section, "B")

        bot.delete_study_log_data(b_message.id)
        self.assertEqual(
            get_sg_b_summary(self.db_path, 123)["questions"], 0
        )

    def test_plan_carries_a_capped_shortfall(self):
        start = date(2026, 9, 28)
        save_sg_plan(
            self.db_path, 123, 3, 30,
            "2026-09-28T00:00:00+09:00", today=start,
        )
        self.save_log(
            "A 18問", bot.build_structured_sg_analysis(
                "情報セキュリティ", 18, 50.0
            ), start + timedelta(days=1),
        )
        second_week = get_sg_plan_status(
            self.db_path, 123, today=start + timedelta(days=8)
        )
        self.assertEqual(second_week["rows"][0]["actual"], 18)
        self.assertEqual(second_week["rows"][1]["target"], 42)

        self.save_log(
            "B 10問", bot.build_structured_sg_b_analysis(
                "委託先管理", 10, 7, "契約条件を誤読"
            ), start + timedelta(days=8),
        )
        third_week = get_sg_plan_status(
            self.db_path, 123, today=start + timedelta(days=15)
        )
        self.assertEqual(third_week["rows"][1]["actual"], 10)
        self.assertEqual(third_week["rows"][2]["target"], 45)

    def test_correct_count_accepts_zero_and_full_width(self):
        self.assertEqual(parse_correct_count("０", 3), 0)
        self.assertEqual(parse_correct_count(0, 3), 0)
        self.assertEqual(score_from_counts(2, 3), 66.7)
        with self.assertRaises(ValueError):
            parse_correct_count("4", 3)

    def test_plan_starts_on_creation_day(self):
        start = date(2026, 9, 29)
        save_sg_plan(
            self.db_path, 123, 2, 30,
            "2026-09-29T00:00:00+09:00", today=start,
        )
        status = get_sg_plan_status(
            self.db_path, 123, today=start
        )
        self.assertEqual(status["start_on"], start)
        self.assertEqual(
            status["rows"][0]["end_on"],
            start + timedelta(days=6),
        )

    def test_existing_analysis_table_gets_exam_section_column(self):
        old_db_path = str(Path(self.temp_dir.name) / "old-study.db")
        with closing(sqlite3.connect(old_db_path)) as conn:
            conn.execute("""
                CREATE TABLE study_log_analysis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    qualification TEXT,
                    activity TEXT,
                    questions INTEGER,
                    correct_answers INTEGER,
                    score_percent REAL,
                    weak_points TEXT,
                    notes TEXT,
                    analysis_warnings TEXT,
                    analyzed_at TEXT NOT NULL,
                    reply_message_id INTEGER
                )
            """)
            conn.commit()

        with patch.object(bot, "DB_PATH", old_db_path):
            bot.init_db()

        with closing(sqlite3.connect(old_db_path)) as conn:
            columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(study_log_analysis)"
                )
            }
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("exam_section", columns)
        self.assertTrue({
            "sg_mistakes", "sg_mistake_attempts",
            "sg_b_practice", "sg_plans",
        }.issubset(tables))


if __name__ == "__main__":
    unittest.main()
