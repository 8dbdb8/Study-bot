import unittest

from study_log_parser import (
    infer_correct_answers,
    normalize_study_analysis,
    parse_question_count_input,
    parse_score_percent_input,
)


class StructuredInputTests(unittest.TestCase):
    def test_parses_full_width_question_count(self):
        self.assertEqual(
            parse_question_count_input("２５"),
            25,
        )

    def test_rejects_invalid_question_counts(self):
        for value in ("0", "1.5", "1001"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_question_count_input(value)

    def test_rounds_score_half_up_to_one_decimal_place(self):
        self.assertEqual(
            parse_score_percent_input("40.25"),
            40.3,
        )
        self.assertEqual(
            parse_score_percent_input("４０．２４％"),
            40.2,
        )

    def test_rejects_out_of_range_score(self):
        with self.assertRaises(ValueError):
            parse_score_percent_input("100.1")

    def test_infers_correct_answers_only_when_consistent(self):
        self.assertEqual(
            infer_correct_answers(25, 40.0),
            10,
        )
        self.assertEqual(
            infer_correct_answers(3, 66.7),
            2,
        )
        self.assertIsNone(
            infer_correct_answers(1, 66.0)
        )


class NormalizeStudyAnalysisTests(unittest.TestCase):
    def test_extracts_overall_score_and_sg_category(self):
        analysis = normalize_study_analysis(
            "SG過去問道場25問。正答率40.0%。"
            "全てテクノロジ系のセキュリティに関して。",
            {
                "qualification": "SG",
                "activity": "過去問道場",
                "questions": 20,
                "score_percent": 80,
                "weak_points": [],
            },
        )

        self.assertEqual(analysis["questions"], 25)
        self.assertEqual(analysis["correct_answers"], 10)
        self.assertEqual(analysis["score_percent"], 40.0)
        self.assertEqual(
            analysis["category_results"],
            [
                {
                    "major_category": "テクノロジ系",
                    "category": "セキュリティ",
                    "questions": 25,
                    "correct_answers": 10,
                    "score_percent": 40.0,
                }
            ],
        )

    def test_correct_count_overrides_conflicting_percent(self):
        analysis = normalize_study_analysis(
            "SG過去問道場10問中7問正解。正答率50%。",
            {},
        )

        self.assertEqual(analysis["questions"], 10)
        self.assertEqual(analysis["correct_answers"], 7)
        self.assertEqual(analysis["score_percent"], 70.0)
        self.assertTrue(analysis["analysis_warnings"])

    def test_does_not_assign_overall_score_without_all_scope(self):
        analysis = normalize_study_analysis(
            "SG過去問道場10問。正答率40%。"
            "セキュリティを学習。",
            {},
        )

        self.assertIsNone(
            analysis["category_results"][0][
                "score_percent"
            ]
        )

    def test_rejects_impossible_question_and_percent_pair(self):
        analysis = normalize_study_analysis(
            "SG過去問道場1問。正答率66%。",
            {},
        )

        self.assertEqual(analysis["questions"], 1)
        self.assertIsNone(analysis["correct_answers"])
        self.assertIsNone(analysis["score_percent"])
        self.assertIn(
            "1問と正答率66%は整合しません",
            analysis["analysis_warnings"][0],
        )

    def test_extracts_each_major_category_score(self):
        analysis = normalize_study_analysis(
            "SG過去問道場16問。正答率50%。"
            "マネジメント系100%、ストラテジ系33.3%、"
            "テクノロジ系44.4%。",
            {},
        )

        category_scores = {
            item["major_category"]: item["score_percent"]
            for item in analysis["category_results"]
        }
        self.assertEqual(
            category_scores,
            {
                "テクノロジ系": 44.4,
                "マネジメント系": 100.0,
                "ストラテジ系": 33.3,
            },
        )

    def test_rejects_out_of_range_category_score(self):
        analysis = normalize_study_analysis(
            "SG過去問道場10問。正答率40%。"
            "テクノロジ系120%。",
            {},
        )

        self.assertIsNone(
            analysis["category_results"][0][
                "score_percent"
            ]
        )
        self.assertTrue(analysis["analysis_warnings"])

    def test_uses_only_the_three_qualification_roadmap(self):
        sg = normalize_study_analysis(
            "過去問道場を10問。正答率40%。",
            {"qualification": "Oracle Java Silver"},
            current_qualification="SG",
        )
        medical = normalize_study_analysis(
            "医療情報技師の問題を10問中8問正解。",
            {},
        )

        self.assertEqual(sg["qualification"], "SG")
        self.assertEqual(
            medical["qualification"],
            "医療情報技師",
        )

    def test_normalizes_weak_points_to_fixed_categories(self):
        analysis = normalize_study_analysis(
            "SG過去問道場10問。正答率40%。",
            {
                "weak_points": [
                    "セキュリティ管理",
                    "マネジメント",
                    "あいまいな分野",
                ]
            },
        )

        self.assertEqual(
            analysis["weak_points"],
            [
                "情報セキュリティ管理",
                "マネジメント系",
            ],
        )
        self.assertTrue(analysis["analysis_warnings"])


if __name__ == "__main__":
    unittest.main()
