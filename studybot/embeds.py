"""Discordに送るEmbedを組み立てる関数。DBには触らない。"""

from datetime import date, timedelta

import discord

from studybot.exam_schedule import WEEKDAY_LABELS
from studybot.qualifications import SG


COLOR_DEFAULT = 0x4752C4
COLOR_ALERT = 0xC2410C
COLOR_SUCCESS = 0x248046

# Embedのフィールド本文の上限
FIELD_LIMIT = 1024


def text_bar(ratio, width=10):
    ratio = max(0.0, min(1.0, ratio or 0.0))
    filled = round(ratio * width)
    return "▰" * filled + "▱" * (width - filled)


def format_minutes(total_seconds):
    minutes = max(0, int(total_seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    return f"{hours}時間{minutes:02d}分" if hours else f"{minutes}分"


def short_date(value):
    return f"{value.month}/{value.day}"


def safe_text(value, limit):
    """利用者が入力した文字列を、メンションと装飾を無効化して切り詰める。"""
    text = discord.utils.escape_mentions(str(value or ""))
    text = discord.utils.escape_markdown(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clip(text, limit=FIELD_LIMIT):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def week_lines(rows, today):
    """月曜〜今日の勉強時間を曜日ごとのバーで表す。"""
    by_day = {}
    for study_date, seconds in rows:
        by_day[date.fromisoformat(study_date)] = seconds
    monday = today - timedelta(days=today.weekday())
    longest = max(by_day.values(), default=0)

    lines = []
    for offset in range(today.weekday() + 1):
        day = monday + timedelta(days=offset)
        seconds = by_day.get(day, 0)
        bar = text_bar(seconds / longest if longest else 0, 8)
        lines.append(
            f"`{WEEKDAY_LABELS[offset]}` `{bar}` "
            + (format_minutes(seconds) if seconds else "—")
        )
    return "\n".join(lines)


# ------------------------------------------------------------
# 勉強時間
# ------------------------------------------------------------

def build_today_embed(total_seconds, streak, countdown=None, goal_text=None):
    embed = discord.Embed(
        title="今日の勉強時間",
        description=f"## {format_minutes(total_seconds)}",
        color=COLOR_DEFAULT,
    )
    embed.add_field(name="連続学習", value=f"🔥 {streak}日", inline=True)
    if goal_text:
        embed.add_field(name="今日の目標", value=goal_text, inline=False)
    if countdown:
        embed.add_field(name="試験", value=countdown, inline=False)
    return embed


def build_week_embed(rows, today, streak):
    total = sum(seconds for _, seconds in rows)
    embed = discord.Embed(
        title="今週の勉強時間",
        description=f"合計 **{format_minutes(total)}**",
        color=COLOR_DEFAULT,
    )
    embed.add_field(
        name="曜日別", value=week_lines(rows, today), inline=False
    )
    embed.add_field(name="連続学習", value=f"🔥 {streak}日", inline=True)
    return embed


def build_vc_summary_embed(
    display_name, session_seconds, today_seconds, week_rows, today,
    streak, countdown=None, log_channel_label="#勉強ログ", extra_fields=(),
):
    week_seconds = sum(seconds for _, seconds in week_rows)
    embed = discord.Embed(
        title=f"{safe_text(display_name, 64)}さん、勉強おつかれさま",
        description=(
            "やった内容は下のボタンから記録できます。"
            f"{log_channel_label} に書いても記録されます。"
        ),
        color=COLOR_DEFAULT,
    )
    embed.add_field(name="今回", value=format_minutes(session_seconds))
    embed.add_field(name="今日", value=format_minutes(today_seconds))
    embed.add_field(name="今週", value=format_minutes(week_seconds))
    embed.add_field(
        name=f"今週の推移 ・ 🔥 連続 {streak}日",
        value=week_lines(week_rows, today) or "—",
        inline=False,
    )
    for name, value in extra_fields:
        embed.add_field(name=name, value=value, inline=False)
    if countdown:
        embed.add_field(name="試験", value=countdown, inline=False)
    return embed


# ------------------------------------------------------------
# SG進捗
# ------------------------------------------------------------

def _progress_line(item, threshold):
    category = item["category"]
    questions = item["questions"]
    score = item["latest_score"]
    last_date = item["last_study_date"]
    score_text = f"{score:.1f}%" if score is not None else "—"

    if questions == 0:
        if last_date is None:
            return f"⚪ **{category}**　未着手"
        return (
            f"⚪ **{category}**　問題数未記録\n"
            f"`{text_bar((score or 0) / 100)}` 直近{score_text} ・{last_date}"
        )

    if questions < 10:
        mark, state = "⚪", "記録少"
    elif score is not None and score < threshold:
        mark, state = "🟠", "要復習"
    else:
        mark, state = "🔵", "学習中"
    return (
        f"{mark} **{category}**　{state}\n"
        f"`{text_bar((score or 0) / 100)}` 直近{score_text} ・"
        f"{questions}問 ・{last_date or '—'}"
    )


def build_progress_embed(items, unclassified, b_summary, threshold=60.0,
                         qualification=None, overall_score=None):
    """分野ごとの進捗。見出しのまとめ方は資格の progress_groups に従う。"""
    qualification = qualification or SG
    total = sum(item["questions"] for item in items) + unclassified
    score_text = (
        f" ・ 正答率 **{overall_score:.1f}%**"
        if overall_score is not None else ""
    )
    embed = discord.Embed(
        title=qualification.section_a_title,
        description=f"累計 **{total}問**{score_text}（バーは直近の正答率）",
        color=COLOR_DEFAULT,
    )
    by_category = {item["category"]: item for item in items}
    for group, categories in qualification.progress_groups:
        lines = [
            _progress_line(by_category[category], threshold)
            for category in categories
            if category in by_category
        ]
        if lines:
            embed.add_field(
                name=group, value=_clip("\n".join(lines)), inline=False
            )
    if unclassified:
        embed.add_field(
            name="分野未特定",
            value=f"旧ログなど：{unclassified}問",
            inline=False,
        )

    if b_summary["questions"]:
        lines = [
            f"{b_summary['questions']}問中"
            f"{b_summary['correct_answers']}問正解"
            f"（{b_summary['score_percent']:.1f}%）"
        ]
        for topic, questions, correct, reason, practiced_on in (
            b_summary["recent_sessions"]
        ):
            line = f"{practiced_on} {topic}：{correct}/{questions}問"
            if reason:
                line += f"\n　判断ミス：{safe_text(reason, 80)}"
            lines.append(line)
        b_text = "\n".join(lines)
    else:
        b_text = "まだ記録なし"
    if qualification.has_part_b:
        embed.add_field(name="科目B", value=_clip(b_text), inline=False)
    embed.set_footer(
        text=(
            f"🟠 要復習＝10問以上で直近{threshold:.0f}%未満　"
            "⚪ 記録少＝10問未満"
        )
    )
    return embed


# ------------------------------------------------------------
# 週次計画
# ------------------------------------------------------------

def build_plan_status_embed(status_data, countdown=None):
    embed = discord.Embed(
        title=f"{status_data.get('qualification', 'SG')} 週次計画の達成状況",
        description=(
            f"開始 {status_data['start_on'].isoformat()} ・"
            f"{status_data['weeks']}週間 ・"
            f"基本目標 {status_data['weekly_questions']}問/週"
        ),
        color=COLOR_DEFAULT,
    )
    if countdown:
        embed.add_field(name="試験", value=countdown, inline=False)

    lines = []
    for week in status_data["rows"]:
        current = week["week"] == status_data["current_week"]
        ratio = week["actual"] / week["target"] if week["target"] else 1
        lines.append(
            f"{'▶' if current else '✓' if week['actual'] >= week['target'] else '・'} "
            f"第{week['week']}週 {short_date(week['start_on'])}〜"
            f"{short_date(week['end_on'])}　`{text_bar(ratio, 8)}` "
            f"{week['actual']}/{week['target']}問"
        )
    embed.add_field(
        name="週ごとの実績",
        value=_clip("\n".join(lines) or "まだ集計できる週がありません"),
        inline=False,
    )

    if status_data["completed"]:
        embed.add_field(name="状態", value="計画期間は終了しました。", inline=False)
    elif status_data["rows"]:
        current = status_data["rows"][-1]
        embed.add_field(
            name="今週の残り",
            value=f"**{current['remaining']}問**",
            inline=False,
        )
    embed.set_footer(text="週の未達分は翌週に最大で基本目標の50%まで繰り越します。")
    return embed


# ------------------------------------------------------------
# 復習
# ------------------------------------------------------------

def mistake_label(item, current_code="SG"):
    """誤答の分野名。今の資格と違う資格の誤答には資格名を付ける（例：SG・ネットワーク）。"""
    qualification = item.get("qualification") or current_code
    if qualification == current_code:
        return item["category"]
    return f"{qualification}・{item['category']}"


def _review_round(item):
    return f"連続正解 {item['success_streak']}/3"


def build_review_list_embed(items, all_items=False, current_code="SG"):
    embed = discord.Embed(
        title="SG 復習リスト" + ("（すべて）" if all_items else ""),
        description=f"対象 **{len(items)}件**",
        color=COLOR_DEFAULT,
    )
    for item in items[:10]:
        value = (
            f"{safe_text(item['question_ref'], 200)}\n"
            f"理由：{safe_text(item['reason'], 80)}"
        )
        if item["memo"]:
            value += f"\nメモ：{safe_text(item['memo'], 60)}"
        embed.add_field(
            name=(
                f"#{item['id']} {mistake_label(item, current_code)}"
                f"（{item['next_review_on']} ・{_review_round(item)}）"
            ),
            value=_clip(value),
            inline=False,
        )
    if len(items) > 10:
        embed.add_field(
            name="…", value=f"ほか{len(items) - 10}件", inline=False
        )
    embed.set_footer(
        text="/review start で1問ずつ解き直し ・ /review answer で結果だけ登録"
    )
    return embed


def build_review_card_embed(item, position, total, current_code="SG"):
    description = f"{safe_text(item['question_ref'], 200)}\n\n"
    description += f"**前回の誤答理由**\n{safe_text(item['reason'], 300)}"
    if item["memo"]:
        description += f"\n\n**メモ**\n{safe_text(item['memo'], 300)}"
    embed = discord.Embed(
        title=(
            f"復習 {position}/{total} ・ #{item['id']} "
            f"{mistake_label(item, current_code)}"
        ),
        description=description,
        color=COLOR_DEFAULT,
    )
    embed.set_footer(text=f"{_review_round(item)} ・ 3回連続で正解すると完了")
    return embed


def build_review_summary_embed(results):
    answered = results["correct"] + results["wrong"]
    lines = [
        f"正解 **{results['correct']}問** ・ 不正解 **{results['wrong']}問**",
    ]
    if results["completed"]:
        lines.append(f"🎉 {results['completed']}問が3回連続正解で完了しました。")
    if results["skipped"]:
        lines.append(f"あとで：{results['skipped']}問（今日のリストに残ります）")
    return discord.Embed(
        title="今日の復習おわり" if answered else "復習を中断しました",
        description="\n".join(lines),
        color=COLOR_SUCCESS if answered else COLOR_DEFAULT,
    )


# ------------------------------------------------------------
# 学習メニュー（平日は夜・土日祝は朝）
# ------------------------------------------------------------

def _final_stretch_fields(embed, final_stretch):
    days_left = final_stretch["days_left"]
    focus_lines = [
        f"{name}　**{amount}**" for name, amount in final_stretch["focus"]
    ]
    embed.add_field(
        name="今日の重点（弱い分野から）" if days_left else "直前の確認",
        value=_clip("\n".join(focus_lines)),
        inline=False,
    )
    unfinished = final_stretch["unfinished"]
    if days_left >= 2:
        if unfinished:
            per_day = -(-unfinished // (days_left - 1))
            value = (
                f"未完了の誤答 **{unfinished}件** を試験前日までに一巡"
                f"（1日あたり約{per_day}件）"
            )
        else:
            value = "未完了の誤答はありません。総合演習で仕上げましょう。"
        embed.add_field(name="仕上げチェック", value=value, inline=False)
    elif days_left == 1:
        embed.add_field(
            name="明日は試験です",
            value=(
                "新しい問題より、誤答の見直しと早めの睡眠を優先しましょう。"
                + (f"（未完了の誤答 {unfinished}件）" if unfinished else "")
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="試験が終わったら",
            value="夜20時に結果をたずねるメッセージが届きます。",
            inline=False,
        )


def build_digest_embed(
    today, countdown, streak, due_items, plan_week=None, weak=(),
    threshold=60.0, final_stretch=None, current_code="SG",
    extra_lines=(),
):
    """今日伝えることがなければNoneを返す。

    final_stretch は試験直前（14日前〜当日）のときだけ渡す辞書：
    days_left（残り日数）, label（例：SG試験）, focus（[(項目, 量)]）,
    unfinished（未完了の誤答の数）。
    """
    if not (countdown or due_items or plan_week or weak or final_stretch):
        return None

    exam_day = final_stretch is not None and final_stretch["days_left"] == 0
    date_text = (
        f"{today.month}月{today.day}日"
        f"（{WEEKDAY_LABELS[today.weekday()]}）"
    )
    if exam_day:
        title = f"{date_text} 今日は{final_stretch['label']}です"
        description = (
            "がんばってください！受験票と本人確認書類を忘れずに。\n"
            f"🔥 連続学習 {streak}日"
        )
    else:
        title = f"{date_text}の学習メニュー"
        if final_stretch is not None:
            title += " ・ 直前モード"
        description = "\n".join(
            line for line in (countdown, f"🔥 連続学習 {streak}日", *extra_lines)
            if line
        )

    embed = discord.Embed(
        title=title,
        description=description,
        color=COLOR_ALERT if (due_items or final_stretch) else COLOR_DEFAULT,
    )

    if final_stretch is not None:
        _final_stretch_fields(embed, final_stretch)
        if exam_day:
            embed.set_footer(text="通知は /plan notify でオフにできます")
            return embed

    if due_items:
        lines = [
            f"`#{item['id']}` **{mistake_label(item, current_code)}** — "
            f"{safe_text(item['question_ref'], 60)}（{_review_round(item)}）"
            for item in due_items[:5]
        ]
        if len(due_items) > 5:
            lines.append(f"ほか{len(due_items) - 5}件")
        embed.add_field(
            name=f"今日の復習 {len(due_items)}件",
            value=_clip("\n".join(lines)),
            inline=False,
        )
    else:
        embed.add_field(
            name="今日の復習", value="なし（予定どおり進んでいます）", inline=False
        )

    if plan_week:
        days_left = (plan_week["end_on"] - today).days + 1
        value = (
            f"`{text_bar(plan_week['actual'] / plan_week['target'] if plan_week['target'] else 1)}` "
            f"{plan_week['actual']}/{plan_week['target']}問"
        )
        if plan_week["remaining"]:
            per_day = -(-plan_week["remaining"] // max(days_left, 1))
            value += (
                f"\n残り{days_left}日で{plan_week['remaining']}問"
                f" → 1日あたり **{per_day}問**"
            )
        else:
            value += "\n今週の目標は達成済みです。"
        embed.add_field(
            name=f"今週の目標（第{plan_week['week']}週）",
            value=value,
            inline=False,
        )

    # 直前モードでは弱い分野を「今日の重点」に出しているので重ねない
    if weak and final_stretch is None:
        embed.add_field(
            name=f"正答率{threshold:.0f}%未満の分野",
            value=" ・ ".join(f"{category} {score:.0f}%" for category, score in weak),
            inline=False,
        )
    embed.set_footer(text="通知は /plan notify でオフにできます")
    return embed
