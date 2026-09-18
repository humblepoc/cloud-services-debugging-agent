"""AWS Athena SQL query tool — service-aware.

Can query by service name (auto-builds SQL from config) or with raw SQL.
Uses the logging account credentials for Athena queries.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

from agent.config import AgentConfig
from agent.tools.base import Tool
from agent.tools.schema import ToolParameter, ToolSchema

log = logging.getLogger(__name__)

POLL_INTERVAL = 2.0
MAX_POLL_TIME = 300


class AthenaTool(Tool):
    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}

    def _get_client(self, region: str | None = None) -> Any:
        """Get or create a boto3 Athena client using logging account creds."""
        key = region or "_default"
        if key not in self._clients:
            la = self._config.logging_account
            self._clients[key] = boto3.client("athena", **la.boto3_kwargs(region))
        return self._clients[key]

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="athena_query",
            description=(
                "Query logs via Athena. Provide a service name + environment to "
                "auto-build an optimized query, or provide raw SQL. "
                "Uses the logging account's Athena database."
            ),
            parameters=[
                ToolParameter(
                    name="service",
                    type="string",
                    description="Service name from config (e.g. 'arm'). Auto-builds query from config.",
                    required=False,
                ),
                ToolParameter(
                    name="environment",
                    type="string",
                    description="Environment (e.g. 'integ', 'prod'). Required with service.",
                    required=False,
                ),
                ToolParameter(
                    name="filter_pattern",
                    type="string",
                    description="Text to search for in log messages (used with service mode).",
                    required=False,
                ),
                ToolParameter(
                    name="hours",
                    type="integer",
                    description="Hours of data to query (default 1, used with service mode).",
                    required=False,
                ),
                ToolParameter(
                    name="level",
                    type="string",
                    description="Filter by log level (e.g. 'ERROR', 'WARN'). Used with service mode.",
                    required=False,
                ),
                ToolParameter(
                    name="limit",
                    type="integer",
                    description="Max rows to return (default 100).",
                    required=False,
                ),
                ToolParameter(
                    name="query",
                    type="string",
                    description="Raw SQL query. Use for custom queries instead of service mode.",
                    required=False,
                ),
                ToolParameter(
                    name="database",
                    type="string",
                    description="Athena database name. Used with raw query mode.",
                    required=False,
                ),
                ToolParameter(
                    name="region",
                    type="string",
                    description="AWS region for Athena (e.g. 'eu-central-1'). Defaults to logging account's default region.",
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> str:
        service_name: str | None = kwargs.get("service")
        if service_name:
            return await asyncio.to_thread(self._query_service, **kwargs)
        elif kwargs.get("query"):
            return await asyncio.to_thread(
                self._run_query,
                kwargs["query"],
                kwargs.get("database", "default"),
                kwargs.get("region"),
            )
        else:
            return "[ERROR] Provide either 'service' + 'environment' or 'query' + 'database'."

    def _query_service(self, **kwargs: Any) -> str:
        """Build and execute an optimized query for a service."""
        service_name: str = kwargs["service"]
        environment: str = kwargs.get("environment", "integ")
        filter_pattern: str = kwargs.get("filter_pattern", "")
        level: str = kwargs.get("level", "")
        hours: int = kwargs.get("hours", 1)
        limit: int = kwargs.get("limit", 100)
        region: str | None = kwargs.get("region")

        # Auto-detect region from compound environment name (e.g. prod-eu -> eu-central-1)
        if not region:
            _, resolved_region = self._config.resolve_environment(environment)
            if resolved_region:
                region = resolved_region

        svc = self._config.services.get(service_name)
        if not svc:
            available = ", ".join(self._config.services.keys())
            return f"[ERROR] Unknown service: '{service_name}'. Available: {available}"

        athena_cfg = svc.athena_env(environment)
        if not athena_cfg.enabled or not athena_cfg.database or not athena_cfg.table:
            envs = svc.athena_envs
            return (
                f"[ERROR] Service '{service_name}' has no Athena config for '{environment}'. "
                f"Available envs: {', '.join(envs) if envs else 'none'}"
            )

        # Build optimized SQL with partition pruning
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=hours)

        where_parts = _build_partition_predicates(start, now)

        if filter_pattern:
            safe = filter_pattern.replace("'", "''")
            where_parts.append(f"msg LIKE '%{safe}%'")

        if level:
            safe_level = level.replace("'", "''")
            where_parts.append(f"level = '{safe_level}'")

        where_clause = " AND ".join(where_parts)

        sql = (
            f"SELECT logtimestamp, level, SUBSTR(msg, 1, 300) as msg, traceid, service, action, pod_name\n"
            f"FROM \"{athena_cfg.table}\"\n"
            f"WHERE {where_clause}\n"
            f"ORDER BY logtimestamp DESC\n"
            f"LIMIT {limit}"
        )

        log.info("Athena service query: %s/%s env=%s", athena_cfg.database, athena_cfg.table, environment)
        result = self._run_query(sql, athena_cfg.database, region)

        header = f"Service: {service_name} | Env: {environment} | Table: {athena_cfg.table} | Last {hours}h"
        if filter_pattern:
            header += f" | Filter: '{filter_pattern}'"
        if level:
            header += f" | Level: {level}"

        return f"{header}\n{result}"

    def _run_query(self, query: str, database: str, region: str | None = None) -> str:
        la = self._config.logging_account
        athena_cfg = la.get_athena_config(region)
        output_location = athena_cfg.output_location
        if not output_location:
            return "[ERROR] logging_account.athena.output_location not configured."

        client = self._get_client(region)

        try:
            response = client.start_query_execution(
                QueryString=query,
                QueryExecutionContext={"Database": database},
                ResultConfiguration={"OutputLocation": output_location},
                WorkGroup=athena_cfg.workgroup,
            )
            execution_id = response["QueryExecutionId"]
            log.info("Athena query started: %s", execution_id)

            # Poll for completion
            start = time.time()
            while time.time() - start < MAX_POLL_TIME:
                status = client.get_query_execution(QueryExecutionId=execution_id)
                state = status["QueryExecution"]["Status"]["State"]

                if state == "SUCCEEDED":
                    return self._format_results(execution_id, region)
                elif state in ("FAILED", "CANCELLED"):
                    reason = status["QueryExecution"]["Status"].get("StateChangeReason", "Unknown")
                    return f"[ERROR] Query {state}: {reason}"

                time.sleep(POLL_INTERVAL)

            return f"[ERROR] Query timed out after {MAX_POLL_TIME}s"

        except Exception as e:
            return f"[ERROR] Athena: {type(e).__name__}: {e}"

    def _format_results(self, execution_id: str, region: str | None = None) -> str:
        client = self._get_client(region)
        results = client.get_query_results(QueryExecutionId=execution_id)
        rows = results["ResultSet"]["Rows"]

        if not rows:
            return "(no results)"

        headers = [col.get("VarCharValue", "") for col in rows[0]["Data"]]
        lines = [" | ".join(headers), " | ".join("-" * len(h) for h in headers)]

        for row in rows[1:]:
            values = [col.get("VarCharValue", "") for col in row["Data"]]
            lines.append(" | ".join(values))

        result = "\n".join(lines)
        if len(result) > 8000:
            result = result[:8000] + f"\n... [{len(rows) - 1} total rows, output truncated]"
        return result


def _build_partition_predicates(start: datetime, end: datetime) -> list[str]:
    """Build year/month/day partition predicates for efficient Athena scanning."""
    parts: list[str] = []

    if start.year == end.year:
        parts.append(f"year = '{start.year}'")
        if start.month == end.month:
            parts.append(f"month = '{start.month:02d}'")
            if start.day == end.day:
                parts.append(f"day = '{start.day:02d}'")
            else:
                parts.append(f"day BETWEEN '{start.day:02d}' AND '{end.day:02d}'")
        else:
            parts.append(f"month BETWEEN '{start.month:02d}' AND '{end.month:02d}'")
    else:
        parts.append(f"year BETWEEN '{start.year}' AND '{end.year}'")

    return parts
