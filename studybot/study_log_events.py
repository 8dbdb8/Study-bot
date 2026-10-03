"""#勉強ログ への投稿・編集・削除を学習ログとして保存・解析する。"""

import json

import discord

from studybot.analysis import build_analysis_reply
from studybot.channels import is_channel
from studybot.database import (
    delete_study_log_data,
    get_analysis_reply_message_id,
    save_study_analysis,
    save_study_log,
    set_analysis_reply_message_id,
)
from studybot.ollama import analyze_study_log
from studybot.stats import get_current_qualification, get_study_status
from studybot.study_log_parser import normalize_study_analysis


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
# 勉強ログ新規投稿
# ============================================================

async def save_study_message(message):

    if message.author.bot:
        return

    if message.guild is None:
        return

    if not is_channel(message.channel, "study_log"):
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

async def update_study_message(
    before,
    after
):
    if after.author.bot:
        return

    if after.guild is None:
        return

    if not is_channel(after.channel, "study_log"):
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

async def delete_study_message(bot, payload):
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


def register(bot):
    bot.add_listener(save_study_message, "on_message")
    bot.add_listener(update_study_message, "on_message_edit")

    async def on_raw_message_delete(payload):
        await delete_study_message(bot, payload)

    bot.add_listener(on_raw_message_delete)
