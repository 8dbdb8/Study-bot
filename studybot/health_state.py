"""健康チェックで最後に知らせた問題（同じ問題を二度知らせないため）。"""

import json
import sqlite3
from contextlib import closing


def init_health_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_health_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)


def load_notified_problems(db_path):
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                "SELECT value FROM bot_health_state WHERE key = 'problems'"
            ).fetchone()
    except sqlite3.Error:
        return []
    return json.loads(row[0]) if row else []


def save_notified_problems(db_path, problems):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO bot_health_state (key, value)
                VALUES ('problems', ?)
            """, (json.dumps(problems, ensure_ascii=False),))
