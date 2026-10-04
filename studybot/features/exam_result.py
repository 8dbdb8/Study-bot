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
    get_pending_exam,
    get_result_prompt_candidates,
    get_study_summary,
    mark_result_prompted,
    record_exam_result,
)
from studybot.features.digest import find_home_channel
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


def build_pass_embed(user_id, qualification, exam_on, next_qualification):
    first_day, study_days, seconds = get_study_summary(
        config.DB_PATH, user_id, exam_on
    )
    total_questions = get_study_status(user_id, qualification)["total_questions"]

    embed = discord.Embed(
        title=f"{qualification}合格おめでとうございます！",
        color=COLOR_SUCCESS,
    )
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
        user_id = interaction.user.id
        today = datetime.now(JST).date()
        pending = get_pending_exam(config.DB_PATH, user_id, today)
        if pending is None:
            await respond_private(
                interaction,
                "結果を記録する試験が見つかりません"
                "（すでに記録済みか、試験日が設定されていません）。",
            )
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
            datetime.now(JST).isoformat(),
        )
        label = "合格" if result == "pass" else "不合格"
        await interaction.response.edit_message(
            content=interaction.message.content + f"\n→ {label}を記録しました。",
            view=None,
        )

        if result == "pass":
            next_qualification = advance_roadmap(config.DB_PATH, qualification)
            await interaction.followup.send(
                embed=build_pass_embed(
                    user_id, qualification, exam_on, next_qualification
                ),
                view=NextStepView(user_id) if next_qualification else None,
            )
        else:
            await interaction.followup.send(
                embed=build_fail_embed(qualification),
                view=RetakeView(user_id),
            )

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
        label="まだ分からない",
        style=discord.ButtonStyle.secondary,
        custom_id="studybot:exam:later",
    )
    async def later_button(self, interaction, button):
        await self._answer(interaction, None)


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
