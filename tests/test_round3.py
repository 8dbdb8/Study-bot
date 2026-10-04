import asyncio
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from studybot import config
from studybot import database as database_module
from studybot.ai_check import check_answer, checked_answer
from studybot.analysis import build_structured_sg_analysis
from studybot.charts import render_calendar, render_mock_chart
from studybot.daily_digest import take_rest_day
from studybot.embeds import build_today_embed
from studybot.exam_schedule import save_exam_date
from studybot.features import ai as ai_feature
from studybot.features import digest as digest_feature
from studybot.features import focus as focus_feature
from studybot.features import plan as plan_feature
from studybot.features import presence as presence_feature
from studybot.features import qualification as qualification_feature
from studybot.features import review as review_feature
from studybot.features import time as time_feature
from studybot.forms import SGMistakeModal, save_mistake_image
from studybot.habits import (
    format_goal_progress,
    get_focus_sets,
    get_rest_days,
    get_study_goal,
    goal_for_day,
    set_study_goal,
)
from studybot.qualifications import FE, IRYO, SG
from studybot.scoring import (
    has_recent_mock,
    list_mock_exams,
    predict_score,
    prediction_summary,
    save_mock_exam,
)
from studybot.sg_features import add_sg_mistake, get_sg_mistakes, save_sg_b_practice
from studybot.voice import build_study_end_extras


PNG = b"\x89PNG\r\n\x1a\n"
TODAY = date(2026, 10, 15)   # 木曜


class _TempDBCase(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.temp_dir = Path(temp_dir.name)
        self.db_path = str(self.temp_dir / "study.db")
        db_patch = patch.object(config, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        database_module.init_db()
        self.next_id = 2000

    def save_log(self, analysis, study_date="2026-10-14", user_id=7):
        self.next_id += 1
        message = SimpleNamespace(
            id=self.next_id,
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2),
            author=SimpleNamespace(id=user_id, display_name="test"),
            created_at=datetime.fromisoformat(f"{study_date}T10:00:00+09:00"),
            content="log",
        )
        database_module.save_study_log(message)
        database_module.save_study_analysis(message, analysis)
        return message

    def save_b(self, questions, correct, study_date="2026-10-14",
               qualification="SG", topic=None):
        message = self.save_log({
            "qualification": qualification, "exam_section": "B",
            "questions": questions, "correct_answers": correct,
            "score_percent": correct / questions * 100, "category_results": [],
        }, study_date)
        save_sg_b_practice(
            self.db_path, message.id, 7,
            topic or ("委託先管理" if qualification == "SG" else "情報セキュリティ"),
            questions, correct, "r" if correct < questions else "", "",
            study_date, qualification=qualification,
        )


class _Response:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.modals = []
        self.deferred = False

    async def send_message(self, content=None, **kwargs):
        self.sent.append(dict(kwargs, content=content))

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def defer(self, **kwargs):
        self.deferred = True


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(dict(kwargs, content=content))


def _interaction(user_id=7):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=_Response(),
        followup=_Followup(),
    )


class _Context:
    def __init__(self, user_id=7, interaction=True):
        self.author = SimpleNamespace(id=user_id)
        self.interaction = _interaction(user_id) if interaction else None
        self.channel = _Channel()
        self.guild = None
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append(dict(kwargs, content=content))

    def typing(self):
        return _Typing()


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Channel:
    def __init__(self, channel_id=1):
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(dict(kwargs, content=content))


# ------------------------------------------------------------
# 案1 予想得点
# ------------------------------------------------------------

class PredictionTests(_TempDBCase):
    def test_not_enough_data(self):
        self.save_log(build_structured_sg_analysis("ネットワーク", 10, 50.0))
        self.assertIsNone(predict_score(self.db_path, 7, SG, TODAY))
        self.assertIsNone(prediction_summary(None, SG))

    def test_sg_combines_a_and_b(self):
        self.save_log(build_structured_sg_analysis("ネットワーク", 40, 70.0))
        self.save_b(10, 5)
        # 古い記録は使わない
        self.save_log(build_structured_sg_analysis("データベース", 40, 0.0), "2026-09-01")

        prediction = predict_score(self.db_path, 7, SG, TODAY)
        # (0.7*48 + 0.5*12) / 60 * 1000 = 660
        self.assertEqual(prediction["score"], 660)
        self.assertEqual(prediction["margin"], 60)
        self.assertEqual(prediction["hint"], "科目Bの正答率を10%上げると ＋20点")
        self.assertEqual(
            prediction_summary(prediction, SG), "予想 660点（合格ライン＋60点）"
        )

    def test_missing_part_b_uses_part_a(self):
        self.save_log(build_structured_sg_analysis("ネットワーク", 25, 52.0))
        prediction = predict_score(self.db_path, 7, SG, TODAY)
        self.assertEqual(prediction["score"], 520)
        self.assertIn("科目Bの記録がない", prediction["note"])
        self.assertIn("あと80点", prediction_summary(prediction, SG))

    def test_fe_needs_both_parts(self):
        self.save_log(build_structured_sg_analysis(
            "ネットワーク", 30, 80.0, qualification="FE"
        ))
        self.save_b(10, 5, qualification="FE")
        prediction = predict_score(self.db_path, 7, FE, TODAY)
        self.assertIsNone(prediction["score"])
        self.assertEqual(prediction["part_scores"], [800, 500])
        self.assertEqual(prediction["margin"], -100)
        self.assertEqual(
            prediction_summary(prediction, FE),
            "予想 科目A 800点 / 科目B 500点（合格ラインまで あと100点）",
        )

    def test_iryo_has_no_pass_line(self):
        self.save_log(build_structured_sg_analysis(
            "医学・医療系", 30, 70.0, qualification="医療情報技師"
        ))
        prediction = predict_score(self.db_path, 7, IRYO, TODAY)
        self.assertEqual(prediction_summary(prediction, IRYO), "予想 700点")


# ------------------------------------------------------------
# 案2 模試
# ------------------------------------------------------------

class MockExamTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def test_save_and_score(self):
        _, score = save_mock_exam(self.db_path, 7, SG, TODAY, [(33, 48), (8, 12)], 104)
        self.assertEqual(score, 683)
        _, fe_score = save_mock_exam(self.db_path, 7, FE, TODAY, [(45, 60), (11, 20)], None)
        self.assertEqual(fe_score, 550)
        history = list_mock_exams(self.db_path, 7, "SG")
        self.assertEqual(history[0]["parts"], [(33, 48), (8, 12)])
        self.assertTrue(has_recent_mock(self.db_path, 7, "SG", TODAY + timedelta(days=6)))
        self.assertFalse(has_recent_mock(self.db_path, 7, "SG", TODAY + timedelta(days=7)))
        with self.assertRaises(ValueError):
            save_mock_exam(self.db_path, 7, SG, TODAY, [(50, 48), (0, 12)], None)

    async def test_modal_records_and_replies_with_chart(self):
        modal = qualification_feature.MockExamModal(SG)
        self.assertEqual(len(modal.to_dict()["components"]), 3)
        modal.part_inputs[0][1]._value = "３３"
        modal.part_inputs[1][1]._value = "8"
        modal.minutes_input._value = "104"
        interaction = _interaction()
        await modal.on_submit(interaction)

        self.assertTrue(interaction.response.deferred)
        sent = interaction.followup.sent[0]
        self.assertEqual(
            sent["embed"].title, "SG 模試 第1回 ・ 683点（合格ライン＋83点）"
        )
        self.assertEqual(sent["file"].filename, "mock.png")

    async def test_iryo_modal_asks_question_count(self):
        modal = qualification_feature.MockExamModal(IRYO)
        self.assertEqual(len(modal.to_dict()["components"]), 3)
        modal.part_inputs[0][1]._value = "40"
        modal.part_inputs[0][2]._value = "60"
        modal.minutes_input._value = ""
        self.assertEqual(modal.parse(), ([(40, 60)], None))
        modal.part_inputs[0][1]._value = "abc"
        with self.assertRaises(ValueError):
            modal.parse()

    async def test_command_opens_modal_or_explains(self):
        ctx = _Context()
        await qualification_feature.COMMANDS["FE"]["mock"].callback(ctx)
        self.assertIsInstance(
            ctx.interaction.response.modals[0], qualification_feature.MockExamModal
        )
        prefix = _Context(interaction=False)
        await qualification_feature.COMMANDS["SG"]["mock"].callback(prefix)
        self.assertIn("/sg mock", prefix.messages[0]["content"])

    def test_chart_and_final_stretch_suggestion(self):
        png = render_mock_chart([{"label": "10/5", "score": 540}], "SG 模試", 600)
        self.assertTrue(png.startswith(PNG))
        focus = digest_feature.final_stretch_focus([], suggest_mock=True)
        self.assertEqual(focus[-1], ("模試（本番形式）", "今週1回"))


class StatusPredictionTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    async def test_status_shows_prediction(self):
        today = datetime.now(config.JST).date()
        self.save_log(
            build_structured_sg_analysis("ネットワーク", 30, 70.0), today.isoformat()
        )
        ctx = _Context()
        await qualification_feature.show_status(ctx, SG)
        fields = {f.name: f.value for f in ctx.messages[0]["embed"].fields}
        self.assertIn("予想 700点（合格ライン＋100点）", fields["予想得点"])
        self.assertIn("本番の採点方式とは異なります", fields["予想得点"])

    def test_digest_extra_lines(self):
        today = datetime.now(config.JST).date()
        self.save_log(
            build_structured_sg_analysis("ネットワーク", 30, 70.0), today.isoformat()
        )
        set_study_goal(self.db_path, 7, 30, 90)
        lines = digest_feature.digest_extra_lines(7, SG, today)
        self.assertTrue(lines[0].startswith("📈 予想 700点"))
        self.assertTrue(lines[1].startswith("🎯 今日の目標 `"))


# ------------------------------------------------------------
# 案3 AIの振り返り
# ------------------------------------------------------------

class AICheckTests(_TempDBCase, unittest.TestCase):
    def test_check_answer(self):
        source = "平均正答率：60.2%\n- 情報セキュリティ：60.0%\n要復習候補（60%未満）"
        answer = (
            "### 今週の課題\n"
            "- 情報セキュリティの正答率が60.0%で、60%未満のラインに達している\n"
            "- 平均正答率は60.2%\n"
            "- 正答率が75%に伸びた\n"
            "- 60%未満の分野はありません\n"
        )
        text, removed = check_answer(answer, source)
        self.assertEqual(len(removed), 2)
        self.assertIn("### 今週の課題", text)
        self.assertIn("60.2%", text)
        self.assertIn("60%未満の分野はありません", text)
        self.assertIn("2行省きました", checked_answer(answer, source))
        self.assertEqual(checked_answer("問題なし", source), "問題なし")

    def test_weekly_facts(self):
        self.save_log(build_structured_sg_analysis("ネットワーク", 30, 45.0), "2026-10-13")
        take_rest_day(self.db_path, 7, date(2026, 10, 14))
        status = {"start_date": "2026-10-12", "end_date": "2026-10-15"}
        facts, prediction = ai_feature.build_weekly_facts(
            7, "SG", status, [("2026-10-13", 3600)], TODAY
        )
        self.assertIn("- 集計期間：4日間（10/12〜10/15）", facts)
        self.assertIn("- 勉強部屋で勉強した日：1日", facts)
        self.assertIn("- 「今日は休む」で休みにした日：1日", facts)
        self.assertIn("ネットワーク（45.0%）", facts)
        self.assertEqual(prediction, "予想 450点（合格ラインまで あと150点）")

        data = {
            "start_date": "2026-10-12", "end_date": "2026-10-15",
            "total_seconds": 3600, "daily_text": "", "log_count": 1,
            "total_questions": 30, "score_text": "45.0%",
            "category_text": "", "review_text": "", "facts_text": facts,
            "prediction_text": prediction,
        }
        self.assertIn("【確定した事実（Botの集計）】", ai_feature.build_weekly_report_prompt(data))
        embed = ai_feature.build_weekly_report_embed(data, "本文")
        self.assertEqual(embed.fields[-1].name, "予想得点（目安）")


# ------------------------------------------------------------
# 案4 誤答の画像
# ------------------------------------------------------------

class _Attachment:
    def __init__(self, filename, content_type="image/png"):
        self.filename = filename
        self.content_type = content_type

    async def save(self, path):
        Path(path).write_bytes(PNG + b"test")


class MistakeImageTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch("studybot.forms.MISTAKE_IMAGE_DIR", self.temp_dir / "images")
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_save_image(self):
        path = await save_mistake_image(_Attachment("shot.PNG"), 5)
        self.assertTrue(path.endswith("5.png"))
        self.assertTrue(Path(path).exists())
        with self.assertRaises(ValueError):
            await save_mistake_image(_Attachment("memo.txt", "text/plain"), 6)

    async def test_form_saves_image_and_review_shows_it(self):
        modal = SGMistakeModal("ネットワーク")
        payload = modal.to_dict()
        self.assertEqual(payload["components"][-1]["component"]["type"], 19)
        modal.reference_input._value = "令和6年 問12"
        modal.reason_input._value = "読み違えた"
        modal.memo_input._value = ""
        modal.image_input._values = [_Attachment("shot.jpg", "image/jpeg")]
        interaction = _interaction()
        await modal.on_submit(interaction)
        self.assertIn("（画像つき）", interaction.followup.sent[0]["content"])

        future = datetime.now(config.JST).date() + timedelta(days=1)
        items = get_sg_mistakes(self.db_path, 7, today=future)
        self.assertTrue(items[0]["image_path"].endswith(".jpg"))

        add_sg_mistake(self.db_path, 7, "データベース", "q2", "r", today=TODAY)
        items = get_sg_mistakes(self.db_path, 7, today=future)
        embed, view = review_feature.build_review_session(7, future, items)
        self.assertEqual(embed.image.url, "attachment://mistake.jpg")
        self.assertEqual(view.files()[0].filename, "mistake.jpg")

        step = _interaction()
        await view.skip_button.callback(step)
        self.assertEqual(step.response.edits[0]["attachments"], [])
        self.assertIsNone(step.response.edits[0]["embed"].image.url)


# ------------------------------------------------------------
# 案5 学習カレンダー
# ------------------------------------------------------------

class CalendarTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def test_render(self):
        png = render_calendar({TODAY: 3600}, {TODAY - timedelta(days=1)}, TODAY, 5)
        self.assertTrue(png.startswith(PNG))
        self.assertEqual(
            get_rest_days(self.db_path, 7, TODAY, TODAY), set()
        )

    async def test_command(self):
        ctx = _Context()
        await time_feature.time_calendar.callback(ctx, weeks=5)
        self.assertEqual(ctx.messages[0]["file"].filename, "calendar.png")


# ------------------------------------------------------------
# 案6 1日の目標時間
# ------------------------------------------------------------

class GoalTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def test_goal_settings(self):
        self.assertEqual(get_study_goal(self.db_path, 7), (None, None))
        set_study_goal(self.db_path, 7, weekday_minutes=30)
        set_study_goal(self.db_path, 7, holiday_minutes=90)
        self.assertEqual(get_study_goal(self.db_path, 7), (30, 90))
        self.assertEqual(goal_for_day(self.db_path, 7, TODAY), 30)
        self.assertEqual(goal_for_day(self.db_path, 7, date(2026, 10, 17)), 90)
        set_study_goal(self.db_path, 7, weekday_minutes=0)
        self.assertEqual(get_study_goal(self.db_path, 7), (None, 90))
        with self.assertRaises(ValueError):
            set_study_goal(self.db_path, 7, weekday_minutes=1000)

    def test_progress_text(self):
        self.assertIsNone(format_goal_progress(600, None))
        self.assertEqual(format_goal_progress(15 * 60, 30), "`▰▰▰▰▰▱▱▱▱▱` 15/30分")
        self.assertEqual(format_goal_progress(45 * 60, 30), "`▰▰▰▰▰▰▰▰▰▰` 45/30分 ✓ 達成")

    async def test_plan_goal_command_and_displays(self):
        ctx = _Context()
        await plan_feature.plan_goal.callback(ctx, weekday=30, holiday=90)
        self.assertIn("平日：**30分**", ctx.messages[0]["content"])
        await plan_feature.plan_goal.callback(ctx, weekday=999)
        self.assertIn("0〜720分", ctx.messages[1]["content"])

        embed = build_today_embed(1800, 3, None, format_goal_progress(1800, 30))
        self.assertEqual(embed.fields[1].name, "今日の目標")

        extras = build_study_end_extras(7, datetime.now(config.JST).date(), 1800)
        self.assertEqual(extras[0][0], "今日の目標")


# ------------------------------------------------------------
# 案7 Botのステータス
# ------------------------------------------------------------

class PresenceTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def test_text(self):
        self.assertIsNone(presence_feature.presence_user(self.db_path))
        self.save_log(build_structured_sg_analysis("ネットワーク", 10, 50.0), "2026-10-14")
        self.save_log(build_structured_sg_analysis("ネットワーク", 10, 50.0), "2026-10-15")
        self.assertEqual(presence_feature.presence_user(self.db_path), 7)
        self.assertEqual(presence_feature.build_presence_text(7, TODAY), "連続学習 2日")

        save_exam_date(self.db_path, 7, "SG", date(2026, 10, 17), "t")
        self.assertEqual(
            presence_feature.build_presence_text(7, TODAY), "SG試験まで あと2日 ・ 連続2日"
        )
        self.assertEqual(
            presence_feature.build_presence_text(7, date(2026, 10, 17)),
            "今日はSG試験！ がんばって",
        )

    async def test_update(self):
        self.save_log(build_structured_sg_analysis("ネットワーク", 10, 50.0))
        calls = []

        async def change_presence(activity):
            calls.append(activity.name)

        bot = SimpleNamespace(change_presence=change_presence)
        text = await presence_feature.update_presence(
            bot, datetime(2026, 10, 15, 0, 0, tzinfo=config.JST)
        )
        self.assertEqual(calls, [text])


# ------------------------------------------------------------
# 案8 集中タイマー
# ------------------------------------------------------------

class FocusTimerTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.waits = []

        async def fake_sleep(seconds):
            self.waits.append(seconds)

        patcher = patch.object(focus_feature, "_sleep", fake_sleep)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(focus_feature.FOCUS_TIMERS.clear)

    async def test_runs_sets_and_records(self):
        ctx = _Context()
        await focus_feature.focus.callback(ctx, minutes=25, break_minutes=5, sets=2)
        self.assertIn("25分 × 2セット", ctx.messages[0]["content"])
        await focus_feature.FOCUS_TIMERS[7]["task"]

        self.assertEqual(self.waits, [1500, 300, 1500])
        texts = [sent["content"] for sent in ctx.channel.sent]
        self.assertIn("1セット目おわり", texts[0])
        self.assertIn("2セット目スタート", texts[1])
        self.assertIn("2セット完了", texts[2])
        today = datetime.now(config.JST).date()
        self.assertEqual(get_focus_sets(self.db_path, 7, today), (2, 50))
        self.assertNotIn(7, focus_feature.FOCUS_TIMERS)

    async def test_uses_focus_channel(self):
        focus_channel = _Channel(55)
        ctx = _Context()
        ctx.guild = SimpleNamespace(id=1)
        with patch.object(
            focus_feature, "find_channel", lambda guild, kind: focus_channel
        ):
            await focus_feature.focus.callback(ctx, sets=1)
        self.assertIn("お知らせは <#55> に届きます", ctx.messages[0]["content"])
        await focus_feature.FOCUS_TIMERS[7]["task"]
        self.assertIn("1セット完了", focus_channel.sent[0]["content"])
        self.assertEqual(ctx.channel.sent, [])

    async def test_stop_and_validation(self):
        gate = asyncio.Event()

        async def slow_sleep(seconds):
            await gate.wait()

        with patch.object(focus_feature, "_sleep", slow_sleep):
            ctx = _Context()
            await focus_feature.focus.callback(ctx)
            await focus_feature.focus.callback(ctx)
            self.assertIn("すでに動いています", ctx.messages[-1]["content"])
            await focus_feature.focus.callback(ctx, stop=True)
            self.assertIn("止めました（0/4セット完了）", ctx.messages[-1]["content"])

        await focus_feature.focus.callback(ctx, stop=True)
        self.assertIn("ありません", ctx.messages[-1]["content"])
        await focus_feature.focus.callback(ctx, minutes=1)
        self.assertIn("5〜120分", ctx.messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
