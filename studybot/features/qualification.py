"""資格ごとのコマンド（/sg・/fe・/iryo）：記録・進捗・グラフ・累積成績。

コマンドの中身は資格を受け取る関数として1つだけ書き、
qualifications.py に並べた資格ごとに /{資格} log などを作る。
"""

import asyncio
from datetime import datetime

import discord
from discord import app_commands

from studybot import config
from studybot.charts import render_score_chart
from studybot.config import JST, REVIEW_SCORE_THRESHOLD
from studybot.embeds import COLOR_DEFAULT, build_progress_embed
from studybot.formatting import format_category_label, get_review_candidates
from studybot.features.review import build_review_session
from studybot.forms import build_sgb_prompt, build_sglog_prompt, open_quick_log
from studybot.groups import QUALIFICATION_GROUPS
from studybot.qualifications import QUALIFICATIONS
from studybot.replies import respond_private, send_png, send_private
from studybot.sg_features import (
    get_sg_b_summary,
    get_sg_category_progress,
    get_sg_mistakes,
    get_weak_categories,
)
from studybot.stats import get_daily_scores, get_study_status


async def record_log(ctx, qualification):
    # スラッシュコマンドなら分野・問題数・正答率を1つのフォームで入力する。
    # !コマンドはフォームを開けないので、分野を選んでからフォームへ進む
    if getattr(ctx, "interaction", None) is not None:
        await open_quick_log(ctx.interaction, qualification)
        return
    message, view = build_sglog_prompt(ctx.guild, ctx.author.id, qualification)
    await send_private(ctx, message, view)


async def record_part_b(ctx, qualification):
    message, view = build_sgb_prompt(ctx.guild, ctx.author.id, qualification)
    await send_private(ctx, message, view)


class WeakReviewView(discord.ui.View):
    """進捗の下に付けるボタン。弱い分野の誤答を解き直す。"""

    def __init__(self, owner_id, qualification):
        super().__init__(timeout=30 * 60)
        self.owner_id = owner_id
        self.qualification = qualification

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "このボタンは本人専用です。", ephemeral=True
        )
        return False

    @discord.ui.button(label="弱い分野を復習", style=discord.ButtonStyle.primary)
    async def weak_review_button(self, interaction, button):
        code = self.qualification.code
        weak = get_weak_categories(
            config.DB_PATH, self.owner_id, code, REVIEW_SCORE_THRESHOLD
        )
        weak_names = {category for category, _ in weak}
        items = [
            item for item in get_sg_mistakes(
                config.DB_PATH, self.owner_id, due_only=False,
                qualification=code,
            )
            if item["category"] in weak_names
        ]
        if items:
            # 弱い分野の誤答は、復習日を待たずにまとめて解き直す
            embed, view = build_review_session(
                self.owner_id, datetime.now(JST).date(), items
            )
            await respond_private(interaction, embed=embed, view=view)
            return
        # 誤答がなければ、一番弱い分野で問題を解いて記録する
        weakest = get_weak_categories(
            config.DB_PATH, self.owner_id, code, threshold=None
        )
        await open_quick_log(
            interaction, self.qualification, weakest[0][0] if weakest else None
        )


async def show_progress(ctx, qualification):
    items, unclassified = get_sg_category_progress(
        config.DB_PATH, ctx.author.id, qualification.code
    )
    embed = build_progress_embed(
        items,
        unclassified,
        get_sg_b_summary(config.DB_PATH, ctx.author.id, qualification.code),
        REVIEW_SCORE_THRESHOLD,
        qualification,
        get_study_status(ctx.author.id, qualification.code)["average_score"],
    )
    await ctx.send(embed=embed, view=WeakReviewView(ctx.author.id, qualification))


async def show_status(ctx, qualification):
    status_data = get_study_status(ctx.author.id, qualification.code)
    total_questions = status_data["total_questions"]
    average_score = status_data["average_score"]
    category_status = status_data["category_status"]
    log_count = status_data["log_count"]

    if log_count == 0:
        await ctx.send(
            f"📊 **{qualification.code} 学習状況**\n"
            "まだ解析済みの勉強ログが"
            "ありません。"
        )
        return

    score_text = (
        f"{average_score:.1f}%" if average_score is not None else "記録なし"
    )

    if category_status:
        category_lines = []
        for item in category_status:
            label = format_category_label(item)
            if item["average_score"] is not None:
                value = f"{item['average_score']:.1f}%"
            else:
                value = f"記録{item['log_count']}回"
            category_lines.append(f"- {label}：{value}")
        category_text = "\n".join(category_lines)
    else:
        category_text = "まだ記録なし"

    review_candidates = get_review_candidates(
        category_status,
        "average_score"
    )
    if review_candidates:
        review_text = "\n".join(
            f"- {format_category_label(item)}："
            f"{item['average_score']:.1f}% "
            f"（{'重点復習' if item['scored_log_count'] >= 2 else '候補'}）"
            for item in review_candidates
        )
    else:
        review_text = "現在はなし"

    embed = discord.Embed(
        title=f"{qualification.code} 学習状況",
        color=COLOR_DEFAULT,
    )
    embed.add_field(name="解析済みログ", value=f"{log_count}件")
    embed.add_field(name="解いた問題", value=f"{total_questions}問")
    embed.add_field(name="平均正答率", value=score_text)
    embed.add_field(
        name="分野別成績", value=category_text[:1024], inline=False
    )
    embed.add_field(
        name="要復習候補", value=review_text[:1024], inline=False
    )
    await ctx.send(embed=embed)


async def show_chart(ctx, qualification, category="all"):
    if category != "all" and category not in qualification.category_names:
        await ctx.send("分野は候補から選んでください。")
        return

    points = get_daily_scores(
        ctx.author.id,
        qualification.code,
        None if category == "all" else category,
    )
    if not points:
        await ctx.send(
            f"グラフにできる{qualification.code}の記録がまだありません。"
        )
        return

    label = "全体" if category == "all" else category
    async with ctx.typing():
        png = await asyncio.to_thread(
            render_score_chart,
            points,
            f"{qualification.code} 正答率の推移（{label}）",
            REVIEW_SCORE_THRESHOLD,
        )
    await send_png(ctx, png, f"{qualification.command}_score.png")


def add_qualification_commands(qualification, group):
    """/{資格} log などのサブコマンドを作り、名前→コマンドの辞書を返す。"""
    code = qualification.code
    created = {}

    @group.command(
        name="log",
        description=(
            f"{qualification.practice_source}の結果を記録（分野・問題数・正答率）"
        )[:100],
    )
    async def log_command(ctx):
        await record_log(ctx, qualification)

    created["log"] = log_command

    if qualification.has_part_b:
        @group.command(name="b", description=f"{code}科目Bの演習結果を記録")
        async def part_b_command(ctx):
            await record_part_b(ctx, qualification)

        created["b"] = part_b_command

    progress_text = (
        f"{code}の{len(qualification.categories)}分野"
        + ("と科目B" if qualification.has_part_b else "")
        + "の進捗を表示"
    )

    @group.command(name="progress", description=progress_text)
    async def progress_command(ctx):
        await show_progress(ctx, qualification)

    created["progress"] = progress_command

    @group.command(
        name="chart",
        description=f"{code}の正答率と問題数の推移をグラフで表示",
    )
    @app_commands.describe(category="分野（省略すると全体）")
    @app_commands.choices(category=[
        app_commands.Choice(name="全体", value="all"),
        *(
            app_commands.Choice(name=name, value=name)
            for name in qualification.category_names
        ),
    ])
    async def chart_command(ctx, category: str = "all"):
        await show_chart(ctx, qualification, category)

    created["chart"] = chart_command

    @group.command(name="status", description=f"{code}の累積学習状況を表示")
    async def status_command(ctx):
        await show_status(ctx, qualification)

    created["status"] = status_command
    return created


# 資格ごとのコマンド：COMMANDS["FE"]["log"] のように取り出せる
COMMANDS = {
    qualification.code: add_qualification_commands(
        qualification, QUALIFICATION_GROUPS[qualification.code]
    )
    for qualification in QUALIFICATIONS
}
