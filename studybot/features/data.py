"""/data：自分の記録の確認・修正・リセット。"""

import discord
from discord import app_commands

from studybot import config
from studybot.data_management import (
    apply_edit,
    editable_fields,
    FIELDS,
    get_overview,
    get_record,
    KINDS,
    list_records,
    prepare_edit,
    prepare_reset,
    RESET_SCOPES,
    reset_user_data,
)
from studybot.formatting import format_duration
from studybot.sg_features import SG_B_TOPICS
from studybot.stats import get_study_status
from studybot.study_log_parser import SG_PRACTICE_CATEGORIES


class DataOwnerView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=300)
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "このデータ画面は実行した本人専用です。", ephemeral=True
        )
        return False


def _data_record_label(kind, row):
    if kind == "log":
        section = "科目B" if row["section"] == "B" else "科目A/旧形式"
        score = row["score_percent"]
        score_text = f"{score:g}%" if score is not None else "正答率なし"
        questions = (
            f"{row['questions']}問" if row["questions"] is not None
            else "問題数なし"
        )
        return f"{row['day']} {section} {questions} {score_text}"
    if kind == "session":
        return f"{row['day']} {format_duration(row['duration_seconds'])}"
    if kind == "mistake":
        state = "完了" if row["completed_on"] else "復習中"
        return f"{row['category']} / {row['question_ref'][:45]} [{state}]"
    return (
        f"{row['day']}開始 {row['weeks']}週 "
        f"{row['weekly_questions']}問/週"
    )


def _data_home_text(user_id):
    overview = get_overview(config.DB_PATH, user_id)
    sg = get_study_status(user_id, "SG")
    score = sg["average_score"]
    score_text = f"{score:.1f}%" if score is not None else "記録なし"
    lines = [
        "**保存データ一覧（自分の記録）**",
        f"SG：{overview['logs']['sg'] or 0}件 / "
        f"{sg['total_questions']}問 / 平均{score_text}",
        f"うち科目B：{overview['logs']['b'] or 0}件 / "
        f"その他・未解析ログ："
        f"{overview['logs']['total'] - (overview['logs']['sg'] or 0)}件",
        f"通話勉強：{overview['sessions']['total']}回 / "
        f"{format_duration(overview['sessions']['seconds'])}",
        f"復習問題：{overview['mistakes']['total']}件 "
        f"（未完了{overview['mistakes']['open_count'] or 0}件）",
        f"SG単語帳の自己評価：{overview['glossary_ratings']['total']}語",
        f"週次計画：{overview['plans']['total']}件 "
        f"（有効{overview['plans']['active_count'] or 0}件）",
    ]
    for kind, title, limit in (
        ("log", "最近のSGログ", 3),
        ("session", "最近の通話", 3),
        ("mistake", "最近の誤答", 2),
        ("plan", "最近の計画", 2),
    ):
        records, _ = list_records(config.DB_PATH, user_id, kind, page_size=limit)
        if records:
            lines.extend(["", f"**{title}**"])
            lines.extend(f"- {_data_record_label(kind, row)}" for row in records)
    lines.extend([
        "", "修正・リセットはDB内の記録に反映します。",
        "元のDiscord投稿は変更されません。",
    ])
    return "\n".join(lines)


class DataHomeView(DataOwnerView):
    @discord.ui.button(label="修正", style=discord.ButtonStyle.primary)
    async def edit(self, interaction, button):
        await interaction.response.edit_message(
            content="修正するデータの種類を選択してください。",
            view=DataKindView(self.owner_id),
        )

    @discord.ui.button(label="リセット", style=discord.ButtonStyle.danger)
    async def reset(self, interaction, button):
        await interaction.response.edit_message(
            content="リセットする範囲を選択してください。DB内の記録だけを削除します。",
            view=DataResetScopeView(self.owner_id),
        )

    @discord.ui.button(label="そのまま", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction, button):
        await interaction.response.edit_message(
            content="変更せず終了しました。", view=None,
        )


class DataKindSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="修正するデータを選択",
            options=[
                discord.SelectOption(label=label, value=kind)
                for kind, label in KINDS.items()
            ],
        )

    async def callback(self, interaction):
        kind = self.values[0]
        view = DataRecordListView(self.view.owner_id, kind)
        await interaction.response.edit_message(
            content=view.description, view=view,
        )


class DataKindView(DataOwnerView):
    def __init__(self, owner_id):
        super().__init__(owner_id)
        self.add_item(DataKindSelect())

    @discord.ui.button(label="一覧へ戻る", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=_data_home_text(self.owner_id),
            view=DataHomeView(self.owner_id),
        )


class DataRecordSelect(discord.ui.Select):
    def __init__(self, kind, rows):
        super().__init__(
            placeholder="修正する記録を選択",
            options=[
                discord.SelectOption(
                    label=_data_record_label(kind, row)[:100],
                    value=str(row["id"]),
                    description=f"記録ID: {row['id']}",
                )
                for row in rows
            ],
        )

    async def callback(self, interaction):
        view = DataFieldView(
            self.view.owner_id, self.view.kind, int(self.values[0]),
        )
        await interaction.response.edit_message(
            content=view.description, view=view,
        )


class DataRecordListView(DataOwnerView):
    def __init__(self, owner_id, kind, page=0):
        super().__init__(owner_id)
        self.kind = kind
        self.page = page
        rows, has_next = list_records(config.DB_PATH, owner_id, kind, page)
        self.description = (
            f"**{KINDS[kind]}**（{page + 1}ページ目）\n"
            "修正する記録を選択してください。"
            if rows else f"**{KINDS[kind]}**の記録はありません。"
        )
        if rows:
            self.add_item(DataRecordSelect(kind, rows))
        self.previous.disabled = page == 0
        self.next_page.disabled = not has_next

    @discord.ui.button(label="前へ", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction, button):
        view = DataRecordListView(self.owner_id, self.kind, self.page - 1)
        await interaction.response.edit_message(content=view.description, view=view)

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction, button):
        view = DataRecordListView(self.owner_id, self.kind, self.page + 1)
        await interaction.response.edit_message(content=view.description, view=view)

    @discord.ui.button(label="戻る", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content="修正するデータの種類を選択してください。",
            view=DataKindView(self.owner_id),
        )


def _data_record_description(kind, record):
    if record is None:
        return "記録が見つかりません。"
    if kind == "log":
        categories = "、".join(
            row["category"] or row["major_category"]
            for row in record["categories"]
        ) or "未分類"
        score = record["score_percent"]
        score_text = f"{score:g}%" if score is not None else "記録なし"
        return (
            f"**SGログ {record['day']}**\n"
            f"分野：{categories}\n問題数：{record['questions']}問 / "
            f"正解数：{record['correct_answers']} / 正答率：{score_text}\n"
            "修正する項目を選択してください。"
        )
    if kind == "session":
        return (
            f"**通話勉強時間 {record['day']}**\n"
            f"開始：{record['start_time']}\n"
            f"終了：{record['end_time']}\n"
            f"記録時間：{format_duration(record['duration_seconds'])}\n"
            "修正する項目を選択してください。"
        )
    if kind == "mistake":
        state = "完了" if record["completed_on"] else "復習中"
        return (
            f"**誤答 #{record['id']} [{state}]**\n"
            f"分野：{record['category']}\n"
            f"問題：{record['question_ref']}\n"
            f"理由：{record['reason']}\n"
            f"メモ：{record['memo'] or 'なし'}\n"
            "修正する項目を選択してください。"
        )[:1800]
    return (
        f"**週次計画 #{record['id']}**\n"
        f"開始：{record['start_on']} / {record['weeks']}週間\n"
        f"基本目標：{record['weekly_questions']}問/週\n"
        f"状態：{'有効' if record['active'] else '過去の計画'}\n"
        "修正する項目を選択してください。"
    )


class DataFieldSelect(discord.ui.Select):
    def __init__(self, record, kind):
        super().__init__(
            placeholder="修正する項目を選択",
            options=[
                discord.SelectOption(label=label, value=field)
                for field, label in editable_fields(record, kind).items()
            ],
        )

    async def callback(self, interaction):
        view = self.view
        field = self.values[0]
        if field == "category":
            choices = (
                SG_B_TOPICS
                if view.kind == "log" and view.record["exam_section"] == "B"
                else SG_PRACTICE_CATEGORIES
            )
            await interaction.response.edit_message(
                content="正しい分野・テーマを選択してください。",
                view=DataChoiceValueView(
                    view.owner_id, view.kind, view.record_id, field, choices,
                ),
            )
        else:
            await interaction.response.send_modal(
                DataEditModal(view.owner_id, view.kind, view.record_id, field)
            )


class DataFieldView(DataOwnerView):
    def __init__(self, owner_id, kind, record_id):
        super().__init__(owner_id)
        self.kind = kind
        self.record_id = record_id
        self.record = get_record(config.DB_PATH, owner_id, kind, record_id)
        self.description = _data_record_description(kind, self.record)
        if self.record is not None:
            self.add_item(DataFieldSelect(self.record, kind))

    @discord.ui.button(label="記録一覧へ", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        view = DataRecordListView(self.owner_id, self.kind)
        await interaction.response.edit_message(
            content=view.description, view=view,
        )


class DataChoiceSelect(discord.ui.Select):
    def __init__(self, choices):
        super().__init__(
            placeholder="分野を選択",
            options=[discord.SelectOption(label=item, value=item) for item in choices],
        )

    async def callback(self, interaction):
        view = self.view
        try:
            preview = prepare_edit(
                config.DB_PATH, view.owner_id, view.kind, view.record_id,
                view.field, self.values[0],
            )
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.edit_message(
            content=_edit_preview_text(preview),
            view=DataConfirmEditView(
                view.owner_id, view.kind, view.record_id,
                view.field, preview,
            ),
        )


class DataChoiceValueView(DataOwnerView):
    def __init__(self, owner_id, kind, record_id, field, choices):
        super().__init__(owner_id)
        self.kind = kind
        self.record_id = record_id
        self.field = field
        self.add_item(DataChoiceSelect(choices))

    @discord.ui.button(label="項目選択へ", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        view = DataFieldView(self.owner_id, self.kind, self.record_id)
        await interaction.response.edit_message(content=view.description, view=view)


def _edit_preview_text(preview):
    return (
        f"**変更前後の確認：{preview['field_label']}**\n"
        f"変更前：{preview['before']}\n"
        f"変更後：{preview['after']}\n\n"
        "DBの記録をこの内容へ修正しますか？"
    )


class DataEditModal(discord.ui.Modal):
    def __init__(self, owner_id, kind, record_id, field):
        label = FIELDS[kind][field]
        super().__init__(title=f"修正：{label}")
        self.owner_id = owner_id
        self.kind = kind
        self.record_id = record_id
        self.field = field
        self.input = discord.ui.TextInput(
            label=label,
            placeholder="開始日はYYYY-MM-DD形式" if field == "start_on" else None,
            style=(discord.TextStyle.paragraph if field in (
                "notes", "reason", "wrong_reason", "memo"
            ) else discord.TextStyle.short),
            required=field not in ("notes", "memo", "wrong_reason"),
            max_length=500,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "この入力画面は実行した本人専用です。", ephemeral=True,
            )
            return
        try:
            preview = prepare_edit(
                config.DB_PATH, self.owner_id, self.kind, self.record_id,
                self.field, self.input.value,
            )
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.send_message(
            _edit_preview_text(preview),
            view=DataConfirmEditView(
                self.owner_id, self.kind, self.record_id,
                self.field, preview,
            ),
            ephemeral=True,
        )


class DataConfirmEditView(DataOwnerView):
    def __init__(self, owner_id, kind, record_id, field, preview):
        super().__init__(owner_id)
        self.kind = kind
        self.record_id = record_id
        self.field = field
        self.preview = preview

    @discord.ui.button(label="修正を確定", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        try:
            apply_edit(
                config.DB_PATH, self.owner_id, self.kind, self.record_id,
                self.field, self.preview["value"], self.preview["record"],
            )
        except ValueError as error:
            await interaction.response.edit_message(content=str(error), view=None)
            return
        sg_text = ""
        if self.kind == "log":
            status = get_study_status(self.owner_id, "SG")
            score = status["average_score"]
            sg_text = (
                f"\nSG累計：{status['total_questions']}問 / "
                f"平均{score:.1f}%" if score is not None else
                f"\nSG累計：{status['total_questions']}問 / 正答率なし"
            )
        await interaction.response.edit_message(
            content=f"DBの記録を修正しました。{sg_text}", view=None,
        )

    @discord.ui.button(label="変更しない", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content="変更せず終了しました。", view=None,
        )


class DataResetScopeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="リセットする範囲を選択",
            options=[
                discord.SelectOption(label=label, value=scope)
                for scope, label in RESET_SCOPES.items()
            ],
        )

    async def callback(self, interaction):
        scope = self.values[0]
        try:
            ids = prepare_reset(config.DB_PATH, self.view.owner_id, scope)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        count = sum(len(items) for items in ids.values())
        await interaction.response.edit_message(
            content=(
                f"**リセットの最終確認**\n"
                f"対象：{RESET_SCOPES[scope]}\n"
                f"削除する記録：{count}件\n"
                "この操作は取り消せません。元のDiscord投稿は残ります。\n"
                "本当にDB内の記録を削除しますか？"
            ),
            view=DataConfirmResetView(self.view.owner_id, scope, ids),
        )


class DataResetScopeView(DataOwnerView):
    def __init__(self, owner_id):
        super().__init__(owner_id)
        self.add_item(DataResetScopeSelect())

    @discord.ui.button(label="一覧へ戻る", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await interaction.response.edit_message(
            content=_data_home_text(self.owner_id),
            view=DataHomeView(self.owner_id),
        )


class DataConfirmResetView(DataOwnerView):
    def __init__(self, owner_id, scope, ids):
        super().__init__(owner_id)
        self.scope = scope
        self.ids = ids

    @discord.ui.button(label="リセットを確定", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        try:
            count = reset_user_data(
                config.DB_PATH, self.owner_id, self.scope, self.ids,
            )
        except ValueError as error:
            await interaction.response.edit_message(content=str(error), view=None)
            return
        await interaction.response.edit_message(
            content=f"DB内の{RESET_SCOPES[self.scope]}をリセットしました（{count}件）。",
            view=None,
        )

    @discord.ui.button(label="変更しない", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(
            content="変更せず終了しました。", view=None,
        )


@app_commands.command(name="data", description="自分のDB記録を確認・修正・リセット")
async def data_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        _data_home_text(interaction.user.id),
        view=DataHomeView(interaction.user.id),
        ephemeral=True,
    )
