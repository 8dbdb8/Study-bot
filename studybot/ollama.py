"""Ollama（ローカルAI）への問い合わせ。"""

import json

import aiohttp

from studybot.config import OLLAMA_MODEL, OLLAMA_URL


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
