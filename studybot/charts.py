"""成績・勉強時間のグラフをPNG画像にする。DBには触らない。

Discordのダークテーマで見やすいよう、暗い背景で描く。
pyplot は使わず Figure を直接作るので、別スレッドで描いても安全。
"""

from datetime import timedelta
from io import BytesIO

from matplotlib import font_manager
from matplotlib.dates import DayLocator, date2num, num2date
from matplotlib.figure import Figure
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
