import random
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from studybot import config
from studybot.analysis import build_structured_sg_analysis
from studybot.badges import BADGES, check_new_badges, get_earned_badges
from studybot.charts import render_time_of_day_chart
from studybot.daily_word import pick_word, posted_today, word_key_for_message
from studybot.database import save_completed_study_session
from studybot.features import ai as ai_feature
from studybot.features import badges as badges_feature
from studybot.features import daily_word as word_feature
from studybot.features import digest as digest_feature
from studybot.features import notes as notes_feature
from studybot.features import notion_export
from studybot.features import qualification as qualification_feature
from studybot.features import quiz as quiz_feature
from studybot.features import time as time_feature
from studybot.forms import SGStudyLogModal
from studybot.habits import format_daily_notes, get_daily_notes, save_daily_note
from studybot.pace import build_pace_lines, daily_question_average
from studybot.qualifications import IRYO, SG
from studybot.quiz import MASK, build_quiz, mask_term, save_quiz_result
from studybot.scoring import save_mock_exam
from studybot.sg_features import add_sg_mistake, record_sg_mistake_attempt
from studybot.sg_glossary import GlossaryEntry, get_sg_glossary_ratings, glossary_entry_key
from studybot.speed import (
    format_speed,
    parse_minutes_input,
    save_practice_minutes,
    speed_summary,
)
from studybot.time_of_day import best_slot, slot_of, time_of_day_stats

from test_round3 import PNG, _Channel, _Context, _TempDBCase, _interaction


TODAY = date(2026, 10, 15)   # 木曜
PNG_HEADER = PNG


def _real_today():
    return datetime.now(config.JST).date()


def _entries():
    return [
        GlossaryEntry(term=f"用語{index}", meaning=f"用語{index}は説明{index}です。",
                      category="セキュリティ" if index < 6 else "法務")
        for index in range(10)
    ]


class _Guild:
    def __init__(self):
        self.id = 1
        self.log = _Channel(20)
        self.log.name = "勉強ログ"
        self.log.guild = self
        self.glossary = _Channel(40)
        self.glossary.name = "SG用語集"
        self.glossary.guild = self
        self.voice_channels = []
        self.text_channels = [self.log, self.glossary]

    def get_channel(self, channel_id):
        return None


# ------------------------------------------------------------
# 案1 用語の4択ミニテスト
# ------------------------------------------------------------

class QuizTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def test_build_quiz(self):
        self.assertEqual(mask_term("DLPとは dlp の略", "DLP"), f"{MASK}とは {MASK} の略")
        questions = build_quiz(_entries(), rng=random.Random(1))
        self.assertEqual(len(questions), 5)
        self.assertEqual(len({q.answer for q in questions}), 5)
        for question in questions:
            self.assertEqual(len(set(question.choices)), 4)
            self.assertIn(question.answer, question.choices)
            self.assertNotIn(question.answer, question.prompt)
        self.assertEqual(build_quiz(_entries()[:3]), [])

        # 「要復習」の用語が優先して出る
        weak = _entries()[9]
        ratings = {glossary_entry_key(weak): "まだ要復習"}
        hits = sum(
            any(q.answer == weak.term for q in build_quiz(_entries(), ratings, rng=random.Random(seed)))
            for seed in range(30)
        )
        self.assertGreater(hits, 15)

    async def test_view_flow(self):
        questions = build_quiz(_entries(), rng=random.Random(2))
        view = quiz_feature.QuizView(7, questions)
        self.assertEqual(len(view.children), 4)
        self.assertIn("この意味の用語は？", view.embed().description)

        answer = questions[0].answer
        wrong = next(term for term in questions[0].choices if term != answer)
        interaction = _interaction()
        await view.answer(interaction, wrong)
        edit = interaction.response.edits[0]
        self.assertIn(f"正解は **{answer}**", edit["embed"].fields[0].value)
        self.assertEqual(len(view.children), 5)   # 4択＋次へ
        ratings = get_sg_glossary_ratings(self.db_path, 7, [questions[0].entry])
        self.assertEqual(list(ratings.values()), ["まだ要復習"])

        for question in questions[1:]:
            await view.next_question(_interaction())
            await view.answer(_interaction(), question.answer)
        finish = _interaction()
        with patch.object(quiz_feature, "announce_new_badges") as announce:
            announce.return_value = None

            async def fake(*args):
                return []

            announce.side_effect = fake
            await view.next_question(finish)
        result = finish.response.edits[0]["embed"]
        self.assertIn("4/5問正解", result.description)
        self.assertIn(answer, result.description)
        self.assertEqual(len(view.children), 1)   # もう5問

    async def test_command(self):
        ctx = _Context()
        with patch.object(quiz_feature, "load_glossary", lambda path: _entries()):
            await quiz_feature.quiz.callback(ctx, category="システム監査")
        self.assertIn("4択の問題を作れません", ctx.messages[0]["content"])
        with patch.object(quiz_feature, "load_glossary", lambda path: _entries()):
            await quiz_feature.quiz.callback(ctx)
        self.assertIsInstance(ctx.messages[1]["view"], quiz_feature.QuizView)


# ------------------------------------------------------------
# 案2 解く速さ
# ------------------------------------------------------------

class SpeedTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def test_parse_and_format(self):
        self.assertIsNone(parse_minutes_input(" "))
        self.assertEqual(parse_minutes_input("３０分"), 30)
        with self.assertRaises(ValueError):
            parse_minutes_input("0")
        save_practice_minutes(self.db_path, 1, 7, "SG", "A", 20, 36, TODAY)
        save_practice_minutes(self.db_path, 2, 7, "SG", "A", 20, 30, TODAY)
        save_practice_minutes(self.db_path, 3, 7, "SG", "B", 3, 12, TODAY)
        save_practice_minutes(self.db_path, 4, 7, "SG", "A", 20, 99, TODAY - timedelta(days=40))
        summary = speed_summary(self.db_path, 7, "SG", TODAY)
        self.assertEqual(format_speed(summary, "SG").splitlines(), [
            "科目A 1問 1.6分（40問・66分・目安 1.5分・少し遅い）",
            "科目B 1問 4.0分（3問・12分・目安 4.0分・目安内）",
        ])
        save_practice_minutes(self.db_path, 5, 7, "医療情報技師", "A", 10, 30, TODAY)
        self.assertEqual(
            format_speed(speed_summary(self.db_path, 7, "医療情報技師", TODAY), "医療情報技師"),
            "1問あたり 1問 3.0分（10問・30分）",
        )

    async def test_log_form_saves_minutes(self):
        log_message = SimpleNamespace(id=900, created_at=datetime(2026, 10, 15, 21, 0, tzinfo=config.JST))

        async def edit(**kwargs):
            pass

        log_message.edit = edit
        channel = _Channel(20)

        async def send(content=None, **kwargs):
            return log_message

        channel.send = send
        modal = SGStudyLogModal("ネットワーク", channel, SG)
        modal.questions_input._value = "20"
        modal.score_input._value = "70"
        modal.minutes_input._value = "36"
        modal.notes_input._value = ""
        interaction = _interaction()
        interaction.guild = SimpleNamespace(id=1, text_channels=[], voice_channels=[],
                                            get_channel=lambda channel_id: None)
        interaction.user = SimpleNamespace(id=7, display_name="test")
        await modal.on_submit(interaction)
        self.assertIn("1問 1.8分", interaction.followup.sent[0]["content"])
        self.assertEqual(speed_summary(self.db_path, 7, "SG", TODAY)["A"][:2], (20, 36))

        modal.minutes_input._value = "abc"
        bad = _interaction()
        await modal.on_submit(bad)
        self.assertIn("かかった時間", bad.response.sent[0]["content"])

    async def test_progress_shows_speed(self):
        save_practice_minutes(self.db_path, 1, 7, "SG", "A", 20, 30, _real_today())
        ctx = _Context()
        await qualification_feature.show_progress(ctx, SG)
        names = [field.name for field in ctx.messages[0]["embed"].fields]
        self.assertIn("解く速さ（直近28日）", names)


# ------------------------------------------------------------
# 案3 ペース
# ------------------------------------------------------------

class PaceTests(_TempDBCase):
    def test_lines(self):
        self.save_log(build_structured_sg_analysis("ネットワーク", 28, 50.0), "2026-10-10")
        self.assertEqual(daily_question_average(self.db_path, 7, "SG", TODAY), 2.0)
        lines = build_pace_lines(self.db_path, 7, "SG", TODAY, TODAY + timedelta(days=12))
        self.assertEqual(lines[0], "📊 直近2週間は1日 2問 → 試験日までに約24問")
        self.assertTrue(lines[1].startswith("⚠️ まだ解いていない分野が13つ（"))
        self.assertTrue(lines[1].endswith("1日2分野ずつ進める必要があります"))
        self.assertEqual(build_pace_lines(self.db_path, 7, "SG", TODAY, None), [])
        self.assertEqual(build_pace_lines(self.db_path, 7, "SG", TODAY, TODAY), [])

        iryo = build_pace_lines(self.db_path, 7, IRYO.code, TODAY, TODAY + timedelta(days=30))
        self.assertIn("直近2週間の問題の記録がありません", iryo[0])
        self.assertIn("10日に1分野ずつ", iryo[1])

    def test_digest_shows_pace(self):
        from studybot.exam_schedule import save_exam_date
        today = _real_today()
        save_exam_date(self.db_path, 7, "SG", today + timedelta(days=20), "t")
        lines = digest_feature.digest_extra_lines(7, SG, today)
        self.assertTrue(lines[0].startswith("📊"))


# ------------------------------------------------------------
# 案4 実績バッジ
# ------------------------------------------------------------

class BadgeTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def test_earn_once(self):
        self.assertEqual(check_new_badges(self.db_path, 7, TODAY), [])
        for day in range(7):
            self.save_log(build_structured_sg_analysis("ネットワーク", 20, 50.0),
                          (TODAY - timedelta(days=day)).isoformat())
        save_mock_exam(self.db_path, 7, SG, TODAY, [(40, 48), (10, 12)], None)
        save_quiz_result(self.db_path, 7, TODAY, None, 5, 5)
        new = {badge.id for badge in check_new_badges(self.db_path, 7, TODAY)}
        self.assertEqual(new, {"streak_7", "questions_100", "mock_pass", "quiz_perfect"})
        self.assertEqual(check_new_badges(self.db_path, 7, TODAY), [])
        self.assertEqual(get_earned_badges(self.db_path, 7)["streak_7"], TODAY.isoformat())

    async def test_announce_and_list(self):
        guild = _Guild()
        save_quiz_result(self.db_path, 7, TODAY, None, 5, 5)
        new = await badges_feature.announce_new_badges(guild, 7, TODAY)
        self.assertEqual([badge.id for badge in new], ["quiz_perfect"])
        self.assertIn("ミニテスト全問正解", guild.log.sent[0]["embed"].description)
        self.assertEqual(await badges_feature.announce_new_badges(guild, 7, TODAY), [])
        self.assertEqual(await badges_feature.announce_new_badges(None, 7, TODAY), [])

        embed = badges_feature.build_badges_embed(7, TODAY)
        self.assertEqual(embed.title, f"実績バッジ 1/{len(BADGES)}")
        self.assertIn("1週間つづいた　あと **7**日連続", embed.fields[0].value)

    async def test_hooks_after_review(self):
        from studybot.features import review as review_feature
        mistake_id, _ = add_sg_mistake(self.db_path, 7, "ネットワーク", "q", "r", today=TODAY)
        for offset in (1, 4):
            record_sg_mistake_attempt(self.db_path, 7, mistake_id, "correct",
                                      today=TODAY + timedelta(days=offset))
        calls = []

        async def fake(guild, user_id, today=None):
            calls.append(user_id)
            return []

        items = [dict(id=mistake_id, category="ネットワーク", question_ref="q",
                      reason="r", memo=None, success_streak=2, qualification="SG",
                      image_path=None, reason_kind=None, next_review_on="2026-10-26")]
        _, view = review_feature.build_review_session(7, TODAY + timedelta(days=11), items)
        with patch.object(review_feature, "announce_new_badges", fake):
            await view.correct_button.callback(_interaction())
        self.assertEqual(calls, [7])


# ------------------------------------------------------------
# 案5 ひとこと日記
# ------------------------------------------------------------

class NoteTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def test_store(self):
        self.assertEqual(save_daily_note(self.db_path, 7, TODAY, "  科目Bの\n時間配分 "),
                         "科目Bの 時間配分")
        save_daily_note(self.db_path, 7, TODAY - timedelta(days=2), "最初")
        save_daily_note(self.db_path, 7, TODAY, "上書き")
        notes = get_daily_notes(self.db_path, 7, TODAY - timedelta(days=6), TODAY)
        self.assertEqual(format_daily_notes(notes), "- 火 10/13：最初\n- 木 10/15：上書き")
        self.assertIsNone(save_daily_note(self.db_path, 7, TODAY, ""))
        with self.assertRaises(ValueError):
            save_daily_note(self.db_path, 7, TODAY, "あ" * 201)

    async def test_modal_command_and_reports(self):
        today = _real_today()
        interaction = _interaction()
        await notes_feature.open_note_modal(interaction)
        modal = interaction.response.modals[0]
        modal.note_input._value = "朝のほうが集中できた"
        submit = _interaction()
        await modal.on_submit(submit)
        self.assertIn("朝のほうが集中できた", submit.response.sent[0]["content"])

        again = _interaction()
        await notes_feature.open_note_modal(again)
        self.assertEqual(again.response.modals[0].note_input.default, "朝のほうが集中できた")

        ctx = _Context()
        await notes_feature.note.callback(ctx, text="上書きした")
        self.assertIn("上書きした", ctx.messages[0]["content"])

        self.save_log(build_structured_sg_analysis("ネットワーク", 20, 50.0), today.isoformat())
        data = ai_feature.collect_weekly_report(7, "SG", today)
        self.assertIn("上書きした", data["notes_text"])
        self.assertIn("【今週のひとこと（本人が書いたメモ）】", ai_feature.build_weekly_report_prompt(data))
        embed = ai_feature.build_weekly_report_embed(data, "本文")
        self.assertEqual(embed.fields[-1].name, "今週のひとこと")
        blocks = notion_export.build_page_children(data, "本文", None, [])
        headings = [block["heading_2"]["rich_text"][0]["text"]["content"]
                    for block in blocks if block["type"] == "heading_2"]
        self.assertIn("今週のひとこと", headings)

    def test_vc_button(self):
        from studybot.voice import VCActionView
        ids = [item.custom_id for item in VCActionView().children]
        self.assertIn("studybot:vc:note", ids)


# ------------------------------------------------------------
# 案6 時間帯ごとの分析
# ------------------------------------------------------------

class TimeOfDayTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def test_stats(self):
        self.assertEqual([slot_of(hour) for hour in (5, 10, 11, 17, 23, 0, 4)],
                         ["朝", "朝", "昼", "夜", "夜", "深夜", "深夜"])
        morning = datetime(2026, 10, 14, 7, 0, tzinfo=config.JST)
        night = datetime(2026, 10, 14, 21, 0, tzinfo=config.JST)
        save_completed_study_session(1, 7, "t", morning, morning + timedelta(hours=1), 3600)
        save_completed_study_session(1, 7, "t", night, night + timedelta(hours=2), 7200)
        self.save_log(build_structured_sg_analysis("ネットワーク", 20, 80.0), "2026-10-14")
        stats = time_of_day_stats(self.db_path, 7, TODAY - timedelta(days=7), TODAY)
        self.assertEqual(stats["朝"]["minutes"], 60)
        self.assertEqual(stats["夜"]["minutes"], 120)
        # 記録の時刻（10:00）で時間帯を決める
        self.assertEqual(stats["朝"]["score"], 80.0)
        self.assertIsNone(best_slot(stats))
        png = render_time_of_day_chart(stats, "時間帯", "説明")
        self.assertTrue(png.startswith(PNG_HEADER))

    async def test_command(self):
        ctx = _Context()
        await time_feature.time_chart.callback(ctx, days=28, view="slots")
        self.assertIn("記録がありません", ctx.messages[0]["content"])
        start = datetime.now(config.JST) - timedelta(hours=1)
        save_completed_study_session(1, 7, "t", start, start + timedelta(minutes=30), 1800)
        await time_feature.time_chart.callback(ctx, days=28, view="slots")
        self.assertEqual(ctx.messages[1]["file"].filename, "time_of_day.png")


# ------------------------------------------------------------
# 案7 今日の1語
# ------------------------------------------------------------

class DailyWordTests(_TempDBCase, unittest.IsolatedAsyncioTestCase):
    def test_pick(self):
        entries = _entries()
        weak = entries[4]
        ratings = {glossary_entry_key(weak): "できなかった"}
        self.assertEqual(pick_word(entries, ratings, TODAY), weak)
        self.assertNotEqual(pick_word(entries, ratings, TODAY, {weak.term}), weak)
        self.assertEqual(pick_word(entries, {}, TODAY), pick_word(entries, {}, TODAY))
        self.assertIsNone(pick_word([GlossaryEntry(term="x")], {}, TODAY))

    async def test_post_and_reveal(self):
        guild = _Guild()
        message = SimpleNamespace(id=555)

        async def send(content=None, **kwargs):
            guild.glossary.sent.append(dict(kwargs, content=content))
            return message

        guild.glossary.send = send
        bot = SimpleNamespace(guilds=[guild])
        now = datetime(2026, 10, 15, 7, 30, tzinfo=config.JST)
        with patch.object(word_feature, "_load_entries", _entries):
            self.assertEqual(await word_feature.post_daily_words(bot, now), 1)
            self.assertEqual(await word_feature.post_daily_words(bot, now), 0)
            self.assertTrue(posted_today(self.db_path, TODAY, 1))
            sent = guild.glossary.sent[0]
            self.assertTrue(sent["embed"].title.startswith("今日の1語（10/15）"))
            key = word_key_for_message(self.db_path, 555)

            interaction = _interaction()
            interaction.message = message
            await word_feature.DailyWordView().show_button.callback(interaction)
        reply = interaction.response.sent[0]
        self.assertTrue(reply["ephemeral"])
        entry = reply["view"].entry
        self.assertEqual(glossary_entry_key(entry), key)

        rate = _interaction()
        await reply["view"].children[2].callback(rate)
        self.assertIn("要復習", rate.response.edits[0]["content"])
        self.assertEqual(
            list(get_sg_glossary_ratings(self.db_path, 7, [entry]).values()), ["まだ要復習"]
        )

    async def test_only_while_learning_sg(self):
        from studybot.exam_results import advance_roadmap
        advance_roadmap(self.db_path, "SG")
        with patch.object(word_feature, "_load_entries", _entries):
            self.assertEqual(await word_feature.post_daily_words(SimpleNamespace(guilds=[_Guild()])), 0)
        self.assertTrue(word_feature.is_daily_word_due(datetime(2026, 10, 15, 8, 0)))
        self.assertFalse(word_feature.is_daily_word_due(datetime(2026, 10, 15, 7, 0)))


if __name__ == "__main__":
    unittest.main()
