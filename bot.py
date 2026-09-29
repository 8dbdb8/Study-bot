import os
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from study_log_parser import (
    SG_MAJOR_CATEGORIES,
    normalize_study_analysis,
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
    intents=intents
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
            questions,
            correct_answers,
            score_percent,
            weak_points,
            notes,
            analysis_warnings,
            analyzed_at,
            reply_message_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(message_id)
        DO UPDATE SET
            user_id = excluded.user_id,
            qualification = excluded.qualification,
            activity = excluded.activity,
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
    print("StudyBot Version: 2.4")
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

@bot.hybrid_command(
    name="hello",
    description="StudyBotの接続確認"
)
async def hello(ctx):
    await ctx.send(
        "こんにちは！📚 StudyBotです！"
    )


@bot.hybrid_command(
    name="today",
    description="今日の勉強時間を表示"
)
async def today(ctx):
    total_seconds = get_today_total(
        ctx.author.id
    )

    await ctx.send(
        "📚 **今日の勉強時間**\n"
        f"{format_duration(total_seconds)}"
    )


@bot.hybrid_command(
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


@bot.hybrid_command(
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



@bot.hybrid_command(
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

    await ctx.send(
        "🗺️ **資格取得ロードマップ**\n\n"
        f"{roadmap_text}"
        f"{footer}"
    )


@bot.hybrid_command(
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


@bot.hybrid_command(
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


@bot.hybrid_command(
    name="plan",
    description="SG合格までの週次学習計画を作成"
)
@app_commands.describe(
    weeks="試験までの残り週数（1〜16週）"
)
async def plan(ctx, weeks: int):
    if not 1 <= weeks <= 16:
        await ctx.send(
            "⚠️ 残り週数は1〜16週で指定してください。\n"
            "例：`/plan weeks:6` または `!plan 6`"
        )
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

    prompt = f"""
情報セキュリティマネジメント（SG）を
残り{weeks}週間で合格するための学習計画を作成してください。

【学習方法】
情報セキュリティマネジメント過去問道場を中心に学習する。

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
- 各週に「目標問題数」「学習内容」「確認ポイント」を書く
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

    async with ctx.typing():
        try:
            answer = await ask_ollama(prompt)

            await ctx.send(
                f"🗓️ **SG合格まで{weeks}週間の計画**\n\n"
                f"{answer}"
            )

        except aiohttp.ClientConnectorError:
            await ctx.send(
                "⚠️ Ollamaに接続できません。\n"
                "Ollamaが起動しているか確認してください。"
            )

        except Exception as e:
            print(f"❌ plan エラー: {e}")

            await ctx.send(
                "⚠️ 学習計画を作成できませんでした。\n"
                "VS Codeのターミナルを確認してください。"
            )


@bot.hybrid_command(
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


@bot.hybrid_command(
    name="ai",
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

        await notification_channel.send(
            f"📚 **{member.mention} "
            "勉強おつかれさま！**\n\n"
            f"⏱️ 今回：**"
            f"{format_duration(total_seconds)}**\n"
            f"📅 今日：**"
            f"{format_duration(today_total_seconds)}**\n"
            f"📊 今週：**"
            f"{format_duration(week_total_seconds)}**\n\n"
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
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
