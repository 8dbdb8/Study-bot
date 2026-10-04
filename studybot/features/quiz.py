"""/sg quiz：SG用語の4択ミニテスト。

用語集の意味を見せて、正しい用語を4つから選ぶ（5問）。
間違えた用語は単語帳の自己評価を「要復習」にする。
"""

import sqlite3
from datetime import datetime

import discord
from discord import app_commands

from studybot import config
from studybot.config import JST, SG_GLOSSARY_CATEGORIES
from studybot.embeds import COLOR_DEFAULT, COLOR_SUCCESS
from studybot.features.badges import announce_new_badges
from studybot.groups import sg_group
from studybot.quiz import QUIZ_SIZE, build_quiz, save_quiz_result
from studybot.replies import send_private
from studybot.sg_glossary import (
    GLOSSARY_PATH,
    GlossaryDataError,
    get_sg_glossary_ratings,
    load_glossary,
    save_sg_glossary_rating,
)


ALL_CATEGORIES = "all"


def load_quiz(user_id, category=ALL_CATEGORIES, rng=None):
    """(問題のリスト, エラー文)。問題を作れないときはリストが空。"""
    try:
        entries = load_glossary(GLOSSARY_PATH)
    except FileNotFoundError:
        return [], "SG用語データがありません。`data/sg_glossary.json` を配置してください。"
    except (OSError, GlossaryDataError) as error:
        return [], f"SG用語データを読み込めません: {error}"
    if category != ALL_CATEGORIES:
        entries = [entry for entry in entries if entry.category == category]
    try:
        ratings = get_sg_glossary_ratings(config.DB_PATH, user_id, entries)
    except (OSError, sqlite3.Error):
        ratings = {}
    questions = build_quiz(entries, ratings, rng=rng)
    if not questions:
        return [], "この分野では4択の問題を作れません。別の分野を選んでください。"
    return questions, None


def _category_label(category):
    return "全分野" if category == ALL_CATEGORIES else category


class ChoiceButton(discord.ui.Button):
    def __init__(self, term, style, disabled, row):
        super().__init__(label=term[:80], style=style, disabled=disabled, row=row)
        self.term = term

    async def callback(self, interaction):
        await self.view.answer(interaction, self.term)


class NextButton(discord.ui.Button):
    def __init__(self, last):
        super().__init__(
            label="結果を見る" if last else "次へ",
            style=discord.ButtonStyle.primary,
            row=2,
        )

    async def callback(self, interaction):
        await self.view.next_question(interaction)


class AgainButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label=f"もう{QUIZ_SIZE}問", style=discord.ButtonStyle.primary)

    async def callback(self, interaction):
        questions, error = load_quiz(self.view.owner_id, self.view.category)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        view = QuizView(self.view.owner_id, questions, self.view.category)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class QuizView(discord.ui.View):
    def __init__(self, owner_id, questions, category=ALL_CATEGORIES):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.questions = questions
        self.category = category
        self.index = 0
        self.correct = 0
        self.chosen = None          # 今の問題で選んだ用語（まだなら None）
        self.missed = []            # 間違えた用語
        self._rebuild()

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "このミニテストは開いた本人専用です。", ephemeral=True
        )
        return False

    @property
    def finished(self):
        return self.index >= len(self.questions)

    @property
    def question(self):
        return self.questions[self.index]

    def _rebuild(self):
        self.clear_items()
        if self.finished:
            self.add_item(AgainButton())
            return
        question = self.question
        for number, term in enumerate(question.choices):
            if self.chosen is None:
                style = discord.ButtonStyle.secondary
            elif term == question.answer:
                style = discord.ButtonStyle.success
            elif term == self.chosen:
                style = discord.ButtonStyle.danger
            else:
                style = discord.ButtonStyle.secondary
            self.add_item(ChoiceButton(
                term, style, self.chosen is not None, row=number // 2
            ))
        if self.chosen is not None:
            self.add_item(NextButton(self.index == len(self.questions) - 1))

    def embed(self):
        if self.finished:
            return self.result_embed()
        question = self.question
        embed = discord.Embed(
            title=(
                f"用語ミニテスト {self.index + 1}/{len(self.questions)}"
                f" ・ {question.entry.category or _category_label(self.category)}"
            ),
            description=f"**この意味の用語は？**\n>>> {question.prompt[:1500]}",
            color=COLOR_DEFAULT,
        )
        if self.chosen is not None:
            if self.chosen == question.answer:
                result = f"✓ 正解！ **{question.answer}**"
            else:
                result = (
                    f"✗ 正解は **{question.answer}** でした"
                    "（単語帳の「要復習」に入れました）"
                )
            embed.add_field(name="結果", value=result, inline=False)
        embed.set_footer(text=f"正解 {self.correct}問")
        return embed

    def result_embed(self):
        total = len(self.questions)
        perfect = self.correct == total
        lines = [f"**{self.correct}/{total}問正解**" + ("　🎉 全問正解！" if perfect else "")]
        if self.missed:
            lines.append(
                "間違えた用語：" + "・".join(f"**{term}**" for term in self.missed)
            )
            lines.append("（単語帳の「要復習」に入れました。`/sg glossary` で見直せます）")
        return discord.Embed(
            title=f"ミニテストおわり ・ {_category_label(self.category)}",
            description="\n".join(lines),
            color=COLOR_SUCCESS if perfect else COLOR_DEFAULT,
        )

    async def answer(self, interaction, term):
        if self.chosen is not None:
            await interaction.response.defer()
            return
        self.chosen = term
        question = self.question
        if term == question.answer:
            self.correct += 1
        else:
            self.missed.append(question.answer)
            try:
                save_sg_glossary_rating(
                    config.DB_PATH, self.owner_id, question.entry, "まだ要復習"
                )
            except (OSError, sqlite3.Error) as error:
                print(f"[quiz] 自己評価を保存できません: {error}")
        self._rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def next_question(self, interaction):
        self.index += 1
        self.chosen = None
        if self.finished:
            save_quiz_result(
                config.DB_PATH, self.owner_id, datetime.now(JST).date(),
                None if self.category == ALL_CATEGORIES else self.category,
                self.correct, len(self.questions),
            )
        self._rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)
        if self.finished:
            await on_quiz_finished(interaction, self.owner_id)


async def on_quiz_finished(interaction, user_id):
    """ミニテストが終わったときの処理（実績バッジの確認）。"""
    await announce_new_badges(interaction.guild, user_id)


@sg_group.command(
    name="quiz",
    description=f"SG用語の4択ミニテスト（{QUIZ_SIZE}問）",
)
@app_commands.describe(category="出題する分野（省略すると全分野）")
@app_commands.choices(category=[
    app_commands.Choice(name="全分野", value=ALL_CATEGORIES),
    *(app_commands.Choice(name=name, value=name) for name in SG_GLOSSARY_CATEGORIES),
])
async def quiz(ctx, category: str = ALL_CATEGORIES):
    if category != ALL_CATEGORIES and category not in SG_GLOSSARY_CATEGORIES:
        await send_private(ctx, "分野は候補から選んでください。")
        return
    questions, error = load_quiz(ctx.author.id, category)
    if error:
        await send_private(ctx, error)
        return
    view = QuizView(ctx.author.id, questions, category)
    await send_private(ctx, embed=view.embed(), view=view)
