"""/review グループ：誤答の登録と復習。"""

from datetime import datetime

import discord
from discord import app_commands

from studybot import config
from studybot.config import JST
from studybot.embeds import (
    build_review_card_embed,
    build_review_list_embed,
    build_review_summary_embed,
)
from studybot.forms import build_mistake_prompt
from studybot.groups import review_group
from studybot.replies import send_private
from studybot.sg_features import get_sg_mistakes, record_sg_mistake_attempt


# ============================================================
# 復習を1問ずつ解き直す画面
# ============================================================

class ReviewSessionView(discord.ui.View):
    def __init__(self, owner_id, items, today):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.items = items
        self.today = today
        self.index = 0
        self.results = {
            "correct": 0, "wrong": 0, "skipped": 0, "completed": 0,
        }

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この復習画面は開いた本人専用です。", ephemeral=True
        )
        return False

    @property
    def finished(self):
        return self.index >= len(self.items)

    def embed(self):
        if self.finished:
            return build_review_summary_embed(self.results)
        return build_review_card_embed(
            self.items[self.index], self.index + 1, len(self.items)
        )

    async def _advance(self, interaction, result):
        item = self.items[self.index]
        if result is None:
            self.results["skipped"] += 1
        else:
            try:
                outcome = record_sg_mistake_attempt(
                    config.DB_PATH, self.owner_id, item["id"], result,
                    today=self.today,
                )
            except ValueError as error:
                await interaction.response.send_message(
                    str(error), ephemeral=True
                )
                return
            self.results[result] += 1
            if outcome["completed"]:
                self.results["completed"] += 1

        self.index += 1
        if self.finished:
            self.stop()
            await interaction.response.edit_message(
                embed=self.embed(), view=None
            )
        else:
            await interaction.response.edit_message(
                embed=self.embed(), view=self
            )

    @discord.ui.button(label="正解した", style=discord.ButtonStyle.success)
    async def correct_button(self, interaction, button):
        await self._advance(interaction, "correct")

    @discord.ui.button(label="また間違えた", style=discord.ButtonStyle.danger)
    async def wrong_button(self, interaction, button):
        await self._advance(interaction, "wrong")

    @discord.ui.button(label="あとで", style=discord.ButtonStyle.secondary)
    async def skip_button(self, interaction, button):
        await self._advance(interaction, None)


def build_review_session(user_id, today=None):
    """(Embed, View) か、復習がなければ (案内文, None)。"""
    today = today or datetime.now(JST).date()
    items = get_sg_mistakes(config.DB_PATH, user_id, today=today, due_only=True)
    if not items:
        return "今日までに復習する問題はありません。", None
    view = ReviewSessionView(user_id, items, today)
    return view.embed(), view


@review_group.command(
    name="add",
    description="間違えた問題を復習リストへ登録"
)
async def mistake(ctx):
    message, view = build_mistake_prompt(ctx.author.id)
    await send_private(ctx, message, view)


@review_group.command(
    name="list",
    description="今日までの復習予定を表示"
)
@app_commands.describe(all_items="今日以降の予定も表示する")
async def reviews(ctx, all_items: bool = False):
    items = get_sg_mistakes(
        config.DB_PATH, ctx.author.id,
        today=datetime.now(JST).date(), due_only=not all_items,
    )
    if not items:
        message = (
            "未完了の復習はありません。"
            if all_items else
            "今日までに復習する問題はありません。"
            " `/review list all_items:true` で今後の予定を見られます。"
        )
        await ctx.send(message)
        return

    await ctx.send(embed=build_review_list_embed(items, all_items))


@review_group.command(
    name="start",
    description="今日の復習を1問ずつ解き直す"
)
async def review_start(ctx):
    content, view = build_review_session(ctx.author.id)
    if view is None:
        await send_private(ctx, content)
    else:
        await send_private(ctx, embed=content, view=view)


@review_group.command(
    name="answer",
    description="復習で解き直した結果（正解・不正解）を登録"
)
@app_commands.describe(
    mistake_id="復習リストに表示されるID",
    result="今回の再挑戦結果",
)
@app_commands.choices(result=[
    app_commands.Choice(name="正解", value="correct"),
    app_commands.Choice(name="不正解", value="wrong"),
])
async def review(ctx, mistake_id: int, result: str):
    try:
        outcome = record_sg_mistake_attempt(
            config.DB_PATH, ctx.author.id, mistake_id, result,
            today=datetime.now(JST).date(),
        )
    except ValueError as error:
        await ctx.send(str(error))
        return

    if outcome["completed"]:
        await ctx.send(
            f"復習 #{mistake_id} は3回連続正解で完了しました。"
        )
    else:
        await ctx.send(
            f"復習 #{mistake_id} を記録しました。"
            f"連続正解：{outcome['streak']}/3。"
            f"次回：{outcome['next_review_on'].isoformat()}"
        )
