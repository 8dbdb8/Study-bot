"""資格ごとのコマンド（/sg・/fe・/iryo）：記録・進捗・グラフ・累積成績。

コマンドの中身は資格を受け取る関数として1つだけ書き、
qualifications.py に並べた資格ごとに /{資格} log などを作る。
"""

import asyncio
import unicodedata
from datetime import datetime, timedelta
from io import BytesIO

import discord
from discord import app_commands

from studybot import config
from studybot.charts import render_mock_chart, render_score_chart
from studybot.config import JST, REVIEW_SCORE_THRESHOLD
from studybot.embeds import COLOR_DEFAULT, COLOR_SUCCESS, build_progress_embed
from studybot.formatting import format_category_label, get_review_candidates
from studybot.features.review import build_review_session
from studybot.activity_forms import build_study_log_prompt
from studybot.forms import build_sgb_prompt, open_quick_log
from studybot.features import mock_timer
from studybot.features.badges import announce_new_badges
from studybot.features.focus import notice_channel
from studybot.groups import QUALIFICATION_GROUPS
from studybot.qualifications import QUALIFICATIONS, get_qualification
from studybot.replies import respond_private, send_png, send_private
from studybot.scoring import (
    DISCLAIMER,
    MIN_PREDICTION_QUESTIONS,
    format_margin,
    get_mock_timer,
    list_mock_exams,
    predict_score,
    prediction_summary,
    save_mock_exam,
)
from studybot.speed import SPEED_DAYS, format_speed, speed_summary
from studybot.sg_features import (
    format_reason_breakdown,
    get_reason_breakdown,
    get_sg_b_summary,
    get_sg_category_progress,
    get_sg_mistakes,
    get_weak_categories,
)
from studybot.stats import get_daily_scores, get_study_status


async def record_log(ctx, qualification):
    # まず「過去問道場・復習・単語帳」のどれかを選び、選んだものの入力フォームを開く
    message, view = build_study_log_prompt(ctx.guild, ctx.author.id, qualification)
    await send_private(ctx, message, view)


async def record_part_b(ctx, qualification):
    message, view = build_sgb_prompt(ctx.guild, ctx.author.id, qualification)
    await send_private(ctx, message, view)


class WeakReviewView(discord.ui.View):
    """進捗の下に付けるボタン。弱い分野の誤答を解き直す。"""

    def __init__(self, owner_id, qualification):
        super().__init__(timeout=30 * 60)
        self.owner_id = owner_id
        self.qualification = qualification

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "このボタンは本人専用です。", ephemeral=True
        )
        return False

    @discord.ui.button(label="弱い分野を復習", style=discord.ButtonStyle.primary)
    async def weak_review_button(self, interaction, button):
        code = self.qualification.code
        weak = get_weak_categories(
            config.DB_PATH, self.owner_id, code, REVIEW_SCORE_THRESHOLD
        )
        weak_names = {category for category, _ in weak}
        items = [
            item for item in get_sg_mistakes(
                config.DB_PATH, self.owner_id, due_only=False,
                qualification=code,
            )
            if item["category"] in weak_names
        ]
        if items:
            # 弱い分野の誤答は、復習日を待たずにまとめて解き直す
            embed, view = build_review_session(
                self.owner_id, datetime.now(JST).date(), items
            )
            await respond_private(
                interaction, embed=embed, view=view, files=view.files()
            )
            return
        # 誤答がなければ、一番弱い分野で問題を解いて記録する
        weakest = get_weak_categories(
            config.DB_PATH, self.owner_id, code, threshold=None
        )
        await open_quick_log(
            interaction, self.qualification, weakest[0][0] if weakest else None
        )


async def show_progress(ctx, qualification):
    items, unclassified = get_sg_category_progress(
        config.DB_PATH, ctx.author.id, qualification.code
    )
    embed = build_progress_embed(
        items,
        unclassified,
        get_sg_b_summary(config.DB_PATH, ctx.author.id, qualification.code),
        REVIEW_SCORE_THRESHOLD,
        qualification,
        get_study_status(ctx.author.id, qualification.code)["average_score"],
    )
    today = datetime.now(JST).date()
    reasons = format_reason_breakdown(get_reason_breakdown(
        config.DB_PATH, ctx.author.id,
        today - timedelta(days=REASON_BREAKDOWN_DAYS - 1), today,
        qualification.code,
    ))
    if reasons:
        embed.add_field(
            name=f"間違え方（直近{REASON_BREAKDOWN_DAYS}日の誤答）",
            value=reasons, inline=False,
        )
    speed = format_speed(
        speed_summary(config.DB_PATH, ctx.author.id, qualification.code, today),
        qualification.code,
    )
    if speed:
        embed.add_field(
            name=f"解く速さ（直近{SPEED_DAYS}日）", value=speed, inline=False
        )
    await ctx.send(embed=embed, view=WeakReviewView(ctx.author.id, qualification))


# /sg progress に出す「間違え方」の集計期間
REASON_BREAKDOWN_DAYS = 28


async def show_status(ctx, qualification):
    status_data = get_study_status(ctx.author.id, qualification.code)
    total_questions = status_data["total_questions"]
    average_score = status_data["average_score"]
    category_status = status_data["category_status"]
    log_count = status_data["log_count"]

    if log_count == 0:
        await ctx.send(
            f"📊 **{qualification.code} 学習状況**\n"
            "まだ解析済みの勉強ログが"
            "ありません。"
        )
        return

    score_text = (
        f"{average_score:.1f}%" if average_score is not None else "記録なし"
    )

    if category_status:
        category_lines = []
        for item in category_status:
            label = format_category_label(item)
            if item["average_score"] is not None:
                value = f"{item['average_score']:.1f}%"
            else:
                value = f"記録{item['log_count']}回"
            category_lines.append(f"- {label}：{value}")
        category_text = "\n".join(category_lines)
    else:
        category_text = "まだ記録なし"

    review_candidates = get_review_candidates(
        category_status,
        "average_score"
    )
    if review_candidates:
        review_text = "\n".join(
            f"- {format_category_label(item)}："
            f"{item['average_score']:.1f}% "
            f"（{'重点復習' if item['scored_log_count'] >= 2 else '候補'}）"
            for item in review_candidates
        )
    else:
        review_text = "現在はなし"

    embed = discord.Embed(
        title=f"{qualification.code} 学習状況",
        color=COLOR_DEFAULT,
    )
    embed.add_field(name="解析済みログ", value=f"{log_count}件")
    embed.add_field(name="解いた問題", value=f"{total_questions}問")
    embed.add_field(name="平均正答率", value=score_text)
    embed.add_field(
        name="分野別成績", value=category_text[:1024], inline=False
    )
    embed.add_field(
        name="要復習候補", value=review_text[:1024], inline=False
    )
    add_prediction_field(
        embed, ctx.author.id, qualification, datetime.now(JST).date()
    )
    await ctx.send(embed=embed)


def add_prediction_field(embed, user_id, qualification, today):
    """予想得点の欄。記録が足りなければ、その旨を書く。"""
    prediction = predict_score(config.DB_PATH, user_id, qualification, today)
    if prediction is None:
        embed.add_field(
            name="予想得点",
            value=(
                f"直近4週間に科目Aを{MIN_PREDICTION_QUESTIONS}問以上解くと、"
                "予想得点を表示します。"
            ),
            inline=False,
        )
        return
    lines = [f"**{prediction_summary(prediction, qualification)}**"]
    lines.append(" ・ ".join(
        f"{name} 正答率{rate:.0f}%（{count}問）"
        for name, rate, count in prediction["parts"]
    ))
    if prediction["hint"]:
        lines.append(f"伸びしろ：{prediction['hint']}")
    if prediction["note"]:
        lines.append(prediction["note"])
    lines.append(DISCLAIMER)
    embed.add_field(name="予想得点", value="\n".join(lines), inline=False)


# ============================================================
# 模試（本番形式）の記録
# ============================================================

class MockExamModal(discord.ui.Modal):
    """模試の正解数と時間を入れる。問題数が決まっていない区分は問題数も入れる。"""

    def __init__(self, qualification, minutes_default=None):
        super().__init__(title=f"{qualification.code} 模試（本番形式）を記録"[:45])
        self.qualification = qualification
        self.part_inputs = []
        for name, total in qualification.exam_parts:
            correct = discord.ui.TextInput(placeholder="例：33", max_length=3)
            label = f"{name}の正解数" + (f"（{total}問中）" if total else "")
            self.add_item(discord.ui.Label(text=label, component=correct))
            total_input = None
            if not total:
                total_input = discord.ui.TextInput(
                    placeholder="例：60", max_length=3
                )
                self.add_item(discord.ui.Label(
                    text=f"{name}の問題数", component=total_input
                ))
            self.part_inputs.append((total, correct, total_input))
        placeholder = (
            f"本番は{qualification.exam_minutes}分（空欄でも可）"
            if qualification.exam_minutes else "空欄でも可"
        )
        self.minutes_input = discord.ui.TextInput(
            placeholder=placeholder, required=False, max_length=3,
            default=str(minutes_default) if minutes_default else None,
        )
        self.add_item(discord.ui.Label(
            text="かかった時間（分）", component=self.minutes_input
        ))

    def parse(self):
        """[(正解数, 問題数)], 分。入力が数字でなければ ValueError。"""
        def number(text, label):
            text = unicodedata.normalize("NFKC", text or "").strip()
            if not text.isdecimal():
                raise ValueError(f"{label}は数字で入力してください。")
            return int(text)

        parts = []
        for total, correct_input, total_input in self.part_inputs:
            count = total or number(total_input.value, "問題数")
            parts.append((number(correct_input.value, "正解数"), count))
        minutes_text = (self.minutes_input.value or "").strip()
        minutes = number(minutes_text, "時間") if minutes_text else None
        return parts, minutes

    async def on_submit(self, interaction):
        try:
            parts, minutes = self.parse()
            _, score = save_mock_exam(
                config.DB_PATH, interaction.user.id, self.qualification,
                datetime.now(JST).date(), parts, minutes,
            )
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        await interaction.response.defer()
        embed, png = await build_mock_result(
            interaction.user.id, self.qualification, parts, minutes, score
        )
        await interaction.followup.send(
            embed=embed, file=discord.File(BytesIO(png), filename="mock.png")
        )
        await announce_new_badges(interaction.guild, interaction.user.id)


async def build_mock_result(user_id, qualification, parts, minutes, score):
    """模試の結果の Embed と、これまでの得点のグラフ。"""
    history = list_mock_exams(config.DB_PATH, user_id, qualification.code)
    margin = (
        score - qualification.pass_score
        if qualification.pass_score is not None else None
    )
    title = f"{qualification.code} 模試 第{len(history)}回 ・ {score}点"
    if margin is not None:
        title += f"（{format_margin(margin)}）"
    embed = discord.Embed(
        title=title,
        color=COLOR_SUCCESS if margin is not None and margin >= 0 else COLOR_DEFAULT,
    )
    for (name, _), (correct, total) in zip(qualification.exam_parts, parts):
        embed.add_field(name=name, value=f"{correct}/{total}問")
    if minutes is not None:
        limit = f"/{qualification.exam_minutes}" if qualification.exam_minutes else ""
        embed.add_field(name="時間", value=f"{minutes}{limit}分")
    if qualification.scoring == "separate":
        embed.description = "科目ごとに600点以上が必要なため、低い方の点数を表示しています。"
    embed.set_image(url="attachment://mock.png")

    mocks = [
        {"label": f"{int(m['taken_on'][5:7])}/{int(m['taken_on'][8:10])}",
         "score": m["score"]}
        for m in history[-10:]
    ]
    png = await asyncio.to_thread(
        render_mock_chart, mocks, f"{qualification.code} 模試の得点",
        qualification.pass_score,
    )
    return embed, png


class MockFinishView(discord.ui.View):
    """模試タイマーの「解き終わった」ボタン。再起動後も押せる。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="解き終わった・結果を入力",
        style=discord.ButtonStyle.primary,
        custom_id="studybot:mock:finish",
    )
    async def finish_button(self, interaction, button):
        timer = get_mock_timer(config.DB_PATH, interaction.user.id)
        qualification = get_qualification(timer["qualification"]) if timer else None
        if qualification is None:
            await respond_private(
                interaction,
                "動いている模試タイマーはありません。"
                "結果は `/sg mock`（FEは `/fe mock`）から入力できます。",
            )
            return
        minutes = mock_timer.elapsed_minutes(timer)
        mock_timer.stop_mock_timer(interaction.user.id)
        await interaction.response.send_modal(
            MockExamModal(qualification, minutes_default=minutes)
        )


def _mock_timer_running(user_id):
    if user_id in mock_timer.MOCK_TIMERS:
        return True
    timer = get_mock_timer(config.DB_PATH, user_id)
    return timer is not None and mock_timer.elapsed_minutes(timer) < timer["minutes"]


async def record_mock(ctx, qualification, timer=None):
    if timer == "start":
        await start_mock(ctx, qualification)
        return
    if timer == "stop":
        stopped = mock_timer.stop_mock_timer(ctx.author.id)
        if stopped is None:
            await send_private(ctx, "動いている模試タイマーはありません。")
        else:
            await send_private(
                ctx,
                f"⏹ 模試タイマーを止めました（{mock_timer.elapsed_minutes(stopped)}分経過）。",
            )
        return
    if getattr(ctx, "interaction", None) is not None:
        await ctx.interaction.response.send_modal(MockExamModal(qualification))
        return
    await ctx.send(
        f"模試の記録は `/{qualification.command} mock` から入力してください。"
    )


async def start_mock(ctx, qualification):
    if not qualification.exam_minutes:
        await send_private(
            ctx, f"{qualification.display_name}は本番の時間が決まっていないため、タイマーを使えません。"
        )
        return
    if _mock_timer_running(ctx.author.id):
        await send_private(
            ctx,
            "模試タイマーはすでに動いています。"
            f"止めるときは `/{qualification.command} mock timer:止める` を使ってください。",
        )
        return
    channel = notice_channel(ctx.guild, ctx.channel)
    mock_timer.start_mock_timer(channel, ctx.author.id, qualification, MockFinishView)
    text = mock_timer.timer_start_text(qualification)
    if channel.id != ctx.channel.id:
        text += f"\nお知らせは {channel.mention} に届きます。"
    await ctx.send(text, view=MockFinishView())


async def show_chart(ctx, qualification, category="all"):
    if category != "all" and category not in qualification.category_names:
        await ctx.send("分野は候補から選んでください。")
        return

    points = get_daily_scores(
        ctx.author.id,
        qualification.code,
        None if category == "all" else category,
    )
    if not points:
        await ctx.send(
            f"グラフにできる{qualification.code}の記録がまだありません。"
        )
        return

    label = "全体" if category == "all" else category
    async with ctx.typing():
        png = await asyncio.to_thread(
            render_score_chart,
            points,
            f"{qualification.code} 正答率の推移（{label}）",
            REVIEW_SCORE_THRESHOLD,
        )
    await send_png(ctx, png, f"{qualification.command}_score.png")


def add_qualification_commands(qualification, group):
    """/{資格} log などのサブコマンドを作り、名前→コマンドの辞書を返す。"""
    code = qualification.code
    created = {}

    @group.command(
        name="log",
        description=(
            f"勉強を記録（{qualification.practice_source}・復習・単語帳）"
        )[:100],
    )
    async def log_command(ctx):
        await record_log(ctx, qualification)

    created["log"] = log_command

    if qualification.has_part_b:
        @group.command(name="b", description=f"{code}科目Bの演習結果を記録")
        async def part_b_command(ctx):
            await record_part_b(ctx, qualification)

        created["b"] = part_b_command

    progress_text = (
        f"{code}の{len(qualification.categories)}分野"
        + ("と科目B" if qualification.has_part_b else "")
        + "の進捗を表示"
    )

    @group.command(name="progress", description=progress_text)
    async def progress_command(ctx):
        await show_progress(ctx, qualification)

    created["progress"] = progress_command

    @group.command(
        name="chart",
        description=f"{code}の正答率と問題数の推移をグラフで表示",
    )
    @app_commands.describe(category="分野（省略すると全体）")
    @app_commands.choices(category=[
        app_commands.Choice(name="全体", value="all"),
        *(
            app_commands.Choice(name=name, value=name)
            for name in qualification.category_names
        ),
    ])
    async def chart_command(ctx, category: str = "all"):
        await show_chart(ctx, qualification, category)

    created["chart"] = chart_command

    @group.command(name="status", description=f"{code}の累積学習状況を表示")
    async def status_command(ctx):
        await show_status(ctx, qualification)

    created["status"] = status_command

    @group.command(
        name="mock",
        description=f"{code}の模試（本番形式）の結果を記録して推移を表示",
    )
    @app_commands.describe(timer="本番と同じ時間を計るタイマー（省略すると結果の入力）")
    @app_commands.choices(timer=[
        app_commands.Choice(name="開始", value="start"),
        app_commands.Choice(name="止める", value="stop"),
    ])
    async def mock_command(ctx, timer: str | None = None):
        await record_mock(ctx, qualification, timer)

    created["mock"] = mock_command
    return created


# 資格ごとのコマンド：COMMANDS["FE"]["log"] のように取り出せる
COMMANDS = {
    qualification.code: add_qualification_commands(
        qualification, QUALIFICATION_GROUPS[qualification.code]
    )
    for qualification in QUALIFICATIONS
}
