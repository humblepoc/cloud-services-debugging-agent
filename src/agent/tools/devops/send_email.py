"""Send investigation reports via email using notification service."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from agent.config import AgentConfig
from agent.tools.base import Tool
from agent.tools.schema import ToolParameter, ToolSchema

log = logging.getLogger(__name__)


class SendEmailTool(Tool):
    def __init__(self, config: AgentConfig) -> None:
        self._config = config

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="send_email",
            description=(
                "Send an investigation report or notification email. "
                "Admin is always BCC'd. Service-specific recipients come from the service config. "
                "Content should be markdown — it will be converted to clean HTML."
            ),
            parameters=[
                ToolParameter(
                    name="title",
                    type="string",
                    description="Email subject (prefix is added automatically)",
                    required=True,
                ),
                ToolParameter(
                    name="content",
                    type="string",
                    description="Email body in Markdown format",
                    required=True,
                ),
                ToolParameter(
                    name="service",
                    type="string",
                    description="Service name — resolves recipients from service config's email_recipients",
                    required=False,
                ),
                ToolParameter(
                    name="recipients",
                    type="string",
                    description="Comma-separated override recipients (used as To instead of service recipients)",
                    required=False,
                ),
                ToolParameter(
                    name="confluence_url",
                    type="string",
                    description="Confluence page URL to include as a link in the email (from publish_confluence result)",
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> str:
        from agent.email_client import EmailClient, markdown_to_html

        cfg = self._config
        email_cfg = cfg.email

        # Validate credentials
        if not email_cfg.client_id or not email_cfg.client_secret:
            return "Error: Email credentials not configured. Set email.client_id and email.client_secret in config.local.yaml."
        if not email_cfg.notify_url or not email_cfg.token_url:
            return "Error: Email URLs not configured. Set email.notify_url and email.token_url in config."

        # Resolve To recipients
        recipients_override = kwargs.get("recipients", "")
        service = kwargs.get("service", "")

        if recipients_override:
            to_list = [r.strip() for r in recipients_override.split(",") if r.strip()]
        elif service and service in cfg.services:
            to_list = list(cfg.services[service].email_recipients)
        else:
            to_list = []

        # BCC admin
        bcc = [email_cfg.admin_email] if email_cfg.admin_email else []

        # Must have at least someone to send to
        if not to_list and not bcc:
            return "Error: No recipients — service has no email_recipients and no admin_email configured."

        # If no To recipients, send to admin directly (can't send BCC-only)
        if not to_list and bcc:
            to_list = bcc
            bcc = []

        # Build subject
        title = kwargs["title"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        subject = f"{email_cfg.subject_prefix} {title} - {now}"

        # Convert content to HTML
        html_body = markdown_to_html(kwargs["content"])

        # Append Confluence link if provided
        confluence_url = kwargs.get("confluence_url", "")
        if confluence_url:
            html_body += (
                '<div style="margin:20px 0;padding:12px 16px;background:#f0f4ff;'
                'border-left:4px solid #2684FF;border-radius:4px;">'
                '<strong>Full Report on Confluence:</strong> '
                f'<a href="{confluence_url}" style="color:#2684FF;">{confluence_url}</a>'
                '</div>'
            )

        # Send
        client = EmailClient(
            notify_url=email_cfg.notify_url,
            token_url=email_cfg.token_url,
            client_id=email_cfg.client_id,
            client_secret=email_cfg.client_secret,
        )
        try:
            client.send_email(
                recipients=to_list,
                subject=subject,
                html_body=html_body,
                bcc=bcc or None,
                from_application=email_cfg.from_application,
                sender=email_cfg.sender,
            )
            bcc_note = f" (BCC: {', '.join(bcc)})" if bcc else ""
            return f"Email sent to {', '.join(to_list)}{bcc_note}. Subject: {subject}"
        except Exception as exc:
            log.exception("Failed to send email")
            return f"Error sending email: {exc}"
