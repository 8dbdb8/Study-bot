"""/plan グループ：週次計画・朝の通知設定・ロードマップ。"""

from datetime import datetime


import aiohttp
from discord import app_commands

from studybot import config
from studybot.channels import channel_label
from studybot.config import (
    DIGEST_TIME_HOLIDAY,
    DIGEST_TIME_WEEKDAY,
    JST,
    WEEKLY_REPORT_TIME,
)
from studybot.daily_digest import is_digest_enabled, set_digest_enabled
from studybot.embeds import build_plan_status_embed
from studybot.exam_schedule import (
    format_exam_countdown,
    format_japanese_date,
    format_plan_schedule,
    strip_week_heading_dates,
    weeks_until,
)
from studybot.formatting import (
    format_category_label,
    format_duration,
    get_review_candidates,
)
from studybot.groups import plan_group
from studybot.ollama import ask_ollama
from studybot.replies import send_private
from studybot.sg_features import (
    get_sg_plan_status,
    save_sg_plan,
    update_sg_plan_text,
)
from studybot.stats import (
    get_current_exam_target,
    get_exam_countdown_line,
    get_roadmap,
    get_study_status,
    get_week_total_seconds,
)
from studybot.weekly_report import (
    is_weekly_report_enabled,
    set_weekly_report_enabled,
)


@plan_group.command(
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

    countdown = get_exam_countdown_line(ctx.author.id)
    if countdown:
        footer += f"\n📆 {countdown}"
    elif current_name:
        footer += "\n📆 試験日は `/plan exam` で設定できます。"

    await ctx.send(
        "🗺️ **資格取得ロードマップ**\n\n"
        f"{roadmap_text}"
        f"{footer}"
    )


@plan_group.command(
    name="new",
    description="SG合格までの週次学習計画を作成"
)
@app_commands.describe(
    weeks="試験までの残り週数（1〜16週）。省略すると/plan examの試験日から計算",
    weekly_questions="1週間の目標問題数（省略時30問）",
)
async def plan(
    ctx, weeks: int | None = None, weekly_questions: int = 30
):
    today_date = datetime.now(JST).date()
    exam_on = get_current_exam_target(ctx.author.id)["exam_on"]
    if exam_on is not None and exam_on < today_date:
        exam_on = None

    plan_note = ""
    if weeks is None:
        if exam_on is None:
            await ctx.send(
                "試験日が未設定です。`/plan exam` で試験日を選ぶか、"
                "`/plan new weeks:6` のように残り週数を指定してください。"
            )
            return
        weeks = weeks_until(exam_on, today_date)
        if weeks > 16:
            plan_note = (
                f"試験日まで{weeks}週ありますが、"
                "計画は直近16週分で作ります。\n"
            )
            weeks = 16

    if not 1 <= weeks <= 16:
        await ctx.send(
            "⚠️ 残り週数は1〜16週で指定してください。\n"
            "例：`/plan new weeks:6` または `!plan new 6`"
        )
        return

    if not 1 <= weekly_questions <= 500:
        await ctx.send("週目標は1〜500問で指定してください。")
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

    exam_text = (
        f"\n【試験日】{format_japanese_date(exam_on)}"
        f"（あと{(exam_on - today_date).days}日）\n"
        if exam_on is not None
        else ""
    )
    schedule_text = format_plan_schedule(today_date, weeks, exam_on)

    prompt = f"""
情報セキュリティマネジメント（SG）を
残り{weeks}週間で合格するための学習計画を作成してください。
{exam_text}
【各週の期間（Botが計算済み）】
{schedule_text}

【学習方法】
情報セキュリティマネジメント過去問道場を中心に学習する。
毎週の基本目標は{weekly_questions}問。
未達の場合、翌週は不足分のうち最大{weekly_questions // 2}問を上乗せする。

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
- 週の見出しは「第1週」のように番号だけにし、日付や曜日は書かない
- 各週に「基本目標{weekly_questions}問」「学習内容」「確認ポイント」を書く
- 翌週の上乗せは実績確定後にBotが計算するので、将来の実績を推測しない
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

    plan_id = save_sg_plan(
        config.DB_PATH,
        ctx.author.id,
        weeks,
        weekly_questions,
        datetime.now(JST).isoformat(),
        today=datetime.now(JST).date(),
    )

    async with ctx.typing():
        try:
            answer = strip_week_heading_dates(await ask_ollama(prompt))

            update_sg_plan_text(config.DB_PATH, plan_id, answer)

            await ctx.send(
                f"🗓️ **SG合格まで{weeks}週間の計画**\n"
                + (
                    format_exam_countdown(exam_on, today_date, "SG試験")
                    + "\n"
                    if exam_on is not None else ""
                )
                + f"{plan_note}\n"
                f"**各週の期間**\n{schedule_text}\n\n"
                f"基本目標：毎週{weekly_questions}問。"
                "達成状況は `/plan status` で確認できます。\n\n"
                f"{answer}"
            )

        except aiohttp.ClientConnectorError:
            await ctx.send(
                f"毎週{weekly_questions}問の数値目標を保存しました。"
                "`/plan status` で達成状況を確認できます。\n"
                "Ollamaに接続できなかったため、文章の計画は未生成です。"
            )

        except Exception as e:
            print(f"❌ plan エラー: {e}")

            await ctx.send(
                f"毎週{weekly_questions}問の数値目標を保存しました。"
                "`/plan status` で達成状況を確認できます。\n"
                "文章の計画は作成できませんでした。"
            )


@plan_group.command(
    name="status",
    description="週次計画の目標と実績を確認"
)
async def plan_status(ctx):
    status_data = get_sg_plan_status(
        config.DB_PATH, ctx.author.id, today=datetime.now(JST).date()
    )
    if status_data is None:
        await ctx.send(
            "保存済みのSG計画がありません。"
            "`/plan new weeks:6 weekly_questions:30` で作成できます。"
        )
        return

    await ctx.send(embed=build_plan_status_embed(
        status_data, get_exam_countdown_line(ctx.author.id)
    ))


ON_OFF_CHOICES = [
    app_commands.Choice(name="オン", value="on"),
    app_commands.Choice(name="オフ", value="off"),
]


def _notify_schedule_text(guild):
    channel = channel_label(guild, "study_log")
    return (
        f"・学習メニュー：平日 {DIGEST_TIME_WEEKDAY.strftime('%H:%M')} / "
        f"土日祝 {DIGEST_TIME_HOLIDAY.strftime('%H:%M')} に {channel} へ"
        "（復習・週目標・試験日のどれもない日は送りません）\n"
        f"・週間レポート：毎週日曜 {WEEKLY_REPORT_TIME.strftime('%H:%M')} に "
        f"{channel} へ（その週に勉強の記録がある場合）"
    )


@plan_group.command(
    name="notify",
    description="学習メニューと週間レポートの自動通知をオン・オフ"
)
@app_commands.describe(
    menu="学習メニュー（平日の夜・土日祝の朝）",
    report="週間レポート（日曜の夜）",
)
@app_commands.choices(menu=ON_OFF_CHOICES, report=ON_OFF_CHOICES)
async def plan_notify(ctx, menu: str | None = None, report: str | None = None):
    for value in (menu, report):
        if value not in (None, "on", "off"):
            await send_private(ctx, "on か off を指定してください。")
            return

    if menu is not None:
        set_digest_enabled(config.DB_PATH, ctx.author.id, menu == "on")
    if report is not None:
        set_weekly_report_enabled(
            config.DB_PATH, ctx.author.id, report == "on"
        )

    def state(enabled):
        return "オン" if enabled else "オフ"

    lines = []
    if menu is None and report is None:
        lines.append(
            "`/plan notify menu:オフ` や `report:オン` で切り替えられます。"
        )
    else:
        lines.append("通知の設定を変更しました。")
    lines.append(
        f"学習メニュー：**{state(is_digest_enabled(config.DB_PATH, ctx.author.id))}**"
        f"　週間レポート：**{state(is_weekly_report_enabled(config.DB_PATH, ctx.author.id))}**"
    )
    lines.append(_notify_schedule_text(ctx.guild))
    await send_private(ctx, "\n".join(lines))
