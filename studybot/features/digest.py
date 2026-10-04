"""学習メニュー（平日は夜・土日祝は朝）の組み立てと送信。"""

import asyncio
from datetime import datetime, time as dt_time

import discord
from discord.ext import tasks

from studybot import config
from studybot.channels import find_channel
from studybot.config import (
    DIGEST_CATCH_UP_UNTIL_HOUR,
    DIGEST_TIME_HOLIDAY,
    DIGEST_TIME_WEEKDAY,
    JST,
    REVIEW_SCORE_THRESHOLD,
)
from studybot.daily_digest import (
    digest_datetime,
    get_digest_candidates,
    get_home_guild_id,
    mark_digest_sent,
)
from studybot.embeds import build_digest_embed, build_plan_status_embed
from studybot.exam_results import FINAL_STRETCH_DAYS
from studybot.exam_schedule import format_exam_countdown
from studybot.features.review import build_review_session
from studybot.qualifications import current_qualification
from studybot.replies import respond_private
from studybot.sg_features import (
    get_sg_category_progress,
    get_sg_mistakes,
    get_sg_plan_status,
)
from studybot.stats import (
    get_current_exam_target,
    get_exam_countdown_line,
    get_study_streak_safe,
)


# ============================================================
# 学習メニュー（平日は夜・土日祝は朝）
# ============================================================

class DailyDigestView(discord.ui.View):
    """押した本人のデータで動くので、だれが押しても安全。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="復習を始める",
        style=discord.ButtonStyle.primary,
        custom_id="studybot:digest:review",
    )
    async def review_button(self, interaction, button):
        content, view = build_review_session(interaction.user.id)
        if view is None:
            await respond_private(interaction, content)
        else:
            await respond_private(interaction, embed=content, view=view)

    @discord.ui.button(
        label="今週の計画",
        style=discord.ButtonStyle.secondary,
        custom_id="studybot:digest:plan",
    )
    async def plan_button(self, interaction, button):
        status_data = get_sg_plan_status(
            config.DB_PATH, interaction.user.id,
            today=datetime.now(JST).date(),
            qualification=current_qualification(config.DB_PATH).code,
        )
        if status_data is None:
            await respond_private(
                interaction,
                "保存済みの計画がありません。`/plan new` で作成できます。",
            )
            return
        await respond_private(interaction, embed=build_plan_status_embed(
            status_data, get_exam_countdown_line(interaction.user.id)
        ))


def build_daily_digest_embed(user_id, today):
    # 復習はすべての資格の分、計画と弱い分野は今学習中の資格の分
    qualification = current_qualification(config.DB_PATH)
    due_items = get_sg_mistakes(
        config.DB_PATH, user_id, today=today, due_only=True
    )

    plan_week = None
    plan = get_sg_plan_status(
        config.DB_PATH, user_id, today=today,
        qualification=qualification.code,
    )
    if plan and not plan["completed"] and plan["rows"]:
        plan_week = plan["rows"][-1]

    items, _ = get_sg_category_progress(
        config.DB_PATH, user_id, qualification.code
    )
    weak = [
        (item["category"], item["latest_score"])
        for item in items
        if item["questions"] >= 10
        and item["latest_score"] is not None
        and item["latest_score"] < REVIEW_SCORE_THRESHOLD
    ]

    target = get_current_exam_target(user_id)
    countdown = None
    final_stretch = None
    if target["exam_on"] is not None and target["exam_on"] >= today:
        countdown = format_exam_countdown(
            target["exam_on"], today, target["label"]
        )
        days_left = (target["exam_on"] - today).days
        if days_left <= FINAL_STRETCH_DAYS:
            final_stretch = {
                "days_left": days_left,
                "label": target["label"],
                "focus": final_stretch_focus(weak),
                "unfinished": len(get_sg_mistakes(
                    config.DB_PATH, user_id, today=today, due_only=False,
                    qualification=qualification.code,
                )),
            }

    return build_digest_embed(
        today,
        countdown,
        get_study_streak_safe(user_id, today),
        due_items,
        plan_week,
        weak,
        REVIEW_SCORE_THRESHOLD,
        final_stretch,
        qualification.code,
    )


# 直前モードで弱い分野に割り当てる、今日の問題数（弱い順）
FINAL_STRETCH_QUOTAS = (20, 10, 10)


def final_stretch_focus(weak):
    """直前モードの「今日の重点」。[(項目, 量), ...]"""
    weakest = sorted(weak, key=lambda item: item[1])[:len(FINAL_STRETCH_QUOTAS)]
    focus = [
        (f"{category}（直近 {score:.0f}%）", f"{quota}問")
        for (category, score), quota in zip(weakest, FINAL_STRETCH_QUOTAS)
    ]
    if not focus:
        focus = [("総合演習（全分野）", "20問")]
    focus.append(("科目B", "1セット"))
    return focus


async def find_home_channel(bot, user_id):
    """その人が最近勉強したサーバーの勉強ログチャンネル。送れなければ None。"""
    guild_id = get_home_guild_id(config.DB_PATH, user_id)
    guild = bot.get_guild(guild_id) if guild_id else None
    channel = find_channel(guild, "study_log")
    if channel is None:
        print(f"[notify] 送信先が見つかりません: user={user_id}")
        return None

    try:
        await guild.fetch_member(user_id)
    except discord.NotFound:
        return None
    return channel


async def send_daily_digest(bot, user_id, today, now=None):
    """1人分を送る。送れたら True。"""
    embed = build_daily_digest_embed(user_id, today)
    if embed is None:
        return False

    channel = await find_home_channel(bot, user_id)
    if channel is None:
        return False

    now = now or datetime.now(JST)
    if now.hour < 11:
        greeting = "おはようございます。"
    elif now.hour >= 17:
        greeting = "おつかれさまです。"
    else:
        greeting = ""
    greeting += "今日の学習メニューです。"
    message = await channel.send(
        content=f"<@{user_id}> {greeting}",
        embed=embed,
        view=DailyDigestView(),
        allowed_mentions=discord.AllowedMentions(
            users=[discord.Object(id=user_id)]
        ),
    )
    mark_digest_sent(
        config.DB_PATH, user_id, today, channel.guild.id, channel.id,
        message.id,
    )
    return True


_digest_lock = asyncio.Lock()


async def send_daily_digests(bot, now=None):
    now = now or datetime.now(JST)
    today = now.date()
    sent = 0
    # 定時実行と起動時の取りこぼし送信が重ならないようにする
    async with _digest_lock:
        for user_id in get_digest_candidates(config.DB_PATH, today):
            try:
                if await send_daily_digest(bot, user_id, today, now):
                    sent += 1
            except Exception as error:
                print(f"[digest] 送信エラー user={user_id}: {error}")
    if sent:
        print(f"[digest] 学習メニューを{sent}件送信しました")
    return sent


def is_digest_due(now):
    """今日の学習メニューを送る時刻を過ぎていて、まだ遅すぎないか。

    平日は夜、土日祝は朝に送る。起動が遅れた日も、この判定で
    当日分をあとから送る（同じ日に二度は送らない）。
    """
    digest_at = digest_datetime(
        now.date(), DIGEST_TIME_WEEKDAY, DIGEST_TIME_HOLIDAY, JST
    )
    return digest_at <= now and now.hour < DIGEST_CATCH_UP_UNTIL_HOUR


# 平日用と休日用の両方の時刻に起き、今日がどちらの日かは is_digest_due で判断する
@tasks.loop(time=[
    dt_time(DIGEST_TIME_WEEKDAY.hour, DIGEST_TIME_WEEKDAY.minute, tzinfo=JST),
    dt_time(DIGEST_TIME_HOLIDAY.hour, DIGEST_TIME_HOLIDAY.minute, tzinfo=JST),
])
async def daily_digest_loop(bot):
    now = datetime.now(JST)
    if is_digest_due(now):
        await send_daily_digests(bot, now)
