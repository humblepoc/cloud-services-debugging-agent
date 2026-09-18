"""CloudWatch Metrics query tool — service-aware.

Can query by service name (auto-resolves Lambda/RDS/DocDB metrics from config)
or by raw namespace/metric/dimensions.
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

# Key metrics to auto-query per resource type
LAMBDA_METRICS = [
    ("Invocations", "Sum"),
    ("Errors", "Sum"),
    ("Duration", "Average"),
    ("Duration", "Maximum"),
    ("Throttles", "Sum"),
    ("ConcurrentExecutions", "Maximum"),
]

RDS_METRICS = [
    ("CPUUtilization", "Average"),
    ("DatabaseConnections", "Average"),
    ("ReadIOPS", "Average"),
    ("WriteIOPS", "Average"),
    ("FreeableMemory", "Average"),
]

DOCDB_METRICS = [
    ("CPUUtilization", "Average"),
    ("DatabaseConnections", "Average"),
    ("OpcountersQuery", "Sum"),
    ("OpcountersInsert", "Sum"),
    ("FreeableMemory", "Average"),
]

ALB_METRICS = [
    ("RequestCount", "Sum"),
    ("HTTPCode_ELB_5XX_Count", "Sum"),
    ("HTTPCode_ELB_4XX_Count", "Sum"),
    ("HTTPCode_Target_5XX_Count", "Sum"),
    ("HTTPCode_Target_4XX_Count", "Sum"),
    ("TargetResponseTime", "Average"),
]

TARGET_GROUP_METRICS = [
    ("RequestCount", "Sum"),
    ("HTTPCode_Target_5XX_Count", "Sum"),
    ("HTTPCode_Target_4XX_Count", "Sum"),
    ("UnHealthyHostCount", "Average"),
    ("HealthyHostCount", "Average"),
    ("TargetResponseTime", "Average"),
]

KINESIS_METRICS = [
    ("IncomingRecords", "Sum"),
    ("IncomingBytes", "Sum"),
    ("GetRecords.IteratorAgeMilliseconds", "Maximum"),
    ("GetRecords.Records", "Sum"),
    ("ReadProvisionedThroughputExceeded", "Sum"),
    ("WriteProvisionedThroughputExceeded", "Sum"),
]

FIREHOSE_METRICS = [
    ("IncomingRecords", "Sum"),
    ("IncomingBytes", "Sum"),
    ("DeliveryToS3.Records", "Sum"),
    ("DeliveryToS3.Success", "Sum"),
    ("DeliveryToS3.DataFreshness", "Maximum"),
]

SQS_METRICS = [
    ("ApproximateNumberOfMessagesVisible", "Maximum"),
    ("ApproximateAgeOfOldestMessage", "Maximum"),
    ("NumberOfMessagesSent", "Sum"),
    ("NumberOfMessagesReceived", "Sum"),
    ("NumberOfMessagesDeleted", "Sum"),
    ("ApproximateNumberOfMessagesNotVisible", "Maximum"),
]


class CloudWatchMetricsTool(Tool):
    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}

    def _get_client(self, environment: str | None = None, region: str | None = None) -> Any:
        key = f"{environment or '_default'}:{region or '_default'}"
        if key not in self._clients:
            if environment:
                region_cfg = self._config.get_region_config(environment, region)
                if region_cfg:
                    self._clients[key] = boto3.client("cloudwatch", **region_cfg.boto3_kwargs())
                else:
                    self._clients[key] = boto3.client("cloudwatch")
            else:
                self._clients[key] = boto3.client("cloudwatch")
        return self._clients[key]

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="cloudwatch_metrics",
            description=(
                "Get CloudWatch metrics. Provide a service name to auto-query all "
                "Lambda/RDS/DocDB/Kinesis/Firehose metrics from config, or provide raw namespace/metric/dimensions."
            ),
            parameters=[
                ToolParameter(
                    name="service",
                    type="string",
                    description="Service name from config. Auto-queries all metrics for its resources.",
                    required=False,
                ),
                ToolParameter(
                    name="environment",
                    type="string",
                    description="Environment name (e.g. 'integ', 'prod').",
                    required=False,
                ),
                ToolParameter(
                    name="namespace",
                    type="string",
                    description="CloudWatch namespace (e.g. AWS/Lambda). Use for raw queries.",
                    required=False,
                ),
                ToolParameter(
                    name="metric_name",
                    type="string",
                    description="Metric name (e.g. Duration, Errors). Use for raw queries.",
                    required=False,
                ),
                ToolParameter(
                    name="dimensions",
                    type="object",
                    description='Dimensions as key-value pairs (e.g. {"FunctionName": "my-func"}).',
                    required=False,
                ),
                ToolParameter(
                    name="stat",
                    type="string",
                    description="Statistic: Average, Sum, Minimum, Maximum, SampleCount.",
                    required=False,
                    enum=["Average", "Sum", "Minimum", "Maximum", "SampleCount"],
                ),
                ToolParameter(
                    name="period",
                    type="integer",
                    description="Period in seconds (auto-calculated if omitted).",
                    required=False,
                ),
                ToolParameter(
                    name="hours",
                    type="integer",
                    description="Hours of data to retrieve (default 3).",
                    required=False,
                ),
                ToolParameter(
                    name="region",
                    type="string",
                    description="AWS region (e.g. 'eu-central-1'). Defaults to the environment's first region.",
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> str:
        if kwargs.get("service"):
            return await asyncio.to_thread(self._query_service, **kwargs)
        elif kwargs.get("namespace") and kwargs.get("metric_name"):
            return await asyncio.to_thread(self._query_raw, **kwargs)
        else:
            return "[ERROR] Provide 'service' or 'namespace' + 'metric_name'."

    def _query_service(self, **kwargs: Any) -> str:
        """Auto-query all metrics for a service's resources."""
        service_name: str = kwargs["service"]
        environment: str = kwargs.get("environment", "integ")
        hours: int = kwargs.get("hours", 3)
        region: str | None = kwargs.get("region")

        svc = self._config.services.get(service_name)
        if not svc:
            available = ", ".join(self._config.services.keys())
            return f"[ERROR] Unknown service: '{service_name}'. Available: {available}"

        if not svc.has_metrics:
            return f"[ERROR] Service '{service_name}' has no metrics configured."

        # Check region availability
        resolved_region = region
        resolved_env = ""
        if not resolved_region:
            resolved_env, resolved_region = self._config.resolve_environment(environment)
        else:
            resolved_env, _ = self._config.resolve_environment(environment)
        if not svc.has_metrics_in_region(resolved_region):
            return (f"[ERROR] Service '{service_name}' metrics resources only exist in "
                    f"{', '.join(svc.metrics.regions_only)}. "
                    f"Requested region: {resolved_region or 'default (us-east-1)'}. "
                    f"Use Athena to query logs for this region instead.")

        client = self._get_client(environment, region)
        now = datetime.now(timezone.utc)
        start_time = now - timedelta(hours=hours)
        period = _auto_period(hours)

        sections: list[str] = []
        sections.append(f"=== Metrics: {service_name} | Env: {environment} | Last {hours}h ===\n")

        m = svc.metrics

        # Lambda metrics
        for fn in m.get_lambda_functions(resolved_region or "", resolved_env):
            sections.append(f"--- Lambda: {fn} ---")
            for metric, stat in LAMBDA_METRICS:
                result = self._get_metric(
                    client, "AWS/Lambda", metric,
                    {"FunctionName": fn}, stat, period, start_time, now,
                )
                sections.append(f"  {metric} ({stat}): {result}")

        # RDS cluster metrics
        for cluster in m.get_rds_clusters(resolved_region or "", resolved_env):
            sections.append(f"--- RDS Cluster: {cluster} ---")
            for metric, stat in RDS_METRICS:
                result = self._get_metric(
                    client, "AWS/RDS", metric,
                    {"DBClusterIdentifier": cluster}, stat, period, start_time, now,
                )
                sections.append(f"  {metric} ({stat}): {result}")

        # RDS instance metrics
        for instance in m.get_rds_instances(resolved_region or "", resolved_env):
            sections.append(f"--- RDS Instance: {instance} ---")
            for metric, stat in RDS_METRICS:
                result = self._get_metric(
                    client, "AWS/RDS", metric,
                    {"DBInstanceIdentifier": instance}, stat, period, start_time, now,
                )
                sections.append(f"  {metric} ({stat}): {result}")

        # DocDB metrics
        for cluster in m.get_docdb_clusters(resolved_region or "", resolved_env):
            sections.append(f"--- DocDB: {cluster} ---")
            for metric, stat in DOCDB_METRICS:
                result = self._get_metric(
                    client, "AWS/DocDB", metric,
                    {"DBClusterIdentifier": cluster}, stat, period, start_time, now,
                )
                sections.append(f"  {metric} ({stat}): {result}")

        # Kinesis Data Stream metrics
        for stream in m.get_kinesis_streams(resolved_region or "", resolved_env):
            sections.append(f"--- Kinesis Stream: {stream} ---")
            for metric, stat in KINESIS_METRICS:
                result = self._get_metric(
                    client, "AWS/Kinesis", metric,
                    {"StreamName": stream}, stat, period, start_time, now,
                )
                sections.append(f"  {metric} ({stat}): {result}")

        # Firehose Delivery Stream metrics
        for stream in m.get_firehose_streams(resolved_region or "", resolved_env):
            sections.append(f"--- Firehose: {stream} ---")
            for metric, stat in FIREHOSE_METRICS:
                result = self._get_metric(
                    client, "AWS/Firehose", metric,
                    {"DeliveryStreamName": stream}, stat, period, start_time, now,
                )
                sections.append(f"  {metric} ({stat}): {result}")

        # SQS Queue metrics
        for queue in m.get_sqs_queues(resolved_region or "", resolved_env):
            sections.append(f"--- SQS Queue: {queue} ---")
            for metric, stat in SQS_METRICS:
                result = self._get_metric(
                    client, "AWS/SQS", metric,
                    {"QueueName": queue}, stat, period, start_time, now,
                )
                sections.append(f"  {metric} ({stat}): {result}")

        # ALB metrics
        alb = m.get_alb(resolved_region or "", resolved_env)
        if alb.load_balancer:
            sections.append(f"--- ALB: {alb.load_balancer} ---")
            for metric, stat in ALB_METRICS:
                result = self._get_metric(
                    client, "AWS/ApplicationELB", metric,
                    {"LoadBalancer": alb.load_balancer}, stat, period, start_time, now,
                )
                sections.append(f"  {metric} ({stat}): {result}")

            # Per target group metrics
            for tg_name, tg_suffix in alb.target_groups.items():
                sections.append(f"--- Target Group: {tg_name} ---")
                for metric, stat in TARGET_GROUP_METRICS:
                    result = self._get_metric(
                        client, "AWS/ApplicationELB", metric,
                        {"LoadBalancer": alb.load_balancer, "TargetGroup": tg_suffix},
                        stat, period, start_time, now,
                    )
                    sections.append(f"  {metric} ({stat}): {result}")

        return "\n".join(sections)

    def _query_raw(self, **kwargs: Any) -> str:
        """Query a single metric with raw parameters."""
        namespace: str = kwargs["namespace"]
        metric_name: str = kwargs["metric_name"]
        dimensions: dict[str, str] = kwargs.get("dimensions", {})
        stat: str = kwargs.get("stat", "Average")
        hours: int = kwargs.get("hours", 3)
        period: int = kwargs.get("period") or _auto_period(hours)

        environment = kwargs.get("environment")
        region: str | None = kwargs.get("region")
        client = self._get_client(environment, region)

        now = datetime.now(timezone.utc)
        start_time = now - timedelta(hours=hours)

        dim_list = [{"Name": k, "Value": v} for k, v in dimensions.items()]

        try:
            response = client.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric_name,
                Dimensions=dim_list,
                StartTime=start_time,
                EndTime=now,
                Period=period,
                Statistics=[stat],
            )

            datapoints = response.get("Datapoints", [])
            if not datapoints:
                return f"No data points for {namespace}/{metric_name} in the last {hours}h."

            datapoints.sort(key=lambda d: d["Timestamp"])

            lines = [f"{'Timestamp':<25} {stat:>15} {'Unit':>10}"]
            lines.append("-" * 55)
            for dp in datapoints:
                ts = dp["Timestamp"].strftime("%Y-%m-%d %H:%M:%S")
                value = dp.get(stat, 0)
                unit = dp.get("Unit", "")
                lines.append(f"{ts:<25} {value:>15.4f} {unit:>10}")

            return "\n".join(lines)

        except Exception as e:
            return f"[ERROR] CloudWatch Metrics: {type(e).__name__}: {e}"

    def _get_metric(
        self, client: Any, namespace: str, metric_name: str,
        dimensions: dict[str, str], stat: str, period: int,
        start_time: datetime, end_time: datetime,
    ) -> str:
        """Get a single metric summary string."""
        dim_list = [{"Name": k, "Value": v} for k, v in dimensions.items()]
        try:
            response = client.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric_name,
                Dimensions=dim_list,
                StartTime=start_time,
                EndTime=end_time,
                Period=period,
                Statistics=[stat],
            )
            datapoints = response.get("Datapoints", [])
            if not datapoints:
                return "no data"

            values = [dp.get(stat, 0) for dp in datapoints]
            unit = datapoints[0].get("Unit", "")
            total = sum(values)
            avg = total / len(values)
            mx = max(values)

            if stat == "Sum":
                return f"total={total:.2f} {unit}"
            elif stat == "Average":
                return f"avg={avg:.2f}, max={mx:.2f} {unit}"
            else:
                return f"max={mx:.2f} {unit}"
        except Exception as e:
            return f"error: {e}"


def _auto_period(hours: int) -> int:
    """Auto-select period based on time range."""
    if hours <= 3:
        return 60
    elif hours <= 24:
        return 300
    else:
        return 3600
