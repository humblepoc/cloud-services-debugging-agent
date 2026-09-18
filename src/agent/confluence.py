"""Confluence Cloud API client for publishing reports.

Uses the Confluence Cloud REST API v1 (v2 doesn't support storage format conversion).
Authentication: basic auth with user_email:api_token.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)


def _markdown_to_storage(md: str) -> str:
    """Convert Markdown to Confluence Storage Format (XHTML).

    Lightweight converter — handles the common patterns in agent reports:
    headings, bold, italic, code blocks, inline code, lists, tables, links.
    """
    lines = md.split("\n")
    out: list[str] = []
    in_code_block = False
    code_lang = ""
    code_lines: list[str] = []
    in_table = False
    table_rows: list[list[str]] = []
    header_row = False

    def _flush_table():
        nonlocal in_table, table_rows
        if not table_rows:
            return
        html = "<table><tbody>"
        for i, row in enumerate(table_rows):
            tag = "th" if i == 0 else "td"
            html += "<tr>" + "".join(f"<{tag}>{cell}</{tag}>" for cell in row) + "</tr>"
        html += "</tbody></table>"
        out.append(html)
        table_rows = []
        in_table = False

    for line in lines:
        # Code block fences
        if line.strip().startswith("```"):
            if in_code_block:
                # Close code block
                code = "\n".join(code_lines)
                lang_attr = f' language="{code_lang}"' if code_lang else ""
                out.append(
                    f'<ac:structured-macro ac:name="code">'
                    f"<ac:parameter ac:name=\"language\">{code_lang or 'text'}</ac:parameter>"
                    f"<ac:plain-text-body><![CDATA[{code}]]></ac:plain-text-body>"
                    f"</ac:structured-macro>"
                )
                in_code_block = False
                code_lines = []
                code_lang = ""
            else:
                _flush_table()
                in_code_block = True
                code_lang = line.strip()[3:].strip()
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        # Table rows (| col1 | col2 |)
        if "|" in line and line.strip().startswith("|"):
            stripped = line.strip()
            # Skip separator rows (|---|---|)
            if re.match(r"^\|[\s\-:|]+\|$", stripped):
                continue
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if not in_table:
                in_table = True
            table_rows.append(cells)
            continue
        elif in_table:
            _flush_table()

        # Headings
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            text = _inline_format(m.group(2))
            out.append(f"<h{level}>{text}</h{level}>")
            continue

        # Unordered list
        m = re.match(r"^(\s*)[-*]\s+(.*)", line)
        if m:
            text = _inline_format(m.group(2))
            out.append(f"<ul><li>{text}</li></ul>")
            continue

        # Ordered list
        m = re.match(r"^(\s*)\d+\.\s+(.*)", line)
        if m:
            text = _inline_format(m.group(2))
            out.append(f"<ol><li>{text}</li></ol>")
            continue

        # Empty line = paragraph break
        if not line.strip():
            out.append("")
            continue

        # Regular paragraph
        out.append(f"<p>{_inline_format(line)}</p>")

    _flush_table()
    return "\n".join(out)


def _inline_format(text: str) -> str:
    """Apply inline Markdown formatting: bold, italic, code, links."""
    # Inline code
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # Italic
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    # Links
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


class ConfluenceClient:
    """Client for Confluence Cloud REST API."""

    def __init__(self, url: str, user_email: str, api_token: str) -> None:
        self.base_url = url.rstrip("/")
        self._auth = (user_email, api_token)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            auth=self._auth,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=30.0,
        )

    def find_page_by_title(self, title: str, space_key: str) -> dict[str, Any] | None:
        """Find a page by exact title in the given space."""
        with self._client() as c:
            resp = c.get(
                "/wiki/rest/api/content",
                params={"spaceKey": space_key, "title": title, "type": "page", "limit": 1},
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            return results[0] if results else None

    def get_or_create_parent_page(self, title: str, space_key: str) -> str:
        """Get or create the parent page. Returns page ID."""
        existing = self.find_page_by_title(title, space_key)
        if existing:
            return existing["id"]

        # Create the parent page
        payload = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": "<p>Auto-created by Debugging Agent. Investigation reports are published as child pages.</p>",
                    "representation": "storage",
                }
            },
        }
        with self._client() as c:
            resp = c.post("/wiki/rest/api/content", json=payload)
            resp.raise_for_status()
            return resp.json()["id"]

    def create_page(
        self,
        title: str,
        content_markdown: str,
        space_key: str,
        parent_page_title: str = "",
        parent_page_id: str = "",
    ) -> dict[str, Any]:
        """Create a new Confluence page from Markdown content.

        Returns dict with 'id', 'url', 'title'.
        parent_page_id takes priority over parent_page_title.
        """
        storage_body = _markdown_to_storage(content_markdown)

        payload: dict[str, Any] = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": storage_body,
                    "representation": "storage",
                }
            },
        }

        # Nest under parent page — ID takes priority over title
        if parent_page_id:
            payload["ancestors"] = [{"id": parent_page_id}]
        elif parent_page_title:
            pid = self.get_or_create_parent_page(parent_page_title, space_key)
            payload["ancestors"] = [{"id": pid}]

        with self._client() as c:
            resp = c.post("/wiki/rest/api/content", json=payload)
            resp.raise_for_status()
            data = resp.json()
            page_id = data["id"]
            page_url = f"{self.base_url}/wiki{data['_links']['webui']}"
            return {"id": page_id, "url": page_url, "title": data["title"]}
