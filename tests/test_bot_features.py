import unittest
from types import SimpleNamespace

import bot as bot_module


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


class PlanCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_uses_recorded_category_scores(self):
        original_get_status = bot_module.get_study_status
        original_get_week_total = (
            bot_module.get_week_total_seconds
        )
        original_ask_ollama = bot_module.ask_ollama
        captured_prompt = None

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

        bot_module.get_study_status = fake_get_status
        bot_module.get_week_total_seconds = lambda user_id: 3600
        bot_module.ask_ollama = fake_ask_ollama

        try:
            ctx = _Context()
            await bot_module.plan.callback(ctx, weeks=6)
        finally:
            bot_module.get_study_status = original_get_status
            bot_module.get_week_total_seconds = (
                original_get_week_total
            )
            bot_module.ask_ollama = original_ask_ollama

        self.assertIn("残り6週間", captured_prompt)
        self.assertIn(
            "テクノロジ系 > セキュリティ：40.0%",
            captured_prompt,
        )
        self.assertIn("SG合格まで6週間", ctx.messages[0])

    async def test_plan_rejects_out_of_range_weeks(self):
        ctx = _Context()

        await bot_module.plan.callback(ctx, weeks=0)

        self.assertIn("1〜16週", ctx.messages[0])


if __name__ == "__main__":
    unittest.main()
