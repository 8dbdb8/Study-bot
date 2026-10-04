"""/time focus：集中タイマー（ポモドーロ）。

決めた分数だけ集中し、短い休憩をはさんで何セットか繰り返す。
区切りごとに #集中タイマー（なければコマンドを使ったチャンネル）で知らせ、
終えたセット数を記録する。

設定をオンにすると、勉強部屋に入ったときに自動で始まり、出ると止まる。
動いているタイマーはDBにも保存し、Botを再起動しても続きから動かす。
"""

import asyncio
from datetime import datetime, timedelta

import discord
from discord import app_commands

from studybot import config
from studybot.channels import find_channel, is_channel
from studybot.config import JST
from studybot.features.badges import announce_new_badges
from studybot.groups import time_group
from studybot.habits import (
    add_focus_set,
    delete_focus_timer,
    get_focus_settings,
    get_focus_sets,
    list_focus_timers,
    save_focus_timer,
    set_focus_settings,
    update_focus_timer_done,
    validate_focus,
)
from studybot.replies import send_private


# 動いているタイマー：user_id -> {"task", "done", "sets", "auto", "channel", ...}
FOCUS_TIMERS = {}

# テストで待ち時間を短くできるように、待つ関数と今の時刻を差し替えられるようにする
_sleep = asyncio.sleep


def _now():
    return datetime.now(JST)


# Botが止まっている間に過ぎた区切りは、知らせずに記録だけする（この秒数より遅れたとき）
LATE_SECONDS = 120


def _mention(user_id):
    return discord.AllowedMentions(users=[discord.Object(id=user_id)])


def focus_end(timer, number):
    """number セット目の集中が終わる時刻。"""
    return timer["started_at"] + timedelta(
        minutes=number * timer["minutes"] + (number - 1) * timer["break_minutes"]
    )


async def _wait_until(when):
    seconds = (when - _now()).total_seconds()
    if seconds > 0:
        await _sleep(seconds)


def _is_late(when):
    return (_now() - when).total_seconds() > LATE_SECONDS


async def run_focus(channel, user_id, timer):
    minutes, break_minutes, sets = (
        timer["minutes"], timer["break_minutes"], timer["sets"]
    )
    try:
        for number in range(timer["done"] + 1, sets + 1):
            end = focus_end(timer, number)
            await _wait_until(end)
            add_focus_set(config.DB_PATH, user_id, end.date(), minutes)
            timer["done"] = number
            update_focus_timer_done(config.DB_PATH, user_id, number)
            late = _is_late(end)

            if number == sets:
                day_sets, day_minutes = get_focus_sets(
                    config.DB_PATH, user_id, end.date()
                )
                if late:
                    await channel.send(
                        f"Botが止まっている間に集中タイマーが終わりました"
                        f"（{sets}セット）。今日の集中：{day_sets}セット"
                        f"（{day_minutes}分）"
                    )
                else:
                    await channel.send(
                        f"<@{user_id}> 🎉 {sets}セット完了！おつかれさまでした。"
                        f"今日の集中：{day_sets}セット（{day_minutes}分）",
                        allowed_mentions=_mention(user_id),
                    )
                await announce_new_badges(getattr(channel, "guild", None), user_id)
                break

            if not late:
                await channel.send(
                    f"<@{user_id}> ⏰ {number}セット目おわり！"
                    f"{break_minutes}分休憩しましょう。",
                    allowed_mentions=_mention(user_id),
                )
            restart = end + timedelta(minutes=break_minutes)
            await _wait_until(restart)
            if not _is_late(restart):
                await channel.send(
                    f"<@{user_id}> ▶ {number + 1}セット目スタート"
                    f"（{minutes}分）",
                    allowed_mentions=_mention(user_id),
                )
        delete_focus_timer(config.DB_PATH, user_id)
    except asyncio.CancelledError:
        # 止めたときは stop_timer がDBから消す。Botの終了で止まったときは
        # DBに残し、次の起動で続きから動かす
        raise
    except Exception as error:
        print(f"[focus] タイマーのエラー user={user_id}: {error}")
        delete_focus_timer(config.DB_PATH, user_id)
    finally:
        if FOCUS_TIMERS.get(user_id) is timer:
            FOCUS_TIMERS.pop(user_id, None)


def start_timer(channel, user_id, minutes, break_minutes, sets, auto=False,
                started_at=None, done=0):
    """タイマーを始める。started_at を渡すと、保存してあったタイマーの続き。"""
    resumed = started_at is not None
    timer = {
        "started_at": started_at or _now(),
        "minutes": minutes,
        "break_minutes": break_minutes,
        "sets": sets,
        "done": done,
        "auto": auto,
        "channel": channel,
    }
    if not resumed:
        save_focus_timer(
            config.DB_PATH, user_id, channel.id, timer["started_at"],
            minutes, break_minutes, sets, auto,
        )
    FOCUS_TIMERS[user_id] = timer
    timer["task"] = asyncio.create_task(run_focus(channel, user_id, timer))
    return timer


def stop_timer(user_id):
    """動いているタイマーを止めて返す。なければ None。"""
    timer = FOCUS_TIMERS.pop(user_id, None)
    if timer is None:
        return None
    timer["task"].cancel()
    delete_focus_timer(config.DB_PATH, user_id)
    return timer


def notice_channel(guild, fallback=None):
    """お知らせを送るチャンネル：#集中タイマー → fallback → #勉強ログ。"""
    return (
        find_channel(guild, "focus")
        or fallback
        or find_channel(guild, "study_log")
    )


def _plan_text(minutes, break_minutes, sets):
    total = minutes * sets + break_minutes * (sets - 1)
    return (
        f"{minutes}分 × {sets}セット"
        f"（休憩{break_minutes}分・全部で約{total}分）"
    )


@time_group.command(
    name="focus",
    description="集中タイマー（例：25分集中＋5分休憩を4セット）",
)
@app_commands.describe(
    minutes="1セットの集中時間（分、省略すると25）",
    break_minutes="休憩時間（分、省略すると5）",
    sets="セット数（省略すると4）",
    stop="動いているタイマーを止める",
    auto="勉強部屋に入ったら自動で始める（この時間とセット数で）",
)
@app_commands.choices(auto=[
    app_commands.Choice(name="オン", value="on"),
    app_commands.Choice(name="オフ", value="off"),
])
async def focus(ctx, minutes: int = 25, break_minutes: int = 5, sets: int = 4,
                stop: bool = False, auto: str | None = None):
    user_id = ctx.author.id

    if auto is not None:
        try:
            set_focus_settings(
                config.DB_PATH, user_id, auto == "on",
                minutes, break_minutes, sets,
            )
        except ValueError as error:
            await send_private(ctx, str(error))
            return
        if auto == "on":
            await send_private(
                ctx,
                "勉強部屋に入ったら、自動で集中タイマー"
                f"（{_plan_text(minutes, break_minutes, sets)}）を始めます。"
                "部屋を出ると止まります。",
            )
        else:
            await send_private(ctx, "集中タイマーの自動開始をオフにしました。")
        return

    running = FOCUS_TIMERS.get(user_id)
    if stop:
        timer = stop_timer(user_id)
        if timer is None:
            await send_private(ctx, "動いている集中タイマーはありません。")
            return
        await ctx.send(
            f"⏹ 集中タイマーを止めました（{timer['done']}/{timer['sets']}"
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
    try:
        validate_focus(minutes, break_minutes, sets)
    except ValueError as error:
        await send_private(ctx, str(error))
        return

    channel = notice_channel(ctx.guild, ctx.channel)
    start_timer(channel, user_id, minutes, break_minutes, sets)
    where = ""
    if channel.id != ctx.channel.id:
        where = f"お知らせは {channel.mention} に届きます。"
    await ctx.send(
        f"⏱ 集中タイマー開始：{_plan_text(minutes, break_minutes, sets)}。"
        f"{where}止めるときは `/time focus stop:True`"
    )


# ============================================================
# 勉強部屋との連動と、再起動後の再開
# ============================================================

async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    entered = (
        is_channel(after.channel, "study_voice")
        and not is_channel(before.channel, "study_voice")
    )
    left = (
        is_channel(before.channel, "study_voice")
        and not is_channel(after.channel, "study_voice")
    )

    if entered and member.id not in FOCUS_TIMERS:
        auto, minutes, break_minutes, sets = get_focus_settings(
            config.DB_PATH, member.id
        )
        channel = notice_channel(member.guild) if auto else None
        if channel is None:
            return
        start_timer(channel, member.id, minutes, break_minutes, sets, auto=True)
        await channel.send(
            f"<@{member.id}> ⏱ 勉強部屋に入ったので集中タイマーを始めました："
            f"{_plan_text(minutes, break_minutes, sets)}",
            allowed_mentions=_mention(member.id),
        )

    if left:
        timer = FOCUS_TIMERS.get(member.id)
        # 自分で始めたタイマーは、部屋を出ても止めない
        if timer is None or not timer["auto"]:
            return
        stop_timer(member.id)
        await timer["channel"].send(
            "⏹ 勉強部屋を出たので集中タイマーを止めました"
            f"（{timer['done']}/{timer['sets']}セット完了）。"
        )


def _in_study_voice(guild, user_id):
    channel = find_channel(guild, "study_voice")
    members = getattr(channel, "members", None) or []
    return any(member.id == user_id for member in members)


async def resume_focus_timers(bot):
    """起動時に、保存してあったタイマーを続きから動かす。再開した数を返す。"""
    resumed = 0
    for saved in list_focus_timers(config.DB_PATH):
        user_id = saved["user_id"]
        if user_id in FOCUS_TIMERS:
            continue
        channel = bot.get_channel(saved["channel_id"])
        # 送り先がない、または自動のタイマーなのにもう勉強部屋にいないときは終わりにする
        if channel is None or (
            saved["auto"] and not _in_study_voice(channel.guild, user_id)
        ):
            delete_focus_timer(config.DB_PATH, user_id)
            continue
        start_timer(
            channel, user_id, saved["minutes"], saved["break_minutes"],
            saved["sets"], auto=saved["auto"],
            started_at=saved["started_at"], done=saved["done"],
        )
        resumed += 1
    if resumed:
        print(f"[focus] 集中タイマーを{resumed}件再開しました")
    return resumed


def register(bot):
    bot.add_listener(on_voice_state_update)
