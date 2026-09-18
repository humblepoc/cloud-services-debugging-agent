"""AWS Lambda handler entry point."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agent.config import load_config
from agent.core.context import ConversationContext
from agent.core.loop import run_agent
from agent.core.types import Message, Role
from agent.llm.registry import get_provider
from agent.tools.base import ToolRegistry
from agent.tools.bash import BashTool
from agent.flavors.devops import DevOpsFlavor, register_devops_tools

log = logging.getLogger(__name__)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point.

    Expects event with:
        - body: JSON string or dict with "query" field
        - Optional: "config_overrides" dict
    """
    try:
        body = _parse_body(event)
        query = body.get("query", "")
        if not query:
            return _response(400, {"error": "Missing 'query' in request body"})

        config_overrides = body.get("config_overrides", {})
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_run(query, config_overrides))
        finally:
            loop.close()
        return _response(200, result)

    except Exception as e:
        log.exception("Lambda handler error")
        return _response(500, {"error": f"{type(e).__name__}: {e}"})


async def _run(query: str, config_overrides: dict[str, Any]) -> dict[str, Any]:
    """Run the agent and return structured results."""
    cfg = load_config()

    # Apply any runtime overrides
    if provider := config_overrides.get("provider"):
        cfg.llm.provider = provider
    if model := config_overrides.get("model"):
        cfg.llm.model = model

    provider = get_provider(cfg.llm.provider, cfg)

    registry = ToolRegistry()
    registry.register(BashTool())
    register_devops_tools(registry, cfg)

    flavor = DevOpsFlavor()
    context = ConversationContext(system_prompt=flavor.system_prompt(cfg))
    context.add(Message(role=Role.USER, content=query))

    result = await run_agent(provider, registry, context, max_iterations=cfg.max_iterations)

    return {
        "answer": result.content,
        "provider": cfg.llm.provider,
        "model": cfg.llm.model,
        "tokens": _total_tokens(context),
    }


def _total_tokens(context: ConversationContext) -> int:
    return context.estimate_tokens()


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body", event)
    if isinstance(body, str):
        body = json.loads(body)
    return body


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
