"""成績・勉強時間のグラフをPNG画像にする。DBには触らない。

Discordのダークテーマで見やすいよう、暗い背景で描く。
pyplot は使わず Figure を直接作るので、別スレッドで描いても安全。
"""

from datetime import timedelta
from io import BytesIO

from matplotlib import font_manager
from matplotlib.dates import DayLocator, date2num, num2date
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter


# 色（ダーク背景用。青とオレンジは色覚の違いがあっても見分けやすい組み合わせ）
SURFACE = "#1a1a19"
TEXT_PRIMARY = "#ffffff"
TEXT_SECONDARY = "#c3c2b7"
GRID = "#3a3a37"
SERIES_1 = "#3987e5"
SERIES_2 = "#d95926"

FONT_CANDIDATES = (
    "Yu Gothic", "Meiryo", "BIZ UDGothic", "Noto Sans JP", "MS Gothic",
)


def _japanese_font():
    installed = {font.name for font in font_manager.fontManager.ttflist}
    for name in FONT_CANDIDATES:
        if name in installed:
            return name
    return None


FONT = _japanese_font()


def _new_figure(height=5.2):
    fig = Figure(figsize=(10, height), dpi=150, facecolor=SURFACE)
    return fig


def _style_axes(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=9, length=0, pad=6)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _apply_font(fig):
    if FONT is None:
        return
    for text in fig.findobj(match=lambda obj: hasattr(obj, "set_fontfamily")):
        text.set_fontfamily(FONT)


def _title(fig, title, subtitle):
    fig.text(0.06, 0.95, title, color=TEXT_PRIMARY, fontsize=15,
             fontweight="bold", va="top")
    fig.text(0.06, 0.885, subtitle, color=TEXT_SECONDARY, fontsize=10,
             va="top")


def _to_png(fig):
    _apply_font(fig)
    buffer = BytesIO()
    fig.savefig(buffer, format="png", facecolor=SURFACE)
    return buffer.getvalue()


def _date_axis(ax, start, end):
    # 記録が数日分しかなくても棒が太くなりすぎないよう、最低1週間の幅で描く
    if (end - start).days < 6:
        end = start + timedelta(days=6)
    span = (end - start).days + 1
    interval = 1 if span <= 14 else 7 if span <= 60 else 14
    ax.xaxis.set_major_locator(DayLocator(interval=interval))
    ax.xaxis.set_major_formatter(FuncFormatter(
        lambda value, _: f"{num2date(value).month}/{num2date(value).day}"
    ))
    # date 型は端数の日を切り捨てるので、数値に直してから余白を付ける
    ax.set_xlim(date2num(start) - 0.7, date2num(end) + 0.7)


def render_study_time_chart(daily_seconds, start, end):
    """日ごとの勉強時間（分）の棒と、7日平均の線。

    daily_seconds: {date: 秒}。記録のない日は0分として描く。
    """
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    minutes = [daily_seconds.get(day, 0) / 60 for day in days]

    averages = []
    for index in range(len(days)):
        window = minutes[max(0, index - 6): index + 1]
        averages.append(sum(window) / len(window))

    total = sum(minutes)
    studied_days = sum(1 for value in minutes if value > 0)

    fig = _new_figure()
    ax = fig.add_axes([0.06, 0.1, 0.88, 0.68])
    _style_axes(ax)
    ax.bar(days, minutes, width=0.7, color=SERIES_1, label="勉強時間")
    ax.plot(days, averages, color=SERIES_2, linewidth=2, label="7日平均")
    ax.set_ylim(0, max(max(minutes, default=0), 30) * 1.15)
    ax.set_ylabel("分", color=TEXT_SECONDARY, fontsize=9, rotation=0,
                  labelpad=12)
    _date_axis(ax, start, end)

    handles, labels = ax.get_legend_handles_labels()
    legend = ax.legend(
        handles[::-1], labels[::-1], loc="lower left", frameon=False,
        ncol=2, fontsize=9, bbox_to_anchor=(-0.01, 1.0), borderaxespad=0.2,
    )
    for text in legend.get_texts():
        text.set_color(TEXT_SECONDARY)

    if averages:
        ax.annotate(
            f"{averages[-1]:.0f}分", xy=(days[-1], averages[-1]),
            xytext=(6, 0), textcoords="offset points", va="center",
            color=TEXT_PRIMARY, fontsize=9,
        )

    hours, mins = divmod(round(total), 60)
    _title(
        fig,
        f"勉強時間（{start.month}/{start.day}〜{end.month}/{end.day}）",
        f"合計 {hours}時間{mins:02d}分 ・ 勉強した日 {studied_days}日 / "
        f"{len(days)}日 ・ 1日平均 {total / len(days):.0f}分",
    )
    return _to_png(fig)


def render_score_chart(points, title, threshold=60.0):
    """上段に正答率の推移、下段に日ごとの問題数。

    points: [(date, 問題数, 正答率 or None), ...]
    2つの量は単位が違うので、軸を共有せず上下に分けて描く。
    """
    scored = [(day, score) for day, _, score in points if score is not None]
    days = [day for day, _, _ in points]
    start, end = min(days), max(days)

    fig = _new_figure(height=6.4)
    ax_score = fig.add_axes([0.06, 0.40, 0.88, 0.39])
    ax_count = fig.add_axes([0.06, 0.08, 0.88, 0.22], sharex=ax_score)
    for ax in (ax_score, ax_count):
        _style_axes(ax)

    ax_score.axhline(threshold, color=TEXT_SECONDARY, linewidth=1,
                     linestyle=(0, (4, 4)))
    ax_score.text(
        1.0, threshold, f"{threshold:.0f}%",
        transform=ax_score.get_yaxis_transform(),
        color=TEXT_SECONDARY, fontsize=8, va="bottom", ha="right",
    )
    if scored:
        ax_score.plot(
            [day for day, _ in scored], [score for _, score in scored],
            color=SERIES_1, linewidth=2, marker="o", markersize=6,
            markeredgecolor=SURFACE, markeredgewidth=1.5,
        )
        last_day, last_score = scored[-1]
        ax_score.annotate(
            f"{last_score:.0f}%", xy=(last_day, last_score),
            xytext=(0, 9), textcoords="offset points", ha="center",
            color=TEXT_PRIMARY, fontsize=9, fontweight="bold",
        )
    ax_score.set_ylim(0, 105)
    ax_score.set_yticks([0, 20, 40, 60, 80, 100])
    ax_score.set_yticklabels(["0", "20", "40", "60", "80", "100%"])
    ax_score.text(0, 1.04, "正答率", transform=ax_score.transAxes,
                  color=TEXT_SECONDARY, fontsize=9)
    ax_score.tick_params(labelbottom=False)

    ax_count.bar(days, [questions for _, questions, _ in points],
                 width=0.7, color=SERIES_1)
    ax_count.text(0, 1.08, "問題数", transform=ax_count.transAxes,
                  color=TEXT_SECONDARY, fontsize=9)
    _date_axis(ax_count, start, end)

    total_questions = sum(questions for _, questions, _ in points)
    _title(
        fig,
        title,
        f"{start.month}/{start.day}〜{end.month}/{end.day} ・ "
        f"{len(points)}日分 ・ 合計 {total_questions}問 ・ "
        f"点線は合格目安の{threshold:.0f}%",
    )
    return _to_png(fig)


def render_mock_chart(mocks, title, pass_score=None):
    """模試の得点の推移（棒）と合格ライン（点線）。mocks は古い順。"""
    labels = [mock["label"] for mock in mocks]
    scores = [mock["score"] for mock in mocks]

    fig = _new_figure(height=4.8)
    ax = fig.add_axes([0.08, 0.12, 0.86, 0.66])
    _style_axes(ax)
    positions = list(range(len(scores)))
    ax.bar(positions, scores, width=0.6, color=SERIES_1)
    for x, score in zip(positions, scores):
        ax.annotate(
            f"{score}", xy=(x, score), xytext=(0, 4),
            textcoords="offset points", ha="center",
            color=TEXT_PRIMARY, fontsize=9, fontweight="bold",
        )
    if pass_score is not None:
        ax.axhline(pass_score, color=TEXT_SECONDARY, linewidth=1,
                   linestyle=(0, (4, 4)))
        ax.text(1.0, pass_score, f"合格 {pass_score}",
                transform=ax.get_yaxis_transform(),
                color=TEXT_SECONDARY, fontsize=8, va="bottom", ha="right")
    ax.set_ylim(0, 1000)
    ax.set_xlim(-0.6, max(len(scores), 5) - 0.4)
    ax.set_xticks(positions)
    ax.set_xticklabels(labels)
    ax.set_ylabel("点", color=TEXT_SECONDARY, fontsize=9, rotation=0,
                  labelpad=12)

    best = max(scores) if scores else 0
    _title(fig, title, f"{len(scores)}回 ・ 最高 {best}点 ・ 点線は合格ライン")
    return _to_png(fig)


# 学習カレンダーの色（勉強時間が長いほど明るい青）
CALENDAR_EMPTY = "#2c2c2a"
CALENDAR_LEVELS = (
    (30, "#184f95"),
    (60, "#256abf"),
    (120, "#3987e5"),
    (None, "#86b6ef"),
)


def _calendar_color(minutes):
    if minutes <= 0:
        return CALENDAR_EMPTY
    for limit, color in CALENDAR_LEVELS:
        if limit is None or minutes < limit:
            return color
    return CALENDAR_LEVELS[-1][1]


def render_calendar(daily_seconds, rest_days, end, weeks=13):
    """GitHub の草のような学習カレンダー。列が週（月曜始まり）、行が曜日。"""
    first_monday = end - timedelta(days=end.weekday() + 7 * (weeks - 1))
    # マスを正方形にするため、週の数に合わせて画像の横幅を決める
    unit = 0.42
    grid_w = (weeks + 0.5) * unit
    grid_h = 8.4 * unit
    fig_w = max(grid_w + 1.0, 8.0)
    fig_h = grid_h + 1.45
    fig = Figure(figsize=(fig_w, fig_h), dpi=150, facecolor=SURFACE)
    ax = fig.add_axes([0.7 / fig_w, 0.55 / fig_h, grid_w / fig_w, grid_h / fig_h])
    ax.set_facecolor(SURFACE)
    ax.set_xlim(-0.5, weeks)
    ax.set_ylim(7.2, -1.2)
    ax.axis("off")

    total_minutes = 0
    studied_days = 0
    previous_month = None
    for week in range(weeks):
        for weekday in range(7):
            day = first_monday + timedelta(days=week * 7 + weekday)
            if day > end:
                continue
            minutes = daily_seconds.get(day, 0) / 60
            total_minutes += minutes
            studied_days += minutes > 0
            ax.add_patch(Rectangle(
                (week + 0.06, weekday + 0.06), 0.88, 0.88,
                facecolor=_calendar_color(minutes), linewidth=0,
            ))
            if day in rest_days and minutes <= 0:
                ax.add_patch(Rectangle(
                    (week + 0.12, weekday + 0.12), 0.76, 0.76,
                    facecolor="none", edgecolor=SERIES_2, linewidth=1.5,
                ))
            if day == end:
                ax.add_patch(Rectangle(
                    (week + 0.03, weekday + 0.03), 0.94, 0.94,
                    facecolor="none", edgecolor=TEXT_PRIMARY, linewidth=1.2,
                ))
            if weekday == 0 and day.month != previous_month:
                ax.text(week + 0.06, -0.35, f"{day.month}月",
                        color=TEXT_SECONDARY, fontsize=8, va="bottom")
                previous_month = day.month

    for weekday, label in ((0, "月"), (2, "水"), (4, "金"), (6, "日")):
        ax.text(-0.15, weekday + 0.5, label, color=TEXT_SECONDARY,
                fontsize=8, ha="right", va="center")

    # 凡例（インチで位置を決めて、画像の幅が変わっても崩れないようにする）
    legend_y = 0.28 / fig_h
    box_w, box_h = 0.16 / fig_w, 0.16 / fig_h
    x = 0.7 / fig_w
    fig.text(x, legend_y, "少", color=TEXT_SECONDARY, fontsize=8, va="center")
    x += 0.2 / fig_w
    for color in [CALENDAR_EMPTY] + [color for _, color in CALENDAR_LEVELS]:
        fig.patches.append(Rectangle(
            (x, legend_y - box_h / 2), box_w, box_h,
            transform=fig.transFigure, facecolor=color, figure=fig,
        ))
        x += 0.2 / fig_w
    fig.text(x, legend_y, "多", color=TEXT_SECONDARY, fontsize=8, va="center")
    x += 0.5 / fig_w
    fig.patches.append(Rectangle(
        (x, legend_y - box_h / 2), box_w, box_h, transform=fig.transFigure,
        facecolor=CALENDAR_EMPTY, edgecolor=SERIES_2, linewidth=1.5, figure=fig,
    ))
    fig.text(x + 0.22 / fig_w, legend_y, "休みの日", color=TEXT_SECONDARY,
             fontsize=8, va="center")

    hours, mins = divmod(round(total_minutes), 60)
    _title(
        fig,
        f"学習カレンダー（{first_monday.month}/{first_monday.day}〜{end.month}/{end.day}）",
        f"勉強した日 {studied_days}日 ・ 合計 {hours}時間{mins:02d}分 ・ "
        "色が明るいほど長く勉強した日",
    )
    return _to_png(fig)


def render_time_of_day_chart(stats, title, subtitle, threshold=60.0):
    """時間帯ごとの勉強時間（左）と正答率（右）。stats は time_of_day_stats の戻り値。"""
    names = list(stats)
    hours = [stats[name]["minutes"] / 60 for name in names]
    scores = [stats[name]["score"] for name in names]
    positions = list(range(len(names)))

    fig = _new_figure(height=4.8)
    left = fig.add_axes([0.07, 0.12, 0.40, 0.62])
    right = fig.add_axes([0.56, 0.12, 0.40, 0.62])
    for ax in (left, right):
        _style_axes(ax)
        ax.set_xticks(positions)
        ax.set_xticklabels(names)
        ax.set_xlim(-0.6, len(names) - 0.4)

    left.bar(positions, hours, width=0.6, color=SERIES_1)
    left.set_ylim(0, max(max(hours, default=0), 1) * 1.2)
    left.set_title("勉強時間（時間）", color=TEXT_SECONDARY, fontsize=10, loc="left")
    for x, value in zip(positions, hours):
        if value:
            left.annotate(f"{value:.1f}", xy=(x, value), xytext=(0, 4),
                          textcoords="offset points", ha="center",
                          color=TEXT_PRIMARY, fontsize=9)

    right.bar(positions, [score or 0 for score in scores], width=0.6, color=SERIES_2)
    right.axhline(threshold, color=TEXT_SECONDARY, linewidth=1, linestyle=(0, (4, 4)))
    right.set_ylim(0, 100)
    right.set_title("正答率（%）", color=TEXT_SECONDARY, fontsize=10, loc="left")
    for x, score in zip(positions, scores):
        label = f"{score:.0f}%" if score is not None else "記録少"
        right.annotate(label, xy=(x, score or 0), xytext=(0, 4),
                       textcoords="offset points", ha="center",
                       color=TEXT_PRIMARY if score is not None else TEXT_SECONDARY,
                       fontsize=9)

    _title(fig, title, subtitle)
    return _to_png(fig)
