"""予想得点（1000点満点）と、本番形式の模試の記録。

予想得点は直近の正答率から出す「目安」。本番の採点（IRT など）とは違う。
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta


# 予想に使う期間と、予想を出すのに必要な科目Aの問題数
PREDICTION_DAYS = 28
MIN_PREDICTION_QUESTIONS = 20
MAX_SCORE = 1000

DISCLAIMER = "直近4週間の正答率からの目安です。本番の採点方式とは異なります。"


def init_scoring_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mock_exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            qualification TEXT NOT NULL,
            taken_on TEXT NOT NULL,
            parts TEXT NOT NULL,
            minutes INTEGER,
            score INTEGER NOT NULL
        )
    """)
    # 動いている模試タイマー（Botを再起動しても続きから動かすため）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mock_timers (
            user_id INTEGER PRIMARY KEY,
            qualification TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            minutes INTEGER NOT NULL
        )
    """)


# ------------------------------------------------------------
# 予想得点
# ------------------------------------------------------------

def part_results(db_path, user_id, qualification, since):
    """{"A": (正解数, 問題数), "B": (正解数, 問題数)}（since 以降）。"""
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT a.questions, a.correct_answers, a.score_percent
            FROM study_log_analysis AS a
            JOIN study_logs AS l ON l.message_id = a.message_id
            WHERE a.user_id = ? AND a.qualification = ?
              AND COALESCE(a.exam_section, 'A') = 'A'
              AND l.study_date >= ?
        """, (user_id, qualification.code, since.isoformat())).fetchall()
        b_row = conn.execute("""
            SELECT COALESCE(SUM(b.correct_answers), 0),
                   COALESCE(SUM(b.questions), 0)
            FROM sg_b_practice AS b
            JOIN study_logs AS l ON l.message_id = b.message_id
            WHERE b.user_id = ? AND b.qualification = ?
              AND b.practiced_on >= ?
        """, (user_id, qualification.code, since.isoformat())).fetchone()

    correct = 0.0
    total = 0
    for questions, correct_answers, score in rows:
        if not questions:
            continue
        if correct_answers is not None:
            correct += correct_answers
        elif score is not None:
            correct += score * questions / 100
        else:
            continue
        total += questions
    return {"A": (correct, total), "B": (b_row[0], b_row[1])}


def predict_score(db_path, user_id, qualification, today):
    """予想得点。科目Aの記録が足りなければ None。

    戻り値：{"parts": [(区分, 正答率%, 解いた数)], "score": 合計点 or None,
    "part_scores": [区分ごとの点], "pass_score", "margin", "note", "hint"}
    """
    since = today - timedelta(days=PREDICTION_DAYS - 1)
    results = part_results(db_path, user_id, qualification, since)
    a_correct, a_total = results["A"]
    if a_total < MIN_PREDICTION_QUESTIONS:
        return None

    a_rate = a_correct / a_total
    parts = [(qualification.exam_parts[0][0], a_rate * 100, a_total)]
    note = None
    rates = [a_rate]
    if qualification.has_part_b:
        b_correct, b_total = results["B"]
        if b_total:
            b_rate = b_correct / b_total
        else:
            b_rate = a_rate
            note = "科目Bの記録がないため、科目Aの正答率で代わりに計算しています。"
        parts.append((qualification.exam_parts[1][0], b_rate * 100, b_total))
        rates.append(b_rate)

    pass_score = qualification.pass_score
    hint = None
    if qualification.scoring == "separate":
        part_scores = [round(rate * MAX_SCORE) for rate in rates]
        score = None
        margin = (
            min(part_scores) - pass_score if pass_score is not None else None
        )
    else:
        counts = [count for _, count in qualification.exam_parts]
        if len(rates) > 1 and all(counts):
            all_questions = sum(counts)
            score = round(
                sum(rate * count for rate, count in zip(rates, counts))
                / all_questions * MAX_SCORE
            )
            # 正答率が低い方の区分を10%上げたときに増える点数
            lower = min(range(len(rates)), key=lambda index: rates[index])
            gain = round(counts[lower] / all_questions * 0.1 * MAX_SCORE)
            hint = (
                f"{qualification.exam_parts[lower][0]}の正答率を"
                f"10%上げると ＋{gain}点"
            )
        else:
            score = round(rates[0] * MAX_SCORE)
        part_scores = [score]
        margin = score - pass_score if pass_score is not None else None

    return {
        "parts": parts,
        "score": score,
        "part_scores": part_scores,
        "pass_score": pass_score,
        "margin": margin,
        "note": note,
        "hint": hint,
    }


def format_margin(margin):
    if margin is None:
        return ""
    if margin >= 0:
        return f"合格ライン＋{margin}点"
    return f"合格ラインまで あと{-margin}点"


def prediction_summary(prediction, qualification):
    """1行の要約（例：予想 640点（合格ライン＋40点））。"""
    if prediction is None:
        return None
    if qualification.scoring == "separate":
        scores = " / ".join(
            f"{name} {score}点"
            for (name, _, _), score in zip(
                prediction["parts"], prediction["part_scores"]
            )
        )
        text = f"予想 {scores}"
    else:
        text = f"予想 {prediction['score']}点"
    margin = format_margin(prediction["margin"])
    return f"{text}（{margin}）" if margin else text


# ------------------------------------------------------------
# 模試
# ------------------------------------------------------------

def mock_score(qualification, parts):
    """parts：[(正解数, 問題数), ...]。合計点（区分ごとの採点なら一番低い点）。"""
    if qualification.scoring == "separate":
        return min(round(correct / total * MAX_SCORE) for correct, total in parts)
    correct = sum(correct for correct, _ in parts)
    total = sum(total for _, total in parts)
    return round(correct / total * MAX_SCORE)


def save_mock_exam(db_path, user_id, qualification, taken_on, parts, minutes):
    for correct, total in parts:
        if not 1 <= total <= 500 or not 0 <= correct <= total:
            raise ValueError("正解数は0〜問題数の範囲で入力してください。")
    if minutes is not None and not 1 <= minutes <= 600:
        raise ValueError("時間は1〜600分で入力してください。")
    score = mock_score(qualification, parts)
    parts_text = ",".join(f"{correct}/{total}" for correct, total in parts)
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            cursor = conn.execute("""
                INSERT INTO mock_exams (
                    user_id, qualification, taken_on, parts, minutes, score
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_id, qualification.code, taken_on.isoformat(),
                parts_text, minutes, score,
            ))
            return cursor.lastrowid, score


def list_mock_exams(db_path, user_id, qualification_code):
    """古い順の模試：[{"taken_on", "parts", "minutes", "score"}]。"""
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT taken_on, parts, minutes, score FROM mock_exams
            WHERE user_id = ? AND qualification = ?
            ORDER BY taken_on, id
        """, (user_id, qualification_code)).fetchall()
    return [
        {
            "taken_on": taken_on,
            "parts": [
                tuple(int(n) for n in part.split("/"))
                for part in parts.split(",")
            ],
            "minutes": minutes,
            "score": score,
        }
        for taken_on, parts, minutes, score in rows
    ]


def has_recent_mock(db_path, user_id, qualification_code, today, days=7):
    since = (today - timedelta(days=days - 1)).isoformat()
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("""
            SELECT 1 FROM mock_exams
            WHERE user_id = ? AND qualification = ? AND taken_on >= ?
            LIMIT 1
        """, (user_id, qualification_code, since)).fetchone()
    return row is not None


# ------------------------------------------------------------
# 模試タイマー
# ------------------------------------------------------------

def save_mock_timer(db_path, user_id, qualification, channel_id, started_at, minutes):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO mock_timers (
                    user_id, qualification, channel_id, started_at, minutes
                ) VALUES (?, ?, ?, ?, ?)
            """, (user_id, qualification, channel_id, started_at.isoformat(), minutes))


def get_mock_timer(db_path, user_id):
    """{"qualification", "channel_id", "started_at", "minutes"}。なければ None。"""
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("""
            SELECT qualification, channel_id, started_at, minutes
            FROM mock_timers WHERE user_id = ?
        """, (user_id,)).fetchone()
    if row is None:
        return None
    return {
        "user_id": user_id, "qualification": row[0], "channel_id": row[1],
        "started_at": datetime.fromisoformat(row[2]), "minutes": row[3],
    }


def list_mock_timers(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        user_ids = [row[0] for row in conn.execute("SELECT user_id FROM mock_timers")]
    return [get_mock_timer(db_path, user_id) for user_id in user_ids]


def delete_mock_timer(db_path, user_id):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("DELETE FROM mock_timers WHERE user_id = ?", (user_id,))
