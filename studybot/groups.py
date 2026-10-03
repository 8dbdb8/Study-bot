"""コマンドのグループ（/sg・/review・/time・/plan・/ai）と /help。"""

import discord
from discord.ext import commands

from studybot.config import STUDY_VOICE_CHANNEL_NAME
from studybot.embeds import COLOR_DEFAULT


# グループのコマンドは !xxx と /xxx の両方で使える。
# 各サブコマンドは studybot/features/ の各ファイルで登録する。

# /help に表示する順番
HELP_COMMAND_ORDER = (
    "sg", "review", "time", "plan", "ai", "data", "setup", "help",
)
HELP_SUBCOMMAND_ORDER = {
    "sg": ("log", "b", "progress", "chart", "status", "glossary"),
    "review": ("add", "list", "start", "answer"),
    "time": ("today", "week", "chart", "logs"),
    "plan": ("new", "status", "exam", "notify", "roadmap"),
    "ai": ("today", "next", "report"),
}


def _ordered_subcommands(group):
    order = HELP_SUBCOMMAND_ORDER.get(group.name, ())
    return sorted(
        group.commands,
        key=lambda sub: (
            order.index(sub.name) if sub.name in order else len(order)
        ),
    )


def build_help_text(bot):
    commands_by_name = {
        command.name: command
        for command in bot.tree.get_commands()
    }
    lines = [
        "**StudyBot の使い方**",
        f"ボイスチャンネル「{STUDY_VOICE_CHANNEL_NAME}」に入ると勉強時間を自動で記録します。"
        "結果は `/sg log`、間違えた問題は `/review add` で登録してください。",
    ]
    for name in HELP_COMMAND_ORDER:
        command = commands_by_name.get(name)
        if command is None:
            continue
        lines.append(f"\n**/{name}**　{command.description}")
        if getattr(command, "commands", None):
            lines.extend(
                f"　`/{name} {sub.name}`　{sub.description}"
                for sub in _ordered_subcommands(command)
            )
    return "\n".join(lines)


async def send_group_help(ctx):
    """サブコマンドなしで !sg などが呼ばれたときの案内。"""
    group = ctx.command
    lines = [f"**/{group.name}**　{group.description}"]
    lines.extend(
        f"　`/{group.name} {sub.name}`　{sub.description}"
        for sub in _ordered_subcommands(group)
    )
    await ctx.send("\n".join(lines))


@commands.hybrid_group(
    name="sg",
    description="SGの記録・進捗・用語集",
    invoke_without_command=True,
)
async def sg_group(ctx):
    await send_group_help(ctx)


@commands.hybrid_group(
    name="review",
    description="間違えた問題の復習",
    invoke_without_command=True,
)
async def review_group(ctx):
    await send_group_help(ctx)


@commands.hybrid_group(
    name="time",
    description="勉強時間とログ",
    invoke_without_command=True,
)
async def time_group(ctx):
    await send_group_help(ctx)


@commands.hybrid_group(
    name="plan",
    description="学習計画・試験日・ロードマップ",
    invoke_without_command=True,
)
async def plan_group(ctx):
    await send_group_help(ctx)


@commands.hybrid_group(
    name="ai",
    description="AIコーチによる分析と提案",
    invoke_without_command=True,
)
async def ai_group(ctx):
    await send_group_help(ctx)


@commands.hybrid_command(
    name="help",
    description="StudyBotのコマンド一覧と使い方"
)
async def help_command(ctx):
    title, _, body = build_help_text(ctx.bot).partition("\n")
    embed = discord.Embed(
        title=title.strip("*"),
        description=body,
        color=COLOR_DEFAULT,
    )
    await ctx.send(embed=embed, ephemeral=True)


ALL_GROUPS = (sg_group, review_group, time_group, plan_group, ai_group)
