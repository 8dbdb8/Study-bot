"""Durable, session-scoped study history for the SG glossary cards."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from sg_glossary import (
    SG_GLOSSARY_RATINGS,
    GlossaryEntry,
    glossary_entry_key,
)


GLOSSARY_HISTORY_PATH = (
    Path(__file__).resolve().parent / "data" / "sg_glossary_history.db"
)


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_glossary_history_db(db_path: str | Path) -> None:
    """Create the separate glossary history database if it does not exist."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(db_path)) as conn:
        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS glossary_sessions (
                    session_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    summary_message_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS glossary_card_ratings (
                    session_id TEXT NOT NULL REFERENCES glossary_sessions(session_id)
                        ON DELETE CASCADE,
                    entry_key TEXT NOT NULL,
                    term TEXT NOT NULL,
                    category TEXT NOT NULL,
                    subcategory TEXT NOT NULL,
                    rating TEXT NOT NULL CHECK (
                        rating IN ('できた', 'できなかった', 'まだ要復習', '微妙')
                    ),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, entry_key)
                );

                CREATE TABLE IF NOT EXISTS glossary_rating_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES glossary_sessions(session_id)
                        ON DELETE CASCADE,
                    entry_key TEXT NOT NULL,
                    term TEXT NOT NULL,
                    category TEXT NOT NULL,
                    subcategory TEXT NOT NULL,
                    rating TEXT NOT NULL CHECK (
                        rating IN ('できた', 'できなかった', 'まだ要復習', '微妙')
                    ),
                    rated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_glossary_rating_events_session
                    ON glossary_rating_events (session_id, id);
            """)


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("単語帳セッションIDが正しくありません。")


def _summary(conn: sqlite3.Connection, session_id: str) -> dict:
    session = conn.execute("""
        SELECT session_id, user_id, started_at, summary_message_id
        FROM glossary_sessions WHERE session_id = ?
    """, (session_id,)).fetchone()
    if session is None:
        raise ValueError("単語帳セッションが見つかりません。")

    counts = dict.fromkeys(SG_GLOSSARY_RATINGS, 0)
    rows = conn.execute("""
        SELECT rating, COUNT(*) AS count
        FROM glossary_card_ratings
        WHERE session_id = ?
        GROUP BY rating
    """, (session_id,))
    for row in rows:
        counts[row["rating"]] = row["count"]

    return {
        "session_id": session["session_id"],
        "user_id": session["user_id"],
        "started_at": session["started_at"],
        "total": sum(counts.values()),
        "counts": counts,
        "summary_message_id": session["summary_message_id"],
    }


def record_glossary_card(
    db_path: str | Path,
    session_id: str,
    guild_id: int,
    channel_id: int,
    user_id: int,
    entry: GlossaryEntry,
    rating: str,
) -> dict:
    """Save one rating action and return the session's current card totals.

    Each action is kept in the event table. A repeat rating for the same card
    changes that card's session total instead of counting another question.
    """
    _validate_session_id(session_id)
    if rating not in SG_GLOSSARY_RATINGS:
        raise ValueError("評価は用意された4つの選択肢から選んでください。")

    init_glossary_history_db(db_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry_key = glossary_entry_key(entry)
    with closing(_connect(db_path)) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            session = conn.execute("""
                SELECT guild_id, channel_id, user_id
                FROM glossary_sessions WHERE session_id = ?
            """, (session_id,)).fetchone()
            if session is None:
                conn.execute("""
                    INSERT INTO glossary_sessions (
                        session_id, guild_id, channel_id, user_id, started_at
                    ) VALUES (?, ?, ?, ?, ?)
                """, (session_id, guild_id, channel_id, user_id, now))
            elif (session["guild_id"], session["channel_id"], session["user_id"]) != (
                guild_id, channel_id, user_id,
            ):
                raise ValueError("単語帳セッションの利用者またはチャンネルが一致しません。")

            conn.execute("""
                INSERT INTO glossary_rating_events (
                    session_id, entry_key, term, category, subcategory,
                    rating, rated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                session_id, entry_key, entry.term, entry.category,
                entry.subcategory, rating, now,
            ))
            conn.execute("""
                INSERT INTO glossary_card_ratings (
                    session_id, entry_key, term, category, subcategory,
                    rating, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, entry_key) DO UPDATE SET
                    rating = excluded.rating,
                    updated_at = excluded.updated_at
            """, (
                session_id, entry_key, entry.term, entry.category,
                entry.subcategory, rating, now,
            ))
            return _summary(conn, session_id)


def get_glossary_session(db_path: str | Path, session_id: str) -> dict:
    """Return a session summary, raising ValueError if it does not exist."""
    _validate_session_id(session_id)
    init_glossary_history_db(db_path)
    with closing(_connect(db_path)) as conn:
        return _summary(conn, session_id)


def set_glossary_summary_message_id(
    db_path: str | Path, session_id: str, message_id: int,
) -> None:
    """Remember the channel summary message so later ratings can edit it."""
    _validate_session_id(session_id)
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
        raise ValueError("メッセージIDが正しくありません。")
    init_glossary_history_db(db_path)
    with closing(_connect(db_path)) as conn:
        with conn:
            result = conn.execute("""
                UPDATE glossary_sessions
                SET summary_message_id = ?
                WHERE session_id = ?
            """, (message_id, session_id))
            if result.rowcount == 0:
                raise ValueError("単語帳セッションが見つかりません。")
