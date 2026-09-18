"""CloudTrail event lookup tool — service-aware.

Uses environment-specific credentials for the correct AWS account.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

from agent.config import AgentConfig
from agent.tools.base import Tool
from agent.tools.schema import ToolParameter, ToolSchema

log = logging.getLogger(__name__)


class CloudTrailTool(Tool):
    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}

    def _get_client(self, environment: str | None = None, region: str | None = None) -> Any:
        key = f"{environment or '_default'}:{region or '_default'}"
        if key not in self._clients:
            if environment:
                region_cfg = self._config.get_region_config(environment, region)
                if region_cfg:
                    self._clients[key] = boto3.client("cloudtrail", **region_cfg.boto3_kwargs())
                else:
                    self._clients[key] = boto3.client("cloudtrail")
            else:
                self._clients[key] = boto3.client("cloudtrail")
        return self._clients[key]

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="cloudtrail_lookup",
            description=(
                "Look up recent AWS CloudTrail events to find infrastructure changes. "
                "Useful for identifying what changed before an incident."
            ),
            parameters=[
                ToolParameter(
                    name="environment",
                    type="string",
                    description="Environment (e.g. 'integ', 'prod'). Determines which AWS account to query.",
                    required=False,
                ),
                ToolParameter(
                    name="hours",
                    type="integer",
                    description="How many hours back to search (default 24).",
                    required=False,
                ),
                ToolParameter(
                    name="event_name",
                    type="string",
                    description="Filter by API action (e.g. UpdateFunctionConfiguration).",
                    required=False,
                ),
                ToolParameter(
                    name="resource_type",
                    type="string",
                    description="Filter by resource type (e.g. AWS::Lambda::Function).",
                    required=False,
                ),
                ToolParameter(
                    name="username",
                    type="string",
                    description="Filter by IAM username or role.",
                    required=False,
                ),
                ToolParameter(
                    name="region",
                    type="string",
                    description="AWS region (e.g. 'eu-central-1'). Defaults to the environment's first region.",
                    required=False,
                ),
                ToolParameter(
                    name="max_results",
                    type="integer",
                    description="Maximum events to return (default 20, max 50).",
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> str:
        return await asyncio.to_thread(self._lookup, **kwargs)

    def _lookup(self, **kwargs: Any) -> str:
        environment: str | None = kwargs.get("environment")
        hours: int = kwargs.get("hours", 24)
        max_results: int = min(kwargs.get("max_results", 20), 50)

        now = datetime.now(timezone.utc)
        start_time = now - timedelta(hours=hours)

        lookup_attrs: list[dict[str, str]] = []
        if event_name := kwargs.get("event_name"):
            lookup_attrs.append({"AttributeKey": "EventName", "AttributeValue": event_name})
        if resource_type := kwargs.get("resource_type"):
            lookup_attrs.append({"AttributeKey": "ResourceType", "AttributeValue": resource_type})
        if username := kwargs.get("username"):
            lookup_attrs.append({"AttributeKey": "Username", "AttributeValue": username})

        region: str | None = kwargs.get("region")
        client = self._get_client(environment, region)

        try:
            params: dict[str, Any] = {
                "StartTime": start_time,
                "EndTime": now,
                "MaxResults": max_results,
            }
            if lookup_attrs:
                params["LookupAttributes"] = lookup_attrs

            response = client.lookup_events(**params)
            events = response.get("Events", [])

            if not events:
                env_str = f" in {environment}" if environment else ""
                return f"No CloudTrail events found{env_str} in the last {hours}h matching the criteria."

            lines = []
            if environment:
                lines.append(f"Environment: {environment} | Last {hours}h")

            for ev in events:
                ts = ev["EventTime"].strftime("%Y-%m-%d %H:%M:%S UTC")
                name = ev.get("EventName", "?")
                user = ev.get("Username", "?")
                resources = ev.get("Resources", [])

                resource_str = ""
                if resources:
                    r = resources[0]
                    resource_str = f" -> {r.get('ResourceType', '')}: {r.get('ResourceName', '')}"

                lines.append(f"[{ts}] {name} by {user}{resource_str}")

                if raw := ev.get("CloudTrailEvent"):
                    try:
                        detail = json.loads(raw)
                        if err := detail.get("errorCode"):
                            lines.append(f"  ERROR: {err} -- {detail.get('errorMessage', '')}")
                        source_ip = detail.get("sourceIPAddress", "")
                        if source_ip:
                            lines.append(f"  Source: {source_ip}")
                    except json.JSONDecodeError:
                        pass

            return "\n".join(lines)

        except Exception as e:
            return f"[ERROR] CloudTrail: {type(e).__name__}: {e}"
