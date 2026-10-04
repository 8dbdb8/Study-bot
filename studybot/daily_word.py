"""今日の1語：毎朝 #SG用語集 に投稿する用語を選び、投稿を記録する。"""

import random
import sqlite3
from contextlib import closing
from datetime import timedelta

from studybot.sg_glossary import glossary_entry_key


# この日数のうちに出した用語は選ばない
REPEAT_DAYS = 60
# 選ぶ順番（自己評価）。覚えていない用語を先に出す
PRIORITY = (("まだ要復習", "できなかった", "微妙"), (None,), ("できた",))


def init_daily_word_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_words (
            day TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            entry_key TEXT NOT NULL,
            term TEXT NOT NULL,
            PRIMARY KEY (day, guild_id)
        )
    """)


def recent_terms(db_path, today, days=REPEAT_DAYS):
    since = (today - timedelta(days=days)).isoformat()
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT term FROM daily_words WHERE day >= ?", (since,)
        ).fetchall()
    return {row[0] for row in rows}


def pick_word(entries, ratings, today, recent=()):
    """今日の1語。毎日違う用語になるよう、日付から乱数を決める。"""
    usable = [entry for entry in entries if entry.meaning and entry.term not in recent]
    if not usable:
        usable = [entry for entry in entries if entry.meaning]
    if not usable:
        return None
    rng = random.Random(today.toordinal())
    for group in PRIORITY:
        candidates = [
            entry for entry in usable
            if ratings.get(glossary_entry_key(entry)) in group
        ]
        if candidates:
            return rng.choice(candidates)
    return rng.choice(usable)


def posted_today(db_path, today, guild_id):
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT 1 FROM daily_words WHERE day = ? AND guild_id = ?",
            (today.isoformat(), guild_id),
        ).fetchone() is not None


def save_daily_word(db_path, today, guild_id, channel_id, message_id, entry):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO daily_words (
                    day, guild_id, channel_id, message_id, entry_key, term
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                today.isoformat(), guild_id, channel_id, message_id,
                glossary_entry_key(entry), entry.term,
            ))


def word_key_for_message(db_path, message_id):
    """その投稿の用語の entry_key。見つからなければ None。"""
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT entry_key FROM daily_words WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    return row[0] if row else None
