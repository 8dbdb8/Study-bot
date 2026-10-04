"""試験の結果（合格・不合格）の記録と、資格ロードマップを次へ進める処理。"""

import sqlite3
from contextlib import closing
from datetime import timedelta


# 試験の何日前から学習メニューを「直前モード」にするか
FINAL_STRETCH_DAYS = 14

# 試験日から何日間、結果をたずね続けるか
RESULT_PROMPT_DAYS = 7


def init_exam_result_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS exam_results (
            user_id INTEGER NOT NULL,
            qualification TEXT NOT NULL,
            exam_on TEXT NOT NULL,
            result TEXT NOT NULL CHECK (result IN ('pass', 'fail')),
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (user_id, qualification, exam_on)
        )
    """)
    columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(exam_results)")
    }
    if "score" not in columns:
        cursor.execute("ALTER TABLE exam_results ADD COLUMN score INTEGER")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS exam_result_prompts (
            user_id INTEGER NOT NULL,
            qualification TEXT NOT NULL,
            exam_on TEXT NOT NULL,
            asked_on TEXT NOT NULL,
            PRIMARY KEY (user_id, qualification, exam_on, asked_on)
        )
    """)


def get_pending_exam(db_path, user_id, today):
    """試験日を過ぎたのに結果がまだの試験 (qualification, exam_on)。なければ None。"""
    since = (today - timedelta(days=RESULT_PROMPT_DAYS)).isoformat()
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("""
            SELECT d.qualification, d.exam_on
            FROM exam_dates AS d
            WHERE d.user_id = ? AND d.exam_on BETWEEN ? AND ?
              AND NOT EXISTS (
                  SELECT 1 FROM exam_results AS r
                  WHERE r.user_id = d.user_id
                    AND r.qualification = d.qualification
                    AND r.exam_on = d.exam_on
              )
            ORDER BY d.exam_on DESC
            LIMIT 1
        """, (user_id, since, today.isoformat())).fetchone()
    return row


def get_result_prompt_candidates(db_path, today):
    """今日まだ結果をたずねていない (user_id, qualification, exam_on) の一覧。"""
    since = (today - timedelta(days=RESULT_PROMPT_DAYS)).isoformat()
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("""
            SELECT d.user_id, d.qualification, d.exam_on
            FROM exam_dates AS d
            WHERE d.exam_on BETWEEN ? AND ?
              AND NOT EXISTS (
                  SELECT 1 FROM exam_results AS r
                  WHERE r.user_id = d.user_id
                    AND r.qualification = d.qualification
                    AND r.exam_on = d.exam_on
              )
              AND NOT EXISTS (
                  SELECT 1 FROM exam_result_prompts AS p
                  WHERE p.user_id = d.user_id
                    AND p.qualification = d.qualification
                    AND p.exam_on = d.exam_on
                    AND p.asked_on = ?
              )
            ORDER BY d.user_id
        """, (since, today.isoformat(), today.isoformat())).fetchall()


def mark_result_prompted(db_path, user_id, qualification, exam_on, today):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT OR IGNORE INTO exam_result_prompts (
                    user_id, qualification, exam_on, asked_on
                ) VALUES (?, ?, ?, ?)
            """, (user_id, qualification, exam_on, today.isoformat()))


# 試験の得点は1000点満点（SG・FE）
MAX_EXAM_SCORE = 1000


def parse_exam_score(value):
    """入力された得点。空欄なら None。"""
    text = str(value or "").strip().translate(
        str.maketrans("０１２３４５６７８９", "0123456789")
    )
    if not text:
        return None
    if not text.isdecimal() or not 0 <= int(text) <= MAX_EXAM_SCORE:
        raise ValueError(f"得点は0〜{MAX_EXAM_SCORE}の整数で入力してください。")
    return int(text)


def record_exam_result(db_path, user_id, qualification, exam_on, result,
                       recorded_at, score=None):
    if result not in ("pass", "fail"):
        raise ValueError("結果は pass か fail で指定してください。")
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT INTO exam_results (
                    user_id, qualification, exam_on, result, recorded_at, score
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, qualification, exam_on) DO UPDATE SET
                    result = excluded.result,
                    recorded_at = excluded.recorded_at,
                    score = excluded.score
            """, (user_id, qualification, exam_on, result, recorded_at, score))


def advance_roadmap(db_path, qualification):
    """合格した資格を「合格済み」にし、次の未合格の資格を学習中にする。

    次の資格の (qualification, display_name) を返す。最後の資格なら None。
    """
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            row = conn.execute(
                "SELECT sort_order FROM certification_roadmap WHERE qualification = ?",
                (qualification,),
            ).fetchone()
            if row is None:
                return None
            conn.execute("""
                UPDATE certification_roadmap
                SET status = 'completed', is_current = 0
                WHERE qualification = ?
            """, (qualification,))
            following = conn.execute("""
                SELECT qualification, display_name
                FROM certification_roadmap
                WHERE sort_order > ? AND status != 'completed'
                ORDER BY sort_order
                LIMIT 1
            """, (row[0],)).fetchone()
            if following is None:
                return None
            conn.execute("UPDATE certification_roadmap SET is_current = 0")
            conn.execute("""
                UPDATE certification_roadmap
                SET status = 'learning', is_current = 1
                WHERE qualification = ?
            """, (following[0],))
    return following


def get_study_summary(db_path, user_id, until):
    """合格までの記録：(最初に勉強した日, 勉強した日数, VC勉強時間の合計秒)。"""
    with closing(sqlite3.connect(db_path)) as conn:
        first_day, study_days = conn.execute("""
            SELECT MIN(study_date), COUNT(DISTINCT study_date) FROM (
                SELECT study_date FROM study_sessions
                WHERE user_id = ? AND duration_seconds > 0 AND study_date <= ?
                UNION ALL
                SELECT study_date FROM study_logs
                WHERE user_id = ? AND study_date <= ?
            )
        """, (user_id, until, user_id, until)).fetchone()
        seconds = conn.execute("""
            SELECT COALESCE(SUM(duration_seconds), 0) FROM study_sessions
            WHERE user_id = ? AND study_date <= ?
        """, (user_id, until)).fetchone()[0]
    return first_day, study_days, seconds
