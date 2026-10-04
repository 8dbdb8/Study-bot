"""SG用語の4択ミニテスト：問題の作り方と結果の記録。

用語集の「意味」を見せて、正しい用語を4つから選ぶ。
自己評価が「要復習」「できなかった」「微妙」や未評価の用語を優先して出す。
"""

import random
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from studybot.sg_glossary import glossary_entry_key


QUIZ_SIZE = 5
CHOICE_COUNT = 4
MASK = "〇〇"

# 出やすさ（自己評価ごと）。覚えていない用語ほど出やすい
RATING_WEIGHTS = {
    "まだ要復習": 4, "できなかった": 4, "微妙": 3, None: 2, "できた": 1,
}


@dataclass(frozen=True)
class QuizQuestion:
    entry: object          # 正解の GlossaryEntry
    prompt: str            # 用語を伏せた意味
    choices: tuple         # 4つの用語（正解を含む）

    @property
    def answer(self):
        return self.entry.term


def init_quiz_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quiz_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            taken_on TEXT NOT NULL,
            category TEXT,
            correct INTEGER NOT NULL,
            total INTEGER NOT NULL
        )
    """)


def mask_term(meaning, term):
    """意味の中に出てくる用語（答え）を〇〇で隠す。"""
    return re.sub(re.escape(term), MASK, meaning, flags=re.IGNORECASE)


def _weighted_sample(entries, weights, count, rng):
    pool = list(zip(entries, weights))
    chosen = []
    while pool and len(chosen) < count:
        total = sum(weight for _, weight in pool)
        point = rng.uniform(0, total)
        for index, (entry, weight) in enumerate(pool):
            point -= weight
            if point <= 0:
                break
        chosen.append(pool.pop(index)[0])
    return chosen


def build_quiz(entries, ratings=None, size=QUIZ_SIZE, rng=None):
    """entries（出題する範囲）から問題を作る。

    ratings は {entry_key: 自己評価}。意味が空の用語や、
    選択肢を4つ作れない範囲の用語は出さない。
    """
    rng = rng or random.Random()
    ratings = ratings or {}
    usable = [entry for entry in entries if entry.meaning]
    terms = sorted({entry.term for entry in usable})
    if len(terms) < CHOICE_COUNT:
        return []

    weights = [
        RATING_WEIGHTS.get(ratings.get(glossary_entry_key(entry)), 2)
        for entry in usable
    ]
    questions = []
    used_terms = set()
    for entry in _weighted_sample(usable, weights, len(usable), rng):
        if entry.term in used_terms:
            continue
        used_terms.add(entry.term)
        # 不正解の選択肢は、なるべく同じ分野の用語から選ぶ
        same = [
            term for term in {e.term for e in usable if e.category == entry.category}
            if term != entry.term
        ]
        others = [term for term in terms if term != entry.term and term not in same]
        rng.shuffle(same)
        rng.shuffle(others)
        wrong = (sorted(same, key=lambda _: rng.random()) + others)[:CHOICE_COUNT - 1]
        choices = [entry.term, *wrong]
        rng.shuffle(choices)
        questions.append(QuizQuestion(
            entry, mask_term(entry.meaning, entry.term), tuple(choices)
        ))
        if len(questions) >= size:
            break
    return questions


def save_quiz_result(db_path, user_id, taken_on, category, correct, total):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT INTO quiz_results (
                    user_id, taken_on, category, correct, total
                ) VALUES (?, ?, ?, ?, ?)
            """, (user_id, taken_on.isoformat(), category, correct, total))
