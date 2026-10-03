"""/sg グループ：SGの記録・進捗・累積成績。"""

import discord

from studybot import config
from studybot.config import REVIEW_SCORE_THRESHOLD
from studybot.embeds import build_progress_embed, COLOR_DEFAULT
from studybot.formatting import format_category_label, get_review_candidates
from studybot.forms import build_sgb_prompt, build_sglog_prompt
from studybot.groups import sg_group
from studybot.replies import send_private
from studybot.sg_features import get_sg_b_summary, get_sg_category_progress
from studybot.stats import get_study_status


@sg_group.command(
    name="log",
    description="SG過去問道場の結果を記録（分野・問題数・正答率）"
)
async def sglog(ctx):
    message, view = build_sglog_prompt(ctx.guild, ctx.author.id)
    await send_private(ctx, message, view)


@sg_group.command(
    name="progress",
    description="SGの14分野と科目Bの進捗を表示"
)
async def sgprogress(ctx):
    items, unclassified = get_sg_category_progress(
        config.DB_PATH, ctx.author.id
    )
    await ctx.send(embed=build_progress_embed(
        items,
        unclassified,
        get_sg_b_summary(config.DB_PATH, ctx.author.id),
        REVIEW_SCORE_THRESHOLD,
    ))


@sg_group.command(
    name="b",
    description="SG科目Bの演習結果を記録"
)
async def sgb(ctx):
    message, view = build_sgb_prompt(ctx.guild, ctx.author.id)
    await send_private(ctx, message, view)


@sg_group.command(
    name="status",
    description="SGの累積学習状況を表示"
)
async def status(ctx):
    qualification = "SG"

    status_data = get_study_status(
        ctx.author.id,
        qualification
    )

    total_questions = (
        status_data["total_questions"]
    )

    average_score = (
        status_data["average_score"]
    )

    category_status = (
        status_data["category_status"]
    )

    log_count = (
        status_data["log_count"]
    )

    if log_count == 0:
        await ctx.send(
            f"📊 **{qualification} 学習状況**\n"
            "まだ解析済みの勉強ログが"
            "ありません。"
        )
        return

    if average_score is not None:
        score_text = (
            f"{average_score:.1f}%"
        )
    else:
        score_text = "記録なし"

    if category_status:
        category_lines = []

        for item in category_status:
            label = format_category_label(item)

            if item["average_score"] is not None:
                value = (
                    f"{item['average_score']:.1f}%"
                )
            else:
                value = (
                    f"記録{item['log_count']}回"
                )

            category_lines.append(
                f"- {label}：{value}"
            )

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
        title=f"{qualification} 学習状況",
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
