"""勉強ログの解析結果づくりと、勉強ログへの返信文の組み立て。"""

from studybot.formatting import format_category_label, get_review_candidates
from studybot.sg_features import score_from_counts
from studybot.study_log_parser import (
    infer_correct_answers,
    SG_CATEGORY_TO_MAJOR,
)


# ============================================================
# 勉強ログ返信生成
# ============================================================

def build_analysis_reply(
    analysis,
    status_data,
    edited=False
):
    qualification = (
        analysis.get("qualification")
        or "不明"
    )

    activity = analysis.get("activity")
    questions = analysis.get("questions")
    correct_answers = analysis.get(
        "correct_answers"
    )
    score_percent = analysis.get(
        "score_percent"
    )

    category_results = (
        analysis.get("category_results")
        or []
    )

    notes = analysis.get("notes")
    analysis_warnings = (
        analysis.get("analysis_warnings")
        or []
    )

    activity_text = (
        activity
        if activity
        else "記録なし"
    )

    questions_text = (
        f"{questions}問"
        if questions is not None
        else "記録なし"
    )

    score_text = (
        f"{score_percent:g}%"
        if score_percent is not None
        else "記録なし"
    )

    if (
        correct_answers is not None
        and questions is not None
    ):
        correct_text = (
            f"{correct_answers}/{questions}問"
        )
    else:
        correct_text = "記録なし"

    category_lines = []

    for result in category_results:
        major = result.get("major_category")
        category = result.get("category")
        category_score = result.get(
            "score_percent"
        )

        if not major:
            continue

        label = (
            f"{major} > {category}"
            if category
            else major
        )

        if category_score is not None:
            label += f"（{category_score:g}%）"

        category_lines.append(label)

    category_text = (
        "、".join(category_lines)
        if category_lines
        else "記録なし"
    )

    review_candidates = get_review_candidates(
        category_results,
        "score_percent"
    )
    review_text = (
        "、".join(
            f"{format_category_label(item)}"
            f"（{item['score_percent']:g}%）"
            for item in review_candidates
        )
        if review_candidates
        else "なし"
    )

    if edited:
        title = (
            "🔄 **編集内容を再解析して"
            "勉強ログを更新しました！**"
        )
    else:
        title = (
            "✅ **勉強ログを"
            "記録しました！**"
        )

    reply_lines = [
        title,
        "",
        f"📘 資格：**{qualification}**",
        f"📝 内容：**{activity_text}**",
        f"🔢 問題数：**{questions_text}**",
        f"⭕ 正解数：**{correct_text}**",
        f"🎯 正答率：**{score_text}**",
        f"📚 分野：**{category_text}**",
        f"🔁 要復習候補：**{review_text}**",
    ]

    if notes:
        reply_lines.append(
            f"💬 メモ：{notes}"
        )

    if analysis_warnings:
        reply_lines.extend([
            "",
            "⚠️ **入力内容を確認してください**",
            *(
                f"- {warning}"
                for warning in analysis_warnings
            ),
        ])

    if (
        qualification != "不明"
        and status_data is not None
    ):
        cumulative_questions = (
            status_data["total_questions"]
        )

        cumulative_average = (
            status_data["average_score"]
        )

        cumulative_categories = (
            status_data.get("category_status", [])
        )

        if cumulative_average is not None:
            cumulative_score_text = (
                f"{cumulative_average:.1f}%"
            )
        else:
            cumulative_score_text = (
                "記録なし"
            )

        cumulative_review_candidates = (
            get_review_candidates(
                cumulative_categories,
                "average_score"
            )
        )

        if cumulative_review_candidates:
            cumulative_review_text = "、".join(
                f"{format_category_label(item)}"
                f"（{item['average_score']:.1f}% / "
                f"{item['scored_log_count']}回）"
                for item in cumulative_review_candidates[:3]
            )
        else:
            cumulative_review_text = "なし"

        reply_lines.extend([
            "",
            f"📊 **{qualification} 累計**",
            (
                f"🔢 問題数："
                f"**{cumulative_questions}問**"
            ),
            (
                f"🎯 平均正答率："
                f"**{cumulative_score_text}**"
            ),
            (
                f"🔁 要復習候補："
                f"**{cumulative_review_text}**"
            ),
        ])

    return "\n".join(reply_lines)


def build_structured_sg_analysis(
    category,
    questions,
    score_percent,
    notes=None
):
    major_category = SG_CATEGORY_TO_MAJOR[category]
    correct_answers = infer_correct_answers(
        questions,
        score_percent
    )

    return {
        "qualification": "SG",
        "activity": "過去問道場",
        "exam_section": "A",
        "questions": questions,
        "correct_answers": correct_answers,
        "score_percent": score_percent,
        "category_results": [
            {
                "major_category": major_category,
                "category": category,
                "questions": questions,
                "correct_answers": correct_answers,
                "score_percent": score_percent,
            }
        ],
        "weak_points": [],
        "notes": notes.strip() if notes and notes.strip() else None,
        "analysis_warnings": [],
    }


def build_structured_sg_b_analysis(
    topic, questions, correct_answers, wrong_reason=None, memo=None
):
    score = score_from_counts(correct_answers, questions)
    note_parts = []
    if wrong_reason and wrong_reason.strip():
        note_parts.append(f"判断ミス：{wrong_reason.strip()}")
    if memo and memo.strip():
        note_parts.append(memo.strip())
    return {
        "qualification": "SG",
        "activity": "科目B演習",
        "exam_section": "B",
        "questions": questions,
        "correct_answers": correct_answers,
        "score_percent": score,
        "category_results": [{
            "major_category": "科目B",
            "category": topic,
            "questions": questions,
            "correct_answers": correct_answers,
            "score_percent": score,
        }],
        "weak_points": [],
        "notes": " / ".join(note_parts) or None,
        "analysis_warnings": [],
    }
