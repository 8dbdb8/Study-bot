"""AIの返答から、Botの集計と食い違う行を取り除く。

AI（ローカルの小さなモデル）は、渡した数字を言い換えたり、
「60.0%で60%未満」のように矛盾した文を書いたりすることがある。
数字はBotが確定させたものだけを残す。
"""

import re


PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")


def _percents(text):
    return [float(value) for value in PERCENT.findall(text)]


def check_answer(answer, source_text, threshold=60.0):
    """(残した文章, 取り除いた行のリスト) を返す。

    - source_text（AIに渡した指示や事実）にないパーセントを含む行は取り除く
    - 「60%未満」と書きつつ、60%以上の数字を挙げている行は取り除く
    見出し（# で始まる行）と空行はそのまま残す。
    """
    allowed = {round(value, 1) for value in _percents(source_text)}
    threshold_phrase = f"{threshold:g}%未満"
    kept = []
    removed = []
    for line in answer.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue
        values = _percents(stripped)
        unknown = [v for v in values if round(v, 1) not in allowed]
        contradicts = threshold_phrase in stripped.replace("％", "%") and any(
            value >= threshold
            for value in _percents(stripped.replace(threshold_phrase, ""))
        )
        if unknown or contradicts:
            removed.append(stripped)
        else:
            kept.append(line)
    text = "\n".join(kept).strip()
    return text, removed


def checked_answer(answer, source_text, threshold=60.0):
    """チェック済みの文章。取り除いた行があれば、その旨を末尾に1行足す。"""
    text, removed = check_answer(answer, source_text, threshold)
    if removed:
        print(f"[ai_check] 集計と食い違う{len(removed)}行を除きました: {removed}")
        text += f"\n\n（記録と合わない内容を{len(removed)}行省きました）"
    return text
