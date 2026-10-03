"""/ai グループ：AIコーチによる分析と提案。"""

from datetime import datetime

import aiohttp
import discord
from discord import app_commands

from studybot.config import JST
from studybot.embeds import COLOR_DEFAULT, format_minutes
from studybot.exam_schedule import WEEKDAY_LABELS
from studybot.formatting import (
    format_category_label,
    format_duration,
    get_review_candidates,
)
from studybot.features.notion_export import (
    is_notion_configured,
    save_weekly_report_to_notion,
)
from studybot.groups import ai_group
from studybot.ollama import ask_ollama
from studybot.stats import (
    get_category_status,
    get_study_status,
    get_today_logs,
    get_today_total,
    get_week_analysis_status,
    get_week_total,
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


def collect_weekly_report(user_id, qualification="SG"):
    """今週（月曜〜今日）の集計。週報にできる記録がなければ None。"""
    week_rows = get_week_total(user_id)
    total_seconds = sum(seconds for _, seconds in week_rows)

    daily_lines = []
    for study_date, day_seconds in week_rows:
        weekday = WEEKDAY_LABELS[
            datetime.strptime(study_date, "%Y-%m-%d").weekday()
        ]
        daily_lines.append(
            f"- {weekday}曜日：{format_duration(day_seconds)}"
        )
    daily_text = "\n".join(daily_lines) if daily_lines else "記録なし"

    report_status = get_week_analysis_status(user_id, qualification)
    total_questions = report_status["total_questions"]
    average_score = report_status["average_score"]
    log_count = report_status["log_count"]

    if total_seconds == 0 and log_count == 0:
        return None

    score_text = (
        f"{average_score:.1f}%" if average_score is not None else "記録なし"
    )

    week_categories = get_category_status(
        user_id,
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

    return {
        "start_date": report_status["start_date"],
        "end_date": report_status["end_date"],
        "total_seconds": total_seconds,
        "daily_text": daily_text,
        "log_count": log_count,
        "total_questions": total_questions,
        "score_text": score_text,
        "category_text": category_text,
        "review_text": review_text,
    }


def build_weekly_report_prompt(data):
    return f"""
今週の資格勉強について週報を作成してください。

【対象資格】
情報セキュリティマネジメント（SG）

【期間】
{data["start_date"]} 〜 {data["end_date"]}

【今週の総勉強時間】
{format_duration(data["total_seconds"])}

【曜日別の勉強時間】
{data["daily_text"]}

【解析済み勉強ログ】
{data["log_count"]}件

【解いた問題数】
{data["total_questions"]}問

【問題数で重み付けした平均正答率】
{data["score_text"]}

【分野別成績】
{data["category_text"]}

【要復習候補（60%未満）】
{data["review_text"]}

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


# Embed の本文の上限（4096文字）に余裕を持たせる
REPORT_TEXT_LIMIT = 3900


def build_weekly_report_embed(data, answer=None, ai_error=None):
    if answer:
        description = answer.strip()
        if len(description) > REPORT_TEXT_LIMIT:
            description = description[:REPORT_TEXT_LIMIT].rstrip() + "\n…（長いため省略）"
    else:
        description = (
            "AIのコメントは作れませんでした"
            + (f"（{ai_error}）" if ai_error else "")
            + "。数字のまとめだけお送りします。"
        )

    embed = discord.Embed(
        title="SG 週間レポート",
        description=description,
        color=COLOR_DEFAULT,
    )
    embed.add_field(
        name="期間",
        value=f"{data['start_date']} 〜 {data['end_date']}",
        inline=False,
    )
    embed.add_field(
        name="勉強時間", value=format_minutes(data["total_seconds"])
    )
    embed.add_field(name="問題数", value=f"{data['total_questions']}問")
    embed.add_field(name="平均正答率", value=data["score_text"])
    return embed


async def create_weekly_report(user_id):
    """今週の週報の (集計, AIの文章, AIのエラー)。記録がなければ None。

    AIが使えないときは文章が None になり、数字だけで週報を作れる。
    """
    data = collect_weekly_report(user_id)
    if data is None:
        return None

    try:
        answer = await ask_ollama(build_weekly_report_prompt(data))
    except aiohttp.ClientConnectorError:
        return data, None, "Ollamaに接続できません"
    except Exception as e:
        print(f"[report] AIの処理に失敗: {e}")
        return data, None, "AIの処理に失敗"
    return data, answer, None


async def create_weekly_report_embed(user_id):
    """今週の週報の Embed。記録がなければ None。"""
    report = await create_weekly_report(user_id)
    if report is None:
        return None
    return build_weekly_report_embed(*report)


@ai_group.command(
    name="report",
    description="今週のSG学習レポートをAIが作成"
)
@app_commands.describe(
    notion="Notionにも週ページとして保存する（.env の設定が必要）"
)
async def report(ctx, notion: bool = False):
    async with ctx.typing():
        report_data = await create_weekly_report(ctx.author.id)
        if report_data is None:
            await ctx.send(
                "📊 今週はまだ週報を作れる"
                "学習記録がありません。"
            )
            return

        content = None
        if notion:
            if is_notion_configured():
                content = await save_weekly_report_to_notion(
                    ctx.author.id, *report_data,
                    datetime.now(JST).date(),
                )
            else:
                content = (
                    "Notionの設定がまだです。.env に NOTION_TOKEN と "
                    "NOTION_PAGE_ID を書いてから再起動してください。"
                )

    kwargs = {"embed": build_weekly_report_embed(*report_data)}
    if content:
        kwargs["content"] = content
    await ctx.send(**kwargs)


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
