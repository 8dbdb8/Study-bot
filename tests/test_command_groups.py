import unittest
from types import SimpleNamespace

import bot


EXPECTED = {
    "sg": {"log", "b", "progress", "status", "glossary"},
    "review": {"add", "list", "answer"},
    "time": {"today", "week", "logs"},
    "plan": {"new", "status", "exam", "roadmap"},
    "ai": {"today", "next", "report"},
}


class CommandGroupTests(unittest.TestCase):
    def test_slash_commands_are_grouped(self):
        top_level = {
            command.name: command for command in bot.bot.tree.get_commands()
        }
        self.assertEqual(
            set(top_level), set(EXPECTED) | {"data", "help"}
        )
        for group, subcommands in EXPECTED.items():
            with self.subTest(group=group):
                self.assertEqual(
                    {sub.name for sub in top_level[group].commands},
                    subcommands,
                )

    def test_old_flat_commands_are_gone(self):
        for name in (
            "hello", "sglog", "sgb", "sgprogress", "sgglossary",
            "mistake", "reviews", "plan_status", "exam", "roadmap",
            "status", "today", "week", "logs", "next", "report",
        ):
            with self.subTest(name=name):
                self.assertIsNone(bot.bot.get_command(name))
                self.assertIsNone(bot.bot.tree.get_command(name))

    def test_prefix_subcommands_resolve(self):
        self.assertIs(bot.bot.get_command("plan new"), bot.plan)
        self.assertIs(bot.bot.get_command("review answer"), bot.review)
        self.assertIs(bot.bot.get_command("ai today"), bot.ai)

    def test_help_lists_every_subcommand_in_order(self):
        text = bot.build_help_text()
        for group, subcommands in EXPECTED.items():
            for sub in subcommands:
                self.assertIn(f"`/{group} {sub}`", text)
        self.assertIn("**/data**", text)
        self.assertLess(text.index("`/sg log`"), text.index("`/sg glossary`"))
        self.assertLess(text.index("`/plan new`"), text.index("`/plan roadmap`"))
        self.assertLess(len(text), 2000)


class _Context:
    def __init__(self, command):
        self.command = command
        self.messages = []

    async def send(self, content, **kwargs):
        self.messages.append((content, kwargs))


class HelpCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_is_ephemeral(self):
        ctx = _Context(None)
        ctx.author = SimpleNamespace(id=1)

        await bot.help_command.callback(ctx)

        content, kwargs = ctx.messages[0]
        self.assertIn("StudyBot の使い方", content)
        self.assertTrue(kwargs.get("ephemeral"))

    async def test_group_without_subcommand_shows_its_list(self):
        ctx = _Context(bot.bot.get_command("review"))

        await bot.review_group.callback(ctx)

        content, _ = ctx.messages[0]
        self.assertTrue(content.startswith("**/review**"))
        self.assertLess(
            content.index("`/review add`"), content.index("`/review answer`")
        )


if __name__ == "__main__":
    unittest.main()
