import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from studybot import config
from studybot import database as database_module
from studybot import stats as stats_module
from studybot.charts import render_score_chart, render_study_time_chart
from studybot.features import sg as sg_feature
from studybot.features import time as time_feature


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class _TempDBCase(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = str(Path(temp_dir.name) / "study.db")
        db_patch = patch.object(config, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        database_module.init_db()
        self.next_id = 100

    def execute(self, sql, params):
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(sql, params)

    def add_session(self, user_id, study_date, seconds):
        self.execute("""
            INSERT INTO study_sessions (
                guild_id, user_id, username, study_date,
                start_time, end_time, duration_seconds
            ) VALUES (1, ?, 'test', ?, 's', 'e', ?)
        """, (user_id, study_date, seconds))

    def add_log(self, user_id, study_date, questions, correct, score,
                categories=()):
        self.next_id += 1
        self.execute("""
            INSERT INTO study_logs (
                guild_id, channel_id, message_id, user_id, username,
                study_date, content, created_at
            ) VALUES (1, 2, ?, ?, 'test', ?, 'log', 'c')
        """, (self.next_id, user_id, study_date))
        self.execute("""
            INSERT INTO study_log_analysis (
                message_id, user_id, qualification, exam_section,
                questions, correct_answers, score_percent, analyzed_at
            ) VALUES (?, ?, 'SG', 'A', ?, ?, ?, 'now')
        """, (self.next_id, user_id, questions, correct, score))
        for category, c_questions, c_correct, c_score in categories:
            self.execute("""
                INSERT INTO study_log_category_results (
                    message_id, major_category, category, questions,
                    correct_answers, score_percent
                ) VALUES (?, 'テクノロジ系', ?, ?, ?, ?)
            """, (self.next_id, category, c_questions, c_correct, c_score))


class ChartDataTests(_TempDBCase):
    def test_daily_study_seconds_in_range(self):
        self.add_session(1, "2026-10-01", 600)
        self.add_session(1, "2026-10-01", 1200)
        self.add_session(1, "2026-09-20", 999)
        self.add_session(2, "2026-10-01", 50)
        self.assertEqual(
            stats_module.get_daily_study_seconds(
                1, date(2026, 9, 25), date(2026, 10, 4)
            ),
            {date(2026, 10, 1): 1800},
        )

    def test_daily_scores_are_weighted_by_questions(self):
        self.add_log(1, "2026-10-02", 10, 5, 50.0)
        self.add_log(1, "2026-10-02", 30, None, 80.0)   # 正解数なし→正答率×問題数
        self.add_log(1, "2026-10-01", None, None, 40.0)  # 問題数なし→単純平均
        self.add_log(1, "2026-10-03", None, None, None)  # 正答率なし
        self.add_log(2, "2026-10-02", 10, 10, 100.0)

        self.assertEqual(stats_module.get_daily_scores(1), [
            (date(2026, 10, 1), 0, 40.0),
            (date(2026, 10, 2), 40, (5 * 100 + 80 * 30) / 40),
            (date(2026, 10, 3), 0, None),
        ])

    def test_daily_scores_by_category(self):
        self.add_log(1, "2026-10-02", 20, 12, 60.0, categories=[
            ("ネットワーク", 10, 4, 40.0), ("データベース", 10, 8, 80.0),
        ])
        self.assertEqual(
            stats_module.get_daily_scores(1, category="ネットワーク"),
            [(date(2026, 10, 2), 10, 40.0)],
        )
        self.assertEqual(stats_module.get_daily_scores(1, category="企業活動"), [])


class RenderTests(unittest.TestCase):
    def test_renders_png(self):
        end = date(2026, 10, 4)
        start = end - timedelta(days=27)
        png = render_study_time_chart({end: 3600}, start, end)
        self.assertTrue(png.startswith(PNG_SIGNATURE))

        png = render_score_chart(
            [(date(2026, 10, 1), 10, None), (date(2026, 10, 2), 20, 65.0)],
            "SG 正答率の推移（全体）",
        )
        self.assertTrue(png.startswith(PNG_SIGNATURE))


class _TypingContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Context:
    def __init__(self, user_id=1):
        self.author = SimpleNamespace(id=user_id)
        self.messages = []

    def typing(self):
        return _TypingContext()

    async def send(self, content=None, **kwargs):
        self.messages.append(dict(kwargs, content=content))


class ChartCommandTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    async def test_time_chart(self):
        ctx = _Context()
        await time_feature.time_chart.callback(ctx)
        self.assertIn("記録がありません", ctx.messages[0]["content"])

        today = datetime.now(config.JST).date()
        self.add_session(1, today.isoformat(), 3600)
        ctx = _Context()
        await time_feature.time_chart.callback(ctx, days=14)
        sent_file = ctx.messages[0]["file"]
        self.assertEqual(sent_file.filename, "study_time.png")
        self.assertTrue(sent_file.fp.read().startswith(PNG_SIGNATURE))

    async def test_sg_chart(self):
        ctx = _Context()
        await sg_feature.sg_chart.callback(ctx, category="存在しない")
        self.assertIn("候補から", ctx.messages[0]["content"])

        await sg_feature.sg_chart.callback(ctx)
        self.assertIn("まだありません", ctx.messages[1]["content"])

        self.add_log(1, "2026-10-02", 20, 12, 60.0, categories=[
            ("ネットワーク", 20, 12, 60.0),
        ])
        await sg_feature.sg_chart.callback(ctx, category="ネットワーク")
        self.assertEqual(ctx.messages[2]["file"].filename, "sg_score.png")


if __name__ == "__main__":
    unittest.main()
