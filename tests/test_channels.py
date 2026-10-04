import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from studybot import config
from studybot import database as database_module
from studybot.channels import (
    channel_label,
    find_channel,
    get_channel_setting,
    is_channel,
    set_channel_setting,
)
from studybot.features import setup as setup_feature


def _channel(channel_id, name, guild=None):
    return SimpleNamespace(
        id=channel_id, name=name, guild=guild, mention=f"<#{channel_id}>"
    )


class _Guild:
    def __init__(self):
        self.id = 1
        self.voice_channels = [_channel(10, "勉強部屋", self), _channel(11, "雑談VC", self)]
        self.text_channels = [_channel(20, "勉強ログ", self), _channel(21, "勉強記録", self)]

    def get_channel(self, channel_id):
        for channel in self.voice_channels + self.text_channels:
            if channel.id == channel_id:
                return channel
        return None


class _ChannelDBCase(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = str(Path(temp_dir.name) / "study.db")
        db_patch = patch.object(config, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        database_module.init_db()
        self.guild = _Guild()


class ChannelSettingTests(_ChannelDBCase):
    def test_falls_back_to_default_names(self):
        self.assertEqual(find_channel(self.guild, "study_voice").id, 10)
        self.assertEqual(find_channel(self.guild, "study_log").id, 20)
        self.assertIsNone(find_channel(self.guild, "glossary"))
        self.assertEqual(channel_label(self.guild, "study_log"), "#勉強ログ")
        self.assertIsNone(find_channel(None, "study_log"))

    def test_configured_channel_wins_even_after_rename(self):
        set_channel_setting(self.db_path, 1, "study_log", 21)
        self.assertEqual(get_channel_setting(self.db_path, 1, "study_log"), 21)

        log_channel = find_channel(self.guild, "study_log")
        self.assertEqual(log_channel.id, 21)
        self.assertEqual(channel_label(self.guild, "study_log"), "<#21>")
        # 名前が「勉強ログ」でも、設定した別チャンネルが優先される
        self.assertFalse(is_channel(self.guild.get_channel(20), "study_log"))
        log_channel.name = "名前を変えた"
        self.assertTrue(is_channel(log_channel, "study_log"))

    def test_deleted_or_cleared_setting_falls_back(self):
        set_channel_setting(self.db_path, 1, "study_voice", 999)
        self.assertEqual(find_channel(self.guild, "study_voice").id, 10)
        self.assertTrue(is_channel(self.guild.get_channel(10), "study_voice"))

        set_channel_setting(self.db_path, 1, "study_voice", 11)
        self.assertEqual(find_channel(self.guild, "study_voice").id, 11)
        set_channel_setting(self.db_path, 1, "study_voice", None)
        self.assertIsNone(get_channel_setting(self.db_path, 1, "study_voice"))
        self.assertEqual(find_channel(self.guild, "study_voice").id, 10)

    def test_name_match_ignores_case_and_needs_a_guild(self):
        guild = SimpleNamespace(id=2)
        self.assertTrue(is_channel(_channel(1, "sg用語集"), "glossary", guild))
        self.assertFalse(is_channel(_channel(1, "雑談"), "glossary", guild))
        self.assertFalse(is_channel(_channel(1, "勉強部屋"), "study_voice"))
        self.assertFalse(is_channel(None, "study_voice", guild))

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            set_channel_setting(self.db_path, 1, "unknown", 1)


class _Response:
    def __init__(self):
        self.edits = []

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


class SetupViewTests(_ChannelDBCase, unittest.IsolatedAsyncioTestCase):
    def test_embed_describes_each_channel(self):
        set_channel_setting(self.db_path, 1, "study_log", 21)
        fields = {
            field.name: field.value
            for field in setup_feature.build_setup_embed(self.guild).fields
        }
        self.assertEqual(len(fields), 5)
        values = list(fields.values())
        self.assertIn("<#10>（名前「勉強部屋」で自動検出）", values)
        self.assertIn("<#21>（設定済み）", values)
        self.assertTrue(any(v.startswith("見つかりません") for v in values))

    async def test_select_and_reset(self):
        view = setup_feature.SetupView(owner_id=5)
        self.assertEqual(
            [option.value for option in view.picker.options],
            ["study_voice", "study_log", "glossary", "ai_report", "focus"],
        )
        self.assertEqual(view.channel_select.kind, "study_voice")
        self.assertEqual(len(view.to_components()), 3)
        interaction = SimpleNamespace(
            guild=self.guild, user=SimpleNamespace(id=5), response=_Response()
        )

        with patch.object(
            type(view.picker), "values", new=["study_log"],
        ):
            await view.picker.callback(interaction)
        log_select = view.channel_select
        self.assertEqual(log_select.kind, "study_log")
        self.assertEqual(
            [option.default for option in view.picker.options],
            [False, True, False, False, False],
        )
        self.assertEqual(len(view.children), 4)

        with patch.object(
            type(log_select), "values",
            new=[SimpleNamespace(id=21)],
        ):
            await log_select.callback(interaction)
        self.assertEqual(get_channel_setting(self.db_path, 1, "study_log"), 21)
        self.assertIs(interaction.response.edits[-1]["view"], view)

        await view.reset_button.callback(interaction)
        self.assertIsNone(get_channel_setting(self.db_path, 1, "study_log"))


if __name__ == "__main__":
    unittest.main()
