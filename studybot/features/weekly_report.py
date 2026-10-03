"""週間レポートを毎週日曜21時に自動で届ける（Notionの設定があればNotionにも保存）。"""

import asyncio
from datetime import datetime

import discord
from discord.ext import tasks

from studybot import config
from studybot.config import JST, WEEKLY_REPORT_TIME, WEEKLY_REPORT_WEEKDAY
from studybot.features.ai import (
    build_weekly_report_embed,
    create_weekly_report,
)
from studybot.features.digest import find_home_channel
from studybot.features.notion_export import save_weekly_report_to_notion
from studybot.weekly_report import (
    get_weekly_report_candidates,
    mark_weekly_report_sent,
)


async def send_weekly_report(bot, user_id, today):
    """1人分を送る。送れたら True。"""
    channel = await find_home_channel(bot, user_id)
    if channel is None:
        return False

    report = await create_weekly_report(user_id)
    if report is None:
        return False

    content = f"<@{user_id}> 今週もおつかれさまでした。週間レポートです。"
    notion_note = await save_weekly_report_to_notion(user_id, *report, today)
    if notion_note:
        content += f"\n{notion_note}"

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
