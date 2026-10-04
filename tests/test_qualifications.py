import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from studybot import config
from studybot import database as database_module
from studybot.analysis import build_structured_sg_analysis
from studybot.data_management import category_choices, get_record, list_records
from studybot.embeds import build_progress_embed, mistake_label
from studybot.exam_results import advance_roadmap
from studybot.features import digest as digest_feature
from studybot.features import qualification as qualification_feature
from studybot.features import review as review_feature
from studybot.forms import (
    SGBPracticeView,
    SGCategorySelect,
    SGMistakeView,
    SGStudyLogView,
    build_mistake_prompt,
    build_sgb_prompt,
    build_sglog_prompt,
)
from studybot.ollama import build_analysis_prompt
from studybot.qualifications import (
    FE,
    IRYO,
    QUALIFICATIONS,
    SG,
    current_qualification,
    get_qualification,
)
from studybot.sg_features import (
    SG_B_TOPICS,
    add_sg_mistake,
    get_sg_b_summary,
    get_sg_category_progress,
    get_sg_mistakes,
    get_sg_plan_status,
    save_sg_b_practice,
    save_sg_plan,
)
from studybot.study_log_parser import (
    SG_CATEGORY_TO_MAJOR,
    SG_PRACTICE_CATEGORIES,
    normalize_study_analysis,
)


TODAY = date(2026, 10, 20)


class QualificationConfigTests(unittest.TestCase):
    def test_sg_matches_the_original_lists(self):
        self.assertEqual(SG.category_names, SG_PRACTICE_CATEGORIES)
        self.assertEqual(SG.b_topics, SG_B_TOPICS)
        for name, major in SG.category_to_major.items():
            self.assertEqual(SG_CATEGORY_TO_MAJOR[name], major)

    def test_every_qualification_is_consistent(self):
        commands = [q.command for q in QUALIFICATIONS]
        self.assertEqual(commands, ["sg", "fe", "iryo"])
        for qualification in QUALIFICATIONS:
            with self.subTest(qualification=qualification.code):
                names = qualification.category_names
                self.assertEqual(len(names), len(set(names)))
                # Discord の選択肢は25件まで（グラフは「全体」を足して25件）
                self.assertLessEqual(len(names), 24)
                grouped = [
                    name for _, group in qualification.progress_groups
                    for name in group
                ]
                self.assertEqual(sorted(grouped), sorted(names))
                for alias, target in qualification.category_aliases.items():
                    self.assertIn(target, qualification.category_to_major)

    def test_fe_and_iryo(self):
        self.assertEqual(len(FE.categories), 23)
        self.assertEqual(FE.majors, ("テクノロジ系", "マネジメント系", "ストラテジ系"))
        self.assertTrue(FE.has_part_b)
        self.assertEqual(FE.section_a_title, "FE 科目A・23分野の進捗")
        self.assertFalse(IRYO.has_part_b)
        self.assertEqual(IRYO.section_a_title, "医療情報技師 3分野の進捗")
        self.assertEqual(IRYO.label, "医療情報技師試験")
        self.assertIs(get_qualification("FE"), FE)
        self.assertIsNone(get_qualification("ITパスポート"))


class ParserTests(unittest.TestCase):
    def test_fe_log_uses_fe_categories(self):
        analysis = normalize_study_analysis(
            "FE過去問道場20問中12問正解。アルゴリズムを中心に解いた。", {},
        )
        self.assertEqual(analysis["qualification"], "FE")
        self.assertEqual(
            [(r["major_category"], r["category"]) for r in analysis["category_results"]],
            [("テクノロジ系", "アルゴリズムとプログラミング")],
        )
        self.assertEqual(analysis["score_percent"], 60.0)

    def test_unknown_qualification_uses_current_one(self):
        analysis = normalize_study_analysis(
            "過去問道場10問。ハードウェア 70%",
            {"weak_points": ["ハードウェア", "情報セキュリティ管理"]},
            current_qualification="FE",
        )
        self.assertEqual(analysis["qualification"], "FE")
        self.assertEqual(analysis["category_results"][0]["category"], "ハードウェア")
        self.assertEqual(analysis["weak_points"], ["ハードウェア"])
        self.assertIn("FEの固定分野", analysis["analysis_warnings"][-1])

    def test_sg_log_is_unchanged(self):
        analysis = normalize_study_analysis(
            "SG過去問道場25問中10問正解。テクノロジ系のセキュリティを学習。", {},
        )
        self.assertEqual(analysis["qualification"], "SG")
        self.assertEqual(analysis["category_results"][0]["category"], "セキュリティ")

    def test_analysis_prompt(self):
        fe_prompt = build_analysis_prompt(FE)
        self.assertIn("FEの分野名は次の固定値だけ", fe_prompt)
        self.assertIn("- アルゴリズムとプログラミング", fe_prompt)
        self.assertNotIn("情報セキュリティ管理", fe_prompt)
        self.assertIn("SGの分野名は次の固定値だけ", build_analysis_prompt())
        self.assertIn("- 医学・医療系", build_analysis_prompt(IRYO))


class _TempDBCase(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = str(Path(temp_dir.name) / "study.db")
        db_patch = patch.object(config, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        database_module.init_db()
        self.next_id = 500

    def save_log(self, analysis, study_date="2026-10-19"):
        self.next_id += 1
        message = SimpleNamespace(
            id=self.next_id,
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2),
            author=SimpleNamespace(id=7, display_name="test"),
            created_at=datetime.fromisoformat(f"{study_date}T10:00:00+09:00"),
            content="log",
        )
        database_module.save_study_log(message)
        database_module.save_study_analysis(message, analysis)
        return message


class FeatureDataTests(_TempDBCase):
    def test_structured_analysis(self):
        fe = build_structured_sg_analysis("ネットワーク", 20, 55.0, qualification="FE")
        self.assertEqual(fe["qualification"], "FE")
        self.assertEqual(fe["category_results"][0]["major_category"], "テクノロジ系")
        self.assertEqual(fe["activity"], "過去問道場")
        iryo = build_structured_sg_analysis(
            "医学・医療系", 10, 70.0, qualification="医療情報技師"
        )
        self.assertEqual(iryo["activity"], "問題演習")
        self.assertEqual(iryo["category_results"][0]["major_category"], "医学・医療系")

    def test_progress_is_separated_by_qualification(self):
        self.save_log(build_structured_sg_analysis("ネットワーク", 20, 40.0))
        self.save_log(build_structured_sg_analysis(
            "ネットワーク", 30, 80.0, qualification="FE"
        ))
        self.save_log(build_structured_sg_analysis(
            "ハードウェア", 10, 50.0, qualification="FE"
        ))

        sg_items, _ = get_sg_category_progress(self.db_path, 7)
        fe_items, _ = get_sg_category_progress(self.db_path, 7, "FE")
        self.assertEqual(len(fe_items), 23)
        by_name = {item["category"]: item for item in fe_items}
        self.assertEqual(by_name["ネットワーク"]["questions"], 30)
        self.assertEqual(by_name["ハードウェア"]["latest_score"], 50.0)
        self.assertEqual(
            {i["category"]: i["questions"] for i in sg_items}["ネットワーク"], 20
        )

    def test_mistakes_part_b_and_plans_per_qualification(self):
        add_sg_mistake(self.db_path, 7, "ネットワーク", "q1", "r", today=TODAY)
        fe_id, _ = add_sg_mistake(
            self.db_path, 7, "ハードウェア", "q2", "r", today=TODAY,
            qualification="FE",
        )
        with self.assertRaisesRegex(ValueError, "FEの23分野"):
            add_sg_mistake(
                self.db_path, 7, "情報セキュリティ管理", "q", "r",
                qualification="FE",
            )
        all_items = get_sg_mistakes(self.db_path, 7, today=TODAY, due_only=False)
        self.assertEqual([i["qualification"] for i in all_items], ["SG", "FE"])
        self.assertEqual(
            [i["id"] for i in get_sg_mistakes(
                self.db_path, 7, today=TODAY, due_only=False, qualification="FE"
            )],
            [fe_id],
        )

        message = self.save_log({
            "qualification": "FE", "exam_section": "B", "questions": 4,
            "correct_answers": 3, "score_percent": 75.0, "category_results": [],
        })
        save_sg_b_practice(
            self.db_path, message.id, 7, "アルゴリズムとプログラミング", 4, 3,
            "条件分岐を読み違えた", "", "2026-10-19", qualification="FE",
        )
        with self.assertRaises(ValueError):
            save_sg_b_practice(
                self.db_path, 999, 7, "委託先管理", 4, 4, "", "", "2026-10-19",
                qualification="FE",
            )
        self.assertEqual(get_sg_b_summary(self.db_path, 7, "FE")["questions"], 4)
        self.assertEqual(get_sg_b_summary(self.db_path, 7)["questions"], 0)

        save_sg_plan(self.db_path, 7, 4, 30, "t", today=TODAY)
        save_sg_plan(self.db_path, 7, 8, 50, "t", today=TODAY, qualification="FE")
        self.assertEqual(get_sg_plan_status(self.db_path, 7, today=TODAY)["weeks"], 4)
        fe_plan = get_sg_plan_status(self.db_path, 7, today=TODAY, qualification="FE")
        self.assertEqual((fe_plan["weeks"], fe_plan["qualification"]), (8, "FE"))

    def test_current_qualification_follows_roadmap(self):
        self.assertIs(current_qualification(self.db_path), SG)
        advance_roadmap(self.db_path, "SG")
        self.assertIs(current_qualification(self.db_path), FE)
        self.assertIs(current_qualification(str(Path(self.db_path).parent / "none.db")), SG)

    def test_data_management_handles_fe_logs(self):
        self.save_log(build_structured_sg_analysis("ネットワーク", 20, 40.0))
        fe_message = self.save_log(build_structured_sg_analysis(
            "ソフトウェア", 10, 60.0, qualification="FE"
        ))
        rows, _ = list_records(self.db_path, 7, "log")
        self.assertEqual({row["qualification"] for row in rows}, {"SG", "FE"})

        record = get_record(self.db_path, 7, "log", fe_message.id)
        self.assertEqual(category_choices(record, "log"), FE.category_names)
        add_sg_mistake(
            self.db_path, 7, "医学・医療系", "q", "r", qualification="医療情報技師"
        )
        mistake = list_records(self.db_path, 7, "mistake")[0][0]
        full = get_record(self.db_path, 7, "mistake", mistake["id"])
        self.assertEqual(category_choices(full, "mistake"), IRYO.category_names)


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.guild = SimpleNamespace(
            id=1,
            text_channels=[SimpleNamespace(name=config.STUDY_LOG_CHANNEL_NAME, id=5)],
        )

    def test_log_and_mistake_screens_use_the_qualification(self):
        message, view = build_sglog_prompt(self.guild, 7, FE)
        self.assertIn("【基本情報技術者（FE）】", message)
        self.assertIsInstance(view, SGStudyLogView)
        select = next(i for i in view.children if isinstance(i, SGCategorySelect))
        self.assertEqual(len(select.options), 23)
        self.assertEqual(select.options[0].description, "テクノロジ系")

        _, mistake_view = build_mistake_prompt(7, IRYO)
        self.assertIsInstance(mistake_view, SGMistakeView)
        self.assertIs(mistake_view.qualification, IRYO)

    def test_part_b_only_where_it_exists(self):
        message, view = build_sgb_prompt(self.guild, 7, FE)
        self.assertIsInstance(view, SGBPracticeView)
        topic_select = next(i for i in view.children if hasattr(i, "options"))
        self.assertEqual(
            [o.label for o in topic_select.options], list(FE.b_topics)
        )
        message, view = build_sgb_prompt(self.guild, 7, IRYO)
        self.assertIsNone(view)
        self.assertIn("科目Bがありません", message)

    def test_commands_per_qualification(self):
        commands = qualification_feature.COMMANDS
        self.assertEqual(set(commands["FE"]), {"log", "b", "mock", "progress", "chart", "status"})
        self.assertNotIn("b", commands["医療情報技師"])
        chart_choices = commands["FE"]["chart"].app_command.parameters[0].choices
        self.assertEqual(len(chart_choices), 24)

    def test_progress_embed_groups(self):
        items = [
            {"category": name, "questions": 12, "latest_score": 50.0,
             "last_study_date": "2026-10-19"}
            for name in FE.category_names
        ]
        b = {"questions": 0, "correct_answers": 0, "score_percent": None,
             "recent_sessions": []}
        embed = build_progress_embed(items, 0, b, 60.0, FE)
        self.assertEqual(embed.title, "FE 科目A・23分野の進捗")
        self.assertEqual(
            [f.name for f in embed.fields],
            ["テクノロジ系", "マネジメント系", "ストラテジ系", "科目B"],
        )
        self.assertTrue(all(len(f.value) <= 1024 for f in embed.fields))

        iryo_items = [
            {"category": name, "questions": 0, "latest_score": None,
             "last_study_date": None}
            for name in IRYO.category_names
        ]
        iryo = build_progress_embed(iryo_items, 0, b, 60.0, IRYO)
        self.assertEqual([f.name for f in iryo.fields], ["分野"])

    def test_mistake_label(self):
        item = {"category": "ネットワーク", "qualification": "SG"}
        self.assertEqual(mistake_label(item, "SG"), "ネットワーク")
        self.assertEqual(mistake_label(item, "FE"), "SG・ネットワーク")
        self.assertEqual(mistake_label({"category": "x"}, "FE"), "x")


class _Context:
    def __init__(self):
        self.author = SimpleNamespace(id=7)
        self.guild = None
        self.interaction = object()
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append(dict(kwargs, content=content))


class CommandFlowTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    async def test_review_add_picks_qualification(self):
        ctx = _Context()
        await review_feature.mistake.callback(ctx)
        self.assertIs(ctx.messages[-1]["view"].qualification, SG)

        await review_feature.mistake.callback(ctx, qualification="FE")
        self.assertIs(ctx.messages[-1]["view"].qualification, FE)

        await review_feature.mistake.callback(ctx, qualification="ITパスポート")
        self.assertIn("候補から", ctx.messages[-1]["content"])

        advance_roadmap(self.db_path, "SG")
        await review_feature.mistake.callback(ctx)
        self.assertIs(ctx.messages[-1]["view"].qualification, FE)

    async def test_fe_progress_command(self):
        self.save_log(build_structured_sg_analysis(
            "データベース", 20, 45.0, qualification="FE"
        ))
        ctx = _Context()
        await qualification_feature.COMMANDS["FE"]["progress"].callback(ctx)
        embed = ctx.messages[0]["embed"]
        self.assertEqual(embed.title, "FE 科目A・23分野の進捗")
        self.assertIn("**データベース**", embed.fields[0].value)

    def test_digest_follows_current_qualification(self):
        self.save_log(build_structured_sg_analysis(
            "ネットワーク", 20, 40.0, qualification="SG"
        ))
        self.save_log(build_structured_sg_analysis(
            "ハードウェア", 20, 45.0, qualification="FE"
        ))
        save_sg_plan(self.db_path, 7, 4, 30, "t", today=TODAY, qualification="FE")

        sg_embed = digest_feature.build_daily_digest_embed(7, TODAY)
        self.assertIsNone(next(
            (f for f in sg_embed.fields if f.name.startswith("今週の目標")), None
        ))
        self.assertIn("ネットワーク", sg_embed.fields[-1].value)

        advance_roadmap(self.db_path, "SG")
        fe_embed = digest_feature.build_daily_digest_embed(7, TODAY)
        self.assertIsNotNone(next(
            (f for f in fe_embed.fields if f.name.startswith("今週の目標")), None
        ))
        weak_field = next(f for f in fe_embed.fields if f.name.startswith("正答率"))
        self.assertIn("ハードウェア 45%", weak_field.value)
        self.assertEqual(fe_embed.fields[-1].name, "今日のチェックリスト")


if __name__ == "__main__":
    unittest.main()
