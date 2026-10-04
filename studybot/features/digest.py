"""学習メニュー（平日は夜・土日祝は朝）の組み立てと送信。"""

import asyncio
from datetime import datetime, time as dt_time, timedelta

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
    REST_DAY_INTERVAL_DAYS,
    digest_datetime,
    get_digest_candidates,
    get_home_guild_id,
    mark_digest_sent,
    take_rest_day,
)
from studybot.checklist import (
    build_checklist_items,
    checklist_progress,
    format_checklist,
    save_checklist,
)
from studybot.embeds import build_digest_embed, build_plan_status_embed
from studybot.exam_prep import get_exam_prep
from studybot.exam_results import FINAL_STRETCH_DAYS
from studybot.habits import format_goal_progress, goal_for_day
from studybot.pace import build_pace_lines
from studybot.scoring import has_recent_mock, predict_score, prediction_summary
from studybot.exam_schedule import format_exam_countdown
from studybot.features.review import build_review_session
from studybot.forms import open_quick_log
from studybot.qualifications import current_qualification
from studybot.replies import respond_private
from studybot.sg_features import (
    get_weak_categories,
    get_sg_mistakes,
    get_sg_plan_status,
)
from studybot.stats import (
    get_current_exam_target,
    get_exam_countdown_line,
    get_study_streak_safe,
    get_today_total,
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
            await respond_private(
                interaction, embed=content, view=view, files=view.files()
            )

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

    @discord.ui.button(
        label="弱点の問題を記録",
        style=discord.ButtonStyle.secondary,
        custom_id="studybot:digest:log",
    )
    async def log_button(self, interaction, button):
        qualification = current_qualification(config.DB_PATH)
        await open_quick_log(
            interaction,
            qualification,
            weakest_category(interaction.user.id, qualification),
        )

    @discord.ui.button(
        label="今日は休む",
        style=discord.ButtonStyle.secondary,
        custom_id="studybot:digest:rest",
    )
    async def rest_button(self, interaction, button):
        today = datetime.now(JST).date()
        result, day = take_rest_day(config.DB_PATH, interaction.user.id, today)
        if result == "too_soon":
            next_day = day + timedelta(days=REST_DAY_INTERVAL_DAYS)
            message = (
                f"休みは1週間に1回までです（前回：{day.month}/{day.day}）。"
                f"次に休めるのは {next_day.month}/{next_day.day} からです。"
            )
        else:
            streak = get_study_streak_safe(interaction.user.id, today)
            message = (
                "今日はお休みにしました。ゆっくり休んでください。\n"
                f"🔥 連続学習 {streak}日は途切れません"
                "（休みの日は日数に数えません）。"
            )
        await respond_private(interaction, message)


def weakest_category(user_id, qualification):
    """直近の正答率が一番低い分野（10問以上解いたもの）。なければ None。"""
    weak = get_weak_categories(
        config.DB_PATH, user_id, qualification.code, threshold=None
    )
    return weak[0][0] if weak else None


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

    weak = get_weak_categories(
        config.DB_PATH, user_id, qualification.code, REVIEW_SCORE_THRESHOLD
    )

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
                "focus": final_stretch_focus(
                    weak,
                    suggest_mock=not has_recent_mock(
                        config.DB_PATH, user_id, qualification.code, today
                    ),
                ),
                "unfinished": len(get_sg_mistakes(
                    config.DB_PATH, user_id, today=today, due_only=False,
                    qualification=qualification.code,
                )),
            }

    # 試験当日は会場メモだけ。ほかの日は予想得点・目標とチェックリストを付ける
    checklist_items = []
    exam_day = final_stretch is not None and final_stretch["days_left"] == 0
    if exam_day:
        extra_lines = exam_day_lines(user_id, target)
    else:
        extra_lines = digest_extra_lines(user_id, qualification, today)
        checklist_items = build_checklist_items(
            qualification, len(due_items), weak, final_stretch is not None,
            goal_for_day(config.DB_PATH, user_id, today),
        )
    embed = build_digest_embed(
        today,
        countdown,
        get_study_streak_safe(user_id, today),
        due_items,
        plan_week,
        weak,
        REVIEW_SCORE_THRESHOLD,
        final_stretch,
        qualification.code,
        extra_lines,
        format_checklist(checklist_progress(
            config.DB_PATH, user_id, today, checklist_items
        )),
    )
    if embed is not None and checklist_items:
        save_checklist(config.DB_PATH, user_id, today, checklist_items)
    return embed


def exam_day_lines(user_id, target):
    """試験当日の学習メニューに出す、前日に書いた会場メモ。"""
    prep = get_exam_prep(
        config.DB_PATH, user_id, target["qualification"],
        target["exam_on"].isoformat(),
    )
    return [f"📍 {prep['memo']}"] if prep["memo"] else []


def digest_extra_lines(user_id, qualification, today):
    """学習メニューの本文に足す行（ペース・予想得点・今日の目標）。"""
    lines = build_pace_lines(
        config.DB_PATH, user_id, qualification.code, today,
        get_current_exam_target(user_id)["exam_on"],
    )
    summary = prediction_summary(
        predict_score(config.DB_PATH, user_id, qualification, today),
        qualification,
    )
    if summary:
        lines.append(f"📈 {summary}")
    goal = goal_for_day(config.DB_PATH, user_id, today)
    if goal:
        progress = format_goal_progress(get_today_total(user_id), goal)
        lines.append(f"🎯 今日の目標 {progress}")
    return lines


# 直前モードで弱い分野に割り当てる、今日の問題数（弱い順）
FINAL_STRETCH_QUOTAS = (20, 10, 10)


def final_stretch_focus(weak, suggest_mock=False):
    """直前モードの「今日の重点」。[(項目, 量), ...]

    suggest_mock：この1週間に模試をしていなければ「模試を1回」を足す。
    """
    weakest = sorted(weak, key=lambda item: item[1])[:len(FINAL_STRETCH_QUOTAS)]
    focus = [
        (f"{category}（直近 {score:.0f}%）", f"{quota}問")
        for (category, score), quota in zip(weakest, FINAL_STRETCH_QUOTAS)
    ]
    if not focus:
        focus = [("総合演習（全分野）", "20問")]
    focus.append(("科目B", "1セット"))
    if suggest_mock:
        focus.append(("模試（本番形式）", "今週1回"))
    return focus


async def find_home_channel(bot, user_id, kind="study_log"):
    """その人が最近勉強したサーバーのチャンネル。送れなければ None。

    kind のチャンネル（例：ai_report）がなければ、勉強ログに送る。
    """
    guild_id = get_home_guild_id(config.DB_PATH, user_id)
    guild = bot.get_guild(guild_id) if guild_id else None
    channel = find_channel(guild, kind)
    if channel is None and kind != "study_log":
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
