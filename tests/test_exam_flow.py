import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import discord

from studybot import config
from studybot import database as database_module
from studybot.embeds import build_digest_embed
from studybot.exam_results import (
    advance_roadmap,
    get_pending_exam,
    get_result_prompt_candidates,
    mark_result_prompted,
    record_exam_result,
)
from studybot.exam_schedule import save_exam_date
from studybot.features import digest as digest_feature
from studybot.features import exam_result as exam_result_feature
from studybot.sg_features import add_sg_mistake
from studybot.stats import get_roadmap


TODAY = date(2026, 10, 10)


def field(embed, prefix):
    return next((f for f in embed.fields if f.name.startswith(prefix)), None)


class FinalStretchEmbedTests(unittest.TestCase):
    focus = [("ネットワーク（直近 48%）", "20問"), ("科目B", "1セット")]

    def stretch(self, days_left, unfinished=12):
        return {
            "days_left": days_left, "label": "SG試験",
            "focus": self.focus, "unfinished": unfinished,
        }

    def test_focus_uses_weakest_categories(self):
        self.assertEqual(
            digest_feature.final_stretch_focus([
                ("システム監査", 55.0), ("ネットワーク", 48.0),
                ("データベース", 59.0), ("企業活動", 58.0),
            ]),
            [("ネットワーク（直近 48%）", "20問"),
             ("システム監査（直近 55%）", "10問"),
             ("企業活動（直近 58%）", "10問"),
             ("科目B", "1セット")],
        )
        self.assertEqual(
            digest_feature.final_stretch_focus([]),
            [("総合演習（全分野）", "20問"), ("科目B", "1セット")],
        )

    def test_seven_days_before(self):
        embed = build_digest_embed(
            TODAY, "SG試験：…あと7日", 12, [], weak=[("ネットワーク", 48.0)],
            final_stretch=self.stretch(7),
        )
        self.assertEqual(embed.title, "10月10日（土）の学習メニュー ・ 直前モード")
        self.assertIn("ネットワーク（直近 48%）　**20問**",
                      field(embed, "今日の重点").value)
        self.assertIn("**12件** を試験前日までに一巡（1日あたり約2件）",
                      field(embed, "仕上げチェック").value)
        self.assertIsNone(field(embed, "正答率60%未満"))   # 重点と重ねない
        self.assertIsNotNone(field(embed, "今日の復習"))

    def test_day_before_and_exam_day(self):
        before = build_digest_embed(
            TODAY, "…", 3, [], final_stretch=self.stretch(1, unfinished=0)
        )
        self.assertIn("早めの睡眠", field(before, "明日は試験です").value)
        self.assertIsNone(field(before, "仕上げチェック"))

        exam_day = build_digest_embed(
            TODAY, "…", 3, [{"id": 1, "category": "x", "question_ref": "q",
                             "success_streak": 0}],
            final_stretch=self.stretch(0),
        )
        self.assertEqual(exam_day.title, "10月10日（土） 今日はSG試験です")
        self.assertIn("がんばってください", exam_day.description)
        self.assertEqual(
            [f.name for f in exam_day.fields], ["直前の確認", "試験が終わったら"]
        )


class _TempDBCase(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = str(Path(temp_dir.name) / "study.db")
        db_patch = patch.object(config, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        database_module.init_db()

    def add_session(self, user_id, study_date, seconds=3600, guild_id=10):
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("""
                    INSERT INTO study_sessions (
                        guild_id, user_id, username, study_date,
                        start_time, end_time, duration_seconds
                    ) VALUES (?, ?, 'test', ?, ?, ?, ?)
                """, (guild_id, user_id, study_date, f"{study_date}T10:00",
                      f"{study_date}T11:00", seconds))


class DailyDigestFinalStretchTests(_TempDBCase):
    def test_digest_switches_to_final_stretch(self):
        save_exam_date(self.db_path, 1, "SG", TODAY + timedelta(days=5), "t")
        add_sg_mistake(self.db_path, 1, "ネットワーク", "q1", "r", today=TODAY)

        embed = digest_feature.build_daily_digest_embed(1, TODAY)
        self.assertTrue(embed.title.endswith("直前モード"))
        self.assertIn("総合演習", field(embed, "今日の重点").value)
        self.assertIn("**1件**", field(embed, "仕上げチェック").value)

        far = digest_feature.build_daily_digest_embed(1, TODAY - timedelta(days=30))
        self.assertFalse(far.title.endswith("直前モード"))


class ExamResultDataTests(_TempDBCase):
    def test_pending_and_prompt_candidates(self):
        save_exam_date(self.db_path, 1, "SG", TODAY, "t")
        save_exam_date(self.db_path, 2, "SG", TODAY + timedelta(days=1), "t")
        save_exam_date(self.db_path, 3, "SG", TODAY - timedelta(days=8), "t")

        self.assertEqual(get_pending_exam(self.db_path, 1, TODAY), ("SG", TODAY.isoformat()))
        self.assertIsNone(get_pending_exam(self.db_path, 2, TODAY))   # まだ試験前
        self.assertIsNone(get_pending_exam(self.db_path, 3, TODAY))   # 8日前は聞かない
        self.assertEqual(
            get_result_prompt_candidates(self.db_path, TODAY),
            [(1, "SG", TODAY.isoformat())],
        )

        mark_result_prompted(self.db_path, 1, "SG", TODAY.isoformat(), TODAY)
        self.assertEqual(get_result_prompt_candidates(self.db_path, TODAY), [])
        tomorrow = TODAY + timedelta(days=1)
        self.assertEqual(
            [row[0] for row in get_result_prompt_candidates(self.db_path, tomorrow)],
            [1, 2],
        )

        record_exam_result(self.db_path, 1, "SG", TODAY.isoformat(), "pass", "t")
        self.assertIsNone(get_pending_exam(self.db_path, 1, tomorrow))
        with self.assertRaises(ValueError):
            record_exam_result(self.db_path, 1, "SG", TODAY.isoformat(), "maybe", "t")

    def test_advance_roadmap(self):
        self.assertEqual(advance_roadmap(self.db_path, "SG")[0], "FE")
        statuses = {row[0]: (row[3], row[4]) for row in get_roadmap()}
        self.assertEqual(statuses["SG"], ("completed", 0))
        self.assertEqual(statuses["FE"], ("learning", 1))

        self.assertEqual(advance_roadmap(self.db_path, "FE")[0], "医療情報技師")
        self.assertIsNone(advance_roadmap(self.db_path, "医療情報技師"))
        self.assertIsNone(advance_roadmap(self.db_path, "存在しない"))

        # 再起動（init_db）しても合格済みのまま
        database_module.init_db()
        self.assertEqual({row[0]: row[3] for row in get_roadmap()}["SG"], "completed")


class _Response:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append(dict(kwargs, content=content))

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(dict(kwargs, content=content))


def _interaction(user_id):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(content="<@1> 結果はどうでしたか？"),
        response=_Response(),
        followup=_Followup(),
    )


class ExamResultViewTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.today = datetime.now(config.JST).date()
        save_exam_date(self.db_path, 1, "SG", self.today, "t")
        self.add_session(1, (self.today - timedelta(days=9)).isoformat())
        self.add_session(1, self.today.isoformat())
        self.view = exam_result_feature.ExamResultView()

    async def test_pass_moves_roadmap_forward(self):
        interaction = _interaction(1)
        await self.view.pass_button.callback(interaction)

        self.assertIn("合格を記録しました", interaction.response.edits[0]["content"])
        self.assertIsNone(interaction.response.edits[0]["view"])
        sent = interaction.followup.sent[0]
        self.assertEqual(sent["embed"].title, "SG合格おめでとうございます！")
        values = {f.name: f.value for f in sent["embed"].fields}
        self.assertEqual(values["学習期間"], "10日（勉強した日 2日）")
        self.assertEqual(values["勉強時間"], "2時間00分")
        self.assertIn("✅ 1. 情報セキュリティマネジメント（SG）　合格済み",
                      values["資格取得ロードマップ"])
        self.assertIn("▶ 2. **基本情報技術者（FE）**　学習中",
                      values["資格取得ロードマップ"])
        self.assertIsInstance(sent["view"], exam_result_feature.NextStepView)

        # 次の試験日の設定画面は FE になる
        next_step = _interaction(1)
        await sent["view"].next_exam_button.callback(next_step)
        self.assertIn("基本情報技術者（FE） の試験日", next_step.response.sent[0]["content"])

        # 記録済みなのでもう一度押しても何も起きない
        again = _interaction(1)
        await self.view.pass_button.callback(again)
        self.assertIn("見つかりません", again.response.sent[0]["content"])

    async def test_fail_offers_retake(self):
        interaction = _interaction(1)
        await self.view.fail_button.callback(interaction)

        self.assertIn("不合格を記録しました", interaction.response.edits[0]["content"])
        sent = interaction.followup.sent[0]
        self.assertIsInstance(sent["view"], exam_result_feature.RetakeView)
        self.assertEqual(
            {row[0]: row[3] for row in get_roadmap()}["SG"], "learning"
        )

    async def test_later_records_nothing(self):
        interaction = _interaction(1)
        await self.view.later_button.callback(interaction)
        self.assertIn("明日の夜20時にもう一度", interaction.response.edits[0]["content"])
        self.assertIsNotNone(get_pending_exam(self.db_path, 1, self.today))


class _Channel:
    def __init__(self, guild):
        self.name = config.STUDY_LOG_CHANNEL_NAME
        self.id = 20
        self.guild = guild
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=1)


class _Guild:
    def __init__(self):
        self.id = 10
        self.channel = _Channel(self)
        self.text_channels = [self.channel]

    async def fetch_member(self, user_id):
        if user_id == 99:
            raise discord.NotFound(SimpleNamespace(status=404, reason=""), "")
        return SimpleNamespace(id=user_id)


class ExamResultPromptTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    async def test_prompts_once_a_day(self):
        guild = _Guild()
        fake_bot = SimpleNamespace(get_guild=lambda guild_id: guild)
        save_exam_date(self.db_path, 1, "SG", TODAY, "t")
        save_exam_date(self.db_path, 2, "SG", TODAY - timedelta(days=2), "t")
        for user_id in (1, 2):
            self.add_session(user_id, "2026-10-09")
        now = datetime(2026, 10, 10, 20, 0, tzinfo=config.JST)

        self.assertEqual(await exam_result_feature.send_exam_result_prompts(fake_bot, now), 2)
        self.assertEqual(await exam_result_feature.send_exam_result_prompts(fake_bot, now), 0)

        contents = [sent["content"] for sent in guild.channel.sent]
        self.assertEqual(contents[0], "<@1> 今日のSG試験、おつかれさまでした。結果はどうでしたか？")
        self.assertTrue(contents[1].startswith("<@2> 10/8のSG試験"))
        self.assertIsInstance(guild.channel.sent[0]["view"], exam_result_feature.ExamResultView)

    def test_prompt_time(self):
        due = exam_result_feature.is_result_prompt_due
        self.assertFalse(due(datetime(2026, 10, 10, 19, 59, tzinfo=config.JST)))
        self.assertTrue(due(datetime(2026, 10, 10, 20, 0, tzinfo=config.JST)))
        view = exam_result_feature.ExamResultView()
        self.assertIsNone(view.timeout)
        self.assertEqual(
            sorted(item.custom_id for item in view.children),
            ["studybot:exam:fail", "studybot:exam:later", "studybot:exam:pass"],
        )


if __name__ == "__main__":
    unittest.main()
