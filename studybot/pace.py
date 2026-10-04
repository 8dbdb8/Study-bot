"""試験日から逆算したペース：今のペースで試験日までにどれだけ解けるか。"""

import sqlite3
from contextlib import closing
from datetime import timedelta

from studybot.sg_features import get_sg_category_progress


PACE_DAYS = 14
# 未着手の分野名を出す数（それより多いときは「ほかN分野」）
NAMES_SHOWN = 3


def daily_question_average(db_path, user_id, qualification, today, days=PACE_DAYS):
    """直近 days 日の1日あたりの問題数（科目A・Bの合計）。"""
    since = (today - timedelta(days=days - 1)).isoformat()
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            total = conn.execute("""
                SELECT COALESCE(SUM(a.questions), 0)
                FROM study_log_analysis AS a
                JOIN study_logs AS l ON l.message_id = a.message_id
                WHERE a.user_id = ? AND a.qualification = ?
                  AND l.study_date BETWEEN ? AND ?
            """, (user_id, qualification, since, today.isoformat())).fetchone()[0]
    except sqlite3.Error:
        total = 0
    return total / days


def untouched_categories(db_path, user_id, qualification):
    try:
        items, _ = get_sg_category_progress(db_path, user_id, qualification)
    except sqlite3.Error:
        return []
    return [item["category"] for item in items if not item["questions"]]


def _names(categories):
    shown = "・".join(categories[:NAMES_SHOWN])
    if len(categories) > NAMES_SHOWN:
        shown += f" ほか{len(categories) - NAMES_SHOWN}分野"
    return shown


def build_pace_lines(db_path, user_id, qualification, today, exam_on):
    """学習メニューに出すペースの行。試験日がなければ空。"""
    if exam_on is None or exam_on <= today:
        return []
    days_left = (exam_on - today).days
    average = daily_question_average(db_path, user_id, qualification, today)
    if average:
        lines = [
            f"📊 直近2週間は1日 {average:.0f}問 → 試験日までに約{round(average * days_left)}問"
        ]
    else:
        lines = ["📊 直近2週間の問題の記録がありません。今日から少しずつ進めましょう"]

    untouched = untouched_categories(db_path, user_id, qualification)
    if untouched:
        count = len(untouched)
        head = f"⚠️ まだ解いていない分野が{count}つ（{_names(untouched)}）。"
        if count > days_left:
            per_day = -(-count // days_left)
            lines.append(head + f"1日{per_day}分野ずつ進める必要があります")
        elif days_left // count >= 2:
            lines.append(head + f"{days_left // count}日に1分野ずつ進めると試験までに一巡できます")
        else:
            lines.append(head + "1日1分野ずつ進めると試験までに一巡できます")
    return lines
