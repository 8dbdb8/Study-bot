"""Notion に作ったデータベースと週ページのIDを覚えておく。"""

import json
import sqlite3
from contextlib import closing
from datetime import timedelta


def init_notion_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notion_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notion_weekly_pages (
            user_id INTEGER NOT NULL,
            week_start TEXT NOT NULL,
            page_id TEXT NOT NULL,
            url TEXT,
            bot_block_ids TEXT,
            memo_block_id TEXT,
            PRIMARY KEY (user_id, week_start)
        )
    """)
    # 最初の版のテーブルには、Botが作ったブロックを覚える列がなかった
    columns = {
        row[1] for row in cursor.execute(
            "PRAGMA table_info(notion_weekly_pages)"
        )
    }
    for column in ("bot_block_ids", "memo_block_id"):
        if column not in columns:
            cursor.execute(
                f"ALTER TABLE notion_weekly_pages ADD COLUMN {column} TEXT"
            )


def init_notion_final_table(cursor):
    """日曜23:59の保存（その週の確定版）を済ませた週。"""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notion_final_saves (
            user_id INTEGER NOT NULL,
            week_start TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            PRIMARY KEY (user_id, week_start)
        )
    """)


def get_notion_save_candidates(db_path, week_day):
    """week_day を含む週に勉強の記録があり、確定版をまだ保存していない人。"""
    monday = (week_day - timedelta(days=week_day.weekday())).isoformat()
    sunday = week_day.isoformat()
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT user_id FROM (
                SELECT user_id FROM study_sessions
                WHERE duration_seconds > 0 AND study_date BETWEEN ? AND ?
                UNION
                SELECT user_id FROM study_logs
                WHERE study_date BETWEEN ? AND ?
            )
            WHERE user_id NOT IN (
                SELECT user_id FROM notion_final_saves WHERE week_start = ?
            )
            ORDER BY user_id
        """, (monday, sunday, monday, sunday, monday)).fetchall()
    return [row[0] for row in rows]


def mark_notion_final_save(db_path, user_id, week_day, saved_at):
    monday = week_day - timedelta(days=week_day.weekday())
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT OR IGNORE INTO notion_final_saves (
                    user_id, week_start, saved_at
                ) VALUES (?, ?, ?)
            """, (user_id, monday.isoformat(), saved_at))


def get_notion_database(db_path, parent_page_id):
    """このページの下に作ったデータベースの (database_id, data_source_id)。"""
    with closing(sqlite3.connect(db_path)) as conn:
        rows = dict(conn.execute(
            "SELECT key, value FROM notion_settings"
        ).fetchall())
    if rows.get("parent_page_id") != parent_page_id:
        return None
    if not rows.get("database_id") or not rows.get("data_source_id"):
        return None
    return rows["database_id"], rows["data_source_id"]


def save_notion_database(db_path, parent_page_id, database_id, data_source_id):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.executemany("""
                INSERT INTO notion_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, [
                ("parent_page_id", parent_page_id),
                ("database_id", database_id),
                ("data_source_id", data_source_id),
            ])


def clear_notion_database(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                DELETE FROM notion_settings
                WHERE key IN ('parent_page_id', 'database_id', 'data_source_id')
            """)


def get_weekly_page(db_path, user_id, week_start):
    """その週のページの情報。なければ None。

    bot_block_ids が None なのは、ブロックを覚える前の版で作ったページ。
    """
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("""
            SELECT page_id, url, bot_block_ids, memo_block_id
            FROM notion_weekly_pages
            WHERE user_id = ? AND week_start = ?
        """, (user_id, week_start.isoformat())).fetchone()
    if row is None:
        return None
    return {
        "page_id": row[0],
        "url": row[1],
        "bot_block_ids": json.loads(row[2]) if row[2] else None,
        "memo_block_id": row[3],
    }


def save_weekly_page(db_path, user_id, week_start, page_id, url,
                     bot_block_ids, memo_block_id):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT INTO notion_weekly_pages (
                    user_id, week_start, page_id, url,
                    bot_block_ids, memo_block_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, week_start) DO UPDATE SET
                    page_id = excluded.page_id,
                    url = excluded.url,
                    bot_block_ids = excluded.bot_block_ids,
                    memo_block_id = excluded.memo_block_id
            """, (
                user_id, week_start.isoformat(), page_id, url,
                json.dumps(bot_block_ids), memo_block_id,
            ))
