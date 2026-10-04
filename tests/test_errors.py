import unittest
from types import SimpleNamespace

import discord
from discord.ext import commands

from studybot import errors
from studybot.app import create_bot
from studybot.replies import safe_reply, send_long, split_message


BOT = create_bot()


def http_error(status, body):
    return discord.HTTPException(
        SimpleNamespace(status=status, reason="error"), body
    )


UNKNOWN_REFERENCE = http_error(400, {
    "code": 50035,
    "message": "Invalid Form Body",
    "errors": {"message_reference": {"_errors": [
        {"code": "MESSAGE_REFERENCE_UNKNOWN_MESSAGE", "message": "Unknown message"},
    ]}},
})


class SplitMessageTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(split_message("こんにちは"), ["こんにちは"])
        self.assertEqual(split_message(""), [""])

    def test_splits_on_newlines_under_limit(self):
        text = "\n".join(f"{i}行目" + "あ" * 40 for i in range(100))
        chunks = split_message(text, limit=500)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))
        self.assertEqual("\n".join(chunks), text)
        self.assertTrue(chunks[1].startswith(f"{len(chunks[0].splitlines())}行目"))

    def test_splits_long_line_without_newline(self):
        chunks = split_message("あ" * 1200, limit=500)
        self.assertEqual([len(chunk) for chunk in chunks], [500, 500, 200])


class _Context:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append(dict(kwargs, content=content))


class _Message:
    def __init__(self, error=None):
        self.error = error
        self.replies = []

    async def reply(self, content, mention_author=True):
        if self.error is not None:
            raise self.error
        self.replies.append(content)
        return SimpleNamespace(id=1)


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_long(self):
        ctx = _Context()
        await send_long(ctx, "\n".join("あ" * 100 for _ in range(50)))
        self.assertEqual(len(ctx.messages), 3)
        self.assertTrue(all(len(m["content"]) <= 1900 for m in ctx.messages))

    async def test_safe_reply_skips_deleted_message(self):
        self.assertIsNone(await safe_reply(_Message(UNKNOWN_REFERENCE), "解析結果"))
        not_found = discord.NotFound(SimpleNamespace(status=404, reason=""), "")
        self.assertIsNone(await safe_reply(_Message(not_found), "解析結果"))

    async def test_safe_reply_raises_other_errors_and_trims(self):
        with self.assertRaises(discord.HTTPException):
            await safe_reply(_Message(http_error(500, "boom")), "解析結果")

        message = _Message()
        await safe_reply(message, "あ" * 2500)
        self.assertLessEqual(len(message.replies[0]), 2000)
        self.assertTrue(message.replies[0].endswith("（長いため省略）"))


class _Response:
    def __init__(self, done=False):
        self.done = done
        self.sent = []

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kwargs):
        self.sent.append(dict(kwargs, content=content))


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(dict(kwargs, content=content))


def _interaction(done=False):
    return SimpleNamespace(
        response=_Response(done), followup=_Followup(),
        command=SimpleNamespace(name="setup"),
    )


class ErrorHandlerTests(unittest.IsolatedAsyncioTestCase):
    def test_installed_on_bot_views_and_modals(self):
        self.assertIs(BOT.tree.on_error, errors.on_app_command_error)
        self.assertIn(errors.on_command_error, BOT.extra_events["on_command_error"])
        self.assertIs(discord.ui.View.on_error, errors._view_on_error)
        self.assertIs(discord.ui.Modal.on_error, errors._modal_on_error)

    async def test_button_error_is_reported_to_the_user(self):
        view = discord.ui.View()
        interaction = _interaction()
        await view.on_error(interaction, RuntimeError("boom"), None)
        self.assertEqual(interaction.response.sent[0]["content"], errors.ERROR_MESSAGE)
        self.assertTrue(interaction.response.sent[0]["ephemeral"])

        answered = _interaction(done=True)
        await view.on_error(answered, RuntimeError("boom"), None)
        self.assertEqual(answered.followup.sent[0]["content"], errors.ERROR_MESSAGE)

    async def test_command_errors(self):
        ctx = _Context()
        ctx.command = "plan new"
        await errors.on_command_error(ctx, commands.CommandNotFound())
        self.assertEqual(ctx.messages, [])

        missing = commands.MissingRequiredArgument(
            SimpleNamespace(name="mistake_id", displayed_name="mistake_id")
        )
        await errors.on_command_error(ctx, missing)
        self.assertIn("`mistake_id` を指定してください", ctx.messages[-1]["content"])

        await errors.on_command_error(
            ctx, commands.CommandInvokeError(RuntimeError("boom"))
        )
        self.assertEqual(ctx.messages[-1]["content"], errors.ERROR_MESSAGE)
        self.assertTrue(ctx.messages[-1]["ephemeral"])

    async def test_app_command_error(self):
        interaction = _interaction()
        await errors.on_app_command_error(interaction, RuntimeError("boom"))
        self.assertEqual(interaction.response.sent[0]["content"], errors.ERROR_MESSAGE)


if __name__ == "__main__":
    unittest.main()
