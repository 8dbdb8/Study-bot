"""学習データDBの初期化・移行と、VC時間・勉強ログの保存と削除。"""

import json
import os
import sqlite3
from datetime import datetime

from studybot import config
from studybot.channels import init_channel_settings_table
from studybot.config import JST
from studybot.daily_digest import init_daily_digest_tables
from studybot.exam_results import init_exam_result_tables
from studybot.notion_store import init_notion_final_table, init_notion_tables
from studybot.weekly_report import init_weekly_report_tables
from studybot.exam_schedule import init_exam_date_table
from studybot.formatting import parse_iso_datetime
from studybot.sg_features import init_sg_feature_tables
from studybot.sg_glossary import init_sg_glossary_rating_table
from studybot.study_log_parser import normalize_study_analysis


def _decode_json_list(value):
    if not value:
        return []

    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []

    return decoded if isinstance(decoded, list) else []


def _replace_category_results(
    cursor,
    message_id,
    category_results
):
    cursor.execute("""
        DELETE FROM study_log_category_results
        WHERE message_id = ?
    """, (
        message_id,
    ))

    for result in category_results:
        cursor.execute("""
            INSERT INTO study_log_category_results (
                message_id,
                major_category,
                category,
                questions,
                correct_answers,
                score_percent
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            message_id,
            result.get("major_category"),
            result.get("category"),
            result.get("questions"),
            result.get("correct_answers"),
            result.get("score_percent")
        ))


def _backfill_existing_study_analysis(cursor):
    cursor.execute("""
        SELECT
            l.message_id,
            l.content,
            a.qualification,
            a.activity,
            a.questions,
            a.correct_answers,
            a.score_percent,
            a.weak_points,
            a.notes
        FROM study_logs AS l
        JOIN study_log_analysis AS a
            ON a.message_id = l.message_id
    """)

    rows = cursor.fetchall()

    for row in rows:
        cursor.execute("""
            SELECT
                major_category,
                category,
                questions,
                correct_answers,
                score_percent
            FROM study_log_category_results
            WHERE message_id = ?
            ORDER BY id ASC
        """, (
            row[0],
        ))
        category_results = [
            {
                "major_category": category_row[0],
                "category": category_row[1],
                "questions": category_row[2],
                "correct_answers": category_row[3],
                "score_percent": category_row[4],
            }
            for category_row in cursor.fetchall()
        ]

        raw_analysis = {
            "qualification": row[2],
            "activity": row[3],
            "questions": row[4],
            "correct_answers": row[5],
            "score_percent": row[6],
            "category_results": category_results,
            "weak_points": _decode_json_list(row[7]),
            "notes": row[8],
        }
        analysis = normalize_study_analysis(
            row[1],
            raw_analysis,
            current_qualification="SG"
        )

        cursor.execute("""
            UPDATE study_log_analysis
            SET
                qualification = ?,
                activity = ?,
                questions = ?,
                correct_answers = ?,
                score_percent = ?,
                weak_points = ?,
                notes = ?,
                analysis_warnings = ?
            WHERE message_id = ?
        """, (
            analysis["qualification"],
            analysis["activity"],
            analysis["questions"],
            analysis["correct_answers"],
            analysis["score_percent"],
            json.dumps(
                analysis["weak_points"],
                ensure_ascii=False
            ),
            analysis["notes"],
            json.dumps(
                analysis["analysis_warnings"],
                ensure_ascii=False
            ),
            row[0]
        ))

        _replace_category_results(
            cursor,
            row[0],
            analysis["category_results"]
        )


# ============================================================
# データベース初期化・マイグレーション
# ============================================================

def init_db():
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)

    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    # 完了済みVC勉強時間
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS study_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            study_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL
        )
    """)

    # 進行中VCセッション
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_study_sessions (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            start_time TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
    """)

    # 元の勉強ログ
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS study_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            study_date TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # AI解析結果
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS study_log_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            qualification TEXT,
            activity TEXT,
            exam_section TEXT,
            questions INTEGER,
            correct_answers INTEGER,
            score_percent REAL,
            weak_points TEXT,
            notes TEXT,
            analysis_warnings TEXT,
            analyzed_at TEXT NOT NULL,
            reply_message_id INTEGER
        )
    """)

    cursor.execute("""
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
        AND name = 'study_log_category_results'
    """)
    category_table_existed = (
        cursor.fetchone() is not None
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS study_log_category_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            major_category TEXT NOT NULL,
            category TEXT,
            questions INTEGER,
            correct_answers INTEGER,
            score_percent REAL,
            FOREIGN KEY (message_id)
                REFERENCES study_log_analysis(message_id)
                ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_study_log_category_message
        ON study_log_category_results(message_id)
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)

    # 資格取得ロードマップ
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS certification_roadmap (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qualification TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            status TEXT NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 0
        )
    """)

    # 初期ロードマップ
    default_roadmap = [
        (
            "SG",
            "情報セキュリティマネジメント（SG）",
            1,
            "learning",
            1
        ),
        (
            "FE",
            "基本情報技術者（FE）",
            2,
            "pending",
            0
        ),
        (
            "医療情報技師",
            "医療情報技師",
            3,
            "pending",
            0
        )
    ]

    cursor.executemany("""
        INSERT INTO certification_roadmap (
            qualification,
            display_name,
            sort_order,
            status,
            is_current
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(qualification)
        DO UPDATE SET
            display_name = excluded.display_name,
            sort_order = excluded.sort_order
    """, default_roadmap)

    cursor.execute("""
        DELETE FROM certification_roadmap
        WHERE qualification IN (
            'Oracle Master Silver SQL',
            'Oracle Java Silver'
        )
    """)

    # 既存DB向けマイグレーション
    cursor.execute("PRAGMA table_info(study_log_analysis)")
    columns = {
        row[1]
        for row in cursor.fetchall()
    }

    needs_analysis_backfill = (
        "correct_answers" not in columns
        or "analysis_warnings" not in columns
        or not category_table_existed
    )

    if "reply_message_id" not in columns:
        cursor.execute("""
            ALTER TABLE study_log_analysis
            ADD COLUMN reply_message_id INTEGER
        """)

    if "correct_answers" not in columns:
        cursor.execute("""
            ALTER TABLE study_log_analysis
            ADD COLUMN correct_answers INTEGER
        """)

    if "analysis_warnings" not in columns:
        cursor.execute("""
            ALTER TABLE study_log_analysis
            ADD COLUMN analysis_warnings TEXT
        """)

    if "exam_section" not in columns:
        cursor.execute("""
            ALTER TABLE study_log_analysis
            ADD COLUMN exam_section TEXT
        """)

    init_sg_feature_tables(cursor)
    init_sg_glossary_rating_table(cursor)
    init_exam_date_table(cursor)
    init_daily_digest_tables(cursor)
    init_weekly_report_tables(cursor)
    init_notion_tables(cursor)
    init_notion_final_table(cursor)
    init_exam_result_tables(cursor)
    init_channel_settings_table(cursor)

    cursor.execute("""
        DELETE FROM study_log_category_results
        WHERE NOT EXISTS (
            SELECT 1
            FROM study_logs
            WHERE study_logs.message_id =
                study_log_category_results.message_id
        )
    """)

    cursor.execute("""
        DELETE FROM study_log_analysis
        WHERE NOT EXISTS (
            SELECT 1
            FROM study_logs
            WHERE study_logs.message_id =
                study_log_analysis.message_id
        )
    """)

    if needs_analysis_backfill:
        _backfill_existing_study_analysis(cursor)

    review_migration = (
        "20260929_objective_review_candidates"
    )
    cursor.execute("""
        SELECT 1
        FROM schema_migrations
        WHERE name = ?
    """, (
        review_migration,
    ))

    if cursor.fetchone() is None:
        _backfill_existing_study_analysis(cursor)

        cursor.execute("""
            INSERT INTO schema_migrations (
                name,
                applied_at
            )
            VALUES (?, ?)
        """, (
            review_migration,
            datetime.now(JST).isoformat()
        ))

    conn.commit()
    conn.close()


# ============================================================
# VCセッション保存
# ============================================================

def save_completed_study_session(
    guild_id,
    user_id,
    username,
    start_time,
    end_time,
    duration_seconds
):
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO study_sessions (
            guild_id,
            user_id,
            username,
            study_date,
            start_time,
            end_time,
            duration_seconds
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        guild_id,
        user_id,
        username,
        start_time.strftime("%Y-%m-%d"),
        start_time.isoformat(),
        end_time.isoformat(),
        duration_seconds
    ))

    conn.commit()
    conn.close()


def save_active_study_session(
    guild_id,
    user_id,
    username,
    start_time
):
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO active_study_sessions (
            guild_id,
            user_id,
            username,
            start_time
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET
            username = excluded.username,
            start_time = excluded.start_time
    """, (
        guild_id,
        user_id,
        username,
        start_time.isoformat()
    ))

    conn.commit()
    conn.close()


def get_active_study_session(guild_id, user_id):
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT start_time
        FROM active_study_sessions
        WHERE guild_id = ?
        AND user_id = ?
    """, (
        guild_id,
        user_id
    ))

    row = cursor.fetchone()
    conn.close()

    if row is None:
        return None

    return parse_iso_datetime(row[0])


def get_all_active_study_sessions():
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            guild_id,
            user_id,
            username,
            start_time
        FROM active_study_sessions
    """)

    rows = cursor.fetchall()
    conn.close()

    return rows


def delete_active_study_session(guild_id, user_id):
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        DELETE FROM active_study_sessions
        WHERE guild_id = ?
        AND user_id = ?
    """, (
        guild_id,
        user_id
    ))

    conn.commit()
    conn.close()


# ============================================================
# 勉強ログ保存・解析結果
# ============================================================

def save_study_log(message):
    created_at = message.created_at.astimezone(JST)

    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    # 編集時も内容を更新する
    cursor.execute("""
        INSERT INTO study_logs (
            guild_id,
            channel_id,
            message_id,
            user_id,
            username,
            study_date,
            content,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id)
        DO UPDATE SET
            guild_id = excluded.guild_id,
            channel_id = excluded.channel_id,
            user_id = excluded.user_id,
            username = excluded.username,
            study_date = excluded.study_date,
            content = excluded.content,
            created_at = excluded.created_at
    """, (
        message.guild.id,
        message.channel.id,
        message.id,
        message.author.id,
        message.author.display_name,
        created_at.strftime("%Y-%m-%d"),
        message.content,
        created_at.isoformat()
    ))

    conn.commit()
    conn.close()


def save_study_analysis(message, analysis, reply_message_id=None):
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO study_log_analysis (
            message_id,
            user_id,
            qualification,
            activity,
            exam_section,
            questions,
            correct_answers,
            score_percent,
            weak_points,
            notes,
            analysis_warnings,
            analyzed_at,
            reply_message_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(message_id)
        DO UPDATE SET
            user_id = excluded.user_id,
            qualification = excluded.qualification,
            activity = excluded.activity,
            exam_section = excluded.exam_section,
            questions = excluded.questions,
            correct_answers = excluded.correct_answers,
            score_percent = excluded.score_percent,
            weak_points = excluded.weak_points,
            notes = excluded.notes,
            analysis_warnings = excluded.analysis_warnings,
            analyzed_at = excluded.analyzed_at,
            reply_message_id = COALESCE(
                excluded.reply_message_id,
                study_log_analysis.reply_message_id
            )
    """, (
        message.id,
        message.author.id,
        analysis.get("qualification"),
        analysis.get("activity"),
        analysis.get("exam_section"),
        analysis.get("questions"),
        analysis.get("correct_answers"),
        analysis.get("score_percent"),
        json.dumps(
            analysis.get("weak_points", []),
            ensure_ascii=False
        ),
        analysis.get("notes"),
        json.dumps(
            analysis.get("analysis_warnings", []),
            ensure_ascii=False
        ),
        datetime.now(JST).isoformat(),
        reply_message_id
    ))

    _replace_category_results(
        cursor,
        message.id,
        analysis.get("category_results", [])
    )

    conn.commit()
    conn.close()


def get_analysis_reply_message_id(message_id):
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT reply_message_id
        FROM study_log_analysis
        WHERE message_id = ?
    """, (
        message_id,
    ))

    row = cursor.fetchone()
    conn.close()

    if row is None:
        return None

    return row[0]


def set_analysis_reply_message_id(message_id, reply_message_id):
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE study_log_analysis
        SET reply_message_id = ?
        WHERE message_id = ?
    """, (
        reply_message_id,
        message_id
    ))

    conn.commit()
    conn.close()


# ============================================================
# 勉強ログ削除
# ============================================================

def delete_study_log_data(message_id):
    """
    Discordの元勉強ログが削除されたとき、
    元ログとAI解析結果をSQLiteから削除する。

    戻り値:
    {
        "deleted": bool,
        "reply_message_id": int | None,
        "user_id": int | None,
        "qualification": str | None
    }
    """
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    # 削除前に関連情報を取得
    cursor.execute("""
        SELECT
            user_id,
            qualification,
            reply_message_id
        FROM study_log_analysis
        WHERE message_id = ?
    """, (
        message_id,
    ))

    analysis_row = cursor.fetchone()

    cursor.execute("""
        SELECT user_id
        FROM study_logs
        WHERE message_id = ?
    """, (
        message_id,
    ))

    log_row = cursor.fetchone()

    # DBに対象が無ければ何もしない
    if analysis_row is None and log_row is None:
        conn.close()

        return {
            "deleted": False,
            "reply_message_id": None,
            "user_id": None,
            "qualification": None
        }

    if analysis_row is not None:
        user_id = analysis_row[0]
        qualification = analysis_row[1]
        reply_message_id = analysis_row[2]

    else:
        user_id = log_row[0]
        qualification = None
        reply_message_id = None

    # 先に分野別結果と解析結果を削除
    cursor.execute("""
        DELETE FROM sg_b_practice
        WHERE message_id = ?
    """, (message_id,))

    cursor.execute("""
        DELETE FROM study_log_category_results
        WHERE message_id = ?
    """, (
        message_id,
    ))

    cursor.execute("""
        DELETE FROM study_log_analysis
        WHERE message_id = ?
    """, (
        message_id,
    ))

    # 元ログを削除
    cursor.execute("""
        DELETE FROM study_logs
        WHERE message_id = ?
    """, (
        message_id,
    ))

    conn.commit()
    conn.close()

    return {
        "deleted": True,
        "reply_message_id": reply_message_id,
        "user_id": user_id,
        "qualification": qualification
    }
