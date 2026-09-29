"""Owner-scoped inspection, correction, and reset of study records."""

import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from sg_features import (
    SG_B_TOPICS, parse_correct_count, score_from_counts, validate_question_ref,
)
from study_log_parser import (
    SG_CATEGORY_TO_MAJOR,
    SG_PRACTICE_CATEGORIES,
    infer_correct_answers,
    parse_question_count_input,
    parse_score_percent_input,
)


KINDS = {
    "log": "SG学習ログ",
    "session": "通話勉強時間",
    "mistake": "誤答の復習リスト",
    "plan": "週次計画",
}
RESET_SCOPES = {
    "sg_logs": "SG学習ログ（科目Bを含む）",
    "sessions": "完了済みの通話勉強時間",
    "mistakes": "誤答の復習リストと挑戦履歴",
    "plans": "週次計画の履歴",
    "all": "自分の全学習データ（SG以外も含む）",
}
FIELDS = {
    "log": {
        "category": "分野・テーマ",
        "questions": "問題数",
        "score_percent": "正答率",
        "correct_answers": "正解数",
        "notes": "メモ",
        "wrong_reason": "判断を間違えた理由",
    },
    "session": {"minutes": "勉強時間（分）"},
    "mistake": {
        "category": "分野",
        "question_ref": "問題のURL・番号",
        "reason": "間違えた理由",
        "memo": "メモ",
    },
    "plan": {
        "weeks": "計画の週数",
        "weekly_questions": "週の目標問題数",
        "start_on": "計画開始日",
    },
}


@contextmanager
def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _one(conn, query, params):
    row = conn.execute(query, params).fetchone()
    return dict(row) if row is not None else None


def get_overview(db_path, user_id):
    with _connect(db_path) as conn:
        logs = _one(conn, """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN a.qualification = 'SG' THEN 1 ELSE 0 END)
                       AS sg,
                   SUM(CASE WHEN a.qualification = 'SG'
                             AND a.exam_section = 'B' THEN 1 ELSE 0 END)
                       AS b
            FROM study_logs AS l
            LEFT JOIN study_log_analysis AS a
                ON a.message_id = l.message_id AND a.user_id = l.user_id
            WHERE l.user_id = ?
        """, (user_id,))
        sessions = _one(conn, """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(duration_seconds), 0) AS seconds
            FROM study_sessions WHERE user_id = ?
        """, (user_id,))
        mistakes = _one(conn, """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN completed_on IS NULL THEN 1 ELSE 0 END)
                       AS open_count
            FROM sg_mistakes WHERE user_id = ?
        """, (user_id,))
        plans = _one(conn, """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END)
                       AS active_count
            FROM sg_plans WHERE user_id = ?
        """, (user_id,))
        active = _one(conn, """
            SELECT COUNT(*) AS total FROM active_study_sessions
            WHERE user_id = ?
        """, (user_id,))["total"]
    return {
        "logs": logs, "sessions": sessions,
        "mistakes": mistakes, "plans": plans, "active": active,
    }


def list_records(db_path, user_id, kind, page=0, page_size=20):
    if kind not in KINDS or page < 0 or not 1 <= page_size <= 25:
        raise ValueError("一覧の指定が正しくありません。")
    queries = {
        "log": """
            SELECT l.message_id AS id, l.study_date AS day,
                   a.exam_section AS section, a.questions,
                   a.score_percent, a.correct_answers
            FROM study_logs AS l JOIN study_log_analysis AS a
                ON a.message_id = l.message_id
            WHERE l.user_id = ? AND a.user_id = ?
                AND a.qualification = 'SG'
            ORDER BY l.study_date DESC, l.message_id DESC
        """,
        "session": """
            SELECT id, study_date AS day, duration_seconds
            FROM study_sessions WHERE user_id = ?
            ORDER BY study_date DESC, id DESC
        """,
        "mistake": """
            SELECT id, created_on AS day, category, question_ref,
                   completed_on FROM sg_mistakes WHERE user_id = ?
            ORDER BY id DESC
        """,
        "plan": """
            SELECT id, start_on AS day, weeks, weekly_questions, active
            FROM sg_plans WHERE user_id = ? ORDER BY id DESC
        """,
    }
    params = (user_id, user_id) if kind == "log" else (user_id,)
    with _connect(db_path) as conn:
        rows = [dict(row) for row in conn.execute(
            queries[kind] + " LIMIT ? OFFSET ?",
            (*params, page_size + 1, page * page_size),
        )]
    return rows[:page_size], len(rows) > page_size


def _record(conn, user_id, kind, record_id):
    if kind == "log":
        record = _one(conn, """
            SELECT a.message_id AS id, l.study_date AS day,
                   a.qualification, a.exam_section, a.questions,
                   a.correct_answers, a.score_percent, a.notes
            FROM study_logs AS l JOIN study_log_analysis AS a
                ON a.message_id = l.message_id
            WHERE l.message_id = ? AND l.user_id = ? AND a.user_id = ?
                AND a.qualification = 'SG'
        """, (record_id, user_id, user_id))
        if record is not None:
            record["categories"] = [dict(row) for row in conn.execute("""
                SELECT major_category, category, questions,
                       correct_answers, score_percent
                FROM study_log_category_results
                WHERE message_id = ? ORDER BY id
            """, (record_id,))]
            record["b"] = _one(conn, """
                SELECT topic, questions, correct_answers,
                       wrong_reason, memo
                FROM sg_b_practice WHERE message_id = ? AND user_id = ?
            """, (record_id, user_id))
        return record
    queries = {
        "session": """
            SELECT id, study_date AS day, start_time, end_time,
                   duration_seconds FROM study_sessions
            WHERE id = ? AND user_id = ?
        """,
        "mistake": """
            SELECT id, category, question_ref, reason, memo,
                   created_on, next_review_on, success_streak, completed_on
            FROM sg_mistakes WHERE id = ? AND user_id = ?
        """,
        "plan": """
            SELECT id, start_on, weeks, weekly_questions, active
            FROM sg_plans WHERE id = ? AND user_id = ?
        """,
    }
    if kind not in queries:
        raise ValueError("記録の種類が正しくありません。")
    return _one(conn, queries[kind], (record_id, user_id))


def get_record(db_path, user_id, kind, record_id):
    with _connect(db_path) as conn:
        return _record(conn, user_id, kind, record_id)


def editable_fields(record, kind):
    if kind != "log":
        return FIELDS[kind]
    if record["exam_section"] == "B":
        return {key: FIELDS[kind][key] for key in (
            "category", "questions", "correct_answers",
            "wrong_reason", "notes",
        )}
    return {key: FIELDS[kind][key] for key in (
        "category", "questions", "score_percent", "notes",
    )}


def _clean_text(value, max_length, required=False):
    value = (value or "").strip()
    if len(value) > max_length or (required and not value):
        raise ValueError(f"1〜{max_length}文字で入力してください。" if required
                         else f"{max_length}文字以内で入力してください。")
    return value or None


def _positive_int(value, low, high, label):
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text.isdecimal() or not low <= int(text) <= high:
        raise ValueError(f"{label}は{low}〜{high}の整数で入力してください。")
    return int(text)


def _parse_value(kind, record, field, value):
    if field not in editable_fields(record, kind):
        raise ValueError("この項目は修正できません。")
    if kind == "log" and record["exam_section"] == "B" and record["b"] is None:
        raise ValueError("科目Bの詳細データがないため、この記録は修正できません。")
    if field == "category":
        options = SG_B_TOPICS if kind == "log" and record["exam_section"] == "B" \
            else SG_PRACTICE_CATEGORIES
        if value not in options:
            raise ValueError("一覧から分野を選択してください。")
        return value
    if kind == "log":
        if field in ("questions", "score_percent") and record["exam_section"] != "B":
            categories = record["categories"]
            if len(categories) != 1 or categories[0]["category"] not in SG_PRACTICE_CATEGORIES:
                raise ValueError("旧形式のログです。先に正しい分野を選んでください。")
        if field == "questions":
            count = parse_question_count_input(value)
            if record["exam_section"] == "B" and record["correct_answers"] > count:
                raise ValueError("問題数は現在の正解数以上にしてください。")
            if (record["exam_section"] == "B"
                    and record["correct_answers"] < count
                    and not record["b"]["wrong_reason"]):
                raise ValueError("先に判断を間違えた理由を入力してください。")
            return count
        if field == "score_percent":
            if record["questions"] is None:
                raise ValueError("先に問題数を入力してください。")
            return parse_score_percent_input(value)
        if field == "correct_answers":
            count = parse_correct_count(value, record["questions"])
            if count < record["questions"] and not record["b"]["wrong_reason"]:
                raise ValueError("先に判断を間違えた理由を入力してください。")
            return count
        if field == "wrong_reason":
            reason = _clean_text(value, 300)
            if record["correct_answers"] < record["questions"] and not reason:
                raise ValueError("誤答がある場合は理由が必要です。")
            return reason
        if field == "notes":
            return _clean_text(value, 300)
    if kind == "session":
        return _positive_int(value, 0, 1440, "勉強時間（分）")
    if kind == "mistake":
        if field == "question_ref":
            return validate_question_ref(value)
        limits = {"question_ref": 200, "reason": 300, "memo": 500}
        return _clean_text(value, limits[field], field != "memo")
    if kind == "plan":
        if field == "start_on":
            try:
                return date.fromisoformat(str(value).strip()).isoformat()
            except ValueError as error:
                raise ValueError("開始日はYYYY-MM-DD形式で入力してください。") from error
        return _positive_int(
            value, 1, 16 if field == "weeks" else 500,
            "週数" if field == "weeks" else "週目標",
        )
    raise ValueError("この項目は修正できません。")


def _old_value(kind, record, field):
    if field == "category" and kind == "log":
        return "、".join(
            item["category"] or item["major_category"]
            for item in record["categories"]
        ) or "未分類"
    if kind == "session":
        return f"{record['duration_seconds'] / 60:g}分"
    if kind == "log" and field == "wrong_reason":
        return (record["b"] or {}).get("wrong_reason") or "なし"
    if kind == "log" and field == "notes" and record["exam_section"] == "B":
        return (record["b"] or {}).get("memo") or "なし"
    return record.get(field) if record.get(field) is not None else "なし"


def prepare_edit(db_path, user_id, kind, record_id, field, raw_value):
    record = get_record(db_path, user_id, kind, record_id)
    if record is None:
        raise ValueError("この記録は見つかりません。")
    value = _parse_value(kind, record, field, raw_value)
    return {
        "record": record, "value": value,
        "field_label": FIELDS[kind][field],
        "before": _old_value(kind, record, field),
        "after": value if value is not None else "なし",
    }


def _update_log(conn, user_id, record, field, value):
    message_id = record["id"]
    section = record["exam_section"]
    is_b = section == "B"
    questions = value if field == "questions" else record["questions"]
    correct = value if field == "correct_answers" else record["correct_answers"]
    score = value if field == "score_percent" else record["score_percent"]
    if field == "category":
        major = "科目B" if is_b else SG_CATEGORY_TO_MAJOR[value]
        conn.execute("""
            DELETE FROM study_log_category_results WHERE message_id = ?
        """, (message_id,))
        conn.execute("""
            INSERT INTO study_log_category_results (
                message_id, major_category, category, questions,
                correct_answers, score_percent
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (message_id, major, value, questions, correct, score))
        if is_b and record["b"] is not None:
            conn.execute("""
                UPDATE sg_b_practice SET topic = ?
                WHERE message_id = ? AND user_id = ?
            """, (value, message_id, user_id))
        return
    if field in ("questions", "correct_answers", "score_percent"):
        if is_b:
            score = score_from_counts(correct, questions)
            conn.execute("""
                UPDATE sg_b_practice SET questions = ?, correct_answers = ?
                WHERE message_id = ? AND user_id = ?
            """, (questions, correct, message_id, user_id))
        else:
            correct = infer_correct_answers(questions, score) if score is not None else None
        conn.execute("""
            UPDATE study_log_analysis
            SET questions = ?, correct_answers = ?, score_percent = ?
            WHERE message_id = ? AND user_id = ?
        """, (questions, correct, score, message_id, user_id))
        conn.execute("""
            UPDATE study_log_category_results
            SET questions = ?, correct_answers = ?, score_percent = ?
            WHERE message_id = ?
        """, (questions, correct, score, message_id))
        return
    if is_b:
        if record["b"] is None:
            raise ValueError("科目Bの詳細データがありません。")
        b_field = "wrong_reason" if field == "wrong_reason" else "memo"
        conn.execute(f"""
            UPDATE sg_b_practice SET {b_field} = ?
            WHERE message_id = ? AND user_id = ?
        """, (value, message_id, user_id))
        reason = value if field == "wrong_reason" else record["b"]["wrong_reason"]
        memo = value if field == "notes" else record["b"]["memo"]
        value = " / ".join(
            part for part in (f"判断ミス：{reason}" if reason else None, memo)
            if part
        ) or None
    conn.execute("""
        UPDATE study_log_analysis SET notes = ?
        WHERE message_id = ? AND user_id = ?
    """, (value, message_id, user_id))


def apply_edit(db_path, user_id, kind, record_id, field, value, expected_record):
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _record(conn, user_id, kind, record_id)
        if current is None or current != expected_record:
            raise ValueError("記録が変更されています。/data から確認し直してください。")
        _parse_value(kind, current, field, value)
        if kind == "log":
            _update_log(conn, user_id, current, field, value)
        elif kind == "session":
            start = datetime.fromisoformat(current["start_time"])
            end = start + timedelta(minutes=value)
            conn.execute("""
                UPDATE study_sessions SET duration_seconds = ?, end_time = ?
                WHERE id = ? AND user_id = ?
            """, (value * 60, end.isoformat(), record_id, user_id))
        elif kind == "mistake":
            conn.execute(f"""
                UPDATE sg_mistakes SET {field} = ?
                WHERE id = ? AND user_id = ?
            """, (value, record_id, user_id))
        elif kind == "plan":
            conn.execute(f"""
                UPDATE sg_plans SET {field} = ?
                WHERE id = ? AND user_id = ?
            """, (value, record_id, user_id))


def _scope_ids(conn, user_id, scope):
    if scope not in RESET_SCOPES:
        raise ValueError("リセット対象が正しくありません。")
    result = {}
    queries = {
        "logs": "SELECT message_id FROM study_logs WHERE user_id = ?",
        "sg_logs": """
            SELECT l.message_id FROM study_logs AS l
            JOIN study_log_analysis AS a ON a.message_id = l.message_id
            WHERE l.user_id = ? AND a.user_id = ? AND a.qualification = 'SG'
        """,
        "sessions": "SELECT id FROM study_sessions WHERE user_id = ?",
        "mistakes": "SELECT id FROM sg_mistakes WHERE user_id = ?",
        "plans": "SELECT id FROM sg_plans WHERE user_id = ?",
    }
    keys = ("logs", "sessions", "mistakes", "plans") if scope == "all" else (scope,)
    for key in keys:
        params = (user_id, user_id) if key == "sg_logs" else (user_id,)
        result[key] = tuple(sorted(
            row[0] for row in conn.execute(queries[key], params)
        ))
    return result


def prepare_reset(db_path, user_id, scope):
    with _connect(db_path) as conn:
        if scope in ("sessions", "all") and conn.execute("""
            SELECT 1 FROM active_study_sessions WHERE user_id = ? LIMIT 1
        """, (user_id,)).fetchone() is not None:
            raise ValueError("通話勉強中は時間をリセットできません。通話終了後に試してください。")
        return _scope_ids(conn, user_id, scope)


def reset_user_data(db_path, user_id, scope, expected_ids):
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if scope in ("sessions", "all") and conn.execute("""
            SELECT 1 FROM active_study_sessions WHERE user_id = ? LIMIT 1
        """, (user_id,)).fetchone() is not None:
            raise ValueError("通話勉強中は時間をリセットできません。")
        if _scope_ids(conn, user_id, scope) != expected_ids:
            raise ValueError("対象の記録が変わりました。/data から確認し直してください。")
        if scope in ("sg_logs", "all"):
            log_ids = expected_ids["sg_logs" if scope == "sg_logs" else "logs"]
            for table in (
                "sg_b_practice", "study_log_category_results",
                "study_log_analysis", "study_logs",
            ):
                conn.executemany(
                    f"DELETE FROM {table} WHERE message_id = ?",
                    ((message_id,) for message_id in log_ids),
                )
        if scope in ("sessions", "all"):
            conn.execute("DELETE FROM study_sessions WHERE user_id = ?", (user_id,))
        if scope in ("mistakes", "all"):
            conn.execute("""
                DELETE FROM sg_mistake_attempts WHERE mistake_id IN (
                    SELECT id FROM sg_mistakes WHERE user_id = ?
                )
            """, (user_id,))
            conn.execute("DELETE FROM sg_mistakes WHERE user_id = ?", (user_id,))
        if scope in ("plans", "all"):
            conn.execute("DELETE FROM sg_plans WHERE user_id = ?", (user_id,))
    return sum(len(ids) for ids in expected_ids.values())
