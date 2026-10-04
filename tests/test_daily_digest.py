import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import discord

from studybot import activity_forms
from studybot import config
from studybot import database as database_module
from studybot import forms
from studybot import voice
from studybot.features import digest as digest_feature
from studybot.features import plan as plan_feature
from studybot.features import review as review_feature
from studybot.weekly_report import is_weekly_report_enabled
from studybot.daily_digest import (
    DEFAULT_DIGEST_TIME,
    get_digest_candidates,
    get_home_guild_id,
    is_digest_enabled,
    mark_digest_sent,
    parse_digest_time,
    set_digest_enabled,
)
from studybot.exam_schedule import save_exam_date
from studybot.sg_features import add_sg_mistake, get_sg_mistakes, save_sg_plan



TODAY = date(2026, 10, 15)


class _TempDBCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = str(Path(self.temp_dir.name) / "study.db")
        db_patch = patch.object(config, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        database_module.init_db()

    def add_session(self, user_id, guild_id, study_date, seconds=600):
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

    def add_due_mistake(self, user_id, ref="https://example.com/q/1"):
        mistake_id, _ = add_sg_mistake(
            self.db_path, user_id, "ネットワーク", ref, "読み違えた",
            today=TODAY - timedelta(days=1),
        )
        return mistake_id


class DigestDatabaseTests(_TempDBCase):
    def test_parse_digest_time(self):
        self.assertEqual(parse_digest_time("06:30").strftime("%H:%M"), "06:30")
        self.assertEqual(parse_digest_time("7時"), DEFAULT_DIGEST_TIME)
        self.assertEqual(parse_digest_time("25:00"), DEFAULT_DIGEST_TIME)

    def test_candidates_need_something_to_say(self):
        self.add_due_mistake(1)
        save_sg_plan(self.db_path, 2, 4, 30, "t", today=TODAY)
        save_exam_date(self.db_path, 3, "SG", TODAY + timedelta(days=30), "t")
        save_exam_date(self.db_path, 4, "SG", TODAY - timedelta(days=1), "t")
        add_sg_mistake(  # 明日が復習日
            self.db_path, 5, "ネットワーク", "q", "r", today=TODAY,
        )
        self.add_session(6, 10, TODAY.isoformat())

        self.assertEqual(get_digest_candidates(self.db_path, TODAY), [1, 2, 3])

    def test_disabled_and_already_sent_are_skipped(self):
        for user_id in (1, 2, 3):
            self.add_due_mistake(user_id)
        self.assertTrue(is_digest_enabled(self.db_path, 1))

        set_digest_enabled(self.db_path, 1, False)
        mark_digest_sent(self.db_path, 2, TODAY, 10, 20, 30)

        self.assertFalse(is_digest_enabled(self.db_path, 1))
        self.assertEqual(get_digest_candidates(self.db_path, TODAY), [3])
        self.assertEqual(
            get_digest_candidates(self.db_path, TODAY + timedelta(days=1)),
            [2, 3],
        )
        set_digest_enabled(self.db_path, 1, True)
        self.assertIn(1, get_digest_candidates(self.db_path, TODAY))

    def test_home_guild_is_most_recent_study_place(self):
        self.assertIsNone(get_home_guild_id(self.db_path, 1))
        self.add_session(1, 111, "2026-10-01")
        self.add_session(1, 222, "2026-10-10")
        self.add_session(2, 333, "2026-10-12")
        self.assertEqual(get_home_guild_id(self.db_path, 1), 222)


class _Channel:
    def __init__(self, name, guild=None):
        self.name = name
        self.id = 20
        self.guild = guild
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=1000 + len(self.sent))


class _Guild:
    def __init__(self, members):
        self.id = 10
        self.members = members
        self.channel = _Channel(config.STUDY_LOG_CHANNEL_NAME, self)
        self.text_channels = [_Channel("雑談", self), self.channel]

    async def fetch_member(self, user_id):
        if user_id not in self.members:
            raise discord.NotFound(SimpleNamespace(status=404, reason=""), "")
        return SimpleNamespace(id=user_id)


class SendDigestTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.guild = _Guild(members={1, 2})
        self.fake_bot = SimpleNamespace(
            get_guild=lambda guild_id: self.guild if guild_id == 10 else None,
        )

    async def test_sends_once_per_day_to_home_guild(self):
        for user_id in (1, 2, 3):
            self.add_due_mistake(user_id)
            self.add_session(user_id, 10, "2026-10-14")
        now = datetime(2026, 10, 15, 7, 0, tzinfo=config.JST)

        sent = await digest_feature.send_daily_digests(self.fake_bot, now)

        # 3 はサーバーにいないので送らない
        self.assertEqual(sent, 2)
        first = self.guild.channel.sent[0]
        self.assertEqual(first["content"], "<@1> おはようございます。今日の学習メニューです。")
        self.assertEqual(first["embed"].title, "10月15日（木）の学習メニュー")
        self.assertIsInstance(first["view"], digest_feature.DailyDigestView)
        self.assertEqual(
            [user.id for user in first["allowed_mentions"].users], [1]
        )

        # 再起動などで二度呼ばれても重複しない
        self.assertEqual(await digest_feature.send_daily_digests(self.fake_bot, now), 0)
        self.assertEqual(len(self.guild.channel.sent), 2)

    async def test_greeting_depends_on_time_and_missing_channel(self):
        self.add_due_mistake(1)
        self.add_session(1, 10, "2026-10-14")
        self.add_due_mistake(4)
        self.add_session(4, 99, "2026-10-14")  # Botがいないサーバー

        sent = await digest_feature.send_daily_digests(
            self.fake_bot,
            datetime(2026, 10, 15, 13, 0, tzinfo=config.JST)
        )

        self.assertEqual(sent, 1)
        self.assertEqual(
            self.guild.channel.sent[0]["content"], "<@1> 今日の学習メニューです。"
        )

        # 平日の夜はねぎらいのひとこと
        self.add_due_mistake(2)
        self.add_session(2, 10, "2026-10-14")
        await digest_feature.send_daily_digests(
            self.fake_bot, datetime(2026, 10, 15, 19, 0, tzinfo=config.JST)
        )
        self.assertEqual(
            self.guild.channel.sent[-1]["content"],
            "<@2> おつかれさまです。今日の学習メニューです。",
        )

    def test_weekday_evening_and_day_off_morning(self):
        def at(day, hour, minute=0):
            return datetime(2026, 10, day, hour, minute, tzinfo=config.JST)

        due = digest_feature.is_digest_due
        # 10/15（木）は平日：19時から
        self.assertFalse(due(at(15, 7, 0)))
        self.assertFalse(due(at(15, 18, 59)))
        self.assertTrue(due(at(15, 19, 0)))
        self.assertTrue(due(at(15, 21, 59)))
        self.assertFalse(due(at(15, 22, 0)))
        # 10/17（土）と 10/12（月・スポーツの日）は朝7時から
        for day in (17, 12):
            self.assertFalse(due(at(day, 6, 59)))
            self.assertTrue(due(at(day, 7, 0)))
            self.assertTrue(due(at(day, 19, 0)))


class _Response:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.modals = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append(dict(kwargs, content=content))

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


def _interaction(user_id, guild=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild=guild,
        response=_Response(),
    )


class ReviewSessionTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    async def test_walks_through_due_reviews(self):
        first = self.add_due_mistake(1, "q1")
        second = self.add_due_mistake(1, "q2")
        third = self.add_due_mistake(1, "q3")
        self.add_due_mistake(2, "other user")

        embed, view = review_feature.build_review_session(1, TODAY)
        self.assertEqual(embed.title, "復習 1/3 ・ #1 ネットワーク")
        interaction = _interaction(1)

        await view.correct_button.callback(interaction)
        self.assertEqual(
            interaction.response.edits[-1]["embed"].title,
            f"復習 2/3 ・ #{second} ネットワーク",
        )
        await view.wrong_button.callback(interaction)
        await view.skip_button.callback(interaction)

        final = interaction.response.edits[-1]
        self.assertIsNone(final["view"])
        self.assertEqual(final["embed"].title, "今日の復習おわり")
        self.assertIn("正解 **1問** ・ 不正解 **1問**", final["embed"].description)

        due = {
            item["id"]: item["next_review_on"]
            for item in get_sg_mistakes(self.db_path, 1, today=date(2026, 12, 31))
        }
        self.assertEqual(due[first], (TODAY + timedelta(days=3)).isoformat())
        self.assertEqual(due[second], (TODAY + timedelta(days=1)).isoformat())
        self.assertEqual(due[third], TODAY.isoformat())

    async def test_only_owner_can_answer(self):
        self.add_due_mistake(1)
        _, view = review_feature.build_review_session(1, TODAY)
        stranger = _interaction(2)

        self.assertFalse(await view.interaction_check(stranger))
        self.assertTrue(stranger.response.sent[0]["ephemeral"])

    def test_no_due_reviews(self):
        content, view = review_feature.build_review_session(1, TODAY)
        self.assertIsNone(view)
        self.assertIn("ありません", content)


class ButtonTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    async def test_vc_buttons_open_forms_for_the_clicker(self):
        guild = _Guild(members={1})
        guild.text_channels.append(_Channel(config.STUDY_LOG_CHANNEL_NAME))
        view = voice.VCActionView()
        self.assertIsNone(view.timeout)
        self.assertTrue(all(item.custom_id.startswith("studybot:vc:")
                            for item in view.children))

        # 勉強の記録は、まず過去問道場・復習・単語帳から選ぶ
        for button, expected in (
            (view.sglog_button, activity_forms.StudyKindView),
            (view.sgb_button, forms.SGBPracticeView),
            (view.mistake_button, forms.SGMistakeView),
        ):
            interaction = _interaction(7, guild)
            await button.callback(interaction)
            sent = interaction.response.sent[0]
            self.assertTrue(sent["ephemeral"])
            self.assertIsInstance(sent["view"], expected)
            self.assertEqual(sent["view"].owner_id, 7)

    async def test_vc_button_outside_server(self):
        interaction = _interaction(7, None)
        await voice.VCActionView().sglog_button.callback(interaction)
        sent = interaction.response.sent[0]
        self.assertNotIn("view", sent)
        self.assertIn("サーバー内", sent["content"])

    async def test_digest_buttons_use_the_clicker(self):
        # ボタンは実行時の「今日」で復習を探す
        add_sg_mistake(
            self.db_path, 5, "ネットワーク", "q", "r",
            today=datetime.now(config.JST).date() - timedelta(days=1),
        )
        view = digest_feature.DailyDigestView()

        interaction = _interaction(5)
        await view.review_button.callback(interaction)
        self.assertIsInstance(
            interaction.response.sent[0]["view"], review_feature.ReviewSessionView
        )

        nobody = _interaction(6)
        await view.review_button.callback(nobody)
        self.assertIn("ありません", nobody.response.sent[0]["content"])

        await view.plan_button.callback(nobody)
        self.assertIn("/plan new", nobody.response.sent[1]["content"])


class _Context:
    def __init__(self, user_id):
        self.author = SimpleNamespace(id=user_id)
        self.guild = None
        self.interaction = object()
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append(dict(kwargs, content=content))


class NotifyCommandTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    async def test_toggle(self):
        ctx = _Context(1)
        await plan_feature.plan_notify.callback(ctx)
        content = ctx.messages[-1]["content"]
        self.assertIn("学習メニュー：**オン**", content)
        self.assertIn("週間レポート：**オン**", content)
        self.assertIn("平日 19:00 / 土日祝 07:00", content)
        self.assertIn("毎週日曜 21:00", content)

        await plan_feature.plan_notify.callback(ctx, menu="off")
        self.assertFalse(is_digest_enabled(self.db_path, 1))
        self.assertTrue(is_weekly_report_enabled(self.db_path, 1))
        self.assertTrue(ctx.messages[-1]["ephemeral"])
        self.assertIn("学習メニュー：**オフ**", ctx.messages[-1]["content"])

        await plan_feature.plan_notify.callback(ctx, menu="on", report="off")
        self.assertTrue(is_digest_enabled(self.db_path, 1))
        self.assertFalse(is_weekly_report_enabled(self.db_path, 1))
        self.assertIn("週間レポート：**オフ**", ctx.messages[-1]["content"])

        await plan_feature.plan_notify.callback(ctx, report="maybe")
        self.assertIn("on か off", ctx.messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
