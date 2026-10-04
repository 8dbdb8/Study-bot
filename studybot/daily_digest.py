"""学習メニュー通知の送る時刻・対象者・送信記録・オンオフ設定。"""

import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta

import jpholiday


DEFAULT_DIGEST_TIME = time(7, 0)


def parse_digest_time(value, default=DEFAULT_DIGEST_TIME):
    """"HH:MM" を time に変換する。読めなければ default。"""
    try:
        hour, minute = (int(part) for part in str(value).strip().split(":"))
        return time(hour, minute)
    except (TypeError, ValueError):
        return default


def is_day_off(day):
    """土日か日本の祝日（振替休日を含む）。"""
    return day.weekday() >= 5 or jpholiday.is_holiday(day)


def digest_datetime(day, weekday_time, holiday_time, tzinfo):
    """その日の学習メニューを送る日時。平日は weekday_time、休日は holiday_time。"""
    send_time = holiday_time if is_day_off(day) else weekday_time
    return datetime.combine(day, send_time, tzinfo=tzinfo)


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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS study_rest_days (
            user_id INTEGER NOT NULL,
            rest_on TEXT NOT NULL,
            PRIMARY KEY (user_id, rest_on)
        )
    """)


# 「今日は休む」は1週間（7日）に1回まで
REST_DAY_INTERVAL_DAYS = 7


def take_rest_day(db_path, user_id, today):
    """今日を休みにする。(結果, 日付) を返す。

    結果は "ok"（休みにした）、"already"（今日はもう休み）、
    "too_soon"（前回の休みから7日たっていない。日付は前回の休み）。
    """
    since = today - timedelta(days=REST_DAY_INTERVAL_DAYS - 1)
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            last = conn.execute("""
                SELECT MAX(rest_on) FROM study_rest_days
                WHERE user_id = ? AND rest_on BETWEEN ? AND ?
            """, (user_id, since.isoformat(), today.isoformat())).fetchone()[0]
            if last == today.isoformat():
                return "already", today
            if last is not None:
                return "too_soon", date.fromisoformat(last)
            conn.execute(
                "INSERT INTO study_rest_days (user_id, rest_on) VALUES (?, ?)",
                (user_id, today.isoformat()),
            )
    return "ok", today


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
