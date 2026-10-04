import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import discord

from studybot import config
from studybot import database as database_module
from studybot.daily_digest import digest_datetime, is_day_off
from studybot.features import ai as ai_feature
from studybot.features import weekly_report as weekly_feature
from studybot.weekly_report import (
    get_weekly_report_candidates,
    mark_weekly_report_sent,
    set_weekly_report_enabled,
    week_start_of,
)


SUNDAY = date(2026, 10, 18)


class _TempDBCase(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = str(Path(temp_dir.name) / "study.db")
        db_patch = patch.object(config, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        database_module.init_db()

    def add_session(self, user_id, study_date, guild_id=10, seconds=600):
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("""
                    INSERT INTO study_sessions (
                        guild_id, user_id, username, study_date,
                        start_time, end_time, duration_seconds
                    ) VALUES (?, ?, 'test', ?, ?, ?, ?)
                """, (
                    guild_id, user_id, study_date,
                    f"{study_date}T10:00:00", f"{study_date}T11:00:00",
                    seconds,
                ))


class ScheduleTests(unittest.TestCase):
    def test_day_off_includes_weekends_and_holidays(self):
        self.assertFalse(is_day_off(date(2026, 10, 15)))   # 木
        self.assertTrue(is_day_off(date(2026, 10, 17)))    # 土
        self.assertTrue(is_day_off(date(2026, 10, 12)))    # スポーツの日
        self.assertTrue(is_day_off(date(2026, 11, 3)))     # 文化の日（火）

    def test_digest_datetime(self):
        weekday, holiday = datetime(1, 1, 1, 19).time(), datetime(1, 1, 1, 7).time()
        self.assertEqual(
            digest_datetime(date(2026, 10, 15), weekday, holiday, config.JST).hour,
            19,
        )
        self.assertEqual(
            digest_datetime(date(2026, 10, 12), weekday, holiday, config.JST).hour,
            7,
        )

    def test_weekly_report_due_only_on_sunday_night(self):
        def at(day, hour, minute=0):
            return datetime(2026, 10, day, hour, minute, tzinfo=config.JST)

        due = weekly_feature.is_weekly_report_due
        self.assertFalse(due(at(18, 20, 59)))
        self.assertTrue(due(at(18, 21, 0)))
        self.assertTrue(due(at(18, 23, 59)))
        self.assertFalse(due(at(17, 21, 0)))
        self.assertFalse(due(at(19, 21, 0)))


class WeeklyReportDataTests(_TempDBCase):
    def test_week_start(self):
        self.assertEqual(week_start_of(SUNDAY), date(2026, 10, 12))
        self.assertEqual(week_start_of(date(2026, 10, 12)), date(2026, 10, 12))

    def test_candidates_studied_this_week(self):
        self.add_session(1, "2026-10-12")
        self.add_session(2, "2026-10-11")             # 先週
        self.add_session(3, "2026-10-15", seconds=0)  # 0秒は数えない
        self.add_session(4, "2026-10-18")
        self.add_session(5, "2026-10-14")
        set_weekly_report_enabled(self.db_path, 5, False)
        mark_weekly_report_sent(self.db_path, 4, SUNDAY, 10, 20, 30)

        self.assertEqual(get_weekly_report_candidates(self.db_path, SUNDAY), [1])
        # 翌週は送信済みの記録に関係なく、その週の勉強で決まる
        self.add_session(4, "2026-10-19")
        self.assertEqual(
            get_weekly_report_candidates(self.db_path, date(2026, 10, 25)), [4]
        )


class _Channel:
    def __init__(self, guild):
        self.name = config.STUDY_LOG_CHANNEL_NAME
        self.id = 20
        self.guild = guild
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=900 + len(self.sent))


class _Guild:
    def __init__(self):
        self.id = 10
        self.channel = _Channel(self)
        self.text_channels = [self.channel]

    async def fetch_member(self, user_id):
        if user_id == 99:
            raise discord.NotFound(SimpleNamespace(status=404, reason=""), "")
        return SimpleNamespace(id=user_id)


class SendWeeklyReportTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    async def test_sends_once_per_week(self):
        guild = _Guild()
        fake_bot = SimpleNamespace(get_guild=lambda guild_id: guild)
        for user_id in (1, 2, 99):
            self.add_session(user_id, "2026-10-16")

        async def fake_report(user_id):
            if user_id == 2:
                return None
            return DATA, f"report {user_id}", None

        async def no_notion(*args):
            return None

        now = datetime(2026, 10, 18, 21, 0, tzinfo=config.JST)
        with patch.object(weekly_feature, "create_weekly_report", fake_report), \
                patch.object(weekly_feature, "save_weekly_report_to_notion", no_notion):
            self.assertEqual(await weekly_feature.send_weekly_reports(fake_bot, now), 1)
            self.assertEqual(await weekly_feature.send_weekly_reports(fake_bot, now), 0)

        sent = guild.channel.sent
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            sent[0]["content"], "<@1> 今週もおつかれさまでした。週間レポートです。"
        )
        self.assertEqual(sent[0]["embed"].description, "report 1")

    async def test_adds_notion_result_line(self):
        guild = _Guild()
        fake_bot = SimpleNamespace(get_guild=lambda guild_id: guild)
        self.add_session(1, "2026-10-16")
        calls = []

        async def fake_report(user_id):
            return DATA, "report", None

        async def fake_notion(user_id, data, answer, ai_error, today):
            calls.append((user_id, answer, today))
            return "📝 Notionにも保存しました：https://notion.so/x"

        now = datetime(2026, 10, 18, 21, 0, tzinfo=config.JST)
        with patch.object(weekly_feature, "create_weekly_report", fake_report), \
                patch.object(weekly_feature, "save_weekly_report_to_notion", fake_notion):
            await weekly_feature.send_weekly_reports(fake_bot, now)

        self.assertEqual(calls, [(1, "report", SUNDAY)])
        self.assertTrue(guild.channel.sent[0]["content"].endswith(
            "\n📝 Notionにも保存しました：https://notion.so/x"
        ))


DATA = {
    "start_date": "2026-10-12", "end_date": "2026-10-18",
    "total_seconds": 4 * 3600 + 5 * 60, "daily_text": "- 月曜日：…",
    "log_count": 3, "total_questions": 75, "score_text": "68.0%",
    "category_text": "記録なし", "review_text": "現在はなし",
}


class _TypingContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Context:
    def __init__(self):
        self.author = SimpleNamespace(id=1)
        self.messages = []

    def typing(self):
        return _TypingContext()

    async def send(self, content=None, **kwargs):
        self.messages.append(dict(kwargs, content=content))


class ReportEmbedTests(unittest.IsolatedAsyncioTestCase):
    def test_embed_fields_and_long_text(self):
        embed = ai_feature.build_weekly_report_embed(DATA, "あ" * 5000)
        self.assertLessEqual(len(embed.description), 4096)
        self.assertTrue(embed.description.endswith("（長いため省略）"))
        self.assertEqual(
            [(f.name, f.value) for f in embed.fields],
            [("期間", "2026-10-12 〜 2026-10-18"), ("勉強時間", "4時間05分"),
             ("問題数", "75問"), ("平均正答率", "68.0%")],
        )
        self.assertIn("週報を作成", ai_feature.build_weekly_report_prompt(DATA))

    async def test_falls_back_to_numbers_when_ai_fails(self):
        async def broken(prompt):
            raise RuntimeError("boom")

        with patch.object(ai_feature, "collect_weekly_report", lambda user_id, qualification="SG": DATA), \
                patch.object(ai_feature, "ask_ollama", broken):
            embed = await ai_feature.create_weekly_report_embed(1)
        self.assertIn("AIのコメントは作れませんでした（AIの処理に失敗）", embed.description)
        self.assertEqual(len(embed.fields), 4)

    async def test_report_command(self):
        async def answer(prompt):
            return "### 今週の実績\nよく頑張りました"

        ctx = _Context()
        with patch.object(ai_feature, "collect_weekly_report", lambda user_id, qualification="SG": DATA), \
                patch.object(ai_feature, "ask_ollama", answer):
            await ai_feature.report.callback(ctx)
        self.assertIn("よく頑張りました", ctx.messages[0]["embed"].description)

        ctx = _Context()
        with patch.object(ai_feature, "collect_weekly_report", lambda user_id, qualification="SG": None):
            await ai_feature.report.callback(ctx)
        self.assertIn("まだ週報を作れる", ctx.messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
