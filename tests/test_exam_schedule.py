import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot
from exam_schedule import (
    build_exam_date,
    delete_exam_date,
    format_exam_countdown,
    format_plan_schedule,
    get_exam_date,
    get_study_streak,
    save_exam_date,
    strip_week_heading_dates,
    weeks_until,
)


class ExamDateLogicTests(unittest.TestCase):
    def test_build_exam_date_validates_choices(self):
        today = date(2026, 10, 4)
        self.assertEqual(
            build_exam_date(2026, 11, 25, today), date(2026, 11, 25)
        )
        self.assertEqual(build_exam_date(2026, 10, 4, today), today)
        with self.assertRaisesRegex(ValueError, "過去"):
            build_exam_date(2026, 10, 3, today)
        with self.assertRaisesRegex(ValueError, "ありません"):
            build_exam_date(2027, 2, 29, today)
        with self.assertRaisesRegex(ValueError, "年"):
            build_exam_date(2030, 1, 1, today)

    def test_weeks_until_rounds_up(self):
        today = date(2026, 10, 4)
        self.assertEqual(weeks_until(date(2026, 10, 5), today), 1)
        self.assertEqual(weeks_until(date(2026, 10, 18), today), 2)
        self.assertEqual(weeks_until(date(2026, 10, 19), today), 3)
        self.assertEqual(weeks_until(today, today), 1)

    def test_countdown_text(self):
        today = date(2026, 10, 4)
        self.assertEqual(
            format_exam_countdown(date(2026, 11, 25), today, "SG試験"),
            "SG試験：2026年11月25日（水）　あと**52日**（約8週間）",
        )
        self.assertIn("今日が試験日", format_exam_countdown(today, today))
        self.assertIn(
            "3日前に終了",
            format_exam_countdown(date(2026, 10, 1), today),
        )


class PlanScheduleTests(unittest.TestCase):
    def test_schedule_ends_on_exam_date(self):
        # 10/4に作成・試験日10/17 → 2週間で10/17まで
        self.assertEqual(
            format_plan_schedule(date(2026, 10, 4), 2, date(2026, 10, 17)),
            "第1週：10/4（日）〜10/10（土）\n"
            "第2週：10/11（日）〜10/17（土） ※最終日が試験日",
        )

    def test_last_week_is_cut_at_exam_date(self):
        self.assertEqual(
            format_plan_schedule(date(2026, 10, 4), 2, date(2026, 10, 14))
            .splitlines()[-1],
            "第2週：10/11（日）〜10/14（水） ※最終日が試験日",
        )

    def test_strips_dates_written_by_ai(self):
        text = (
            "**第1週（10月10日〜10月16日）**\n- 基本目標\n"
            "**第2週(10/17〜10/23)**\n第3週（総合演習）"
        )
        self.assertEqual(
            strip_week_heading_dates(text),
            "**第1週**\n- 基本目標\n**第2週**\n第3週（総合演習）",
        )


class ExamDateDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = str(Path(self.temp_dir.name) / "study.db")
        db_patch = patch.object(bot, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        bot.init_db()

    def test_save_overwrite_and_delete_per_user(self):
        save_exam_date(self.db_path, 1, "SG", date(2026, 11, 25), "t1")
        save_exam_date(self.db_path, 1, "SG", date(2026, 12, 2), "t2")
        save_exam_date(self.db_path, 2, "SG", date(2027, 1, 10), "t3")

        self.assertEqual(
            get_exam_date(self.db_path, 1, "SG"), date(2026, 12, 2)
        )
        self.assertEqual(
            get_exam_date(self.db_path, 2, "SG"), date(2027, 1, 10)
        )
        self.assertIsNone(get_exam_date(self.db_path, 1, "FE"))

        self.assertTrue(delete_exam_date(self.db_path, 1, "SG"))
        self.assertIsNone(get_exam_date(self.db_path, 1, "SG"))
        self.assertFalse(delete_exam_date(self.db_path, 1, "SG"))

    def test_current_exam_target_uses_current_qualification(self):
        save_exam_date(self.db_path, 1, "SG", date(2026, 11, 25), "t")
        target = bot.get_current_exam_target(1)
        self.assertEqual(target["qualification"], "SG")
        self.assertEqual(target["label"], "SG試験")
        self.assertEqual(target["exam_on"], date(2026, 11, 25))

    def _add_session(self, user_id, study_date, seconds=600):
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("""
                    INSERT INTO study_sessions (
                        guild_id, user_id, username, study_date,
                        start_time, end_time, duration_seconds
                    ) VALUES (1, ?, 'test', ?, 's', 'e', ?)
                """, (user_id, study_date, seconds))

    def _add_log(self, user_id, study_date, message_id):
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("""
                    INSERT INTO study_logs (
                        guild_id, channel_id, message_id, user_id,
                        username, study_date, content, created_at
                    ) VALUES (1, 2, ?, ?, 'test', ?, 'log', 'c')
                """, (message_id, user_id, study_date))

    def test_streak_counts_sessions_and_logs(self):
        today = date(2026, 10, 4)
        self._add_session(1, "2026-10-04")
        self._add_log(1, "2026-10-03", 10)
        self._add_session(1, "2026-10-02")
        self._add_session(1, "2026-09-30")  # 10/1 が空いている
        self._add_session(2, "2026-10-01")  # 別ユーザー

        self.assertEqual(get_study_streak(self.db_path, 1, today), 3)

    def test_streak_survives_until_today_is_recorded(self):
        today = date(2026, 10, 4)
        self._add_session(1, "2026-10-03")
        self._add_session(1, "2026-10-02")
        self._add_session(1, "2026-10-01", seconds=0)

        self.assertEqual(get_study_streak(self.db_path, 1, today), 2)
        self.assertEqual(
            get_study_streak(self.db_path, 1, date(2026, 10, 5)), 0
        )


class _Response:
    def __init__(self):
        self.edits = []

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


class ExamDateViewTests(unittest.IsolatedAsyncioTestCase):
    def make_view(self, exam_on=None, today=date(2026, 10, 4)):
        target = {
            "qualification": "SG",
            "display_name": "情報セキュリティマネジメント（SG）",
            "label": "SG試験",
            "exam_on": exam_on,
        }
        return bot.ExamDateView(123, target, today)

    def selects(self, view):
        return [
            child for child in view.children
            if isinstance(child, bot.ExamDatePartSelect)
        ]

    def save_button(self, view):
        return next(
            child for child in view.children
            if getattr(child, "label", None) == "この日で保存"
        )

    def test_starts_empty_with_month_and_day_disabled(self):
        view = self.make_view()
        year, month, day = self.selects(view)
        self.assertEqual(
            [option.label for option in year.options],
            ["2026年", "2027年", "2028年"],
        )
        self.assertTrue(month.disabled)
        self.assertTrue(day.disabled)
        self.assertTrue(self.save_button(view).disabled)

    async def test_selecting_parts_builds_a_savable_date(self):
        view = self.make_view()
        interaction = SimpleNamespace(response=_Response())

        await view.select_part(interaction, "year", 2026)
        month = self.selects(view)[1]
        # 今年は今月以降だけ選べる
        self.assertEqual(month.options[0].label, "10月")
        self.assertEqual(len(month.options), 3)

        await view.select_part(interaction, "month", 12)
        day_selects = self.selects(view)[2:]
        # 31日は25件を超えるので2つに分ける
        self.assertEqual(len(day_selects), 2)
        self.assertEqual(day_selects[0].options[0].label, "1日（火）")
        self.assertEqual(day_selects[1].options[-1].label, "31日（木）")

        await view.select_part(interaction, "day", 24)
        self.assertEqual(view.selected_date(), date(2026, 12, 24))
        self.assertFalse(self.save_button(view).disabled)
        self.assertIn("あと**81日**", interaction.response.edits[-1]["content"])

    async def test_changing_month_clears_impossible_day(self):
        view = self.make_view(exam_on=date(2027, 1, 31))
        interaction = SimpleNamespace(response=_Response())

        await view.select_part(interaction, "month", 2)

        self.assertIsNone(view.day)
        self.assertTrue(self.save_button(view).disabled)
        day_options = [
            option for select in self.selects(view)[2:]
            for option in select.options
        ]
        self.assertEqual(len(day_options), 28)

    def test_current_month_only_offers_remaining_days(self):
        view = self.make_view(exam_on=date(2026, 10, 20))
        day_options = [
            option for select in self.selects(view)[2:]
            for option in select.options
        ]
        self.assertEqual(day_options[0].label, "4日（日）")
        self.assertEqual(len(day_options), 28)
        self.assertTrue(
            any(o.default and o.value == "20" for o in day_options)
        )


class _TypingContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Context:
    def __init__(self):
        self.author = SimpleNamespace(id=123)
        self.messages = []

    def typing(self):
        return _TypingContext()

    async def send(self, content):
        self.messages.append(content)


class PlanFromExamDateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = None
        self.prompt = None

        def fake_save_plan(db_path, user_id, weeks, weekly_questions,
                           created_at, today=None):
            self.saved = (weeks, weekly_questions)
            return 1

        async def fake_ask(prompt):
            self.prompt = prompt
            return "計画本文"

        for name, value in {
            "get_study_status": lambda user_id, q: {
                "total_questions": 0,
                "average_score": None,
                "category_status": [],
            },
            "get_week_total_seconds": lambda user_id: 0,
            "save_sg_plan": fake_save_plan,
            "update_sg_plan_text": lambda *args: None,
            "ask_ollama": fake_ask,
        }.items():
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def set_exam(self, exam_on):
        patcher = patch.object(
            bot, "get_current_exam_target",
            lambda user_id: {
                "qualification": "SG", "display_name": "SG",
                "label": "SG試験", "exam_on": exam_on,
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_weeks_omitted_uses_exam_date(self):
        today = bot.datetime.now(bot.JST).date()
        exam_on = date.fromordinal(today.toordinal() + 40)
        self.set_exam(exam_on)
        ctx = _Context()

        await bot.plan.callback(ctx)

        self.assertEqual(self.saved, (6, 30))
        self.assertIn("【試験日】", self.prompt)
        self.assertIn("【各週の期間（Botが計算済み）】", self.prompt)
        self.assertIn("**各週の期間**", ctx.messages[0])
        self.assertIn("※最終日が試験日", ctx.messages[0])
        self.assertIn("あと**40日**", ctx.messages[0])

    async def test_weeks_omitted_without_exam_date_asks_to_set(self):
        self.set_exam(None)
        ctx = _Context()

        await bot.plan.callback(ctx)

        self.assertIsNone(self.saved)
        self.assertIn("/plan exam", ctx.messages[0])

    async def test_far_exam_is_capped_to_16_weeks(self):
        today = bot.datetime.now(bot.JST).date()
        self.set_exam(date.fromordinal(today.toordinal() + 200))
        ctx = _Context()

        await bot.plan.callback(ctx)

        self.assertEqual(self.saved, (16, 30))
        self.assertIn("直近16週分", ctx.messages[0])


if __name__ == "__main__":
    unittest.main()
