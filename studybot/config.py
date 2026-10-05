"""StudyBot の設定値。.env から読む値と、コード内の決まりごと。"""

import os
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from studybot.daily_digest import parse_digest_time
from studybot.notion import extract_notion_id


# プロジェクトのフォルダ（bot.py がある場所）
PROJECT_ROOT = Path(__file__).resolve().parents[1]

load_dotenv(PROJECT_ROOT / ".env")

TOKEN = os.getenv("DISCORD_TOKEN")

# チャンネルは /setup で選んだものを優先し、未設定ならこの名前で探す
STUDY_VOICE_CHANNEL_NAME = "勉強部屋"
STUDY_LOG_CHANNEL_NAME = "勉強ログ"
SG_GLOSSARY_CHANNEL_NAME = "SG用語集"
AI_REPORT_CHANNEL_NAME = "ai-report"
FOCUS_CHANNEL_NAME = "集中タイマー"
FE_GLOSSARY_CHANNEL_NAME = "FE用語集"

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

# 学習データ。テストでは一時ファイルに差し替えるので、
# 使う側は必ず config.DB_PATH として参照する
DB_PATH = str(PROJECT_ROOT / "data" / "study.db")

# 分野別正答率がこの値未満なら要復習候補
REVIEW_SCORE_THRESHOLD = 60.0

# 学習メニューを送る時刻。平日は夜、土日祝は朝。
# .env の STUDYBOT_DIGEST_TIME_WEEKDAY / STUDYBOT_DIGEST_TIME_HOLIDAY で変更可
DIGEST_TIME_WEEKDAY = parse_digest_time(
    os.getenv("STUDYBOT_DIGEST_TIME_WEEKDAY", "19:00"), default=time(19, 0)
)
DIGEST_TIME_HOLIDAY = parse_digest_time(
    os.getenv("STUDYBOT_DIGEST_TIME_HOLIDAY", "07:00"), default=time(7, 0)
)

# 再起動が遅れたとき、この時刻までなら当日分をあとから送る
DIGEST_CATCH_UP_UNTIL_HOUR = 22

# 週間レポートを自動で送る曜日（0=月曜 … 6=日曜）と時刻
WEEKLY_REPORT_WEEKDAY = 6
WEEKLY_REPORT_TIME = time(21, 0)

# Notion への週ページの保存（日曜のうちの勉強まで入るよう、日付が変わる直前）
NOTION_SAVE_TIME = time(23, 59)
# 保存時刻にBotが止まっていたら、翌朝この時刻までに前の週の分を保存する
NOTION_CATCH_UP_UNTIL_HOUR = 12

# Notion への週間レポートの保存（両方そろったときだけ動く）
NOTION_TOKEN = os.getenv("NOTION_TOKEN") or None
# 保存先ページのURLかID。インテグレーションに共有しておく
NOTION_PAGE_ID = extract_notion_id(os.getenv("NOTION_PAGE_ID", ""))

BOT_VERSION = "3.1"
