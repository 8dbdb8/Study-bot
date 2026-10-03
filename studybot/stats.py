"""学習時間・成績の集計と、ロードマップ・試験日・連続学習日数の取得。"""

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

from studybot import config
from studybot.config import JST
from studybot.exam_schedule import (
    format_exam_countdown,
    get_exam_date,
    get_study_streak,
)
from studybot.study_log_parser import SG_MAJOR_CATEGORIES


# ============================================================
# 資格ロードマップ
# ============================================================

def get_roadmap():
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            qualification,
            display_name,
            sort_order,
            status,
            is_current
        FROM certification_roadmap
        ORDER BY sort_order ASC
    """)

    rows = cursor.fetchall()
    conn.close()

    return rows


def get_current_qualification():
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            qualification,
            display_name
        FROM certification_roadmap
        WHERE is_current = 1
        ORDER BY sort_order ASC
        LIMIT 1
    """)

    row = cursor.fetchone()
    conn.close()

    if row is None:
        return None

    return {
        "qualification": row[0],
        "display_name": row[1]
    }


def get_current_exam_target(user_id):
    """現在の最優先資格と、本人が設定した試験日（未設定ならNone）。"""
    try:
        current = get_current_qualification()
    except sqlite3.Error:
        current = None

    qualification = current["qualification"] if current else "SG"

    try:
        exam_on = get_exam_date(config.DB_PATH, user_id, qualification)
    except sqlite3.Error:
        exam_on = None

    return {
        "qualification": qualification,
        "display_name": (
            current["display_name"] if current else qualification
        ),
        "label": f"{qualification}試験",
        "exam_on": exam_on,
    }


def get_exam_countdown_line(user_id, today=None):
    target = get_current_exam_target(user_id)
    if target["exam_on"] is None:
        return None
    return format_exam_countdown(
        target["exam_on"],
        today or datetime.now(JST).date(),
        target["label"],
    )


def get_study_streak_safe(user_id, today=None):
    try:
        return get_study_streak(
            config.DB_PATH, user_id, today or datetime.now(JST).date()
        )
    except sqlite3.Error:
        return 0


# ============================================================
# 集計
# ============================================================

def get_today_total(user_id):
    today = datetime.now(JST).strftime("%Y-%m-%d")

    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COALESCE(SUM(duration_seconds), 0)
        FROM study_sessions
        WHERE user_id = ?
        AND study_date = ?
    """, (
        user_id,
        today
    ))

    total_seconds = cursor.fetchone()[0]

    conn.close()

    return total_seconds


def get_week_total(user_id):
    now = datetime.now(JST)

    monday = now - timedelta(
        days=now.weekday()
    )

    start_date = monday.strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            study_date,
            COALESCE(SUM(duration_seconds), 0)
        FROM study_sessions
        WHERE user_id = ?
        AND study_date BETWEEN ? AND ?
        GROUP BY study_date
        ORDER BY study_date ASC
    """, (
        user_id,
        start_date,
        end_date
    ))

    rows = cursor.fetchall()

    conn.close()

    return rows


def get_week_total_seconds(user_id):
    return sum(
        seconds
        for _, seconds
        in get_week_total(user_id)
    )


def get_today_logs(user_id):
    today = datetime.now(JST).strftime("%Y-%m-%d")

    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT content
        FROM study_logs
        WHERE user_id = ?
        AND study_date = ?
        ORDER BY created_at ASC
    """, (
        user_id,
        today
    ))

    rows = cursor.fetchall()

    conn.close()

    return [
        row[0]
        for row in rows
    ]


def get_study_status(user_id, qualification="SG"):
    """
    資格ごとの累積状況。

    正答率は問題数が分かる場合、
    問題数で重み付けした平均に統一する。
    """
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            a.questions,
            a.correct_answers,
            a.score_percent,
            a.weak_points
        FROM study_log_analysis AS a
        JOIN study_logs AS l
            ON a.message_id = l.message_id
        WHERE a.user_id = ?
        AND a.qualification = ?
        ORDER BY a.analyzed_at ASC
    """, (
        user_id,
        qualification
    ))

    rows = cursor.fetchall()

    conn.close()

    total_questions = 0
    weak_point_counter = Counter()

    weighted_score_sum = 0
    weighted_question_sum = 0
    fallback_scores = []

    for (
        questions,
        correct_answers,
        score_percent,
        weak_points_json
    ) in rows:

        if questions is not None:
            total_questions += questions

        if score_percent is not None:
            fallback_scores.append(
                score_percent
            )

            if (
                questions is not None
                and questions > 0
            ):
                if correct_answers is not None:
                    weighted_score_sum += (
                        correct_answers * 100
                    )
                else:
                    weighted_score_sum += (
                        score_percent
                        * questions
                    )

                weighted_question_sum += (
                    questions
                )

        if weak_points_json:
            try:
                weak_points = json.loads(
                    weak_points_json
                )

                weak_point_counter.update(
                    weak_points
                )

            except json.JSONDecodeError:
                pass

    if weighted_question_sum > 0:
        average_score = (
            weighted_score_sum
            / weighted_question_sum
        )

    elif fallback_scores:
        average_score = (
            sum(fallback_scores)
            / len(fallback_scores)
        )

    else:
        average_score = None

    return {
        "total_questions": total_questions,
        "average_score": average_score,
        "weak_points":
            weak_point_counter.most_common(5),
        "log_count": len(rows),
        "category_status": get_category_status(
            user_id,
            qualification
        )
    }


def get_category_status(
    user_id,
    qualification="SG",
    start_date=None,
    end_date=None
):
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            c.major_category,
            c.category,
            c.questions,
            c.correct_answers,
            c.score_percent
        FROM study_log_category_results AS c
        JOIN study_log_analysis AS a
            ON c.message_id = a.message_id
        JOIN study_logs AS l
            ON a.message_id = l.message_id
        WHERE a.user_id = ?
        AND a.qualification = ?
        AND (? IS NULL OR l.study_date >= ?)
        AND (? IS NULL OR l.study_date <= ?)
        ORDER BY l.study_date ASC, l.created_at ASC
    """, (
        user_id,
        qualification,
        start_date,
        start_date,
        end_date,
        end_date
    ))

    rows = cursor.fetchall()
    conn.close()

    grouped = {}

    for (
        major_category,
        category,
        questions,
        correct_answers,
        score_percent
    ) in rows:
        key = (major_category, category)
        data = grouped.setdefault(key, {
            "log_count": 0,
            "scored_log_count": 0,
            "weighted_score_sum": 0,
            "weighted_question_sum": 0,
            "fallback_scores": [],
        })
        data["log_count"] += 1

        if score_percent is None:
            continue

        data["scored_log_count"] += 1

        if questions is not None and questions > 0:
            if correct_answers is not None:
                data["weighted_score_sum"] += (
                    correct_answers * 100
                )
            else:
                data["weighted_score_sum"] += (
                    score_percent * questions
                )

            data["weighted_question_sum"] += questions
        else:
            data["fallback_scores"].append(
                score_percent
            )

    major_order = {
        name: index
        for index, name in enumerate(
            SG_MAJOR_CATEGORIES
        )
    }
    result = []

    for (major_category, category), data in grouped.items():
        if data["weighted_question_sum"] > 0:
            average_score = (
                data["weighted_score_sum"]
                / data["weighted_question_sum"]
            )
        elif data["fallback_scores"]:
            average_score = (
                sum(data["fallback_scores"])
                / len(data["fallback_scores"])
            )
        else:
            average_score = None

        result.append({
            "major_category": major_category,
            "category": category,
            "average_score": average_score,
            "log_count": data["log_count"],
            "scored_log_count": data[
                "scored_log_count"
            ],
        })

    return sorted(
        result,
        key=lambda item: (
            major_order.get(
                item["major_category"],
                len(major_order),
            ),
            item["category"] or "",
        ),
    )


def get_week_analysis_status(
    user_id,
    qualification="SG"
):
    now = datetime.now(JST)

    monday = now - timedelta(
        days=now.weekday()
    )

    start_date = monday.strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            a.questions,
            a.correct_answers,
            a.score_percent,
            a.weak_points
        FROM study_log_analysis AS a

        JOIN study_logs AS l
            ON a.message_id = l.message_id

        WHERE a.user_id = ?
        AND a.qualification = ?
        AND l.study_date BETWEEN ? AND ?

        ORDER BY
            l.study_date ASC,
            l.created_at ASC
    """, (
        user_id,
        qualification,
        start_date,
        end_date
    ))

    rows = cursor.fetchall()

    conn.close()

    total_questions = 0
    weak_point_counter = Counter()

    weighted_score_sum = 0
    weighted_question_sum = 0
    fallback_scores = []

    for (
        questions,
        correct_answers,
        score_percent,
        weak_points_json
    ) in rows:

        if questions is not None:
            total_questions += questions

        if score_percent is not None:
            fallback_scores.append(
                score_percent
            )

            if (
                questions is not None
                and questions > 0
            ):
                if correct_answers is not None:
                    weighted_score_sum += (
                        correct_answers * 100
                    )
                else:
                    weighted_score_sum += (
                        score_percent
                        * questions
                    )

                weighted_question_sum += (
                    questions
                )

        if weak_points_json:
            try:
                weak_points = json.loads(
                    weak_points_json
                )

                weak_point_counter.update(
                    weak_points
                )

            except json.JSONDecodeError:
                pass

    if weighted_question_sum > 0:
        average_score = (
            weighted_score_sum
            / weighted_question_sum
        )

    elif fallback_scores:
        average_score = (
            sum(fallback_scores)
            / len(fallback_scores)
        )

    else:
        average_score = None

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_questions": total_questions,
        "average_score": average_score,
        "weak_points":
            weak_point_counter.most_common(5),
        "log_count": len(rows)
    }
