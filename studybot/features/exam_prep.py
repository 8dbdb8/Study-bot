"""試験前日の夜（20時）に、持ち物チェックをボタン付きで送る。

ボタンを押すとチェックが付き、全部そろうと「準備OK」になる。
会場や集合時刻は「会場メモ」から書いておける。ボタンは再起動後も使える。
"""

from datetime import date, datetime

import discord

from studybot import config
from studybot.config import JST
from studybot.embeds import COLOR_ALERT, COLOR_SUCCESS
from studybot.exam_prep import (
    MEMO_MAX_LENGTH,
    PREP_ITEMS,
    find_upcoming_prep,
    get_exam_prep,
    get_prep_candidates,
    mark_prep_prompted,
    set_prep_memo,
    toggle_prep_item,
)
from studybot.features.digest import find_home_channel
from studybot.replies import respond_private


def _when(exam_on, today):
    days = (date.fromisoformat(exam_on) - today).days
    if days == 1:
        return "明日"
    if days == 0:
        return "今日"
    day = date.fromisoformat(exam_on)
    return f"{day.month}/{day.day}"


def build_prep_embed(qualification, exam_on, prep, today):
    lines = [
        f"{'✅' if index in prep['checked'] else '⬜'} {text}"
        for index, (text, _) in enumerate(PREP_ITEMS)
    ]
    ready = len(prep["checked"]) == len(PREP_ITEMS)
    if ready:
        lines.append("\n**準備OK！** 今日は早めに休みましょう。")
    embed = discord.Embed(
        title=f"{_when(exam_on, today)}は{qualification}試験です ・ 持ち物チェック",
        description="\n".join(lines),
        color=COLOR_SUCCESS if ready else COLOR_ALERT,
    )
    embed.add_field(
        name="会場メモ",
        value=prep["memo"] or "未入力（「会場メモ」から書いておけます）",
        inline=False,
    )
    embed.set_footer(text="ボタンを押すとチェックが付きます")
    return embed


class PrepItemButton(discord.ui.Button):
    def __init__(self, index, checked=False):
        super().__init__(
            label=PREP_ITEMS[index][1],
            style=(
                discord.ButtonStyle.success if checked
                else discord.ButtonStyle.secondary
            ),
            custom_id=f"studybot:prep:{index}",
        )
        self.index = index

    async def callback(self, interaction):
        target = _find_target(interaction)
        if target is None:
            await respond_private(interaction, NO_TARGET_TEXT)
            return
        qualification, exam_on = target
        prep = toggle_prep_item(
            config.DB_PATH, interaction.user.id, qualification, exam_on,
            self.index,
        )
        await _show(interaction, qualification, exam_on, prep)


class PrepMemoModal(discord.ui.Modal):
    def __init__(self, qualification, exam_on, memo=None):
        super().__init__(title="会場メモ")
        self.qualification = qualification
        self.exam_on = exam_on
        self.memo_input = discord.ui.TextInput(
            placeholder="例：○○テストセンター 9:30集合／○○駅から徒歩5分",
            style=discord.TextStyle.paragraph,
            default=memo,
            required=False,
            max_length=MEMO_MAX_LENGTH,
        )
        self.add_item(discord.ui.Label(
            text="会場・集合時刻など", component=self.memo_input
        ))

    async def on_submit(self, interaction):
        prep = set_prep_memo(
            config.DB_PATH, interaction.user.id, self.qualification,
            self.exam_on, self.memo_input.value,
        )
        await _show(interaction, self.qualification, self.exam_on, prep)


class PrepMemoButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="会場メモ",
            style=discord.ButtonStyle.primary,
            custom_id="studybot:prep:memo",
        )

    async def callback(self, interaction):
        target = _find_target(interaction)
        if target is None:
            await respond_private(interaction, NO_TARGET_TEXT)
            return
        qualification, exam_on = target
        prep = get_exam_prep(
            config.DB_PATH, interaction.user.id, qualification, exam_on
        )
        await interaction.response.send_modal(
            PrepMemoModal(qualification, exam_on, prep["memo"])
        )


class ExamPrepView(discord.ui.View):
    """持ち物チェックのボタン。押した本人の、これからの試験のチェックを付ける。"""

    def __init__(self, checked=frozenset()):
        super().__init__(timeout=None)
        for index in range(len(PREP_ITEMS)):
            self.add_item(PrepItemButton(index, index in checked))
        self.add_item(PrepMemoButton())


NO_TARGET_TEXT = "持ち物チェックをする試験が見つかりません（試験日が過ぎています）。"


def _find_target(interaction):
    return find_upcoming_prep(
        config.DB_PATH, interaction.user.id, datetime.now(JST).date()
    )


async def _show(interaction, qualification, exam_on, prep):
    await interaction.response.edit_message(
        embed=build_prep_embed(
            qualification, exam_on, prep, datetime.now(JST).date()
        ),
        view=ExamPrepView(prep["checked"]),
    )


async def send_exam_prep(bot, user_id, qualification, exam_on, today):
    channel = await find_home_channel(bot, user_id)
    if channel is None:
        return False
    prep = get_exam_prep(config.DB_PATH, user_id, qualification, exam_on)
    await channel.send(
        content=(
            f"<@{user_id}> 明日は{qualification}試験です。"
            "持ち物と会場を確認しておきましょう。"
        ),
        embed=build_prep_embed(qualification, exam_on, prep, today),
        view=ExamPrepView(prep["checked"]),
        allowed_mentions=discord.AllowedMentions(
            users=[discord.Object(id=user_id)]
        ),
    )
    mark_prep_prompted(config.DB_PATH, user_id, qualification, exam_on, today)
    return True


async def send_exam_prep_prompts(bot, now=None):
    """明日が試験日の人に送る。送った数を返す。"""
    today = (now or datetime.now(JST)).date()
    sent = 0
    for user_id, qualification, exam_on in get_prep_candidates(
        config.DB_PATH, today
    ):
        try:
            if await send_exam_prep(bot, user_id, qualification, exam_on, today):
                sent += 1
        except Exception as error:
            print(f"[prep] 送信エラー user={user_id}: {error}")
    return sent
