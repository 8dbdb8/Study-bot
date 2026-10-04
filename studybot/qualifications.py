"""資格ごとの分野・科目B・コマンド名などの決まりごと。

資格を増やすときは、ここに Qualification を1つ足して QUALIFICATIONS に
並べる。コマンド（/sg・/fe など）や入力画面、進捗の表示は、ここに書いた
内容から組み立てる。
"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Qualification:
    # ロードマップやDBで使う短い名前（例：SG）
    code: str
    # スラッシュコマンドのグループ名（例：sg → /sg log）
    command: str
    display_name: str
    # 過去問の出どころ（AIへの指示や記録の文面に使う）
    practice_source: str
    # 選べる分野と、その分野が属する大分類。選択肢に出る順番どおりに並べる
    categories: tuple
    # 進捗表示での見出しと、その下に並べる分野
    progress_groups: tuple
    # 科目Bのテーマ。科目Bがない資格は空
    b_topics: tuple = ()
    # 分野の別名（AIや自由入力の揺れを正しい分野名にそろえる）
    category_aliases: dict = field(default_factory=dict)
    # 過去の記録との互換のために受け付ける分野（選択肢には出さない）
    extra_categories: tuple = ()
    # 本番の形式：((区分, 問題数), ...)。問題数が分からない区分は None
    exam_parts: tuple = ()
    # "combined"＝合計で採点（SG）、"separate"＝区分ごとに合格点が必要（FE）
    scoring: str = "combined"
    # 1000点満点での合格点。分からなければ None（予想得点の目安を出さない）
    pass_score: int | None = 600
    # 本番の試験時間（分）
    exam_minutes: int | None = None

    @property
    def label(self):
        """試験の呼び名（例：SG試験）。"""
        return f"{self.code}試験"

    @property
    def category_names(self):
        return tuple(name for name, _ in self.categories)

    @property
    def category_to_major(self):
        return dict(self.extra_categories + self.categories)

    @property
    def majors(self):
        seen = []
        for _, major in self.categories:
            if major not in seen:
                seen.append(major)
        return tuple(seen)

    @property
    def has_part_b(self):
        return bool(self.b_topics)

    @property
    def section_a_title(self):
        """進捗の見出し（例：SG 科目A・14分野の進捗）。"""
        part = "科目A・" if self.has_part_b else ""
        return f"{self.code} {part}{len(self.categories)}分野の進捗"


SG = Qualification(
    code="SG",
    command="sg",
    display_name="情報セキュリティマネジメント（SG）",
    practice_source="SG過去問道場",
    categories=(
        ("情報セキュリティ", "テクノロジ系"),
        ("情報セキュリティ管理", "テクノロジ系"),
        ("セキュリティ技術評価", "テクノロジ系"),
        ("情報セキュリティ対策", "テクノロジ系"),
        ("セキュリティ実装技術", "テクノロジ系"),
        ("システム構成要素", "テクノロジ系"),
        ("データベース", "テクノロジ系"),
        ("ネットワーク", "テクノロジ系"),
        ("プロジェクトマネジメント", "マネジメント系"),
        ("サービスマネジメント", "マネジメント系"),
        ("システム監査", "マネジメント系"),
        ("システム戦略", "ストラテジ系"),
        ("システム企画", "ストラテジ系"),
        ("企業活動", "ストラテジ系"),
    ),
    progress_groups=(
        ("セキュリティ", (
            "情報セキュリティ", "情報セキュリティ管理", "セキュリティ技術評価",
            "情報セキュリティ対策", "セキュリティ実装技術",
        )),
        ("その他分野", (
            "システム構成要素", "データベース", "ネットワーク",
            "プロジェクトマネジメント", "サービスマネジメント", "システム監査",
            "システム戦略", "システム企画", "企業活動",
        )),
    ),
    b_topics=(
        "情報資産管理",
        "リスクアセスメント",
        "IT利用のセキュリティ",
        "委託先管理",
        "教育・訓練",
        "その他のケース",
    ),
    category_aliases={
        "情報セキュリティ全般": "情報セキュリティ",
        "セキュリティ管理": "情報セキュリティ管理",
        "セキュリティ対策": "情報セキュリティ対策",
        "セキュリティ実装": "セキュリティ実装技術",
        "法律": "法務",
    },
    # 以前の記録で使っていた分野名
    extra_categories=(
        ("セキュリティ", "テクノロジ系"),
        ("法務", "ストラテジ系"),
    ),
    # 科目A・Bをまとめて120分、総合評価点600点以上で合格
    exam_parts=(("科目A", 48), ("科目B", 12)),
    scoring="combined",
    exam_minutes=120,
)

FE_TECHNOLOGY = (
    "基礎理論", "アルゴリズムとプログラミング", "コンピュータ構成要素",
    "システム構成要素", "ソフトウェア", "ハードウェア",
    "ユーザーインタフェース", "情報メディア", "データベース", "ネットワーク",
    "セキュリティ", "システム開発技術", "ソフトウェア開発管理技術",
)
FE_MANAGEMENT = ("プロジェクトマネジメント", "サービスマネジメント", "システム監査")
FE_STRATEGY = (
    "システム戦略", "システム企画", "経営・組織論", "技術戦略マネジメント",
    "ビジネスインダストリ", "企業活動", "法務",
)

FE = Qualification(
    code="FE",
    command="fe",
    display_name="基本情報技術者（FE）",
    practice_source="FE過去問道場",
    categories=(
        tuple((name, "テクノロジ系") for name in FE_TECHNOLOGY)
        + tuple((name, "マネジメント系") for name in FE_MANAGEMENT)
        + tuple((name, "ストラテジ系") for name in FE_STRATEGY)
    ),
    progress_groups=(
        ("テクノロジ系", FE_TECHNOLOGY),
        ("マネジメント系", FE_MANAGEMENT),
        ("ストラテジ系", FE_STRATEGY),
    ),
    b_topics=("アルゴリズムとプログラミング", "情報セキュリティ"),
    category_aliases={
        "アルゴリズム": "アルゴリズムとプログラミング",
        "プログラミング": "アルゴリズムとプログラミング",
        "情報セキュリティ": "セキュリティ",
        "ユーザインタフェース": "ユーザーインタフェース",
        "経営組織論": "経営・組織論",
        "法律": "法務",
    },
    # 科目A（90分）と科目B（100分）のそれぞれで600点以上が必要
    exam_parts=(("科目A", 60), ("科目B", 20)),
    scoring="separate",
    exam_minutes=190,
)

IRYO_FIELDS = ("情報処理技術系", "医学・医療系", "医療情報システム系")

IRYO = Qualification(
    code="医療情報技師",
    command="iryo",
    display_name="医療情報技師",
    practice_source="過去問・問題集",
    categories=tuple((name, name) for name in IRYO_FIELDS),
    progress_groups=(("分野", IRYO_FIELDS),),
    category_aliases={
        "情報処理技術": "情報処理技術系",
        "医学・医療": "医学・医療系",
        "医学医療系": "医学・医療系",
        "医療情報システム": "医療情報システム系",
    },
    # 問題数や合格点はここでは決めず、模試の記録で入力してもらう
    exam_parts=(("全体", None),),
    pass_score=None,
)

QUALIFICATIONS = (SG, FE, IRYO)
BY_CODE = {qualification.code: qualification for qualification in QUALIFICATIONS}


def get_qualification(code):
    """資格の短い名前から Qualification。知らない名前なら None。"""
    return BY_CODE.get(code)


def current_qualification(db_path):
    """ロードマップで「学習中」の資格。決まっていなければ SG。"""
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute("""
                SELECT qualification FROM certification_roadmap
                WHERE is_current = 1
                ORDER BY sort_order
                LIMIT 1
            """).fetchone()
    except sqlite3.Error:
        row = None
    return BY_CODE.get(row[0], SG) if row else SG
