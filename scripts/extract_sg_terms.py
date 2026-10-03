"""Extract SG syllabus term examples from the IPA Ver. 4.1 PDF.

The output contains term names only. It does not use sg-siken.com content or
claim to reproduce that site's curated 911-entry glossary.

Requires pdfplumber. Run from the repository root:

    python scripts/extract_sg_terms.py

Pass --pdf PATH to use a previously downloaded copy of the official PDF.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber


SOURCE_URL = (
    "https://www.ipa.go.jp/shiken/syllabus/"
    "nl10bi0000007tch-att/syllabus_sg_ver4_1.pdf"
)
SOURCE_PAGE = "https://www.ipa.go.jp/shiken/syllabus/gaiyou.html"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "sg_glossary.json"


@dataclass(frozen=True)
class TableSpec:
    page: int  # zero-based PDF page
    table: int
    category: str
    initial_subcategory: str = ""
    overrides: dict[int, str] = field(default_factory=dict)


# Only the "required knowledge" tables contain the syllabus's term examples.
# The later "required skills" chapter is intentionally excluded.
TABLES = [
    TableSpec(3, 0, "セキュリティ", "情報セキュリティ"),
    TableSpec(4, 0, "セキュリティ", "情報セキュリティ", {7: "情報セキュリティ管理"}),
    TableSpec(5, 0, "セキュリティ", "情報セキュリティ管理", {8: "セキュリティ技術評価"}),
    TableSpec(6, 0, "セキュリティ", "セキュリティ技術評価", {2: "情報セキュリティ対策", 5: "セキュリティ実装技術"}),
    TableSpec(7, 0, "セキュリティ", "セキュリティ実装技術"),
    TableSpec(7, 1, "法務", "知的財産権", {11: "その他の法律・ガイドライン・技術者倫理"}),
    TableSpec(8, 0, "法務", "標準化関連"),
    TableSpec(9, 0, "システム構成要素", "システムの構成"),
    TableSpec(9, 1, "データベース", "データベース方式"),
    TableSpec(9, 2, "ネットワーク", "ネットワーク方式"),
    TableSpec(10, 0, "ネットワーク", "データ通信と制御"),
    TableSpec(10, 1, "プロジェクトマネジメント", "プロジェクトマネジメント", {4: "プロジェクトのスコープ"}),
    TableSpec(11, 0, "プロジェクトマネジメント", "プロジェクトの資源"),
    TableSpec(11, 1, "サービスマネジメント", "サービスマネジメント"),
    TableSpec(12, 0, "サービスマネジメント", "パフォーマンス評価及び改善"),
    TableSpec(12, 1, "システム監査", "システム監査"),
    TableSpec(12, 2, "システム戦略", "情報システム戦略"),
    TableSpec(13, 0, "システム企画", "システム化計画"),
    TableSpec(13, 1, "企業活動", "経営・組織論"),
    TableSpec(14, 0, "企業活動", "業務分析・データ利活用"),
]


def clean_label(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def clean_examples(value: str) -> str:
    # Bullet starts are independent examples. Other line breaks are wraps.
    value = re.sub(r"(^|\n)\s*・", "，", value or "")
    value = re.sub(r"〔[^〕]+〕", "，", value)
    # Preserve commas at page boundaries before merging split table cells.
    return re.sub(r"\s+", "", value)


def split_top_level(value: str) -> list[str]:
    items: list[str] = []
    start = 0
    depth = 0
    for pos, char in enumerate(value):
        if char in "（(":
            depth += 1
        elif char in "）)":
            depth = max(0, depth - 1)
        elif char in "，、" and depth == 0:
            items.append(value[start:pos])
            start = pos + 1
    items.append(value[start:])
    return [item.strip(" ・，、") for item in items if item.strip(" ・，、")]


def strip_explanation(value: str) -> str:
    # Parentheses in the syllabus mostly hold translations, aliases or examples.
    # Preserve them in syllabus_context and use the displayed base term here.
    out: list[str] = []
    depth = 0
    for char in value:
        if char in "（(":
            depth += 1
        elif char in "）)":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    term = "".join(out).strip(" ・，、")
    term = re.sub(r"などの関連機構の役割$", "", term)
    return re.sub(r"(?:ほか|など)$", "", term).strip(" ・，、")


def extract(pdf_path: Path) -> dict:
    rows: list[dict] = []
    with pdfplumber.open(pdf_path) as pdf:
        for spec in TABLES:
            # Omit tiny ruby pronunciation characters inserted above kanji.
            page = pdf.pages[spec.page].filter(
                lambda obj: obj.get("object_type") != "char" or obj["size"] >= 6.5
            )
            table = page.extract_tables()[spec.table]
            subcategory = spec.initial_subcategory
            for row_number, row in enumerate(table[1:], start=1):
                if len(row) < 8:
                    continue
                item = clean_label(row[4] or "")
                examples = clean_examples(row[7] or "")
                if not examples:
                    continue

                if row_number in spec.overrides:
                    subcategory = spec.overrides[row_number]
                elif row[1]:
                    subcategory = clean_label(row[1])

                # Four page breaks split a single cell/word. Merge before
                # tokenizing so that e.g. ペ + ネトレーションテスト stays intact.
                continuation = not item or (spec.page, spec.table, row_number) in {
                    (5, 0, 1),  # リスク対応）
                    (6, 0, 1),  # セキュリティ技術評価
                }
                if continuation and rows and rows[-1]["category"] == spec.category:
                    rows[-1]["examples"] += examples
                    continue
                rows.append(
                    {
                        "category": spec.category,
                        "subcategory": subcategory,
                        "item": item,
                        "examples": examples,
                        "pdf_page": spec.page + 1,
                    }
                )

    entries: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        for chunk in split_top_level(row["examples"]):
            term = strip_explanation(chunk)
            if not term or term in {"ほか", "など"}:
                continue
            key = (term, row["category"], row["subcategory"])
            if key in seen:
                continue
            seen.add(key)
            entries.append(
                {
                    "term": term,
                    "meaning": "",
                    "category": row["category"],
                    "subcategory": row["subcategory"],
                    "syllabus_context": row["item"],
                    "source_url": SOURCE_URL,
                    "source_page": row["pdf_page"],
                }
            )

    return {
        "metadata": {
            "title": "情報セキュリティマネジメント試験 シラバス Ver.4.1 用語例",
            "source": "独立行政法人情報処理推進機構（IPA）",
            "source_url": SOURCE_URL,
            "source_page_url": SOURCE_PAGE,
            "version": "4.1",
            "source_published": "2025-04-17",
            "extraction_note": (
                "IPA公式シラバスの『要求される知識』章の用語例から独立抽出。"
                "括弧内の別名・例示は用語名に含めず、同一小分類内の同名は1件に統合。"
                "sg-siken.comの911語の選定・説明文とは一致を保証しない。"
            ),
            "reuse_note": (
                "出典: 独立行政法人情報処理推進機構（IPA）『情報セキュリティマネジメント試験 "
                "シラバス Ver.4.1』。IPAの転載・引用条件に従い出典を表示すること。"
            ),
        },
        "entries": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, help="Local copy of the official PDF")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--force", action="store_true", help="Replace an existing output file")
    args = parser.parse_args()

    if args.output.exists() and not args.force:
        parser.error(f"{args.output} already exists; pass --force to replace it")

    if args.pdf:
        result = extract(args.pdf)
    else:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sg_syllabus_ver4_1.pdf"
            urllib.request.urlretrieve(SOURCE_URL, source)
            result = extract(source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(result['entries'])} IPA syllabus term placements to {args.output}")


if __name__ == "__main__":
    main()
