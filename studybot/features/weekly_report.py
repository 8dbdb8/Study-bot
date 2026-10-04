"""週間レポートを毎週日曜21時に #ai-report へ自動で届ける。

Notion への保存は、日曜の勉強がすべて入るよう23:59に別に行う
（features/notion_export.py）。
"""

import asyncio
from datetime import datetime, timedelta

import discord
from discord.ext import tasks

from studybot import config
from studybot.config import (
    JST,
    NOTION_CATCH_UP_UNTIL_HOUR,
    NOTION_SAVE_TIME,
    WEEKLY_REPORT_TIME,
    WEEKLY_REPORT_WEEKDAY,
)
from studybot.features.ai import (
    build_weekly_report_embed,
    create_weekly_report,
)
from studybot.features.digest import find_home_channel
from studybot.features.notion_export import (
    is_notion_configured,
    try_save_weekly_report,
)
from studybot.notion_store import (
    get_notion_save_candidates,
    mark_notion_final_save,
)
from studybot.weekly_report import (
    get_weekly_report_candidates,
    mark_weekly_report_sent,
)


async def send_weekly_report(bot, user_id, today):
    """1人分を送る。送れたら True。"""
    channel = await find_home_channel(bot, user_id, "ai_report")
    if channel is None:
        return False

    report = await create_weekly_report(user_id, today)
    if report is None:
        return False

    content = f"<@{user_id}> 今週もおつかれさまでした。週間レポートです。"

    message = await channel.send(
        content=content,
        embed=build_weekly_report_embed(*report),
        allowed_mentions=discord.AllowedMentions(
            users=[discord.Object(id=user_id)]
        ),
    )
    mark_weekly_report_sent(
        config.DB_PATH, user_id, today, channel.guild.id, channel.id,
        message.id,
    )
    return True


_report_lock = asyncio.Lock()


async def send_weekly_reports(bot, now=None):
    now = now or datetime.now(JST)
    today = now.date()
    sent = 0
    # 定時実行と起動時の取りこぼし送信が重ならないようにする
    async with _report_lock:
        for user_id in get_weekly_report_candidates(config.DB_PATH, today):
            try:
                if await send_weekly_report(bot, user_id, today):
                    sent += 1
            except Exception as error:
                print(f"[weekly] 送信エラー user={user_id}: {error}")
    if sent:
        print(f"[weekly] 週間レポートを{sent}件送信しました")
    return sent


def is_weekly_report_due(now):
    """日曜21時を過ぎていれば、その日のうちは送る（起動が遅れた場合も）。"""
    return (
        now.weekday() == WEEKLY_REPORT_WEEKDAY
        and now.time() >= WEEKLY_REPORT_TIME
    )


@tasks.loop(time=WEEKLY_REPORT_TIME.replace(tzinfo=JST))
async def weekly_report_loop(bot):
    now = datetime.now(JST)
    if is_weekly_report_due(now):
        await send_weekly_reports(bot, now)


# ============================================================
# Notion への保存（日曜23:59）
# ============================================================

async def save_week_to_notion(bot, user_id, week_day, now=None):
    """week_day を含む週を Notion に保存し、#ai-report に知らせる。成功で True。"""
    report = await create_weekly_report(user_id, week_day)
    if report is None:
        return False

    url, error = await try_save_weekly_report(user_id, *report, week_day)
    monday = week_day - timedelta(days=week_day.weekday())
    period = f"{monday.month}/{monday.day}〜{week_day.month}/{week_day.day}"
    if error:
        text = f"⚠️ {period}の記録をNotionに保存できませんでした：{error}"
    else:
        text = f"📝 {period}の記録をNotionに保存しました：{url}".rstrip("：")
        mark_notion_final_save(
            config.DB_PATH, user_id, week_day,
            (now or datetime.now(JST)).isoformat(),
        )

    channel = await find_home_channel(bot, user_id, "ai_report")
    if channel is not None:
        await channel.send(
            content=text, allowed_mentions=discord.AllowedMentions.none()
        )
    return error is None


_notion_lock = asyncio.Lock()


async def save_weeks_to_notion(bot, week_day, now=None):
    if not is_notion_configured():
        return 0
    saved = 0
    async with _notion_lock:
        for user_id in get_notion_save_candidates(config.DB_PATH, week_day):
            try:
                if await save_week_to_notion(bot, user_id, week_day, now):
                    saved += 1
            except Exception as error:
                print(f"[notion] 保存エラー user={user_id}: {error}")
    return saved


def notion_save_week_day(now):
    """今 Notion に保存すべき週の日曜日。保存しない時間なら None。

    日曜23:59以降はその週、月曜の昼までは（止まっていた分の）前の週。
    """
    if now.weekday() == 6 and now.time() >= NOTION_SAVE_TIME:
        return now.date()
    if now.weekday() == 0 and now.hour < NOTION_CATCH_UP_UNTIL_HOUR:
        return now.date() - timedelta(days=1)
    return None


@tasks.loop(time=NOTION_SAVE_TIME.replace(tzinfo=JST))
async def notion_save_loop(bot):
    now = datetime.now(JST)
    week_day = notion_save_week_day(now)
    if week_day is not None:
        await save_weeks_to_notion(bot, week_day, now)
