"""Kinesis Data Stream inspection tool — hybrid static + dynamic.

Uses YAML config as baseline (known streams per service), plus live
Kinesis & CloudWatch API calls to get stream status, shard count,
metrics, and error logs.
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


class DescribeKinesisTool(Tool):
    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}

    def _get_client(self, service_name: str, environment: str | None = None,
                    region: str | None = None) -> Any:
        """Get or create a boto3 client for the given AWS service."""
        key = f"{service_name}:{environment or '_default'}:{region or '_default'}"
        if key not in self._clients:
            if environment:
                region_cfg = self._config.get_region_config(environment, region)
                if region_cfg:
                    self._clients[key] = boto3.client(service_name, **region_cfg.boto3_kwargs())
                else:
                    self._clients[key] = boto3.client(service_name)
            else:
                self._clients[key] = boto3.client(service_name)
        return self._clients[key]

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="describe_kinesis",
            description=(
                "Describe Kinesis data streams for a service. "
                "Shows stream status, shard count, retention, encryption, "
                "consumer lag (iterator age), and recent error logs. "
                "Uses config as baseline and enriches with live API data. "
                "Can also inspect a stream by name directly."
            ),
            parameters=[
                ToolParameter(
                    name="service",
                    type="string",
                    description="Service name from config (e.g. 'audit-logging'). "
                                "Resolves Kinesis streams from config.",
                    required=False,
                ),
                ToolParameter(
                    name="environment",
                    type="string",
                    description="Environment: 'integ', 'prod', 'prod-eu', 'prod-ap'.",
                    required=False,
                ),
                ToolParameter(
                    name="region",
                    type="string",
                    description="AWS region (e.g. 'eu-central-1').",
                    required=False,
                ),
                ToolParameter(
                    name="stream_name",
                    type="string",
                    description="Kinesis stream name for direct lookup (bypasses config).",
                    required=False,
                ),
                ToolParameter(
                    name="hours",
                    type="number",
                    description="Hours of metrics/logs to fetch (default: 1).",
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> str:
        if kwargs.get("service"):
            return await asyncio.to_thread(self._describe_service, **kwargs)
        elif kwargs.get("stream_name"):
            return await asyncio.to_thread(self._describe_stream, **kwargs)
        else:
            return "[ERROR] Provide 'service' or 'stream_name'."

    def _describe_service(self, **kwargs: Any) -> str:
        """Describe all Kinesis streams for a configured service."""
        service_name: str = kwargs["service"]
        environment: str = kwargs.get("environment", "prod")
        region: str | None = kwargs.get("region")
        hours: float = kwargs.get("hours", 1)

        svc = self._config.services.get(service_name)
        if not svc:
            available = ", ".join(self._config.services.keys())
            return f"[ERROR] Unknown service: '{service_name}'. Available: {available}"

        resolved_region = region
        resolved_env = ""
        if not resolved_region:
            resolved_env, resolved_region = self._config.resolve_environment(environment)
        else:
            resolved_env, _ = self._config.resolve_environment(environment)

        streams = svc.metrics.get_kinesis_streams(resolved_region or "", resolved_env)
        if not streams:
            return (f"[INFO] Service '{service_name}' has no Kinesis streams configured "
                    f"for region {resolved_region or 'default'}. "
                    f"Use stream_name parameter for direct lookup.")

        kinesis = self._get_client("kinesis", environment, region)
        cw = self._get_client("cloudwatch", environment, region)
        logs_client = self._get_client("logs", environment, region)

        sections: list[str] = []
        sections.append(f"=== Kinesis Streams: {service_name} | Env: {environment} "
                        f"| Region: {resolved_region} ===\n")

        for stream in streams:
            sections.append(f"--- Stream: {stream} ---")
            self._append_stream_details(kinesis, cw, logs_client, stream, hours, sections)
            sections.append("")

        return "\n".join(sections)

    def _describe_stream(self, **kwargs: Any) -> str:
        """Describe a single stream by name."""
        stream_name: str = kwargs["stream_name"]
        environment: str = kwargs.get("environment", "prod")
        region: str | None = kwargs.get("region")
        hours: float = kwargs.get("hours", 1)

        kinesis = self._get_client("kinesis", environment, region)
        cw = self._get_client("cloudwatch", environment, region)
        logs_client = self._get_client("logs", environment, region)

        sections: list[str] = [f"=== Kinesis Stream: {stream_name} ===\n"]
        self._append_stream_details(kinesis, cw, logs_client, stream_name, hours, sections)
        return "\n".join(sections)

    def _append_stream_details(self, kinesis: Any, cw: Any, logs_client: Any,
                               stream_name: str, hours: float,
                               sections: list[str]) -> None:
        """Fetch stream description, metrics, and error logs."""
        # --- Describe stream ---
        try:
            resp = kinesis.describe_stream_summary(StreamName=stream_name)
            desc = resp["StreamDescriptionSummary"]

            sections.append(f"  Status: {desc.get('StreamStatus', '?')}")
            sections.append(f"  Stream ARN: {desc.get('StreamARN', '?')}")
            sections.append(f"  Shard count (open): {desc.get('OpenShardCount', '?')}")
            sections.append(f"  Retention: {desc.get('RetentionPeriodHours', '?')} hours")

            # Encryption
            enc_type = desc.get("EncryptionType", "NONE")
            if enc_type != "NONE":
                key_id = desc.get("KeyId", "")
                sections.append(f"  Encryption: {enc_type} (key: {key_id})")
            else:
                sections.append(f"  Encryption: NONE")

            # Enhanced monitoring
            enhanced = desc.get("EnhancedMonitoring", [])
            shard_levels = []
            for em in enhanced:
                shard_levels.extend(em.get("ShardLevelMetrics", []))
            if shard_levels:
                sections.append(f"  Enhanced monitoring: {', '.join(shard_levels)}")

            # Consumer count
            consumers = desc.get("ConsumerCount", 0)
            if consumers:
                sections.append(f"  Registered consumers: {consumers}")

            # Stream mode
            mode = desc.get("StreamModeDetails", {}).get("StreamMode", "PROVISIONED")
            sections.append(f"  Mode: {mode}")

        except Exception as e:
            sections.append(f"  [ERROR] describe_stream_summary failed: {e}")
            return  # Can't continue without basic info

        # --- Key metrics (last N hours) ---
        self._append_metrics(cw, stream_name, hours, sections)

        # --- Error logs (if CW logging is configured) ---
        self._append_error_logs(logs_client, stream_name, hours, sections)

    def _append_metrics(self, cw: Any, stream_name: str, hours: float,
                        sections: list[str]) -> None:
        """Fetch key Kinesis metrics from CloudWatch."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        period = max(300, int(hours * 3600 / 12))  # ~12 data points

        metrics = [
            ("IncomingRecords", "Sum"),
            ("IncomingBytes", "Sum"),
            ("GetRecords.IteratorAgeMilliseconds", "Maximum"),
            ("ReadProvisionedThroughputExceeded", "Sum"),
            ("WriteProvisionedThroughputExceeded", "Sum"),
        ]

        try:
            queries = []
            for i, (metric, stat) in enumerate(metrics):
                queries.append({
                    "Id": f"m{i}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/Kinesis",
                            "MetricName": metric,
                            "Dimensions": [{"Name": "StreamName", "Value": stream_name}],
                        },
                        "Period": period,
                        "Stat": stat,
                    },
                })

            resp = cw.get_metric_data(
                MetricDataQueries=queries,
                StartTime=start,
                EndTime=end,
            )

            sections.append(f"\n  Metrics (last {hours}h):")
            for i, (metric, stat) in enumerate(metrics):
                result = next((r for r in resp["MetricDataResults"] if r["Id"] == f"m{i}"), None)
                if result and result["Values"]:
                    vals = result["Values"]
                    if stat == "Sum":
                        total = sum(vals)
                        sections.append(f"    {metric}: {total:,.0f} (total)")
                    else:
                        max_val = max(vals)
                        if "IteratorAge" in metric:
                            # Convert ms to readable
                            if max_val > 3600000:
                                sections.append(f"    {metric}: {max_val / 3600000:.1f}h [ALERT: HIGH LAG]")
                            elif max_val > 60000:
                                sections.append(f"    {metric}: {max_val / 60000:.1f}m [WARN: ELEVATED LAG]")
                            elif max_val > 0:
                                sections.append(f"    {metric}: {max_val / 1000:.1f}s")
                            else:
                                sections.append(f"    {metric}: 0 (caught up)")
                        else:
                            sections.append(f"    {metric}: {max_val:,.0f} (max)")

                    # Highlight throttling
                    if "Exceeded" in metric and stat == "Sum" and sum(vals) > 0:
                        sections.append(f"    [WARN] Throttling detected: {sum(vals):,.0f} events")
                else:
                    sections.append(f"    {metric}: no data")

        except Exception as e:
            sections.append(f"  [ERROR] Metrics fetch failed: {e}")

    def _append_error_logs(self, logs_client: Any, stream_name: str, hours: float,
                           sections: list[str]) -> None:
        """Check for Kinesis-related error logs in CloudWatch Logs."""
        # Kinesis error log group naming convention
        log_group = f"/aws/kinesis/{stream_name}"
        try:
            # Check if log group exists
            resp = logs_client.describe_log_groups(logGroupNamePrefix=log_group, limit=1)
            groups = resp.get("logGroups", [])
            if not groups or groups[0]["logGroupName"] != log_group:
                return  # No error log group — that's normal

            sections.append(f"\n  Error logs ({log_group}):")
            start_ms = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000)
            end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

            resp = logs_client.filter_log_events(
                logGroupName=log_group,
                startTime=start_ms,
                endTime=end_ms,
                limit=20,
            )
            events = resp.get("events", [])
            if events:
                sections.append(f"    Found {len(events)} log entries:")
                for evt in events[:10]:
                    ts = datetime.fromtimestamp(evt["timestamp"] / 1000, tz=timezone.utc)
                    msg = evt.get("message", "").strip()[:200]
                    sections.append(f"    [{ts:%H:%M:%S}] {msg}")
                if len(events) > 10:
                    sections.append(f"    ... and {len(events) - 10} more")
            else:
                sections.append(f"    No error logs in last {hours}h")

        except Exception:
            pass  # No log group or access denied — silently skip
