import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot as bot_module
from sg_glossary import (
    GLOSSARY_PATH, GlossaryEntry, get_sg_glossary_ratings,
    glossary_entry_key, load_glossary,
)
from sg_glossary_history import get_glossary_session


RATING_VALUES = ("できた", "できなかった", "まだ要復習", "微妙")
RATING_LABELS = ("できた", "できなかった", "要復習", "微妙")


class _Context:
    def __init__(self, channel_name="SG用語集", interaction=True):
        self.guild = SimpleNamespace(id=1)
        self.channel = SimpleNamespace(id=7, name=channel_name)
        self.author = SimpleNamespace(id=123)
        self.interaction = object() if interaction else None
        self.messages = []

    async def send(self, content, **kwargs):
        self.messages.append((content, kwargs))


class GlossaryCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reader = patch.object(
            bot_module, "get_sg_glossary_ratings", return_value={},
        )
        reader.start()
        self.addCleanup(reader.stop)

    async def test_command_selects_category_and_card_mode(self):
        self.assertIsNotNone(bot_module.bot.get_command("sg glossary"))
        entries = [
            GlossaryEntry("用語A", "意味A", "セキュリティ"),
            GlossaryEntry("用語B", "", "法務", "https://example.com/b"),
        ]
        ctx = _Context(channel_name="sg用語集")
        with patch.object(bot_module, "load_glossary", return_value=entries):
            await bot_module.sgglossary.callback(
                ctx, mode="cards", category="法務"
            )
        content, kwargs = ctx.messages[0]
        self.assertIn("用語B", content)
        self.assertNotIn("用語A", content)
        self.assertIn("対象: 法務", content)
        self.assertIn("件）\n\n**用語B**\n> 法務", content)
        self.assertTrue(kwargs["ephemeral"])
        view = kwargs["view"]
        self.assertEqual(view.mode, "cards")
        self.assertEqual(len(view.entries), 1)
        self.assertIs(view.record_channel, ctx.channel)
        self.assertEqual(view.guild_id, ctx.guild.id)
        self.assertEqual(view.history_db_path, bot_module.GLOSSARY_HISTORY_PATH)
        view.revealed = True
        self.assertIn("意味は未登録です", view.content())
        self.assertIn("https://example.com/b", view.content())

    async def test_default_shows_all_categories(self):
        entries = [
            GlossaryEntry("用語A", "意味A", "セキュリティ"),
            GlossaryEntry("用語B", "意味B", "法務"),
        ]
        ctx = _Context()
        with patch.object(bot_module, "load_glossary", return_value=entries):
            await bot_module.sgglossary.callback(ctx)
        self.assertIn("全分野 / 2件", ctx.messages[0][0])
        self.assertEqual(len(ctx.messages[0][1]["view"].entries), 2)

    async def test_command_reloads_saved_self_rating(self):
        entry = GlossaryEntry("用語A", "意味A", "セキュリティ")
        ratings = {glossary_entry_key(entry): "微妙"}
        ctx = _Context()
        with (
            patch.object(bot_module, "load_glossary", return_value=[entry]),
            patch.object(
                bot_module, "get_sg_glossary_ratings", return_value=ratings,
            ) as reader,
        ):
            await bot_module.sgglossary.callback(ctx, mode="cards")
        reader.assert_called_once_with(bot_module.DB_PATH, 123, [entry])
        view = ctx.messages[0][1]["view"]
        view.revealed = True
        self.assertIn("自己評価: 微妙", view.content())

    async def test_legacy_review_rating_uses_short_visible_label(self):
        entry = GlossaryEntry("用語A", "意味A", "セキュリティ")
        view = bot_module.SGGlossaryView(
            123, [entry], mode="cards",
            ratings={glossary_entry_key(entry): "まだ要復習"},
        )
        view.revealed = True
        self.assertIn("自己評価: 要復習", view.content())
        self.assertNotIn("まだ要復習", view.content())
        self.assertEqual(view.rated_review.label, "要復習")
        self.assertEqual(
            [(option.label, option.value)
             for option in view.rating_filter_select.options],
            list(zip(RATING_LABELS, RATING_VALUES)),
        )

    async def test_rating_filter_selects_one_or_multiple_saved_ratings(self):
        entries = [
            GlossaryEntry("できた語", "意味"),
            GlossaryEntry("できなかった語", "意味"),
            GlossaryEntry("復習語", "意味"),
            GlossaryEntry("微妙語", "意味"),
            GlossaryEntry("未評価語", "意味"),
        ]
        ratings = {
            glossary_entry_key(entry): rating
            for entry, rating in zip(entries, RATING_VALUES)
        }
        view = bot_module.SGGlossaryView(
            123, entries, mode="cards", ratings=ratings,
        )
        select = view.rating_filter_select
        self.assertIn(select, view.children)
        self.assertEqual((select.min_values, select.max_values), (1, 4))
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                edit_message=AsyncMock(), send_message=AsyncMock(),
            ),
        )

        await view._apply_rating_filter(interaction, ("まだ要復習",))
        self.assertEqual(view.entries, (entries[2],))
        self.assertIn("評価（選択時）: 要復習 / 1/1件", view.content())
        self.assertIn(view.clear_rating_filter, view.children)
        self.assertTrue(select.options[2].default)

        await view._apply_rating_filter(
            interaction, ("できた", "まだ要復習"),
        )
        self.assertEqual(view.entries, (entries[0], entries[2]))
        self.assertIn("評価（選択時）: できた・要復習 / 1/2件", view.content())
        self.assertEqual(view.card_index, 0)
        self.assertTrue(select.options[0].default)
        self.assertTrue(select.options[2].default)

        await view.clear_rating_filter.callback(interaction)
        self.assertEqual(view.entries, tuple(entries))
        self.assertEqual(view.rating_filter, ())
        self.assertNotIn(view.clear_rating_filter, view.children)
        self.assertFalse(any(option.default for option in select.options))
        self.assertEqual(interaction.response.edit_message.await_count, 3)
        interaction.response.send_message.assert_not_awaited()

    async def test_rating_filter_with_no_match_keeps_current_cards(self):
        entry = GlossaryEntry("未評価語", "意味")
        view = bot_module.SGGlossaryView(123, [entry], mode="cards")
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                edit_message=AsyncMock(), send_message=AsyncMock(),
            ),
        )
        await view._apply_rating_filter(interaction, ("まだ要復習",))
        self.assertEqual(view.entries, (entry,))
        self.assertEqual(view.rating_filter, ())
        interaction.response.edit_message.assert_not_awaited()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        self.assertIn("該当する単語はありません", interaction.response.send_message.await_args.args[0])

    async def test_filtered_deck_is_fixed_until_ratings_are_selected_again(self):
        first = GlossaryEntry("最初", "意味")
        second = GlossaryEntry("次", "意味")
        view = bot_module.SGGlossaryView(
            123, [first, second], mode="cards",
            ratings={
                glossary_entry_key(first): "まだ要復習",
                glossary_entry_key(second): "まだ要復習",
            },
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                edit_message=AsyncMock(), send_message=AsyncMock(),
            ),
        )
        await view._apply_rating_filter(interaction, ("まだ要復習",))
        await view.reveal.callback(interaction)
        with patch.object(bot_module, "save_sg_glossary_rating") as save:
            await view.rated_yes.callback(interaction)
        save.assert_called_once_with(bot_module.DB_PATH, 123, first, "できた")
        self.assertEqual(view.entries, (first, second))
        self.assertEqual(view.card_index, 1)
        self.assertIn("評価（選択時）: 要復習", view.content())

        await view._apply_rating_filter(interaction, ("まだ要復習",))
        self.assertEqual(view.entries, (second,))
        self.assertEqual(view.card_index, 0)

    async def test_search_works_with_and_without_category(self):
        entries = [
            GlossaryEntry("アクセス制御", "秘密を守る", "セキュリティ"),
            GlossaryEntry("暗号", "秘密を守る", "法務"),
            GlossaryEntry("認証", "本人確認", "セキュリティ"),
        ]
        ctx = _Context()
        with patch.object(bot_module, "load_glossary", return_value=entries):
            await bot_module.sgglossary.callback(
                ctx, category="セキュリティ", query="秘密",
            )
        content, kwargs = ctx.messages[0]
        self.assertIn("アクセス制御", content)
        self.assertNotIn("暗号", content)
        self.assertNotIn("認証", content)
        self.assertIn("検索: 秘密", content)
        self.assertEqual(len(kwargs["view"].entries), 1)

        ctx = _Context()
        with patch.object(bot_module, "load_glossary", return_value=entries):
            await bot_module.sgglossary.callback(ctx, query="本人確認")
        self.assertIn("認証", ctx.messages[0][0])
        self.assertEqual(len(ctx.messages[0][1]["view"].entries), 1)

    async def test_unmatched_or_too_long_search_has_guidance(self):
        ctx = _Context()
        with patch.object(bot_module, "load_glossary") as reader:
            await bot_module.sgglossary.callback(ctx, query="長" * 101)
        reader.assert_not_called()
        self.assertIn("100文字以内", ctx.messages[0][0])

        ctx = _Context()
        with patch.object(bot_module, "load_glossary", return_value=[
            GlossaryEntry("認証", "本人確認", "セキュリティ"),
        ]):
            await bot_module.sgglossary.callback(ctx, query="暗号")
        self.assertIn("一致する用語がありません", ctx.messages[0][0])

    async def test_invalid_or_empty_category_has_guidance(self):
        ctx = _Context()
        with patch.object(bot_module, "load_glossary") as reader:
            await bot_module.sgglossary.callback(ctx, category="存在しない分野")
        reader.assert_not_called()
        self.assertIn("候補から", ctx.messages[0][0])

        ctx = _Context()
        with patch.object(bot_module, "load_glossary", return_value=[
            GlossaryEntry("用語A", "意味A", "セキュリティ"),
        ]):
            await bot_module.sgglossary.callback(ctx, category="法務")
        self.assertIn("選んだ分野の用語がありません", ctx.messages[0][0])

    async def test_wrong_channel_does_not_read_data(self):
        ctx = _Context(channel_name="雑談")
        with patch.object(bot_module, "load_glossary") as reader:
            await bot_module.sgglossary.callback(ctx)
        reader.assert_not_called()
        self.assertIn("#SG用語集", ctx.messages[0][0])

    async def test_missing_data_gives_file_and_source_guidance(self):
        ctx = _Context()
        with patch.object(bot_module, "load_glossary", side_effect=FileNotFoundError):
            await bot_module.sgglossary.callback(ctx)
        self.assertIn("data/sg_glossary.json", ctx.messages[0][0])
        self.assertIn("https://www.sg-siken.com/keyword/", ctx.messages[0][0])

    async def test_list_pages_keep_long_meaning_and_stay_bounded(self):
        meaning = "長い意味" * 500
        pages = bot_module._sg_glossary_list_pages([
            GlossaryEntry("長い用語", meaning),
        ])
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(body) <= 1500 for body, _ in pages))
        self.assertIn("長い意味", pages[-1][0])

    async def test_one_hand_buttons_reveal_advance_and_switch_modes(self):
        view = bot_module.SGGlossaryView(123, [
            GlossaryEntry("最初", "一つ目の意味"),
            GlossaryEntry("次", "二つ目の意味"),
        ], mode="cards")
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            response=SimpleNamespace(edit_message=AsyncMock()),
        )

        await view.reveal.callback(interaction)
        self.assertTrue(view.revealed)
        self.assertIn("一つ目の意味", view.content())
        await view.next_page.callback(interaction)
        self.assertEqual(view.card_index, 1)
        self.assertFalse(view.revealed)
        await view.switch_mode.callback(interaction)
        self.assertEqual(view.mode, "list")
        self.assertIn("二つ目の意味", view.content())
        self.assertEqual(interaction.response.edit_message.await_count, 3)

    async def test_self_rating_buttons_appear_only_for_revealed_cards(self):
        view = bot_module.SGGlossaryView(123, [
            GlossaryEntry("最初", "一つ目の意味"),
            GlossaryEntry("次", "二つ目の意味"),
        ])
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            response=SimpleNamespace(edit_message=AsyncMock()),
        )

        def rating_buttons():
            return {
                child.label: child for child in view.children
                if getattr(child, "label", None) in RATING_LABELS
            }

        self.assertEqual(rating_buttons(), {})
        await view.switch_mode.callback(interaction)
        self.assertEqual(set(rating_buttons()), set(RATING_LABELS))
        self.assertTrue(all(button.disabled for button in rating_buttons().values()))
        self.assertTrue(all(button.row == 1 for button in rating_buttons().values()))

        await view.reveal.callback(interaction)
        self.assertTrue(all(not button.disabled for button in rating_buttons().values()))
        await view.switch_mode.callback(interaction)
        self.assertEqual(rating_buttons(), {})

    async def test_each_self_rating_saves_and_advances_one_card(self):
        first = GlossaryEntry("最初", "一つ目の意味")
        second = GlossaryEntry("次", "二つ目の意味")
        for label, rating in zip(RATING_LABELS, RATING_VALUES):
            with self.subTest(rating=label):
                view = bot_module.SGGlossaryView(
                    123, [first, second], mode="cards",
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=123),
                    response=SimpleNamespace(edit_message=AsyncMock()),
                )
                await view.reveal.callback(interaction)
                button = next(
                    child for child in view.children
                    if getattr(child, "label", None) == label
                )
                with patch.object(bot_module, "save_sg_glossary_rating") as save:
                    await button.callback(interaction)
                save.assert_called_once_with(
                    bot_module.DB_PATH, 123, first, rating,
                )
                self.assertEqual(view.card_index, 1)
                self.assertFalse(view.revealed)
                self.assertEqual(interaction.response.edit_message.await_count, 2)

    async def test_rating_last_card_stays_on_last_card(self):
        entry = GlossaryEntry("最後", "最後の意味")
        view = bot_module.SGGlossaryView(123, [entry], mode="cards")
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            response=SimpleNamespace(edit_message=AsyncMock()),
        )
        await view.reveal.callback(interaction)
        button = next(
            child for child in view.children
            if getattr(child, "label", None) == "できた"
        )
        with patch.object(bot_module, "save_sg_glossary_rating") as save:
            await button.callback(interaction)
        save.assert_called_once_with(bot_module.DB_PATH, 123, entry, "できた")
        self.assertEqual(view.card_index, 0)

    async def test_failed_rating_keeps_current_card(self):
        entry = GlossaryEntry("最初", "一つ目の意味")
        view = bot_module.SGGlossaryView(
            123, [entry, GlossaryEntry("次", "二つ目の意味")], mode="cards",
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            response=SimpleNamespace(
                edit_message=AsyncMock(), send_message=AsyncMock(),
            ),
        )
        await view.reveal.callback(interaction)
        with patch.object(
            bot_module, "save_sg_glossary_rating",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            await view.rated_yes.callback(interaction)
        self.assertEqual(view.card_index, 0)
        self.assertTrue(view.revealed)
        self.assertEqual(view.ratings, {})
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_navigation_without_rating_does_not_post_study_record(self):
        channel = SimpleNamespace(id=7, send=AsyncMock())
        view = bot_module.SGGlossaryView(
            123, [GlossaryEntry("最初", "一つ目の意味")],
            record_channel=channel, guild_id=1,
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            response=SimpleNamespace(edit_message=AsyncMock()),
        )
        with patch.object(bot_module, "record_glossary_card") as record:
            await view.switch_mode.callback(interaction)
            await view.reveal.callback(interaction)
            await view.reveal.callback(interaction)
            await view.switch_mode.callback(interaction)
        record.assert_not_called()
        channel.send.assert_not_awaited()

    async def test_rating_creates_and_updates_one_public_session_summary(self):
        first = GlossaryEntry("最初", "一つ目の意味")
        second = GlossaryEntry("次", "二つ目の意味")
        public_message = SimpleNamespace(id=456, edit=AsyncMock())
        channel = SimpleNamespace(
            id=7, send=AsyncMock(return_value=public_message),
            fetch_message=AsyncMock(return_value=public_message),
        )
        view = bot_module.SGGlossaryView(
            123, [first, second], mode="cards", record_channel=channel,
            guild_id=1, history_db_path="test-glossary-history.db",
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            response=SimpleNamespace(edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        summaries = [
            {
                "session_id": view.session_id, "user_id": 123,
                "started_at": "2026-10-01T00:00:00+00:00", "total": 1,
                "counts": dict.fromkeys(RATING_VALUES, 0) | {"できた": 1},
                "summary_message_id": None,
            },
            {
                "session_id": view.session_id, "user_id": 123,
                "started_at": "2026-10-01T00:00:00+00:00", "total": 2,
                "counts": dict.fromkeys(RATING_VALUES, 0)
                | {"できた": 1, "できなかった": 1},
                "summary_message_id": 456,
            },
            {
                "session_id": view.session_id, "user_id": 123,
                "started_at": "2026-10-01T00:00:00+00:00", "total": 2,
                "counts": dict.fromkeys(RATING_VALUES, 0)
                | {"できた": 2},
                "summary_message_id": 456,
            },
        ]
        with (
            patch.object(bot_module, "save_sg_glossary_rating") as save_latest,
            patch.object(
                bot_module, "record_glossary_card", side_effect=summaries,
            ) as record,
            patch.object(
                bot_module, "set_glossary_summary_message_id",
            ) as save_message_id,
        ):
            await view.reveal.callback(interaction)
            await view.rated_yes.callback(interaction)
            await view.reveal.callback(interaction)
            await view.rated_no.callback(interaction)
            await view.rated_yes.callback(interaction)

        self.assertEqual(record.call_count, 3)
        self.assertEqual(
            [call.args[1] for call in record.call_args_list],
            [view.session_id] * 3,
        )
        self.assertEqual(
            [call.args[-2:] for call in record.call_args_list],
            [(first, "できた"), (second, "できなかった"),
             (second, "できた")],
        )
        self.assertEqual(save_latest.call_count, 3)
        channel.send.assert_awaited_once()
        self.assertEqual(public_message.edit.await_count, 2)
        save_message_id.assert_called_once_with(
            "test-glossary-history.db", view.session_id, 456,
        )
        first_text = channel.send.await_args.args[0]
        final_text = public_message.edit.await_args.kwargs["content"]
        self.assertIn("合計 **1問**", first_text)
        self.assertIn("合計 **2問**", final_text)
        self.assertIn("できた: 2問", final_text)
        self.assertIn("できなかった: 0問", final_text)
        self.assertTrue(all(label in final_text for label in RATING_LABELS))
        interaction.followup.send.assert_not_awaited()

    async def test_public_post_failure_preserves_record_and_warns_privately(self):
        entry = GlossaryEntry("最初", "一つ目の意味")
        response = SimpleNamespace(status=403, reason="Forbidden")
        channel = SimpleNamespace(
            id=7,
            send=AsyncMock(side_effect=bot_module.discord.Forbidden(
                response, "cannot send",
            )),
        )
        view = bot_module.SGGlossaryView(
            123, [entry], mode="cards", record_channel=channel,
            guild_id=1, history_db_path="test-glossary-history.db",
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            response=SimpleNamespace(edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        summary = {
            "session_id": view.session_id, "user_id": 123,
            "started_at": "2026-10-01T00:00:00+00:00", "total": 1,
            "counts": dict.fromkeys(RATING_VALUES, 0) | {"微妙": 1},
            "summary_message_id": None,
        }
        with (
            patch.object(bot_module, "save_sg_glossary_rating"),
            patch.object(
                bot_module, "record_glossary_card", return_value=summary,
            ) as record,
            patch.object(bot_module, "set_glossary_summary_message_id") as save_id,
        ):
            await view.reveal.callback(interaction)
            await view.rated_unsure.callback(interaction)
        record.assert_called_once()
        save_id.assert_not_called()
        self.assertIn("微妙", view.ratings.values())
        interaction.response.edit_message.assert_awaited()
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.assertIn(
            "専用DBに保存", interaction.followup.send.await_args.args[0],
        )

    async def test_history_write_failure_keeps_card_for_retry_without_post(self):
        first = GlossaryEntry("最初", "一つ目の意味")
        second = GlossaryEntry("次", "二つ目の意味")
        public_message = SimpleNamespace(id=456, edit=AsyncMock())
        channel = SimpleNamespace(
            id=7, send=AsyncMock(return_value=public_message),
        )
        view = bot_module.SGGlossaryView(
            123, [first, second], mode="cards", record_channel=channel,
            guild_id=1, history_db_path="test-glossary-history.db",
        )
        failed_interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            response=SimpleNamespace(
                edit_message=AsyncMock(), send_message=AsyncMock(),
            ),
        )
        retry_interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            response=SimpleNamespace(edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        summary = {
            "session_id": view.session_id, "user_id": 123,
            "started_at": "2026-10-01T00:00:00+00:00", "total": 1,
            "counts": dict.fromkeys(RATING_VALUES, 0) | {"できた": 1},
            "summary_message_id": None,
        }
        with (
            patch.object(bot_module, "save_sg_glossary_rating") as save_latest,
            patch.object(
                bot_module, "record_glossary_card",
                side_effect=[sqlite3.OperationalError("database is locked"), summary],
            ) as record,
            patch.object(bot_module, "set_glossary_summary_message_id"),
        ):
            await view.reveal.callback(failed_interaction)
            await view.rated_yes.callback(failed_interaction)
            self.assertEqual(view.card_index, 0)
            self.assertTrue(view.revealed)
            channel.send.assert_not_awaited()
            self.assertTrue(
                failed_interaction.response.send_message.await_args.kwargs[
                    "ephemeral"
                ]
            )
            self.assertIn(
                "学習記録を保存できませんでした",
                failed_interaction.response.send_message.await_args.args[0],
            )

            await view.rated_yes.callback(retry_interaction)

        self.assertEqual(record.call_count, 2)
        self.assertEqual(save_latest.call_count, 2)
        self.assertEqual(view.card_index, 1)
        channel.send.assert_awaited_once()
        retry_interaction.response.edit_message.assert_awaited_once()

    async def test_rating_writes_primary_and_separate_history_databases(self):
        entry = GlossaryEntry("最初", "一つ目の意味", "セキュリティ")
        public_message = SimpleNamespace(id=456, edit=AsyncMock())
        channel = SimpleNamespace(
            id=7, send=AsyncMock(return_value=public_message),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=123),
            response=SimpleNamespace(edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with tempfile.TemporaryDirectory() as directory:
            primary_path = Path(directory) / "study.db"
            history_path = Path(directory) / "sg_glossary_history.db"
            view = bot_module.SGGlossaryView(
                123, [entry], mode="cards", record_channel=channel,
                guild_id=1, history_db_path=history_path,
            )
            with patch.object(bot_module, "DB_PATH", primary_path):
                await view.reveal.callback(interaction)
                await view.rated_review.callback(interaction)

            self.assertTrue(primary_path.exists())
            self.assertTrue(history_path.exists())
            self.assertEqual(
                get_sg_glossary_ratings(primary_path, 123, [entry]),
                {glossary_entry_key(entry): "まだ要復習"},
            )
            summary = get_glossary_session(history_path, view.session_id)
            self.assertEqual(summary["total"], 1)
            self.assertEqual(summary["counts"]["まだ要復習"], 1)
            self.assertEqual(summary["summary_message_id"], 456)
        channel.send.assert_awaited_once()
        self.assertIn("合計 **1問**", channel.send.await_args.args[0])
        interaction.followup.send.assert_not_awaited()

    async def test_bundled_glossary_messages_fit_discord_limit(self):
        view = bot_module.SGGlossaryView(
            123, load_glossary(GLOSSARY_PATH),
            category="プロジェクトマネジメント", query="*" * 100,
        )
        for page_index in range(len(view.list_pages)):
            view.page_index = page_index
            self.assertLessEqual(len(view.content()), 2000)

        view.mode = "cards"
        view.revealed = True
        for card_index in range(len(view.entries)):
            view.card_index = card_index
            self.assertLessEqual(len(view.content()), 2000)

        view.rating_filter = RATING_VALUES
        view.mode = "list"
        for page_index in range(len(view.list_pages)):
            view.page_index = page_index
            self.assertLessEqual(len(view.content()), 2000)
        view.mode = "cards"
        for card_index in range(len(view.entries)):
            view.card_index = card_index
            self.assertLessEqual(len(view.content()), 2000)


if __name__ == "__main__":
    unittest.main()
