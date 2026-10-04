"""今日のひとこと：その日に気づいたことを1行残す。

勉強部屋の退出通知の「ひとこと」ボタンか /time note で書く。
週間レポートとNotionの週ページに日ごとに並ぶ。
"""

from datetime import datetime

import discord
from discord import app_commands

from studybot import config
from studybot.config import JST
from studybot.features.badges import announce_new_badges
from studybot.groups import time_group
from studybot.habits import NOTE_MAX_LENGTH, get_daily_notes, save_daily_note
from studybot.replies import respond_private, send_private


def _today():
    return datetime.now(JST).date()


def today_note(user_id, day):
    notes = get_daily_notes(config.DB_PATH, user_id, day, day)
    return notes[0][1] if notes else None


def saved_text(note):
    if note is None:
        return "今日のひとことを消しました。"
    return f"今日のひとことを残しました：{note}\n週間レポートとNotionの週ページに載ります。"


class DailyNoteModal(discord.ui.Modal):
    def __init__(self, current=None):
        super().__init__(title="今日のひとこと")
        self.note_input = discord.ui.TextInput(
            placeholder="例：科目Bの時間配分がつかめてきた",
            default=current,
            required=False,
            max_length=NOTE_MAX_LENGTH,
        )
        self.add_item(discord.ui.Label(
            text="気づいたこと・気分など（空にすると消します）",
            component=self.note_input,
        ))

    async def on_submit(self, interaction):
        try:
            note = save_daily_note(
                config.DB_PATH, interaction.user.id, _today(),
                self.note_input.value,
            )
        except ValueError as error:
            await respond_private(interaction, str(error))
            return
        await respond_private(interaction, saved_text(note))
        await announce_new_badges(interaction.guild, interaction.user.id)


async def open_note_modal(interaction):
    await interaction.response.send_modal(
        DailyNoteModal(today_note(interaction.user.id, _today()))
    )


@time_group.command(name="note", description="今日のひとことを残す（1日1行）")
@app_commands.describe(text="今日のひとこと（省略するとフォームが開きます）")
async def note(ctx, text: str | None = None):
    if text is None:
        if getattr(ctx, "interaction", None) is not None:
            await open_note_modal(ctx.interaction)
            return
        current = today_note(ctx.author.id, _today())
        await ctx.send(
            f"今日のひとこと：{current}" if current
            else "`!time note 気づいたこと` のように書いて残せます。"
        )
        return
    try:
        saved = save_daily_note(config.DB_PATH, ctx.author.id, _today(), text)
    except ValueError as error:
        await send_private(ctx, str(error))
        return
    await send_private(ctx, saved_text(saved))
    await announce_new_badges(ctx.guild, ctx.author.id)
