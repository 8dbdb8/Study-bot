import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from studybot import config
from studybot import database as database_module
from studybot.features import ai as ai_feature
from studybot.features import notion_export
from studybot.notion import (
    TEXT_LIMIT,
    NotionError,
    extract_notion_id,
    markdown_to_blocks,
    rich_text,
)
from studybot.notion_store import get_notion_database, get_weekly_page


PAGE_ID = "0123456789abcdef0123456789abcdef"
PAGE_UUID = "01234567-89ab-cdef-0123-456789abcdef"
TODAY = date(2026, 10, 18)
DATA = {
    "start_date": "2026-10-12", "end_date": "2026-10-18",
    "total_seconds": 2 * 3600 + 30 * 60, "daily_text": "",
    "log_count": 2, "total_questions": 40, "score_text": "72.5%",
    "category_text": "", "review_text": "",
}


class HelperTests(unittest.TestCase):
    def test_extract_notion_id(self):
        self.assertEqual(
            extract_notion_id(
                f"https://www.notion.so/akash/StudyBot-{PAGE_ID}?pvs=4"
            ),
            PAGE_UUID,
        )
        self.assertEqual(extract_notion_id(PAGE_UUID), PAGE_UUID)
        self.assertEqual(extract_notion_id(PAGE_ID.upper()), PAGE_UUID)
        self.assertIsNone(extract_notion_id("https://www.notion.so/akash"))
        self.assertIsNone(extract_notion_id(""))

    def test_rich_text_bold_and_split(self):
        self.assertEqual(rich_text("前**太字**後"), [
            {"type": "text", "text": {"content": "前"}},
            {"type": "text", "text": {"content": "太字"},
             "annotations": {"bold": True}},
            {"type": "text", "text": {"content": "後"}},
        ])
        parts = rich_text("あ" * (TEXT_LIMIT + 5))
        self.assertEqual([len(p["text"]["content"]) for p in parts], [TEXT_LIMIT, 5])

    def test_markdown_to_blocks(self):
        blocks = markdown_to_blocks(
            "### 今週の実績\n\n- 30問解いた\n* **ネットワーク** を復習\n"
            "1. 科目Bを1セット\n---\n全体として順調です。"
        )
        self.assertEqual(
            [block["type"] for block in blocks],
            ["heading_3", "bulleted_list_item", "bulleted_list_item",
             "numbered_list_item", "paragraph"],
        )
        self.assertEqual(
            blocks[0]["heading_3"]["rich_text"][0]["text"]["content"],
            "今週の実績",
        )
        self.assertEqual(
            blocks[2]["bulleted_list_item"]["rich_text"][0]["annotations"],
            {"bold": True},
        )

    def test_friendly_errors(self):
        self.assertIn("トークン", NotionError(401, "unauthorized", "").friendly())
        self.assertIn("接続", NotionError(404, "object_not_found", "").friendly())
        self.assertIn("500", NotionError(500, "internal", "").friendly())

    def test_page_properties_and_children(self):
        properties = notion_export.build_page_properties(DATA, date(2026, 10, 12))
        self.assertEqual(
            properties["週"]["title"][0]["text"]["content"], "10/12〜10/18"
        )
        self.assertEqual(properties["開始日"], {"date": {"start": "2026-10-12"}})
        self.assertEqual(properties["勉強時間（分）"], {"number": 150})
        self.assertEqual(properties["正答率（%）"], {"number": 72.5})
        self.assertEqual(
            notion_export.build_page_properties(
                dict(DATA, score_text="記録なし"), date(2026, 10, 12)
            )["正答率（%）"],
            {"number": None},
        )

        children = notion_export.build_page_children(
            DATA, None, "Ollamaに接続できません", ["img-1"]
        )
        types = [block["type"] for block in children]
        self.assertEqual(types[0], "callout")
        self.assertEqual(types.count("image"), 1)
        self.assertNotIn(
            "メモ（ChatGPT）",
            [b[b["type"]]["rich_text"][0]["text"]["content"]
             for b in children if b["type"] == "heading_2"],
        )
        self.assertIn(
            "（Ollamaに接続できません）",
            children[-1]["paragraph"]["rich_text"][0]["text"]["content"],
        )


class FakeNotion:
    """ページとブロックの並びをまねる、Notion API の代わり。"""

    def __init__(self):
        self.calls = []
        self.databases = 0
        self.pages = {}       # page_id -> [block_id, ...]
        self.blocks = {}      # block_id -> 表示用の文字
        self.properties = {}
        self.fail_create_page_once = False
        self.trashed_pages = set()
        self._next = 0

    def _new_id(self, prefix):
        self._next += 1
        return f"{prefix}-{self._next}"

    def _check_page(self, page_id):
        if page_id in self.trashed_pages:
            raise NotionError(400, "validation_error", "Can't edit block that is archived.")

    async def upload_png(self, png, filename):
        self.calls.append(("upload", filename))
        return f"file-{filename}"

    async def create_database(self, parent_page_id, title, properties):
        self.databases += 1
        self.calls.append(("database", parent_page_id, title))
        return f"db-{self.databases}", f"ds-{self.databases}"

    async def create_page(self, data_source_id, properties):
        if self.fail_create_page_once:
            self.fail_create_page_once = False
            raise NotionError(404, "object_not_found", "gone")
        page_id = self._new_id("page")
        self.calls.append(("page", data_source_id))
        self.pages[page_id] = []
        self.properties[page_id] = properties
        return page_id, f"https://notion.so/{page_id}"

    async def update_page_properties(self, page_id, properties):
        self._check_page(page_id)
        self.calls.append(("properties", page_id))
        self.properties[page_id] = properties

    async def append_children(self, block_id, children, position=None):
        self._check_page(block_id)
        ids = []
        for child in children:
            new_id = self._new_id("block")
            block_type = child["type"]
            rich = child[block_type].get("rich_text", [])
            label = rich[0]["text"]["content"] if rich else block_type
            self.blocks[new_id] = f"{block_type}:{label}"
            ids.append(new_id)
        order = self.pages[block_id]
        if position == "start":
            self.pages[block_id] = ids + order
        else:
            order.extend(ids)
        self.calls.append(("append", block_id, position, len(ids)))
        return ids

    async def list_children(self, block_id):
        return [{"id": child} for child in self.pages[block_id]]

    async def retrieve_block(self, block_id):
        if block_id not in self.blocks:
            raise NotionError(404, "object_not_found", "")
        return {"id": block_id}

    async def delete_block(self, block_id):
        self.calls.append(("delete", block_id))
        for order in self.pages.values():
            if block_id in order:
                order.remove(block_id)
                del self.blocks[block_id]
                return
        raise NotionError(404, "object_not_found", "")

    def write_by_hand(self, page_id, text):
        """ChatGPTや自分がページの末尾に書き足す。"""
        block_id = self._new_id("note")
        self.blocks[block_id] = f"paragraph:{text}"
        self.pages[page_id].append(block_id)
        return block_id

    def contents(self, page_id):
        return [self.blocks[block_id] for block_id in self.pages[page_id]]


DATA_NEXT_WEEK = dict(DATA, start_date="2026-10-19", end_date="2026-10-25")


class ExportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = str(Path(temp_dir.name) / "study.db")
        for name, value in (
            ("DB_PATH", self.db_path),
            ("NOTION_TOKEN", "secret_test"),
            ("NOTION_PAGE_ID", PAGE_UUID),
        ):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        database_module.init_db()
        self.week = date(2026, 10, 12)

    def add_session(self, study_date):
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("""
                    INSERT INTO study_sessions (
                        guild_id, user_id, username, study_date,
                        start_time, end_time, duration_seconds
                    ) VALUES (1, 1, 'test', ?, 's', 'e', 3600)
                """, (study_date,))

    async def export(self, notion, answer="### 今週\n- よくできた", data=DATA):
        return await notion_export.export_weekly_report(
            notion, 1, data, answer, None, TODAY
        )

    async def test_new_page_has_bot_section_then_memo_heading(self):
        self.add_session("2026-10-14")
        notion = FakeNotion()

        url = await self.export(notion)

        page_id = get_weekly_page(self.db_path, 1, self.week)["page_id"]
        self.assertEqual(url, f"https://notion.so/{page_id}")
        self.assertEqual(notion.calls[0], ("upload", "study_time.png"))
        self.assertIn(("database", PAGE_UUID, "StudyBot 週間レポート"), notion.calls)
        self.assertEqual(get_notion_database(self.db_path, PAGE_UUID), ("db-1", "ds-1"))
        contents = notion.contents(page_id)
        self.assertTrue(contents[0].startswith("callout:このページは StudyBot"))
        self.assertIn("image:image", contents)
        self.assertEqual(contents[-1], "heading_2:メモ（ChatGPT）")

    async def test_resave_keeps_notes_below_memo_heading(self):
        notion = FakeNotion()
        await self.export(notion, answer="最初の振り返り")
        page_id = get_weekly_page(self.db_path, 1, self.week)["page_id"]
        notion.write_by_hand(page_id, "ChatGPT：参考書を2章読んだ")
        notion.write_by_hand(page_id, "自分：ネットワークが苦手")

        url = await self.export(notion, answer="更新した振り返り")

        self.assertEqual(url, f"https://notion.so/{page_id}")  # 同じページのまま
        self.assertEqual(notion.databases, 1)
        self.assertIn(("properties", page_id), notion.calls)
        contents = notion.contents(page_id)
        self.assertNotIn("paragraph:最初の振り返り", contents)
        self.assertIn("paragraph:更新した振り返り", contents)
        self.assertEqual(contents[-3:], [
            "heading_2:メモ（ChatGPT）",
            "paragraph:ChatGPT：参考書を2章読んだ",
            "paragraph:自分：ネットワークが苦手",
        ])
        self.assertEqual(contents.count("heading_2:メモ（ChatGPT）"), 1)

    async def test_memo_heading_deleted_by_hand_is_added_again(self):
        notion = FakeNotion()
        await self.export(notion)
        record = get_weekly_page(self.db_path, 1, self.week)
        await notion.delete_block(record["memo_block_id"])

        await self.export(notion)

        contents = notion.contents(record["page_id"])
        self.assertEqual(contents[-1], "heading_2:メモ（ChatGPT）")
        self.assertEqual(contents.count("heading_2:メモ（ChatGPT）"), 1)

    async def test_page_from_old_version_is_rewritten_in_place(self):
        notion = FakeNotion()
        page_id, url = await notion.create_page("ds-old", {})
        await notion.append_children(page_id, [
            {"type": "paragraph", "paragraph": {"rich_text": rich_text("古い数字")}},
        ])
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("""
                    INSERT INTO notion_weekly_pages (user_id, week_start, page_id, url)
                    VALUES (1, '2026-10-12', ?, ?)
                """, (page_id, url))

        await self.export(notion)

        contents = notion.contents(page_id)
        self.assertNotIn("paragraph:古い数字", contents)
        self.assertEqual(contents[-1], "heading_2:メモ（ChatGPT）")
        self.assertIsNotNone(get_weekly_page(self.db_path, 1, self.week)["bot_block_ids"])

    async def test_trashed_page_is_created_again(self):
        notion = FakeNotion()
        await self.export(notion)
        old_page = get_weekly_page(self.db_path, 1, self.week)["page_id"]
        notion.trashed_pages.add(old_page)

        await self.export(notion)

        new_page = get_weekly_page(self.db_path, 1, self.week)["page_id"]
        self.assertNotEqual(new_page, old_page)
        self.assertEqual(notion.contents(new_page)[-1], "heading_2:メモ（ChatGPT）")

    async def test_recreates_database_deleted_in_notion(self):
        notion = FakeNotion()
        await self.export(notion)
        notion.fail_create_page_once = True

        await self.export(notion, data=DATA_NEXT_WEEK)

        self.assertEqual(get_notion_database(self.db_path, PAGE_UUID), ("db-2", "ds-2"))
        # 記録がない週はグラフをアップロードしない
        self.assertNotIn("upload", [call[0] for call in notion.calls])

    async def test_changing_parent_page_creates_new_database(self):
        notion = FakeNotion()
        await self.export(notion)
        other = "fedcba98-7654-3210-fedc-ba9876543210"
        with patch.object(config, "NOTION_PAGE_ID", other):
            await self.export(notion, data=DATA_NEXT_WEEK)
        self.assertEqual(get_notion_database(self.db_path, other), ("db-2", "ds-2"))

    async def test_save_returns_line_for_discord(self):
        async def ok(*args):
            return "https://notion.so/p"

        async def broken(*args):
            raise NotionError(401, "unauthorized", "bad token")

        with patch.object(notion_export, "export_weekly_report", ok):
            line = await notion_export.save_weekly_report_to_notion(
                1, DATA, "a", None, TODAY
            )
        self.assertEqual(line, "📝 Notionにも保存しました：https://notion.so/p")

        with patch.object(notion_export, "export_weekly_report", broken):
            line = await notion_export.save_weekly_report_to_notion(
                1, DATA, "a", None, TODAY
            )
        self.assertIn("トークンが正しくありません", line)

        with patch.object(config, "NOTION_TOKEN", None):
            self.assertIsNone(await notion_export.save_weekly_report_to_notion(
                1, DATA, "a", None, TODAY
            ))


class _TypingContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Context:
    def __init__(self):
        self.author = SimpleNamespace(id=1)
        self.messages = []

    def typing(self):
        return _TypingContext()

    async def send(self, content=None, **kwargs):
        self.messages.append(dict(kwargs, content=content))


class ReportCommandNotionTests(unittest.IsolatedAsyncioTestCase):
    async def test_notion_option(self):
        async def fake_report(user_id):
            return DATA, "本文", None

        async def fake_save(user_id, data, answer, ai_error, today):
            return "📝 Notionにも保存しました：https://notion.so/p"

        with patch.object(ai_feature, "create_weekly_report", fake_report), \
                patch.object(ai_feature, "save_weekly_report_to_notion", fake_save), \
                patch.object(ai_feature, "is_notion_configured", lambda: True):
            ctx = _Context()
            await ai_feature.report.callback(ctx, notion=True)
            self.assertIn("Notionにも保存しました", ctx.messages[0]["content"])
            self.assertEqual(ctx.messages[0]["embed"].description, "本文")

            ctx = _Context()
            await ai_feature.report.callback(ctx)
            self.assertIsNone(ctx.messages[0]["content"])

        with patch.object(ai_feature, "create_weekly_report", fake_report), \
                patch.object(ai_feature, "is_notion_configured", lambda: False):
            ctx = _Context()
            await ai_feature.report.callback(ctx, notion=True)
            self.assertIn("NOTION_TOKEN", ctx.messages[0]["content"])


if __name__ == "__main__":
    unittest.main()


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    async def json(self, content_type=None):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, headers=None, json=None, data=None):
        self.requests.append(SimpleNamespace(
            method=method, url=url, headers=headers, json=json, data=data,
        ))
        return _FakeResponse(*self.responses.pop(0))


class NotionClientRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_follow_the_api(self):
        from studybot.notion import NOTION_VERSION, NotionClient

        session = _FakeSession([
            (200, {"id": "up-1"}),
            (200, {"id": "up-1", "status": "uploaded"}),
            (200, {"id": "db-1", "data_sources": [{"id": "ds-1"}]}),
            (200, {"id": "pg-1", "url": "https://notion.so/pg-1"}),
            (200, {"id": "pg-1"}),
            (200, {"results": [{"id": f"b{i}"} for i in range(100)]}),
            (200, {"results": [{"id": f"b{i}"} for i in range(100, 120)]}),
            (200, {"results": [{"id": "x1"}], "has_more": True, "next_cursor": "c1"}),
            (200, {"results": [{"id": "x2"}], "has_more": False}),
            (200, {"id": "x1", "in_trash": True}),
        ])
        client = NotionClient("secret_test", session)

        self.assertEqual(await client.upload_png(b"png", "a.png"), "up-1")
        self.assertEqual(
            await client.create_database(PAGE_UUID, "DB", {"週": {"type": "title", "title": {}}}),
            ("db-1", "ds-1"),
        )
        self.assertEqual(
            await client.create_page("ds-1", {}), ("pg-1", "https://notion.so/pg-1")
        )
        await client.update_page_properties("pg-1", {"問題数": {"number": 3}})
        ids = await client.append_children("pg-1", [{}] * 120, position="start")
        self.assertEqual(len(ids), 120)
        self.assertEqual(
            [block["id"] for block in await client.list_children("pg-1")],
            ["x1", "x2"],
        )
        await client.delete_block("x1")

        (create_upload, send, database, page, properties,
         first_chunk, second_chunk, list_1, list_2, delete) = session.requests
        self.assertEqual(create_upload.url, "https://api.notion.com/v1/file_uploads")
        self.assertEqual(create_upload.headers["Notion-Version"], NOTION_VERSION)
        self.assertEqual(create_upload.headers["Authorization"], "Bearer secret_test")
        self.assertEqual(send.url, "https://api.notion.com/v1/file_uploads/up-1/send")
        self.assertNotIn("Content-Type", send.headers)  # multipart は自動で付く
        self.assertIsNotNone(send.data)
        self.assertEqual(database.json["parent"], {"type": "page_id", "page_id": PAGE_UUID})
        self.assertIn("initial_data_source", database.json)
        self.assertEqual(
            page.json["parent"], {"type": "data_source_id", "data_source_id": "ds-1"}
        )
        self.assertNotIn("children", page.json)
        self.assertEqual(
            (properties.method, properties.json),
            ("PATCH", {"properties": {"問題数": {"number": 3}}}),
        )
        # 先頭に入れ、101個目からは直前のブロックの後ろに続ける
        self.assertEqual(first_chunk.url, "https://api.notion.com/v1/blocks/pg-1/children")
        self.assertEqual(len(first_chunk.json["children"]), 100)
        self.assertEqual(first_chunk.json["position"], {"type": "start"})
        self.assertEqual(
            second_chunk.json["position"],
            {"type": "after_block", "after_block": {"id": "b99"}},
        )
        self.assertTrue(list_2.url.endswith("start_cursor=c1"))
        self.assertEqual(
            (delete.method, delete.url), ("DELETE", "https://api.notion.com/v1/blocks/x1")
        )

    async def test_error_response_raises(self):
        from studybot.notion import NotionClient

        session = _FakeSession([(404, {"code": "object_not_found", "message": "x"})])
        with self.assertRaises(NotionError) as caught:
            await NotionClient("t", session).delete_block("p")
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "object_not_found"))
