"""時間帯ごとの分析：朝・昼・夜・深夜のどこでどれだけ勉強し、正答率はどうか。

勉強時間は勉強部屋に入った時刻、正答率は記録した時刻で時間帯を決める。
"""

import sqlite3
from contextlib import closing
from datetime import datetime

from studybot.config import JST


# (名前, 開始時, 終了時)。表示はこの順
SLOTS = (("朝", 5, 11), ("昼", 11, 17), ("夜", 17, 24), ("深夜", 0, 5))
# 正答率を比べるのに必要な、時間帯ごとの最低の問題数
MIN_QUESTIONS = 10


def slot_of(hour):
    for name, start, end in SLOTS:
        if start <= hour < end:
            return name
    return SLOTS[0][0]


def _hour(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is not None:
        moment = moment.astimezone(JST)
    return moment.hour


def time_of_day_stats(db_path, user_id, start, end):
    """{時間帯: {"minutes", "questions", "score"}}。score は問題が少なければ None。"""
    stats = {
        name: {"minutes": 0.0, "questions": 0, "correct": 0.0}
        for name, _, _ in SLOTS
    }
    with closing(sqlite3.connect(db_path)) as conn:
        sessions = conn.execute("""
            SELECT start_time, duration_seconds FROM study_sessions
            WHERE user_id = ? AND study_date BETWEEN ? AND ?
        """, (user_id, start.isoformat(), end.isoformat())).fetchall()
        logs = conn.execute("""
            SELECT l.created_at, a.questions, a.correct_answers, a.score_percent
            FROM study_log_analysis AS a
            JOIN study_logs AS l ON l.message_id = a.message_id
            WHERE a.user_id = ? AND l.study_date BETWEEN ? AND ?
              AND a.questions > 0
        """, (user_id, start.isoformat(), end.isoformat())).fetchall()

    for started, seconds in sessions:
        try:
            stats[slot_of(_hour(started))]["minutes"] += (seconds or 0) / 60
        except ValueError:
            continue
    for created, questions, correct, score in logs:
        if correct is None and score is None:
            continue
        try:
            slot = stats[slot_of(_hour(created))]
        except ValueError:
            continue
        slot["questions"] += questions
        slot["correct"] += correct if correct is not None else questions * score / 100

    for slot in stats.values():
        slot["score"] = (
            slot["correct"] / slot["questions"] * 100
            if slot["questions"] >= MIN_QUESTIONS else None
        )
        del slot["correct"]
    return stats


def best_slot(stats):
    """正答率がいちばん高い時間帯 (名前, 正答率)。比べられなければ None。"""
    scored = [(name, slot["score"]) for name, slot in stats.items() if slot["score"] is not None]
    if len(scored) < 2:
        return None
    return max(scored, key=lambda pair: pair[1])
