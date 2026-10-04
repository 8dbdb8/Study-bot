import math
from collections import namedtuple
import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from studybot.qualifications import SG, get_qualification


SUPPORTED_QUALIFICATIONS = (
    "SG",
    "FE",
    "医療情報技師",
    "不明",
)

SG_MAJOR_CATEGORIES = (
    "テクノロジ系",
    "マネジメント系",
    "ストラテジ系",
)

SG_PRACTICE_CATEGORIES = SG.category_names

SG_CATEGORY_TO_MAJOR = {
    "セキュリティ": "テクノロジ系",
    "情報セキュリティ": "テクノロジ系",
    "情報セキュリティ管理": "テクノロジ系",
    "セキュリティ技術評価": "テクノロジ系",
    "情報セキュリティ対策": "テクノロジ系",
    "セキュリティ実装技術": "テクノロジ系",
    "システム構成要素": "テクノロジ系",
    "データベース": "テクノロジ系",
    "ネットワーク": "テクノロジ系",
    "プロジェクトマネジメント": "マネジメント系",
    "サービスマネジメント": "マネジメント系",
    "システム監査": "マネジメント系",
    "法務": "ストラテジ系",
    "システム戦略": "ストラテジ系",
    "システム企画": "ストラテジ系",
    "企業活動": "ストラテジ系",
}

_QUALIFICATION_ALIASES = {
    "SG": "SG",
    "情報セキュリティマネジメント": "SG",
    "FE": "FE",
    "基本情報技術者": "FE",
    "基本情報技術者試験": "FE",
    "医療情報技師": "医療情報技師",
    "不明": "不明",
}

_MAJOR_ALIASES = {
    "テクノロジ": "テクノロジ系",
    "テクノロジ系": "テクノロジ系",
    "マネジメント": "マネジメント系",
    "マネジメント系": "マネジメント系",
    "ストラテジ": "ストラテジ系",
    "ストラテジ系": "ストラテジ系",
}

_CATEGORY_ALIASES = {
    "情報セキュリティ全般": "情報セキュリティ",
    "セキュリティ管理": "情報セキュリティ管理",
    "セキュリティ対策": "情報セキュリティ対策",
    "セキュリティ実装": "セキュリティ実装技術",
    "法律": "法務",
}


# 分野の正規化に使う対応表（資格ごと）
CategoryMaps = namedtuple(
    "CategoryMaps", "label category_to_major majors aliases"
)

# SG は以前からの対応表（順番も含めて）をそのまま使う
SG_MAPS = CategoryMaps(
    "SG", SG_CATEGORY_TO_MAJOR, SG_MAJOR_CATEGORIES, _CATEGORY_ALIASES
)


def category_maps_for(qualification_code):
    """資格の分野対応表。SG・不明・知らない資格は SG の表を使う。"""
    qualification = get_qualification(qualification_code)
    if qualification is None or qualification.code == "SG":
        return SG_MAPS
    return CategoryMaps(
        qualification.code,
        qualification.category_to_major,
        qualification.majors,
        qualification.category_aliases,
    )


def parse_question_count_input(value):
    text = _normalize_text(value)

    if not re.fullmatch(r"\d+", text):
        raise ValueError(
            "問題数は1以上の整数で入力してください。"
        )

    questions = int(text)

    if not 1 <= questions <= 1000:
        raise ValueError(
            "問題数は1〜1000問で入力してください。"
        )

    return questions


def parse_score_percent_input(value):
    text = _normalize_text(value).removesuffix("%").strip()

    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        raise ValueError(
            "正答率は0〜100の数字で入力してください。"
        )

    try:
        score = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(
            "正答率を読み取れませんでした。"
        ) from error

    if not Decimal("0") <= score <= Decimal("100"):
        raise ValueError(
            "正答率は0〜100で入力してください。"
        )

    return float(
        score.quantize(
            Decimal("0.1"),
            rounding=ROUND_HALF_UP,
        )
    )


def infer_correct_answers(questions, score_percent):
    question_decimal = Decimal(questions)
    score_decimal = Decimal(str(score_percent))
    estimated = int(
        (
            question_decimal
            * score_decimal
            / Decimal("100")
        ).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )
    reconstructed = (
        Decimal(estimated)
        / question_decimal
        * Decimal("100")
    ).quantize(
        Decimal("0.1"),
        rounding=ROUND_HALF_UP,
    )

    if reconstructed == score_decimal:
        return estimated

    return None


def _normalize_text(value):
    if value is None:
        return ""

    return unicodedata.normalize(
        "NFKC",
        str(value),
    ).strip()


def _as_int(value):
    if value is None or isinstance(value, bool):
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(number) or not number.is_integer():
        return None

    return int(number)


def _as_float(value):
    if value is None or isinstance(value, bool):
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(number):
        return None

    return number


def _text_or_none(value):
    text = _normalize_text(value)
    return text or None


def _extract_question_and_correct_counts(content):
    patterns = (
        r"(?P<questions>\d+)\s*問\s*中\s*"
        r"(?P<correct>\d+)\s*問?\s*(?:正解|正答)",
        r"(?P<correct>\d+)\s*/\s*(?P<questions>\d+)\s*"
        r"(?:問)?\s*(?:正解|正答)",
    )

    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            return (
                int(match.group("questions")),
                int(match.group("correct")),
            )

    correct_patterns = (
        r"(?:正解|正答)(?:数)?\s*(?:は|[:：])?\s*"
        r"(?P<correct>\d+)\s*問",
        r"(?P<correct>\d+)\s*問\s*(?:正解|正答)",
    )

    correct_answers = None
    for pattern in correct_patterns:
        match = re.search(pattern, content)
        if match:
            correct_answers = int(match.group("correct"))
            break

    question_patterns = (
        r"(?:過去問道場(?:を)?|過去問(?:を)?|演習(?:を)?)\s*"
        r"(?P<questions>\d+)\s*問",
        r"(?P<questions>\d+)\s*問(?!\s*(?:正解|正答))",
    )

    questions = None
    for pattern in question_patterns:
        match = re.search(pattern, content)
        if match:
            questions = int(match.group("questions"))
            break

    return questions, correct_answers


def _extract_score_percent(content):
    match = re.search(
        r"(?:正答率|正解率)\s*(?:は|[:：])?\s*"
        r"(?P<score>\d+(?:\.\d+)?)\s*%",
        content,
    )

    if not match:
        return None, None

    score_text = match.group("score")
    decimal_places = (
        len(score_text.split(".", 1)[1])
        if "." in score_text
        else 0
    )

    return float(score_text), decimal_places


def _score_tolerance(decimal_places):
    if decimal_places is None or decimal_places == 0:
        return 0.75

    return 0.075 * (10 ** -(decimal_places - 1))


def _resolve_score(
    questions,
    correct_answers,
    score_percent,
    decimal_places,
    warnings,
    label="全体",
):
    if questions is not None and questions <= 0:
        questions = None

    if correct_answers is not None and correct_answers < 0:
        correct_answers = None

    if score_percent is not None and not 0 <= score_percent <= 100:
        warnings.append(
            f"{label}の正答率が0〜100%の範囲外です。"
        )
        score_percent = None

    if (
        questions is not None
        and correct_answers is not None
    ):
        if correct_answers > questions:
            warnings.append(
                f"{label}の正解数が問題数を超えています。"
            )
            correct_answers = None
            score_percent = None
        else:
            calculated_score = (
                correct_answers / questions * 100
            )

            if (
                score_percent is not None
                and not math.isclose(
                    calculated_score,
                    score_percent,
                    abs_tol=_score_tolerance(
                        decimal_places
                    ),
                )
            ):
                warnings.append(
                    f"{label}の正答率は正解数から"
                    f"{calculated_score:.1f}%に補正しました。"
                )

            score_percent = calculated_score

    elif questions is not None and score_percent is not None:
        estimated_correct = round(
            questions * score_percent / 100
        )
        reconstructed_score = (
            estimated_correct / questions * 100
        )

        if math.isclose(
            reconstructed_score,
            score_percent,
            abs_tol=_score_tolerance(decimal_places),
        ):
            correct_answers = estimated_correct
        else:
            warnings.append(
                f"{label}の{questions}問と"
                f"正答率{score_percent:g}%は整合しません。"
                "問題数か正答率を確認してください。"
            )
            score_percent = None

    return questions, correct_answers, score_percent


def _canonical_qualification(value):
    text = _normalize_text(value)
    return _QUALIFICATION_ALIASES.get(text)


def _qualification_from_content(content):
    if "情報セキュリティマネジメント" in content:
        return "SG"

    if re.search(r"(?<![A-Za-z])SG(?![A-Za-z])", content):
        return "SG"

    if "基本情報技術者" in content:
        return "FE"

    if re.search(r"(?<![A-Za-z])FE(?![A-Za-z])", content):
        return "FE"

    if "医療情報技師" in content:
        return "医療情報技師"

    return None


def _canonical_major(value):
    text = _normalize_text(value)
    return _MAJOR_ALIASES.get(text)


def _canonical_category(value, maps=SG_MAPS):
    text = _normalize_text(value)

    if text in maps.category_to_major:
        return text

    return maps.aliases.get(text)


def _extract_percent_after_label(content, label):
    match = re.search(
        rf"{re.escape(label)}(?:分野)?\s*"
        r"(?:は|[:：])?\s*"
        r"(?P<score>\d+(?:\.\d+)?)\s*%",
        content,
    )

    if not match:
        return None

    return float(match.group("score"))


def _find_specific_categories(content, maps=SG_MAPS):
    matches = []
    occupied_spans = []

    aliases = {
        **{name: name for name in maps.category_to_major},
        **maps.aliases,
    }

    for label in sorted(aliases, key=len, reverse=True):
        for match in re.finditer(re.escape(label), content):
            span = match.span()

            if any(
                span[0] < used_end
                and used_start < span[1]
                for used_start, used_end in occupied_spans
            ):
                continue

            matches.append(
                (
                    span[0],
                    aliases[label],
                    _extract_percent_after_label(
                        content,
                        label,
                    ),
                )
            )
            occupied_spans.append(span)

    matches.sort(key=lambda item: item[0])
    return matches


def _normalize_category_results(raw_results, content, warnings,
                                maps=SG_MAPS):
    results = {}

    if isinstance(raw_results, list):
        for item in raw_results:
            if not isinstance(item, dict):
                continue

            category = _canonical_category(
                item.get("category")
                or item.get("subcategory")
                or item.get("field"),
                maps,
            )
            major = _canonical_major(
                item.get("major_category")
                or item.get("major")
            )

            if category:
                major = maps.category_to_major[category]

            if not major:
                continue

            questions = _as_int(item.get("questions"))
            correct_answers = _as_int(
                item.get("correct_answers")
            )
            score_percent = _as_float(
                item.get("score_percent")
            )

            results[(major, category)] = {
                "major_category": major,
                "category": category,
                "questions": questions,
                "correct_answers": correct_answers,
                "score_percent": score_percent,
            }

    specific_matches = _find_specific_categories(content, maps)

    for _, category, score_percent in specific_matches:
        major = maps.category_to_major[category]
        key = (major, category)
        existing = results.get(key, {})

        results[key] = {
            "major_category": major,
            "category": category,
            "questions": existing.get("questions"),
            "correct_answers": existing.get(
                "correct_answers"
            ),
            "score_percent": (
                score_percent
                if score_percent is not None
                else existing.get("score_percent")
            ),
        }

    majors_with_specific_category = {
        major
        for major, category in results
        if category is not None
    }

    for major in maps.majors:
        if major not in content:
            continue

        score_percent = _extract_percent_after_label(
            content,
            major,
        )

        if (
            major in majors_with_specific_category
            and score_percent is None
        ):
            continue

        key = (major, None)
        existing = results.get(key, {})
        results[key] = {
            "major_category": major,
            "category": None,
            "questions": existing.get("questions"),
            "correct_answers": existing.get(
                "correct_answers"
            ),
            "score_percent": (
                score_percent
                if score_percent is not None
                else existing.get("score_percent")
            ),
        }

    for item in results.values():
        (
            item["questions"],
            item["correct_answers"],
            item["score_percent"],
        ) = _resolve_score(
            item["questions"],
            item["correct_answers"],
            item["score_percent"],
            None,
            warnings,
            label=(
                item["category"]
                or item["major_category"]
            ),
        )

    major_order = {
        name: index
        for index, name in enumerate(maps.majors)
    }
    category_order = {
        name: index
        for index, name in enumerate(maps.category_to_major)
    }

    return sorted(
        results.values(),
        key=lambda item: (
            major_order[item["major_category"]],
            category_order.get(
                item["category"],
                -1,
            ),
        ),
    )


def _normalize_weak_points(raw_weak_points, warnings, maps=SG_MAPS):
    if not isinstance(raw_weak_points, list):
        return []

    normalized = []

    for value in raw_weak_points:
        category = _canonical_category(value, maps)
        major = _canonical_major(value)
        weak_point = category or major

        if weak_point and weak_point not in normalized:
            normalized.append(weak_point)
        elif _text_or_none(value):
            warnings.append(
                f"弱点「{_normalize_text(value)}」は"
                f"{maps.label}の固定分野へ分類できませんでした。"
            )

    return normalized


def normalize_study_analysis(
    content,
    raw_analysis,
    current_qualification="SG",
):
    content = _normalize_text(content)
    raw_analysis = (
        raw_analysis
        if isinstance(raw_analysis, dict)
        else {}
    )
    warnings = []

    qualification = (
        _qualification_from_content(content)
        or _canonical_qualification(
            raw_analysis.get("qualification")
        )
    )

    activity = _text_or_none(
        raw_analysis.get("activity")
    )

    if "過去問道場" in content:
        activity = "過去問道場"

    if (
        qualification is None
        and activity == "過去問道場"
    ):
        qualification = (
            _canonical_qualification(
                current_qualification
            )
            or "不明"
        )

    qualification = qualification or "不明"

    (
        explicit_questions,
        explicit_correct_answers,
    ) = _extract_question_and_correct_counts(content)
    (
        explicit_score_percent,
        score_decimal_places,
    ) = _extract_score_percent(content)

    questions = (
        explicit_questions
        if explicit_questions is not None
        else _as_int(raw_analysis.get("questions"))
    )
    correct_answers = (
        explicit_correct_answers
        if explicit_correct_answers is not None
        else _as_int(
            raw_analysis.get("correct_answers")
        )
    )
    score_percent = (
        explicit_score_percent
        if explicit_score_percent is not None
        else _as_float(
            raw_analysis.get("score_percent")
        )
    )

    (
        questions,
        correct_answers,
        score_percent,
    ) = _resolve_score(
        questions,
        correct_answers,
        score_percent,
        score_decimal_places,
        warnings,
    )

    # 資格が分からないときは、いま学習中の資格の分野で読む
    maps = category_maps_for(
        qualification
        if qualification != "不明"
        else _canonical_qualification(current_qualification)
    )

    category_results = _normalize_category_results(
        raw_analysis.get("category_results"),
        content,
        warnings,
        maps,
    )

    if (
        score_percent is not None
        and len(category_results) == 1
        and category_results[0]["score_percent"] is None
        and re.search(r"(?:全て|すべて|全部)", content)
    ):
        category_results[0]["questions"] = questions
        category_results[0]["correct_answers"] = (
            correct_answers
        )
        category_results[0]["score_percent"] = (
            score_percent
        )

    weak_points = _normalize_weak_points(
        raw_analysis.get("weak_points"),
        warnings,
        maps,
    )

    return {
        "qualification": qualification,
        "activity": activity,
        "questions": questions,
        "correct_answers": correct_answers,
        "score_percent": score_percent,
        "category_results": category_results,
        "weak_points": weak_points,
        "notes": _text_or_none(raw_analysis.get("notes")),
        "analysis_warnings": warnings,
    }
