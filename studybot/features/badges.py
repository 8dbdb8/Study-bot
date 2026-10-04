"""実績バッジのお祝いと、/time badges の一覧。"""

import sqlite3
from datetime import date, datetime

import discord

from studybot import config
from studybot.badges import (
    BADGES,
    BADGES_BY_ID,
    check_new_badges,
    get_earned_badges,
    next_goals,
)
from studybot.channels import find_channel
from studybot.config import JST
from studybot.embeds import COLOR_DEFAULT, COLOR_SUCCESS
from studybot.groups import time_group


def badge_line(badge):
    return f"{badge.emoji} **{badge.name}**"


def build_new_badges_embed(badges):
    return discord.Embed(
        title="🏅 実績バッジを獲得しました！",
        description="\n".join(badge_line(badge) for badge in badges),
        color=COLOR_SUCCESS,
    ).set_footer(text="/time badges で一覧を見られます")


async def announce_new_badges(guild, user_id, today=None):
    """新しいバッジがあれば #勉強ログ でお祝いする。獲得したバッジを返す。"""
    if guild is None:
        return []
    try:
        new = check_new_badges(
            config.DB_PATH, user_id, today or datetime.now(JST).date()
        )
    except sqlite3.Error as error:
        print(f"[badges] 確認に失敗: {error}")
        return []
    channel = find_channel(guild, "study_log")
    if new and channel is not None:
        try:
            await channel.send(
                content=f"<@{user_id}>",
                embed=build_new_badges_embed(new),
                allowed_mentions=discord.AllowedMentions(
                    users=[discord.Object(id=user_id)]
                ),
            )
        except discord.HTTPException as error:
            print(f"[badges] 送信に失敗: {error}")
    return new


def build_badges_embed(user_id, today):
    earned = get_earned_badges(config.DB_PATH, user_id)
    lines = []
    for badge in BADGES:
        if badge.id in earned:
            day = date.fromisoformat(earned[badge.id])
            lines.append(f"{badge_line(badge)}　{day.month}/{day.day}")
    embed = discord.Embed(
        title=f"実績バッジ {len(earned)}/{len(BADGES)}",
        description="\n".join(lines) or "まだバッジはありません。",
        color=COLOR_DEFAULT,
    )
    goals = [
        f"{badge.emoji} {badge.name}　あと **{badge.threshold - value}**{badge.unit}"
        f"（今 {value}）"
        for badge, value in next_goals(config.DB_PATH, user_id, today)
        if badge.id in BADGES_BY_ID and value < badge.threshold
    ]
    if goals:
        embed.add_field(name="次のバッジ", value="\n".join(goals), inline=False)
    return embed


@time_group.command(
    name="badges",
    description="実績バッジの一覧と、次のバッジまでの残り",
)
async def badges(ctx):
    today = datetime.now(JST).date()
    await announce_new_badges(ctx.guild, ctx.author.id, today)
    await ctx.send(embed=build_badges_embed(ctx.author.id, today))
