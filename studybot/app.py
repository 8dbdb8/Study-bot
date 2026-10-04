"""Botの組み立てと起動。

コマンドやイベントの中身は機能ごとのファイルにあり、ここではそれらを
1つのBotに登録して起動する。
"""

from datetime import datetime

import discord
from discord.ext import commands

from studybot import config, groups, study_log_events, voice
from studybot.database import init_db
from studybot.errors import install_error_handlers
from studybot.features import (
    ai,
    data,
    digest,
    exam,
    exam_result,
    glossary,
    plan,
    qualification,
    review,
    setup,
    time,
    weekly_report,
)
from studybot.sg_glossary_history import (
    GLOSSARY_HISTORY_PATH,
    init_glossary_history_db,
)


# 読み込むと各サブコマンドが /sg や /plan などのグループに登録される
FEATURE_MODULES = (
    ai, data, digest, exam, exam_result, glossary, plan, qualification,
    review, setup, time, weekly_report,
)


def create_bot():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True

    bot = commands.Bot(
        command_prefix="!",
        intents=intents,
        # 標準の !help の代わりに独自の /help を使う
        help_command=None,
    )

    for group in groups.ALL_GROUPS:
        bot.add_command(group)
    bot.add_command(groups.help_command)
    bot.tree.add_command(data.data_command)
    bot.tree.add_command(setup.setup_command)

    study_log_events.register(bot)
    voice.register(bot)
    install_error_handlers(bot)

    # 再接続で on_ready が何度呼ばれても、同期と登録は1回だけ
    synced_guild_ids = set()
    started = False

    @bot.event
    async def on_ready():
        nonlocal started
        print("--------------------")
        print("StudyBot 起動完了！")
        print(f"ログイン中: {bot.user}")
        print(f"AIモデル: {config.OLLAMA_MODEL}")
        print(f"StudyBot Version: {config.BOT_VERSION}")
        print("--------------------")

        # /コマンドを各参加サーバーへ同期
        for guild in bot.guilds:
            if guild.id in synced_guild_ids:
                continue

            try:
                guild_obj = discord.Object(id=guild.id)

                # Hybrid Commandを即時反映しやすい
                # Guild Commandとしてコピー
                bot.tree.copy_global_to(guild=guild_obj)
                synced = await bot.tree.sync(guild=guild_obj)
                synced_guild_ids.add(guild.id)

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

        await voice.recover_active_voice_sessions(bot)

        if not started:
            # 再起動前に送ったメッセージのボタンも押せるようにする
            bot.add_view(voice.VCActionView())
            bot.add_view(digest.DailyDigestView())
            bot.add_view(exam_result.ExamResultView())
            digest.daily_digest_loop.start(bot)
            weekly_report.weekly_report_loop.start(bot)
            weekly_report.notion_save_loop.start(bot)
            exam_result.exam_result_loop.start(bot)
            started = True

        now = datetime.now(config.JST)
        if digest.is_digest_due(now):
            await digest.send_daily_digests(bot, now)
        if weekly_report.is_weekly_report_due(now):
            await weekly_report.send_weekly_reports(bot, now)
        notion_week = weekly_report.notion_save_week_day(now)
        if notion_week is not None:
            await weekly_report.save_weeks_to_notion(bot, notion_week, now)
        if exam_result.is_result_prompt_due(now):
            await exam_result.send_exam_result_prompts(bot, now)

    return bot


def main():
    if config.TOKEN is None:
        raise RuntimeError(
            "DISCORD_TOKENが読み込めませんでした。"
            ".envを確認してください。"
        )

    init_db()
    init_glossary_history_db(GLOSSARY_HISTORY_PATH)
    create_bot().run(config.TOKEN)
