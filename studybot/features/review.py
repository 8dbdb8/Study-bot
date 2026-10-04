"""/review グループ：誤答の登録と復習。"""

from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands

from studybot import config
from studybot.ai_check import checked_answer
from studybot.config import JST
from studybot.embeds import (
    COLOR_DEFAULT,
    build_review_card_embed,
    build_review_list_embed,
    build_review_summary_embed,
)
from studybot.features.badges import announce_new_badges
from studybot.forms import build_mistake_prompt
from studybot.qualifications import (
    QUALIFICATIONS,
    current_qualification,
    get_qualification,
)
from studybot.groups import review_group
from studybot.ollama import ask_ollama
from studybot.replies import send_private
from studybot.sg_features import get_sg_mistakes, record_sg_mistake_attempt
from studybot.sg_glossary import GlossaryDataError, load_glossary


# ============================================================
# AIによる解説
# ============================================================

# 解説のヒントとしてAIに渡す用語集の件数
EXPLAIN_GLOSSARY_LIMIT = 3
EXPLAIN_NOTE = "AIの説明は目安です。正解は問題の解説で確認してください。"


def related_glossary(item, entries=None):
    """誤答の番号・理由・メモに出てくるSG用語（長い用語から最大3件）。"""
    if (item.get("qualification") or "SG") != "SG":
        return []
    if entries is None:
        try:
            entries = load_glossary()
        except (OSError, GlossaryDataError):
            return []
    text = " ".join(
        item.get(key) or "" for key in ("question_ref", "reason", "memo")
    ).casefold()
    found = []
    seen = set()
    for entry in sorted(entries, key=lambda entry: -len(entry.term)):
        term = entry.term.casefold()
        if len(term) >= 2 and term in text and term not in seen:
            seen.add(term)
            found.append(entry)
            if len(found) >= EXPLAIN_GLOSSARY_LIMIT:
                break
    return found


def build_explain_prompt(item, glossary=()):
    qualification = get_qualification(item.get("qualification") or "SG")
    name = qualification.display_name if qualification else item.get("qualification")
    lines = [
        f"【資格】{name}",
        f"【分野】{item['category']}",
        f"【問題】{item['question_ref']}（問題文はありません）",
    ]
    if item.get("reason_kind"):
        lines.append(f"【間違えた理由の種類】{item['reason_kind']}")
    if item.get("reason"):
        lines.append(f"【間違えた理由】{item['reason']}")
    if item.get("memo"):
        lines.append(f"【次回確認すること】{item['memo']}")
    if glossary:
        lines.append("【関係する用語（用語集より）】")
        lines.extend(
            f"- {entry.term}：{entry.meaning or '（意味未登録）'}"
            for entry in glossary
        )
    return "\n".join(lines) + """

上の誤答を解き直す前に読む、短い解説を書いてください。

ルール:
- 問題文は分からないので、問題の正解を推測して断定しない
- 分野と間違えた理由から、押さえるべき知識の要点を説明する
- 間違えた理由の種類に合わせた、次に同じミスをしないコツを1つ入れる
- 箇条書き3つ（各80文字以内）だけで答える。見出しや前置きは書かない
"""


def build_explain_embed(item, answer, glossary=()):
    embed = discord.Embed(
        title=f"AIの解説（{item['category']}）",
        description=answer.strip()[:1500],
        color=COLOR_DEFAULT,
    )
    if glossary:
        embed.add_field(
            name="関係する用語",
            value=" ・ ".join(entry.term for entry in glossary),
            inline=False,
        )
    embed.set_footer(text=EXPLAIN_NOTE)
    return embed


async def explain_mistake(interaction, item):
    """誤答の解説をAIに作らせ、押した本人にだけ送る。"""
    await interaction.response.defer(ephemeral=True, thinking=True)
    glossary = related_glossary(item)
    prompt = build_explain_prompt(item, glossary)
    try:
        answer = checked_answer(await ask_ollama(prompt), prompt)
    except Exception as error:
        print(f"[explain] AIの処理に失敗: {error}")
        await interaction.followup.send(
            "AIに接続できませんでした。Ollamaが起動しているか確認してください。",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        embed=build_explain_embed(item, answer, glossary), ephemeral=True
    )


# ============================================================
# 復習を1問ずつ解き直す画面
# ============================================================

class ReviewSessionView(discord.ui.View):
    def __init__(self, owner_id, items, today, current_code="SG"):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.items = items
        self.today = today
        self.current_code = current_code
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

    def _image_path(self):
        if self.finished:
            return None
        path = self.items[self.index].get("image_path")
        return Path(path) if path and Path(path).exists() else None

    def embed(self):
        if self.finished:
            return build_review_summary_embed(self.results)
        embed = build_review_card_embed(
            self.items[self.index], self.index + 1, len(self.items),
            self.current_code,
        )
        image = self._image_path()
        if image is not None:
            embed.set_image(url=f"attachment://mistake{image.suffix}")
        return embed

    def files(self):
        """今のカードに付ける画像（なければ空）。"""
        image = self._image_path()
        if image is None:
            return []
        return [discord.File(image, filename=f"mistake{image.suffix}")]

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
                embed=self.embed(), view=None, attachments=[]
            )
            await announce_new_badges(interaction.guild, self.owner_id)
        else:
            # 前のカードの画像を外し、このカードの画像に差し替える
            await interaction.response.edit_message(
                embed=self.embed(), view=self, attachments=self.files()
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

    @discord.ui.button(label="AIに解説してもらう", style=discord.ButtonStyle.primary)
    async def explain_button(self, interaction, button):
        if self.finished:
            await interaction.response.send_message(
                "復習はもう終わっています。", ephemeral=True
            )
            return
        await explain_mistake(interaction, self.items[self.index])


def build_review_session(user_id, today=None, items=None):
    """(Embed, View) か、復習がなければ (案内文, None)。

    items を渡すとその誤答を、省略すると今日が復習日の誤答を解き直す。
    """
    today = today or datetime.now(JST).date()
    if items is None:
        items = get_sg_mistakes(
            config.DB_PATH, user_id, today=today, due_only=True
        )
    if not items:
        return "今日までに復習する問題はありません。", None
    view = ReviewSessionView(
        user_id, items, today, current_qualification(config.DB_PATH).code
    )
    return view.embed(), view


@review_group.command(
    name="add",
    description="間違えた問題を復習リストへ登録"
)
@app_commands.describe(qualification="資格（省略すると今学習中の資格）")
@app_commands.choices(qualification=[
    app_commands.Choice(name=q.display_name, value=q.code)
    for q in QUALIFICATIONS
])
async def mistake(ctx, qualification: str | None = None):
    if qualification is None:
        chosen = current_qualification(config.DB_PATH)
    else:
        chosen = get_qualification(qualification)
        if chosen is None:
            await send_private(ctx, "資格は候補から選んでください。")
            return
    message, view = build_mistake_prompt(ctx.author.id, chosen)
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

    await ctx.send(embed=build_review_list_embed(
        items, all_items, current_qualification(config.DB_PATH).code
    ))


@review_group.command(
    name="start",
    description="今日の復習を1問ずつ解き直す"
)
async def review_start(ctx):
    content, view = build_review_session(ctx.author.id)
    if view is None:
        await send_private(ctx, content)
    else:
        await send_private(ctx, embed=content, view=view, files=view.files())


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
