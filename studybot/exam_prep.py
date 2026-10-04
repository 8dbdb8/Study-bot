"""試験前日の持ち物チェックの記録。"""

import sqlite3
from contextlib import closing
from datetime import timedelta


# (チェックの文, ボタンの文)
PREP_ITEMS = (
    ("受験票（確認票）を用意した", "受験票"),
    ("本人確認書類（顔写真つき）を用意した", "本人確認書類"),
    ("会場までの行き方と集合時刻を確認した", "会場と時刻"),
    ("目覚ましをセットした", "目覚まし"),
)
MEMO_MAX_LENGTH = 200


def init_exam_prep_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS exam_prep (
            user_id INTEGER NOT NULL,
            qualification TEXT NOT NULL,
            exam_on TEXT NOT NULL,
            checked TEXT NOT NULL DEFAULT '',
            memo TEXT,
            prompted_on TEXT,
            PRIMARY KEY (user_id, qualification, exam_on)
        )
    """)


def get_prep_candidates(db_path, today):
    """明日が試験日で、まだ持ち物チェックを送っていない (user_id, 資格, 試験日)。"""
    tomorrow = (today + timedelta(days=1)).isoformat()
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("""
            SELECT d.user_id, d.qualification, d.exam_on
            FROM exam_dates AS d
            WHERE d.exam_on = ?
              AND NOT EXISTS (
                  SELECT 1 FROM exam_prep AS p
                  WHERE p.user_id = d.user_id
                    AND p.qualification = d.qualification
                    AND p.exam_on = d.exam_on
                    AND p.prompted_on IS NOT NULL
              )
            ORDER BY d.user_id
        """, (tomorrow,)).fetchall()


def _ensure_row(conn, user_id, qualification, exam_on):
    conn.execute("""
        INSERT OR IGNORE INTO exam_prep (user_id, qualification, exam_on)
        VALUES (?, ?, ?)
    """, (user_id, qualification, exam_on))


def mark_prep_prompted(db_path, user_id, qualification, exam_on, today):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            _ensure_row(conn, user_id, qualification, exam_on)
            conn.execute("""
                UPDATE exam_prep SET prompted_on = ?
                WHERE user_id = ? AND qualification = ? AND exam_on = ?
            """, (today.isoformat(), user_id, qualification, exam_on))


def get_exam_prep(db_path, user_id, qualification, exam_on):
    """{"checked": チェック済みの番号の集合, "memo": 会場メモ}。"""
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("""
            SELECT checked, memo FROM exam_prep
            WHERE user_id = ? AND qualification = ? AND exam_on = ?
        """, (user_id, qualification, exam_on)).fetchone()
    if row is None:
        return {"checked": set(), "memo": None}
    checked = {int(value) for value in row[0].split(",") if value}
    return {"checked": checked, "memo": row[1]}


def find_upcoming_prep(db_path, user_id, today):
    """今日以降の、持ち物チェックを送った試験 (資格, 試験日)。なければ None。"""
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("""
            SELECT qualification, exam_on FROM exam_prep
            WHERE user_id = ? AND exam_on >= ?
            ORDER BY exam_on
            LIMIT 1
        """, (user_id, today.isoformat())).fetchone()


def toggle_prep_item(db_path, user_id, qualification, exam_on, index):
    """index 番目のチェックを付け外しして、新しい状態を返す。"""
    if not 0 <= index < len(PREP_ITEMS):
        raise ValueError("持ち物の番号が正しくありません。")
    checked = get_exam_prep(db_path, user_id, qualification, exam_on)["checked"]
    checked ^= {index}
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            _ensure_row(conn, user_id, qualification, exam_on)
            conn.execute("""
                UPDATE exam_prep SET checked = ?
                WHERE user_id = ? AND qualification = ? AND exam_on = ?
            """, (
                ",".join(str(value) for value in sorted(checked)),
                user_id, qualification, exam_on,
            ))
    return get_exam_prep(db_path, user_id, qualification, exam_on)


def set_prep_memo(db_path, user_id, qualification, exam_on, memo):
    memo = (memo or "").strip()
    if len(memo) > MEMO_MAX_LENGTH:
        raise ValueError(f"メモは{MEMO_MAX_LENGTH}文字以内で入力してください。")
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            _ensure_row(conn, user_id, qualification, exam_on)
            conn.execute("""
                UPDATE exam_prep SET memo = ?
                WHERE user_id = ? AND qualification = ? AND exam_on = ?
            """, (memo or None, user_id, qualification, exam_on))
    return get_exam_prep(db_path, user_id, qualification, exam_on)
