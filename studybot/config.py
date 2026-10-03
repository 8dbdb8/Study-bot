"""StudyBot の設定値。.env から読む値と、コード内の決まりごと。"""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from studybot.daily_digest import parse_digest_time


# プロジェクトのフォルダ（bot.py がある場所）
PROJECT_ROOT = Path(__file__).resolve().parents[1]

load_dotenv(PROJECT_ROOT / ".env")

TOKEN = os.getenv("DISCORD_TOKEN")

# チャンネルは /setup で選んだものを優先し、未設定ならこの名前で探す
STUDY_VOICE_CHANNEL_NAME = "勉強部屋"
STUDY_LOG_CHANNEL_NAME = "勉強ログ"
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

# 学習データ。テストでは一時ファイルに差し替えるので、
# 使う側は必ず config.DB_PATH として参照する
DB_PATH = str(PROJECT_ROOT / "data" / "study.db")

# 分野別正答率がこの値未満なら要復習候補
REVIEW_SCORE_THRESHOLD = 60.0

# 毎朝の学習メニューを送る時刻（.env の STUDYBOT_DIGEST_TIME で変更可）
DIGEST_TIME = parse_digest_time(
    os.getenv("STUDYBOT_DIGEST_TIME", "07:00")
)

# 再起動が遅れたとき、この時刻までなら当日分をあとから送る
DIGEST_CATCH_UP_UNTIL_HOUR = 20

BOT_VERSION = "3.0"
