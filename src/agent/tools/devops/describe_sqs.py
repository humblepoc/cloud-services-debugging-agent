"""SQS queue inspection tool — hybrid static + dynamic.

Uses YAML config as baseline (known queues per service), plus live
SQS API calls to get queue attributes, message counts, and status.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import boto3

from agent.config import AgentConfig
from agent.tools.base import Tool
from agent.tools.schema import ToolParameter, ToolSchema

log = logging.getLogger(__name__)

# Attributes to fetch from SQS
_QUEUE_ATTRIBUTES = [
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
    "VisibilityTimeout",
    "MessageRetentionPeriod",
    "CreatedTimestamp",
    "LastModifiedTimestamp",
    "QueueArn",
    "RedrivePolicy",
]


class DescribeSQSTool(Tool):
    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}

    def _get_client(self, environment: str | None = None, region: str | None = None) -> Any:
        """Get or create a boto3 SQS client."""
        key = f"{environment or '_default'}:{region or '_default'}"
        if key not in self._clients:
            if environment:
                region_cfg = self._config.get_region_config(environment, region)
                if region_cfg:
                    self._clients[key] = boto3.client("sqs", **region_cfg.boto3_kwargs())
                else:
                    self._clients[key] = boto3.client("sqs")
            else:
                self._clients[key] = boto3.client("sqs")
        return self._clients[key]

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="describe_sqs",
            description=(
                "Describe SQS queue status for a service. "
                "Shows approximate message counts, visibility timeout, "
                "retention period, and dead-letter queue config. "
                "Uses config as baseline and enriches with live SQS API data. "
                "Can also inspect a queue by name or URL directly."
            ),
            parameters=[
                ToolParameter(
                    name="service",
                    type="string",
                    description="Service name from config (e.g. 'audit-logging'). "
                                "Resolves SQS queues from config then checks live status.",
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
                    name="queue_name",
                    type="string",
                    description="Queue name for direct lookup (bypasses config). "
                                "Will search for matching queue URL.",
                    required=False,
                ),
                ToolParameter(
                    name="queue_url",
                    type="string",
                    description="Full SQS queue URL for direct inspection.",
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> str:
        if kwargs.get("service"):
            return await asyncio.to_thread(self._describe_service, **kwargs)
        elif kwargs.get("queue_url"):
            return await asyncio.to_thread(self._describe_queue_url, **kwargs)
        elif kwargs.get("queue_name"):
            return await asyncio.to_thread(self._describe_queue_name, **kwargs)
        else:
            return "[ERROR] Provide 'service', 'queue_name', or 'queue_url'."

    def _describe_service(self, **kwargs: Any) -> str:
        """Describe all SQS queues for a configured service."""
        service_name: str = kwargs["service"]
        environment: str = kwargs.get("environment", "prod")
        region: str | None = kwargs.get("region")

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

        queues = svc.metrics.get_sqs_queues(resolved_region or "", resolved_env)
        if not queues:
            return (f"[INFO] Service '{service_name}' has no SQS queues configured "
                    f"for region {resolved_region or 'default'}. "
                    f"Use queue_name or queue_url parameter for direct lookup.")

        client = self._get_client(environment, region)
        sections: list[str] = []
        sections.append(f"=== SQS Queues: {service_name} | Env: {environment} "
                        f"| Region: {resolved_region} ===\n")

        for queue_name in queues:
            sections.append(f"--- Queue: {queue_name} ---")
            self._append_queue_details(client, queue_name, sections)
            sections.append("")

        return "\n".join(sections)

    def _describe_queue_name(self, **kwargs: Any) -> str:
        """Describe a single queue by name."""
        queue_name: str = kwargs["queue_name"]
        environment: str = kwargs.get("environment", "prod")
        region: str | None = kwargs.get("region")

        client = self._get_client(environment, region)
        sections: list[str] = [f"=== SQS Queue: {queue_name} ===\n"]
        self._append_queue_details(client, queue_name, sections)
        return "\n".join(sections)

    def _describe_queue_url(self, **kwargs: Any) -> str:
        """Describe a single queue by URL."""
        queue_url: str = kwargs["queue_url"]
        environment: str = kwargs.get("environment", "prod")
        region: str | None = kwargs.get("region")

        client = self._get_client(environment, region)
        queue_name = queue_url.split("/")[-1]
        sections: list[str] = [f"=== SQS Queue: {queue_name} ===\nURL: {queue_url}\n"]
        self._append_queue_attrs(client, queue_url, sections)
        return "\n".join(sections)

    def _append_queue_details(self, client: Any, queue_name: str,
                              sections: list[str]) -> None:
        """Find queue URL by name, then fetch attributes."""
        try:
            resp = client.get_queue_url(QueueName=queue_name)
            queue_url = resp["QueueUrl"]
            sections.append(f"  URL: {queue_url}")
            self._append_queue_attrs(client, queue_url, sections)
        except client.exceptions.QueueDoesNotExist:
            sections.append(f"  [WARN] Queue not found: {queue_name}")
            # Try with .fifo suffix
            try:
                resp = client.get_queue_url(QueueName=f"{queue_name}.fifo")
                queue_url = resp["QueueUrl"]
                sections.append(f"  Found as FIFO: {queue_url}")
                self._append_queue_attrs(client, queue_url, sections)
            except Exception:
                sections.append(f"  [WARN] Also not found as FIFO queue")
        except Exception as e:
            sections.append(f"  [ERROR] {e}")

    def _append_queue_attrs(self, client: Any, queue_url: str,
                            sections: list[str]) -> None:
        """Fetch and format queue attributes."""
        try:
            resp = client.get_queue_attributes(
                QueueUrl=queue_url,
                AttributeNames=_QUEUE_ATTRIBUTES,
            )
            attrs = resp.get("Attributes", {})

            msgs_visible = attrs.get("ApproximateNumberOfMessages", "?")
            msgs_inflight = attrs.get("ApproximateNumberOfMessagesNotVisible", "?")
            msgs_delayed = attrs.get("ApproximateNumberOfMessagesDelayed", "?")
            visibility = attrs.get("VisibilityTimeout", "?")
            retention = attrs.get("MessageRetentionPeriod", "?")
            redrive = attrs.get("RedrivePolicy", "")

            sections.append(f"  Messages visible: {msgs_visible}")
            sections.append(f"  Messages in-flight: {msgs_inflight}")
            sections.append(f"  Messages delayed: {msgs_delayed}")
            sections.append(f"  Visibility timeout: {visibility}s")

            # Convert retention to human-readable
            try:
                ret_secs = int(retention)
                if ret_secs >= 86400:
                    sections.append(f"  Retention: {ret_secs // 86400} days")
                elif ret_secs >= 3600:
                    sections.append(f"  Retention: {ret_secs // 3600} hours")
                else:
                    sections.append(f"  Retention: {ret_secs}s")
            except (ValueError, TypeError):
                sections.append(f"  Retention: {retention}s")

            if redrive:
                sections.append(f"  Dead-letter config: {redrive}")

            # Highlight if queue is backing up
            try:
                visible = int(msgs_visible)
                if visible > 1000:
                    sections.append(f"  [ALERT] High message count: {visible} messages visible!")
                elif visible > 100:
                    sections.append(f"  [WARN] Elevated message count: {visible} messages visible")
            except (ValueError, TypeError):
                pass

        except Exception as e:
            sections.append(f"  [ERROR] Failed to get attributes: {e}")
