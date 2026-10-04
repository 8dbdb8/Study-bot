import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse

from studybot.qualifications import SG, get_qualification


# 名前は SG 時代のままだが、qualification 列で資格を区別する
SG_B_TOPICS = SG.b_topics

# 資格ごとに分けるために後から足した列（既存の記録は SG）
QUALIFICATION_COLUMN_TABLES = ("sg_mistakes", "sg_b_practice", "sg_plans")


def _qualification(code):
    qualification = get_qualification(code)
    if qualification is None:
        raise ValueError(f"資格 {code} は登録されていません。")
    return qualification


@contextmanager
def _connect(db_path):
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def parse_correct_count(value, questions):
    text = unicodedata.normalize(
        "NFKC", "" if value is None else str(value)
    ).strip()
    if not text.isdecimal():
        raise ValueError("正解数は0以上の整数で入力してください。")
    count = int(text)
    if count > questions:
        raise ValueError("正解数は問題数以下で入力してください。")
    return count


def score_from_counts(correct_answers, questions):
    if questions <= 0 or not 0 <= correct_answers <= questions:
        raise ValueError("問題数と正解数を確認してください。")
    return float((
        Decimal(correct_answers) * 100 / Decimal(questions)
    ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def init_sg_feature_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sg_mistakes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            question_ref TEXT NOT NULL,
            reason TEXT NOT NULL,
            memo TEXT,
            created_on TEXT NOT NULL,
            next_review_on TEXT,
            success_streak INTEGER NOT NULL DEFAULT 0,
            completed_on TEXT
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_sg_mistakes_due
        ON sg_mistakes(user_id, completed_on, next_review_on)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sg_mistake_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mistake_id INTEGER NOT NULL,
            attempted_on TEXT NOT NULL,
            result TEXT NOT NULL CHECK(result IN ('correct', 'wrong')),
            FOREIGN KEY (mistake_id) REFERENCES sg_mistakes(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sg_b_practice (
            message_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            questions INTEGER NOT NULL,
            correct_answers INTEGER NOT NULL,
            wrong_reason TEXT,
            memo TEXT,
            practiced_on TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sg_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            start_on TEXT NOT NULL,
            weeks INTEGER NOT NULL,
            weekly_questions INTEGER NOT NULL,
            plan_text TEXT,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_sg_plans_active
        ON sg_plans(user_id, active, id)
    """)
    mistake_columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(sg_mistakes)")
    }
    if "image_path" not in mistake_columns:
        # 誤答に添付した問題の画像（PCに保存したファイルの場所）
        cursor.execute("ALTER TABLE sg_mistakes ADD COLUMN image_path TEXT")
    for table in QUALIFICATION_COLUMN_TABLES:
        columns = {
            row[1] for row in cursor.execute(f"PRAGMA table_info({table})")
        }
        if "qualification" not in columns:
            cursor.execute(
                f"ALTER TABLE {table} "
                "ADD COLUMN qualification TEXT NOT NULL DEFAULT 'SG'"
            )


def validate_question_ref(value):
    reference = (value or "").strip()
    if not reference or len(reference) > 200:
        raise ValueError("問題のURLまたは番号を200文字以内で入力してください。")

    if reference.startswith(("http://", "https://")):
        parsed = urlparse(reference)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("URLはhttps://から始まる形式で入力してください。")

    return reference


def add_sg_mistake(
    db_path, user_id, category, question_ref, reason, memo="", today=None,
    qualification="SG",
):
    qualification = _qualification(qualification)
    if category not in qualification.category_names:
        raise ValueError(
            f"{qualification.code}の{len(qualification.categories)}分野から"
            "選択してください。"
        )

    reference = validate_question_ref(question_ref)
    reason = (reason or "").strip()
    memo = (memo or "").strip()
    if not reason or len(reason) > 300:
        raise ValueError("間違えた理由を300文字以内で入力してください。")
    if len(memo) > 500:
        raise ValueError("メモは500文字以内で入力してください。")

    today = today or date.today()
    due = today + timedelta(days=1)
    with _connect(db_path) as conn:
        cursor = conn.execute("""
            INSERT INTO sg_mistakes (
                user_id, category, question_ref, reason, memo,
                created_on, next_review_on, qualification
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, category, reference, reason, memo or None,
            today.isoformat(), due.isoformat(), qualification.code,
        ))
        return cursor.lastrowid, due


def get_sg_mistakes(db_path, user_id, today=None, due_only=True,
                    qualification=None):
    """未完了の誤答。qualification を省略するとすべての資格。"""
    today = today or date.today()
    query = """
        SELECT id, category, question_ref, reason, memo,
               next_review_on, success_streak, qualification, image_path
        FROM sg_mistakes
        WHERE user_id = ? AND completed_on IS NULL
    """
    params = [user_id]
    if qualification is not None:
        query += " AND qualification = ?"
        params.append(qualification)
    if due_only:
        query += " AND next_review_on <= ?"
        params.append(today.isoformat())
    query += " ORDER BY next_review_on, id"

    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(query, params)]


def record_sg_mistake_attempt(
    db_path, user_id, mistake_id, result, today=None
):
    if result not in ("correct", "wrong"):
        raise ValueError("結果はcorrectまたはwrongで指定してください。")

    today = today or date.today()
    with _connect(db_path) as conn:
        row = conn.execute("""
            SELECT success_streak FROM sg_mistakes
            WHERE id = ? AND user_id = ? AND completed_on IS NULL
        """, (mistake_id, user_id)).fetchone()
        if row is None:
            raise ValueError("その復習IDは見つからないか、すでに完了しています。")

        streak = row[0] + 1 if result == "correct" else 0
        completed = streak >= 3
        wait_days = (1, 3, 7)[min(streak, 2)]
        due = None if completed else today + timedelta(days=wait_days)

        conn.execute("""
            INSERT INTO sg_mistake_attempts (mistake_id, attempted_on, result)
            VALUES (?, ?, ?)
        """, (mistake_id, today.isoformat(), result))
        conn.execute("""
            UPDATE sg_mistakes
            SET success_streak = ?, next_review_on = ?, completed_on = ?
            WHERE id = ?
        """, (
            streak, due.isoformat() if due else None,
            today.isoformat() if completed else None, mistake_id,
        ))

    return {"streak": streak, "completed": completed, "next_review_on": due}


def get_sg_category_progress(db_path, user_id, qualification="SG"):
    qualification = _qualification(qualification)
    categories = qualification.category_names
    placeholders = ",".join("?" for _ in categories)
    with _connect(db_path) as conn:
        rows = conn.execute(f"""
            SELECT c.category, c.questions, c.score_percent, l.study_date
            FROM study_log_category_results AS c
            JOIN study_log_analysis AS a ON a.message_id = c.message_id
            JOIN study_logs AS l ON l.message_id = a.message_id
            WHERE a.user_id = ? AND a.qualification = ?
              AND COALESCE(a.exam_section, 'A') = 'A'
              AND c.category IN ({placeholders})
            ORDER BY l.study_date, l.created_at, l.message_id
        """, (user_id, qualification.code, *categories)).fetchall()

        unclassified = conn.execute(f"""
            SELECT COALESCE(SUM(a.questions), 0)
            FROM study_log_analysis AS a
            JOIN study_logs AS l ON l.message_id = a.message_id
            WHERE a.user_id = ? AND a.qualification = ?
              AND COALESCE(a.exam_section, 'A') = 'A'
              AND NOT EXISTS (
                  SELECT 1 FROM study_log_category_results AS c
                  WHERE c.message_id = a.message_id
                    AND c.category IN ({placeholders})
              )
        """, (user_id, qualification.code, *categories)).fetchone()[0]

    progress = {
        category: {
            "category": category,
            "questions": 0,
            "latest_score": None,
            "last_study_date": None,
        }
        for category in categories
    }
    for category, questions, score, study_date in rows:
        item = progress[category]
        item["questions"] += questions or 0
        item["latest_score"] = score
        item["last_study_date"] = study_date

    return list(progress.values()), unclassified


def save_sg_b_practice(
    db_path, message_id, user_id, topic, questions, correct_answers,
    wrong_reason, memo, practiced_on, qualification="SG",
):
    qualification = _qualification(qualification)
    if topic not in qualification.b_topics:
        raise ValueError("科目Bのテーマを選択してください。")
    if not 1 <= questions <= 1000 or not 0 <= correct_answers <= questions:
        raise ValueError("問題数と正解数を確認してください。")
    if correct_answers < questions and not (wrong_reason or "").strip():
        raise ValueError("誤答がある場合は判断を間違えた理由が必要です。")

    with _connect(db_path) as conn:
        conn.execute("""
            INSERT INTO sg_b_practice (
                message_id, user_id, topic, questions, correct_answers,
                wrong_reason, memo, practiced_on, qualification
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                topic = excluded.topic,
                questions = excluded.questions,
                correct_answers = excluded.correct_answers,
                wrong_reason = excluded.wrong_reason,
                memo = excluded.memo
        """, (
            message_id, user_id, topic, questions, correct_answers,
            (wrong_reason or "").strip() or None,
            (memo or "").strip() or None, practiced_on, qualification.code,
        ))


def get_sg_b_summary(db_path, user_id, qualification="SG"):
    with _connect(db_path) as conn:
        count, questions, correct = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(b.questions), 0),
                   COALESCE(SUM(b.correct_answers), 0)
            FROM sg_b_practice AS b
            JOIN study_logs AS l ON l.message_id = b.message_id
            WHERE b.user_id = ? AND b.qualification = ?
        """, (user_id, qualification)).fetchone()
        recent = conn.execute("""
            SELECT b.topic, b.questions, b.correct_answers,
                   b.wrong_reason, b.practiced_on
            FROM sg_b_practice AS b
            JOIN study_logs AS l ON l.message_id = b.message_id
            WHERE b.user_id = ? AND b.qualification = ?
            ORDER BY b.practiced_on DESC, b.message_id DESC
            LIMIT 3
        """, (user_id, qualification)).fetchall()

    return {
        "log_count": count,
        "questions": questions,
        "correct_answers": correct,
        "score_percent": 100 * correct / questions if questions else None,
        "recent_sessions": recent,
    }


def save_sg_plan(
    db_path, user_id, weeks, weekly_questions, created_at, today=None,
    qualification="SG",
):
    if not 1 <= weeks <= 16 or not 1 <= weekly_questions <= 500:
        raise ValueError("週数は1〜16、週目標は1〜500問で指定してください。")
    today = today or date.today()
    with _connect(db_path) as conn:
        conn.execute("""
            UPDATE sg_plans SET active = 0
            WHERE user_id = ? AND active = 1 AND qualification = ?
        """, (user_id, qualification))
        cursor = conn.execute("""
            INSERT INTO sg_plans (
                user_id, start_on, weeks, weekly_questions, created_at,
                qualification
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            user_id, today.isoformat(), weeks, weekly_questions, created_at,
            qualification,
        ))
        return cursor.lastrowid


def update_sg_plan_text(db_path, plan_id, text):
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE sg_plans SET plan_text = ? WHERE id = ?",
            (text, plan_id),
        )


def get_sg_plan_status(db_path, user_id, today=None, qualification="SG"):
    today = today or date.today()
    with _connect(db_path) as conn:
        plan = conn.execute("""
            SELECT id, start_on, weeks, weekly_questions
            FROM sg_plans
            WHERE user_id = ? AND active = 1 AND qualification = ?
            ORDER BY id DESC LIMIT 1
        """, (user_id, qualification)).fetchone()
        if plan is None:
            return None

        plan_id, start_on, weeks, base = plan
        start = date.fromisoformat(start_on)
        end = start + timedelta(days=7 * weeks)
        solved_rows = conn.execute("""
            SELECT l.study_date, COALESCE(SUM(a.questions), 0)
            FROM study_log_analysis AS a
            JOIN study_logs AS l ON l.message_id = a.message_id
            WHERE a.user_id = ? AND a.qualification = ?
              AND l.study_date >= ? AND l.study_date < ?
            GROUP BY l.study_date
        """, (
            user_id, qualification, start.isoformat(), end.isoformat(),
        )).fetchall()

    solved_by_day = {date.fromisoformat(day): count for day, count in solved_rows}
    current_index = (today - start).days // 7
    rows = []
    previous_shortfall = 0
    for index in range(min(weeks, max(current_index + 1, 0))):
        week_start = start + timedelta(days=7 * index)
        week_end = week_start + timedelta(days=7)
        target = base + min(previous_shortfall, base // 2)
        actual = sum(
            count for day, count in solved_by_day.items()
            if week_start <= day < week_end
        )
        rows.append({
            "week": index + 1,
            "start_on": week_start,
            "end_on": week_end - timedelta(days=1),
            "target": target,
            "actual": actual,
            "remaining": max(target - actual, 0),
        })
        previous_shortfall = max(target - actual, 0)

    return {
        "id": plan_id,
        "start_on": start,
        "weeks": weeks,
        "weekly_questions": base,
        "current_week": current_index + 1,
        "rows": rows,
        "completed": current_index >= weeks,
        "qualification": qualification,
    }


def get_weak_categories(db_path, user_id, qualification="SG", threshold=60.0,
                        min_questions=10):
    """直近の正答率が低い分野を、弱い順に [(分野, 正答率), ...] で返す。

    min_questions 問以上解いた分野だけを見る。threshold が None なら
    正答率に関係なくすべて（一番弱い分野を探すとき用）。
    """
    items, _ = get_sg_category_progress(db_path, user_id, qualification)
    weak = [
        (item["category"], item["latest_score"])
        for item in items
        if item["questions"] >= min_questions
        and item["latest_score"] is not None
        and (threshold is None or item["latest_score"] < threshold)
    ]
    return sorted(weak, key=lambda pair: pair[1])


def set_mistake_image(db_path, user_id, mistake_id, image_path):
    with _connect(db_path) as conn:
        conn.execute("""
            UPDATE sg_mistakes SET image_path = ?
            WHERE id = ? AND user_id = ?
        """, (image_path, mistake_id, user_id))
