import asyncio
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from studybot import config
from studybot.analysis import build_structured_sg_analysis
from studybot.checklist import (
    build_checklist_items,
    checklist_status,
    checklist_summary,
    format_checklist,
    get_checklist,
    save_checklist,
)
from studybot.database import save_completed_study_session
from studybot.embeds import build_review_card_embed
from studybot.exam_prep import (
    PREP_ITEMS,
    get_exam_prep,
    get_prep_candidates,
    set_prep_memo,
)
from studybot.exam_schedule import save_exam_date
from studybot.features import ai as ai_feature
from studybot.features import digest as digest_feature
from studybot.features import exam_prep as prep_feature
from studybot.features import focus as focus_feature
from studybot.features import health as health_feature
from studybot.features import qualification as qualification_feature
from studybot.features import review as review_feature
from studybot.features import time as time_feature
from studybot.forms import SGMistakeModal
from studybot.habits import (
    get_focus_settings,
    list_focus_timers,
    save_focus_timer,
    set_focus_settings,
)
from studybot.health_state import load_notified_problems
from studybot.qualifications import FE, IRYO, SG
from studybot.sg_features import (
    add_sg_mistake,
    format_reason_breakdown,
    get_reason_breakdown,
    get_sg_mistakes,
    record_sg_mistake_attempt,
)
from studybot.sg_glossary import GlossaryEntry
from studybot.voice import build_study_end_extras

from test_round3 import _Channel, _Context, _TempDBCase, _interaction


TODAY = date(2026, 10, 15)   # 木曜


def _real_today():
    return datetime.now(config.JST).date()


# ------------------------------------------------------------
# 案1 間違えた理由の種類
# ------------------------------------------------------------

class ReasonKindTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def test_store_and_breakdown(self):
        add_sg_mistake(self.db_path, 7, "ネットワーク", "q1", "", today=TODAY,
                       reason_kind="読み違い")
        add_sg_mistake(self.db_path, 7, "ネットワーク", "q2", "条件を見落とし",
                       today=TODAY, reason_kind="読み違い")
        add_sg_mistake(self.db_path, 7, "データベース", "q3", "用語を知らない",
                       today=TODAY, reason_kind="知識不足")
        add_sg_mistake(self.db_path, 7, "データベース", "q4", "古い記録", today=TODAY)
        with self.assertRaises(ValueError):
            add_sg_mistake(self.db_path, 7, "ネットワーク", "q5", "", today=TODAY)
        with self.assertRaises(ValueError):
            add_sg_mistake(self.db_path, 7, "ネットワーク", "q5", "x",
                           today=TODAY, reason_kind="その他")

        breakdown = get_reason_breakdown(self.db_path, 7, TODAY, TODAY, "SG")
        self.assertEqual(breakdown["items"], [("読み違い", 2), ("知識不足", 1)])
        self.assertEqual(
            format_reason_breakdown(breakdown),
            "読み違い 2問（67%） ・ 知識不足 1問（33%）（種類なし 1問）",
        )
        empty = get_reason_breakdown(self.db_path, 7, TODAY - timedelta(days=9),
                                     TODAY - timedelta(days=1))
        self.assertIsNone(format_reason_breakdown(empty))

        items = get_sg_mistakes(self.db_path, 7, today=TODAY + timedelta(days=1))
        card = build_review_card_embed(items[0], 1, 4)
        self.assertIn("【読み違い】", card.description)
        card = build_review_card_embed(items[1], 2, 4)
        self.assertIn("【読み違い】条件を見落とし", card.description)

    async def test_modal_has_kind_select(self):
        modal = SGMistakeModal("ネットワーク")
        components = modal.to_dict()["components"]
        self.assertEqual(len(components), 5)
        self.assertEqual(components[1]["component"]["type"], 3)
        modal.reference_input._value = "令和6年 問12"
        modal.kind_select._values = ["うっかり"]
        modal.reason_input._value = ""
        modal.memo_input._value = ""
        modal.image_input._values = []
        interaction = _interaction()
        await modal.on_submit(interaction)
        self.assertIn("を登録しました", interaction.followup.sent[0]["content"])
        item = get_sg_mistakes(self.db_path, 7, today=_real_today() + timedelta(days=1))[0]
        self.assertEqual(item["reason_kind"], "うっかり")

    async def test_weekly_report_and_progress(self):
        today = _real_today()
        self.save_log(build_structured_sg_analysis("ネットワーク", 20, 50.0),
                      today.isoformat())
        add_sg_mistake(self.db_path, 7, "ネットワーク", "q1", "", today=today,
                       reason_kind="読み違い")
        data = ai_feature.collect_weekly_report(7, "SG", today)
        self.assertIn("今週登録した誤答の理由：読み違い 1問（100%）", data["facts_text"])
        embed = ai_feature.build_weekly_report_embed(data, "本文")
        self.assertEqual(embed.fields[-1].name, "今週の間違え方")

        ctx = _Context()
        await qualification_feature.show_progress(ctx, SG)
        names = [field.name for field in ctx.messages[0]["embed"].fields]
        self.assertIn("間違え方（直近28日の誤答）", names)


# ------------------------------------------------------------
# 案2 AIの解説
# ------------------------------------------------------------

ENTRIES = [
    GlossaryEntry(term="DMZ", meaning="公開サーバーを置く区画"),
    GlossaryEntry(term="ファイアウォール", meaning="通信を制御する仕組み"),
    GlossaryEntry(term="A", meaning="1文字の用語は使わない"),
]


class ExplainTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def item(self, **values):
        item = {
            "id": 1, "category": "ネットワーク", "question_ref": "令和6年 問12",
            "reason": "DMZとファイアウォールの関係を読み違えた", "memo": None,
            "reason_kind": "読み違い", "qualification": "SG",
            "success_streak": 0, "next_review_on": "2026-10-15",
            "image_path": None,
        }
        item.update(values)
        return item

    def test_prompt_and_glossary(self):
        found = review_feature.related_glossary(self.item(), ENTRIES)
        self.assertEqual([entry.term for entry in found], ["ファイアウォール", "DMZ"])
        self.assertEqual(review_feature.related_glossary(
            self.item(qualification="FE"), ENTRIES
        ), [])
        prompt = review_feature.build_explain_prompt(self.item(), found)
        self.assertIn("【間違えた理由の種類】読み違い", prompt)
        self.assertIn("- DMZ：公開サーバーを置く区画", prompt)
        self.assertIn("問題文はありません", prompt)

    async def test_button_sends_explanation(self):
        add_sg_mistake(self.db_path, 7, "ネットワーク", "q1", "理由", today=TODAY,
                       reason_kind="知識不足")
        items = get_sg_mistakes(self.db_path, 7, today=TODAY + timedelta(days=1))
        _, view = review_feature.build_review_session(7, TODAY, items)

        async def fake_ask(prompt):
            self.assertIn("知識不足", prompt)
            return "- 要点1\n- 要点2\n- 要点3"

        interaction = _interaction()
        with patch.object(review_feature, "ask_ollama", fake_ask), \
                patch.object(review_feature, "load_glossary", lambda: ENTRIES):
            await view.explain_button.callback(interaction)
        self.assertTrue(interaction.response.deferred)
        sent = interaction.followup.sent[0]
        self.assertEqual(sent["embed"].title, "AIの解説（ネットワーク）")
        self.assertTrue(sent["ephemeral"])
        self.assertEqual(view.index, 0)

        async def broken(prompt):
            raise RuntimeError("down")

        interaction = _interaction()
        with patch.object(review_feature, "ask_ollama", broken):
            await view.explain_button.callback(interaction)
        self.assertIn("AIに接続できませんでした", interaction.followup.sent[0]["content"])


# ------------------------------------------------------------
# 案3 集中タイマーと勉強部屋の連動・再開
# ------------------------------------------------------------

class _VoiceChannel:
    def __init__(self, guild, channel_id, name, members=()):
        self.guild = guild
        self.id = channel_id
        self.name = name
        self.members = list(members)


class _Guild:
    def __init__(self, members=()):
        self.id = 1
        self.study = _VoiceChannel(self, 10, "勉強部屋", members)
        self.focus = _Channel(30)
        self.focus.name = "集中タイマー"
        self.focus.guild = self
        self.voice_channels = [self.study]
        self.text_channels = [self.focus]

    def get_channel(self, channel_id):
        for channel in self.voice_channels + self.text_channels:
            if channel.id == channel_id:
                return channel
        return None


class FocusLinkTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.clock = datetime(2026, 10, 15, 9, 0, tzinfo=config.JST)
        self.gate = asyncio.Event()

        async def blocked_sleep(seconds):
            await self.gate.wait()

        for name, value in (("_sleep", blocked_sleep), ("_now", lambda: self.clock)):
            patcher = patch.object(focus_feature, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._stop_all)

    def _stop_all(self):
        for user_id in list(focus_feature.FOCUS_TIMERS):
            focus_feature.stop_timer(user_id)

    async def test_settings_command(self):
        ctx = _Context()
        await focus_feature.focus.callback(ctx, minutes=50, break_minutes=10,
                                           sets=2, auto="on")
        self.assertIn("50分 × 2セット", ctx.messages[0]["content"])
        self.assertEqual(get_focus_settings(self.db_path, 7), (True, 50, 10, 2))
        await focus_feature.focus.callback(ctx, auto="off")
        self.assertEqual(get_focus_settings(self.db_path, 7)[0], False)
        await focus_feature.focus.callback(ctx, minutes=1, auto="on")
        self.assertIn("5〜120分", ctx.messages[-1]["content"])
        self.assertEqual(focus_feature.FOCUS_TIMERS, {})

    async def test_starts_and_stops_with_study_room(self):
        guild = _Guild()
        member = SimpleNamespace(bot=False, id=7, guild=guild)
        outside = SimpleNamespace(channel=None)
        inside = SimpleNamespace(channel=guild.study)

        await focus_feature.on_voice_state_update(member, outside, inside)
        self.assertEqual(focus_feature.FOCUS_TIMERS, {})   # 設定がオフ

        set_focus_settings(self.db_path, 7, True, 25, 5, 4)
        await focus_feature.on_voice_state_update(member, outside, inside)
        self.assertTrue(focus_feature.FOCUS_TIMERS[7]["auto"])
        self.assertIn("勉強部屋に入ったので", guild.focus.sent[0]["content"])
        self.assertEqual(len(list_focus_timers(self.db_path)), 1)

        await focus_feature.on_voice_state_update(member, inside, outside)
        self.assertNotIn(7, focus_feature.FOCUS_TIMERS)
        self.assertIn("0/4セット完了", guild.focus.sent[1]["content"])
        self.assertEqual(list_focus_timers(self.db_path), [])

    async def test_manual_timer_keeps_running_after_leaving(self):
        guild = _Guild()
        ctx = _Context()
        ctx.guild = guild
        await focus_feature.focus.callback(ctx)
        member = SimpleNamespace(bot=False, id=7, guild=guild)
        await focus_feature.on_voice_state_update(
            member, SimpleNamespace(channel=guild.study), SimpleNamespace(channel=None)
        )
        self.assertIn(7, focus_feature.FOCUS_TIMERS)

    async def test_bot_shutdown_keeps_saved_timer(self):
        timer = focus_feature.start_timer(_Channel(), 7, 25, 5, 4)
        await asyncio.sleep(0)
        timer["task"].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await timer["task"]
        self.assertEqual(len(list_focus_timers(self.db_path)), 1)
        self.assertNotIn(7, focus_feature.FOCUS_TIMERS)

    async def test_resume_after_restart(self):
        guild = _Guild(members=[SimpleNamespace(id=7)])
        started = self.clock - timedelta(minutes=40)
        save_focus_timer(self.db_path, 7, guild.focus.id, started, 25, 5, 2, auto=True)
        save_focus_timer(self.db_path, 8, 999, started, 25, 5, 2)   # 送り先がない
        save_focus_timer(self.db_path, 9, guild.focus.id, started, 25, 5, 2, auto=True)
        bot = SimpleNamespace(get_channel=guild.get_channel)

        self.assertEqual(await focus_feature.resume_focus_timers(bot), 1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # 1セット目は停止中に終わっていたので、知らせずに記録だけ
        self.assertEqual(focus_feature.FOCUS_TIMERS[7]["done"], 1)
        self.assertEqual(guild.focus.sent, [])
        self.assertEqual(
            [timer["user_id"] for timer in list_focus_timers(self.db_path)], [7]
        )


class FocusScheduleTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    async def test_late_finish_is_reported_quietly(self):
        clock = datetime(2026, 10, 15, 12, 0, tzinfo=config.JST)

        async def no_sleep(seconds):
            raise AssertionError("待たないはず")

        channel = _Channel()
        with patch.object(focus_feature, "_sleep", no_sleep), \
                patch.object(focus_feature, "_now", lambda: clock):
            timer = focus_feature.start_timer(
                channel, 7, 25, 5, 2, started_at=clock - timedelta(hours=2)
            )
            await timer["task"]
        self.assertIn("Botが止まっている間に", channel.sent[0]["content"])
        self.assertEqual(len(channel.sent), 1)


# ------------------------------------------------------------
# 案7 チェックリスト
# ------------------------------------------------------------

class ChecklistTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def test_items(self):
        weak = [("ネットワーク", 40.0), ("データベース", 55.0)]
        items = build_checklist_items(SG, 3, weak, goal_minutes=30)
        self.assertEqual(
            [item["label"] for item in items],
            ["今日の復習 3問", "ネットワーク 10問", "勉強 30分"],
        )
        final = build_checklist_items(SG, 0, weak, final_stretch=True)
        self.assertEqual(
            [item["label"] for item in final],
            ["ネットワーク 20問", "データベース 10問", "科目B 1セット"],
        )
        self.assertEqual(
            [item["label"] for item in build_checklist_items(IRYO, 0, [], True)],
            ["総合演習 20問"],
        )
        self.assertEqual(build_checklist_items(FE, 0, []), [])

    def test_progress_from_records(self):
        day = TODAY
        mistake_id, _ = add_sg_mistake(self.db_path, 7, "ネットワーク", "q", "r",
                                       today=day - timedelta(days=1))
        items = build_checklist_items(
            SG, 1, [("ネットワーク", 40.0)], final_stretch=True, goal_minutes=30
        )
        save_checklist(self.db_path, 7, day, items)
        self.assertEqual(get_checklist(self.db_path, 7, day), items)
        self.assertEqual(checklist_summary(checklist_status(self.db_path, 7, day)),
                         "0/4 完了")

        record_sg_mistake_attempt(self.db_path, 7, mistake_id, "wrong", today=day)
        self.save_log(build_structured_sg_analysis("ネットワーク", 12, 50.0),
                      day.isoformat())
        self.save_b(4, 3, day.isoformat())
        start = datetime(2026, 10, 15, 9, 0, tzinfo=config.JST)
        save_completed_study_session(1, 7, "test", start,
                                     start + timedelta(minutes=20), 1200)

        status = checklist_status(self.db_path, 7, day)
        self.assertEqual(checklist_summary(status), "2/4 完了")
        self.assertEqual(format_checklist(status).splitlines(), [
            "✅ 今日の復習 1問（1/1）",
            "⬜ ネットワーク 20問（12/20）",
            "✅ 科目B 1セット（1/1）",
            "⬜ 勉強 30分（20/30分）",
        ])

    async def test_digest_vc_and_today(self):
        today = _real_today()
        self.save_log(build_structured_sg_analysis("ネットワーク", 12, 40.0),
                      today.isoformat())
        embed = digest_feature.build_daily_digest_embed(7, today)
        self.assertEqual(embed.fields[-1].name, "今日のチェックリスト")
        self.assertIn("ネットワーク 10問（10/10）", embed.fields[-1].value)
        self.assertEqual(len(get_checklist(self.db_path, 7, today)), 1)

        extras = dict(build_study_end_extras(7, today, 0))
        self.assertIn("今日のメニュー 1/1 完了 🎉", extras)

        ctx = _Context()
        await time_feature.today.callback(ctx)
        names = [field.name for field in ctx.messages[0]["embed"].fields]
        self.assertIn("今日のメニュー 1/1 完了 🎉", names)


# ------------------------------------------------------------
# 案8 健康チェック
# ------------------------------------------------------------

class _Response:
    def __init__(self, status, data):
        self.status = status
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        return self.data


class _Session:
    def __init__(self, response):
        self.response = response

    def get(self, url):
        self.url = url
        return self.response


class _TextChannel(_Channel):
    def __init__(self, guild, channel_id, name, can_send=True):
        super().__init__(channel_id)
        self.guild = guild
        self.name = name
        self.can_send = can_send

    def permissions_for(self, member):
        return SimpleNamespace(send_messages=self.can_send, embed_links=True)


class HealthTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    async def test_ollama(self):
        ok = _Session(_Response(200, {"models": [{"name": "qwen3:8b"}]}))
        self.assertTrue((await health_feature.check_ollama(ok))[1])
        self.assertEqual(ok.url, "http://localhost:11434/api/tags")
        missing = _Session(_Response(200, {"models": [{"name": "llama3:8b"}]}))
        result = await health_feature.check_ollama(missing)
        self.assertFalse(result[1])
        self.assertIn("ollama pull qwen3:8b", result[2])

        class Down:
            def get(self, url):
                raise health_feature.aiohttp.ClientConnectionError()

        self.assertIn("接続できません", (await health_feature.check_ollama(Down()))[2])

    async def test_notion_not_configured(self):
        with patch.object(config, "NOTION_TOKEN", None):
            self.assertIsNone((await health_feature.check_notion(None))[1])

    def test_channels(self):
        guild = SimpleNamespace(id=1, me=object(), voice_channels=[])
        guild.text_channels = [
            _TextChannel(guild, 20, "勉強ログ", can_send=False),
            _TextChannel(guild, 21, "ai-report"),
        ]
        guild.get_channel = lambda channel_id: None
        results = health_feature.check_channels(guild)
        problems = health_feature.problem_lines(results)
        self.assertIn("#勉強部屋：見つかりません（「勉強部屋」を作るか /setup で選んでください）",
                      problems)
        self.assertIn("#勉強ログ：<#20> にメッセージを送る権限がありません", problems)
        self.assertNotIn("#SG用語集", " ".join(problems))

    async def test_notifies_only_when_problems_change(self):
        guild = SimpleNamespace(id=1, voice_channels=[])
        report = _TextChannel(guild, 21, "ai-report")
        guild.text_channels = [report]
        guild.get_channel = lambda channel_id: None
        bot = SimpleNamespace(guilds=[guild])
        results = {"value": [("Ollama", False, "接続できません"), ("Notion", None, "未設定")]}

        async def fake_run(bot):
            return results["value"]

        with patch.object(health_feature, "run_health_check", fake_run):
            await health_feature.check_and_notify(bot)
            await health_feature.check_and_notify(bot)
            self.assertEqual(len(report.sent), 1)
            self.assertIn("Ollama：接続できません", report.sent[0]["embed"].description)
            self.assertEqual(load_notified_problems(self.db_path), ["Ollama：接続できません"])

            results["value"] = [("Ollama", True, "qwen3:8b が使えます")]
            await health_feature.check_and_notify(bot)
            self.assertEqual(len(report.sent), 2)
            self.assertIn("解決しました", report.sent[1]["embed"].title)

        text = health_feature.format_health(
            [("Ollama", True, "OK"), ("Notion", None, "未設定"), ("#勉強ログ", True, "<#20>")],
            datetime(2026, 10, 15, 6, 30),
        )
        self.assertEqual(text.splitlines(), [
            "✅ Ollama：OK", "・ Notion：未設定", "（10/15 06:30 に確認）",
        ])


# ------------------------------------------------------------
# 案9 試験前日の持ち物チェック
# ------------------------------------------------------------

class ExamPrepTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    async def test_send_toggle_and_memo(self):
        today = _real_today()
        tomorrow = today + timedelta(days=1)
        save_exam_date(self.db_path, 7, "SG", tomorrow, "t")
        self.assertEqual(get_prep_candidates(self.db_path, today),
                         [(7, "SG", tomorrow.isoformat())])

        channel = _Channel()

        async def home(bot, user_id):
            return channel

        with patch.object(prep_feature, "find_home_channel", home):
            now = datetime.combine(today, datetime.min.time(), tzinfo=config.JST)
            self.assertEqual(await prep_feature.send_exam_prep_prompts(None, now), 1)
            self.assertEqual(await prep_feature.send_exam_prep_prompts(None, now), 0)
        sent = channel.sent[0]
        self.assertEqual(sent["embed"].title, "明日はSG試験です ・ 持ち物チェック")
        self.assertEqual(len(sent["view"].children), len(PREP_ITEMS) + 1)

        view = prep_feature.ExamPrepView()
        for index in range(len(PREP_ITEMS)):
            interaction = _interaction()
            await view.children[index].callback(interaction)
        edit = interaction.response.edits[0]
        self.assertIn("準備OK", edit["embed"].description)
        self.assertEqual(edit["view"].children[0].style.name, "success")

        interaction = _interaction()
        await view.children[-1].callback(interaction)
        modal = interaction.response.modals[0]
        modal.memo_input._value = "○○テストセンター 9:30集合"
        await modal.on_submit(interaction)
        self.assertEqual(
            interaction.response.edits[0]["embed"].fields[0].value,
            "○○テストセンター 9:30集合",
        )
        self.assertEqual(
            get_exam_prep(self.db_path, 7, "SG", tomorrow.isoformat())["checked"],
            {0, 1, 2, 3},
        )

        other = _interaction(user_id=8)
        await view.children[0].callback(other)
        self.assertIn("見つかりません", other.response.sent[0]["content"])

    def test_exam_day_digest_shows_memo(self):
        today = _real_today()
        save_exam_date(self.db_path, 7, "SG", today, "t")
        set_prep_memo(self.db_path, 7, "SG", today.isoformat(), "○○センター 9:30")
        embed = digest_feature.build_daily_digest_embed(7, today)
        self.assertIn("📍 ○○センター 9:30", embed.description)
        self.assertEqual(get_checklist(self.db_path, 7, today), [])


if __name__ == "__main__":
    unittest.main()
