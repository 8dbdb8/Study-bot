import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from studybot import config
from studybot import database as database_module
from studybot.analysis import build_structured_sg_analysis
from studybot.daily_digest import take_rest_day
from studybot.embeds import build_progress_embed
from studybot.exam_results import get_pending_exam, parse_exam_score
from studybot.exam_schedule import get_study_streak, save_exam_date
from studybot.features import digest as digest_feature
from studybot.features import exam_result as exam_result_feature
from studybot.features import qualification as qualification_feature
from studybot.features import review as review_feature
from studybot.forms import SGStudyLogModal, build_quick_log_modal
from studybot.groups import GETTING_STARTED
from studybot.qualifications import FE, SG
from studybot.sg_features import add_sg_mistake, get_weak_categories


TODAY = date(2026, 10, 20)


class _Response:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.modals = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append(dict(kwargs, content=content))

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(dict(kwargs, content=content))


def _guild():
    return SimpleNamespace(
        id=1,
        text_channels=[SimpleNamespace(name=config.STUDY_LOG_CHANNEL_NAME, id=5)],
    )


def _interaction(user_id=7, guild=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild=guild if guild is not None else _guild(),
        message=SimpleNamespace(content="<@7> 結果はどうでしたか？"),
        response=_Response(),
        followup=_Followup(),
    )


class _TempDBCase(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = str(Path(temp_dir.name) / "study.db")
        db_patch = patch.object(config, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        database_module.init_db()
        self.next_id = 900

    def save_log(self, analysis, study_date="2026-10-19", user_id=7):
        self.next_id += 1
        message = SimpleNamespace(
            id=self.next_id,
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2),
            author=SimpleNamespace(id=user_id, display_name="test"),
            created_at=datetime.fromisoformat(f"{study_date}T10:00:00+09:00"),
            content="log",
        )
        database_module.save_study_log(message)
        database_module.save_study_analysis(message, analysis)


class QuickLogFormTests(unittest.IsolatedAsyncioTestCase):
    def test_form_has_category_select_with_default(self):
        modal, message = build_quick_log_modal(_guild(), FE, "ネットワーク")
        self.assertIsNone(message)
        self.assertIsInstance(modal, SGStudyLogModal)
        payload = modal.to_dict()
        self.assertEqual(payload["title"], "FE過去問道場を記録")
        labels = [c["label"] for c in payload["components"]]
        self.assertEqual(labels, [
            "分野", "解いた問題数", "正答率（%）", "かかった時間（分・任意）", "メモ（任意）",
        ])
        options = payload["components"][0]["component"]["options"]
        self.assertEqual(len(options), 23)
        self.assertEqual(
            [o["label"] for o in options if o.get("default")], ["ネットワーク"]
        )

    def test_form_with_fixed_category_has_no_select(self):
        modal = SGStudyLogModal("ネットワーク", object(), SG)
        self.assertIsNone(modal.category_select)
        self.assertEqual(len(modal.to_dict()["components"]), 4)

    def test_form_cannot_open_without_channel(self):
        modal, message = build_quick_log_modal(None, SG)
        self.assertIsNone(modal)
        self.assertIn("サーバー内", message)
        modal, message = build_quick_log_modal(
            SimpleNamespace(id=1, text_channels=[]), SG
        )
        self.assertIsNone(modal)
        self.assertIn("/setup", message)

    async def test_log_asks_what_was_studied_first(self):
        sent = []

        async def send(content=None, **kwargs):
            sent.append(dict(kwargs, content=content))

        prefix_ctx = SimpleNamespace(
            interaction=None, guild=_guild(), author=SimpleNamespace(id=7),
            send=send,
        )
        await qualification_feature.record_log(prefix_ctx, FE)
        self.assertIn("何を勉強しましたか", sent[0]["content"])
        self.assertIs(sent[0]["view"].qualification, FE)


class RestDayTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def test_once_a_week(self):
        self.assertEqual(take_rest_day(self.db_path, 7, TODAY), ("ok", TODAY))
        self.assertEqual(take_rest_day(self.db_path, 7, TODAY), ("already", TODAY))
        self.assertEqual(
            take_rest_day(self.db_path, 7, TODAY + timedelta(days=6)),
            ("too_soon", TODAY),
        )
        later = TODAY + timedelta(days=7)
        self.assertEqual(take_rest_day(self.db_path, 7, later), ("ok", later))
        self.assertEqual(take_rest_day(self.db_path, 8, TODAY), ("ok", TODAY))

    def test_rest_day_keeps_the_streak_without_counting(self):
        for day in ("2026-10-16", "2026-10-17", "2026-10-19"):
            self.save_log(build_structured_sg_analysis("ネットワーク", 10, 50.0), day)
        self.assertEqual(get_study_streak(self.db_path, 7, TODAY), 1)

        take_rest_day(self.db_path, 7, date(2026, 10, 18))
        self.assertEqual(get_study_streak(self.db_path, 7, TODAY), 3)
        # 今日休んでも、昨日までの連続は残る
        take_rest_day(self.db_path, 7, TODAY + timedelta(days=7))
        self.assertEqual(
            get_study_streak(self.db_path, 7, TODAY + timedelta(days=7)), 0
        )

    async def test_rest_button(self):
        view = digest_feature.DailyDigestView()
        first = _interaction()
        await view.rest_button.callback(first)
        self.assertIn("今日はお休みにしました", first.response.sent[0]["content"])

        second = _interaction()
        with patch.object(
            digest_feature, "datetime",
            SimpleNamespace(now=lambda tz: datetime.now(tz) + timedelta(days=3)),
        ):
            await view.rest_button.callback(second)
        self.assertIn("1週間に1回まで", second.response.sent[0]["content"])


class WeakCategoryTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.save_log(build_structured_sg_analysis("ネットワーク", 20, 40.0))
        self.save_log(build_structured_sg_analysis("データベース", 20, 55.0))
        self.save_log(build_structured_sg_analysis("システム監査", 20, 80.0))
        self.save_log(build_structured_sg_analysis("企業活動", 5, 10.0))

    def test_weak_categories(self):
        self.assertEqual(
            get_weak_categories(self.db_path, 7),
            [("ネットワーク", 40.0), ("データベース", 55.0)],
        )
        self.assertEqual(
            get_weak_categories(self.db_path, 7, threshold=None)[-1],
            ("システム監査", 80.0),
        )
        self.assertEqual(digest_feature.weakest_category(7, SG), "ネットワーク")

    async def test_digest_log_button_preselects_weakest(self):
        interaction = _interaction()
        await digest_feature.DailyDigestView().log_button.callback(interaction)
        modal = interaction.response.modals[0]
        defaults = [o.value for o in modal.category_select.options if o.default]
        self.assertEqual(defaults, ["ネットワーク"])

    async def test_progress_has_score_and_weak_review(self):
        sent = []

        async def send(content=None, **kwargs):
            sent.append(dict(kwargs, content=content))

        ctx = SimpleNamespace(author=SimpleNamespace(id=7), send=send)
        await qualification_feature.show_progress(ctx, SG)
        self.assertIn("正答率 **", sent[0]["embed"].description)
        view = sent[0]["view"]
        self.assertIsInstance(view, qualification_feature.WeakReviewView)

        # 弱い分野に誤答がなければ、一番弱い分野で記録フォームを開く
        no_mistakes = _interaction()
        await view.weak_review_button.callback(no_mistakes)
        modal = no_mistakes.response.modals[0]
        self.assertEqual(
            [o.value for o in modal.category_select.options if o.default],
            ["ネットワーク"],
        )

        # 弱い分野の誤答は、復習日前でも解き直す
        future = datetime.now(config.JST).date() + timedelta(days=30)
        add_sg_mistake(self.db_path, 7, "ネットワーク", "q1", "r", today=future)
        add_sg_mistake(self.db_path, 7, "システム監査", "q2", "r", today=future)
        with_mistakes = _interaction()
        await view.weak_review_button.callback(with_mistakes)
        session = with_mistakes.response.sent[0]["view"]
        self.assertIsInstance(session, review_feature.ReviewSessionView)
        self.assertEqual([i["category"] for i in session.items], ["ネットワーク"])

    def test_progress_embed_without_score(self):
        embed = build_progress_embed([], 0, {"questions": 0}, 60.0, SG)
        self.assertNotIn("正答率 **", embed.description)


class ExamScoreTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.today = datetime.now(config.JST).date()
        save_exam_date(self.db_path, 7, "SG", self.today, "t")

    def test_parse_score(self):
        self.assertEqual(parse_exam_score("720"), 720)
        self.assertEqual(parse_exam_score("７２０"), 720)
        self.assertIsNone(parse_exam_score(" "))
        for value in ("1001", "-5", "abc"):
            with self.assertRaises(ValueError):
                parse_exam_score(value)

    async def test_score_form_records_pass_with_score(self):
        view = exam_result_feature.ExamResultView()
        open_form = _interaction()
        await view.score_button.callback(open_form)
        modal = open_form.response.modals[0]
        self.assertIsInstance(modal, exam_result_feature.ExamScoreModal)

        modal.score_input._value = "720"
        modal.result_select._values = ["pass"]
        submit = _interaction()
        await modal.on_submit(submit)

        self.assertIn("合格（720点）を記録しました", submit.response.edits[0]["content"])
        sent = submit.followup.sent[0]
        self.assertEqual(sent["embed"].description, "この記録は「合格までの記録」として残ります。")
        self.assertEqual(
            {f.name: f.value for f in sent["embed"].fields}["得点"], "720点"
        )
        self.assertEqual(
            sent["view"].next_exam_button.label, "基本情報技術者（FE）の試験日を設定"
        )
        self.assertIsNone(get_pending_exam(self.db_path, 7, self.today))

    async def test_bad_score_and_missing_exam(self):
        modal = exam_result_feature.ExamScoreModal()
        modal.score_input._value = "1200"
        modal.result_select._values = ["fail"]
        interaction = _interaction()
        await modal.on_submit(interaction)
        self.assertIn("0〜1000", interaction.response.sent[0]["content"])

        nobody = _interaction(user_id=99)
        await exam_result_feature.ExamResultView().score_button.callback(nobody)
        self.assertIn("見つかりません", nobody.response.sent[0]["content"])


class HelpTests(unittest.TestCase):
    def test_getting_started_steps(self):
        lines = GETTING_STARTED.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("**1. 勉強部屋に入る**"))
        self.assertIn("`/fe log`", lines[1])


if __name__ == "__main__":
    unittest.main()
