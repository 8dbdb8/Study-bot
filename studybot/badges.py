"""実績バッジ：連続日数や累計の問題数などの節目で獲得する。

一度獲得したバッジは、その後に数字が下がっても（連続が途切れても）消えない。
"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass

from studybot.exam_schedule import get_study_streak
from studybot.qualifications import get_qualification


@dataclass(frozen=True)
class Badge:
    id: str
    emoji: str
    name: str
    metric: str
    threshold: int
    unit: str


BADGES = (
    Badge("streak_7", "🔥", "1週間つづいた", "streak", 7, "日連続"),
    Badge("streak_30", "🔥", "1か月つづいた", "streak", 30, "日連続"),
    Badge("streak_100", "🔥", "100日つづいた", "streak", 100, "日連続"),
    Badge("questions_100", "📝", "100問解いた", "questions", 100, "問"),
    Badge("questions_500", "📝", "500問解いた", "questions", 500, "問"),
    Badge("questions_1000", "📝", "1000問解いた", "questions", 1000, "問"),
    Badge("questions_3000", "📝", "3000問解いた", "questions", 3000, "問"),
    Badge("hours_10", "⏱", "勉強10時間", "hours", 10, "時間"),
    Badge("hours_50", "⏱", "勉強50時間", "hours", 50, "時間"),
    Badge("hours_100", "⏱", "勉強100時間", "hours", 100, "時間"),
    Badge("mistakes_10", "🔁", "誤答を10問克服", "mistakes", 10, "問完了"),
    Badge("mistakes_50", "🔁", "誤答を50問克服", "mistakes", 50, "問完了"),
    Badge("mistakes_100", "🔁", "誤答を100問克服", "mistakes", 100, "問完了"),
    Badge("mock_pass", "🏆", "模試で合格ライン突破", "mock_pass", 1, "回"),
    Badge("focus_10", "🍅", "集中タイマー10セット", "focus", 10, "セット"),
    Badge("focus_50", "🍅", "集中タイマー50セット", "focus", 50, "セット"),
    Badge("quiz_perfect", "💯", "ミニテスト全問正解", "quiz_perfect", 1, "回"),
    Badge("notes_7", "✏️", "ひとことを7日書いた", "notes", 7, "日"),
)
BADGES_BY_ID = {badge.id: badge for badge in BADGES}


def init_badge_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS earned_badges (
            user_id INTEGER NOT NULL,
            badge_id TEXT NOT NULL,
            earned_on TEXT NOT NULL,
            PRIMARY KEY (user_id, badge_id)
        )
    """)


def _scalar(conn, query, params):
    try:
        return conn.execute(query, params).fetchone()[0] or 0
    except sqlite3.OperationalError:
        # まだ作られていない表（古いDB）なら 0
        return 0


def _mock_passes(conn, user_id):
    try:
        rows = conn.execute(
            "SELECT qualification, score FROM mock_exams WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    passes = 0
    for code, score in rows:
        qualification = get_qualification(code)
        if qualification and qualification.pass_score and score >= qualification.pass_score:
            passes += 1
    return passes


def badge_metrics(db_path, user_id, today):
    """バッジの判定に使う数字。"""
    with closing(sqlite3.connect(db_path)) as conn:
        metrics = {
            "questions": _scalar(conn, """
                SELECT SUM(questions) FROM study_log_analysis WHERE user_id = ?
            """, (user_id,)),
            "hours": _scalar(conn, """
                SELECT SUM(duration_seconds) FROM study_sessions WHERE user_id = ?
            """, (user_id,)) // 3600,
            "mistakes": _scalar(conn, """
                SELECT COUNT(*) FROM sg_mistakes
                WHERE user_id = ? AND completed_on IS NOT NULL
            """, (user_id,)),
            "focus": _scalar(conn, """
                SELECT SUM(sets) FROM focus_sets WHERE user_id = ?
            """, (user_id,)),
            "quiz_perfect": _scalar(conn, """
                SELECT COUNT(*) FROM quiz_results
                WHERE user_id = ? AND correct = total AND total > 0
            """, (user_id,)),
            "notes": _scalar(conn, """
                SELECT COUNT(*) FROM daily_notes WHERE user_id = ?
            """, (user_id,)),
            "mock_pass": _mock_passes(conn, user_id),
        }
    metrics["streak"] = get_study_streak(db_path, user_id, today)
    return metrics


def get_earned_badges(db_path, user_id):
    """{badge_id: 獲得日} 。"""
    with closing(sqlite3.connect(db_path)) as conn:
        return dict(conn.execute("""
            SELECT badge_id, earned_on FROM earned_badges WHERE user_id = ?
        """, (user_id,)).fetchall())


def check_new_badges(db_path, user_id, today):
    """新しく獲得したバッジを記録して返す。"""
    metrics = badge_metrics(db_path, user_id, today)
    earned = get_earned_badges(db_path, user_id)
    new = [
        badge for badge in BADGES
        if badge.id not in earned and metrics[badge.metric] >= badge.threshold
    ]
    if new:
        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                conn.executemany("""
                    INSERT OR IGNORE INTO earned_badges (user_id, badge_id, earned_on)
                    VALUES (?, ?, ?)
                """, [(user_id, badge.id, today.isoformat()) for badge in new])
    return new


def next_goals(db_path, user_id, today):
    """まだ獲得していない、それぞれの種類の次のバッジと今の数字。[(Badge, 今の値)]"""
    metrics = badge_metrics(db_path, user_id, today)
    earned = get_earned_badges(db_path, user_id)
    goals = []
    seen = set()
    for badge in BADGES:
        if badge.id in earned or badge.metric in seen:
            continue
        seen.add(badge.metric)
        goals.append((badge, metrics[badge.metric]))
    return goals
