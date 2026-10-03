"""毎朝の学習メニュー通知の対象者・送信記録・オンオフ設定。"""

import sqlite3
from contextlib import closing
from datetime import time


DEFAULT_DIGEST_TIME = time(7, 0)


def parse_digest_time(value):
    """"HH:MM" を time に変換する。読めなければ既定の7:00。"""
    try:
        hour, minute = (int(part) for part in str(value).strip().split(":"))
        return time(hour, minute)
    except (TypeError, ValueError):
        return DEFAULT_DIGEST_TIME


def init_daily_digest_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_digest_settings (
            user_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_digest_sent (
            user_id INTEGER NOT NULL,
            sent_on TEXT NOT NULL,
            guild_id INTEGER,
            channel_id INTEGER,
            message_id INTEGER,
            PRIMARY KEY (user_id, sent_on)
        )
    """)


def set_digest_enabled(db_path, user_id, enabled):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT INTO daily_digest_settings (user_id, enabled)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled
            """, (user_id, 1 if enabled else 0))


def is_digest_enabled(db_path, user_id):
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT enabled FROM daily_digest_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row is None or bool(row[0])


def get_digest_candidates(db_path, today):
    """今日の通知がまだで、復習・計画・試験日のどれかがある利用者。"""
    today_text = today.isoformat()
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT user_id FROM (
                SELECT user_id FROM sg_mistakes
                WHERE completed_on IS NULL AND next_review_on <= ?
                UNION
                SELECT user_id FROM sg_plans WHERE active = 1
                UNION
                SELECT user_id FROM exam_dates WHERE exam_on >= ?
            )
            WHERE user_id NOT IN (
                SELECT user_id FROM daily_digest_settings WHERE enabled = 0
            )
            AND user_id NOT IN (
                SELECT user_id FROM daily_digest_sent WHERE sent_on = ?
            )
            ORDER BY user_id
        """, (today_text, today_text, today_text)).fetchall()
    return [row[0] for row in rows]


def get_home_guild_id(db_path, user_id):
    """最近勉強した（VCかログ）サーバー。通知の投稿先に使う。"""
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("""
            SELECT guild_id FROM (
                SELECT guild_id, study_date, end_time AS at
                FROM study_sessions WHERE user_id = ?
                UNION ALL
                SELECT guild_id, study_date, created_at AS at
                FROM study_logs WHERE user_id = ?
            )
            ORDER BY study_date DESC, at DESC
            LIMIT 1
        """, (user_id, user_id)).fetchone()
    return row[0] if row else None


def mark_digest_sent(db_path, user_id, today, guild_id, channel_id, message_id):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT OR IGNORE INTO daily_digest_sent (
                    user_id, sent_on, guild_id, channel_id, message_id
                ) VALUES (?, ?, ?, ?, ?)
            """, (user_id, today.isoformat(), guild_id, channel_id, message_id))
