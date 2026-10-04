"""解く速さ：過去問の記録に「かかった時間」を付けて、1問あたりの時間を出す。"""

import sqlite3
import unicodedata
from contextlib import closing
from datetime import timedelta


# 本番の1問あたりの目安（分）。SGは120分で科目A 48問＋科目B 12問、
# FEは科目A 90分60問・科目B 100分20問
SPEED_TARGETS = {
    "SG": {"A": 1.5, "B": 4.0},
    "FE": {"A": 1.5, "B": 5.0},
}
SPEED_DAYS = 28
MAX_MINUTES = 600


def init_speed_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS practice_minutes (
            message_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            qualification TEXT NOT NULL,
            section TEXT NOT NULL,
            questions INTEGER NOT NULL,
            minutes INTEGER NOT NULL,
            practiced_on TEXT NOT NULL
        )
    """)


def parse_minutes_input(value):
    """「かかった時間（分）」の入力。空なら None。"""
    text = unicodedata.normalize("NFKC", value or "").strip().removesuffix("分")
    if not text:
        return None
    if not text.isdigit() or not 1 <= int(text) <= MAX_MINUTES:
        raise ValueError(f"かかった時間は1〜{MAX_MINUTES}の分数で入力してください。")
    return int(text)


def save_practice_minutes(db_path, message_id, user_id, qualification, section,
                          questions, minutes, practiced_on):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO practice_minutes (
                    message_id, user_id, qualification, section, questions,
                    minutes, practiced_on
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                message_id, user_id, qualification, section, questions,
                minutes, practiced_on.isoformat(),
            ))


def speed_summary(db_path, user_id, qualification, today, days=SPEED_DAYS):
    """直近 days 日の {科目: (問題数, 分, 1問あたりの分)}。"""
    since = (today - timedelta(days=days - 1)).isoformat()
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            rows = conn.execute("""
                SELECT section, SUM(questions), SUM(minutes)
                FROM practice_minutes
                WHERE user_id = ? AND qualification = ?
                  AND practiced_on BETWEEN ? AND ?
                GROUP BY section
                ORDER BY section
            """, (user_id, qualification, since, today.isoformat())).fetchall()
    except sqlite3.Error:
        rows = []
    return {
        section: (questions, minutes, minutes / questions)
        for section, questions, minutes in rows
        if questions
    }


def per_question_text(questions, minutes):
    return f"1問 {minutes / questions:.1f}分"


def judge_speed(per_question, target):
    if target is None:
        return None
    if per_question <= target:
        return "目安内"
    if per_question <= target * 1.2:
        return "少し遅い"
    return "遅い"


def format_speed(summary, qualification):
    """例：科目A 1問 1.8分（目安 1.5分・少し遅い）。記録がなければ None。"""
    if not summary:
        return None
    targets = SPEED_TARGETS.get(qualification, {})
    lines = []
    for section, (questions, minutes, per_question) in summary.items():
        label = f"科目{section}" if targets else "1問あたり"
        text = f"{label} {per_question_text(questions, minutes)}（{questions}問・{minutes}分"
        target = targets.get(section)
        if target is not None:
            text += f"・目安 {target}分・{judge_speed(per_question, target)}"
        lines.append(text + "）")
    return "\n".join(lines)
