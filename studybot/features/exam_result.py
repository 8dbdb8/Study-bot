"""試験が終わったら結果をたずね、合格ならロードマップを次の資格へ進める。

試験日から最大7日間、毎晩20時に「結果はどうでしたか？」と聞く。
ボタンで答えると結果を記録し、合格なら次の資格を学習中にして
試験日の設定へ、不合格なら再受験日の設定へ案内する。
"""

import asyncio
from datetime import date, datetime, time

import discord
from discord.ext import tasks

from studybot import config
from studybot.config import JST
from studybot.embeds import COLOR_DEFAULT, COLOR_SUCCESS, format_minutes
from studybot.exam_results import (
    advance_roadmap,
    parse_exam_score,
    get_pending_exam,
    get_result_prompt_candidates,
    get_study_summary,
    mark_result_prompted,
    record_exam_result,
)
from studybot.features.digest import find_home_channel
from studybot.features.exam_prep import send_exam_prep_prompts
from studybot.features.exam import ExamDateView
from studybot.replies import respond_private
from studybot.stats import get_current_exam_target, get_roadmap, get_study_status


# 結果をたずねる時刻
RESULT_PROMPT_TIME = time(20, 0)

ROADMAP_MARKS = {"completed": "✅", "learning": "▶", "pending": "・", "paused": "⏸"}


def _exam_label(qualification):
    return f"{qualification}試験"


def build_roadmap_text():
    lines = []
    for qualification, display_name, sort_order, status, is_current in get_roadmap():
        mark = ROADMAP_MARKS.get(status, "・")
        suffix = {"completed": "　合格済み", "learning": "　学習中"}.get(status, "")
        name = f"**{display_name}**" if is_current else display_name
        lines.append(f"{mark} {sort_order}. {name}{suffix}")
    return "\n".join(lines) or "ロードマップがまだ登録されていません。"


def build_pass_embed(user_id, qualification, exam_on, next_qualification,
                     score=None):
    first_day, study_days, seconds = get_study_summary(
        config.DB_PATH, user_id, exam_on
    )
    total_questions = get_study_status(user_id, qualification)["total_questions"]

    embed = discord.Embed(
        title=f"{qualification}合格おめでとうございます！",
        description="この記録は「合格までの記録」として残ります。",
        color=COLOR_SUCCESS,
    )
    if score is not None:
        embed.add_field(name="得点", value=f"{score}点")
    if first_day:
        days = (date.fromisoformat(exam_on) - date.fromisoformat(first_day)).days + 1
        embed.add_field(name="学習期間", value=f"{days}日（勉強した日 {study_days}日）")
    embed.add_field(name="解いた問題", value=f"{total_questions}問")
    embed.add_field(name="勉強時間", value=format_minutes(seconds))
    embed.add_field(name="資格取得ロードマップ", value=build_roadmap_text(), inline=False)
    if next_qualification is not None:
        embed.set_footer(
            text=f"次は {next_qualification[1]} を学習中にしました。"
        )
    else:
        embed.set_footer(text="ロードマップの資格はすべて合格です！")
    return embed


def build_fail_embed(qualification):
    return discord.Embed(
        title=f"{qualification}試験、おつかれさまでした",
        description=(
            "今回の記録や誤答は、そのまま次の挑戦に使えます。\n"
            "少し休んだら、再受験日を決めて立て直しましょう。"
        ),
        color=COLOR_DEFAULT,
    )


class _OwnerView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=24 * 60 * 60)
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "このボタンは本人専用です。", ephemeral=True
        )
        return False

    async def open_exam_date(self, interaction):
        view = ExamDateView(
            self.owner_id,
            get_current_exam_target(self.owner_id),
            datetime.now(JST).date(),
        )
        await respond_private(interaction, view.content(), view)


class NextStepView(_OwnerView):
    def __init__(self, owner_id, next_name=None):
        super().__init__(owner_id)
        if next_name:
            # 例：「基本情報技術者（FE）の試験日を設定」
            self.next_exam_button.label = f"{next_name}の試験日を設定"[:80]

    @discord.ui.button(label="次の試験日を設定", style=discord.ButtonStyle.primary)
    async def next_exam_button(self, interaction, button):
        await self.open_exam_date(interaction)

    @discord.ui.button(label="少し休む", style=discord.ButtonStyle.secondary)
    async def rest_button(self, interaction, button):
        await interaction.response.edit_message(view=None)
        await interaction.followup.send(
            "ゆっくり休んでください。準備ができたら `/plan exam` で"
            "次の試験日を設定できます。",
            ephemeral=True,
        )


class RetakeView(_OwnerView):
    @discord.ui.button(label="再受験日を設定", style=discord.ButtonStyle.primary)
    async def retake_button(self, interaction, button):
        await self.open_exam_date(interaction)


class ExamResultView(discord.ui.View):
    """結果をたずねるメッセージのボタン。再起動後も押せる。"""

    def __init__(self):
        super().__init__(timeout=None)

    async def _answer(self, interaction, result):
        await answer_exam_result(interaction, result)

    @discord.ui.button(
        label="合格した",
        style=discord.ButtonStyle.success,
        custom_id="studybot:exam:pass",
    )
    async def pass_button(self, interaction, button):
        await self._answer(interaction, "pass")

    @discord.ui.button(
        label="不合格だった",
        style=discord.ButtonStyle.secondary,
        custom_id="studybot:exam:fail",
    )
    async def fail_button(self, interaction, button):
        await self._answer(interaction, "fail")

    @discord.ui.button(
        label="スコアを入力",
        style=discord.ButtonStyle.primary,
        custom_id="studybot:exam:score",
    )
    async def score_button(self, interaction, button):
        if find_pending_exam(interaction) is None:
            await respond_private(interaction, NO_PENDING_EXAM_TEXT)
            return
        await interaction.response.send_modal(ExamScoreModal())

    @discord.ui.button(
        label="まだ分からない",
        style=discord.ButtonStyle.secondary,
        custom_id="studybot:exam:later",
    )
    async def later_button(self, interaction, button):
        await self._answer(interaction, None)


NO_PENDING_EXAM_TEXT = (
    "結果を記録する試験が見つかりません"
    "（すでに記録済みか、試験日が設定されていません）。"
)


def find_pending_exam(interaction):
    return get_pending_exam(
        config.DB_PATH, interaction.user.id, datetime.now(JST).date()
    )


class ExamScoreModal(discord.ui.Modal):
    """得点と合否を入力する。"""

    def __init__(self):
        super().__init__(title="試験の結果を入力")
        self.score_input = discord.ui.TextInput(
            placeholder="例：720（分からなければ空欄）",
            required=False,
            max_length=4,
        )
        self.result_select = discord.ui.Select(
            placeholder="合否を選択",
            options=[
                discord.SelectOption(label="合格", value="pass"),
                discord.SelectOption(label="不合格", value="fail"),
            ],
        )
        self.add_item(discord.ui.Label(
            text="得点（1000点満点）", component=self.score_input
        ))
        self.add_item(discord.ui.Label(
            text="合否", component=self.result_select
        ))

    async def on_submit(self, interaction):
        try:
            score = parse_exam_score(self.score_input.value)
        except ValueError as error:
            await respond_private(interaction, str(error))
            return
        await answer_exam_result(
            interaction, self.result_select.values[0], score
        )


async def answer_exam_result(interaction, result, score=None):
    """ボタンかフォームの答えを記録し、合格なら次の資格へ進める。"""
    user_id = interaction.user.id
    pending = find_pending_exam(interaction)
    if pending is None:
        await respond_private(interaction, NO_PENDING_EXAM_TEXT)
        return

    qualification, exam_on = pending
    if result is None:
        await interaction.response.edit_message(
            content=(
                interaction.message.content
                + "\n→ わかりました。明日の夜20時にもう一度聞きます。"
            ),
            view=None,
        )
        return

    record_exam_result(
        config.DB_PATH, user_id, qualification, exam_on, result,
        datetime.now(JST).isoformat(), score,
    )
    label = "合格" if result == "pass" else "不合格"
    score_text = f"（{score}点）" if score is not None else ""
    await interaction.response.edit_message(
        content=(
            interaction.message.content
            + f"\n→ {label}{score_text}を記録しました。"
        ),
        view=None,
    )

    if result == "pass":
        next_qualification = advance_roadmap(config.DB_PATH, qualification)
        await interaction.followup.send(
            embed=build_pass_embed(
                user_id, qualification, exam_on, next_qualification, score
            ),
            view=(
                NextStepView(user_id, next_qualification[1])
                if next_qualification else None
            ),
        )
    else:
        await interaction.followup.send(
            embed=build_fail_embed(qualification),
            view=RetakeView(user_id),
        )

async def send_exam_result_prompt(bot, user_id, qualification, exam_on, today):
    channel = await find_home_channel(bot, user_id)
    if channel is None:
        return False

    exam_day = date.fromisoformat(exam_on)
    when = "今日の" if exam_day == today else f"{exam_day.month}/{exam_day.day}の"
    await channel.send(
        content=(
            f"<@{user_id}> {when}{_exam_label(qualification)}、"
            "おつかれさまでした。結果はどうでしたか？"
        ),
        view=ExamResultView(),
        allowed_mentions=discord.AllowedMentions(
            users=[discord.Object(id=user_id)]
        ),
    )
    mark_result_prompted(config.DB_PATH, user_id, qualification, exam_on, today)
    return True


_prompt_lock = asyncio.Lock()


async def send_exam_result_prompts(bot, now=None):
    now = now or datetime.now(JST)
    today = now.date()
    sent = 0
    async with _prompt_lock:
        for user_id, qualification, exam_on in get_result_prompt_candidates(
            config.DB_PATH, today
        ):
            try:
                if await send_exam_result_prompt(
                    bot, user_id, qualification, exam_on, today
                ):
                    sent += 1
            except Exception as error:
                print(f"[exam] 送信エラー user={user_id}: {error}")
    return sent


def is_result_prompt_due(now):
    """20時を過ぎていれば、その日のうちにたずねる（起動が遅れた場合も）。"""
    return now.time() >= RESULT_PROMPT_TIME


@tasks.loop(time=RESULT_PROMPT_TIME.replace(tzinfo=JST))
async def exam_result_loop(bot):
    now = datetime.now(JST)
    if is_result_prompt_due(now):
        await send_exam_result_prompts(bot, now)
        # 同じ20時に、明日が試験の人へ持ち物チェックを送る
        await send_exam_prep_prompts(bot, now)
