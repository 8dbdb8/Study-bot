"""週間レポートとグラフを Notion のデータベースに週ごとのページとして保存する。

.env の NOTION_TOKEN と NOTION_PAGE_ID が設定されているときだけ動く。
最初の保存で、共有されたページの中に「StudyBot 週間レポート」の
データベースを作り、以降はそこに1週間＝1ページを追加していく。

週ページは「Botの欄」と、その下の「メモ（ChatGPT）」の欄に分かれる。
同じ週を保存し直すと、Botが作ったブロックだけを消して書き直し、
メモ欄から下（ChatGPTや自分が書いた内容）には触れない。
"""

import asyncio
from datetime import date, timedelta

import aiohttp

from studybot import config
from studybot.charts import render_score_chart, render_study_time_chart
from studybot.embeds import format_minutes
from studybot.notion import (
    NotionClient,
    NotionError,
    callout_block,
    image_block,
    markdown_to_blocks,
    text_block,
)
from studybot.notion_store import (
    clear_notion_database,
    get_notion_database,
    get_weekly_page,
    save_notion_database,
    save_weekly_page,
)
from studybot.stats import get_daily_scores, get_daily_study_seconds


DATABASE_TITLE = "StudyBot 週間レポート"

# データベースの列（初回に自動で作る）
PROPERTY_WEEK = "週"
PROPERTY_START = "開始日"
PROPERTY_MINUTES = "勉強時間（分）"
PROPERTY_QUESTIONS = "問題数"
PROPERTY_SCORE = "正答率（%）"

DATABASE_PROPERTIES = {
    PROPERTY_WEEK: {"type": "title", "title": {}},
    PROPERTY_START: {"type": "date", "date": {}},
    PROPERTY_MINUTES: {"type": "number", "number": {"format": "number"}},
    PROPERTY_QUESTIONS: {"type": "number", "number": {"format": "number"}},
    PROPERTY_SCORE: {"type": "number", "number": {"format": "number"}},
}

# この見出しから下は、ChatGPTや自分が書く欄。Botは触らない
MEMO_HEADING = "メモ（ChatGPT）"

# 正答率グラフは直近4週間の流れが分かるようにする
SCORE_CHART_DAYS = 28

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)


def is_notion_configured():
    return bool(config.NOTION_TOKEN and config.NOTION_PAGE_ID)


def _score_number(data):
    text = data["score_text"].rstrip("%")
    try:
        return round(float(text), 1)
    except ValueError:
        return None


def build_page_properties(data, week_start):
    week_end = week_start + timedelta(days=6)
    return {
        PROPERTY_WEEK: {"title": [{
            "type": "text",
            "text": {
                "content": (
                    f"{week_start.month}/{week_start.day}〜"
                    f"{week_end.month}/{week_end.day}"
                ),
            },
        }]},
        PROPERTY_START: {"date": {"start": week_start.isoformat()}},
        PROPERTY_MINUTES: {"number": round(data["total_seconds"] / 60)},
        PROPERTY_QUESTIONS: {"number": data["total_questions"]},
        PROPERTY_SCORE: {"number": _score_number(data)},
    }


def memo_heading_block():
    return text_block("heading_2", MEMO_HEADING)


def build_page_children(data, answer, ai_error, chart_ids):
    """Botが毎回書き換える欄。メモ欄の見出しは含まない。"""
    blocks = [
        callout_block(
            f"このページは StudyBot が自動で更新します。"
            f"「{MEMO_HEADING}」から下は書き換えないので、自由に書けます。",
            "🤖",
        ),
        text_block("heading_2", "今週の数字"),
        text_block(
            "bulleted_list_item", f"資格：{data.get('qualification', 'SG')}"
        ),
        text_block(
            "bulleted_list_item",
            f"期間：{data['start_date']} 〜 {data['end_date']}",
        ),
        text_block(
            "bulleted_list_item",
            f"勉強時間：{format_minutes(data['total_seconds'])}",
        ),
        text_block("bulleted_list_item", f"問題数：{data['total_questions']}問"),
        text_block("bulleted_list_item", f"平均正答率：{data['score_text']}"),
    ]
    if data.get("notes_text"):
        blocks.append(text_block("heading_2", "今週のひとこと"))
        blocks.extend(
            text_block("bulleted_list_item", line.removeprefix("- "))
            for line in data["notes_text"].splitlines()
        )
    if chart_ids:
        blocks.append(text_block("heading_2", "グラフ"))
        blocks.extend(image_block(file_id) for file_id in chart_ids)

    blocks.append(text_block("heading_2", "AIの振り返り"))
    if answer:
        blocks.extend(markdown_to_blocks(answer))
    else:
        blocks.append(text_block(
            "paragraph",
            "AIのコメントは作れませんでした"
            + (f"（{ai_error}）" if ai_error else "") + "。",
        ))
    return blocks


def render_weekly_charts(user_id, week_start, today, qualification="SG"):
    """(ファイル名, PNG) のリスト。記録がないグラフは作らない。"""
    charts = []
    week_end = week_start + timedelta(days=6)
    daily = get_daily_study_seconds(user_id, week_start, week_end)
    if daily:
        charts.append((
            "study_time.png",
            render_study_time_chart(daily, week_start, week_end),
        ))

    since = today - timedelta(days=SCORE_CHART_DAYS - 1)
    points = [
        point for point in get_daily_scores(user_id, qualification)
        if since <= point[0] <= today
    ]
    if points:
        charts.append((
            "sg_score.png",
            render_score_chart(
                points, f"{qualification} 正答率の推移（直近4週間）",
                config.REVIEW_SCORE_THRESHOLD,
            ),
        ))
    return charts


async def _ensure_data_source(client, parent_page_id):
    stored = get_notion_database(config.DB_PATH, parent_page_id)
    if stored is not None:
        return stored[1]
    database_id, data_source_id = await client.create_database(
        parent_page_id, DATABASE_TITLE, DATABASE_PROPERTIES
    )
    save_notion_database(
        config.DB_PATH, parent_page_id, database_id, data_source_id
    )
    return data_source_id


def _is_page_gone(error):
    """ページが消された・ゴミ箱に入れられたときのエラーか。"""
    message = (error.message or "").lower()
    return error.status == 404 or (
        error.status == 400 and ("archived" in message or "trash" in message)
    )


async def _create_page_in_database(client, parent_page_id, properties):
    data_source_id = await _ensure_data_source(client, parent_page_id)
    try:
        return await client.create_page(data_source_id, properties)
    except NotionError as error:
        if error.status != 404:
            raise
        # データベースが手で消された場合は作り直してもう一度
        clear_notion_database(config.DB_PATH)
        data_source_id = await _ensure_data_source(client, parent_page_id)
        return await client.create_page(data_source_id, properties)


async def _create_week_page(client, properties, bot_blocks):
    """新しい週ページ。Botの欄の下に「メモ（ChatGPT）」の見出しを置く。"""
    page_id, url = await _create_page_in_database(
        client, config.NOTION_PAGE_ID, properties
    )
    bot_block_ids = await client.append_children(page_id, bot_blocks)
    memo_ids = await client.append_children(page_id, [memo_heading_block()])
    return page_id, url, bot_block_ids, memo_ids[0]


async def _memo_heading_exists(client, memo_block_id):
    if not memo_block_id:
        return False
    try:
        block = await client.retrieve_block(memo_block_id)
    except NotionError as error:
        if error.status == 404:
            return False
        raise
    return not (block.get("in_trash") or block.get("archived"))


async def _update_week_page(client, record, properties, bot_blocks):
    """Botの欄だけを書き換える。メモ欄から下には触れない。"""
    page_id = record["page_id"]
    await client.update_page_properties(page_id, properties)

    old_ids = record["bot_block_ids"]
    if old_ids is None:
        # メモ欄を作る前の版のページは、中身がすべてBotの書いたもの
        old_ids = [block["id"] for block in await client.list_children(page_id)]
    for block_id in old_ids:
        try:
            await client.delete_block(block_id)
        except NotionError as error:
            if error.status != 404:  # 手で消されていたら気にしない
                raise

    bot_block_ids = await client.append_children(
        page_id, bot_blocks, position="start"
    )
    memo_block_id = record["memo_block_id"]
    if not await _memo_heading_exists(client, memo_block_id):
        memo_block_id = (
            await client.append_children(page_id, [memo_heading_block()])
        )[0]
    return page_id, record["url"], bot_block_ids, memo_block_id


async def export_weekly_report(client, user_id, data, answer, ai_error, today):
    """週ページを作るか書き換えて、ページの URL を返す。"""
    week_start = date.fromisoformat(data["start_date"])

    charts = await asyncio.to_thread(
        render_weekly_charts, user_id, week_start, today,
        data.get("qualification", "SG"),
    )
    chart_ids = [
        await client.upload_png(png, filename) for filename, png in charts
    ]
    properties = build_page_properties(data, week_start)
    bot_blocks = build_page_children(data, answer, ai_error, chart_ids)

    record = get_weekly_page(config.DB_PATH, user_id, week_start)
    result = None
    if record is not None:
        try:
            result = await _update_week_page(
                client, record, properties, bot_blocks
            )
        except NotionError as error:
            if not _is_page_gone(error):
                raise
            print("[notion] 週ページが見つからないため作り直します")
    if result is None:
        result = await _create_week_page(client, properties, bot_blocks)

    page_id, url, bot_block_ids, memo_block_id = result
    save_weekly_page(
        config.DB_PATH, user_id, week_start, page_id, url,
        bot_block_ids, memo_block_id,
    )
    return url


async def try_save_weekly_report(user_id, data, answer, ai_error, today):
    """Notion に保存して (URL, None)。失敗したら (None, 理由)。"""
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            url = await export_weekly_report(
                NotionClient(config.NOTION_TOKEN, session),
                user_id, data, answer, ai_error, today,
            )
    except NotionError as error:
        print(f"[notion] 保存に失敗: {error}")
        return None, error.friendly()
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        print(f"[notion] 接続に失敗: {error!r}")
        return None, "Notionに接続できませんでした"
    return url or "", None


async def save_weekly_report_to_notion(user_id, data, answer, ai_error, today):
    """Discord に添える1行（保存先のリンクか、失敗の理由）。未設定なら None。"""
    if not is_notion_configured():
        return None
    url, error = await try_save_weekly_report(
        user_id, data, answer, ai_error, today
    )
    if error:
        return f"⚠️ Notionへの保存に失敗しました：{error}"
    return f"📝 Notionにも保存しました：{url}" if url else "📝 Notionにも保存しました"

