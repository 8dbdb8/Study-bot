"""過去問道場のほかの勉強の記録：復習（過去問道場・単語）と単語帳。

分野は複数選べる。正答率は持たないので、分野別の正答率や進捗には混ぜない。
勉強ログの1件として保存するので、連続学習日数には数える。
"""

import json
import sqlite3
import unicodedata
from contextlib import closing

from studybot.config import SG_GLOSSARY_CATEGORIES


# 種類 -> (名前, 数の入力欄の名前, 数の単位)
ACTIVITY_KINDS = {
    "review_past": ("復習（過去問道場）", "解き直した問題数", "問"),
    "review_terms": ("復習（単語）", "復習した単語数", "語"),
    "terms": ("単語帳", "覚えた単語数", "語"),
}
MAX_AMOUNT = 9999


def init_activity_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS study_activities (
            message_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            qualification TEXT NOT NULL,
            kind TEXT NOT NULL,
            categories TEXT NOT NULL,
            amount INTEGER,
            minutes INTEGER,
            memo TEXT,
            studied_on TEXT NOT NULL
        )
    """)


def activity_categories(kind, qualification):
    """選べる分野。過去問道場の復習は資格の分野、単語はSGなら用語集の分野。"""
    if kind == "review_past" or qualification.code != "SG":
        return qualification.category_names
    return SG_GLOSSARY_CATEGORIES


def parse_amount_input(value):
    text = (value or "").strip()
    if not text:
        return None
    text = unicodedata.normalize("NFKC", text).rstrip("問語個")
    if not text.isdigit() or not 1 <= int(text) <= MAX_AMOUNT:
        raise ValueError(f"数は1〜{MAX_AMOUNT}の数字で入力してください。")
    return int(text)


def _amount_text(kind, amount):
    return f"{amount}{ACTIVITY_KINDS[kind][2]}" if amount else None


def build_activity_text(qualification, kind, categories, amount=None,
                        minutes=None, memo=None):
    """勉強ログに残す1文。例：SG 復習（単語）：法務・ネットワーク。30語。20分。"""
    parts = [
        f"{qualification.code} {ACTIVITY_KINDS[kind][0]}：{'・'.join(categories)}。"
    ]
    if amount:
        parts.append(f"{_amount_text(kind, amount)}。")
    if minutes:
        parts.append(f"{minutes}分。")
    if memo:
        parts.append(f"メモ：{memo}")
    return "".join(parts)


def save_activity(db_path, message_id, user_id, qualification, kind, categories,
                  amount, minutes, memo, studied_on):
    if kind not in ACTIVITY_KINDS:
        raise ValueError("勉強の種類を選んでください。")
    allowed = set(activity_categories(kind, qualification))
    if not categories or not set(categories) <= allowed:
        raise ValueError("分野を候補から1つ以上選んでください。")
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO study_activities (
                    message_id, user_id, qualification, kind, categories,
                    amount, minutes, memo, studied_on
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                message_id, user_id, qualification.code, kind,
                json.dumps(list(categories), ensure_ascii=False),
                amount, minutes, (memo or "").strip() or None,
                studied_on.isoformat(),
            ))


def get_activities(db_path, user_id, start, end, qualification=None):
    query = """
        SELECT kind, categories, amount, minutes, studied_on
        FROM study_activities
        WHERE user_id = ? AND studied_on BETWEEN ? AND ?
    """
    params = [user_id, start.isoformat(), end.isoformat()]
    if qualification is not None:
        query += " AND qualification = ?"
        params.append(qualification)
    query += " ORDER BY studied_on, message_id"
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            rows = conn.execute(query, params).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "kind": kind, "categories": json.loads(categories),
            "amount": amount, "minutes": minutes, "studied_on": studied_on,
        }
        for kind, categories, amount, minutes, studied_on in rows
    ]


def summarize_activities(activities):
    """例：復習（過去問道場）2回・12問 ／ 単語帳 3回・60語。なければ None。"""
    if not activities:
        return None
    parts = []
    for kind, (name, _, unit) in ACTIVITY_KINDS.items():
        rows = [row for row in activities if row["kind"] == kind]
        if not rows:
            continue
        text = f"{name} {len(rows)}回"
        amount = sum(row["amount"] or 0 for row in rows)
        if amount:
            text += f"・{amount}{unit}"
        parts.append(text)
    return " ／ ".join(parts)
