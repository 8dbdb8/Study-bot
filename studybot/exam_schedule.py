"""試験日の保存・カウントダウンと連続学習日数の計算。"""

import calendar
import re
import sqlite3
from contextlib import closing
from datetime import date, timedelta


# 試験日として選べる年数（今年を含む）
EXAM_YEAR_CHOICES = 3

WEEKDAY_LABELS = ("月", "火", "水", "木", "金", "土", "日")


def init_exam_date_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS exam_dates (
            user_id INTEGER NOT NULL,
            qualification TEXT NOT NULL,
            exam_on TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, qualification)
        )
    """)


def days_in_month(year, month):
    return calendar.monthrange(year, month)[1]


def exam_year_choices(today):
    return [today.year + offset for offset in range(EXAM_YEAR_CHOICES)]


def format_japanese_date(value):
    return (
        f"{value.year}年{value.month}月{value.day}日"
        f"（{WEEKDAY_LABELS[value.weekday()]}）"
    )


def build_exam_date(year, month, day, today):
    """選択された年月日を検証してdateを返す。"""
    if year not in exam_year_choices(today):
        raise ValueError("年は選択肢から選んでください。")
    if not 1 <= month <= 12:
        raise ValueError("月は1〜12から選んでください。")
    if not 1 <= day <= days_in_month(year, month):
        raise ValueError(
            f"{year}年{month}月に{day}日はありません。"
        )
    exam_on = date(year, month, day)
    if exam_on < today:
        raise ValueError("過去の日付は試験日に設定できません。")
    return exam_on


def save_exam_date(db_path, user_id, qualification, exam_on, updated_at):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT INTO exam_dates (
                    user_id, qualification, exam_on, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, qualification) DO UPDATE SET
                    exam_on = excluded.exam_on,
                    updated_at = excluded.updated_at
            """, (user_id, qualification, exam_on.isoformat(), updated_at))


def get_exam_date(db_path, user_id, qualification):
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("""
            SELECT exam_on
            FROM exam_dates
            WHERE user_id = ? AND qualification = ?
        """, (user_id, qualification)).fetchone()
    return date.fromisoformat(row[0]) if row else None


def delete_exam_date(db_path, user_id, qualification):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            cursor = conn.execute("""
                DELETE FROM exam_dates
                WHERE user_id = ? AND qualification = ?
            """, (user_id, qualification))
            return cursor.rowcount > 0


def days_until(exam_on, today):
    return (exam_on - today).days


def weeks_until(exam_on, today):
    """試験日までの週数（端数は切り上げ、最低1週）。"""
    return max(1, -(-days_until(exam_on, today) // 7))


def format_exam_countdown(exam_on, today, label=None):
    days = days_until(exam_on, today)
    prefix = f"{label}：" if label else ""
    date_text = format_japanese_date(exam_on)
    if days > 0:
        return (
            f"{prefix}{date_text}　あと**{days}日**"
            f"（約{weeks_until(exam_on, today)}週間）"
        )
    if days == 0:
        return f"{prefix}{date_text}　**今日が試験日です**"
    return f"{prefix}{date_text}　（{-days}日前に終了）"


def plan_week_ranges(start, weeks, exam_on=None):
    """計画の各週の期間。最終週は試験日で打ち切る。"""
    ranges = []
    for index in range(weeks):
        week_start = start + timedelta(days=7 * index)
        week_end = week_start + timedelta(days=6)
        if exam_on is not None and week_end > exam_on:
            week_end = exam_on
        if week_start > week_end:
            break
        ranges.append((index + 1, week_start, week_end))
    return ranges


def _short_date(value):
    return f"{value.month}/{value.day}（{WEEKDAY_LABELS[value.weekday()]}）"


def format_plan_schedule(start, weeks, exam_on=None):
    lines = []
    for number, week_start, week_end in plan_week_ranges(
        start, weeks, exam_on
    ):
        line = f"第{number}週：{_short_date(week_start)}〜{_short_date(week_end)}"
        if exam_on is not None and week_end == exam_on:
            line += " ※最終日が試験日"
        lines.append(line)
    return "\n".join(lines)


_WEEK_HEADING_DATES = re.compile(r"(第\d+週)\s*[（(][^）)\n]*\d+[^）)\n]*[）)]")


def strip_week_heading_dates(text):
    """AIが「第1週（10月10日〜…）」のように独自に書いた日付を消す。"""
    return _WEEK_HEADING_DATES.sub(r"\1", text)


def get_study_streak(db_path, user_id, today):
    """VC学習か勉強ログがある日の連続日数。

    今日まだ記録がなくても、昨日まで続いていれば途切れていない扱いにする。
    """
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT study_date FROM study_sessions
            WHERE user_id = ? AND duration_seconds > 0
            UNION
            SELECT study_date FROM study_logs
            WHERE user_id = ?
        """, (user_id, user_id)).fetchall()

    study_days = set()
    for (value,) in rows:
        try:
            study_days.add(date.fromisoformat(value))
        except (TypeError, ValueError):
            continue

    cursor_day = today if today in study_days else today - timedelta(days=1)
    streak = 0
    while cursor_day in study_days:
        streak += 1
        cursor_day -= timedelta(days=1)
    return streak
