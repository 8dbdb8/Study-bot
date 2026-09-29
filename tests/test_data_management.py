import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot
from data_management import (
    apply_edit,
    get_overview,
    get_record,
    list_records,
    prepare_edit,
    prepare_reset,
    reset_user_data,
)
from sg_features import add_sg_mistake, save_sg_b_practice, save_sg_plan


class DataManagementTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = str(Path(directory.name) / "study.db")
        db_patch = patch.object(bot, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        bot.init_db()

    def save_log(self, user_id, message_id, analysis):
        message = SimpleNamespace(
            id=message_id,
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2),
            author=SimpleNamespace(id=user_id, display_name="student"),
            created_at=datetime(2026, 9, 29, 20, 0, tzinfo=bot.JST),
            content="test log",
        )
        bot.save_study_log(message)
        bot.save_study_analysis(message, analysis)

    def edit(self, user_id, kind, record_id, field, raw_value):
        preview = prepare_edit(
            self.db_path, user_id, kind, record_id, field, raw_value,
        )
        apply_edit(
            self.db_path, user_id, kind, record_id, field,
            preview["value"], preview["record"],
        )

    def test_sg_log_correction_recalculates_totals_and_category(self):
        self.save_log(
            123, 101,
            bot.build_structured_sg_analysis("情報セキュリティ", 25, 40.0),
        )
        preview = prepare_edit(
            self.db_path, 123, "log", 101, "score_percent", "60.25",
        )
        self.assertEqual(preview["value"], 60.3)
        self.assertEqual(bot.get_study_status(123)["average_score"], 40.0)
        apply_edit(
            self.db_path, 123, "log", 101, "score_percent",
            preview["value"], preview["record"],
        )
        self.assertEqual(bot.get_study_status(123)["average_score"], 60.3)
        self.edit(123, "log", 101, "questions", "10")
        self.edit(123, "log", 101, "category", "ネットワーク")
        self.assertEqual(bot.get_study_status(123)["total_questions"], 10)
        progress, _ = bot.get_sg_category_progress(self.db_path, 123)
        self.assertEqual(
            next(item for item in progress if item["category"] == "ネットワーク")
            ["questions"], 10,
        )
        self.assertEqual(get_record(self.db_path, 123, "log", 101)
                         ["categories"][0]["major_category"], "テクノロジ系")

    def test_legacy_ambiguous_log_requires_category_first(self):
        analysis = bot.build_structured_sg_analysis("情報セキュリティ", 16, 50.0)
        analysis["exam_section"] = None
        analysis["category_results"] = [
            {"major_category": "テクノロジ系", "category": None,
             "questions": None, "correct_answers": None,
             "score_percent": 50.0},
            {"major_category": "マネジメント系", "category": None,
             "questions": None, "correct_answers": None,
             "score_percent": 50.0},
        ]
        self.save_log(123, 102, analysis)
        with self.assertRaisesRegex(ValueError, "先に正しい分野"):
            prepare_edit(self.db_path, 123, "log", 102, "questions", "20")
        self.edit(123, "log", 102, "category", "企業活動")
        self.edit(123, "log", 102, "questions", "20")
        record = get_record(self.db_path, 123, "log", 102)
        self.assertEqual(len(record["categories"]), 1)
        self.assertEqual(record["categories"][0]["category"], "企業活動")
        self.assertEqual(record["categories"][0]["questions"], 20)

    def test_b_edit_updates_both_tables_and_owner_isolation(self):
        self.save_log(
            123, 103,
            bot.build_structured_sg_b_analysis(
                "情報資産管理", 5, 3, "読み違い", "復習する",
            ),
        )
        save_sg_b_practice(
            self.db_path, 103, 123, "情報資産管理", 5, 3,
            "読み違い", "復習する", "2026-09-29",
        )
        self.assertIsNone(get_record(self.db_path, 999, "log", 103))
        with self.assertRaises(ValueError):
            prepare_edit(self.db_path, 999, "log", 103, "questions", "10")
        self.edit(123, "log", 103, "correct_answers", "4")
        self.edit(123, "log", 103, "category", "委託先管理")
        record = get_record(self.db_path, 123, "log", 103)
        self.assertEqual(record["score_percent"], 80.0)
        self.assertEqual(record["b"]["correct_answers"], 4)
        self.assertEqual(record["b"]["topic"], "委託先管理")
        self.assertEqual(record["categories"][0]["score_percent"], 80.0)

    def test_b_correction_requires_reason_when_it_creates_a_mistake(self):
        self.save_log(
            123, 108,
            bot.build_structured_sg_b_analysis("情報資産管理", 5, 5),
        )
        save_sg_b_practice(
            self.db_path, 108, 123, "情報資産管理", 5, 5,
            None, None, "2026-09-29",
        )
        with self.assertRaisesRegex(ValueError, "先に判断"):
            prepare_edit(self.db_path, 123, "log", 108, "correct_answers", "4")
        self.edit(123, "log", 108, "wrong_reason", "条件を読み違えた")
        self.edit(123, "log", 108, "correct_answers", "4")
        self.assertEqual(get_record(self.db_path, 123, "log", 108)
                         ["b"]["correct_answers"], 4)

    def test_session_mistake_and_plan_edits(self):
        start = datetime(2026, 9, 29, 18, 0, tzinfo=bot.JST)
        bot.save_completed_study_session(
            1, 123, "student", start, start + timedelta(minutes=30), 1800,
        )
        session_id = list_records(self.db_path, 123, "session")[0][0]["id"]
        self.edit(123, "session", session_id, "minutes", "45")
        session = get_record(self.db_path, 123, "session", session_id)
        self.assertEqual(session["duration_seconds"], 2700)
        self.assertEqual(
            datetime.fromisoformat(session["end_time"]),
            start + timedelta(minutes=45),
        )
        mistake_id, _ = add_sg_mistake(
            self.db_path, 123, "情報セキュリティ", "令和6年 問12",
            "読み違い", today=date(2026, 9, 29),
        )
        self.edit(123, "mistake", mistake_id, "category", "ネットワーク")
        self.assertEqual(get_record(self.db_path, 123, "mistake", mistake_id)
                         ["category"], "ネットワーク")
        plan_id = save_sg_plan(
            self.db_path, 123, 6, 30, start.isoformat(),
            today=date(2026, 9, 29),
        )
        self.edit(123, "plan", plan_id, "weekly_questions", "40")
        self.assertEqual(get_record(self.db_path, 123, "plan", plan_id)
                         ["weekly_questions"], 40)
        with self.assertRaises(ValueError):
            prepare_edit(self.db_path, 999, "plan", plan_id, "weeks", "2")

    def test_reset_is_scoped_and_confirmation_snapshot_is_required(self):
        self.save_log(
            123, 104,
            bot.build_structured_sg_analysis("情報セキュリティ", 10, 40.0),
        )
        self.save_log(
            999, 105,
            bot.build_structured_sg_analysis("ネットワーク", 8, 75.0),
        )
        ids = prepare_reset(self.db_path, 123, "sg_logs")
        self.assertEqual(bot.get_study_status(123)["log_count"], 1)
        self.save_log(
            123, 106,
            bot.build_structured_sg_analysis("企業活動", 5, 60.0),
        )
        with self.assertRaisesRegex(ValueError, "記録が変わりました"):
            reset_user_data(self.db_path, 123, "sg_logs", ids)
        self.assertEqual(bot.get_study_status(123)["log_count"], 2)
        count = reset_user_data(
            self.db_path, 123, "sg_logs",
            prepare_reset(self.db_path, 123, "sg_logs"),
        )
        self.assertEqual(count, 2)
        self.assertEqual(bot.get_study_status(123)["log_count"], 0)
        self.assertEqual(bot.get_study_status(999)["log_count"], 1)
        self.assertEqual(get_overview(self.db_path, 123)["logs"]["total"], 0)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM study_log_category_results "
                "WHERE message_id IN (104, 106)"
            ).fetchone()[0], 0)

    def test_active_session_blocks_time_reset(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("""
                INSERT INTO active_study_sessions
                    (guild_id, user_id, username, start_time)
                VALUES (1, 123, 'student', '2026-09-29T20:00:00+09:00')
            """)
            conn.commit()
        with self.assertRaisesRegex(ValueError, "通話勉強中"):
            prepare_reset(self.db_path, 123, "all")

    def test_reset_all_removes_only_owners_related_rows(self):
        for user_id, message_id in ((123, 109), (999, 110)):
            self.save_log(
                user_id, message_id,
                bot.build_structured_sg_b_analysis(
                    "情報資産管理", 5, 3, "読み違い",
                ),
            )
            save_sg_b_practice(
                self.db_path, message_id, user_id, "情報資産管理",
                5, 3, "読み違い", None, "2026-09-29",
            )
            start = datetime(2026, 9, 29, 18, 0, tzinfo=bot.JST)
            bot.save_completed_study_session(
                1, user_id, "student", start,
                start + timedelta(minutes=30), 1800,
            )
            add_sg_mistake(
                self.db_path, user_id, "情報セキュリティ",
                "令和6年 問12", "読み違い", today=date(2026, 9, 29),
            )
            save_sg_plan(
                self.db_path, user_id, 6, 30, start.isoformat(),
                today=date(2026, 9, 29),
            )
        ids = prepare_reset(self.db_path, 123, "all")
        self.assertEqual(reset_user_data(self.db_path, 123, "all", ids), 4)
        own = get_overview(self.db_path, 123)
        other = get_overview(self.db_path, 999)
        for key in ("logs", "sessions", "mistakes", "plans"):
            self.assertEqual(own[key]["total"], 0)
            self.assertEqual(other[key]["total"], 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM sg_b_practice WHERE user_id = 123"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM sg_b_practice WHERE user_id = 999"
            ).fetchone()[0], 1)

    def test_edit_rejects_stale_confirmation(self):
        self.save_log(
            123, 107,
            bot.build_structured_sg_analysis("情報セキュリティ", 10, 40.0),
        )
        preview = prepare_edit(
            self.db_path, 123, "log", 107, "score_percent", "50",
        )
        self.edit(123, "log", 107, "score_percent", "60")
        with self.assertRaisesRegex(ValueError, "記録が変更"):
            apply_edit(
                self.db_path, 123, "log", 107, "score_percent",
                preview["value"], preview["record"],
            )
        self.assertEqual(bot.get_study_status(123)["average_score"], 60.0)

    def test_data_command_and_initial_view_are_available(self):
        self.assertIsNotNone(bot.bot.tree.get_command("data"))
        view = bot.DataHomeView(123)
        self.assertEqual(
            {item.label for item in view.children},
            {"修正", "リセット", "そのまま"},
        )
        self.assertIn("保存データ一覧", bot._data_home_text(123))
        self.save_log(
            123, 111,
            bot.build_structured_sg_analysis("情報セキュリティ", 10, 40.0),
        )
        self.assertTrue(bot.DataKindView(123).children)
        self.assertTrue(bot.DataRecordListView(123, "log").children)
        self.assertTrue(bot.DataFieldView(123, "log", 111).children)
        self.assertTrue(bot.DataChoiceValueView(
            123, "log", 111, "category", ("情報セキュリティ",),
        ).children)
        self.assertTrue(bot.DataResetScopeView(123).children)


if __name__ == "__main__":
    unittest.main()
