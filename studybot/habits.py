"""毎日の習慣：1日の目標時間、集中タイマーのセット数、休んだ日。"""

import sqlite3
from contextlib import closing
from datetime import date

from studybot.daily_digest import is_day_off


def init_habit_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS study_goals (
            user_id INTEGER PRIMARY KEY,
            weekday_minutes INTEGER,
            holiday_minutes INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS focus_sets (
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            sets INTEGER NOT NULL DEFAULT 0,
            minutes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, day)
        )
    """)


# ------------------------------------------------------------
# 1日の目標時間
# ------------------------------------------------------------

def set_study_goal(db_path, user_id, weekday_minutes=None, holiday_minutes=None):
    """省略した方は今の設定のまま。0 にするとその日の目標をなくす。"""
    for minutes in (weekday_minutes, holiday_minutes):
        if minutes is not None and not 0 <= minutes <= 720:
            raise ValueError("目標時間は0〜720分で指定してください。")
    current = get_study_goal(db_path, user_id)
    weekday = weekday_minutes if weekday_minutes is not None else current[0]
    holiday = holiday_minutes if holiday_minutes is not None else current[1]
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT INTO study_goals (user_id, weekday_minutes, holiday_minutes)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    weekday_minutes = excluded.weekday_minutes,
                    holiday_minutes = excluded.holiday_minutes
            """, (user_id, weekday or None, holiday or None))
    return weekday or None, holiday or None


def get_study_goal(db_path, user_id):
    """(平日の目標分, 土日祝の目標分)。未設定は None。"""
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute("""
                SELECT weekday_minutes, holiday_minutes FROM study_goals
                WHERE user_id = ?
            """, (user_id,)).fetchone()
    except sqlite3.Error:
        row = None
    return (row[0], row[1]) if row else (None, None)


def goal_for_day(db_path, user_id, day):
    weekday, holiday = get_study_goal(db_path, user_id)
    return holiday if is_day_off(day) else weekday


def format_goal_progress(studied_seconds, goal_minutes):
    """例：`▰▰▰▱▱` 45/30分 ✓。目標がなければ None。"""
    if not goal_minutes:
        return None
    minutes = int(studied_seconds) // 60
    ratio = min(minutes / goal_minutes, 1.0)
    filled = round(ratio * 10)
    bar = "▰" * filled + "▱" * (10 - filled)
    done = " ✓ 達成" if minutes >= goal_minutes else ""
    return f"`{bar}` {minutes}/{goal_minutes}分{done}"


# ------------------------------------------------------------
# 集中タイマー
# ------------------------------------------------------------

def add_focus_set(db_path, user_id, day, minutes):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT INTO focus_sets (user_id, day, sets, minutes)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(user_id, day) DO UPDATE SET
                    sets = sets + 1,
                    minutes = minutes + excluded.minutes
            """, (user_id, day.isoformat(), minutes))


def get_focus_sets(db_path, user_id, day):
    """その日に終えた (セット数, 集中した分)。"""
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute("""
                SELECT sets, minutes FROM focus_sets
                WHERE user_id = ? AND day = ?
            """, (user_id, day.isoformat())).fetchone()
    except sqlite3.Error:
        row = None
    return (row[0], row[1]) if row else (0, 0)


# ------------------------------------------------------------
# 休んだ日（「今日は休む」）
# ------------------------------------------------------------

def get_rest_days(db_path, user_id, start, end):
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            rows = conn.execute("""
                SELECT rest_on FROM study_rest_days
                WHERE user_id = ? AND rest_on BETWEEN ? AND ?
            """, (user_id, start.isoformat(), end.isoformat())).fetchall()
    except sqlite3.Error:
        rows = []
    return {date.fromisoformat(row[0]) for row in rows}
