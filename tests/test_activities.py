import unittest
from datetime import date, datetime
from types import SimpleNamespace

from studybot import activity_forms, config
from studybot.activities import (
    activity_categories,
    build_activity_text,
    get_activities,
    parse_amount_input,
    save_activity,
    summarize_activities,
)
from studybot.config import SG_GLOSSARY_CATEGORIES
from studybot.database import delete_study_log_data
from studybot.exam_schedule import get_study_streak
from studybot.features import ai as ai_feature
from studybot.forms import SGStudyLogModal
from studybot.qualifications import FE, SG

from test_round3 import _Channel, _TempDBCase, _interaction


TODAY = date(2026, 10, 15)


class _LogChannel(_Channel):
    def __init__(self):
        super().__init__(20)
        self.name = "勉強ログ"
        self.deleted = False

    async def send(self, content=None, **kwargs):
        self.sent.append(dict(kwargs, content=content))
        channel = self

        async def delete():
            channel.deleted = True

        return SimpleNamespace(
            id=800 + len(self.sent),
            created_at=datetime(2026, 10, 15, 21, 0, tzinfo=config.JST),
            delete=delete,
        )


def _guild(channel):
    guild = SimpleNamespace(id=1, voice_channels=[], text_channels=[channel])
    guild.get_channel = lambda channel_id: None
    channel.guild = guild
    return guild


class ActivityDataTests(_TempDBCase):
    def test_categories_text_and_summary(self):
        self.assertEqual(activity_categories("review_past", SG), SG.category_names)
        self.assertEqual(activity_categories("terms", SG), SG_GLOSSARY_CATEGORIES)
        self.assertEqual(activity_categories("review_terms", FE), FE.category_names)
        self.assertIsNone(parse_amount_input(""))
        self.assertEqual(parse_amount_input("３０語"), 30)
        with self.assertRaises(ValueError):
            parse_amount_input("0")
        self.assertEqual(
            build_activity_text(SG, "review_terms", ["法務", "ネットワーク"], 30, 20, "略語"),
            "SG 復習（単語）：法務・ネットワーク。30語。20分。メモ：略語",
        )

        save_activity(self.db_path, 1, 7, SG, "review_past", ["ネットワーク", "データベース"],
                      12, None, "", TODAY)
        save_activity(self.db_path, 2, 7, SG, "terms", ["法務"], 20, 15, "", TODAY)
        save_activity(self.db_path, 3, 7, SG, "terms", ["セキュリティ"], None, None, "", TODAY)
        with self.assertRaises(ValueError):
            save_activity(self.db_path, 4, 7, SG, "terms", ["ネットワーク", "存在しない"],
                          None, None, "", TODAY)
        with self.assertRaises(ValueError):
            save_activity(self.db_path, 4, 7, SG, "terms", [], None, None, "", TODAY)
        rows = get_activities(self.db_path, 7, TODAY, TODAY, "SG")
        self.assertEqual(rows[0]["categories"], ["ネットワーク", "データベース"])
        self.assertEqual(
            summarize_activities(rows), "復習（過去問道場） 1回・12問 ／ 単語帳 2回・20語"
        )
        self.assertIsNone(summarize_activities([]))


class ActivityFlowTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.channel = _LogChannel()
        self.guild = _guild(self.channel)

    def interaction(self, user_id=7):
        interaction = _interaction(user_id)
        interaction.guild = self.guild
        interaction.user = SimpleNamespace(id=user_id, display_name="test",
                                           mention=f"<@{user_id}>")
        return interaction

    async def test_choose_kind_then_form(self):
        message, view = activity_forms.build_study_log_prompt(self.guild, 7, SG)
        self.assertIn("何を勉強しましたか", message)
        select = view.children[0]
        self.assertEqual([o.value for o in select.options], ["past", "review", "terms"])

        select._values = ["past"]
        interaction = self.interaction()
        await select.callback(interaction)
        self.assertIsInstance(interaction.response.modals[0], SGStudyLogModal)
        self.assertIsNotNone(interaction.response.modals[0].category_select)

        select._values = ["review"]
        interaction = self.interaction()
        await select.callback(interaction)
        review_view = interaction.response.edits[0]["view"]
        self.assertIn("何の復習", interaction.response.edits[0]["content"])
        interaction = self.interaction()
        await review_view.terms_button.callback(interaction)
        modal = interaction.response.modals[0]
        self.assertEqual(modal.kind, "review_terms")
        self.assertEqual(modal.category_select.max_values, len(SG_GLOSSARY_CATEGORIES))
        interaction = self.interaction()
        await review_view.past_button.callback(interaction)
        self.assertEqual(interaction.response.modals[0].category_select.max_values, 14)

        select._values = ["terms"]
        interaction = self.interaction()
        await select.callback(interaction)
        self.assertEqual(interaction.response.modals[0].kind, "terms")

        stranger = self.interaction(8)
        self.assertFalse(await view.interaction_check(stranger))

    async def test_submit_saves_log_and_activity(self):
        modal = activity_forms.ActivityModal("terms", self.channel, SG)
        modal.category_select._values = ["法務", "ネットワーク"]
        modal.amount_input._value = "25"
        modal.minutes_input._value = ""
        modal.memo_input._value = "略語が多い"
        interaction = self.interaction()
        await modal.on_submit(interaction)

        self.assertIn("SG 単語帳：法務・ネットワーク。25語。", self.channel.sent[0]["content"])
        self.assertIn("記録しました", interaction.followup.sent[0]["content"])
        rows = get_activities(self.db_path, 7, TODAY, TODAY)
        self.assertEqual(rows[0]["categories"], ["法務", "ネットワーク"])
        # 勉強ログの1件なので、連続学習日数に数える
        self.assertEqual(get_study_streak(self.db_path, 7, TODAY), 1)

        data = ai_feature.collect_weekly_report(7, "SG", TODAY)
        self.assertIn("単語帳 1回・25語", data["facts_text"])
        embed = ai_feature.build_weekly_report_embed(data, "本文")
        self.assertIn("そのほかの勉強", [field.name for field in embed.fields])

        delete_study_log_data(801)
        self.assertEqual(get_activities(self.db_path, 7, TODAY, TODAY), [])

        modal.amount_input._value = "たくさん"
        bad = self.interaction()
        await modal.on_submit(bad)
        self.assertIn("数は1〜", bad.response.sent[0]["content"])

    def test_missing_channel(self):
        other = _Channel(99)
        other.name = "雑談"
        message, view = activity_forms.build_study_log_prompt(_guild(other), 7, SG)
        self.assertIsNone(view)
        self.assertIn("/setup", message)


if __name__ == "__main__":
    unittest.main()
