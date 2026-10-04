import unittest
from types import SimpleNamespace

from studybot import analysis as analysis_module
from studybot import forms
from studybot import sg_features
from studybot import study_log_parser
from studybot.features import plan as plan_feature
from studybot.app import create_bot


BOT = create_bot()


class _TypingContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Context:
    def __init__(self):
        self.author = SimpleNamespace(id=123)
        self.messages = []

    def typing(self):
        return _TypingContext()

    async def send(self, content):
        self.messages.append(content)


class StructuredSGLogTests(unittest.TestCase):
    def test_sglog_command_is_registered(self):
        self.assertIsNotNone(
            BOT.get_command("sg log")
        )

    def test_sg_study_commands_are_registered(self):
        for name in (
            "review add", "review list", "review answer",
            "sg progress", "sg b", "plan status",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(
                    BOT.get_command(name)
                )

        b_view = forms.SGBPracticeView(123, object())
        topic_select = next(
            child for child in b_view.children
            if isinstance(child, forms.SGBTopicSelect)
        )
        self.assertEqual(
            len(topic_select.options),
            len(sg_features.SG_B_TOPICS),
        )

    def test_sglog_view_has_all_practice_categories(self):
        view = forms.SGStudyLogView(
            owner_id=123,
            target_channel=object(),
        )
        select = next(
            child
            for child in view.children
            if isinstance(child, forms.SGCategorySelect)
        )

        self.assertEqual(len(select.options), 14)
        self.assertEqual(
            [option.value for option in select.options],
            list(study_log_parser.SG_PRACTICE_CATEGORIES),
        )

    def test_builds_exact_structured_analysis(self):
        analysis = analysis_module.build_structured_sg_analysis(
            "情報セキュリティ",
            25,
            40.3,
            "アクセス制御を復習",
        )

        self.assertEqual(analysis["qualification"], "SG")
        self.assertEqual(analysis["questions"], 25)
        self.assertEqual(analysis["score_percent"], 40.3)
        self.assertIsNone(analysis["correct_answers"])
        self.assertEqual(
            analysis["category_results"],
            [
                {
                    "major_category": "テクノロジ系",
                    "category": "情報セキュリティ",
                    "questions": 25,
                    "correct_answers": None,
                    "score_percent": 40.3,
                }
            ],
        )
        self.assertEqual(
            analysis["notes"],
            "アクセス制御を復習",
        )


class PlanCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_uses_recorded_category_scores(self):
        original_get_status = plan_feature.get_study_status
        original_get_week_total = (
            plan_feature.get_week_total_seconds
        )
        original_ask_ollama = plan_feature.ask_ollama
        original_save_plan = plan_feature.save_sg_plan
        original_update_plan = plan_feature.update_sg_plan_text
        captured_prompt = None
        saved_plan = None

        def fake_get_status(user_id, qualification):
            self.assertEqual(user_id, 123)
            self.assertEqual(qualification, "SG")
            return {
                "total_questions": 55,
                "average_score": 44.8,
                "category_status": [
                    {
                        "major_category": "テクノロジ系",
                        "category": "セキュリティ",
                        "average_score": 40.0,
                        "log_count": 3,
                        "scored_log_count": 2,
                    }
                ],
            }

        async def fake_ask_ollama(prompt):
            nonlocal captured_prompt
            captured_prompt = prompt
            return "計画本文"

        def fake_save_plan(db_path, user_id, weeks, weekly_questions,
                           created_at, today=None, qualification="SG"):
            nonlocal saved_plan
            saved_plan = (user_id, weeks, weekly_questions)
            return 1

        plan_feature.get_study_status = fake_get_status
        plan_feature.get_week_total_seconds = lambda user_id: 3600
        plan_feature.ask_ollama = fake_ask_ollama
        plan_feature.save_sg_plan = fake_save_plan
        plan_feature.update_sg_plan_text = lambda *args: None

        try:
            ctx = _Context()
            await plan_feature.plan.callback(ctx, weeks=6)
        finally:
            plan_feature.get_study_status = original_get_status
            plan_feature.get_week_total_seconds = (
                original_get_week_total
            )
            plan_feature.ask_ollama = original_ask_ollama
            plan_feature.save_sg_plan = original_save_plan
            plan_feature.update_sg_plan_text = original_update_plan

        self.assertIn("残り6週間", captured_prompt)
        self.assertIn(
            "テクノロジ系 > セキュリティ：40.0%",
            captured_prompt,
        )
        self.assertIn("SG合格まで6週間", ctx.messages[0])
        self.assertEqual(saved_plan, (123, 6, 30))

    async def test_plan_rejects_out_of_range_weeks(self):
        ctx = _Context()

        await plan_feature.plan.callback(ctx, weeks=0)

        self.assertIn("1〜16週", ctx.messages[0])


if __name__ == "__main__":
    unittest.main()
