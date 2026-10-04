"""/time focus：集中タイマー（ポモドーロ）。

決めた分数だけ集中し、短い休憩をはさんで何セットか繰り返す。
区切りごとに #集中タイマー（なければコマンドを使ったチャンネル）で知らせ、
終えたセット数を記録する。
タイマーはBotのメモリ上で動くので、Botを再起動すると止まる。
"""

import asyncio
from datetime import datetime

import discord
from discord import app_commands

from studybot import config
from studybot.channels import find_channel
from studybot.config import JST
from studybot.groups import time_group
from studybot.habits import add_focus_set, get_focus_sets
from studybot.replies import send_private


# 動いているタイマー：user_id -> {"task", "done", "sets"}
FOCUS_TIMERS = {}

# テストで待ち時間を短くできるように、待つ関数を差し替えられるようにする
_sleep = asyncio.sleep


def _mention(user_id):
    return discord.AllowedMentions(users=[discord.Object(id=user_id)])


async def run_focus(channel, user_id, focus_minutes, break_minutes, sets):
    timer = FOCUS_TIMERS[user_id]
    try:
        for number in range(1, sets + 1):
            await _sleep(focus_minutes * 60)
            today = datetime.now(JST).date()
            add_focus_set(config.DB_PATH, user_id, today, focus_minutes)
            timer["done"] = number
            if number < sets:
                await channel.send(
                    f"<@{user_id}> ⏰ {number}セット目おわり！"
                    f"{break_minutes}分休憩しましょう。",
                    allowed_mentions=_mention(user_id),
                )
                await _sleep(break_minutes * 60)
                await channel.send(
                    f"<@{user_id}> ▶ {number + 1}セット目スタート"
                    f"（{focus_minutes}分）",
                    allowed_mentions=_mention(user_id),
                )
            else:
                day_sets, day_minutes = get_focus_sets(
                    config.DB_PATH, user_id, today
                )
                await channel.send(
                    f"<@{user_id}> 🎉 {sets}セット完了！おつかれさまでした。"
                    f"今日の集中：{day_sets}セット（{day_minutes}分）",
                    allowed_mentions=_mention(user_id),
                )
    finally:
        FOCUS_TIMERS.pop(user_id, None)


@time_group.command(
    name="focus",
    description="集中タイマー（例：25分集中＋5分休憩を4セット）",
)
@app_commands.describe(
    minutes="1セットの集中時間（分、省略すると25）",
    break_minutes="休憩時間（分、省略すると5）",
    sets="セット数（省略すると4）",
    stop="動いているタイマーを止める",
)
async def focus(ctx, minutes: int = 25, break_minutes: int = 5, sets: int = 4,
                stop: bool = False):
    user_id = ctx.author.id
    running = FOCUS_TIMERS.get(user_id)

    if stop:
        if running is None:
            await send_private(ctx, "動いている集中タイマーはありません。")
            return
        running["task"].cancel()
        FOCUS_TIMERS.pop(user_id, None)
        await ctx.send(
            f"⏹ 集中タイマーを止めました（{running['done']}/{running['sets']}"
            "セット完了）。"
        )
        return

    if running is not None:
        await send_private(
            ctx,
            "集中タイマーはすでに動いています。"
            "止めるときは `/time focus stop:True` を使ってください。",
        )
        return
    if not 5 <= minutes <= 120 or not 1 <= break_minutes <= 60 \
            or not 1 <= sets <= 12:
        await send_private(
            ctx, "集中は5〜120分、休憩は1〜60分、セット数は1〜12で指定してください。"
        )
        return

    channel = find_channel(ctx.guild, "focus") or ctx.channel
    FOCUS_TIMERS[user_id] = {"done": 0, "sets": sets}
    FOCUS_TIMERS[user_id]["task"] = asyncio.create_task(
        run_focus(channel, user_id, minutes, break_minutes, sets)
    )
    total = minutes * sets + break_minutes * (sets - 1)
    where = ""
    if channel.id != ctx.channel.id:
        where = f"お知らせは {channel.mention} に届きます。"
    await ctx.send(
        f"⏱ 集中タイマー開始：{minutes}分 × {sets}セット"
        f"（休憩{break_minutes}分・全部で約{total}分）。{where}"
        "止めるときは `/time focus stop:True`"
    )
