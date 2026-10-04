"""Botのステータス欄に、試験までの日数と連続学習日数を表示する。

メンバー一覧の StudyBot の下に「SG試験まで あと13日 ・ 連続12日」のように出る。
個人用のBotなので、いちばん最近勉強した人の情報を表示する。
"""

import sqlite3
from contextlib import closing
from datetime import datetime, time

import discord
from discord.ext import tasks

from studybot import config
from studybot.channels import is_channel
from studybot.config import JST
from studybot.stats import get_current_exam_target, get_study_streak_safe


def presence_user(db_path):
    """いちばん最近勉強した人の user_id。記録がなければ None。"""
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute("""
                SELECT user_id FROM (
                    SELECT user_id, study_date FROM study_sessions
                    UNION ALL
                    SELECT user_id, study_date FROM study_logs
                )
                ORDER BY study_date DESC
                LIMIT 1
            """).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def build_presence_text(user_id, today):
    streak = get_study_streak_safe(user_id, today)
    target = get_current_exam_target(user_id)
    exam_on = target["exam_on"]
    if exam_on is not None and exam_on >= today:
        days = (exam_on - today).days
        if days == 0:
            return f"今日は{target['label']}！ がんばって"
        return f"{target['label']}まで あと{days}日 ・ 連続{streak}日"
    return f"連続学習 {streak}日"


async def update_presence(bot, now=None):
    user_id = presence_user(config.DB_PATH)
    if user_id is None:
        return None
    text = build_presence_text(user_id, (now or datetime.now(JST)).date())
    await bot.change_presence(activity=discord.CustomActivity(name=text))
    return text


@tasks.loop(time=time(0, 0, tzinfo=JST))
async def presence_loop(bot):
    await update_presence(bot)


def register(bot):
    async def on_voice_state_update(member, before, after):
        # 勉強部屋を出たら、連続日数が変わっているかもしれないので更新する
        if (
            not member.bot
            and is_channel(before.channel, "study_voice")
            and not is_channel(after.channel, "study_voice")
        ):
            await update_presence(bot)

    bot.add_listener(on_voice_state_update)
