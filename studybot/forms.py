"""過去問・科目B・誤答を入力する画面（選択肢とフォーム）。

資格（SG・FE など）を受け取り、その資格の分野やテーマで入力させる。
"""

from datetime import datetime
from types import SimpleNamespace

import discord

from studybot import config
from studybot.analysis import (
    build_analysis_reply,
    build_structured_sg_analysis,
    build_structured_sg_b_analysis,
)
from studybot.channels import find_channel
from studybot.config import JST
from studybot.database import (
    delete_study_log_data,
    save_study_analysis,
    save_study_log,
)
from studybot.qualifications import SG
from studybot.sg_features import (
    add_sg_mistake,
    parse_correct_count,
    save_sg_b_practice,
)
from studybot.stats import get_study_status
from studybot.study_log_parser import (
    parse_question_count_input,
    parse_score_percent_input,
)


def _source_text(qualification):
    """記録の文面に使う出どころ（例：SG過去問道場、医療情報技師 過去問・問題集）。"""
    source = qualification.practice_source
    if source.startswith(qualification.code):
        return source
    return f"{qualification.code} {source}"


def category_options(qualification, default=None):
    """分野の選択肢。説明には進捗表示の見出し（例：テクノロジ系）を付ける。"""
    group_of = {
        category: group
        for group, categories in qualification.progress_groups
        for category in categories
    }
    return [
        discord.SelectOption(
            label=category,
            value=category,
            description=group_of.get(category),
            default=category == default,
        )
        for category in qualification.category_names
    ]


class SGStudyLogModal(discord.ui.Modal):
    """過去問の記録フォーム。

    category を渡さなければ、フォームの一番上で分野を選ぶ（1画面で入力できる）。
    default_category を渡すと、その分野を最初から選んだ状態にする。
    """

    def __init__(self, category, target_channel, qualification=SG,
                 default_category=None):
        super().__init__(title=f"{_source_text(qualification)}を記録"[:45])
        self.category = category
        self.target_channel = target_channel
        self.qualification = qualification

        self.category_select = None
        if category is None:
            self.category_select = discord.ui.Select(
                placeholder="学習した分野を1つ選択",
                options=category_options(qualification, default_category),
            )
            self.add_item(discord.ui.Label(
                text="分野", component=self.category_select
            ))
        self.questions_input = discord.ui.TextInput(
            placeholder="例：25", min_length=1, max_length=4,
        )
        self.score_input = discord.ui.TextInput(
            placeholder="例：40 または 40.25", min_length=1, max_length=7,
        )
        self.notes_input = discord.ui.TextInput(
            placeholder="気になった用語や次回見直す内容",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=300,
        )
        self.add_item(discord.ui.Label(
            text="解いた問題数", component=self.questions_input
        ))
        self.add_item(discord.ui.Label(
            text="正答率（%）", component=self.score_input
        ))
        self.add_item(discord.ui.Label(
            text="メモ（任意）", component=self.notes_input
        ))

    async def on_submit(self, interaction):
        qualification = self.qualification
        if self.category_select is not None:
            self.category = self.category_select.values[0]
        try:
            questions = parse_question_count_input(
                self.questions_input.value
            )
            score_percent = parse_score_percent_input(
                self.score_input.value
            )
        except ValueError as error:
            await interaction.response.send_message(
                f"⚠️ {error}",
                ephemeral=True
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "⚠️ 記録はサーバー内で入力してください。",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        log_message = None

        try:
            log_message = await self.target_channel.send(
                f"{qualification.code}の勉強ログを記録しています..."
            )
            major_category = qualification.category_to_major[
                self.category
            ]
            score_text = f"{score_percent:.1f}"
            content = (
                f"{_source_text(qualification)}{questions}問。"
                f"正答率{score_text}%。"
                f"全て{major_category}の"
                f"{self.category}分野。"
            )
            message_for_db = SimpleNamespace(
                id=log_message.id,
                guild=interaction.guild,
                channel=self.target_channel,
                author=interaction.user,
                created_at=log_message.created_at,
                content=content
            )
            analysis = build_structured_sg_analysis(
                self.category,
                questions,
                score_percent,
                self.notes_input.value,
                qualification.code,
            )

            save_study_log(message_for_db)
            save_study_analysis(
                message_for_db,
                analysis
            )
            status_data = get_study_status(
                interaction.user.id,
                qualification.code
            )
            reply_text = build_analysis_reply(
                analysis,
                status_data
            )

            await log_message.edit(
                content=reply_text,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await interaction.followup.send(
                f"記録しました：{self.category} / "
                f"{questions}問 / {score_text}%",
                ephemeral=True
            )

        except Exception as error:
            print(f"❌ sglog 保存エラー: {error}")

            if log_message is not None:
                try:
                    await log_message.edit(
                        content=(
                            "⚠️ 勉強ログの保存に失敗しました。"
                        )
                    )
                except discord.HTTPException:
                    pass

            await interaction.followup.send(
                "⚠️ 勉強ログを保存できませんでした。\n"
                "studybot.log を確認してください。",
                ephemeral=True
            )


class SGCategorySelect(discord.ui.Select):
    def __init__(self, action_label="問題数と正答率を入力", qualification=SG):
        self.action_label = action_label
        options = category_options(qualification)

        super().__init__(
            placeholder="学習した分野を1つ選択",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction):
        self.view.selected_category = self.values[0]

        await interaction.response.edit_message(
            content=(
                f"選択中：**{self.values[0]}**\n"
                f"「{self.action_label}」を押してください。"
            ),
            view=self.view
        )


class SGStudyLogView(discord.ui.View):
    def __init__(self, owner_id, target_channel, qualification=SG):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.target_channel = target_channel
        self.qualification = qualification
        self.selected_category = None
        self.add_item(SGCategorySelect(qualification=qualification))

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            "この入力画面はコマンドを実行した本人専用です。",
            ephemeral=True
        )
        return False

    @discord.ui.button(
        label="問題数と正答率を入力",
        style=discord.ButtonStyle.primary
    )
    async def open_modal(self, interaction, button):
        if self.selected_category is None:
            await interaction.response.send_message(
                "先に学習した分野を選択してください。",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            SGStudyLogModal(
                self.selected_category,
                self.target_channel,
                self.qualification,
            )
        )


class SGMistakeModal(discord.ui.Modal, title="SG誤答を登録"):
    reference_input = discord.ui.TextInput(
        label="問題のURLまたは番号",
        placeholder="例：https://... または 令和6年 問12",
        max_length=200,
    )
    reason_input = discord.ui.TextInput(
        label="間違えた理由",
        placeholder="例：アクセス制御の条件を読み違えた",
        style=discord.TextStyle.paragraph,
        max_length=300,
    )
    memo_input = discord.ui.TextInput(
        label="次回確認すること（任意）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, category, qualification=SG):
        super().__init__(title=f"{qualification.code}の誤答を登録")
        self.category = category
        self.qualification = qualification

    async def on_submit(self, interaction):
        try:
            mistake_id, due = add_sg_mistake(
                config.DB_PATH,
                interaction.user.id,
                self.category,
                self.reference_input.value,
                self.reason_input.value,
                self.memo_input.value,
                today=datetime.now(JST).date(),
                qualification=self.qualification.code,
            )
        except ValueError as error:
            await interaction.response.send_message(
                str(error), ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"誤答 #{mistake_id} を登録しました。"
            f"次の復習日：{due.isoformat()}\n"
            "復習するときは `/review list` を開いてください。",
            ephemeral=True,
        )


class SGMistakeView(discord.ui.View):
    def __init__(self, owner_id, qualification=SG):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.qualification = qualification
        self.selected_category = None
        self.add_item(SGCategorySelect("誤答を入力", qualification))

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この画面はコマンドを実行した本人専用です。",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="誤答を入力", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction, button):
        if self.selected_category is None:
            await interaction.response.send_message(
                "先に分野を選択してください。", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            SGMistakeModal(self.selected_category, self.qualification)
        )


class SGBPracticeModal(discord.ui.Modal, title="SG科目Bの演習結果"):
    questions_input = discord.ui.TextInput(
        label="解いた問題数", placeholder="例：5", max_length=4
    )
    correct_input = discord.ui.TextInput(
        label="正解数", placeholder="例：3", max_length=4
    )
    reason_input = discord.ui.TextInput(
        label="判断を間違えた理由（全問正解なら空欄）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )
    memo_input = discord.ui.TextInput(
        label="メモ（任意）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, topic, target_channel, qualification=SG):
        super().__init__(title=f"{qualification.code}科目Bの演習結果")
        self.topic = topic
        self.target_channel = target_channel
        self.qualification = qualification

    async def on_submit(self, interaction):
        try:
            questions = parse_question_count_input(
                self.questions_input.value
            )
            correct = parse_correct_count(
                self.correct_input.value, questions
            )
            reason = (self.reason_input.value or "").strip()
            if correct < questions and not reason:
                raise ValueError("誤答がある場合は判断を間違えた理由を入力してください。")
        except ValueError as error:
            await interaction.response.send_message(
                str(error), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        log_message = None
        saved = False
        try:
            log_message = await self.target_channel.send(
                f"{self.qualification.code}科目Bの演習結果を記録しています..."
            )
            analysis = build_structured_sg_b_analysis(
                self.topic, questions, correct,
                reason, self.memo_input.value,
                qualification=self.qualification.code,
            )
            message_for_db = SimpleNamespace(
                id=log_message.id,
                guild=interaction.guild,
                channel=self.target_channel,
                author=interaction.user,
                created_at=log_message.created_at,
                content=(
                    f"{self.qualification.code}科目B {self.topic}を{questions}問中"
                    f"{correct}問正解。"
                ),
            )
            save_study_log(message_for_db)
            save_study_analysis(message_for_db, analysis)
            save_sg_b_practice(
                config.DB_PATH, log_message.id, interaction.user.id,
                self.topic, questions, correct, reason,
                self.memo_input.value,
                log_message.created_at.astimezone(JST).date().isoformat(),
                qualification=self.qualification.code,
            )
            saved = True
            status_data = get_study_status(
                interaction.user.id, self.qualification.code
            )
            await log_message.edit(
                content=build_analysis_reply(analysis, status_data),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await interaction.followup.send(
                f"科目Bを記録しました：{self.topic} / "
                f"{correct}/{questions}問正解 "
                f"({analysis['score_percent']:.1f}%)",
                ephemeral=True,
            )
        except Exception as error:
            print(f"[{self.qualification.code}科目B] 保存エラー: {error}")
            if saved:
                await interaction.followup.send(
                    "科目BはDBに記録済みですが、Discord表示の更新に"
                    "失敗しました。"
                    f"`/{self.qualification.command} progress` で確認してください。",
                    ephemeral=True,
                )
                return
            if log_message is not None:
                delete_study_log_data(log_message.id)
                try:
                    await log_message.edit(
                        content="科目Bの保存に失敗しました。"
                    )
                except discord.HTTPException:
                    pass
            await interaction.followup.send(
                "科目Bを保存できませんでした。実行ログを確認してください。",
                ephemeral=True,
            )


class SGBTopicSelect(discord.ui.Select):
    def __init__(self, qualification=SG):
        super().__init__(
            placeholder="科目Bのテーマを選択",
            options=[
                discord.SelectOption(label=topic, value=topic)
                for topic in qualification.b_topics
            ],
        )

    async def callback(self, interaction):
        self.view.selected_topic = self.values[0]
        await interaction.response.edit_message(
            content=(
                f"選択中：**{self.values[0]}**\n"
                "「演習結果を入力」を押してください。"
            ),
            view=self.view,
        )


class SGBPracticeView(discord.ui.View):
    def __init__(self, owner_id, target_channel, qualification=SG):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.target_channel = target_channel
        self.qualification = qualification
        self.selected_topic = None
        self.add_item(SGBTopicSelect(qualification))

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この画面はコマンドを実行した本人専用です。",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="演習結果を入力", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction, button):
        if self.selected_topic is None:
            await interaction.response.send_message(
                "先にテーマを選択してください。", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            SGBPracticeModal(
                self.selected_topic, self.target_channel, self.qualification
            )
        )


# ============================================================
# 記録の入力画面（コマンドとボタンで共通）
# ============================================================

SGLOG_PROMPT_TEXT = (
    "学習した分野を1つ選択してください。\n"
    "続けて問題数と正答率を数字で入力します。\n"
    "正答率は小数第2位を四捨五入し、"
    "小数第1位で保存します。\n"
    "複数分野は分野ごとに登録してください。"
)


LOG_CHANNEL_MISSING_TEXT = (
    "勉強ログのチャンネルが見つかりません。"
    "`/setup` で設定するか、#勉強ログ を作成してください。"
)


def build_sglog_prompt(guild, user_id, qualification=SG):
    """(案内文, View) を返す。入力できないときは View が None。"""
    if guild is None:
        return "記録はサーバー内で入力してください。", None
    target_channel = find_channel(guild, "study_log")
    if target_channel is None:
        return LOG_CHANNEL_MISSING_TEXT, None
    return (
        f"【{qualification.display_name}】\n" + SGLOG_PROMPT_TEXT,
        SGStudyLogView(user_id, target_channel, qualification),
    )


def build_quick_log_modal(guild, qualification=SG, default_category=None):
    """(フォーム, 案内文)。フォームを開けないときはフォームが None。"""
    if guild is None:
        return None, "記録はサーバー内で入力してください。"
    target_channel = find_channel(guild, "study_log")
    if target_channel is None:
        return None, LOG_CHANNEL_MISSING_TEXT
    modal = SGStudyLogModal(
        None, target_channel, qualification, default_category
    )
    return modal, None


async def open_quick_log(interaction, qualification=SG, default_category=None):
    """ボタンやスラッシュコマンドから、分野つきの記録フォームを開く。"""
    modal, message = build_quick_log_modal(
        interaction.guild, qualification, default_category
    )
    if modal is None:
        await interaction.response.send_message(message, ephemeral=True)
    else:
        await interaction.response.send_modal(modal)


def build_sgb_prompt(guild, user_id, qualification=SG):
    if not qualification.has_part_b:
        return f"{qualification.display_name}には科目Bがありません。", None
    if guild is None:
        return "サーバー内で入力してください。", None
    target_channel = find_channel(guild, "study_log")
    if target_channel is None:
        return LOG_CHANNEL_MISSING_TEXT, None
    return (
        f"【{qualification.display_name}】\n"
        "科目Bで取り組んだテーマを選択してください。",
        SGBPracticeView(user_id, target_channel, qualification),
    )


def build_mistake_prompt(user_id, qualification=SG):
    return (
        f"【{qualification.display_name}】\n間違えた問題の分野を選択してください。",
        SGMistakeView(user_id, qualification),
    )
