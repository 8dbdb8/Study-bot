import asyncio
import json
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from studybot import config
from studybot.analysis import build_structured_sg_analysis
from studybot.daily_digest import is_compact_display
from studybot.embeds import build_vc_summary_embed
from studybot.features import digest as digest_feature
from studybot.features import glossary as glossary_feature
from studybot.features import mock_timer
from studybot.features import plan as plan_feature
from studybot.features import qualification as qualification_feature
from studybot.features import quiz as quiz_feature
from studybot.qualifications import FE, IRYO, SG
from studybot.scoring import get_mock_timer, list_mock_timers, save_mock_timer
from studybot.sg_glossary import FE_GLOSSARY_PATH, load_glossary

from test_round3 import _Channel, _Context, _TempDBCase, _interaction


TODAY = date(2026, 10, 15)


# ------------------------------------------------------------
# 案2 模試タイマー
# ------------------------------------------------------------

class MockTimerTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.clock = datetime(2026, 10, 15, 13, 0, tzinfo=config.JST)
        self.waits = []

        async def fake_sleep(seconds):
            self.waits.append(seconds)
            self.clock += timedelta(seconds=seconds)

        for name, value in (("_sleep", fake_sleep), ("_now", lambda: self.clock)):
            patcher = patch.object(mock_timer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(mock_timer.MOCK_TIMERS.clear)

    def test_checkpoints(self):
        self.assertEqual(mock_timer.checkpoints(120), [60, 15])
        self.assertEqual(mock_timer.checkpoints(190), [95, 15])
        self.assertEqual(mock_timer.checkpoints(20), [15, 10])
        timer = {"started_at": self.clock, "minutes": 120}
        self.assertEqual(mock_timer.elapsed_minutes(timer, self.clock + timedelta(minutes=104)), 104)
        self.assertEqual(mock_timer.elapsed_minutes(timer, self.clock + timedelta(hours=5)), 120)

    async def test_start_runs_and_finish_opens_form(self):
        ctx = _Context()
        await qualification_feature.record_mock(ctx, SG, "start")
        self.assertIn("120分（科目A 48問・科目B 12問）", ctx.messages[0]["content"])
        self.assertIsInstance(ctx.messages[0]["view"], qualification_feature.MockFinishView)
        await qualification_feature.record_mock(ctx, SG, "start")
        self.assertIn("すでに動いています", ctx.messages[1]["content"])

        await mock_timer.MOCK_TIMERS[7]
        self.assertEqual(self.waits, [3600, 2700, 900])
        texts = [sent["content"] for sent in ctx.channel.sent]
        self.assertIn("残り60分", texts[0])
        self.assertIn("残り15分", texts[1])
        self.assertIn("終わりました", texts[2])
        self.assertIsNotNone(get_mock_timer(self.db_path, 7))   # 結果の入力を待つ

        interaction = _interaction()
        with patch.object(mock_timer, "_now", lambda: self.clock):
            await qualification_feature.MockFinishView().finish_button.callback(interaction)
        modal = interaction.response.modals[0]
        self.assertIsInstance(modal, qualification_feature.MockExamModal)
        self.assertEqual(modal.minutes_input.default, "120")
        self.assertIsNone(get_mock_timer(self.db_path, 7))

        again = _interaction()
        await qualification_feature.MockFinishView().finish_button.callback(again)
        self.assertIn("ありません", again.response.sent[0]["content"])

    async def test_stop_and_no_time_limit(self):
        gate = asyncio.Event()

        async def blocked(seconds):
            await gate.wait()

        ctx = _Context()
        with patch.object(mock_timer, "_sleep", blocked):
            await qualification_feature.record_mock(ctx, FE, "start")
            await asyncio.sleep(0)
            self.clock += timedelta(minutes=30)
            await qualification_feature.record_mock(ctx, FE, "stop")
        self.assertIn("30分経過", ctx.messages[-1]["content"])
        self.assertEqual(list_mock_timers(self.db_path), [])
        await qualification_feature.record_mock(ctx, FE, "stop")
        self.assertIn("ありません", ctx.messages[-1]["content"])
        await qualification_feature.record_mock(ctx, IRYO, "start")
        self.assertIn("タイマーを使えません", ctx.messages[-1]["content"])

    async def test_resume(self):
        channel = _Channel(40)
        bot = SimpleNamespace(get_channel=lambda channel_id: channel if channel_id == 40 else None)
        save_mock_timer(self.db_path, 7, "SG", 40, self.clock - timedelta(minutes=70), 120)
        save_mock_timer(self.db_path, 8, "SG", 99, self.clock, 120)          # 送り先がない
        save_mock_timer(self.db_path, 9, "SG", 40, self.clock - timedelta(days=1), 120)  # 古い
        resumed = await mock_timer.resume_mock_timers(bot, qualification_feature.MockFinishView)
        self.assertEqual(resumed, 1)
        await mock_timer.MOCK_TIMERS[7]
        # 残り60分のお知らせは停止中に過ぎていたので送らない
        texts = [sent["content"] for sent in channel.sent]
        self.assertEqual(len(texts), 2)
        self.assertIn("残り15分", texts[0])
        self.assertEqual([timer["user_id"] for timer in list_mock_timers(self.db_path)], [7])


# ------------------------------------------------------------
# 案7 コンパクト表示
# ------------------------------------------------------------

class CompactDisplayTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    async def test_setting(self):
        self.assertFalse(is_compact_display(self.db_path, 7))
        ctx = _Context()
        await plan_feature.plan_notify.callback(ctx, display="compact")
        self.assertTrue(is_compact_display(self.db_path, 7))
        self.assertIn("表示：**コンパクト**", ctx.messages[0]["content"])
        await plan_feature.plan_notify.callback(ctx, display="normal")
        self.assertFalse(is_compact_display(self.db_path, 7))

    async def test_compact_digest_and_detail(self):
        today = datetime.now(config.JST).date()
        self.save_log(build_structured_sg_analysis("ネットワーク", 20, 40.0), today.isoformat())
        full = digest_feature.build_daily_digest_embed(7, today)
        compact = digest_feature.build_daily_digest_embed(7, today, save=False, compact=True)
        self.assertEqual(compact.fields, [])
        lines = compact.description.splitlines()
        self.assertLessEqual(len(lines), 8)
        self.assertIn("☑ チェックリスト 1/1", lines)
        self.assertTrue(any(line.startswith("**正答率60%未満の分野**") for line in lines))
        self.assertEqual(compact.title, full.title)

        self.assertNotIn("detail_button", [
            item.callback.callback.__name__ for item in digest_feature.DailyDigestView(compact=False).children
            if hasattr(item.callback, "callback")
        ])
        self.assertEqual(len(digest_feature.DailyDigestView(compact=False).children), 4)
        view = digest_feature.DailyDigestView()
        self.assertEqual(len(view.children), 5)
        interaction = _interaction()
        await view.detail_button.callback(interaction)
        self.assertEqual(interaction.response.sent[0]["embed"].fields[-1].name, "今日のチェックリスト")

    def test_compact_vc_summary(self):
        embed = build_vc_summary_embed(
            "test", 1800, 3600, [("2026-10-15", 3600)], TODAY, 5,
            "SG試験：10/17　あと**2日**", "#勉強ログ",
            [("今日の目標", "`▰▰▰▱` 60/90分"), ("今日のメニュー 1/2 完了", "✅ a\n⬜ b")],
            compact=True,
        )
        self.assertEqual(embed.fields, [])
        self.assertEqual(embed.description.splitlines(), [
            "今回 **30分** ・ 今日 1時間00分 ・ 今週 1時間00分",
            "🔥 連続 5日",
            "今日の目標：`▰▰▰▱` 60/90分",
            "今日のメニュー 1/2 完了：✅ a",
            "SG試験：10/17　あと**2日**",
        ])


# ------------------------------------------------------------
# 案8 FE用語集
# ------------------------------------------------------------

class FEGlossaryTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def test_data(self):
        entries = load_glossary(FE_GLOSSARY_PATH)
        self.assertGreaterEqual(len(entries), 300)
        self.assertEqual({entry.category for entry in entries}, set(FE.category_names))
        self.assertTrue(all(entry.meaning for entry in entries))
        self.assertEqual(len({(e.category, e.term) for e in entries}), len(entries))
        metadata = json.loads(FE_GLOSSARY_PATH.read_text(encoding="utf-8"))["metadata"]
        self.assertIn("正確さを保証するものではありません", metadata["meaning_review_note"])

    async def test_fe_glossary_anywhere(self):
        ctx = _Context()
        ctx.guild = SimpleNamespace(id=1)
        await glossary_feature.feglossary.callback(ctx, mode="cards", category="データベース")
        sent = ctx.messages[0]
        self.assertTrue(sent["ephemeral"])
        self.assertIn("**FE単語帳**", sent["content"])
        self.assertIsNone(sent["view"].record_channel)
        await glossary_feature.feglossary.callback(ctx, category="存在しない")
        self.assertIn("候補から", ctx.messages[1]["content"])

    async def test_fe_quiz(self):
        ctx = _Context()
        await quiz_feature.fe_quiz.callback(ctx, category="ネットワーク")
        view = ctx.messages[0]["view"]
        self.assertEqual(view.label, "FE")
        self.assertTrue(ctx.messages[0]["embed"].title.startswith("FE用語ミニテスト 1/5"))
        self.assertTrue(all(q.entry.category == "ネットワーク" for q in view.questions))


if __name__ == "__main__":
    unittest.main()
