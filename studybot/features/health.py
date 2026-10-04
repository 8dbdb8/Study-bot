"""Botの健康チェック：Ollama・Notion・チャンネルが使えるかを確かめる。

起動したときと毎朝6:30に確認し、問題が見つかったら #ai-report
（なければ #勉強ログ）に知らせる。同じ問題は二度知らせず、
直ったときに「解決しました」と1回だけ知らせる。
"""

import asyncio
from datetime import datetime, time

import aiohttp
import discord
from discord.ext import tasks

from studybot import config
from studybot.channels import CHANNEL_KINDS, find_channel
from studybot.config import JST
from studybot.embeds import COLOR_ALERT, COLOR_SUCCESS
from studybot.features.notion_export import is_notion_configured
from studybot.health_state import load_notified_problems, save_notified_problems
from studybot.notion import NotionClient, NotionError


HEALTH_CHECK_TIME = time(6, 30)
CHECK_TIMEOUT = aiohttp.ClientTimeout(total=8)

# 最後に確認した結果（/setup の画面に出す）
LAST_CHECK = {"checked_at": None, "results": []}


# ------------------------------------------------------------
# それぞれの確認。結果は (名前, 状態, 説明)。状態は True=OK・False=問題・None=未設定
# ------------------------------------------------------------

def _ollama_tags_url():
    return config.OLLAMA_URL.rsplit("/api/", 1)[0] + "/api/tags"


async def check_ollama(session):
    try:
        async with session.get(_ollama_tags_url()) as response:
            if response.status != 200:
                return "Ollama", False, f"エラーが返りました（{response.status}）"
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return "Ollama", False, "接続できません（Ollamaが起動していないかもしれません）"
    names = {model.get("name", "") for model in (data or {}).get("models", [])}
    model = config.OLLAMA_MODEL
    if model not in names and f"{model}:latest" not in names:
        return (
            "Ollama", False,
            f"モデル {model} が入っていません（`ollama pull {model}`）",
        )
    return "Ollama", True, f"{model} が使えます"


async def check_notion(session):
    if not is_notion_configured():
        return "Notion", None, "未設定（週間レポートはNotionに保存しません）"
    try:
        await NotionClient(config.NOTION_TOKEN, session).retrieve_page(
            config.NOTION_PAGE_ID
        )
    except NotionError as error:
        return "Notion", False, error.friendly()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return "Notion", False, "Notionに接続できませんでした"
    return "Notion", True, "保存先のページが使えます"


# 見つからないときに問題とするチャンネル（ほかは見つからなければ代わりを使う）
REQUIRED_CHANNELS = ("study_voice", "study_log")


def check_channels(guild):
    results = []
    me = guild.me
    for kind, (_, default_name, channel_type) in CHANNEL_KINDS.items():
        channel = find_channel(guild, kind)
        name = f"#{default_name}"
        if channel is None:
            if kind in REQUIRED_CHANNELS:
                results.append((
                    name, False,
                    f"見つかりません（「{default_name}」を作るか /setup で選んでください）",
                ))
            continue
        if channel_type == "text" and me is not None:
            permissions = channel.permissions_for(me)
            if not (permissions.send_messages and permissions.embed_links):
                results.append((
                    name, False, f"{channel.mention} にメッセージを送る権限がありません",
                ))
                continue
        results.append((name, True, channel.mention))
    return results


async def run_health_check(bot):
    async with aiohttp.ClientSession(timeout=CHECK_TIMEOUT) as session:
        results = [await check_ollama(session), await check_notion(session)]
    for guild in bot.guilds:
        results.extend(check_channels(guild))
    return results


def problem_lines(results):
    return [f"{name}：{detail}" for name, ok, detail in results if ok is False]


def format_health(results, checked_at=None):
    """/setup に出す接続状態。"""
    marks = {True: "✅", False: "⚠️", None: "・"}
    lines = [
        f"{marks[ok]} {name}：{detail}"
        for name, ok, detail in results
        if not name.startswith("#")
    ]
    problems = [line for line in problem_lines(results) if line.startswith("#")]
    lines.extend(f"⚠️ {line}" for line in problems)
    if checked_at is not None:
        lines.append(f"（{checked_at.month}/{checked_at.day} {checked_at:%H:%M} に確認）")
    return "\n".join(lines)


def _notice_channels(bot):
    for guild in bot.guilds:
        channel = find_channel(guild, "ai_report") or find_channel(guild, "study_log")
        if channel is not None:
            yield channel


async def check_and_notify(bot, now=None):
    """確認して、問題が変わったときだけ知らせる。結果を返す。"""
    try:
        results = await run_health_check(bot)
    except Exception as error:
        print(f"[health] 確認に失敗: {error!r}")
        return []
    LAST_CHECK["checked_at"] = now or datetime.now(JST)
    LAST_CHECK["results"] = results

    problems = problem_lines(results)
    previous = load_notified_problems(config.DB_PATH)
    if problems == previous:
        return results

    if problems:
        embed = discord.Embed(
            title="StudyBot の健康チェック：確認が必要です",
            description="\n".join(f"⚠️ {line}" for line in problems),
            color=COLOR_ALERT,
        )
        embed.set_footer(text="直ったら、ここでお知らせします")
    else:
        embed = discord.Embed(
            title="StudyBot の健康チェック：問題は解決しました",
            description="Ollama・Notion・チャンネルはすべて使えます。",
            color=COLOR_SUCCESS,
        )
    for channel in _notice_channels(bot):
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as error:
            print(f"[health] 送信に失敗: {error}")
    save_notified_problems(config.DB_PATH, problems)
    print(f"[health] problems={len(problems)}")
    return results


@tasks.loop(time=HEALTH_CHECK_TIME.replace(tzinfo=JST))
async def health_loop(bot):
    await check_and_notify(bot)
