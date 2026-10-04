"""勉強の記録の入口：まず「何を勉強したか」を選んでから入力する。

- 過去問道場：これまでどおり分野・問題数・正答率（SGStudyLogModal）
- 復習：「過去問道場の復習」か「単語の復習」を選び、分野は複数選べる
- 単語帳：覚えた単語の分野を複数選べる
"""

from types import SimpleNamespace

import discord

from studybot import config
from studybot.activities import (
    ACTIVITY_KINDS,
    activity_categories,
    build_activity_text,
    parse_amount_input,
    save_activity,
)
from studybot.channels import find_channel
from studybot.config import JST
from studybot.database import delete_study_log_data, save_study_log
from studybot.features.badges import announce_new_badges
from studybot.forms import LOG_CHANNEL_MISSING_TEXT, SGStudyLogModal
from studybot.qualifications import SG
from studybot.replies import respond_private
from studybot.speed import parse_minutes_input


STUDY_KIND_TEXT = "何を勉強しましたか？"
REVIEW_KIND_TEXT = "何の復習をしましたか？"


class ActivityModal(discord.ui.Modal):
    """復習・単語帳の記録フォーム。分野は複数選べる。"""

    def __init__(self, kind, target_channel, qualification=SG):
        name, amount_label, _ = ACTIVITY_KINDS[kind]
        super().__init__(title=f"{qualification.code} {name}を記録"[:45])
        self.kind = kind
        self.target_channel = target_channel
        self.qualification = qualification

        categories = activity_categories(kind, qualification)
        self.category_select = discord.ui.Select(
            placeholder="分野を選ぶ（複数選べます）",
            min_values=1,
            max_values=len(categories),
            options=[
                discord.SelectOption(label=category, value=category)
                for category in categories
            ],
        )
        self.amount_input = discord.ui.TextInput(
            placeholder="例：20（空欄でも可）", required=False, max_length=5,
        )
        self.minutes_input = discord.ui.TextInput(
            placeholder="例：30（空欄でも可）", required=False, max_length=4,
        )
        self.memo_input = discord.ui.TextInput(
            placeholder="覚えにくかった用語や、次に見直すこと",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=300,
        )
        for text, component in (
            ("分野（複数選べます）", self.category_select),
            (f"{amount_label}（任意）", self.amount_input),
            ("かかった時間（分・任意）", self.minutes_input),
            ("メモ（任意）", self.memo_input),
        ):
            self.add_item(discord.ui.Label(text=text, component=component))

    async def on_submit(self, interaction):
        try:
            amount = parse_amount_input(self.amount_input.value)
            minutes = parse_minutes_input(self.minutes_input.value)
        except ValueError as error:
            await respond_private(interaction, f"⚠️ {error}")
            return
        categories = list(self.category_select.values)
        memo = (self.memo_input.value or "").strip()
        text = build_activity_text(
            self.qualification, self.kind, categories, amount, minutes, memo
        )

        await interaction.response.defer(ephemeral=True)
        log_message = None
        try:
            log_message = await self.target_channel.send(
                f"📗 {interaction.user.mention} {text}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            message_for_db = SimpleNamespace(
                id=log_message.id,
                guild=interaction.guild,
                channel=self.target_channel,
                author=interaction.user,
                created_at=log_message.created_at,
                content=text,
            )
            save_study_log(message_for_db)
            save_activity(
                config.DB_PATH, log_message.id, interaction.user.id,
                self.qualification, self.kind, categories, amount, minutes,
                memo, log_message.created_at.astimezone(JST).date(),
            )
        except Exception as error:
            print(f"[activity] 保存エラー: {error}")
            if log_message is not None:
                delete_study_log_data(log_message.id)
                try:
                    await log_message.delete()
                except discord.HTTPException:
                    pass
            await interaction.followup.send(
                "⚠️ 記録を保存できませんでした。studybot.log を確認してください。",
                ephemeral=True,
            )
            return

        await interaction.followup.send(f"記録しました：{text}", ephemeral=True)
        await announce_new_badges(interaction.guild, interaction.user.id)


class _OwnerView(discord.ui.View):
    def __init__(self, owner_id, target_channel, qualification):
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.target_channel = target_channel
        self.qualification = qualification

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この入力画面は開いた本人専用です。", ephemeral=True
        )
        return False

    async def open_activity(self, interaction, kind):
        await interaction.response.send_modal(
            ActivityModal(kind, self.target_channel, self.qualification)
        )


class StudyKindSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder=STUDY_KIND_TEXT,
            options=[
                discord.SelectOption(
                    label="過去問道場", value="past",
                    description="分野・問題数・正答率を記録",
                ),
                discord.SelectOption(
                    label="復習", value="review",
                    description="過去問道場の復習か、単語の復習（分野は複数可）",
                ),
                discord.SelectOption(
                    label="単語帳", value="terms",
                    description="覚えた単語の分野（複数可）",
                ),
            ],
        )

    async def callback(self, interaction):
        view = self.view
        choice = self.values[0]
        if choice == "past":
            await interaction.response.send_modal(SGStudyLogModal(
                None, view.target_channel, view.qualification
            ))
        elif choice == "review":
            await interaction.response.edit_message(
                content=f"【{view.qualification.display_name}】\n{REVIEW_KIND_TEXT}",
                view=ReviewKindView(
                    view.owner_id, view.target_channel, view.qualification
                ),
            )
        else:
            await view.open_activity(interaction, "terms")


class StudyKindView(_OwnerView):
    def __init__(self, owner_id, target_channel, qualification=SG):
        super().__init__(owner_id, target_channel, qualification)
        self.add_item(StudyKindSelect())


class ReviewKindView(_OwnerView):
    @discord.ui.button(label="過去問道場の復習", style=discord.ButtonStyle.primary)
    async def past_button(self, interaction, button):
        await self.open_activity(interaction, "review_past")

    @discord.ui.button(label="単語の復習", style=discord.ButtonStyle.primary)
    async def terms_button(self, interaction, button):
        await self.open_activity(interaction, "review_terms")

    @discord.ui.button(label="戻る", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction, button):
        await interaction.response.edit_message(
            content=f"【{self.qualification.display_name}】\n{STUDY_KIND_TEXT}",
            view=StudyKindView(self.owner_id, self.target_channel, self.qualification),
        )


def build_study_log_prompt(guild, user_id, qualification=SG):
    """(案内文, View)。記録できないときは View が None。"""
    if guild is None:
        return "記録はサーバー内で入力してください。", None
    target_channel = find_channel(guild, "study_log")
    if target_channel is None:
        return LOG_CHANNEL_MISSING_TEXT, None
    return (
        f"【{qualification.display_name}】\n{STUDY_KIND_TEXT}",
        StudyKindView(user_id, target_channel, qualification),
    )


async def open_study_log_prompt(interaction, qualification=SG):
    """ボタンから、勉強の種類を選ぶ画面を本人だけに出す。"""
    message, view = build_study_log_prompt(
        interaction.guild, interaction.user.id, qualification
    )
    await respond_private(interaction, message, view)

