"""Publish investigation reports to Confluence Cloud."""
from __future__ import annotations

import logging
from typing import Any

from agent.config import AgentConfig
from agent.tools.base import Tool
from agent.tools.schema import ToolParameter, ToolSchema

log = logging.getLogger(__name__)


class PublishConfluenceTool(Tool):
    def __init__(self, config: AgentConfig) -> None:
        self._config = config

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="publish_confluence",
            description=(
                "Publish an investigation report to Confluence Cloud as a new page. "
                "Provide a descriptive title and the full report content in Markdown. "
                "The page will be created under the configured parent page."
            ),
            parameters=[
                ToolParameter(
                    name="title",
                    type="string",
                    description="Page title (e.g. 'identity-management 5xx spike - 2026-04-28')",
                    required=True,
                ),
                ToolParameter(
                    name="content",
                    type="string",
                    description="Full report content in Markdown format.",
                    required=True,
                ),
                ToolParameter(
                    name="space_key",
                    type="string",
                    description="Confluence space key (default: from config).",
                    required=False,
                ),
                ToolParameter(
                    name="parent_page_title",
                    type="string",
                    description="Parent page title to nest under (default: from config).",
                    required=False,
                ),
                ToolParameter(
                    name="parent_page_id",
                    type="string",
                    description="Parent page ID to nest under (takes priority over title, default: from config).",
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> str:
        title: str = kwargs.get("title", "")
        content: str = kwargs.get("content", "")
        space_key: str = kwargs.get("space_key", "")
        parent_page_title: str = kwargs.get("parent_page_title", "")
        parent_page_id: str = kwargs.get("parent_page_id", "")

        if not title or not content:
            missing = []
            if not title:
                missing.append("title")
            if not content:
                missing.append("content")
            return (
                f"Error: missing required parameter(s): {', '.join(missing)}. "
                f"Please call publish_confluence again with both 'title' and 'content' (the full report in Markdown)."
            )

        conf = self._config.confluence
        if not conf.user_email or not conf.api_token:
            return (
                "Error: Confluence credentials not configured. "
                "Set confluence.user_email and confluence.api_token in config.local.yaml. "
                "Generate an API token at https://id.atlassian.com/manage-profile/security/api-tokens"
            )

        space = space_key or conf.space_key
        parent_title = parent_page_title or conf.parent_page_title
        parent_id = parent_page_id or conf.parent_page_id

        try:
            from agent.confluence import ConfluenceClient

            client = ConfluenceClient(conf.url, conf.user_email, conf.api_token)
            result = client.create_page(
                title=title,
                content_markdown=content,
                space_key=space,
                parent_page_title=parent_title,
                parent_page_id=parent_id,
            )
            return (
                f"Published to Confluence!\n"
                f"Title: {result['title']}\n"
                f"URL: {result['url']}"
            )
        except Exception as exc:
            log.exception("Failed to publish to Confluence")
            return f"Error publishing to Confluence: {exc}"
