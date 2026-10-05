"""勉強部屋の入退室で勉強時間を記録し、退出時に通知する。"""

from datetime import datetime

import discord

from studybot import config
from studybot.channels import channel_label, find_channel, is_channel
from studybot.config import JST
from studybot.database import (
    delete_active_study_session,
    get_active_study_session,
    get_all_active_study_sessions,
    save_active_study_session,
    save_completed_study_session,
)
from studybot.daily_digest import is_compact_display
from studybot.embeds import build_vc_summary_embed
from studybot.formatting import format_duration
from studybot.forms import (
    build_mistake_prompt,
    build_sgb_prompt,
)
from studybot.checklist import checklist_status, checklist_summary, format_checklist
from studybot.activity_forms import open_study_log_prompt
from studybot.features.badges import announce_new_badges
from studybot.features.notes import open_note_modal
from studybot.habits import format_goal_progress, get_focus_sets, goal_for_day
from studybot.qualifications import current_qualification
from studybot.replies import respond_private
from studybot.stats import (
    get_exam_countdown_line,
    get_study_streak_safe,
    get_today_total,
    get_week_total,
)


# ============================================================
# 現在勉強中のセッション（メモリ）
# key = (guild_id, user_id)
# ============================================================

study_sessions = {}


# ============================================================
# 起動時の進行中VCセッション復元
# ============================================================

async def recover_active_voice_sessions(bot):
    """
    進行中セッションをSQLiteから復元する。

    ・再起動前からVCにいて、DBにactiveがある
      → 元の開始時刻を復元

    ・Bot停止中にVCへ入っていてactiveが無い
      → 正確な入室時刻が分からないため、
        Bot起動時刻から計測開始

    ・activeはあるが現在VCにいない
      → 正確な退出時刻が不明なので、
        過大計上を避けてactive記録を破棄
    """
    now = datetime.now(JST)

    currently_in_study_vc = set()

    for guild in bot.guilds:
        study_channel = find_channel(guild, "study_voice")
        for channel in guild.voice_channels:

            if (
                study_channel is None
                or channel.id != study_channel.id
            ):
                continue

            for member in channel.members:

                if member.bot:
                    continue

                key = (
                    guild.id,
                    member.id
                )

                currently_in_study_vc.add(
                    key
                )

                db_start = (
                    get_active_study_session(
                        guild.id,
                        member.id
                    )
                )

                if db_start is not None:
                    study_sessions[key] = (
                        db_start
                    )

                    print(
                        "♻️ 進行中セッション復元: "
                        f"{member.display_name} "
                        f"{db_start.strftime('%Y-%m-%d %H:%M:%S')}"
                    )

                else:
                    # Bot停止中に入室した可能性あり。
                    # 正確な時刻は分からないので起動時から。
                    study_sessions[key] = now

                    save_active_study_session(
                        guild.id,
                        member.id,
                        member.display_name,
                        now
                    )

                    print(
                        "🆕 起動時に勉強VC滞在を検出: "
                        f"{member.display_name}\n"
                        "正確な入室時刻が取得できないため、"
                        "Bot起動時刻から計測します。"
                    )

    # DBにはactiveがあるのに現在VCにいない
    # → Bot停止中に退出した可能性があり終了時刻不明。
    for (
        guild_id,
        user_id,
        username,
        start_time_str
    ) in get_all_active_study_sessions():

        key = (
            guild_id,
            user_id
        )

        if key not in currently_in_study_vc:
            delete_active_study_session(
                guild_id,
                user_id
            )

            study_sessions.pop(
                key,
                None
            )

            print(
                "⚠️ 復旧不能な進行中セッションを破棄: "
                f"{username}\n"
                f"開始記録: {start_time_str}\n"
                "Bot停止中に退出した可能性があり、"
                "正確な終了時刻を判断できないため"
                "過大計上を避けました。"
            )


# ============================================================
# 勉強部屋を出たときの通知のボタン（再起動後も有効）
# ============================================================

class VCActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # ボタンは今学習中の資格（ロードマップ）の入力画面を開く。
    # custom_id は再起動前の通知のボタンも動くよう、最初の版のまま
    @discord.ui.button(
        label="勉強を記録",
        style=discord.ButtonStyle.primary,
        custom_id="studybot:vc:sglog",
    )
    async def sglog_button(self, interaction, button):
        # 過去問道場・復習・単語帳から選んで記録する
        await open_study_log_prompt(
            interaction, current_qualification(config.DB_PATH)
        )

    @discord.ui.button(
        label="科目Bを記録",
        style=discord.ButtonStyle.secondary,
        custom_id="studybot:vc:sgb",
    )
    async def sgb_button(self, interaction, button):
        message, view = build_sgb_prompt(
            interaction.guild, interaction.user.id,
            current_qualification(config.DB_PATH),
        )
        await respond_private(interaction, message, view)

    @discord.ui.button(
        label="誤答を登録",
        style=discord.ButtonStyle.secondary,
        custom_id="studybot:vc:mistake",
    )
    async def mistake_button(self, interaction, button):
        message, view = build_mistake_prompt(
            interaction.user.id, current_qualification(config.DB_PATH)
        )
        await respond_private(interaction, message, view)

    @discord.ui.button(
        label="ひとこと",
        style=discord.ButtonStyle.secondary,
        custom_id="studybot:vc:note",
    )
    async def note_button(self, interaction, button):
        await open_note_modal(interaction)


# ============================================================
# VC入退室
# ============================================================

async def on_voice_state_update(
    member,
    before,
    after
):
    if member.bot:
        return

    key = (
        member.guild.id,
        member.id
    )

    # --------------------------------------------------------
    # 勉強VCへ入室
    # --------------------------------------------------------

    entered_study_vc = (
        is_channel(after.channel, "study_voice")
        and not is_channel(before.channel, "study_voice")
    )

    if entered_study_vc:
        # DBに進行中セッションがあれば
        # 二重開始を避けてそれを使う
        existing_start = (
            get_active_study_session(
                member.guild.id,
                member.id
            )
        )

        if existing_start is not None:
            start_time = existing_start
        else:
            start_time = datetime.now(JST)

            save_active_study_session(
                member.guild.id,
                member.id,
                member.display_name,
                start_time
            )

        study_sessions[key] = start_time

        print(
            f"📚 勉強開始: "
            f"{member.display_name} "
            f"{start_time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    # --------------------------------------------------------
    # 勉強VCから退出
    # --------------------------------------------------------

    left_study_vc = (
        is_channel(before.channel, "study_voice")
        and not is_channel(after.channel, "study_voice")
    )

    if left_study_vc:
        end_time = datetime.now(JST)

        start_time = study_sessions.pop(
            key,
            None
        )

        # メモリに無ければDBから復元
        if start_time is None:
            start_time = (
                get_active_study_session(
                    member.guild.id,
                    member.id
                )
            )

        if start_time is None:
            print(
                "⚠️ 開始時刻が記録されて"
                "いません。"
            )
            return

        total_seconds = int(
            (
                end_time
                - start_time
            ).total_seconds()
        )

        if total_seconds < 0:
            total_seconds = 0

        save_completed_study_session(
            guild_id=member.guild.id,
            user_id=member.id,
            username=member.display_name,
            start_time=start_time,
            end_time=end_time,
            duration_seconds=total_seconds
        )

        # 完了保存後にactiveを削除
        delete_active_study_session(
            member.guild.id,
            member.id
        )

        print(
            f"✅ 勉強終了: "
            f"{member.display_name}\n"
            f"開始: "
            f"{start_time.strftime('%H:%M:%S')}\n"
            f"終了: "
            f"{end_time.strftime('%H:%M:%S')}\n"
            f"勉強時間: "
            f"{format_duration(total_seconds)}\n"
            "💾 データベースへ保存しました"
        )

        # ----------------------------------------------------
        # Discordへ終了通知
        # ----------------------------------------------------

        notification_channel = find_channel(member.guild, "study_log")

        if notification_channel is None:
            print(
                "⚠️ 勉強終了通知を送信"
                "できませんでした。"
                "勉強ログのテキストチャンネルが見つかりません。"
                "/setup で設定してください。"
            )
            return

        await notification_channel.send(
            content=f"{member.mention} 勉強おつかれさま！",
            embed=build_study_end_embed(
                member.guild, member.id, member.display_name, total_seconds
            ),
            view=VCActionView(),
        )
        await announce_new_badges(member.guild, member.id)


def build_study_end_extras(user_id, today, today_seconds):
    """退出通知に足す欄：今日の目標と、集中タイマーのセット数。"""
    extras = []
    goal_text = format_goal_progress(
        today_seconds, goal_for_day(config.DB_PATH, user_id, today)
    )
    if goal_text:
        extras.append(("今日の目標", goal_text))
    sets, minutes = get_focus_sets(config.DB_PATH, user_id, today)
    if sets:
        extras.append(("集中タイマー", f"今日 {sets}セット（{minutes}分）"))
    status = checklist_status(config.DB_PATH, user_id, today)
    if status:
        extras.append((
            f"今日のメニュー {checklist_summary(status)}",
            format_checklist(status),
        ))
    return extras


def build_study_end_embed(guild, user_id, display_name, session_seconds):
    today = datetime.now(JST).date()
    today_seconds = get_today_total(user_id)
    return build_vc_summary_embed(
        display_name,
        session_seconds,
        today_seconds,
        get_week_total(user_id),
        today,
        get_study_streak_safe(user_id),
        get_exam_countdown_line(user_id),
        channel_label(guild, "study_log"),
        build_study_end_extras(user_id, today, today_seconds),
        compact=is_compact_display(config.DB_PATH, user_id),
    )


def register(bot):
    bot.add_listener(on_voice_state_update)
