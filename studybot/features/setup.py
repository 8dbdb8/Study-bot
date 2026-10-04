"""/setup：Botが使うチャンネル（勉強部屋・勉強ログ・SG用語集・ai-report）を選ぶ。"""

import discord
from discord import app_commands

from studybot import config
from studybot.channels import (
    CHANNEL_KINDS,
    find_channel,
    get_channel_setting,
    set_channel_setting,
)
from studybot.embeds import COLOR_DEFAULT


def build_setup_embed(guild):
    embed = discord.Embed(
        title="StudyBot のチャンネル設定",
        description=(
            "下のメニューで選んだチャンネルは、名前を変えても使い続けます。"
            "未設定のものは名前で自動的に探します。"
        ),
        color=COLOR_DEFAULT,
    )
    for kind, (label, default_name, _) in CHANNEL_KINDS.items():
        configured_id = get_channel_setting(config.DB_PATH, guild.id, kind)
        channel = find_channel(guild, kind)
        if channel is None:
            value = (
                f"見つかりません。「{default_name}」を作るか、"
                "下のメニューで選んでください。"
            )
        elif channel.id == configured_id:
            value = f"{channel.mention}（設定済み）"
        else:
            value = f"{channel.mention}（名前「{default_name}」で自動検出）"
        embed.add_field(name=label, value=value, inline=False)
    return embed


class ChannelKindSelect(discord.ui.ChannelSelect):
    def __init__(self, kind, row):
        label, _, channel_type = CHANNEL_KINDS[kind]
        channel_types = (
            [discord.ChannelType.voice, discord.ChannelType.stage_voice]
            if channel_type == "voice"
            else [discord.ChannelType.text]
        )
        super().__init__(
            placeholder=label,
            channel_types=channel_types,
            min_values=1,
            max_values=1,
            row=row,
        )
        self.kind = kind

    async def callback(self, interaction):
        set_channel_setting(
            config.DB_PATH, interaction.guild.id, self.kind,
            self.values[0].id,
        )
        await interaction.response.edit_message(
            embed=build_setup_embed(interaction.guild), view=self.view
        )


class SetupView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=600)
        self.owner_id = owner_id
        for row, kind in enumerate(CHANNEL_KINDS):
            self.add_item(ChannelKindSelect(kind, row))

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この設定画面は実行した本人専用です。", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="名前で探す設定に戻す",
        style=discord.ButtonStyle.secondary,
        row=4,
    )
    async def reset_button(self, interaction, button):
        for kind in CHANNEL_KINDS:
            set_channel_setting(
                config.DB_PATH, interaction.guild.id, kind, None
            )
        await interaction.response.edit_message(
            embed=build_setup_embed(interaction.guild), view=self
        )


@app_commands.command(
    name="setup",
    description="StudyBotが使うチャンネル（勉強部屋・勉強ログ・用語集・ai-report）を選ぶ",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def setup_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=build_setup_embed(interaction.guild),
        view=SetupView(interaction.user.id),
        ephemeral=True,
    )
