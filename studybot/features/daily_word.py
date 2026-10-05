"""今日の1語：毎朝7:30に用語を1つ投稿する。

SGを学習中なら #SG用語集 にSG用語集から、FEを学習中なら #FE用語集
（なければ #勉強ログ）にFE用語集から選ぶ。
「意味を見る」で意味を開き、そのまま単語帳と同じ自己評価を付けられる。
ボタンは再起動後も使える。
"""

import sqlite3
from datetime import datetime, time

import discord
from discord.ext import tasks

from studybot import config
from studybot.channels import find_channel
from studybot.config import JST
from studybot.daily_word import (
    pick_word,
    posted_today,
    recent_terms,
    save_daily_word,
    word_key_for_message,
)
from studybot.embeds import COLOR_DEFAULT
from studybot.features.presence import presence_user
from studybot.qualifications import current_qualification
from studybot.replies import respond_private
from studybot.sg_glossary import (
    FE_GLOSSARY_PATH,
    GLOSSARY_PATH,
    GlossaryDataError,
    get_sg_glossary_ratings,
    glossary_entry_key,
    load_glossary,
    save_sg_glossary_rating,
)


DAILY_WORD_TIME = time(7, 30)
# Botの起動が遅れた日も、この時刻までならその日の分を投稿する
DAILY_WORD_CATCH_UP_UNTIL_HOUR = 21

RATING_BUTTONS = (
    ("できた", "できた", discord.ButtonStyle.success),
    ("できなかった", "できなかった", discord.ButtonStyle.danger),
    ("要復習", "まだ要復習", discord.ButtonStyle.primary),
    ("微妙", "微妙", discord.ButtonStyle.secondary),
)


# 資格 -> (用語集の場所, 投稿するチャンネルの種類)
WORD_SOURCES = {
    "SG": (GLOSSARY_PATH, "glossary"),
    "FE": (FE_GLOSSARY_PATH, "fe_glossary"),
}


def _load_entries(path):
    try:
        return load_glossary(path)
    except (OSError, GlossaryDataError) as error:
        print(f"[word] 用語集を読み込めません: {error}")
        return []


def word_channel(guild, kind):
    channel = find_channel(guild, kind)
    if channel is None and kind != "glossary":
        # FE用語集のチャンネルがなければ勉強ログに投稿する
        channel = find_channel(guild, "study_log")
    return channel


def build_word_embed(entry, today, label="SG"):
    category = entry.category or label
    if label != "SG":
        category = f"{label} {category}"
    embed = discord.Embed(
        title=f"今日の1語（{today.month}/{today.day}）・ {category}",
        description=f"## {discord.utils.escape_markdown(entry.term)}",
        color=COLOR_DEFAULT,
    )
    if entry.subcategory:
        embed.add_field(name="小分類", value=entry.subcategory, inline=False)
    embed.set_footer(text="意味を思い出してから「意味を見る」を押してください")
    return embed


def build_meaning_embed(entry):
    return discord.Embed(
        title=entry.term,
        description=entry.meaning or "意味未登録",
        color=COLOR_DEFAULT,
    ).set_footer(text="覚えていたかを選ぶと、単語帳の自己評価に記録します")


class WordRatingButton(discord.ui.Button):
    def __init__(self, label, rating, style):
        super().__init__(label=label, style=style)
        self.rating = rating

    async def callback(self, interaction):
        try:
            save_sg_glossary_rating(
                config.DB_PATH, interaction.user.id, self.view.entry, self.rating
            )
        except (OSError, sqlite3.Error, ValueError) as error:
            await respond_private(interaction, f"自己評価を保存できませんでした: {error}")
            return
        label = "要復習" if self.rating == "まだ要復習" else self.rating
        await interaction.response.edit_message(
            content=f"自己評価「{label}」を記録しました。",
            embed=build_meaning_embed(self.view.entry),
            view=None,
        )


class WordRatingView(discord.ui.View):
    def __init__(self, entry):
        super().__init__(timeout=600)
        self.entry = entry
        for label, rating, style in RATING_BUTTONS:
            self.add_item(WordRatingButton(label, rating, style))


class DailyWordView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="意味を見る",
        style=discord.ButtonStyle.primary,
        custom_id="studybot:word:show",
    )
    async def show_button(self, interaction, button):
        key = word_key_for_message(config.DB_PATH, interaction.message.id)
        entry = next(
            (
                entry
                for path, _ in WORD_SOURCES.values()
                for entry in _load_entries(path)
                if glossary_entry_key(entry) == key
            ),
            None,
        )
        if entry is None:
            await respond_private(interaction, "この用語は用語集に見つかりませんでした。")
            return
        await interaction.response.send_message(
            embed=build_meaning_embed(entry),
            view=WordRatingView(entry),
            ephemeral=True,
        )


async def post_daily_words(bot, now=None):
    """今日の1語を投稿する。投稿した数を返す。"""
    now = now or datetime.now(JST)
    today = now.date()
    code = current_qualification(config.DB_PATH).code
    if code not in WORD_SOURCES:
        return 0
    path, channel_kind = WORD_SOURCES[code]
    entries = _load_entries(path)
    if not entries:
        return 0
    user_id = presence_user(config.DB_PATH)
    try:
        ratings = (
            get_sg_glossary_ratings(config.DB_PATH, user_id, entries)
            if user_id else {}
        )
    except (OSError, sqlite3.Error):
        ratings = {}
    entry = pick_word(entries, ratings, today, recent_terms(config.DB_PATH, today))
    if entry is None:
        return 0

    posted = 0
    for guild in bot.guilds:
        channel = word_channel(guild, channel_kind)
        if channel is None or posted_today(config.DB_PATH, today, guild.id):
            continue
        try:
            message = await channel.send(
                embed=build_word_embed(entry, today, code), view=DailyWordView()
            )
        except discord.HTTPException as error:
            print(f"[word] 投稿に失敗: {error}")
            continue
        save_daily_word(
            config.DB_PATH, today, guild.id, channel.id, message.id, entry
        )
        posted += 1
    return posted


def is_daily_word_due(now):
    return (
        now.time() >= DAILY_WORD_TIME
        and now.hour < DAILY_WORD_CATCH_UP_UNTIL_HOUR
    )


@tasks.loop(time=DAILY_WORD_TIME.replace(tzinfo=JST))
async def daily_word_loop(bot):
    await post_daily_words(bot)
