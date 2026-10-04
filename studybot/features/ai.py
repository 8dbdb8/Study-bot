"""/ai グループ：AIコーチによる分析と提案。"""

from datetime import date, datetime

import aiohttp
import discord
from discord import app_commands

from studybot import config
from studybot.config import JST, REVIEW_SCORE_THRESHOLD
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
from studybot.ai_check import checked_answer
from studybot.groups import ai_group
from studybot.habits import format_daily_notes, get_daily_notes, get_rest_days
from studybot.scoring import predict_score, prediction_summary
from studybot.sg_features import (
    format_reason_breakdown,
    get_reason_breakdown,
    get_weak_categories,
)
from studybot.qualifications import SG, current_qualification, get_qualification
from studybot.replies import send_long
from studybot.ollama import ask_ollama
from studybot.stats import (
    get_category_status,
    get_study_streak_safe,
    get_roadmap,
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
    qualification = current_qualification(config.DB_PATH)

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
        qualification.code
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
{qualification.display_name}

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
- ユーザーは{qualification.practice_source}を中心に勉強している

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

            answer = checked_answer(answer, prompt)
            await send_long(ctx, f"🎯 **次の勉強メニュー**\n\n{answer}")

        except Exception as e:
            print(
                f"❌ next エラー: {e}"
            )

            await ctx.send(
                "⚠️ 次の勉強メニューを"
                "作成できませんでした。\n"
                "studybot.log を"
                "確認してください。"
            )


def collect_weekly_report(user_id, qualification="SG", today=None):
    """today を含む週（月曜〜today）の集計。週報にできる記録がなければ None。"""
    week_rows = get_week_total(user_id, today)
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

    report_status = get_week_analysis_status(user_id, qualification, today)
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

    facts_text, prediction_text = build_weekly_facts(
        user_id, qualification, report_status, week_rows,
        today or datetime.now(JST).date(),
    )
    reason_breakdown = format_reason_breakdown(get_reason_breakdown(
        config.DB_PATH, user_id,
        date.fromisoformat(report_status["start_date"]),
        date.fromisoformat(report_status["end_date"]),
        qualification,
    ))
    if reason_breakdown:
        facts_text += f"\n- 今週登録した誤答の理由：{reason_breakdown}"
    notes_text = format_daily_notes(get_daily_notes(
        config.DB_PATH, user_id,
        date.fromisoformat(report_status["start_date"]),
        date.fromisoformat(report_status["end_date"]),
    ))

    return {
        "facts_text": facts_text,
        "prediction_text": prediction_text,
        "reason_text": reason_breakdown,
        "notes_text": notes_text,
        "start_date": report_status["start_date"],
        "end_date": report_status["end_date"],
        "total_seconds": total_seconds,
        "daily_text": daily_text,
        "log_count": log_count,
        "total_questions": total_questions,
        "score_text": score_text,
        "category_text": category_text,
        "review_text": review_text,
        "qualification": qualification,
    }


def build_weekly_facts(user_id, qualification_code, report_status, week_rows,
                       today):
    """AIに「確定した事実」として渡す、Botが計算した内容。"""
    start = datetime.strptime(report_status["start_date"], "%Y-%m-%d").date()
    end = datetime.strptime(report_status["end_date"], "%Y-%m-%d").date()
    period_days = (end - start).days + 1
    studied_days = sum(1 for _, seconds in week_rows if seconds > 0)
    rest_days = len(get_rest_days(config.DB_PATH, user_id, start, end))
    qualification = get_qualification(qualification_code) or SG
    prediction = predict_score(config.DB_PATH, user_id, qualification, today)
    weak = get_weak_categories(
        config.DB_PATH, user_id, qualification.code, REVIEW_SCORE_THRESHOLD
    )
    lines = [
        f"- 集計期間：{period_days}日間（{start.month}/{start.day}〜{end.month}/{end.day}）",
        f"- 勉強部屋で勉強した日：{studied_days}日",
        f"- 「今日は休む」で休みにした日：{rest_days}日",
        f"- 連続学習日数：{get_study_streak_safe(user_id, end)}日",
        "- 正答率60%未満の分野："
        + ("、".join(f"{name}（{score:.1f}%）" for name, score in weak) if weak else "なし"),
        "- 60.0%ちょうどは「60%未満」ではない",
    ]
    summary = prediction_summary(prediction, qualification)
    if summary:
        lines.append(f"- {summary}（目安）")
    return "\n".join(lines), summary


def build_weekly_report_prompt(data):
    qualification = (
        get_qualification(data.get("qualification", "SG")) or SG
    )
    return f"""
今週の資格勉強について週報を作成してください。

【対象資格】
{qualification.display_name}

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

【確定した事実（Botの集計）】
{data.get("facts_text", "（なし）")}

【今週のひとこと（本人が書いたメモ）】
{data.get("notes_text") or "なし"}

【データの読み方に関する重要ルール】
- 数字や日数は、上の記録と【確定した事実】に書かれたものだけを使う
- 新しい数字を計算したり、推測で数字を作ったりしない
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
誤答の理由の内訳があれば、多い理由への対策を1つ入れる。
根拠がなければ断定しない。

### 来週の方針
AIからの提案として3項目以内で提案する。
{qualification.practice_source}を中心に、
要復習候補と総合問題をバランスよく提案する。
"""


# Embed の本文の上限（4096文字）に余裕を持たせる
REPORT_TEXT_LIMIT = 3900


def build_weekly_report_embed(data, answer=None, ai_error=None):
    if answer:
        # 「---」の区切り線は Embed ではそのまま文字で出るので消す
        description = "\n".join(
            line for line in answer.strip().splitlines()
            if line.strip() not in ("---", "***", "___")
        )
        if len(description) > REPORT_TEXT_LIMIT:
            description = description[:REPORT_TEXT_LIMIT].rstrip() + "\n…（長いため省略）"
    else:
        description = (
            "AIのコメントは作れませんでした"
            + (f"（{ai_error}）" if ai_error else "")
            + "。数字のまとめだけお送りします。"
        )

    embed = discord.Embed(
        title=f"{data.get('qualification', 'SG')} 週間レポート",
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
    if data.get("prediction_text"):
        embed.add_field(
            name="予想得点（目安）", value=data["prediction_text"], inline=False
        )
    if data.get("reason_text"):
        embed.add_field(
            name="今週の間違え方", value=data["reason_text"], inline=False
        )
    if data.get("notes_text"):
        notes = data["notes_text"]
        if len(notes) > 1000:
            notes = notes[:1000].rstrip() + "…"
        embed.add_field(name="今週のひとこと", value=notes, inline=False)
    return embed


async def create_weekly_report(user_id, today=None):
    """週報の (集計, AIの文章, AIのエラー)。記録がなければ None。

    today を含む週（省略すると今週）。AIが使えないときは文章が None になり、
    数字だけで週報を作れる。
    """
    data = collect_weekly_report(
        user_id, current_qualification(config.DB_PATH).code, today
    )
    if data is None:
        return None

    prompt = build_weekly_report_prompt(data)
    try:
        answer = checked_answer(await ask_ollama(prompt), prompt)
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
    description="今週の学習レポートをAIが作成"
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
    qualification = current_qualification(config.DB_PATH)
    today_categories = get_category_status(
        ctx.author.id,
        qualification.code,
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

    roadmap_text = "\n".join(
        f"{sort_order}. {display_name}"
        + {"completed": "（合格済み）", "learning": "（学習中）"}.get(status, "")
        for _, display_name, sort_order, status, _ in get_roadmap()
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
{roadmap_text}

現在は{qualification.display_name}を
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

            answer = checked_answer(answer, prompt)
            await send_long(ctx, f"🤖 **Study Coach**\n\n{answer}")

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
                "studybot.log を"
                "確認してください。"
            )
