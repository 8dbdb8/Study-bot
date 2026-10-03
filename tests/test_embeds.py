import unittest
from datetime import date

from studybot.embeds import (
    COLOR_ALERT,
    build_digest_embed,
    build_plan_status_embed,
    build_progress_embed,
    build_review_card_embed,
    build_review_list_embed,
    build_review_summary_embed,
    build_vc_summary_embed,
    format_minutes,
    text_bar,
    week_lines,
)
from studybot.study_log_parser import SG_PRACTICE_CATEGORIES


def field(embed, name_prefix):
    return next(f for f in embed.fields if f.name.startswith(name_prefix))


def mistake(id_, category="ネットワーク", streak=0, memo=None):
    return {
        "id": id_, "category": category,
        "question_ref": f"令和5年 問{id_}", "reason": "選択肢を読み違えた",
        "memo": memo, "next_review_on": "2026-10-04",
        "success_streak": streak,
    }


class BasicsTests(unittest.TestCase):
    def test_text_bar_and_minutes(self):
        self.assertEqual(text_bar(0.64), "▰▰▰▰▰▰▱▱▱▱")
        self.assertEqual(text_bar(2), "▰" * 10)
        self.assertEqual(text_bar(None, 4), "▱▱▱▱")
        self.assertEqual(format_minutes(59), "0分")
        self.assertEqual(format_minutes(4747), "1時間19分")

    def test_week_lines_cover_monday_to_today(self):
        # 2026-10-04 は日曜
        lines = week_lines(
            [("2026-09-28", 3600), ("2026-10-01", 1800)], date(2026, 10, 4)
        ).splitlines()
        self.assertEqual(len(lines), 7)
        self.assertTrue(lines[0].startswith("`月` `▰▰▰▰▰▰▰▰`"))
        self.assertTrue(lines[1].endswith("—"))
        self.assertIn("30分", lines[3])

        self.assertEqual(
            len(week_lines([], date(2026, 9, 30)).splitlines()), 3
        )


class ProgressEmbedTests(unittest.TestCase):
    def test_all_categories_fit_without_truncation(self):
        items = [
            {
                "category": category,
                "questions": 120 if index % 3 else 0,
                "latest_score": 55.5 if index % 2 else 72.0,
                "last_study_date": "2026-10-01" if index % 3 else None,
            }
            for index, category in enumerate(SG_PRACTICE_CATEGORIES)
        ]
        b_summary = {
            "questions": 38, "correct_answers": 27, "score_percent": 71.05,
            "recent_sessions": [
                ("アクセス権限", 5, 4, "@everyone 判断ミス" * 20, "2026-10-02")
            ] * 3,
        }

        embed = build_progress_embed(items, 7, b_summary)

        text = "\n".join(f.value for f in embed.fields)
        for category in SG_PRACTICE_CATEGORIES:
            self.assertIn(f"**{category}**", text)
        self.assertIn("🟠 **情報セキュリティ管理**　要復習", text)
        self.assertIn("⚪ **情報セキュリティ**　未着手", text)
        self.assertIn("旧ログなど：7問", text)
        self.assertIn("38問中27問正解（71.0%）", field(embed, "科目B").value)
        self.assertNotIn("@everyone", field(embed, "科目B").value)
        self.assertTrue(all(len(f.value) <= 1024 for f in embed.fields))
        self.assertLessEqual(len(embed), 6000)


class PlanAndReviewEmbedTests(unittest.TestCase):
    def test_plan_status_marks_current_week(self):
        status = {
            "start_on": date(2026, 10, 4), "weeks": 2,
            "weekly_questions": 30, "current_week": 2, "completed": False,
            "rows": [
                {"week": 1, "start_on": date(2026, 10, 4),
                 "end_on": date(2026, 10, 10), "target": 30, "actual": 30,
                 "remaining": 0},
                {"week": 2, "start_on": date(2026, 10, 11),
                 "end_on": date(2026, 10, 17), "target": 30, "actual": 12,
                 "remaining": 18},
            ],
        }
        embed = build_plan_status_embed(status, "SG試験：…")
        weeks = field(embed, "週ごとの実績").value.splitlines()
        self.assertTrue(weeks[0].startswith("✓ 第1週 10/4〜10/10"))
        self.assertTrue(weeks[1].startswith("▶ 第2週 10/11〜10/17"))
        self.assertEqual(field(embed, "今週の残り").value, "**18問**")
        self.assertEqual(field(embed, "試験").value, "SG試験：…")

    def test_review_list_and_card(self):
        items = [mistake(i, memo="メモ" if i == 1 else None) for i in range(1, 13)]
        embed = build_review_list_embed(items)
        self.assertEqual(len(embed.fields), 11)
        self.assertIn("ほか2件", embed.fields[-1].value)
        self.assertIn("メモ：メモ", embed.fields[0].value)

        card = build_review_card_embed(mistake(5, streak=2), 2, 3)
        self.assertEqual(card.title, "復習 2/3 ・ #5 ネットワーク")
        self.assertIn("連続正解 2/3", card.footer.text)

    def test_review_summary(self):
        summary = build_review_summary_embed(
            {"correct": 2, "wrong": 1, "skipped": 1, "completed": 1}
        )
        self.assertEqual(summary.title, "今日の復習おわり")
        self.assertIn("1問が3回連続正解で完了", summary.description)
        self.assertIn("あとで：1問", summary.description)

        stopped = build_review_summary_embed(
            {"correct": 0, "wrong": 0, "skipped": 3, "completed": 0}
        )
        self.assertEqual(stopped.title, "復習を中断しました")


class DigestEmbedTests(unittest.TestCase):
    today = date(2026, 10, 15)  # 木曜

    def test_nothing_to_say_returns_none(self):
        self.assertIsNone(build_digest_embed(self.today, None, 3, []))

    def test_full_digest(self):
        plan_week = {
            "week": 2, "start_on": date(2026, 10, 11),
            "end_on": date(2026, 10, 17), "target": 30, "actual": 12,
            "remaining": 18,
        }
        embed = build_digest_embed(
            self.today,
            "SG試験：2026年10月17日（土）　あと**2日**（約1週間）",
            12,
            [mistake(i) for i in range(1, 8)],
            plan_week,
            [("ネットワーク", 48.0), ("システム監査", 55.4)],
        )

        self.assertEqual(embed.title, "10月15日（木）の学習メニュー")
        self.assertEqual(embed.color.value, COLOR_ALERT)
        self.assertIn("🔥 連続学習 12日", embed.description)
        reviews = field(embed, "今日の復習 7件").value
        self.assertEqual(reviews.count("\n"), 5)
        self.assertIn("ほか2件", reviews)
        goal = field(embed, "今週の目標（第2週）").value
        self.assertIn("12/30問", goal)
        self.assertIn("残り3日で18問 → 1日あたり **6問**", goal)
        self.assertEqual(
            field(embed, "正答率60%未満").value,
            "ネットワーク 48% ・ システム監査 55%",
        )

    def test_goal_met_and_no_reviews(self):
        embed = build_digest_embed(
            self.today, None, 0, [],
            {"week": 1, "start_on": self.today, "end_on": self.today,
             "target": 30, "actual": 31, "remaining": 0},
        )
        self.assertIn("予定どおり", field(embed, "今日の復習").value)
        self.assertIn("達成済み", field(embed, "今週の目標").value)


class VCSummaryEmbedTests(unittest.TestCase):
    def test_summary_fields(self):
        embed = build_vc_summary_embed(
            "**太郎**", 4747, 7512,
            [("2026-10-03", 3000), ("2026-10-04", 7512)],
            date(2026, 10, 4), 12, "SG試験：…",
        )
        self.assertEqual(embed.title, "\\*\\*太郎\\*\\*さん、勉強おつかれさま")
        self.assertEqual(
            [(f.name, f.value) for f in embed.fields[:3]],
            [("今回", "1時間19分"), ("今日", "2時間05分"), ("今週", "2時間55分")],
        )
        self.assertIn("連続 12日", embed.fields[3].name)
        self.assertEqual(field(embed, "試験").value, "SG試験：…")


if __name__ == "__main__":
    unittest.main()
