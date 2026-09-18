"""CloudWatch Logs query tool — service-aware.

Can query by service name (resolves log groups from config) or by raw log_group.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

from agent.config import AgentConfig
from agent.tools.base import Tool
from agent.tools.schema import ToolParameter, ToolSchema

log = logging.getLogger(__name__)


class CloudWatchLogsTool(Tool):
    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}

    def _get_client(self, environment: str | None = None, region: str | None = None) -> Any:
        """Get or create a boto3 CloudWatch Logs client for the given environment/region."""
        key = f"{environment or '_default'}:{region or '_default'}"
        if key not in self._clients:
            if environment:
                region_cfg = self._config.get_region_config(environment, region)
                if region_cfg:
                    self._clients[key] = boto3.client("logs", **region_cfg.boto3_kwargs())
                else:
                    self._clients[key] = boto3.client("logs")
            else:
                self._clients[key] = boto3.client("logs")
        return self._clients[key]

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="cloudwatch_logs",
            description=(
                "Search CloudWatch Logs. Provide a service name to auto-resolve "
                "log groups from config, or provide a raw log_group name. "
                "Filter by time range and pattern."
            ),
            parameters=[
                ToolParameter(
                    name="service",
                    type="string",
                    description="Service name from config (e.g. 'arm', 'audit-logging'). Auto-resolves log groups.",
                    required=False,
                ),
                ToolParameter(
                    name="environment",
                    type="string",
                    description="Environment name (e.g. 'integ', 'prod'). Determines AWS credentials.",
                    required=False,
                ),
                ToolParameter(
                    name="log_group",
                    type="string",
                    description="Raw CloudWatch log group name. Use when not querying by service.",
                    required=False,
                ),
                ToolParameter(
                    name="filter_pattern",
                    type="string",
                    description='CloudWatch filter pattern (e.g. "ERROR", "{ $.statusCode = 500 }").',
                    required=False,
                ),
                ToolParameter(
                    name="start_time",
                    type="string",
                    description="Start time in ISO 8601 format. Defaults to 1 hour ago.",
                    required=False,
                ),
                ToolParameter(
                    name="end_time",
                    type="string",
                    description="End time in ISO 8601 format. Defaults to now.",
                    required=False,
                ),
                ToolParameter(
                    name="region",
                    type="string",
                    description="AWS region (e.g. 'eu-central-1'). Defaults to the environment's first region.",
                    required=False,
                ),
                ToolParameter(
                    name="limit",
                    type="integer",
                    description="Max events to return (default 50, max 200).",
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> str:
        return await asyncio.to_thread(self._query, **kwargs)

    def _query(self, **kwargs: Any) -> str:
        service_name: str | None = kwargs.get("service")
        environment: str | None = kwargs.get("environment")
        raw_log_group: str | None = kwargs.get("log_group")
        filter_pattern: str = kwargs.get("filter_pattern", "")
        limit: int = min(kwargs.get("limit", 50), 200)

        # Resolve log groups
        if service_name:
            svc = self._config.services.get(service_name)
            if not svc:
                available = ", ".join(self._config.services.keys())
                return f"[ERROR] Unknown service: '{service_name}'. Available: {available}"
            if not svc.has_cloudwatch:
                return f"[ERROR] Service '{service_name}' has no CloudWatch log groups configured."
            # Check region availability
            region: str | None = kwargs.get("region")
            resolved_region = region
            resolved_env = ""
            if not resolved_region and environment:
                resolved_env, resolved_region = self._config.resolve_environment(environment)
            elif environment:
                resolved_env, _ = self._config.resolve_environment(environment)
            if not svc.has_cloudwatch_in_region(resolved_region):
                return (f"[ERROR] Service '{service_name}' CW log groups only exist in "
                        f"{', '.join(svc.cloudwatch.regions_only)}. "
                        f"Requested region: {resolved_region or 'default (us-east-1)'}. "
                        f"Use Athena to query logs for this region instead.")
            log_groups = svc.cloudwatch.get_log_groups(resolved_region or "", resolved_env)
        elif raw_log_group:
            log_groups = [raw_log_group]
        else:
            return "[ERROR] Provide either 'service' or 'log_group' parameter."

        # Resolve time range
        now = datetime.now(timezone.utc)
        start_ms = self._parse_time_ms(kwargs.get("start_time"), default_hours_ago=1)
        end_ms = self._parse_time_ms(kwargs.get("end_time")) or int(now.timestamp() * 1000)

        region_param: str | None = kwargs.get("region")
        client = self._get_client(environment, region_param)

        # Query all log groups, merge results
        all_lines: list[tuple[int, str]] = []  # (timestamp_ms, formatted_line)
        errors: list[str] = []

        per_group_limit = max(limit // len(log_groups), 10) if len(log_groups) > 1 else limit

        for lg in log_groups:
            try:
                params: dict[str, Any] = {
                    "logGroupName": lg,
                    "startTime": start_ms,
                    "endTime": end_ms,
                    "limit": per_group_limit,
                    "interleaved": True,
                }
                if filter_pattern:
                    params["filterPattern"] = filter_pattern

                response = client.filter_log_events(**params)
                for ev in response.get("events", []):
                    ts = ev["timestamp"]
                    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                    msg = ev.get("message", "").strip()
                    group_short = lg.rsplit("/", 1)[-1]
                    all_lines.append((ts, f"[{dt.isoformat()}] [{group_short}] {msg}"))

            except client.exceptions.ResourceNotFoundException:
                errors.append(f"Log group not found: {lg}")
            except Exception as e:
                errors.append(f"{lg}: {type(e).__name__}: {e}")

        # Sort by timestamp and format
        all_lines.sort(key=lambda x: x[0])
        lines = [line for _, line in all_lines[:limit]]

        parts: list[str] = []
        if service_name:
            parts.append(f"Service: {service_name} | Log groups: {len(log_groups)} | Events: {len(lines)}")
        if errors:
            parts.append("Errors: " + "; ".join(errors))
        if lines:
            parts.append("\n".join(lines))
        else:
            parts.append("No log events found matching the criteria.")

        result = "\n".join(parts)
        if len(result) > 8000:
            result = result[:8000] + f"\n... [output truncated]"
        return result

    def _parse_time_ms(self, time_str: str | None, default_hours_ago: int = 0) -> int:
        if time_str:
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        if default_hours_ago:
            now = datetime.now(timezone.utc)
            dt = now - timedelta(hours=default_hours_ago)
            return int(dt.timestamp() * 1000)
        return 0
