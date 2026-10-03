"""表示用の小さな整形関数。"""

from datetime import datetime

from studybot.config import JST, REVIEW_SCORE_THRESHOLD


# ============================================================
# 共通
# ============================================================

def format_duration(total_seconds):
    total_seconds = max(0, int(total_seconds))

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    return f"{hours}時間{minutes}分{seconds}秒"


def parse_iso_datetime(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)

        return dt.astimezone(JST)

    except ValueError:
        return None


def format_category_label(item):
    major = item.get("major_category")
    category = item.get("category")

    if category:
        return f"{major} > {category}"

    return major or "分野不明"


def get_review_candidates(items, score_key):
    candidates = [
        item
        for item in items
        if item.get(score_key) is not None
        and item[score_key] < REVIEW_SCORE_THRESHOLD
    ]

    return sorted(
        candidates,
        key=lambda item: item[score_key]
    )
