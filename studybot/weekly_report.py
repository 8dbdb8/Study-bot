"""週間レポートの自動送信の対象者・送信記録・オンオフ設定。"""

import sqlite3
from contextlib import closing
from datetime import timedelta


def init_weekly_report_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weekly_report_settings (
            user_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weekly_report_sent (
            user_id INTEGER NOT NULL,
            week_start TEXT NOT NULL,
            guild_id INTEGER,
            channel_id INTEGER,
            message_id INTEGER,
            PRIMARY KEY (user_id, week_start)
        )
    """)


def week_start_of(day):
    """その週の月曜日。"""
    return day - timedelta(days=day.weekday())


def set_weekly_report_enabled(db_path, user_id, enabled):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT INTO weekly_report_settings (user_id, enabled)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled
            """, (user_id, 1 if enabled else 0))


def is_weekly_report_enabled(db_path, user_id):
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT enabled FROM weekly_report_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row is None or bool(row[0])


def get_weekly_report_candidates(db_path, today):
    """今週（月曜〜today）に勉強の記録があり、今週分をまだ送っていない人。"""
    monday = week_start_of(today).isoformat()
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT user_id FROM (
                SELECT user_id FROM study_sessions
                WHERE duration_seconds > 0
                  AND study_date BETWEEN ? AND ?
                UNION
                SELECT user_id FROM study_logs
                WHERE study_date BETWEEN ? AND ?
            )
            WHERE user_id NOT IN (
                SELECT user_id FROM weekly_report_settings WHERE enabled = 0
            )
            AND user_id NOT IN (
                SELECT user_id FROM weekly_report_sent WHERE week_start = ?
            )
            ORDER BY user_id
        """, (
            monday, today.isoformat(), monday, today.isoformat(), monday,
        )).fetchall()
    return [row[0] for row in rows]


def mark_weekly_report_sent(db_path, user_id, today, guild_id, channel_id,
                            message_id):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT OR IGNORE INTO weekly_report_sent (
                    user_id, week_start, guild_id, channel_id, message_id
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                user_id, week_start_of(today).isoformat(),
                guild_id, channel_id, message_id,
            ))
