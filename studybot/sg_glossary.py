"""Load and search the local SG glossary used by the Discord command."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


GLOSSARY_PATH = Path(__file__).resolve().parents[1] / "data" / "sg_glossary.json"
SOURCE_GLOSSARY_URL = "https://www.sg-siken.com/keyword/"
SG_GLOSSARY_RATINGS = (
    "できた",
    "できなかった",
    "まだ要復習",
    "微妙",
)


class GlossaryDataError(ValueError):
    """The glossary file exists but cannot be used."""


@dataclass(frozen=True)
class GlossaryEntry:
    term: str
    meaning: str = ""
    category: str = ""
    source_url: str = ""
    subcategory: str = ""


def glossary_entry_key(entry: GlossaryEntry) -> str:
    """Identify a glossary card without depending on its editable meaning."""
    return json.dumps(
        (entry.category, entry.subcategory, entry.term),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def init_sg_glossary_rating_table(cursor: sqlite3.Cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sg_glossary_ratings (
            user_id INTEGER NOT NULL,
            entry_key TEXT NOT NULL,
            rating TEXT NOT NULL CHECK (
                rating IN ('できた', 'できなかった', 'まだ要復習', '微妙')
            ),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, entry_key)
        )
    """)


def get_sg_glossary_ratings(
    db_path: str | Path,
    user_id: int,
    entries: list[GlossaryEntry] | tuple[GlossaryEntry, ...],
) -> dict[str, str]:
    """Return this user's latest ratings for the supplied cards."""
    keys = tuple(dict.fromkeys(glossary_entry_key(entry) for entry in entries))
    if not keys:
        return {}

    ratings = {}
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            init_sg_glossary_rating_table(conn.cursor())
            # Stay below SQLite's variable limit even for custom glossaries.
            for start in range(0, len(keys), 500):
                batch = keys[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT entry_key, rating FROM sg_glossary_ratings "
                    f"WHERE user_id = ? AND entry_key IN ({placeholders})",
                    (user_id, *batch),
                )
                ratings.update(rows)
    return ratings


def save_sg_glossary_rating(
    db_path: str | Path,
    user_id: int,
    entry: GlossaryEntry,
    rating: str,
) -> None:
    """Store or replace this user's self-rating for one glossary card."""
    if rating not in SG_GLOSSARY_RATINGS:
        raise ValueError("評価は用意された4つの選択肢から選んでください。")

    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            init_sg_glossary_rating_table(conn.cursor())
            conn.execute("""
                INSERT INTO sg_glossary_ratings (
                    user_id, entry_key, rating, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (user_id, entry_key) DO UPDATE SET
                    rating = excluded.rating,
                    updated_at = excluded.updated_at
            """, (
                user_id,
                glossary_entry_key(entry),
                rating,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ))


def load_glossary(path: str | Path = GLOSSARY_PATH) -> list[GlossaryEntry]:
    """Read a JSON list or an object containing an ``entries`` list.

    Empty meanings are intentional: they let a terms-only source be browsed
    without presenting a definition that has not been verified.
    """
    try:
        with Path(path).open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as error:
        raise GlossaryDataError("JSONの形式が正しくありません。") from error
    except UnicodeError as error:
        raise GlossaryDataError("UTF-8のJSONファイルにしてください。") from error

    if isinstance(payload, dict):
        payload = payload.get("entries")
    if not isinstance(payload, list):
        raise GlossaryDataError("用語データは配列、または entries 配列を含むJSONにしてください。")

    entries = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise GlossaryDataError(f"{index}件目の用語データはオブジェクトにしてください。")
        values = {}
        for field in ("term", "meaning", "category", "source_url", "subcategory"):
            value = item.get(field, "")
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise GlossaryDataError(f"{index}件目の {field} は文字列にしてください。")
            values[field] = value.strip()
        if not values["term"]:
            raise GlossaryDataError(f"{index}件目の term が空です。")
        entries.append(GlossaryEntry(**values))
    return entries


def _search_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def search_glossary(
    entries: list[GlossaryEntry] | tuple[GlossaryEntry, ...],
    query: str | None,
) -> list[GlossaryEntry]:
    """Search terms, meanings and categories, preserving the source order."""
    words = [_search_key(word) for word in (query or "").split()]
    if not words:
        return list(entries)
    return [
        entry for entry in entries
        if all(
            word in _search_key(" ".join((
                entry.term, entry.meaning, entry.category, entry.subcategory,
            )))
            for word in words
        )
    ]


def split_text(text: str, limit: int) -> list[str]:
    """Split without dropping characters, including spaces and newlines."""
    if limit < 1:
        raise ValueError("limit must be positive")
    return [text[pos:pos + limit] for pos in range(0, len(text), limit)] or [""]
