"""Notion API との通信と、Notion のブロックの組み立て。

API バージョン 2026-03-11 を使う。この版ではデータベースの中に
「データソース」があり、ページはデータソースに作る。
"""

import re

import aiohttp


NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"

# Notion の rich_text 1つあたりの文字数上限
TEXT_LIMIT = 2000
# 1回のリクエストで追加できるブロック数の上限
CHILDREN_LIMIT = 100


class NotionError(Exception):
    def __init__(self, status, code, message):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message

    def friendly(self):
        """Discord に表示する、原因が分かる短い説明。"""
        if self.status == 401:
            return "Notionのトークンが正しくありません（.env の NOTION_TOKEN を確認）"
        if self.status in (403, 404):
            return (
                "Notionのページが見つからないか、StudyBotのコネクトが"
                "追加されていません（ページの「…」→「接続」を確認）"
            )
        if self.status == 429:
            return "Notionへのリクエストが多すぎます。少し待ってから再度お試しください"
        return f"Notionでエラーが発生しました（{self.status} {self.code}）"


def extract_notion_id(value):
    """ページのURLやIDから、ハイフン付きのID（UUID形式）を取り出す。"""
    if not value:
        return None
    compact = value.replace("-", "")
    found = re.findall(r"[0-9a-fA-F]{32}", compact)
    if not found:
        return None
    raw = found[-1].lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


# ------------------------------------------------------------
# ブロックの組み立て
# ------------------------------------------------------------

def rich_text(text, bold=False):
    """**太字** を太字に変えつつ、上限に合わせて分割した rich_text の配列。"""
    items = []
    for index, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if not part:
            continue
        is_bold = bold or index % 2 == 1
        for start in range(0, len(part), TEXT_LIMIT):
            item = {
                "type": "text",
                "text": {"content": part[start:start + TEXT_LIMIT]},
            }
            if is_bold:
                item["annotations"] = {"bold": True}
            items.append(item)
    return items


def text_block(block_type, text):
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": rich_text(text)},
    }


def callout_block(text, emoji):
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": rich_text(text),
            "icon": {"type": "emoji", "emoji": emoji},
        },
    }


def image_block(file_upload_id):
    return {
        "object": "block",
        "type": "image",
        "image": {
            "type": "file_upload",
            "file_upload": {"id": file_upload_id},
        },
    }


def markdown_to_blocks(text):
    """AIの返答（簡単なMarkdown）を Notion のブロックに変える。"""
    blocks = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or set(line) <= {"-", "*", "_"}:
            continue  # 空行と区切り線は飛ばす
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        bullet = re.match(r"^[-*・]\s+(.*)$", line)
        numbered = re.match(r"^\d+[.)．]\s*(.*)$", line)
        if heading:
            level = "heading_3" if len(heading.group(1)) >= 3 else "heading_2"
            blocks.append(text_block(level, heading.group(2).strip("* ")))
        elif bullet:
            blocks.append(text_block("bulleted_list_item", bullet.group(1)))
        elif numbered:
            blocks.append(text_block("numbered_list_item", numbered.group(1)))
        else:
            blocks.append(text_block("paragraph", line))
    return blocks


# ------------------------------------------------------------
# API クライアント
# ------------------------------------------------------------

class NotionClient:
    def __init__(self, token, session):
        self.token = token
        self.session = session

    def _headers(self, json_body=True):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    async def _request(self, method, path, json=None, data=None):
        async with self.session.request(
            method,
            f"{NOTION_API}{path}",
            headers=self._headers(json_body=data is None),
            json=json,
            data=data,
        ) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                raise NotionError(
                    response.status,
                    (body or {}).get("code", "unknown"),
                    (body or {}).get("message", ""),
                )
            return body

    async def retrieve_page(self, page_id):
        """ページの情報。共有されていないなどで読めなければ NotionError。"""
        return await self._request("GET", f"/pages/{page_id}")

    async def upload_png(self, png, filename):
        """画像をアップロードし、image ブロックで使う file_upload の ID を返す。"""
        created = await self._request("POST", "/file_uploads", json={
            "filename": filename,
            "content_type": "image/png",
        })
        form = aiohttp.FormData()
        form.add_field(
            "file", png, filename=filename, content_type="image/png"
        )
        await self._request(
            "POST", f"/file_uploads/{created['id']}/send", data=form
        )
        return created["id"]

    async def create_database(self, parent_page_id, title, properties):
        """(database_id, data_source_id) を返す。"""
        created = await self._request("POST", "/databases", json={
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": rich_text(title),
            "initial_data_source": {"properties": properties},
        })
        return created["id"], created["data_sources"][0]["id"]

    async def create_page(self, data_source_id, properties):
        """中身が空のページを作り、(page_id, url) を返す。"""
        created = await self._request("POST", "/pages", json={
            "parent": {
                "type": "data_source_id",
                "data_source_id": data_source_id,
            },
            "properties": properties,
        })
        return created["id"], created.get("url")

    async def update_page_properties(self, page_id, properties):
        await self._request(
            "PATCH", f"/pages/{page_id}", json={"properties": properties}
        )

    async def append_children(self, block_id, children, position=None):
        """ブロックを追加し、追加したブロックのIDを順番どおりに返す。

        position が None なら末尾、"start" なら先頭に入れる。
        100個を超える場合は分けて送り、2回目以降は直前の続きに入れる。
        """
        ids = []
        for start in range(0, len(children), CHILDREN_LIMIT):
            body = {"children": children[start:start + CHILDREN_LIMIT]}
            if ids:
                body["position"] = {
                    "type": "after_block", "after_block": {"id": ids[-1]},
                }
            elif position == "start":
                body["position"] = {"type": "start"}
            created = await self._request(
                "PATCH", f"/blocks/{block_id}/children", json=body
            )
            ids.extend(block["id"] for block in created["results"])
        return ids

    async def list_children(self, block_id):
        """直下のブロックをすべて返す。"""
        blocks = []
        cursor = None
        while True:
            path = f"/blocks/{block_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            listed = await self._request("GET", path)
            blocks.extend(listed["results"])
            if not listed.get("has_more"):
                return blocks
            cursor = listed["next_cursor"]

    async def retrieve_block(self, block_id):
        return await self._request("GET", f"/blocks/{block_id}")

    async def delete_block(self, block_id):
        await self._request("DELETE", f"/blocks/{block_id}")
