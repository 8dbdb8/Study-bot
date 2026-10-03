"""/plan exam：試験日を年・月・日から選ぶ画面。"""

from datetime import date, datetime

import discord

from studybot import config
from studybot.config import JST
from studybot.exam_schedule import (
    build_exam_date,
    days_in_month,
    delete_exam_date,
    exam_year_choices,
    format_exam_countdown,
    save_exam_date,
    WEEKDAY_LABELS,
)
from studybot.groups import plan_group
from studybot.stats import get_current_exam_target


# ============================================================
# 試験日設定（/plan exam）
# ============================================================

class ExamDatePartSelect(discord.ui.Select):
    def __init__(self, part, choices, placeholder, selected, row,
                 disabled=False):
        options = [
            discord.SelectOption(
                label=label,
                value=str(value),
                default=value == selected,
            )
            for label, value in choices
        ] or [discord.SelectOption(label="—", value="0")]
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            row=row,
            disabled=disabled or not choices,
        )
        self.part = part

    async def callback(self, interaction):
        await self.view.select_part(
            interaction, self.part, int(self.values[0])
        )


class ExamDateView(discord.ui.View):
    def __init__(self, owner_id, target, today):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.target = target
        self.today = today
        current = target["exam_on"]
        if current is not None and current >= today:
            self.year, self.month, self.day = (
                current.year, current.month, current.day
            )
        else:
            self.year = self.month = self.day = None
        self._build()

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この設定画面は実行した本人専用です。", ephemeral=True
        )
        return False

    def _valid_months(self):
        if self.year is None:
            return []
        first = self.today.month if self.year == self.today.year else 1
        return list(range(first, 13))

    def _valid_days(self):
        if self.year is None or self.month is None:
            return []
        first = (
            self.today.day
            if (self.year, self.month)
            == (self.today.year, self.today.month)
            else 1
        )
        return list(
            range(first, days_in_month(self.year, self.month) + 1)
        )

    def selected_date(self):
        if None in (self.year, self.month, self.day):
            return None
        try:
            return build_exam_date(
                self.year, self.month, self.day, self.today
            )
        except ValueError:
            return None

    def _build(self):
        # 年や月を変えて存在しなくなった選択は外す
        if self.month not in self._valid_months():
            self.month = None
        if self.day not in self._valid_days():
            self.day = None

        self.clear_items()
        self.add_item(ExamDatePartSelect(
            "year",
            [(f"{year}年", year) for year in exam_year_choices(self.today)],
            "年を選択",
            self.year,
            row=0,
        ))
        self.add_item(ExamDatePartSelect(
            "month",
            [(f"{month}月", month) for month in self._valid_months()],
            "月を選択" if self.year else "先に年を選択",
            self.month,
            row=1,
            disabled=self.year is None,
        ))

        day_choices = [
            (
                f"{day}日（"
                f"{WEEKDAY_LABELS[date(self.year, self.month, day).weekday()]}）",
                day,
            )
            for day in self._valid_days()
        ]
        if not day_choices:
            self.add_item(ExamDatePartSelect(
                "day", [], "先に年と月を選択", None, row=2, disabled=True,
            ))
        elif len(day_choices) <= 25:
            self.add_item(ExamDatePartSelect(
                "day", day_choices, "日を選択", self.day, row=2,
            ))
        else:
            # Discordのセレクトは25件までなので前半・後半に分ける
            for row, chunk in (
                (2, day_choices[:16]), (3, day_choices[16:])
            ):
                self.add_item(ExamDatePartSelect(
                    "day",
                    chunk,
                    f"日を選択（{chunk[0][1]}〜{chunk[-1][1]}日）",
                    self.day,
                    row=row,
                ))

        save_button = discord.ui.Button(
            label="この日で保存",
            style=discord.ButtonStyle.success,
            disabled=self.selected_date() is None,
            row=4,
        )
        save_button.callback = self.save
        self.add_item(save_button)

        if self.target["exam_on"] is not None:
            delete_button = discord.ui.Button(
                label="試験日を削除",
                style=discord.ButtonStyle.danger,
                row=4,
            )
            delete_button.callback = self.delete
            self.add_item(delete_button)

        close_button = discord.ui.Button(
            label="閉じる",
            style=discord.ButtonStyle.secondary,
            row=4,
        )
        close_button.callback = self.close
        self.add_item(close_button)

    def content(self):
        current = self.target["exam_on"]
        lines = [f"**{self.target['display_name']} の試験日**"]
        lines.append(
            "現在の設定："
            + (
                format_exam_countdown(current, self.today)
                if current is not None else "未設定"
            )
        )

        year_text = f"{self.year}年" if self.year else "□年"
        month_text = f"{self.month}月" if self.month else "□月"
        day_text = f"{self.day}日" if self.day else "□日"
        lines.append(f"\n選択中：**{year_text}{month_text}{day_text}**")

        selected = self.selected_date()
        if selected is not None:
            lines.append(
                "保存すると：" + format_exam_countdown(selected, self.today)
            )
        else:
            lines.append(
                "年・月・日を順に選んで「この日で保存」を押してください。"
            )
        return "\n".join(lines)

    async def select_part(self, interaction, part, value):
        setattr(self, part, value)
        self._build()
        await interaction.response.edit_message(
            content=self.content(), view=self
        )

    async def save(self, interaction):
        selected = self.selected_date()
        if selected is None:
            await interaction.response.send_message(
                "年・月・日をすべて選んでください。", ephemeral=True
            )
            return

        save_exam_date(
            config.DB_PATH,
            self.owner_id,
            self.target["qualification"],
            selected,
            datetime.now(JST).isoformat(),
        )
        self.stop()
        await interaction.response.edit_message(
            content=(
                "試験日を保存しました。\n"
                + format_exam_countdown(
                    selected, self.today, self.target["label"]
                )
                + "\n`/plan new` で weeks を省略すると、"
                "この日までの週数で計画を作ります。"
            ),
            view=None,
        )

    async def delete(self, interaction):
        delete_exam_date(
            config.DB_PATH, self.owner_id, self.target["qualification"]
        )
        self.stop()
        await interaction.response.edit_message(
            content=f"{self.target['label']}の試験日を削除しました。",
            view=None,
        )

    async def close(self, interaction):
        self.stop()
        await interaction.response.edit_message(
            content="試験日は変更していません。", view=None
        )


@plan_group.command(
    name="exam",
    description="試験日を年・月・日から選んで設定・確認"
)
async def exam_command(ctx):
    view = ExamDateView(
        ctx.author.id,
        get_current_exam_target(ctx.author.id),
        datetime.now(JST).date(),
    )
    await ctx.send(view.content(), view=view, ephemeral=True)
