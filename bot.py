import os
import json
import random
import sqlite3
from uuid import uuid4
from collections import Counter
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from data_management import (
    FIELDS,
    KINDS,
    RESET_SCOPES,
    apply_edit,
    editable_fields,
    get_overview,
    get_record,
    list_records,
    prepare_edit,
    prepare_reset,
    reset_user_data,
)
from study_log_parser import (
    SG_CATEGORY_TO_MAJOR,
    SG_MAJOR_CATEGORIES,
    SG_PRACTICE_CATEGORIES,
    infer_correct_answers,
    normalize_study_analysis,
    parse_question_count_input,
    parse_score_percent_input,
)
from sg_features import (
    SG_B_TOPICS,
    add_sg_mistake,
    get_sg_b_summary,
    get_sg_category_progress,
    get_sg_mistakes,
    get_sg_plan_status,
    init_sg_feature_tables,
    parse_correct_count,
    record_sg_mistake_attempt,
    save_sg_b_practice,
    save_sg_plan,
    score_from_counts,
    update_sg_plan_text,
)
from sg_glossary import (
    GLOSSARY_PATH,
    SG_GLOSSARY_RATINGS,
    SOURCE_GLOSSARY_URL,
    GlossaryDataError,
    glossary_entry_key,
    init_sg_glossary_rating_table,
    load_glossary,
    get_sg_glossary_ratings,
    save_sg_glossary_rating,
    search_glossary,
    split_text,
)
from exam_schedule import (
    WEEKDAY_LABELS,
    build_exam_date,
    days_in_month,
    delete_exam_date,
    exam_year_choices,
    format_exam_countdown,
    format_japanese_date,
    format_plan_schedule,
    get_exam_date,
    get_study_streak,
    init_exam_date_table,
    save_exam_date,
    strip_week_heading_dates,
    weeks_until,
)
from sg_glossary_history import (
    GLOSSARY_HISTORY_PATH,
    init_glossary_history_db,
    record_glossary_card,
    set_glossary_summary_message_id,
)


# ============================================================
# 基本設定
# ============================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# 勉強時間を計測するVC
STUDY_VOICE_CHANNEL_NAME = "勉強部屋"

# 勉強内容を書き込むチャンネル
STUDY_LOG_CHANNEL_NAME = "勉強ログ"

# VC退出時の通知先
STUDY_NOTIFICATION_CHANNEL_NAME = "勉強ログ"

# SG用語集コマンドの利用先
SG_GLOSSARY_CHANNEL_NAME = "SG用語集"
SG_GLOSSARY_CATEGORIES = (
    "セキュリティ",
    "法務",
    "システム構成要素",
    "データベース",
    "ネットワーク",
    "プロジェクトマネジメント",
    "サービスマネジメント",
    "システム監査",
    "システム戦略",
    "システム企画",
    "企業活動",
)

# Ollama
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen3:8b"

# 日本時間
JST = ZoneInfo("Asia/Tokyo")

# DB
DB_PATH = "data/study.db"

# 分野別正答率がこの値未満なら要復習候補
REVIEW_SCORE_THRESHOLD = 60.0


# ============================================================
# Discord
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    # 標準の !help の代わりに独自の /help を使う
    help_command=None,
)

# 二重同期を避ける
_synced_guild_ids = set()


# ============================================================
# 現在勉強中のセッション（メモリ）
# key = (guild_id, user_id)
# ============================================================

study_sessions = {}


# ============================================================
# 共通
# ============================================================

def format_duration(total_seconds):
    total_seconds = max(0, int(total_seconds))

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    return f"{hours}時間{minutes}分{seconds}秒"


def parse_iso_datetime(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)

        return dt.astimezone(JST)

    except ValueError:
        return None


def format_category_label(item):
    major = item.get("major_category")
    category = item.get("category")

    if category:
        return f"{major} > {category}"

    return major or "分野不明"


def get_review_candidates(items, score_key):
    candidates = [
        item
        for item in items
        if item.get(score_key) is not None
        and item[score_key] < REVIEW_SCORE_THRESHOLD
    ]

    return sorted(
        candidates,
        key=lambda item: item[score_key]
    )


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
    os.makedirs("data", exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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

    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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



# ============================================================
# 資格ロードマップ
# ============================================================

def get_roadmap():
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
        exam_on = get_exam_date(DB_PATH, user_id, qualification)
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
            DB_PATH, user_id, today or datetime.now(JST).date()
        )
    except sqlite3.Error:
        return 0


# ============================================================
# Ollama: 勉強ログ構造化
# ============================================================

async def analyze_study_log(content):
    payload = {
        "model": OLLAMA_MODEL,
        "think": False,
        "stream": False,
        "format": "json",

        "messages": [
            {
                "role": "system",
                "content": """
あなたは資格勉強ログを構造化するシステムです。

ユーザーが実際に書いた内容だけを抽出してください。
書かれていない情報を推測してはいけません。

qualification は次のどれかにしてください。

SG
FE
医療情報技師
不明

SGの分野名は次の固定値だけを使ってください。

大分類:
- テクノロジ系
- マネジメント系
- ストラテジ系

中分類:
- セキュリティ
- 情報セキュリティ
- 情報セキュリティ管理
- セキュリティ技術評価
- 情報セキュリティ対策
- セキュリティ実装技術
- システム構成要素
- データベース
- ネットワーク
- プロジェクトマネジメント
- サービスマネジメント
- システム監査
- 法務
- システム戦略
- システム企画
- 企業活動

必ず以下のJSON形式だけを返してください。

{
  "qualification": "SG",
  "activity": "過去問道場",
  "questions": 20,
  "correct_answers": 12,
  "score_percent": null,
  "category_results": [
    {
      "major_category": "テクノロジ系",
      "category": "ネットワーク",
      "questions": null,
      "correct_answers": null,
      "score_percent": 40.0
    }
  ],
  "notes": null
}

ルール:
- 不明な値は null
- category_results がなければ []
- SGの分野は上記の固定値以外を作らない
- 大分類だけ書かれている場合、category は null
- 正答率は数値だけにする
- 問題数は実際に書かれた数だけを使う
- 正解数は実際に書かれた場合だけを使う
"""
            },
            {
                "role": "user",
                "content": content
            }
        ]
    }

    timeout = aiohttp.ClientTimeout(total=120)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            OLLAMA_URL,
            json=payload
        ) as response:

            if response.status != 200:
                error_text = await response.text()

                raise RuntimeError(
                    f"Ollama解析エラー "
                    f"{response.status}: {error_text}"
                )

            data = await response.json()
            result = data["message"]["content"]

            return json.loads(result)


# ============================================================
# 集計
# ============================================================

def get_today_total(user_id):
    today = datetime.now(JST).strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
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

    conn = sqlite3.connect(DB_PATH)
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

    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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

    conn = sqlite3.connect(DB_PATH)
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


# ============================================================
# Ollama: 学習コーチ
# ============================================================

async def ask_ollama(prompt):
    payload = {
        "model": OLLAMA_MODEL,
        "think": False,
        "stream": False,

        "messages": [
            {
                "role": "system",
                "content": (
                    "あなたは資格試験の学習コーチです。"
                    "必ず日本語で回答してください。"
                    "簡潔かつ具体的に回答してください。"
                    "記録されている事実をもとに分析してください。"
                    "記録がないことを『実施していない』と断定しないでください。"
                    "1回の低得点だけで弱点と断定しないでください。"
                    "60%未満の分野は要復習候補として扱ってください。"
                    "記録された実績とAIからの提案を区別してください。"
                    "勉強量を過度に褒めず、次に取るべき行動を明確にしてください。"
                    "Discordで読みやすい形式にしてください。"
                    "回答は1200文字以内にしてください。"
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
    }

    timeout = aiohttp.ClientTimeout(
        total=180
    )

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:

        async with session.post(
            OLLAMA_URL,
            json=payload
        ) as response:

            if response.status != 200:
                error_text = (
                    await response.text()
                )

                raise RuntimeError(
                    f"Ollamaエラー "
                    f"{response.status}: "
                    f"{error_text}"
                )

            data = await response.json()

            return data[
                "message"
            ]["content"]


# ============================================================
# 勉強ログ返信生成
# ============================================================

def build_analysis_reply(
    analysis,
    status_data,
    edited=False
):
    qualification = (
        analysis.get("qualification")
        or "不明"
    )

    activity = analysis.get("activity")
    questions = analysis.get("questions")
    correct_answers = analysis.get(
        "correct_answers"
    )
    score_percent = analysis.get(
        "score_percent"
    )

    category_results = (
        analysis.get("category_results")
        or []
    )

    notes = analysis.get("notes")
    analysis_warnings = (
        analysis.get("analysis_warnings")
        or []
    )

    activity_text = (
        activity
        if activity
        else "記録なし"
    )

    questions_text = (
        f"{questions}問"
        if questions is not None
        else "記録なし"
    )

    score_text = (
        f"{score_percent:g}%"
        if score_percent is not None
        else "記録なし"
    )

    if (
        correct_answers is not None
        and questions is not None
    ):
        correct_text = (
            f"{correct_answers}/{questions}問"
        )
    else:
        correct_text = "記録なし"

    category_lines = []

    for result in category_results:
        major = result.get("major_category")
        category = result.get("category")
        category_score = result.get(
            "score_percent"
        )

        if not major:
            continue

        label = (
            f"{major} > {category}"
            if category
            else major
        )

        if category_score is not None:
            label += f"（{category_score:g}%）"

        category_lines.append(label)

    category_text = (
        "、".join(category_lines)
        if category_lines
        else "記録なし"
    )

    review_candidates = get_review_candidates(
        category_results,
        "score_percent"
    )
    review_text = (
        "、".join(
            f"{format_category_label(item)}"
            f"（{item['score_percent']:g}%）"
            for item in review_candidates
        )
        if review_candidates
        else "なし"
    )

    if edited:
        title = (
            "🔄 **編集内容を再解析して"
            "勉強ログを更新しました！**"
        )
    else:
        title = (
            "✅ **勉強ログを"
            "記録しました！**"
        )

    reply_lines = [
        title,
        "",
        f"📘 資格：**{qualification}**",
        f"📝 内容：**{activity_text}**",
        f"🔢 問題数：**{questions_text}**",
        f"⭕ 正解数：**{correct_text}**",
        f"🎯 正答率：**{score_text}**",
        f"📚 分野：**{category_text}**",
        f"🔁 要復習候補：**{review_text}**",
    ]

    if notes:
        reply_lines.append(
            f"💬 メモ：{notes}"
        )

    if analysis_warnings:
        reply_lines.extend([
            "",
            "⚠️ **入力内容を確認してください**",
            *(
                f"- {warning}"
                for warning in analysis_warnings
            ),
        ])

    if (
        qualification != "不明"
        and status_data is not None
    ):
        cumulative_questions = (
            status_data["total_questions"]
        )

        cumulative_average = (
            status_data["average_score"]
        )

        cumulative_categories = (
            status_data.get("category_status", [])
        )

        if cumulative_average is not None:
            cumulative_score_text = (
                f"{cumulative_average:.1f}%"
            )
        else:
            cumulative_score_text = (
                "記録なし"
            )

        cumulative_review_candidates = (
            get_review_candidates(
                cumulative_categories,
                "average_score"
            )
        )

        if cumulative_review_candidates:
            cumulative_review_text = "、".join(
                f"{format_category_label(item)}"
                f"（{item['average_score']:.1f}% / "
                f"{item['scored_log_count']}回）"
                for item in cumulative_review_candidates[:3]
            )
        else:
            cumulative_review_text = "なし"

        reply_lines.extend([
            "",
            f"📊 **{qualification} 累計**",
            (
                f"🔢 問題数："
                f"**{cumulative_questions}問**"
            ),
            (
                f"🎯 平均正答率："
                f"**{cumulative_score_text}**"
            ),
            (
                f"🔁 要復習候補："
                f"**{cumulative_review_text}**"
            ),
        ])

    return "\n".join(reply_lines)


def build_structured_sg_analysis(
    category,
    questions,
    score_percent,
    notes=None
):
    major_category = SG_CATEGORY_TO_MAJOR[category]
    correct_answers = infer_correct_answers(
        questions,
        score_percent
    )

    return {
        "qualification": "SG",
        "activity": "過去問道場",
        "exam_section": "A",
        "questions": questions,
        "correct_answers": correct_answers,
        "score_percent": score_percent,
        "category_results": [
            {
                "major_category": major_category,
                "category": category,
                "questions": questions,
                "correct_answers": correct_answers,
                "score_percent": score_percent,
            }
        ],
        "weak_points": [],
        "notes": notes.strip() if notes and notes.strip() else None,
        "analysis_warnings": [],
    }


class SGStudyLogModal(
    discord.ui.Modal,
    title="SG過去問道場ログ"
):
    questions_input = discord.ui.TextInput(
        label="解いた問題数",
        placeholder="例：25",
        required=True,
        min_length=1,
        max_length=4
    )
    score_input = discord.ui.TextInput(
        label="正答率（%）",
        placeholder="例：40 または 40.25",
        required=True,
        min_length=1,
        max_length=7
    )
    notes_input = discord.ui.TextInput(
        label="メモ（任意）",
        placeholder="気になった用語や次回見直す内容",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300
    )

    def __init__(self, category, target_channel):
        super().__init__()
        self.category = category
        self.target_channel = target_channel

    async def on_submit(self, interaction):
        try:
            questions = parse_question_count_input(
                self.questions_input.value
            )
            score_percent = parse_score_percent_input(
                self.score_input.value
            )
        except ValueError as error:
            await interaction.response.send_message(
                f"⚠️ {error}",
                ephemeral=True
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "⚠️ SGログはサーバー内で入力してください。",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        log_message = None

        try:
            log_message = await self.target_channel.send(
                "SG勉強ログを記録しています..."
            )
            major_category = SG_CATEGORY_TO_MAJOR[
                self.category
            ]
            score_text = f"{score_percent:.1f}"
            content = (
                f"SG過去問道場{questions}問。"
                f"正答率{score_text}%。"
                f"全て{major_category}の"
                f"{self.category}分野。"
            )
            message_for_db = SimpleNamespace(
                id=log_message.id,
                guild=interaction.guild,
                channel=self.target_channel,
                author=interaction.user,
                created_at=log_message.created_at,
                content=content
            )
            analysis = build_structured_sg_analysis(
                self.category,
                questions,
                score_percent,
                self.notes_input.value
            )

            save_study_log(message_for_db)
            save_study_analysis(
                message_for_db,
                analysis
            )
            status_data = get_study_status(
                interaction.user.id,
                "SG"
            )
            reply_text = build_analysis_reply(
                analysis,
                status_data
            )

            await log_message.edit(
                content=reply_text,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await interaction.followup.send(
                f"記録しました：{self.category} / "
                f"{questions}問 / {score_text}%",
                ephemeral=True
            )

        except Exception as error:
            print(f"❌ sglog 保存エラー: {error}")

            if log_message is not None:
                try:
                    await log_message.edit(
                        content=(
                            "⚠️ SG勉強ログの保存に失敗しました。"
                        )
                    )
                except discord.HTTPException:
                    pass

            await interaction.followup.send(
                "⚠️ SG勉強ログを保存できませんでした。\n"
                "VS Codeのターミナルを確認してください。",
                ephemeral=True
            )


class SGCategorySelect(discord.ui.Select):
    def __init__(self, action_label="問題数と正答率を入力"):
        self.action_label = action_label
        security_categories = set(
            SG_PRACTICE_CATEGORIES[:5]
        )
        options = [
            discord.SelectOption(
                label=category,
                value=category,
                description=(
                    "セキュリティ"
                    if category in security_categories
                    else "その他分野"
                )
            )
            for category in SG_PRACTICE_CATEGORIES
        ]

        super().__init__(
            placeholder="学習した分野を1つ選択",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction):
        self.view.selected_category = self.values[0]

        await interaction.response.edit_message(
            content=(
                f"選択中：**{self.values[0]}**\n"
                f"「{self.action_label}」を押してください。"
            ),
            view=self.view
        )


class SGStudyLogView(discord.ui.View):
    def __init__(self, owner_id, target_channel):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.target_channel = target_channel
        self.selected_category = None
        self.add_item(SGCategorySelect())

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            "この入力画面はコマンドを実行した本人専用です。",
            ephemeral=True
        )
        return False

    @discord.ui.button(
        label="問題数と正答率を入力",
        style=discord.ButtonStyle.primary
    )
    async def open_modal(self, interaction, button):
        if self.selected_category is None:
            await interaction.response.send_message(
                "先に学習した分野を選択してください。",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            SGStudyLogModal(
                self.selected_category,
                self.target_channel
            )
        )


class SGMistakeModal(discord.ui.Modal, title="SG誤答を登録"):
    reference_input = discord.ui.TextInput(
        label="問題のURLまたは番号",
        placeholder="例：https://... または 令和6年 問12",
        max_length=200,
    )
    reason_input = discord.ui.TextInput(
        label="間違えた理由",
        placeholder="例：アクセス制御の条件を読み違えた",
        style=discord.TextStyle.paragraph,
        max_length=300,
    )
    memo_input = discord.ui.TextInput(
        label="次回確認すること（任意）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, category):
        super().__init__()
        self.category = category

    async def on_submit(self, interaction):
        try:
            mistake_id, due = add_sg_mistake(
                DB_PATH,
                interaction.user.id,
                self.category,
                self.reference_input.value,
                self.reason_input.value,
                self.memo_input.value,
                today=datetime.now(JST).date(),
            )
        except ValueError as error:
            await interaction.response.send_message(
                str(error), ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"誤答 #{mistake_id} を登録しました。"
            f"次の復習日：{due.isoformat()}\n"
            "復習するときは `/review list` を開いてください。",
            ephemeral=True,
        )


class SGMistakeView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.selected_category = None
        self.add_item(SGCategorySelect("誤答を入力"))

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この画面はコマンドを実行した本人専用です。",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="誤答を入力", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction, button):
        if self.selected_category is None:
            await interaction.response.send_message(
                "先に分野を選択してください。", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            SGMistakeModal(self.selected_category)
        )


def build_structured_sg_b_analysis(
    topic, questions, correct_answers, wrong_reason=None, memo=None
):
    score = score_from_counts(correct_answers, questions)
    note_parts = []
    if wrong_reason and wrong_reason.strip():
        note_parts.append(f"判断ミス：{wrong_reason.strip()}")
    if memo and memo.strip():
        note_parts.append(memo.strip())
    return {
        "qualification": "SG",
        "activity": "科目B演習",
        "exam_section": "B",
        "questions": questions,
        "correct_answers": correct_answers,
        "score_percent": score,
        "category_results": [{
            "major_category": "科目B",
            "category": topic,
            "questions": questions,
            "correct_answers": correct_answers,
            "score_percent": score,
        }],
        "weak_points": [],
        "notes": " / ".join(note_parts) or None,
        "analysis_warnings": [],
    }


class SGBPracticeModal(discord.ui.Modal, title="SG科目Bの演習結果"):
    questions_input = discord.ui.TextInput(
        label="解いた問題数", placeholder="例：5", max_length=4
    )
    correct_input = discord.ui.TextInput(
        label="正解数", placeholder="例：3", max_length=4
    )
    reason_input = discord.ui.TextInput(
        label="判断を間違えた理由（全問正解なら空欄）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )
    memo_input = discord.ui.TextInput(
        label="メモ（任意）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, topic, target_channel):
        super().__init__()
        self.topic = topic
        self.target_channel = target_channel

    async def on_submit(self, interaction):
        try:
            questions = parse_question_count_input(
                self.questions_input.value
            )
            correct = parse_correct_count(
                self.correct_input.value, questions
            )
            reason = (self.reason_input.value or "").strip()
            if correct < questions and not reason:
                raise ValueError("誤答がある場合は判断を間違えた理由を入力してください。")
        except ValueError as error:
            await interaction.response.send_message(
                str(error), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        log_message = None
        saved = False
        try:
            log_message = await self.target_channel.send(
                "SG科目Bの演習結果を記録しています..."
            )
            analysis = build_structured_sg_b_analysis(
                self.topic, questions, correct,
                reason, self.memo_input.value,
            )
            message_for_db = SimpleNamespace(
                id=log_message.id,
                guild=interaction.guild,
                channel=self.target_channel,
                author=interaction.user,
                created_at=log_message.created_at,
                content=(
                    f"SG科目B {self.topic}を{questions}問中"
                    f"{correct}問正解。"
                ),
            )
            save_study_log(message_for_db)
            save_study_analysis(message_for_db, analysis)
            save_sg_b_practice(
                DB_PATH, log_message.id, interaction.user.id,
                self.topic, questions, correct, reason,
                self.memo_input.value,
                log_message.created_at.astimezone(JST).date().isoformat(),
            )
            saved = True
            status_data = get_study_status(interaction.user.id, "SG")
            await log_message.edit(
                content=build_analysis_reply(analysis, status_data),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await interaction.followup.send(
                f"科目Bを記録しました：{self.topic} / "
                f"{correct}/{questions}問正解 "
                f"({analysis['score_percent']:.1f}%)",
                ephemeral=True,
            )
        except Exception as error:
            print(f"SG科目B 保存エラー: {error}")
            if saved:
                await interaction.followup.send(
                    "科目BはDBに記録済みですが、Discord表示の更新に"
                    "失敗しました。`/sg progress` で確認してください。",
                    ephemeral=True,
                )
                return
            if log_message is not None:
                delete_study_log_data(log_message.id)
                try:
                    await log_message.edit(
                        content="科目Bの保存に失敗しました。"
                    )
                except discord.HTTPException:
                    pass
            await interaction.followup.send(
                "科目Bを保存できませんでした。実行ログを確認してください。",
                ephemeral=True,
            )


class SGBTopicSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="科目Bのテーマを選択",
            options=[
                discord.SelectOption(label=topic, value=topic)
                for topic in SG_B_TOPICS
            ],
        )

    async def callback(self, interaction):
        self.view.selected_topic = self.values[0]
        await interaction.response.edit_message(
            content=(
                f"選択中：**{self.values[0]}**\n"
                "「演習結果を入力」を押してください。"
            ),
            view=self.view,
        )


class SGBPracticeView(discord.ui.View):
    def __init__(self, owner_id, target_channel):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.target_channel = target_channel
        self.selected_topic = None
        self.add_item(SGBTopicSelect())

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この画面はコマンドを実行した本人専用です。",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="演習結果を入力", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction, button):
        if self.selected_topic is None:
            await interaction.response.send_message(
                "先にテーマを選択してください。", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            SGBPracticeModal(self.selected_topic, self.target_channel)
        )


# ============================================================
# 勉強ログを保存 → AI解析 → Discord返信
# 新規投稿と編集の両方で使う
# ============================================================

async def process_study_log_message(
    message,
    edited=False
):
    save_study_log(message)

    if edited:
        print(
            f"✏️ 勉強ログ編集: "
            f"{message.author.display_name}\n"
            f"{message.content}"
        )
    else:
        print(
            f"📝 勉強ログ保存: "
            f"{message.author.display_name}\n"
            f"{message.content}"
        )

    try:
        print("🤖 Ollamaで解析中...")

        raw_analysis = await analyze_study_log(
            message.content
        )

        current_qualification = (
            get_current_qualification()
        )
        current_qualification_code = (
            current_qualification["qualification"]
            if current_qualification
            else "SG"
        )
        analysis = normalize_study_analysis(
            message.content,
            raw_analysis,
            current_qualification=(
                current_qualification_code
            )
        )

        # 先に解析結果を保存
        save_study_analysis(
            message,
            analysis
        )

        qualification = (
            analysis.get("qualification")
            or "不明"
        )

        status_data = None

        if qualification != "不明":
            status_data = get_study_status(
                message.author.id,
                qualification
            )

        reply_text = build_analysis_reply(
            analysis,
            status_data,
            edited=edited
        )

        existing_reply_id = (
            get_analysis_reply_message_id(
                message.id
            )
        )

        reply_message = None

        # 既存返信があれば編集
        if existing_reply_id:
            try:
                reply_message = (
                    await message.channel.fetch_message(
                        existing_reply_id
                    )
                )

                await reply_message.edit(
                    content=reply_text
                )

            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException
            ):
                reply_message = None

        # 返信が無い・消された場合は新規作成
        if reply_message is None:
            reply_message = await message.reply(
                reply_text,
                mention_author=False
            )

            set_analysis_reply_message_id(
                message.id,
                reply_message.id
            )

        print("✅ 勉強ログ解析完了")
        print(
            json.dumps(
                analysis,
                ensure_ascii=False,
                indent=2
            )
        )

    except Exception as e:
        print(
            f"❌ 勉強ログ解析エラー: {e}"
        )

        # 編集失敗時は古い解析返信を消さない。
        # 新規投稿時のみエラーを返す。
        if not edited:
            await message.reply(
                "⚠️ 勉強ログ自体は保存しましたが、"
                "AI解析または解析結果の返信に失敗しました。\n"
                "VS Codeのターミナルを確認してください。",
                mention_author=False
            )


# ============================================================
# 起動時の進行中VCセッション復元
# ============================================================

async def recover_active_voice_sessions():
    """
    進行中セッションをSQLiteから復元する。

    ・再起動前からVCにいて、DBにactiveがある
      → 元の開始時刻を復元

    ・Bot停止中にVCへ入っていてactiveが無い
      → 正確な入室時刻が分からないため、
        Bot起動時刻から計測開始

    ・activeはあるが現在VCにいない
      → 正確な退出時刻が不明なので、
        過大計上を避けてactive記録を破棄
    """
    now = datetime.now(JST)

    currently_in_study_vc = set()

    for guild in bot.guilds:
        for channel in guild.voice_channels:

            if (
                channel.name
                != STUDY_VOICE_CHANNEL_NAME
            ):
                continue

            for member in channel.members:

                if member.bot:
                    continue

                key = (
                    guild.id,
                    member.id
                )

                currently_in_study_vc.add(
                    key
                )

                db_start = (
                    get_active_study_session(
                        guild.id,
                        member.id
                    )
                )

                if db_start is not None:
                    study_sessions[key] = (
                        db_start
                    )

                    print(
                        "♻️ 進行中セッション復元: "
                        f"{member.display_name} "
                        f"{db_start.strftime('%Y-%m-%d %H:%M:%S')}"
                    )

                else:
                    # Bot停止中に入室した可能性あり。
                    # 正確な時刻は分からないので起動時から。
                    study_sessions[key] = now

                    save_active_study_session(
                        guild.id,
                        member.id,
                        member.display_name,
                        now
                    )

                    print(
                        "🆕 起動時に勉強VC滞在を検出: "
                        f"{member.display_name}\n"
                        "正確な入室時刻が取得できないため、"
                        "Bot起動時刻から計測します。"
                    )

    # DBにはactiveがあるのに現在VCにいない
    # → Bot停止中に退出した可能性があり終了時刻不明。
    for (
        guild_id,
        user_id,
        username,
        start_time_str
    ) in get_all_active_study_sessions():

        key = (
            guild_id,
            user_id
        )

        if key not in currently_in_study_vc:
            delete_active_study_session(
                guild_id,
                user_id
            )

            study_sessions.pop(
                key,
                None
            )

            print(
                "⚠️ 復旧不能な進行中セッションを破棄: "
                f"{username}\n"
                f"開始記録: {start_time_str}\n"
                "Bot停止中に退出した可能性があり、"
                "正確な終了時刻を判断できないため"
                "過大計上を避けました。"
            )


# ============================================================
# Bot起動・Slash Command同期
# ============================================================

@bot.event
async def on_ready():
    print("--------------------")
    print("StudyBot 起動完了！")
    print(f"ログイン中: {bot.user}")
    print(f"AIモデル: {OLLAMA_MODEL}")
    print("StudyBot Version: 2.7")
    print("--------------------")

    # /コマンドを各参加サーバーへ同期
    for guild in bot.guilds:
        if guild.id in _synced_guild_ids:
            continue

        try:
            guild_obj = discord.Object(
                id=guild.id
            )

            # Hybrid Commandを即時反映しやすい
            # Guild Commandとしてコピー
            bot.tree.copy_global_to(
                guild=guild_obj
            )

            synced = await bot.tree.sync(
                guild=guild_obj
            )

            _synced_guild_ids.add(
                guild.id
            )

            print(
                f"✅ /コマンド同期: "
                f"{guild.name} "
                f"({len(synced)}件)"
            )

        except Exception as e:
            print(
                f"⚠️ /コマンド同期失敗 "
                f"{guild.name}: {e}"
            )

    await recover_active_voice_sessions()


# ============================================================
# 勉強ログ新規投稿
# ============================================================

@bot.listen("on_message")
async def save_study_message(message):

    if message.author.bot:
        return

    if message.guild is None:
        return

    if (
        message.channel.name
        != STUDY_LOG_CHANNEL_NAME
    ):
        return

    if not message.content.strip():
        return

    # コマンドは勉強ログ扱いしない
    if message.content.startswith("!"):
        return

    await process_study_log_message(
        message,
        edited=False
    )


# ============================================================
# 勉強ログ編集
# ============================================================

@bot.listen("on_message_edit")
async def update_study_message(
    before,
    after
):
    if after.author.bot:
        return

    if after.guild is None:
        return

    if (
        after.channel.name
        != STUDY_LOG_CHANNEL_NAME
    ):
        return

    if before.content == after.content:
        return

    if not after.content.strip():
        return

    if after.content.startswith("!"):
        return

    await process_study_log_message(
        after,
        edited=True
    )



# ============================================================
# 勉強ログ削除
# Discordの元投稿を削除したらDBと解析返信も同期削除
# ============================================================

@bot.listen("on_raw_message_delete")
async def delete_study_message(payload):
    # DMは対象外
    if payload.guild_id is None:
        return

    result = delete_study_log_data(
        payload.message_id
    )

    # DB上の勉強ログではなければ何もしない
    # Bot自身の解析返信が削除された場合などもここで無視される
    if not result["deleted"]:
        return

    print(
        "🗑️ 勉強ログ削除を検出\n"
        f"message_id: {payload.message_id}"
    )

    reply_message_id = result[
        "reply_message_id"
    ]

    # 元ログに対応するStudyBotの解析返信も削除
    if reply_message_id is not None:
        channel = bot.get_channel(
            payload.channel_id
        )

        if channel is None:
            try:
                channel = await bot.fetch_channel(
                    payload.channel_id
                )
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException
            ):
                channel = None

        if channel is not None:
            try:
                reply_message = (
                    await channel.fetch_message(
                        reply_message_id
                    )
                )

                await reply_message.delete()

                print(
                    "🧹 対応するStudyBotの"
                    "解析返信も削除しました"
                )

            except discord.NotFound:
                # すでに返信が削除されている
                pass

            except (
                discord.Forbidden,
                discord.HTTPException
            ) as e:
                print(
                    "⚠️ 解析返信の削除に失敗: "
                    f"{e}"
                )

    qualification = result[
        "qualification"
    ]

    user_id = result[
        "user_id"
    ]

    # 削除後の累計をコンソールに表示
    if (
        qualification
        and qualification != "不明"
        and user_id is not None
    ):
        status_data = get_study_status(
            user_id,
            qualification
        )

        average_score = status_data[
            "average_score"
        ]

        if average_score is not None:
            score_text = (
                f"{average_score:.1f}%"
            )
        else:
            score_text = "記録なし"

        print(
            "✅ DBから元ログ＋解析結果を"
            "削除しました\n"
            f"📊 {qualification} 更新後累計: "
            f"{status_data['total_questions']}問 / "
            f"{score_text}"
        )

    else:
        print(
            "✅ DBから元ログ＋解析結果を"
            "削除しました"
        )


# ============================================================
# Hybrid Commands
# !xxx と /xxx の両方で使える
# ============================================================

# ============================================================
# コマンドグループと /help
# ============================================================

# /help に表示する順番
HELP_COMMAND_ORDER = ("sg", "review", "time", "plan", "ai", "data", "help")
HELP_SUBCOMMAND_ORDER = {
    "sg": ("log", "b", "progress", "status", "glossary"),
    "review": ("add", "list", "answer"),
    "time": ("today", "week", "logs"),
    "plan": ("new", "status", "exam", "roadmap"),
    "ai": ("today", "next", "report"),
}


def _ordered_subcommands(group):
    order = HELP_SUBCOMMAND_ORDER.get(group.name, ())
    return sorted(
        group.commands,
        key=lambda sub: (
            order.index(sub.name) if sub.name in order else len(order)
        ),
    )


def build_help_text():
    commands_by_name = {
        command.name: command
        for command in bot.tree.get_commands()
    }
    lines = [
        "**StudyBot の使い方**",
        f"ボイスチャンネル「{STUDY_VOICE_CHANNEL_NAME}」に入ると勉強時間を自動で記録します。"
        "結果は `/sg log`、間違えた問題は `/review add` で登録してください。",
    ]
    for name in HELP_COMMAND_ORDER:
        command = commands_by_name.get(name)
        if command is None:
            continue
        lines.append(f"\n**/{name}**　{command.description}")
        if getattr(command, "commands", None):
            lines.extend(
                f"　`/{name} {sub.name}`　{sub.description}"
                for sub in _ordered_subcommands(command)
            )
    return "\n".join(lines)


async def send_group_help(ctx):
    """サブコマンドなしで !sg などが呼ばれたときの案内。"""
    group = ctx.command
    lines = [f"**/{group.name}**　{group.description}"]
    lines.extend(
        f"　`/{group.name} {sub.name}`　{sub.description}"
        for sub in _ordered_subcommands(group)
    )
    await ctx.send("\n".join(lines))


@bot.hybrid_group(
    name="sg",
    description="SGの記録・進捗・用語集",
    invoke_without_command=True,
)
async def sg_group(ctx):
    await send_group_help(ctx)


@bot.hybrid_group(
    name="review",
    description="間違えた問題の復習",
    invoke_without_command=True,
)
async def review_group(ctx):
    await send_group_help(ctx)


@bot.hybrid_group(
    name="time",
    description="勉強時間とログ",
    invoke_without_command=True,
)
async def time_group(ctx):
    await send_group_help(ctx)


@bot.hybrid_group(
    name="plan",
    description="学習計画・試験日・ロードマップ",
    invoke_without_command=True,
)
async def plan_group(ctx):
    await send_group_help(ctx)


@bot.hybrid_group(
    name="ai",
    description="AIコーチによる分析と提案",
    invoke_without_command=True,
)
async def ai_group(ctx):
    await send_group_help(ctx)


@bot.hybrid_command(
    name="help",
    description="StudyBotのコマンド一覧と使い方"
)
async def help_command(ctx):
    await ctx.send(build_help_text(), ephemeral=True)


@time_group.command(
    name="today",
    description="今日の勉強時間と連続学習日数を表示"
)
async def today(ctx):
    total_seconds = get_today_total(
        ctx.author.id
    )

    await ctx.send(
        "📚 **今日の勉強時間**\n"
        f"{format_duration(total_seconds)}\n"
        f"🔥 連続学習：**{get_study_streak_safe(ctx.author.id)}日**"
    )


@time_group.command(
    name="week",
    description="今週の勉強時間を曜日別に表示"
)
async def week(ctx):
    rows = get_week_total(
        ctx.author.id
    )

    if not rows:
        await ctx.send(
            "📅 今週はまだ勉強時間の"
            "記録がありません。"
        )
        return

    total_seconds = sum(
        seconds
        for _, seconds
        in rows
    )

    day_names = {
        0: "月",
        1: "火",
        2: "水",
        3: "木",
        4: "金",
        5: "土",
        6: "日"
    }

    lines = []

    for (
        study_date,
        seconds
    ) in rows:

        date_obj = datetime.strptime(
            study_date,
            "%Y-%m-%d"
        )

        weekday = day_names[
            date_obj.weekday()
        ]

        lines.append(
            f"**{weekday}曜日**："
            f"{format_duration(seconds)}"
        )

    detail_text = "\n".join(
        lines
    )

    await ctx.send(
        "📅 **今週の勉強時間**\n\n"
        f"{detail_text}\n\n"
        "⏱️ **合計："
        f"{format_duration(total_seconds)}**"
    )


class DataOwnerView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=300)
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "このデータ画面は実行した本人専用です。", ephemeral=True
        )
        return False


def _data_record_label(kind, row):
    if kind == "log":
        section = "科目B" if row["section"] == "B" else "科目A/旧形式"
        score = row["score_percent"]
        score_text = f"{score:g}%" if score is not None else "正答率なし"
        questions = (
            f"{row['questions']}問" if row["questions"] is not None
            else "問題数なし"
        )
        return f"{row['day']} {section} {questions} {score_text}"
    if kind == "session":
        return f"{row['day']} {format_duration(row['duration_seconds'])}"
    if kind == "mistake":
        state = "完了" if row["completed_on"] else "復習中"
        return f"{row['category']} / {row['question_ref'][:45]} [{state}]"
    return (
        f"{row['day']}開始 {row['weeks']}週 "
        f"{row['weekly_questions']}問/週"
    )


def _data_home_text(user_id):
    overview = get_overview(DB_PATH, user_id)
    sg = get_study_status(user_id, "SG")
    score = sg["average_score"]
    score_text = f"{score:.1f}%" if score is not None else "記録なし"
    lines = [
        "**保存データ一覧（自分の記録）**",
        f"SG：{overview['logs']['sg'] or 0}件 / "
        f"{sg['total_questions']}問 / 平均{score_text}",
        f"うち科目B：{overview['logs']['b'] or 0}件 / "
        f"その他・未解析ログ："
        f"{overview['logs']['total'] - (overview['logs']['sg'] or 0)}件",
        f"通話勉強：{overview['sessions']['total']}回 / "
        f"{format_duration(overview['sessions']['seconds'])}",
        f"復習問題：{overview['mistakes']['total']}件 "
        f"（未完了{overview['mistakes']['open_count'] or 0}件）",
        f"SG単語帳の自己評価：{overview['glossary_ratings']['total']}語",
        f"週次計画：{overview['plans']['total']}件 "
        f"（有効{overview['plans']['active_count'] or 0}件）",
    ]
    for kind, title, limit in (
        ("log", "最近のSGログ", 3),
        ("session", "最近の通話", 3),
        ("mistake", "最近の誤答", 2),
        ("plan", "最近の計画", 2),
    ):
        records, _ = list_records(DB_PATH, user_id, kind, page_size=limit)
        if records:
            lines.extend(["", f"**{title}**"])
            lines.extend(f"- {_data_record_label(kind, row)}" for row in records)
    lines.extend([
        "", "修正・リセットはDB内の記録に反映します。",
        "元のDiscord投稿は変更されません。",
    ])
    return "\n".join(lines)


class DataHomeView(DataOwnerView):
    @discord.ui.button(label="修正", style=discord.ButtonStyle.primary)
    async def edit(self, interaction, button):
        await interaction.response.edit_message(
            content="修正するデータの種類を選択してください。",
            view=DataKindView(self.owner_id),
        )

    @discord.ui.button(label="リセット", style=discord.ButtonStyle.danger)
    async def reset(self, interaction, button):
        await interaction.response.edit_message(
            content="リセットする範囲を選択してください。DB内の記録だけを削除します。",
            view=DataResetScopeView(self.owner_id),
        )

    @discord.ui.button(label="そのまま", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction, button):
        await interaction.response.edit_message(
            content="変更せず終了しました。", view=None,
        )


class DataKindSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="修正するデータを選択",
            options=[
                discord.SelectOption(label=label, value=kind)
                for kind, label in KINDS.items()
            ],
        )

    async def callback(self, interaction):
        kind = self.values[0]
        view = DataRecordListView(self.view.owner_id, kind)
        await interaction.response.edit_message(
            content=view.description, view=view,
        )


class DataKindView(DataOwnerView):
    def __init__(self, owner_id):
        super().__init__(owner_id)
        self.add_item(DataKindSelect())

    @discord.ui.button(label="一覧へ戻る", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=_data_home_text(self.owner_id),
            view=DataHomeView(self.owner_id),
        )


class DataRecordSelect(discord.ui.Select):
    def __init__(self, kind, rows):
        super().__init__(
            placeholder="修正する記録を選択",
            options=[
                discord.SelectOption(
                    label=_data_record_label(kind, row)[:100],
                    value=str(row["id"]),
                    description=f"記録ID: {row['id']}",
                )
                for row in rows
            ],
        )

    async def callback(self, interaction):
        view = DataFieldView(
            self.view.owner_id, self.view.kind, int(self.values[0]),
        )
        await interaction.response.edit_message(
            content=view.description, view=view,
        )


class DataRecordListView(DataOwnerView):
    def __init__(self, owner_id, kind, page=0):
        super().__init__(owner_id)
        self.kind = kind
        self.page = page
        rows, has_next = list_records(DB_PATH, owner_id, kind, page)
        self.description = (
            f"**{KINDS[kind]}**（{page + 1}ページ目）\n"
            "修正する記録を選択してください。"
            if rows else f"**{KINDS[kind]}**の記録はありません。"
        )
        if rows:
            self.add_item(DataRecordSelect(kind, rows))
        self.previous.disabled = page == 0
        self.next_page.disabled = not has_next

    @discord.ui.button(label="前へ", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction, button):
        view = DataRecordListView(self.owner_id, self.kind, self.page - 1)
        await interaction.response.edit_message(content=view.description, view=view)

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction, button):
        view = DataRecordListView(self.owner_id, self.kind, self.page + 1)
        await interaction.response.edit_message(content=view.description, view=view)

    @discord.ui.button(label="戻る", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content="修正するデータの種類を選択してください。",
            view=DataKindView(self.owner_id),
        )


def _data_record_description(kind, record):
    if record is None:
        return "記録が見つかりません。"
    if kind == "log":
        categories = "、".join(
            row["category"] or row["major_category"]
            for row in record["categories"]
        ) or "未分類"
        score = record["score_percent"]
        score_text = f"{score:g}%" if score is not None else "記録なし"
        return (
            f"**SGログ {record['day']}**\n"
            f"分野：{categories}\n問題数：{record['questions']}問 / "
            f"正解数：{record['correct_answers']} / 正答率：{score_text}\n"
            "修正する項目を選択してください。"
        )
    if kind == "session":
        return (
            f"**通話勉強時間 {record['day']}**\n"
            f"開始：{record['start_time']}\n"
            f"終了：{record['end_time']}\n"
            f"記録時間：{format_duration(record['duration_seconds'])}\n"
            "修正する項目を選択してください。"
        )
    if kind == "mistake":
        state = "完了" if record["completed_on"] else "復習中"
        return (
            f"**誤答 #{record['id']} [{state}]**\n"
            f"分野：{record['category']}\n"
            f"問題：{record['question_ref']}\n"
            f"理由：{record['reason']}\n"
            f"メモ：{record['memo'] or 'なし'}\n"
            "修正する項目を選択してください。"
        )[:1800]
    return (
        f"**週次計画 #{record['id']}**\n"
        f"開始：{record['start_on']} / {record['weeks']}週間\n"
        f"基本目標：{record['weekly_questions']}問/週\n"
        f"状態：{'有効' if record['active'] else '過去の計画'}\n"
        "修正する項目を選択してください。"
    )


class DataFieldSelect(discord.ui.Select):
    def __init__(self, record, kind):
        super().__init__(
            placeholder="修正する項目を選択",
            options=[
                discord.SelectOption(label=label, value=field)
                for field, label in editable_fields(record, kind).items()
            ],
        )

    async def callback(self, interaction):
        view = self.view
        field = self.values[0]
        if field == "category":
            choices = (
                SG_B_TOPICS
                if view.kind == "log" and view.record["exam_section"] == "B"
                else SG_PRACTICE_CATEGORIES
            )
            await interaction.response.edit_message(
                content="正しい分野・テーマを選択してください。",
                view=DataChoiceValueView(
                    view.owner_id, view.kind, view.record_id, field, choices,
                ),
            )
        else:
            await interaction.response.send_modal(
                DataEditModal(view.owner_id, view.kind, view.record_id, field)
            )


class DataFieldView(DataOwnerView):
    def __init__(self, owner_id, kind, record_id):
        super().__init__(owner_id)
        self.kind = kind
        self.record_id = record_id
        self.record = get_record(DB_PATH, owner_id, kind, record_id)
        self.description = _data_record_description(kind, self.record)
        if self.record is not None:
            self.add_item(DataFieldSelect(self.record, kind))

    @discord.ui.button(label="記録一覧へ", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        view = DataRecordListView(self.owner_id, self.kind)
        await interaction.response.edit_message(
            content=view.description, view=view,
        )


class DataChoiceSelect(discord.ui.Select):
    def __init__(self, choices):
        super().__init__(
            placeholder="分野を選択",
            options=[discord.SelectOption(label=item, value=item) for item in choices],
        )

    async def callback(self, interaction):
        view = self.view
        try:
            preview = prepare_edit(
                DB_PATH, view.owner_id, view.kind, view.record_id,
                view.field, self.values[0],
            )
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.edit_message(
            content=_edit_preview_text(preview),
            view=DataConfirmEditView(
                view.owner_id, view.kind, view.record_id,
                view.field, preview,
            ),
        )


class DataChoiceValueView(DataOwnerView):
    def __init__(self, owner_id, kind, record_id, field, choices):
        super().__init__(owner_id)
        self.kind = kind
        self.record_id = record_id
        self.field = field
        self.add_item(DataChoiceSelect(choices))

    @discord.ui.button(label="項目選択へ", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        view = DataFieldView(self.owner_id, self.kind, self.record_id)
        await interaction.response.edit_message(content=view.description, view=view)


def _edit_preview_text(preview):
    return (
        f"**変更前後の確認：{preview['field_label']}**\n"
        f"変更前：{preview['before']}\n"
        f"変更後：{preview['after']}\n\n"
        "DBの記録をこの内容へ修正しますか？"
    )


class DataEditModal(discord.ui.Modal):
    def __init__(self, owner_id, kind, record_id, field):
        label = FIELDS[kind][field]
        super().__init__(title=f"修正：{label}")
        self.owner_id = owner_id
        self.kind = kind
        self.record_id = record_id
        self.field = field
        self.input = discord.ui.TextInput(
            label=label,
            placeholder="開始日はYYYY-MM-DD形式" if field == "start_on" else None,
            style=(discord.TextStyle.paragraph if field in (
                "notes", "reason", "wrong_reason", "memo"
            ) else discord.TextStyle.short),
            required=field not in ("notes", "memo", "wrong_reason"),
            max_length=500,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "この入力画面は実行した本人専用です。", ephemeral=True,
            )
            return
        try:
            preview = prepare_edit(
                DB_PATH, self.owner_id, self.kind, self.record_id,
                self.field, self.input.value,
            )
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.send_message(
            _edit_preview_text(preview),
            view=DataConfirmEditView(
                self.owner_id, self.kind, self.record_id,
                self.field, preview,
            ),
            ephemeral=True,
        )


class DataConfirmEditView(DataOwnerView):
    def __init__(self, owner_id, kind, record_id, field, preview):
        super().__init__(owner_id)
        self.kind = kind
        self.record_id = record_id
        self.field = field
        self.preview = preview

    @discord.ui.button(label="修正を確定", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        try:
            apply_edit(
                DB_PATH, self.owner_id, self.kind, self.record_id,
                self.field, self.preview["value"], self.preview["record"],
            )
        except ValueError as error:
            await interaction.response.edit_message(content=str(error), view=None)
            return
        sg_text = ""
        if self.kind == "log":
            status = get_study_status(self.owner_id, "SG")
            score = status["average_score"]
            sg_text = (
                f"\nSG累計：{status['total_questions']}問 / "
                f"平均{score:.1f}%" if score is not None else
                f"\nSG累計：{status['total_questions']}問 / 正答率なし"
            )
        await interaction.response.edit_message(
            content=f"DBの記録を修正しました。{sg_text}", view=None,
        )

    @discord.ui.button(label="変更しない", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content="変更せず終了しました。", view=None,
        )


class DataResetScopeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="リセットする範囲を選択",
            options=[
                discord.SelectOption(label=label, value=scope)
                for scope, label in RESET_SCOPES.items()
            ],
        )

    async def callback(self, interaction):
        scope = self.values[0]
        try:
            ids = prepare_reset(DB_PATH, self.view.owner_id, scope)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        count = sum(len(items) for items in ids.values())
        await interaction.response.edit_message(
            content=(
                f"**リセットの最終確認**\n"
                f"対象：{RESET_SCOPES[scope]}\n"
                f"削除する記録：{count}件\n"
                "この操作は取り消せません。元のDiscord投稿は残ります。\n"
                "本当にDB内の記録を削除しますか？"
            ),
            view=DataConfirmResetView(self.view.owner_id, scope, ids),
        )


class DataResetScopeView(DataOwnerView):
    def __init__(self, owner_id):
        super().__init__(owner_id)
        self.add_item(DataResetScopeSelect())

    @discord.ui.button(label="一覧へ戻る", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=_data_home_text(self.owner_id),
            view=DataHomeView(self.owner_id),
        )


class DataConfirmResetView(DataOwnerView):
    def __init__(self, owner_id, scope, ids):
        super().__init__(owner_id)
        self.scope = scope
        self.ids = ids

    @discord.ui.button(label="リセットを確定", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        try:
            count = reset_user_data(
                DB_PATH, self.owner_id, self.scope, self.ids,
            )
        except ValueError as error:
            await interaction.response.edit_message(content=str(error), view=None)
            return
        await interaction.response.edit_message(
            content=f"DB内の{RESET_SCOPES[self.scope]}をリセットしました（{count}件）。",
            view=None,
        )

    @discord.ui.button(label="変更しない", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content="変更せず終了しました。", view=None,
        )


@bot.tree.command(name="data", description="自分のDB記録を確認・修正・リセット")
async def data_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        _data_home_text(interaction.user.id),
        view=DataHomeView(interaction.user.id),
        ephemeral=True,
    )


def _safe_glossary_text(value):
    return discord.utils.escape_markdown(
        discord.utils.escape_mentions(value)
    )


def _sg_glossary_rating_label(rating):
    return "要復習" if rating == "まだ要復習" else rating


def _sg_glossary_list_pages(entries):
    """Return bounded message bodies and their first entry positions."""
    pages = []
    sections = []
    size = 0
    first_entry = 0
    # Leave room for the heading, a search query, and the source URL.
    body_limit = 1300

    for index, entry in enumerate(entries):
        title = f"**{_safe_glossary_text(entry.term)}**"
        category = " / ".join(filter(None, (entry.category, entry.subcategory)))
        if category:
            title += f" · {_safe_glossary_text(category)}"
        meaning = (
            _safe_glossary_text(entry.meaning)
            if entry.meaning else "意味未登録"
        )
        block = f"{title}\n{meaning}"
        for part in split_text(block, body_limit):
            added = len(part) + (2 if sections else 0)
            if sections and size + added > body_limit:
                pages.append(("\n\n".join(sections), first_entry))
                sections = []
                size = 0
            if not sections:
                first_entry = index
            sections.append(part)
            size += len(part) + (2 if len(sections) > 1 else 0)

    if sections:
        pages.append(("\n\n".join(sections), first_entry))
    return pages


def _sg_glossary_source_url(entry):
    url = entry.source_url or SOURCE_GLOSSARY_URL
    try:
        parsed = urlsplit(url)
    except ValueError:
        return SOURCE_GLOSSARY_URL
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or len(url) > 350
        or any(character.isspace() or character in "<>" for character in url)
    ):
        return SOURCE_GLOSSARY_URL
    return url


def _sg_glossary_session_summary_text(summary, category, query):
    started_at = datetime.fromisoformat(summary["started_at"]).astimezone(JST)
    category_name = "全分野" if category == "all" else _safe_glossary_text(category)
    query_note = (
        f" / 検索: {_safe_glossary_text(query[:100])}"
        if query and query.strip() else ""
    )
    counts = summary["counts"]
    return (
        "**SG単語帳の学習記録**\n"
        f"学習者: <@{summary['user_id']}>\n"
        f"開始: {started_at:%Y-%m-%d %H:%M}\n"
        f"分野: {category_name}{query_note}\n"
        f"合計 **{summary['total']}問**\n"
        f"できた: {counts['できた']}問 / "
        f"できなかった: {counts['できなかった']}問 / "
        f"要復習: {counts['まだ要復習']}問 / "
        f"微妙: {counts['微妙']}問"
    )


class SGGlossaryView(discord.ui.View):
    def __init__(
        self, owner_id, entries, mode="list", category="all", query=None,
        ratings=None, record_channel=None, guild_id=None,
        history_db_path=GLOSSARY_HISTORY_PATH,
    ):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.all_entries = tuple(entries)
        self.entries = self.all_entries
        self.mode = mode
        self.category = category
        self.query = query or ""
        self.list_pages = _sg_glossary_list_pages(self.entries)
        self.page_index = 0
        self.card_index = 0
        self.meaning_page = 0
        self.revealed = False
        self.ratings = dict(ratings or {})
        self.rating_filter = ()
        self.last_rating_feedback = ""
        self.record_channel = record_channel
        self.guild_id = guild_id
        self.history_db_path = history_db_path
        self.session_id = uuid4().hex
        self.summary_message = None
        self._update_buttons()

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この用語集画面はコマンドを実行した本人専用です。",
            ephemeral=True,
        )
        return False

    def _meaning_parts(self):
        entry = self.entries[self.card_index]
        meaning = (
            _safe_glossary_text(entry.meaning)
            if entry.meaning else "意味は未登録です。"
        )
        return split_text(meaning, 1300)

    def _update_buttons(self):
        rating_buttons = (
            self.rated_yes, self.rated_no,
            self.rated_review, self.rated_unsure,
        )
        for rating_button in rating_buttons:
            if self.mode == "cards":
                if rating_button not in self.children:
                    self.add_item(rating_button)
                rating_button.disabled = not self.revealed
            elif rating_button in self.children:
                self.remove_item(rating_button)

        if self.mode == "cards":
            if self.rating_filter_select not in self.children:
                self.add_item(self.rating_filter_select)
        elif self.rating_filter_select in self.children:
            self.remove_item(self.rating_filter_select)
        for option in self.rating_filter_select.options:
            option.default = option.value in self.rating_filter
        if self.rating_filter:
            if self.clear_rating_filter not in self.children:
                self.add_item(self.clear_rating_filter)
        elif self.clear_rating_filter in self.children:
            self.remove_item(self.clear_rating_filter)

        if self.mode == "list":
            self.previous.disabled = self.page_index == 0
            self.next_page.disabled = self.page_index >= len(self.list_pages) - 1
            self.reveal.disabled = True
            self.reveal.label = "意味を見る"
            self.shuffle.disabled = True
            self.switch_mode.label = "単語帳へ"
        else:
            parts = self._meaning_parts()
            self.previous.disabled = (
                self.card_index == 0
                and (not self.revealed or self.meaning_page == 0)
            )
            self.next_page.disabled = (
                self.card_index >= len(self.entries) - 1
                and (not self.revealed or self.meaning_page >= len(parts) - 1)
            )
            self.reveal.disabled = False
            self.reveal.label = "意味を隠す" if self.revealed else "意味を見る"
            self.shuffle.disabled = len(self.entries) < 2
            self.switch_mode.label = "一覧へ"

    def content(self):
        count = len(self.entries)
        category_name = (
            "全分野" if self.category == "all"
            else _safe_glossary_text(self.category)
        )
        query_note = (
            f" / 検索: {_safe_glossary_text(self.query[:100])}"
            if self.query.strip() else ""
        )
        filter_note = (
            " / 評価（選択時）: " + "・".join(
                _sg_glossary_rating_label(rating)
                for rating in self.rating_filter
            )
            if self.rating_filter else ""
        )
        if self.mode == "list":
            body, _ = self.list_pages[self.page_index]
            sources = {_sg_glossary_source_url(entry) for entry in self.entries}
            source_label = (
                "用語出典" if all(entry.source_url for entry in self.entries)
                else "参考サイト"
            )
            source_note = (
                f"\n\n{source_label}: <{next(iter(sources))}>"
                if len(sources) == 1 else "\n\n用語出典は各カードに表示"
            )
            return (
                f"**SG用語集・一覧**（分野: {category_name}{query_note}"
                f"{filter_note} / {count}件） "
                f"{self.page_index + 1}/{len(self.list_pages)}ページ\n\n"
                f"{body}{source_note}"
            )

        entry = self.entries[self.card_index]
        category = " / ".join(filter(None, (entry.category, entry.subcategory)))
        category_label = _safe_glossary_text(category) if category else "未登録"
        category_quote = "\n".join(
            f"> {line}" for line in category_label.splitlines()
        )
        heading = (
            f"**SG単語帳**（対象: {category_name}{query_note}{filter_note} / "
            f"{self.card_index + 1}/{count}件）\n\n"
            f"**{_safe_glossary_text(entry.term)}**\n"
            f"{category_quote}"
        )
        feedback = (
            f"\n{self.last_rating_feedback}"
            if self.last_rating_feedback else ""
        )
        if not self.revealed:
            return f"{heading}{feedback}\n\n「意味を見る」を押してください。"

        parts = self._meaning_parts()
        continuation = (
            f"（意味 {self.meaning_page + 1}/{len(parts)}）\n"
            if len(parts) > 1 else ""
        )
        source = _sg_glossary_source_url(entry)
        source_label = "用語出典" if entry.source_url else "参考サイト"
        current_rating = self.ratings.get(glossary_entry_key(entry))
        rating_note = (
            f"\n自己評価: {_sg_glossary_rating_label(current_rating)}"
            if current_rating else "\n自己評価: 未評価"
        )
        return (
            f"{heading}{feedback}\n\n{continuation}{parts[self.meaning_page]}\n\n"
            f"{source_label}: <{source}>{rating_note}"
        )

    async def _refresh(self, interaction):
        self._update_buttons()
        await interaction.response.edit_message(
            content=self.content(), view=self,
        )

    async def _apply_rating_filter(self, interaction, selected_ratings):
        selected = set(selected_ratings)
        if not selected.issubset(SG_GLOSSARY_RATINGS):
            await interaction.response.send_message(
                "自己評価は表示された4つから選んでください。",
                ephemeral=True,
            )
            return
        entries = (
            tuple(entry for entry in self.all_entries
                  if self.ratings.get(glossary_entry_key(entry)) in selected)
            if selected else self.all_entries
        )
        if not entries:
            await interaction.response.send_message(
                "選んだ自己評価に該当する単語はありません。別の評価を選んでください。",
                ephemeral=True,
            )
            return
        self.rating_filter = tuple(
            rating for rating in SG_GLOSSARY_RATINGS if rating in selected
        )
        self.entries = entries
        self.list_pages = _sg_glossary_list_pages(entries)
        self.page_index = 0
        self.card_index = 0
        self.meaning_page = 0
        self.revealed = False
        self.last_rating_feedback = ""
        await self._refresh(interaction)

    @discord.ui.select(
        placeholder="自己評価で絞る（複数選択可）",
        min_values=1, max_values=4, row=2,
        options=[
            discord.SelectOption(label="できた", value="できた"),
            discord.SelectOption(label="できなかった", value="できなかった"),
            discord.SelectOption(label="要復習", value="まだ要復習"),
            discord.SelectOption(label="微妙", value="微妙"),
        ],
    )
    async def rating_filter_select(self, interaction, select):
        await self._apply_rating_filter(interaction, select.values)

    @discord.ui.button(label="絞り込み解除", style=discord.ButtonStyle.secondary, row=3)
    async def clear_rating_filter(self, interaction, button):
        await self._apply_rating_filter(interaction, ())

    @discord.ui.button(label="前へ", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.last_rating_feedback = ""
        if self.mode == "list":
            self.page_index -= 1
        elif self.revealed and self.meaning_page > 0:
            self.meaning_page -= 1
        else:
            self.card_index -= 1
            self.meaning_page = 0
            self.revealed = False
        await self._refresh(interaction)

    @discord.ui.button(label="意味を見る", style=discord.ButtonStyle.primary)
    async def reveal(self, interaction, button):
        self.last_rating_feedback = ""
        self.revealed = not self.revealed
        self.meaning_page = 0
        await self._refresh(interaction)

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        self.last_rating_feedback = ""
        if self.mode == "list":
            self.page_index += 1
        elif self.revealed and self.meaning_page < len(self._meaning_parts()) - 1:
            self.meaning_page += 1
        else:
            self.card_index += 1
            self.meaning_page = 0
            self.revealed = False
        await self._refresh(interaction)

    @discord.ui.button(label="シャッフル", style=discord.ButtonStyle.secondary)
    async def shuffle(self, interaction, button):
        self.last_rating_feedback = ""
        # Pick a different word so a tap always changes the card.
        target = random.randrange(len(self.entries) - 1)
        if target >= self.card_index:
            target += 1
        self.card_index = target
        self.meaning_page = 0
        self.revealed = False
        await self._refresh(interaction)

    @discord.ui.button(label="単語帳へ", style=discord.ButtonStyle.success)
    async def switch_mode(self, interaction, button):
        self.last_rating_feedback = ""
        if self.mode == "list":
            self.card_index = self.list_pages[self.page_index][1]
            self.mode = "cards"
        else:
            exact_pages = [
                index for index, (_, first_entry) in enumerate(self.list_pages)
                if first_entry == self.card_index
            ]
            if exact_pages:
                self.page_index = exact_pages[0]
            else:
                self.page_index = max(
                    index for index, (_, first_entry) in enumerate(self.list_pages)
                    if first_entry < self.card_index
                )
            self.mode = "list"
        self.meaning_page = 0
        self.revealed = False
        await self._refresh(interaction)

    async def _rate(self, interaction, rating):
        if self.mode != "cards" or not self.revealed:
            await interaction.response.send_message(
                "意味を表示してから自己評価を選んでください。",
                ephemeral=True,
            )
            return
        entry = self.entries[self.card_index]
        try:
            save_sg_glossary_rating(DB_PATH, self.owner_id, entry, rating)
        except (OSError, sqlite3.Error) as error:
            await interaction.response.send_message(
                f"自己評価を保存できませんでした: {error}",
                ephemeral=True,
            )
            return
        summary = None
        if self.record_channel is not None:
            try:
                summary = record_glossary_card(
                    self.history_db_path, self.session_id,
                    self.guild_id, self.record_channel.id,
                    self.owner_id, entry, rating,
                )
            except (OSError, sqlite3.Error, ValueError) as error:
                self.ratings[glossary_entry_key(entry)] = rating
                await interaction.response.send_message(
                    f"自己評価は保存しましたが、単語帳の学習記録を保存できませんでした: {error}"
                    " もう一度評価を選んでください。",
                    ephemeral=True,
                )
                return
        self.ratings[glossary_entry_key(entry)] = rating
        self.last_rating_feedback = (
            f"自己評価「{_sg_glossary_rating_label(rating)}」を記録しました。"
        )
        if self.card_index < len(self.entries) - 1:
            self.card_index += 1
            self.revealed = False
            self.meaning_page = 0
        await self._refresh(interaction)
        if summary is not None:
            try:
                await self._publish_session_summary(summary)
            except (discord.HTTPException, OSError, sqlite3.Error) as error:
                await interaction.followup.send(
                    "学習記録は専用DBに保存しましたが、チャンネルへの投稿に"
                    f"失敗しました: {error} 次の評価時に再試行します。",
                    ephemeral=True,
                )

    async def _publish_session_summary(self, summary):
        text = _sg_glossary_session_summary_text(
            summary, self.category, self.query,
        )
        message_id = summary["summary_message_id"]
        if self.summary_message is None and message_id is not None:
            try:
                self.summary_message = await self.record_channel.fetch_message(
                    message_id,
                )
            except discord.NotFound:
                pass
        allowed_mentions = discord.AllowedMentions.none()
        if self.summary_message is None:
            self.summary_message = await self.record_channel.send(
                text, allowed_mentions=allowed_mentions,
            )
        else:
            try:
                await self.summary_message.edit(
                    content=text, allowed_mentions=allowed_mentions,
                )
            except discord.NotFound:
                self.summary_message = await self.record_channel.send(
                    text, allowed_mentions=allowed_mentions,
                )
        if message_id != self.summary_message.id:
            set_glossary_summary_message_id(
                self.history_db_path, self.session_id,
                self.summary_message.id,
            )

    @discord.ui.button(label="できた", style=discord.ButtonStyle.success, row=1)
    async def rated_yes(self, interaction, button):
        await self._rate(interaction, "できた")

    @discord.ui.button(label="できなかった", style=discord.ButtonStyle.danger, row=1)
    async def rated_no(self, interaction, button):
        await self._rate(interaction, "できなかった")

    @discord.ui.button(label="要復習", style=discord.ButtonStyle.primary, row=1)
    async def rated_review(self, interaction, button):
        await self._rate(interaction, "まだ要復習")

    @discord.ui.button(label="微妙", style=discord.ButtonStyle.secondary, row=1)
    async def rated_unsure(self, interaction, button):
        await self._rate(interaction, "微妙")


@sg_group.command(
    name="glossary",
    description="SG用語集を一覧または単語帳で見る"
)
@app_commands.describe(
    mode="表示方法",
    category="表示する分野",
    query="用語・意味・分野・小分類を検索",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="一覧", value="list"),
        app_commands.Choice(name="単語帳", value="cards"),
    ],
    category=[
        app_commands.Choice(name="全分野", value="all"),
        *(
            app_commands.Choice(name=name, value=name)
            for name in SG_GLOSSARY_CATEGORIES
        ),
    ],
)
async def sgglossary(
    ctx, mode: str = "list", category: str = "all", query: str | None = None,
):
    is_interaction = getattr(ctx, "interaction", None) is not None

    async def reply(message, view=None):
        kwargs = {"view": view} if view is not None else {}
        if is_interaction:
            kwargs["ephemeral"] = True
        await ctx.send(message, **kwargs)

    if (
        ctx.guild is None
        or getattr(ctx.channel, "name", "").casefold()
        != SG_GLOSSARY_CHANNEL_NAME.casefold()
    ):
        await reply(f"このコマンドは #{SG_GLOSSARY_CHANNEL_NAME} で使ってください。")
        return

    normalized_mode = {"一覧": "list", "単語帳": "cards"}.get(mode, mode)
    if normalized_mode not in ("list", "cards"):
        await reply("表示方法は「一覧」または「単語帳」を選んでください。")
        return
    if category != "all" and category not in SG_GLOSSARY_CATEGORIES:
        await reply("分野は候補から選んでください。")
        return
    if query and len(query) > 100:
        await reply("検索語は100文字以内で入力してください。")
        return

    try:
        all_entries = load_glossary(GLOSSARY_PATH)
    except FileNotFoundError:
        await reply(
            "SG用語データがありません。`data/sg_glossary.json` を配置してください。\n"
            f"参照先: <{SOURCE_GLOSSARY_URL}>"
        )
        return
    except (OSError, GlossaryDataError) as error:
        await reply(f"SG用語データを読み込めません: {error}")
        return

    if not all_entries:
        await reply("SG用語データは空です。`data/sg_glossary.json` に用語を追加してください。")
        return
    entries = (
        all_entries if category == "all"
        else [entry for entry in all_entries if entry.category == category]
    )
    if not entries:
        await reply("選んだ分野の用語がありません。別の分野を選んでください。")
        return

    entries = search_glossary(entries, query)
    if not entries:
        await reply("一致する用語がありません。分野や検索語を変えて試してください。")
        return

    try:
        ratings = get_sg_glossary_ratings(DB_PATH, ctx.author.id, entries)
    except (OSError, sqlite3.Error) as error:
        await reply(f"自己評価を読み込めません: {error}")
        return
    view = SGGlossaryView(
        ctx.author.id, entries, normalized_mode, category, query,
        ratings=ratings, record_channel=ctx.channel,
        guild_id=ctx.guild.id,
        history_db_path=GLOSSARY_HISTORY_PATH,
    )
    await reply(view.content(), view=view)


@sg_group.command(
    name="log",
    description="SG過去問道場の結果を記録（分野・問題数・正答率）"
)
async def sglog(ctx):
    is_interaction = getattr(ctx, "interaction", None) is not None

    if ctx.guild is None:
        message = "SGログはサーバー内で入力してください。"
        if is_interaction:
            await ctx.send(message, ephemeral=True)
        else:
            await ctx.send(message)
        return

    target_channel = discord.utils.get(
        ctx.guild.text_channels,
        name=STUDY_LOG_CHANNEL_NAME
    )

    if target_channel is None:
        message = (
            f"#{STUDY_LOG_CHANNEL_NAME} チャンネルが"
            "見つかりません。"
        )
        if is_interaction:
            await ctx.send(message, ephemeral=True)
        else:
            await ctx.send(message)
        return

    view = SGStudyLogView(
        ctx.author.id,
        target_channel
    )
    message = (
        "学習した分野を1つ選択してください。\n"
        "続けて問題数と正答率を数字で入力します。\n"
        "正答率は小数第2位を四捨五入し、"
        "小数第1位で保存します。\n"
        "複数分野は分野ごとに登録してください。"
    )

    if is_interaction:
        await ctx.send(
            message,
            view=view,
            ephemeral=True
        )
    else:
        await ctx.send(message, view=view)


@review_group.command(
    name="add",
    description="間違えた問題を復習リストへ登録"
)
async def mistake(ctx):
    view = SGMistakeView(ctx.author.id)
    message = "間違えた問題の分野を選択してください。"
    if getattr(ctx, "interaction", None) is not None:
        await ctx.send(message, view=view, ephemeral=True)
    else:
        await ctx.send(message, view=view)


@review_group.command(
    name="list",
    description="今日までの復習予定を表示"
)
@app_commands.describe(all_items="今日以降の予定も表示する")
async def reviews(ctx, all_items: bool = False):
    items = get_sg_mistakes(
        DB_PATH, ctx.author.id,
        today=datetime.now(JST).date(), due_only=not all_items,
    )
    if not items:
        message = (
            "未完了の復習はありません。"
            if all_items else
            "今日までに復習する問題はありません。"
            " `/review list all_items:true` で今後の予定を見られます。"
        )
        await ctx.send(message)
        return

    lines = [
        "**SG 復習リスト**",
        f"対象：{len(items)}件",
    ]
    for item in items[:10]:
        reference = discord.utils.escape_mentions(item["question_ref"])
        reason = discord.utils.escape_markdown(
            discord.utils.escape_mentions(item["reason"][:80])
        )
        memo = item["memo"] or ""
        lines.append(
            f"**#{item['id']}** {item['category']} "
            f"（{item['next_review_on']}）\n"
            f"{reference}\n理由：{reason}"
            + (
                "\nメモ：" + discord.utils.escape_markdown(
                    discord.utils.escape_mentions(memo[:60])
                )
                if memo else ""
            )
        )
    if len(items) > 10:
        lines.append(f"ほか{len(items) - 10}件")
    lines.append("再挑戦後は `/review answer` で結果を登録。")
    await ctx.send("\n\n".join(lines)[:1900])


@review_group.command(
    name="answer",
    description="復習で解き直した結果（正解・不正解）を登録"
)
@app_commands.describe(
    mistake_id="復習リストに表示されるID",
    result="今回の再挑戦結果",
)
@app_commands.choices(result=[
    app_commands.Choice(name="正解", value="correct"),
    app_commands.Choice(name="不正解", value="wrong"),
])
async def review(ctx, mistake_id: int, result: str):
    try:
        outcome = record_sg_mistake_attempt(
            DB_PATH, ctx.author.id, mistake_id, result,
            today=datetime.now(JST).date(),
        )
    except ValueError as error:
        await ctx.send(str(error))
        return

    if outcome["completed"]:
        await ctx.send(
            f"復習 #{mistake_id} は3回連続正解で完了しました。"
        )
    else:
        await ctx.send(
            f"復習 #{mistake_id} を記録しました。"
            f"連続正解：{outcome['streak']}/3。"
            f"次回：{outcome['next_review_on'].isoformat()}"
        )


@sg_group.command(
    name="progress",
    description="SGの14分野と科目Bの進捗を表示"
)
async def sgprogress(ctx):
    items, unclassified = get_sg_category_progress(
        DB_PATH, ctx.author.id
    )
    b_summary = get_sg_b_summary(DB_PATH, ctx.author.id)
    lines = ["**SG 科目A・14分野の進捗**", "**セキュリティ**"]
    for index, item in enumerate(items):
        if index == 5:
            lines.append("\n**その他分野**")
        questions = item["questions"]
        score = item["latest_score"]
        last_date = item["last_study_date"] or "—"
        if questions == 0:
            if last_date != "—":
                score_text = (
                    f"{score:.1f}%" if score is not None else "未記録"
                )
                detail = f"問題数未記録 / 直近{score_text} / {last_date}"
            else:
                detail = "未着手"
        else:
            score_text = f"{score:.1f}%" if score is not None else "未記録"
            state = "記録少" if questions < 10 else (
                "要復習" if score is not None and score < 60 else "学習中"
            )
            detail = f"{questions}問 / 直近{score_text} / {last_date} / {state}"
        lines.append(f"{item['category']}：{detail}")
    if unclassified:
        lines.append(f"\n旧ログなど分野未特定：{unclassified}問")

    lines.append("\n**科目B**")
    if b_summary["questions"]:
        lines.append(
            f"{b_summary['questions']}問中"
            f"{b_summary['correct_answers']}問正解 "
            f"({b_summary['score_percent']:.1f}%)"
        )
        for topic, questions, correct, reason, practiced_on in (
            b_summary["recent_sessions"]
        ):
            line = f"{practiced_on} {topic}：{correct}/{questions}問"
            if reason:
                safe_reason = discord.utils.escape_markdown(
                    discord.utils.escape_mentions(reason[:80])
                )
                line += f" / 判断ミス：{safe_reason}"
            lines.append(line)
    else:
        lines.append("まだ記録なし")

    await ctx.send("\n".join(lines)[:1900])


@sg_group.command(
    name="b",
    description="SG科目Bの演習結果を記録"
)
async def sgb(ctx):
    if ctx.guild is None:
        await ctx.send("サーバー内で入力してください。")
        return
    target_channel = discord.utils.get(
        ctx.guild.text_channels, name=STUDY_LOG_CHANNEL_NAME
    )
    if target_channel is None:
        await ctx.send(f"#{STUDY_LOG_CHANNEL_NAME} が見つかりません。")
        return

    view = SGBPracticeView(ctx.author.id, target_channel)
    message = "科目Bで取り組んだケースのテーマを選択してください。"
    if getattr(ctx, "interaction", None) is not None:
        await ctx.send(message, view=view, ephemeral=True)
    else:
        await ctx.send(message, view=view)


@time_group.command(
    name="logs",
    description="今日の勉強ログを表示"
)
async def logs(ctx):
    logs_data = get_today_logs(
        ctx.author.id
    )

    if not logs_data:
        await ctx.send(
            "📝 今日はまだ勉強ログが"
            "ありません。"
        )
        return

    text = "\n".join(
        f"・{log}"
        for log in logs_data
    )

    # Discord 2000文字制限を軽く回避
    if len(text) > 1700:
        text = (
            text[:1700]
            + "\n…（以下省略）"
        )

    await ctx.send(
        "📝 **今日の勉強ログ**\n"
        f"{text}"
    )



@plan_group.command(
    name="roadmap",
    description="資格取得ロードマップを表示"
)
async def roadmap(ctx):
    rows = get_roadmap()

    if not rows:
        await ctx.send(
            "🗺️ ロードマップがまだ登録されていません。"
        )
        return

    status_labels = {
        "learning": "🟢 学習中",
        "pending": "⚪ 未着手",
        "completed": "✅ 合格済み",
        "paused": "⏸️ 保留"
    }

    lines = []
    current_name = None

    for (
        qualification,
        display_name,
        sort_order,
        status,
        is_current
    ) in rows:

        label = status_labels.get(
            status,
            f"⚪ {status}"
        )

        current_mark = ""

        if is_current:
            current_mark = " ← **現在の最優先**"
            current_name = display_name

        lines.append(
            f"**{sort_order}. {display_name}**\n"
            f"　{label}{current_mark}"
        )

    roadmap_text = "\n\n".join(lines)

    if current_name:
        footer = (
            f"\n\n🎯 現在の最優先："
            f"**{current_name}**"
        )
    else:
        footer = (
            "\n\n🎯 現在の最優先資格は"
            "設定されていません。"
        )

    countdown = get_exam_countdown_line(ctx.author.id)
    if countdown:
        footer += f"\n📆 {countdown}"
    elif current_name:
        footer += "\n📆 試験日は `/plan exam` で設定できます。"

    await ctx.send(
        "🗺️ **資格取得ロードマップ**\n\n"
        f"{roadmap_text}"
        f"{footer}"
    )


@sg_group.command(
    name="status",
    description="SGの累積学習状況を表示"
)
async def status(ctx):
    qualification = "SG"

    status_data = get_study_status(
        ctx.author.id,
        qualification
    )

    total_questions = (
        status_data["total_questions"]
    )

    average_score = (
        status_data["average_score"]
    )

    category_status = (
        status_data["category_status"]
    )

    log_count = (
        status_data["log_count"]
    )

    if log_count == 0:
        await ctx.send(
            f"📊 **{qualification} 学習状況**\n"
            "まだ解析済みの勉強ログが"
            "ありません。"
        )
        return

    if average_score is not None:
        score_text = (
            f"{average_score:.1f}%"
        )
    else:
        score_text = "記録なし"

    if category_status:
        category_lines = []

        for item in category_status:
            label = format_category_label(item)

            if item["average_score"] is not None:
                value = (
                    f"{item['average_score']:.1f}%"
                )
            else:
                value = (
                    f"記録{item['log_count']}回"
                )

            category_lines.append(
                f"- {label}：{value}"
            )

        category_text = "\n".join(category_lines)
    else:
        category_text = "まだ記録なし"

    review_candidates = get_review_candidates(
        category_status,
        "average_score"
    )

    if review_candidates:
        review_text = "\n".join(
            f"- {format_category_label(item)}："
            f"{item['average_score']:.1f}% "
            f"（{'重点復習' if item['scored_log_count'] >= 2 else '候補'}）"
            for item in review_candidates
        )
    else:
        review_text = "現在はなし"

    await ctx.send(
        f"📊 **{qualification} 学習状況**\n\n"
        f"📝 解析済みログ：{log_count}件\n"
        f"🔢 解いた問題：{total_questions}問\n"
        f"🎯 平均正答率：{score_text}\n\n"
        "📚 **分野別成績**\n"
        f"{category_text}\n\n"
        "🔁 **要復習候補**\n"
        f"{review_text}"
    )


@ai_group.command(
    name="next",
    description="次回の勉強メニューをAIが提案"
)
async def next_study(ctx):
    qualification = "SG"

    total_seconds = get_today_total(
        ctx.author.id
    )

    logs_data = get_today_logs(
        ctx.author.id
    )

    if logs_data:
        log_text = "\n".join(
            f"- {log}"
            for log in logs_data
        )
    else:
        log_text = (
            "今日はまだ勉強ログが"
            "ありません。"
        )

    status_data = get_study_status(
        ctx.author.id,
        qualification
    )

    total_questions = (
        status_data["total_questions"]
    )

    average_score = (
        status_data["average_score"]
    )

    category_status = (
        status_data["category_status"]
    )

    if average_score is not None:
        score_text = (
            f"{average_score:.1f}%"
        )
    else:
        score_text = "記録なし"

    review_candidates = get_review_candidates(
        category_status,
        "average_score"
    )

    if review_candidates:
        review_text = "\n".join(
            f"- {format_category_label(item)}："
            f"{item['average_score']:.1f}%"
            for item in review_candidates
        )
    else:
        review_text = (
            "現在はなし"
        )

    prompt = f"""
次回の勉強メニューを作ってください。

【現在の資格】
情報セキュリティマネジメント（SG）

【今日の勉強時間】
{format_duration(total_seconds)}

【今日の勉強ログ】
{log_text}

【これまでに解いた問題数】
{total_questions}問

【問題数で重み付けした平均正答率】
{score_text}

【分野別正答率から見た要復習候補】
{review_text}

重要:
- 1回の結果だけで弱点と断定しない
- 60%未満の分野は要復習候補として扱う
- 記録のない実績を作らない
- ユーザーは過去問道場を中心に勉強している

次回の勉強について、
具体的なメニューを3項目以内で提案してください。

各項目には、
・何をするか
・何問程度やるか
・その理由
を含めてください。

全体で30分前後を目安にしてください。
"""

    async with ctx.typing():
        try:
            answer = await ask_ollama(
                prompt
            )

            await ctx.send(
                "🎯 **次の勉強メニュー**\n\n"
                f"{answer}"
            )

        except Exception as e:
            print(
                f"❌ next エラー: {e}"
            )

            await ctx.send(
                "⚠️ 次の勉強メニューを"
                "作成できませんでした。\n"
                "VS Codeのターミナルを"
                "確認してください。"
            )


@plan_group.command(
    name="new",
    description="SG合格までの週次学習計画を作成"
)
@app_commands.describe(
    weeks="試験までの残り週数（1〜16週）。省略すると/plan examの試験日から計算",
    weekly_questions="1週間の目標問題数（省略時30問）",
)
async def plan(
    ctx, weeks: int | None = None, weekly_questions: int = 30
):
    today_date = datetime.now(JST).date()
    exam_on = get_current_exam_target(ctx.author.id)["exam_on"]
    if exam_on is not None and exam_on < today_date:
        exam_on = None

    plan_note = ""
    if weeks is None:
        if exam_on is None:
            await ctx.send(
                "試験日が未設定です。`/plan exam` で試験日を選ぶか、"
                "`/plan new weeks:6` のように残り週数を指定してください。"
            )
            return
        weeks = weeks_until(exam_on, today_date)
        if weeks > 16:
            plan_note = (
                f"試験日まで{weeks}週ありますが、"
                "計画は直近16週分で作ります。\n"
            )
            weeks = 16

    if not 1 <= weeks <= 16:
        await ctx.send(
            "⚠️ 残り週数は1〜16週で指定してください。\n"
            "例：`/plan new weeks:6` または `!plan new 6`"
        )
        return

    if not 1 <= weekly_questions <= 500:
        await ctx.send("週目標は1〜500問で指定してください。")
        return

    qualification = "SG"
    status_data = get_study_status(
        ctx.author.id,
        qualification
    )
    total_questions = status_data["total_questions"]
    average_score = status_data["average_score"]
    category_status = status_data["category_status"]
    week_seconds = get_week_total_seconds(
        ctx.author.id
    )

    score_text = (
        f"{average_score:.1f}%"
        if average_score is not None
        else "記録なし"
    )

    category_text = (
        "\n".join(
            f"- {format_category_label(item)}："
            + (
                f"{item['average_score']:.1f}% "
                f"（採点{item['scored_log_count']}回）"
                if item["average_score"] is not None
                else f"正答率なし（記録{item['log_count']}回）"
            )
            for item in category_status
        )
        if category_status
        else "記録なし"
    )

    review_candidates = get_review_candidates(
        category_status,
        "average_score"
    )
    review_text = (
        "\n".join(
            f"- {format_category_label(item)}："
            f"{item['average_score']:.1f}%"
            for item in review_candidates
        )
        if review_candidates
        else "現在はなし"
    )

    exam_text = (
        f"\n【試験日】{format_japanese_date(exam_on)}"
        f"（あと{(exam_on - today_date).days}日）\n"
        if exam_on is not None
        else ""
    )
    schedule_text = format_plan_schedule(today_date, weeks, exam_on)

    prompt = f"""
情報セキュリティマネジメント（SG）を
残り{weeks}週間で合格するための学習計画を作成してください。
{exam_text}
【各週の期間（Botが計算済み）】
{schedule_text}

【学習方法】
情報セキュリティマネジメント過去問道場を中心に学習する。
毎週の基本目標は{weekly_questions}問。
未達の場合、翌週は不足分のうち最大{weekly_questions // 2}問を上乗せする。

【現在の記録】
- 累計問題数：{total_questions}問
- 平均正答率：{score_text}
- 今週ここまでの勉強時間：{format_duration(week_seconds)}

【分野別成績】
{category_text}

【要復習候補（60%未満）】
{review_text}

ルール:
- 第1週から第{weeks}週まで、各週を必ず分ける
- 週の見出しは「第1週」のように番号だけにし、日付や曜日は書かない
- 各週に「基本目標{weekly_questions}問」「学習内容」「確認ポイント」を書く
- 翌週の上乗せは実績確定後にBotが計算するので、将来の実績を推測しない
- 過去問道場で実行できる具体的な内容にする
- 分野別記録が少ない場合は、最初に実力測定を入れる
- 1回の低得点だけで弱点と断定しない
- 最終週は総合演習と誤答の見直しを中心にする
- 記録にない実績を作らない
- 全体を1200文字以内にする

次の形式で回答してください。

### 全体方針
短くまとめる。

### 週ごとの計画
第1週から第{weeks}週まで記載する。

### 毎週の確認
進捗を判断する基準を3項目以内で記載する。
"""

    plan_id = save_sg_plan(
        DB_PATH,
        ctx.author.id,
        weeks,
        weekly_questions,
        datetime.now(JST).isoformat(),
        today=datetime.now(JST).date(),
    )

    async with ctx.typing():
        try:
            answer = strip_week_heading_dates(await ask_ollama(prompt))

            update_sg_plan_text(DB_PATH, plan_id, answer)

            await ctx.send(
                f"🗓️ **SG合格まで{weeks}週間の計画**\n"
                + (
                    format_exam_countdown(exam_on, today_date, "SG試験")
                    + "\n"
                    if exam_on is not None else ""
                )
                + f"{plan_note}\n"
                f"**各週の期間**\n{schedule_text}\n\n"
                f"基本目標：毎週{weekly_questions}問。"
                "達成状況は `/plan status` で確認できます。\n\n"
                f"{answer}"
            )

        except aiohttp.ClientConnectorError:
            await ctx.send(
                f"毎週{weekly_questions}問の数値目標を保存しました。"
                "`/plan status` で達成状況を確認できます。\n"
                "Ollamaに接続できなかったため、文章の計画は未生成です。"
            )

        except Exception as e:
            print(f"❌ plan エラー: {e}")

            await ctx.send(
                f"毎週{weekly_questions}問の数値目標を保存しました。"
                "`/plan status` で達成状況を確認できます。\n"
                "文章の計画は作成できませんでした。"
            )


@plan_group.command(
    name="status",
    description="週次計画の目標と実績を確認"
)
async def plan_status(ctx):
    status_data = get_sg_plan_status(
        DB_PATH, ctx.author.id, today=datetime.now(JST).date()
    )
    if status_data is None:
        await ctx.send(
            "保存済みのSG計画がありません。"
            "`/plan new weeks:6 weekly_questions:30` で作成できます。"
        )
        return

    lines = [
        "**SG 週次計画の達成状況**",
        f"開始：{status_data['start_on'].isoformat()} / "
        f"{status_data['weeks']}週間 / "
        f"基本目標：{status_data['weekly_questions']}問/週",
    ]
    countdown = get_exam_countdown_line(ctx.author.id)
    if countdown:
        lines.append(countdown)
    for week in status_data["rows"]:
        label = (
            "進行中" if week["week"] == status_data["current_week"]
            else "完了"
        )
        lines.append(
            f"第{week['week']}週 "
            f"({week['start_on'].isoformat()}〜"
            f"{week['end_on'].isoformat()})："
            f"{week['actual']}/{week['target']}問 "
            f"[{label}]"
        )

    if status_data["completed"]:
        lines.append("計画期間は終了しました。")
    elif status_data["rows"]:
        current = status_data["rows"][-1]
        lines.append(f"今週の残り：{current['remaining']}問")
        lines.append(
            "週の未達分は翌週に最大で基本目標の50%まで繰り越します。"
        )

    await ctx.send("\n".join(lines)[:1900])


# ============================================================
# 試験日設定（/plan exam）
# ============================================================

class ExamDatePartSelect(discord.ui.Select):
    def __init__(self, part, choices, placeholder, selected, row,
                 disabled=False):
        options = [
            discord.SelectOption(
                label=label,
                value=str(value),
                default=value == selected,
            )
            for label, value in choices
        ] or [discord.SelectOption(label="—", value="0")]
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            row=row,
            disabled=disabled or not choices,
        )
        self.part = part

    async def callback(self, interaction):
        await self.view.select_part(
            interaction, self.part, int(self.values[0])
        )


class ExamDateView(discord.ui.View):
    def __init__(self, owner_id, target, today):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.target = target
        self.today = today
        current = target["exam_on"]
        if current is not None and current >= today:
            self.year, self.month, self.day = (
                current.year, current.month, current.day
            )
        else:
            self.year = self.month = self.day = None
        self._build()

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この設定画面は実行した本人専用です。", ephemeral=True
        )
        return False

    def _valid_months(self):
        if self.year is None:
            return []
        first = self.today.month if self.year == self.today.year else 1
        return list(range(first, 13))

    def _valid_days(self):
        if self.year is None or self.month is None:
            return []
        first = (
            self.today.day
            if (self.year, self.month)
            == (self.today.year, self.today.month)
            else 1
        )
        return list(
            range(first, days_in_month(self.year, self.month) + 1)
        )

    def selected_date(self):
        if None in (self.year, self.month, self.day):
            return None
        try:
            return build_exam_date(
                self.year, self.month, self.day, self.today
            )
        except ValueError:
            return None

    def _build(self):
        # 年や月を変えて存在しなくなった選択は外す
        if self.month not in self._valid_months():
            self.month = None
        if self.day not in self._valid_days():
            self.day = None

        self.clear_items()
        self.add_item(ExamDatePartSelect(
            "year",
            [(f"{year}年", year) for year in exam_year_choices(self.today)],
            "年を選択",
            self.year,
            row=0,
        ))
        self.add_item(ExamDatePartSelect(
            "month",
            [(f"{month}月", month) for month in self._valid_months()],
            "月を選択" if self.year else "先に年を選択",
            self.month,
            row=1,
            disabled=self.year is None,
        ))

        day_choices = [
            (
                f"{day}日（"
                f"{WEEKDAY_LABELS[date(self.year, self.month, day).weekday()]}）",
                day,
            )
            for day in self._valid_days()
        ]
        if not day_choices:
            self.add_item(ExamDatePartSelect(
                "day", [], "先に年と月を選択", None, row=2, disabled=True,
            ))
        elif len(day_choices) <= 25:
            self.add_item(ExamDatePartSelect(
                "day", day_choices, "日を選択", self.day, row=2,
            ))
        else:
            # Discordのセレクトは25件までなので前半・後半に分ける
            for row, chunk in (
                (2, day_choices[:16]), (3, day_choices[16:])
            ):
                self.add_item(ExamDatePartSelect(
                    "day",
                    chunk,
                    f"日を選択（{chunk[0][1]}〜{chunk[-1][1]}日）",
                    self.day,
                    row=row,
                ))

        save_button = discord.ui.Button(
            label="この日で保存",
            style=discord.ButtonStyle.success,
            disabled=self.selected_date() is None,
            row=4,
        )
        save_button.callback = self.save
        self.add_item(save_button)

        if self.target["exam_on"] is not None:
            delete_button = discord.ui.Button(
                label="試験日を削除",
                style=discord.ButtonStyle.danger,
                row=4,
            )
            delete_button.callback = self.delete
            self.add_item(delete_button)

        close_button = discord.ui.Button(
            label="閉じる",
            style=discord.ButtonStyle.secondary,
            row=4,
        )
        close_button.callback = self.close
        self.add_item(close_button)

    def content(self):
        current = self.target["exam_on"]
        lines = [f"**{self.target['display_name']} の試験日**"]
        lines.append(
            "現在の設定："
            + (
                format_exam_countdown(current, self.today)
                if current is not None else "未設定"
            )
        )

        year_text = f"{self.year}年" if self.year else "□年"
        month_text = f"{self.month}月" if self.month else "□月"
        day_text = f"{self.day}日" if self.day else "□日"
        lines.append(f"\n選択中：**{year_text}{month_text}{day_text}**")

        selected = self.selected_date()
        if selected is not None:
            lines.append(
                "保存すると：" + format_exam_countdown(selected, self.today)
            )
        else:
            lines.append(
                "年・月・日を順に選んで「この日で保存」を押してください。"
            )
        return "\n".join(lines)

    async def select_part(self, interaction, part, value):
        setattr(self, part, value)
        self._build()
        await interaction.response.edit_message(
            content=self.content(), view=self
        )

    async def save(self, interaction):
        selected = self.selected_date()
        if selected is None:
            await interaction.response.send_message(
                "年・月・日をすべて選んでください。", ephemeral=True
            )
            return

        save_exam_date(
            DB_PATH,
            self.owner_id,
            self.target["qualification"],
            selected,
            datetime.now(JST).isoformat(),
        )
        self.stop()
        await interaction.response.edit_message(
            content=(
                "試験日を保存しました。\n"
                + format_exam_countdown(
                    selected, self.today, self.target["label"]
                )
                + "\n`/plan new` で weeks を省略すると、"
                "この日までの週数で計画を作ります。"
            ),
            view=None,
        )

    async def delete(self, interaction):
        delete_exam_date(
            DB_PATH, self.owner_id, self.target["qualification"]
        )
        self.stop()
        await interaction.response.edit_message(
            content=f"{self.target['label']}の試験日を削除しました。",
            view=None,
        )

    async def close(self, interaction):
        self.stop()
        await interaction.response.edit_message(
            content="試験日は変更していません。", view=None
        )


@plan_group.command(
    name="exam",
    description="試験日を年・月・日から選んで設定・確認"
)
async def exam_command(ctx):
    view = ExamDateView(
        ctx.author.id,
        get_current_exam_target(ctx.author.id),
        datetime.now(JST).date(),
    )
    await ctx.send(view.content(), view=view, ephemeral=True)


@ai_group.command(
    name="report",
    description="今週のSG学習レポートをAIが作成"
)
async def report(ctx):
    qualification = "SG"

    week_rows = get_week_total(
        ctx.author.id
    )

    total_seconds = sum(
        seconds
        for _, seconds
        in week_rows
    )

    day_names = {
        0: "月",
        1: "火",
        2: "水",
        3: "木",
        4: "金",
        5: "土",
        6: "日"
    }

    daily_lines = []

    for (
        study_date,
        day_seconds
    ) in week_rows:

        date_obj = datetime.strptime(
            study_date,
            "%Y-%m-%d"
        )

        day = day_names[
            date_obj.weekday()
        ]

        daily_lines.append(
            f"- {day}曜日："
            f"{format_duration(day_seconds)}"
        )

    if daily_lines:
        daily_text = "\n".join(
            daily_lines
        )
    else:
        daily_text = "記録なし"

    report_status = (
        get_week_analysis_status(
            ctx.author.id,
            qualification
        )
    )

    total_questions = (
        report_status[
            "total_questions"
        ]
    )

    average_score = (
        report_status[
            "average_score"
        ]
    )

    log_count = (
        report_status[
            "log_count"
        ]
    )

    if average_score is not None:
        score_text = (
            f"{average_score:.1f}%"
        )
    else:
        score_text = "記録なし"

    week_categories = get_category_status(
        ctx.author.id,
        qualification,
        report_status["start_date"],
        report_status["end_date"]
    )
    category_text = (
        "\n".join(
            f"- {format_category_label(item)}："
            + (
                f"{item['average_score']:.1f}%"
                if item["average_score"] is not None
                else f"記録{item['log_count']}回"
            )
            for item in week_categories
        )
        if week_categories
        else "記録なし"
    )

    review_candidates = get_review_candidates(
        week_categories,
        "average_score"
    )
    review_text = (
        "\n".join(
            f"- {format_category_label(item)}："
            f"{item['average_score']:.1f}%"
            for item in review_candidates
        )
        if review_candidates
        else "現在はなし"
    )

    if (
        total_seconds == 0
        and log_count == 0
    ):
        await ctx.send(
            "📊 今週はまだ週報を作れる"
            "学習記録がありません。"
        )
        return

    prompt = f"""
今週の資格勉強について週報を作成してください。

【対象資格】
情報セキュリティマネジメント（SG）

【期間】
{report_status["start_date"]} 〜 {report_status["end_date"]}

【今週の総勉強時間】
{format_duration(total_seconds)}

【曜日別の勉強時間】
{daily_text}

【解析済み勉強ログ】
{log_count}件

【解いた問題数】
{total_questions}問

【問題数で重み付けした平均正答率】
{score_text}

【分野別成績】
{category_text}

【要復習候補（60%未満）】
{review_text}

【データの読み方に関する重要ルール】
- 1回の結果だけで弱点と断定しない
- 曜日別一覧に存在しない曜日を
  「勉強していない」と断定しない
- 集計期間終了日より後の曜日や未来の日付を評価しない
- 目標時間は与えられていないため
  「勉強時間が不足」と断定しない
- 記録にない教材を実績として追加しない

次の3項目で回答してください。

### 今週の実績
記録された事実のみをまとめる。

### 今週の課題
記録から判断できる改善候補を整理する。
根拠がなければ断定しない。

### 来週の方針
AIからの提案として3項目以内で提案する。
過去問道場を中心に、
要復習候補と総合問題をバランスよく提案する。
"""

    async with ctx.typing():
        try:
            answer = await ask_ollama(
                prompt
            )

            await ctx.send(
                "📊 **SG 週間レポート**\n\n"
                f"📅 期間：**"
                f"{report_status['start_date']} "
                f"〜 "
                f"{report_status['end_date']}**\n"
                f"⏱️ 勉強時間：**"
                f"{format_duration(total_seconds)}**\n"
                f"🔢 問題数：**"
                f"{total_questions}問**\n"
                f"🎯 平均正答率：**"
                f"{score_text}**\n\n"
                f"{answer}"
            )

        except aiohttp.ClientConnectorError:
            await ctx.send(
                "⚠️ Ollamaに接続できません。\n"
                "Ollamaが起動しているか"
                "確認してください。"
            )

        except Exception as e:
            print(
                f"❌ report エラー: {e}"
            )

            await ctx.send(
                "⚠️ 週間レポートの作成に"
                "失敗しました。\n"
                "VS Codeのターミナルを"
                "確認してください。"
            )


@ai_group.command(
    name="today",
    description="今日の学習状況をAIが分析"
)
async def ai(ctx):
    total_seconds = get_today_total(
        ctx.author.id
    )

    logs_data = get_today_logs(
        ctx.author.id
    )

    if logs_data:
        log_text = "\n".join(
            f"- {log}"
            for log in logs_data
        )
    else:
        log_text = (
            "今日はまだ勉強ログが"
            "ありません。"
        )

    today = datetime.now(JST).strftime("%Y-%m-%d")
    today_categories = get_category_status(
        ctx.author.id,
        "SG",
        today,
        today
    )
    review_candidates = get_review_candidates(
        today_categories,
        "average_score"
    )
    review_text = (
        "\n".join(
            f"- {format_category_label(item)}："
            f"{item['average_score']:.1f}%"
            for item in review_candidates
        )
        if review_candidates
        else "現在はなし"
    )

    prompt = f"""
今日の学習状況を分析してください。

【勉強時間】
{format_duration(total_seconds)}

【今日の勉強ログ】
{log_text}

【今日の要復習候補（分野別正答率60%未満）】
{review_text}

【資格取得ロードマップ】
1. 情報セキュリティマネジメント（SG）
2. 基本情報技術者（FE）
3. 医療情報技師

現在は情報セキュリティマネジメント（SG）を
最優先で勉強しています。

以下の3項目に分けて回答してください。

### 今日できたこと
今日の記録から実施した内容をまとめる。

### 要復習候補・気になる点
ログから判断できる復習候補を整理する。
分からないことは推測しすぎない。

### 次回やること
AIからの提案として、
次の勉強で具体的に何をすればいいか提案する。
"""

    async with ctx.typing():
        try:
            answer = await ask_ollama(
                prompt
            )

            await ctx.send(
                "🤖 **Study Coach**\n\n"
                f"{answer}"
            )

        except aiohttp.ClientConnectorError:
            await ctx.send(
                "⚠️ Ollamaに接続できません。\n"
                "Ollamaが起動しているか"
                "確認してください。"
            )

        except Exception as e:
            print(
                f"❌ AIエラー: {e}"
            )

            await ctx.send(
                "⚠️ AIの処理中に"
                "エラーが発生しました。\n"
                "VS Codeのターミナルを"
                "確認してください。"
            )


# ============================================================
# VC入退室
# ============================================================

@bot.event
async def on_voice_state_update(
    member,
    before,
    after
):
    if member.bot:
        return

    key = (
        member.guild.id,
        member.id
    )

    # --------------------------------------------------------
    # 勉強VCへ入室
    # --------------------------------------------------------

    entered_study_vc = (
        after.channel is not None
        and after.channel.name
        == STUDY_VOICE_CHANNEL_NAME
        and (
            before.channel is None
            or before.channel.name
            != STUDY_VOICE_CHANNEL_NAME
        )
    )

    if entered_study_vc:
        # DBに進行中セッションがあれば
        # 二重開始を避けてそれを使う
        existing_start = (
            get_active_study_session(
                member.guild.id,
                member.id
            )
        )

        if existing_start is not None:
            start_time = existing_start
        else:
            start_time = datetime.now(JST)

            save_active_study_session(
                member.guild.id,
                member.id,
                member.display_name,
                start_time
            )

        study_sessions[key] = start_time

        print(
            f"📚 勉強開始: "
            f"{member.display_name} "
            f"{start_time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    # --------------------------------------------------------
    # 勉強VCから退出
    # --------------------------------------------------------

    left_study_vc = (
        before.channel is not None
        and before.channel.name
        == STUDY_VOICE_CHANNEL_NAME
        and (
            after.channel is None
            or after.channel.name
            != STUDY_VOICE_CHANNEL_NAME
        )
    )

    if left_study_vc:
        end_time = datetime.now(JST)

        start_time = study_sessions.pop(
            key,
            None
        )

        # メモリに無ければDBから復元
        if start_time is None:
            start_time = (
                get_active_study_session(
                    member.guild.id,
                    member.id
                )
            )

        if start_time is None:
            print(
                "⚠️ 開始時刻が記録されて"
                "いません。"
            )
            return

        total_seconds = int(
            (
                end_time
                - start_time
            ).total_seconds()
        )

        if total_seconds < 0:
            total_seconds = 0

        save_completed_study_session(
            guild_id=member.guild.id,
            user_id=member.id,
            username=member.display_name,
            start_time=start_time,
            end_time=end_time,
            duration_seconds=total_seconds
        )

        # 完了保存後にactiveを削除
        delete_active_study_session(
            member.guild.id,
            member.id
        )

        print(
            f"✅ 勉強終了: "
            f"{member.display_name}\n"
            f"開始: "
            f"{start_time.strftime('%H:%M:%S')}\n"
            f"終了: "
            f"{end_time.strftime('%H:%M:%S')}\n"
            f"勉強時間: "
            f"{format_duration(total_seconds)}\n"
            "💾 データベースへ保存しました"
        )

        # ----------------------------------------------------
        # Discordへ終了通知
        # ----------------------------------------------------

        notification_channel = (
            discord.utils.get(
                member.guild.text_channels,
                name=(
                    STUDY_NOTIFICATION_CHANNEL_NAME
                )
            )
        )

        if notification_channel is None:
            print(
                "⚠️ 勉強終了通知を送信"
                "できませんでした。"
                f"テキストチャンネル"
                f"「{STUDY_NOTIFICATION_CHANNEL_NAME}」"
                "が見つかりません。"
            )
            return

        today_total_seconds = (
            get_today_total(
                member.id
            )
        )

        week_total_seconds = (
            get_week_total_seconds(
                member.id
            )
        )

        streak = get_study_streak_safe(member.id)
        countdown = get_exam_countdown_line(member.id)
        countdown_text = f"📆 {countdown}\n" if countdown else ""

        await notification_channel.send(
            f"📚 **{member.mention} "
            "勉強おつかれさま！**\n\n"
            f"⏱️ 今回：**"
            f"{format_duration(total_seconds)}**\n"
            f"📅 今日：**"
            f"{format_duration(today_total_seconds)}**\n"
            f"📊 今週：**"
            f"{format_duration(week_total_seconds)}**\n"
            f"🔥 連続学習：**{streak}日**\n"
            f"{countdown_text}\n"
            f"📝 今日やった内容をこの "
            f"**#{STUDY_LOG_CHANNEL_NAME}** "
            "に書いてね！\n"
            "例：`SG過去問道場25問中10問正解。"
            "テクノロジ系のセキュリティを学習。`"
        )


# ============================================================
# Token確認 / 起動
# ============================================================

def main():
    if TOKEN is None:
        raise RuntimeError(
            "DISCORD_TOKENが読み込めませんでした。"
            ".envを確認してください。"
        )

    init_db()
    init_glossary_history_db(GLOSSARY_HISTORY_PATH)
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
