"""模試タイマー：本番と同じ時間を計り、途中と終わりに知らせる。

/sg mock timer:開始 で始め、半分・残り15分・終了をメンションで知らせる。
「解き終わった」ボタンで、かかった時間を入れた結果のフォームを開く
（ボタンとフォームは features/qualification.py）。
動いているタイマーはDBにも保存し、Botを再起動しても続きから動かす。
"""

import asyncio
from datetime import datetime, timedelta

import discord

from studybot import config
from studybot.config import JST
from studybot.qualifications import get_qualification
from studybot.scoring import (
    delete_mock_timer,
    get_mock_timer,
    list_mock_timers,
    save_mock_timer,
)


# 動いているタイマー：user_id -> asyncio.Task
MOCK_TIMERS = {}

# テストで待ち時間を短くできるように差し替えられるようにする
_sleep = asyncio.sleep


def _now():
    return datetime.now(JST)


# Botが止まっている間に過ぎたお知らせは送らない（この秒数より遅れたとき）
LATE_SECONDS = 120
# 終了から、この時間を過ぎたタイマーは片付ける（結果の入力を待つ時間）
KEEP_AFTER_END = timedelta(hours=12)
LAST_CALL_MINUTES = 15


def checkpoints(minutes):
    """お知らせする「残り何分」。例：120分 → [60, 15]。"""
    marks = {minutes // 2, LAST_CALL_MINUTES}
    return sorted((mark for mark in marks if 0 < mark < minutes), reverse=True)


def elapsed_minutes(timer, now=None):
    """始めてから今までの分（本番の時間を上限にする）。"""
    seconds = ((now or _now()) - timer["started_at"]).total_seconds()
    return max(1, min(round(seconds / 60), timer["minutes"]))


def _mention(user_id):
    return discord.AllowedMentions(users=[discord.Object(id=user_id)])


async def _wait_until(when):
    seconds = (when - _now()).total_seconds()
    if seconds > 0:
        await _sleep(seconds)


def _is_late(when):
    return (_now() - when).total_seconds() > LATE_SECONDS


async def run_mock_timer(channel, timer, finish_view):
    user_id = timer["user_id"]
    started_at, minutes = timer["started_at"], timer["minutes"]
    try:
        for left in checkpoints(minutes):
            when = started_at + timedelta(minutes=minutes - left)
            await _wait_until(when)
            if not _is_late(when):
                await channel.send(
                    f"<@{user_id}> ⏱ 模試 残り{left}分です。"
                    + ("見直しの時間を残しましょう。" if left <= LAST_CALL_MINUTES else ""),
                    allowed_mentions=_mention(user_id),
                )
        end = started_at + timedelta(minutes=minutes)
        await _wait_until(end)
        if not _is_late(end):
            await channel.send(
                f"<@{user_id}> ⏰ 模試の時間（{minutes}分）が終わりました。"
                "おつかれさまでした！結果を入力してください。",
                view=finish_view(),
                allowed_mentions=_mention(user_id),
            )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"[mock] タイマーのエラー user={user_id}: {error}")
    finally:
        if MOCK_TIMERS.get(user_id) is asyncio.current_task():
            MOCK_TIMERS.pop(user_id, None)


def start_mock_timer(channel, user_id, qualification, finish_view, started_at=None):
    """タイマーを始める（started_at を渡すと保存してあったタイマーの続き）。"""
    timer = {
        "user_id": user_id,
        "qualification": qualification.code,
        "started_at": started_at or _now(),
        "minutes": qualification.exam_minutes,
    }
    if started_at is None:
        save_mock_timer(
            config.DB_PATH, user_id, qualification.code, channel.id,
            timer["started_at"], timer["minutes"],
        )
    MOCK_TIMERS[user_id] = asyncio.create_task(
        run_mock_timer(channel, timer, finish_view)
    )
    return timer


def stop_mock_timer(user_id):
    """タイマーを止めて、保存していた内容を返す。なければ None。"""
    task = MOCK_TIMERS.pop(user_id, None)
    if task is not None:
        task.cancel()
    timer = get_mock_timer(config.DB_PATH, user_id)
    delete_mock_timer(config.DB_PATH, user_id)
    return timer


def timer_start_text(qualification):
    parts = "・".join(
        f"{name} {count}問" if count else name
        for name, count in qualification.exam_parts
    )
    marks = "・".join(f"残り{left}分" for left in checkpoints(qualification.exam_minutes))
    return (
        f"⏱ {qualification.code}模試のタイマーを始めました："
        f"{qualification.exam_minutes}分（{parts}）。\n"
        f"{marks}と終了のときに知らせます。"
        "解き終わったら下のボタンで結果を入力できます（時間は自動で入ります）。"
    )


async def resume_mock_timers(bot, finish_view):
    """起動時に、保存してあった模試タイマーを続きから動かす。"""
    resumed = 0
    now = _now()
    for timer in list_mock_timers(config.DB_PATH):
        user_id = timer["user_id"]
        if user_id in MOCK_TIMERS:
            continue
        channel = bot.get_channel(timer["channel_id"])
        qualification = get_qualification(timer["qualification"])
        end = timer["started_at"] + timedelta(minutes=timer["minutes"])
        if channel is None or qualification is None or now > end + KEEP_AFTER_END:
            delete_mock_timer(config.DB_PATH, user_id)
            continue
        if now > end:
            continue   # 時間は終わっていて、結果の入力を待っている
        start_mock_timer(
            channel, user_id, qualification, finish_view,
            started_at=timer["started_at"],
        )
        resumed += 1
    if resumed:
        print(f"[mock] 模試タイマーを{resumed}件再開しました")
    return resumed
