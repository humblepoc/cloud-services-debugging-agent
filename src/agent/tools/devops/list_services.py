"""List configured services and their available data sources.

This is the agent's most important discovery tool — it tells the LLM what
services exist and what data sources are available for each, so it can
query by service name instead of guessing raw AWS resource names.
"""
from __future__ import annotations

from typing import Any

from agent.config import AgentConfig
from agent.tools.base import Tool
from agent.tools.schema import ToolParameter, ToolSchema


class ListServicesTool(Tool):
    def __init__(self, config: AgentConfig) -> None:
        self._config = config

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_services",
            description=(
                "List all configured services and their available data sources "
                "(CloudWatch log groups, Athena tables, Lambda functions, RDS/DocDB clusters). "
                "Use this first to discover what you can query."
            ),
            parameters=[
                ToolParameter(
                    name="tag_filter",
                    type="string",
                    description="Filter by tag (comma-separated, e.g. 'core,app').",
                    required=False,
                ),
                ToolParameter(
                    name="service_filter",
                    type="string",
                    description="Filter by service name (comma-separated, e.g. 'arm,audit-logging').",
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> str:
        tag_filter = kwargs.get("tag_filter")
        service_filter = kwargs.get("service_filter")
        services = self._config.get_services(
            name_filter=service_filter,
            tag_filter=tag_filter,
        )

        if not services:
            return "No services configured. Add services to config.yaml."

        lines: list[str] = []
        lines.append(f"=== {len(services)} Services ===")
        lines.append(f"Environments: {', '.join(self._config.environment_names) or 'none configured'}")
        lines.append("")

        for name, svc in services.items():
            lines.append(f"--- {name} ---")
            if svc.description:
                lines.append(f"  Description: {svc.description}")
            if svc.tags:
                lines.append(f"  Tags: {', '.join(svc.tags)}")

            # CloudWatch
            if svc.has_cloudwatch:
                lines.append(f"  CloudWatch Log Groups:")
                if svc.cloudwatch.log_groups:
                    for lg in svc.cloudwatch.log_groups:
                        lines.append(f"    - {lg}")
                for region_name, ro in svc.cloudwatch.regions.items():
                    if ro.log_groups:
                        lines.append(f"    [{region_name}]:")
                        for lg in ro.log_groups:
                            lines.append(f"      - {lg}")

            # Athena
            if svc.has_athena:
                lines.append(f"  Athena:")
                for env_name in svc.athena_envs:
                    acfg = svc.athena_env(env_name)
                    if acfg.enabled and acfg.database and acfg.table:
                        lines.append(f"    {env_name}: {acfg.database} / {acfg.table}")

            # Metrics
            if svc.has_metrics:
                m = svc.metrics
                if m.lambda_functions:
                    lines.append(f"  Lambda Functions: {', '.join(m.lambda_functions)}")
                if m.rds_clusters:
                    lines.append(f"  RDS Clusters: {', '.join(m.rds_clusters)}")
                if m.rds_instances:
                    lines.append(f"  RDS Instances: {', '.join(m.rds_instances)}")
                if m.docdb_clusters:
                    lines.append(f"  DocDB Clusters: {', '.join(m.docdb_clusters)}")
                if m.kinesis_streams:
                    lines.append(f"  Kinesis Streams: {', '.join(m.kinesis_streams)}")
                if m.firehose_streams:
                    lines.append(f"  Firehose Streams: {', '.join(m.firehose_streams)}")
                if m.sqs_queues:
                    lines.append(f"  SQS Queues: {', '.join(m.sqs_queues)}")
                for region_name, ro in m.regions.items():
                    region_parts = []
                    if ro.lambda_functions:
                        region_parts.append(f"Lambdas: {', '.join(ro.lambda_functions)}")
                    if ro.rds_clusters:
                        region_parts.append(f"RDS: {', '.join(ro.rds_clusters)}")
                    if ro.docdb_clusters:
                        region_parts.append(f"DocDB: {', '.join(ro.docdb_clusters)}")
                    if ro.kinesis_streams:
                        region_parts.append(f"Kinesis: {', '.join(ro.kinesis_streams)}")
                    if ro.firehose_streams:
                        region_parts.append(f"Firehose: {', '.join(ro.firehose_streams)}")
                    if ro.sqs_queues:
                        region_parts.append(f"SQS: {', '.join(ro.sqs_queues)}")
                    if ro.alb.load_balancer:
                        region_parts.append(f"ALB: {ro.alb.load_balancer}")
                    if region_parts:
                        lines.append(f"  [{region_name}]: {' | '.join(region_parts)}")

            lines.append("")

        return "\n".join(lines)
