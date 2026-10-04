"""/time グループ：勉強時間とログ。"""

import asyncio
from datetime import datetime, timedelta

from discord import app_commands

from studybot import config
from studybot.charts import render_calendar, render_study_time_chart
from studybot.config import JST
from studybot.embeds import build_today_embed, build_week_embed
from studybot.groups import time_group
from studybot.habits import format_goal_progress, goal_for_day, get_rest_days
from studybot.replies import send_png
from studybot.stats import (
    get_daily_study_seconds,
    get_exam_countdown_line,
    get_study_streak_safe,
    get_today_logs,
    get_today_total,
    get_week_total,
)


@time_group.command(
    name="today",
    description="今日の勉強時間と連続学習日数を表示"
)
async def today(ctx):
    total_seconds = get_today_total(ctx.author.id)
    goal_text = format_goal_progress(
        total_seconds,
        goal_for_day(config.DB_PATH, ctx.author.id, datetime.now(JST).date()),
    )
    await ctx.send(embed=build_today_embed(
        total_seconds,
        get_study_streak_safe(ctx.author.id),
        get_exam_countdown_line(ctx.author.id),
        goal_text,
    ))


@time_group.command(
    name="week",
    description="今週の勉強時間を曜日別に表示"
)
async def week(ctx):
    rows = get_week_total(
        ctx.author.id
    )

    if not rows:
        await ctx.send(
            "📅 今週はまだ勉強時間の"
            "記録がありません。"
        )
        return

    await ctx.send(embed=build_week_embed(
        rows,
        datetime.now(JST).date(),
        get_study_streak_safe(ctx.author.id),
    ))


@time_group.command(
    name="logs",
    description="今日の勉強ログを表示"
)
async def logs(ctx):
    logs_data = get_today_logs(
        ctx.author.id
    )

    if not logs_data:
        await ctx.send(
            "📝 今日はまだ勉強ログが"
            "ありません。"
        )
        return

    text = "\n".join(
        f"・{log}"
        for log in logs_data
    )

    # Discord 2000文字制限を軽く回避
    if len(text) > 1700:
        text = (
            text[:1700]
            + "\n…（以下省略）"
        )

    await ctx.send(
        "📝 **今日の勉強ログ**\n"
        f"{text}"
    )


CHART_PERIOD_CHOICES = [
    app_commands.Choice(name="2週間", value=14),
    app_commands.Choice(name="4週間", value=28),
    app_commands.Choice(name="12週間", value=84),
]


@time_group.command(
    name="chart",
    description="日ごとの勉強時間をグラフで表示"
)
@app_commands.describe(days="表示する期間（省略すると4週間）")
@app_commands.choices(days=CHART_PERIOD_CHOICES)
async def time_chart(ctx, days: int = 28):
    if days not in {choice.value for choice in CHART_PERIOD_CHOICES}:
        days = 28
    end = datetime.now(JST).date()
    start = end - timedelta(days=days - 1)
    daily = get_daily_study_seconds(ctx.author.id, start, end)
    if not daily:
        await ctx.send("この期間の勉強時間の記録がありません。")
        return

    async with ctx.typing():
        png = await asyncio.to_thread(
            render_study_time_chart, daily, start, end
        )
    await send_png(ctx, png, "study_time.png")


CALENDAR_PERIOD_CHOICES = [
    app_commands.Choice(name="1か月", value=5),
    app_commands.Choice(name="3か月", value=13),
    app_commands.Choice(name="半年", value=26),
]


@time_group.command(
    name="calendar",
    description="学習カレンダー（勉強した日を色で表示）",
)
@app_commands.describe(weeks="表示する期間（省略すると3か月）")
@app_commands.choices(weeks=CALENDAR_PERIOD_CHOICES)
async def time_calendar(ctx, weeks: int = 13):
    if weeks not in {choice.value for choice in CALENDAR_PERIOD_CHOICES}:
        weeks = 13
    end = datetime.now(JST).date()
    start = end - timedelta(days=end.weekday() + 7 * (weeks - 1))
    daily = get_daily_study_seconds(ctx.author.id, start, end)
    rest_days = get_rest_days(config.DB_PATH, ctx.author.id, start, end)

    async with ctx.typing():
        png = await asyncio.to_thread(
            render_calendar, daily, rest_days, end, weeks
        )
    await send_png(ctx, png, "calendar.png")
