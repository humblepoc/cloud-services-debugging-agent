"""Email client — sends notifications via the notification service using OAuth2 client_credentials."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


@dataclass
class _CachedToken:
    access_token: str = ""
    expires_at: float = 0.0

    @property
    def valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at - 30


class EmailClient:
    """Sends emails via the notification multipart API with OAuth2 Bearer auth."""

    def __init__(self, notify_url: str, token_url: str, client_id: str, client_secret: str):
        self.notify_url = notify_url
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = _CachedToken()

    def _get_token(self) -> str:
        """Fetch OAuth2 token via client_credentials grant (cached)."""
        if self._token.valid:
            return self._token.access_token

        creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        resp = httpx.post(
            self.token_url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {creds}",
            },
            data="grant_type=client_credentials&response_type=token",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token.access_token = data["access_token"]
        self._token.expires_at = time.time() + data.get("expires_in", 1200)
        return self._token.access_token

    def send_email(
        self,
        recipients: list[str],
        subject: str,
        html_body: str,
        *,
        bcc: list[str] | None = None,
        from_application: str = "Debugging-Agent",
        sender: str = "Debugging Agent",
        priority: str = "Normal",
    ) -> dict:
        """Send email via the notification API. Returns response JSON or raises."""
        token = self._get_token()

        metadata = {
            "subject": subject,
            "fromApplication": from_application,
            "sender": sender,
            "priority": priority,
            "recipients": recipients,
            "message": html_body,
            "templateId": "",
            "templateParams": [],
        }
        if bcc:
            metadata["bcc"] = bcc

        resp = httpx.post(
            f"{self.notify_url}/email-notification-jobs",
            headers={"Authorization": f"Bearer {token}"},
            files={"metadata": (None, json.dumps(metadata), "application/json")},
            timeout=30,
        )
        resp.raise_for_status()
        logger.info("Email sent to %s — status %d", recipients, resp.status_code)
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "text": resp.text[:200]}


# ---------------------------------------------------------------------------
# Markdown → simple HTML converter
# ---------------------------------------------------------------------------

def markdown_to_html(md: str) -> str:
    """Convert markdown to clean, simple HTML suitable for email rendering."""
    lines = md.split("\n")
    html_parts: list[str] = []
    in_code_block = False
    in_list = False
    code_lang = ""
    code_lines: list[str] = []

    def _close_list():
        nonlocal in_list
        if in_list:
            html_parts.append("</ul>")
            in_list = False

    def _inline(text: str) -> str:
        """Process inline markdown: bold, italic, code, links."""
        # Code spans first (avoid processing markdown inside them)
        text = re.sub(r"`([^`]+)`", r'<code style="background:#f4f4f4;padding:2px 4px;border-radius:3px;font-size:13px;">\1</code>', text)
        # Bold
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)
        # Italic
        text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
        text = re.sub(r"_(.+?)_", r"<em>\1</em>", text)
        # Links
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
        return text

    for line in lines:
        # Code block toggle
        if line.strip().startswith("```"):
            if in_code_block:
                html_parts.append(
                    '<pre style="background:#1e1e1e;color:#d4d4d4;padding:12px;border-radius:6px;'
                    'font-size:13px;overflow-x:auto;margin:8px 0;">'
                    + _escape("\n".join(code_lines))
                    + "</pre>"
                )
                code_lines = []
                in_code_block = False
            else:
                _close_list()
                in_code_block = True
                code_lang = line.strip().removeprefix("```").strip()
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        stripped = line.strip()

        # Empty line
        if not stripped:
            _close_list()
            continue

        # Headings
        m = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if m:
            _close_list()
            level = len(m.group(1))
            sizes = {1: "22px", 2: "18px", 3: "16px", 4: "14px", 5: "13px", 6: "12px"}
            html_parts.append(
                f'<h{level} style="margin:16px 0 8px 0;font-size:{sizes[level]};">'
                f'{_inline(m.group(2))}</h{level}>'
            )
            continue

        # List items
        if re.match(r"^[-*+]\s+", stripped):
            if not in_list:
                html_parts.append('<ul style="margin:4px 0;padding-left:24px;">')
                in_list = True
            item_text = re.sub(r"^[-*+]\s+", "", stripped)
            html_parts.append(f"<li>{_inline(item_text)}</li>")
            continue

        # Numbered list
        if re.match(r"^\d+\.\s+", stripped):
            if not in_list:
                html_parts.append('<ul style="margin:4px 0;padding-left:24px;">')
                in_list = True
            item_text = re.sub(r"^\d+\.\s+", "", stripped)
            html_parts.append(f"<li>{_inline(item_text)}</li>")
            continue

        # Horizontal rule
        if re.match(r"^---+$", stripped):
            _close_list()
            html_parts.append('<hr style="border:none;border-top:1px solid #ddd;margin:16px 0;">')
            continue

        # Table (simple — just pass rows)
        if "|" in stripped and stripped.startswith("|"):
            # Skip separator rows
            if re.match(r"^\|[\s\-:|]+\|$", stripped):
                continue
            _close_list()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            row = "".join(f'<td style="padding:4px 8px;border:1px solid #ddd;">{_inline(c)}</td>' for c in cells)
            html_parts.append(f"<tr>{row}</tr>")
            continue

        # Regular paragraph
        _close_list()
        html_parts.append(f'<p style="margin:6px 0;line-height:1.5;">{_inline(stripped)}</p>')

    _close_list()

    body = "\n".join(html_parts)
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;'
        'max-width:800px;margin:0 auto;padding:16px;color:#333;font-size:14px;">\n'
        f"{body}\n</div>"
    )


def _escape(text: str) -> str:
    """HTML-escape text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
