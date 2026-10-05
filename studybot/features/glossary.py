"""/sg glossary・/fe glossary：用語集の一覧と単語帳。"""

import random
import sqlite3
from datetime import datetime
from urllib.parse import urlsplit
from uuid import uuid4

import discord
from discord import app_commands

from studybot import config
from studybot.channels import channel_label, is_channel
from studybot.config import JST, SG_GLOSSARY_CATEGORIES
from studybot.groups import QUALIFICATION_GROUPS, sg_group
from studybot.qualifications import FE
from studybot.sg_glossary import (
    get_sg_glossary_ratings,
    glossary_entry_key,
    GLOSSARY_PATH,
    GlossaryDataError,
    load_glossary,
    save_sg_glossary_rating,
    search_glossary,
    SG_GLOSSARY_RATINGS,
    SOURCE_GLOSSARY_URL,
    FE_GLOSSARY_PATH,
    FE_SOURCE_URL,
    split_text,
)
from studybot.sg_glossary_history import (
    GLOSSARY_HISTORY_PATH,
    record_glossary_card,
    set_glossary_summary_message_id,
)


def _safe_glossary_text(value):
    return discord.utils.escape_markdown(
        discord.utils.escape_mentions(value)
    )


def _sg_glossary_rating_label(rating):
    return "要復習" if rating == "まだ要復習" else rating


def _sg_glossary_list_pages(entries):
    """Return bounded message bodies and their first entry positions."""
    pages = []
    sections = []
    size = 0
    first_entry = 0
    # Leave room for the heading, a search query, and the source URL.
    body_limit = 1300

    for index, entry in enumerate(entries):
        title = f"**{_safe_glossary_text(entry.term)}**"
        category = " / ".join(filter(None, (entry.category, entry.subcategory)))
        if category:
            title += f" · {_safe_glossary_text(category)}"
        meaning = (
            _safe_glossary_text(entry.meaning)
            if entry.meaning else "意味未登録"
        )
        block = f"{title}\n{meaning}"
        for part in split_text(block, body_limit):
            added = len(part) + (2 if sections else 0)
            if sections and size + added > body_limit:
                pages.append(("\n\n".join(sections), first_entry))
                sections = []
                size = 0
            if not sections:
                first_entry = index
            sections.append(part)
            size += len(part) + (2 if len(sections) > 1 else 0)

    if sections:
        pages.append(("\n\n".join(sections), first_entry))
    return pages


def _sg_glossary_source_url(entry, fallback=SOURCE_GLOSSARY_URL):
    url = entry.source_url or fallback
    try:
        parsed = urlsplit(url)
    except ValueError:
        return fallback
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or len(url) > 350
        or any(character.isspace() or character in "<>" for character in url)
    ):
        return fallback
    return url


def _sg_glossary_session_summary_text(summary, category, query):
    started_at = datetime.fromisoformat(summary["started_at"]).astimezone(JST)
    category_name = "全分野" if category == "all" else _safe_glossary_text(category)
    query_note = (
        f" / 検索: {_safe_glossary_text(query[:100])}"
        if query and query.strip() else ""
    )
    counts = summary["counts"]
    return (
        "**SG単語帳の学習記録**\n"
        f"学習者: <@{summary['user_id']}>\n"
        f"開始: {started_at:%Y-%m-%d %H:%M}\n"
        f"分野: {category_name}{query_note}\n"
        f"合計 **{summary['total']}問**\n"
        f"できた: {counts['できた']}問 / "
        f"できなかった: {counts['できなかった']}問 / "
        f"要復習: {counts['まだ要復習']}問 / "
        f"微妙: {counts['微妙']}問"
    )


class SGGlossaryView(discord.ui.View):
    def __init__(
        self, owner_id, entries, mode="list", category="all", query=None,
        ratings=None, record_channel=None, guild_id=None,
        history_db_path=GLOSSARY_HISTORY_PATH, label="SG",
        source_fallback=SOURCE_GLOSSARY_URL,
    ):
        super().__init__(timeout=300)
        self.label = label
        self.source_fallback = source_fallback
        self.owner_id = owner_id
        self.all_entries = tuple(entries)
        self.entries = self.all_entries
        self.mode = mode
        self.category = category
        self.query = query or ""
        self.list_pages = _sg_glossary_list_pages(self.entries)
        self.page_index = 0
        self.card_index = 0
        self.meaning_page = 0
        self.revealed = False
        self.ratings = dict(ratings or {})
        self.rating_filter = ()
        self.last_rating_feedback = ""
        self.record_channel = record_channel
        self.guild_id = guild_id
        self.history_db_path = history_db_path
        self.session_id = uuid4().hex
        self.summary_message = None
        self._update_buttons()

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この用語集画面はコマンドを実行した本人専用です。",
            ephemeral=True,
        )
        return False

    def _meaning_parts(self):
        entry = self.entries[self.card_index]
        meaning = (
            _safe_glossary_text(entry.meaning)
            if entry.meaning else "意味は未登録です。"
        )
        return split_text(meaning, 1300)

    def _update_buttons(self):
        rating_buttons = (
            self.rated_yes, self.rated_no,
            self.rated_review, self.rated_unsure,
        )
        for rating_button in rating_buttons:
            if self.mode == "cards":
                if rating_button not in self.children:
                    self.add_item(rating_button)
                rating_button.disabled = not self.revealed
            elif rating_button in self.children:
                self.remove_item(rating_button)

        if self.mode == "cards":
            if self.rating_filter_select not in self.children:
                self.add_item(self.rating_filter_select)
        elif self.rating_filter_select in self.children:
            self.remove_item(self.rating_filter_select)
        for option in self.rating_filter_select.options:
            option.default = option.value in self.rating_filter
        if self.rating_filter:
            if self.clear_rating_filter not in self.children:
                self.add_item(self.clear_rating_filter)
        elif self.clear_rating_filter in self.children:
            self.remove_item(self.clear_rating_filter)

        if self.mode == "list":
            self.previous.disabled = self.page_index == 0
            self.next_page.disabled = self.page_index >= len(self.list_pages) - 1
            self.reveal.disabled = True
            self.reveal.label = "意味を見る"
            self.shuffle.disabled = True
            self.switch_mode.label = "単語帳へ"
        else:
            parts = self._meaning_parts()
            self.previous.disabled = (
                self.card_index == 0
                and (not self.revealed or self.meaning_page == 0)
            )
            self.next_page.disabled = (
                self.card_index >= len(self.entries) - 1
                and (not self.revealed or self.meaning_page >= len(parts) - 1)
            )
            self.reveal.disabled = False
            self.reveal.label = "意味を隠す" if self.revealed else "意味を見る"
            self.shuffle.disabled = len(self.entries) < 2
            self.switch_mode.label = "一覧へ"

    def content(self):
        count = len(self.entries)
        category_name = (
            "全分野" if self.category == "all"
            else _safe_glossary_text(self.category)
        )
        query_note = (
            f" / 検索: {_safe_glossary_text(self.query[:100])}"
            if self.query.strip() else ""
        )
        filter_note = (
            " / 評価（選択時）: " + "・".join(
                _sg_glossary_rating_label(rating)
                for rating in self.rating_filter
            )
            if self.rating_filter else ""
        )
        if self.mode == "list":
            body, _ = self.list_pages[self.page_index]
            sources = {
                _sg_glossary_source_url(entry, self.source_fallback)
                for entry in self.entries
            }
            source_label = (
                "用語出典" if all(entry.source_url for entry in self.entries)
                else "参考サイト"
            )
            source_note = (
                f"\n\n{source_label}: <{next(iter(sources))}>"
                if len(sources) == 1 else "\n\n用語出典は各カードに表示"
            )
            return (
                f"**{self.label}用語集・一覧**（分野: {category_name}{query_note}"
                f"{filter_note} / {count}件） "
                f"{self.page_index + 1}/{len(self.list_pages)}ページ\n\n"
                f"{body}{source_note}"
            )

        entry = self.entries[self.card_index]
        category = " / ".join(filter(None, (entry.category, entry.subcategory)))
        category_label = _safe_glossary_text(category) if category else "未登録"
        category_quote = "\n".join(
            f"> {line}" for line in category_label.splitlines()
        )
        heading = (
            f"**{self.label}単語帳**（対象: {category_name}{query_note}{filter_note} / "
            f"{self.card_index + 1}/{count}件）\n\n"
            f"**{_safe_glossary_text(entry.term)}**\n"
            f"{category_quote}"
        )
        feedback = (
            f"\n{self.last_rating_feedback}"
            if self.last_rating_feedback else ""
        )
        if not self.revealed:
            return f"{heading}{feedback}\n\n「意味を見る」を押してください。"

        parts = self._meaning_parts()
        continuation = (
            f"（意味 {self.meaning_page + 1}/{len(parts)}）\n"
            if len(parts) > 1 else ""
        )
        source = _sg_glossary_source_url(entry, self.source_fallback)
        source_label = "用語出典" if entry.source_url else "参考サイト"
        current_rating = self.ratings.get(glossary_entry_key(entry))
        rating_note = (
            f"\n自己評価: {_sg_glossary_rating_label(current_rating)}"
            if current_rating else "\n自己評価: 未評価"
        )
        return (
            f"{heading}{feedback}\n\n{continuation}{parts[self.meaning_page]}\n\n"
            f"{source_label}: <{source}>{rating_note}"
        )

    async def _refresh(self, interaction):
        self._update_buttons()
        await interaction.response.edit_message(
            content=self.content(), view=self,
        )

    async def _apply_rating_filter(self, interaction, selected_ratings):
        selected = set(selected_ratings)
        if not selected.issubset(SG_GLOSSARY_RATINGS):
            await interaction.response.send_message(
                "自己評価は表示された4つから選んでください。",
                ephemeral=True,
            )
            return
        entries = (
            tuple(entry for entry in self.all_entries
                  if self.ratings.get(glossary_entry_key(entry)) in selected)
            if selected else self.all_entries
        )
        if not entries:
            await interaction.response.send_message(
                "選んだ自己評価に該当する単語はありません。別の評価を選んでください。",
                ephemeral=True,
            )
            return
        self.rating_filter = tuple(
            rating for rating in SG_GLOSSARY_RATINGS if rating in selected
        )
        self.entries = entries
        self.list_pages = _sg_glossary_list_pages(entries)
        self.page_index = 0
        self.card_index = 0
        self.meaning_page = 0
        self.revealed = False
        self.last_rating_feedback = ""
        await self._refresh(interaction)

    @discord.ui.select(
        placeholder="自己評価で絞る（複数選択可）",
        min_values=1, max_values=4, row=2,
        options=[
            discord.SelectOption(label="できた", value="できた"),
            discord.SelectOption(label="できなかった", value="できなかった"),
            discord.SelectOption(label="要復習", value="まだ要復習"),
            discord.SelectOption(label="微妙", value="微妙"),
        ],
    )
    async def rating_filter_select(self, interaction, select):
        await self._apply_rating_filter(interaction, select.values)

    @discord.ui.button(label="絞り込み解除", style=discord.ButtonStyle.secondary, row=3)
    async def clear_rating_filter(self, interaction, button):
        await self._apply_rating_filter(interaction, ())

    @discord.ui.button(label="前へ", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.last_rating_feedback = ""
        if self.mode == "list":
            self.page_index -= 1
        elif self.revealed and self.meaning_page > 0:
            self.meaning_page -= 1
        else:
            self.card_index -= 1
            self.meaning_page = 0
            self.revealed = False
        await self._refresh(interaction)

    @discord.ui.button(label="意味を見る", style=discord.ButtonStyle.primary)
    async def reveal(self, interaction, button):
        self.last_rating_feedback = ""
        self.revealed = not self.revealed
        self.meaning_page = 0
        await self._refresh(interaction)

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        self.last_rating_feedback = ""
        if self.mode == "list":
            self.page_index += 1
        elif self.revealed and self.meaning_page < len(self._meaning_parts()) - 1:
            self.meaning_page += 1
        else:
            self.card_index += 1
            self.meaning_page = 0
            self.revealed = False
        await self._refresh(interaction)

    @discord.ui.button(label="シャッフル", style=discord.ButtonStyle.secondary)
    async def shuffle(self, interaction, button):
        self.last_rating_feedback = ""
        # Pick a different word so a tap always changes the card.
        target = random.randrange(len(self.entries) - 1)
        if target >= self.card_index:
            target += 1
        self.card_index = target
        self.meaning_page = 0
        self.revealed = False
        await self._refresh(interaction)

    @discord.ui.button(label="単語帳へ", style=discord.ButtonStyle.success)
    async def switch_mode(self, interaction, button):
        self.last_rating_feedback = ""
        if self.mode == "list":
            self.card_index = self.list_pages[self.page_index][1]
            self.mode = "cards"
        else:
            exact_pages = [
                index for index, (_, first_entry) in enumerate(self.list_pages)
                if first_entry == self.card_index
            ]
            if exact_pages:
                self.page_index = exact_pages[0]
            else:
                self.page_index = max(
                    index for index, (_, first_entry) in enumerate(self.list_pages)
                    if first_entry < self.card_index
                )
            self.mode = "list"
        self.meaning_page = 0
        self.revealed = False
        await self._refresh(interaction)

    async def _rate(self, interaction, rating):
        if self.mode != "cards" or not self.revealed:
            await interaction.response.send_message(
                "意味を表示してから自己評価を選んでください。",
                ephemeral=True,
            )
            return
        entry = self.entries[self.card_index]
        try:
            save_sg_glossary_rating(config.DB_PATH, self.owner_id, entry, rating)
        except (OSError, sqlite3.Error) as error:
            await interaction.response.send_message(
                f"自己評価を保存できませんでした: {error}",
                ephemeral=True,
            )
            return
        summary = None
        if self.record_channel is not None:
            try:
                summary = record_glossary_card(
                    self.history_db_path, self.session_id,
                    self.guild_id, self.record_channel.id,
                    self.owner_id, entry, rating,
                )
            except (OSError, sqlite3.Error, ValueError) as error:
                self.ratings[glossary_entry_key(entry)] = rating
                await interaction.response.send_message(
                    f"自己評価は保存しましたが、単語帳の学習記録を保存できませんでした: {error}"
                    " もう一度評価を選んでください。",
                    ephemeral=True,
                )
                return
        self.ratings[glossary_entry_key(entry)] = rating
        self.last_rating_feedback = (
            f"自己評価「{_sg_glossary_rating_label(rating)}」を記録しました。"
        )
        if self.card_index < len(self.entries) - 1:
            self.card_index += 1
            self.revealed = False
            self.meaning_page = 0
        await self._refresh(interaction)
        if summary is not None:
            try:
                await self._publish_session_summary(summary)
            except (discord.HTTPException, OSError, sqlite3.Error) as error:
                await interaction.followup.send(
                    "学習記録は専用DBに保存しましたが、チャンネルへの投稿に"
                    f"失敗しました: {error} 次の評価時に再試行します。",
                    ephemeral=True,
                )

    async def _publish_session_summary(self, summary):
        text = _sg_glossary_session_summary_text(
            summary, self.category, self.query,
        )
        message_id = summary["summary_message_id"]
        if self.summary_message is None and message_id is not None:
            try:
                self.summary_message = await self.record_channel.fetch_message(
                    message_id,
                )
            except discord.NotFound:
                pass
        allowed_mentions = discord.AllowedMentions.none()
        if self.summary_message is None:
            self.summary_message = await self.record_channel.send(
                text, allowed_mentions=allowed_mentions,
            )
        else:
            try:
                await self.summary_message.edit(
                    content=text, allowed_mentions=allowed_mentions,
                )
            except discord.NotFound:
                self.summary_message = await self.record_channel.send(
                    text, allowed_mentions=allowed_mentions,
                )
        if message_id != self.summary_message.id:
            set_glossary_summary_message_id(
                self.history_db_path, self.session_id,
                self.summary_message.id,
            )

    @discord.ui.button(label="できた", style=discord.ButtonStyle.success, row=1)
    async def rated_yes(self, interaction, button):
        await self._rate(interaction, "できた")

    @discord.ui.button(label="できなかった", style=discord.ButtonStyle.danger, row=1)
    async def rated_no(self, interaction, button):
        await self._rate(interaction, "できなかった")

    @discord.ui.button(label="要復習", style=discord.ButtonStyle.primary, row=1)
    async def rated_review(self, interaction, button):
        await self._rate(interaction, "まだ要復習")

    @discord.ui.button(label="微妙", style=discord.ButtonStyle.secondary, row=1)
    async def rated_unsure(self, interaction, button):
        await self._rate(interaction, "微妙")


@sg_group.command(
    name="glossary",
    description="SG用語集を一覧または単語帳で見る"
)
@app_commands.describe(
    mode="表示方法",
    category="表示する分野",
    query="用語・意味・分野・小分類を検索",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="一覧", value="list"),
        app_commands.Choice(name="単語帳", value="cards"),
    ],
    category=[
        app_commands.Choice(name="全分野", value="all"),
        *(
            app_commands.Choice(name=name, value=name)
            for name in SG_GLOSSARY_CATEGORIES
        ),
    ],
)
async def sgglossary(
    ctx, mode: str = "list", category: str = "all", query: str | None = None,
):
    is_interaction = getattr(ctx, "interaction", None) is not None
    if (
        ctx.guild is None
        or not is_channel(ctx.channel, "glossary", ctx.guild)
    ):
        message = f"このコマンドは {channel_label(ctx.guild, 'glossary')} で使ってください。"
        if is_interaction:
            await ctx.send(message, ephemeral=True)
        else:
            await ctx.send(message)
        return
    await open_glossary(
        ctx, mode, category, query,
        label="SG", path=GLOSSARY_PATH, categories=SG_GLOSSARY_CATEGORIES,
        source_fallback=SOURCE_GLOSSARY_URL, record_channel=ctx.channel,
    )


@QUALIFICATION_GROUPS["FE"].command(
    name="glossary",
    description="FE用語集を一覧または単語帳で見る（本人にだけ表示）"
)
@app_commands.describe(
    mode="表示方法",
    category="表示する分野",
    query="用語・意味・分野を検索",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="一覧", value="list"),
        app_commands.Choice(name="単語帳", value="cards"),
    ],
    category=[
        app_commands.Choice(name="全分野", value="all"),
        *(app_commands.Choice(name=name, value=name) for name in FE.category_names),
    ],
)
async def feglossary(
    ctx, mode: str = "list", category: str = "all", query: str | None = None,
):
    # FE用語集はどのチャンネルでも使える（学習記録の投稿はしない）
    await open_glossary(
        ctx, mode, category, query,
        label="FE", path=FE_GLOSSARY_PATH, categories=FE.category_names,
        source_fallback=FE_SOURCE_URL, record_channel=None,
    )


async def open_glossary(ctx, mode, category, query, label, path, categories,
                        source_fallback, record_channel):
    is_interaction = getattr(ctx, "interaction", None) is not None
    data_file = f"data/{path.name}" if hasattr(path, "name") else str(path)

    async def reply(message, view=None):
        kwargs = {"view": view} if view is not None else {}
        if is_interaction:
            kwargs["ephemeral"] = True
        await ctx.send(message, **kwargs)

    normalized_mode = {"一覧": "list", "単語帳": "cards"}.get(mode, mode)
    if normalized_mode not in ("list", "cards"):
        await reply("表示方法は「一覧」または「単語帳」を選んでください。")
        return
    if category != "all" and category not in categories:
        await reply("分野は候補から選んでください。")
        return
    if query and len(query) > 100:
        await reply("検索語は100文字以内で入力してください。")
        return

    try:
        all_entries = load_glossary(path)
    except FileNotFoundError:
        await reply(
            f"{label}用語データがありません。`{data_file}` を配置してください。\n"
            f"参照先: <{source_fallback}>"
        )
        return
    except (OSError, GlossaryDataError) as error:
        await reply(f"{label}用語データを読み込めません: {error}")
        return

    if not all_entries:
        await reply(f"{label}用語データは空です。`{data_file}` に用語を追加してください。")
        return
    entries = (
        all_entries if category == "all"
        else [entry for entry in all_entries if entry.category == category]
    )
    if not entries:
        await reply("選んだ分野の用語がありません。別の分野を選んでください。")
        return

    entries = search_glossary(entries, query)
    if not entries:
        await reply("一致する用語がありません。分野や検索語を変えて試してください。")
        return

    try:
        ratings = get_sg_glossary_ratings(config.DB_PATH, ctx.author.id, entries)
    except (OSError, sqlite3.Error) as error:
        await reply(f"自己評価を読み込めません: {error}")
        return
    view = SGGlossaryView(
        ctx.author.id, entries, normalized_mode, category, query,
        ratings=ratings, record_channel=record_channel,
        guild_id=ctx.guild.id if ctx.guild else None,
        history_db_path=GLOSSARY_HISTORY_PATH,
        label=label, source_fallback=source_fallback,
    )
    await reply(view.content(), view=view)
