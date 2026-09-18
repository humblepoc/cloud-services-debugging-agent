"""Firehose delivery stream inspection tool — hybrid static + dynamic.

Uses YAML config as baseline (known streams per service), plus live
Firehose & CloudWatch API calls to get stream status, destination config,
buffering hints, delivery metrics, and error logs.
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


class DescribeFirehoseTool(Tool):
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
            name="describe_firehose",
            description=(
                "Describe Firehose delivery streams for a service. "
                "Shows stream status, destination config (S3/Redshift/ES), "
                "buffering hints, compression, delivery metrics, and error logs. "
                "Uses config as baseline and enriches with live API data. "
                "Can also inspect a stream by name directly."
            ),
            parameters=[
                ToolParameter(
                    name="service",
                    type="string",
                    description="Service name from config (e.g. 'audit-logging'). "
                                "Resolves Firehose streams from config.",
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
                    description="Firehose delivery stream name for direct lookup (bypasses config).",
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
        """Describe all Firehose streams for a configured service."""
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

        streams = svc.metrics.get_firehose_streams(resolved_region or "", resolved_env)
        if not streams:
            return (f"[INFO] Service '{service_name}' has no Firehose streams configured "
                    f"for region {resolved_region or 'default'}. "
                    f"Use stream_name parameter for direct lookup.")

        firehose = self._get_client("firehose", environment, region)
        cw = self._get_client("cloudwatch", environment, region)
        logs_client = self._get_client("logs", environment, region)

        sections: list[str] = []
        sections.append(f"=== Firehose Streams: {service_name} | Env: {environment} "
                        f"| Region: {resolved_region} ===\n")

        for stream in streams:
            sections.append(f"--- Stream: {stream} ---")
            self._append_stream_details(firehose, cw, logs_client, stream, hours, sections)
            sections.append("")

        return "\n".join(sections)

    def _describe_stream(self, **kwargs: Any) -> str:
        """Describe a single stream by name."""
        stream_name: str = kwargs["stream_name"]
        environment: str = kwargs.get("environment", "prod")
        region: str | None = kwargs.get("region")
        hours: float = kwargs.get("hours", 1)

        firehose = self._get_client("firehose", environment, region)
        cw = self._get_client("cloudwatch", environment, region)
        logs_client = self._get_client("logs", environment, region)

        sections: list[str] = [f"=== Firehose Stream: {stream_name} ===\n"]
        self._append_stream_details(firehose, cw, logs_client, stream_name, hours, sections)
        return "\n".join(sections)

    def _append_stream_details(self, firehose: Any, cw: Any, logs_client: Any,
                               stream_name: str, hours: float,
                               sections: list[str]) -> None:
        """Fetch stream description, metrics, and error logs."""
        cw_log_group: str | None = None
        cw_log_stream: str | None = None

        # --- Describe delivery stream ---
        try:
            resp = firehose.describe_delivery_stream(DeliveryStreamName=stream_name)
            desc = resp["DeliveryStreamDescription"]

            sections.append(f"  Status: {desc.get('DeliveryStreamStatus', '?')}")
            sections.append(f"  Stream ARN: {desc.get('DeliveryStreamARN', '?')}")
            sections.append(f"  Type: {desc.get('DeliveryStreamType', '?')}")
            sections.append(f"  Version: {desc.get('VersionId', '?')}")

            # Source
            source = desc.get("Source", {})
            if source.get("KinesisStreamSourceDescription"):
                ks = source["KinesisStreamSourceDescription"]
                sections.append(f"  Source: Kinesis ({ks.get('KinesisStreamARN', '?')})")
                sections.append(f"  Source status: {ks.get('DeliveryStartTimestamp', '?')}")

            # Encryption
            enc = desc.get("DeliveryStreamEncryptionConfiguration", {})
            enc_status = enc.get("Status", "DISABLED")
            if enc_status != "DISABLED":
                sections.append(f"  Encryption: {enc_status} (key type: {enc.get('KeyType', '?')})")
            else:
                sections.append(f"  Encryption: DISABLED")

            # Destinations
            destinations = desc.get("Destinations", [])
            for dest in destinations:
                dest_id = dest.get("DestinationId", "?")

                # S3 destination
                s3_dest = dest.get("ExtendedS3DestinationDescription") or dest.get("S3DestinationDescription")
                if s3_dest:
                    bucket = s3_dest.get("BucketARN", "?")
                    prefix = s3_dest.get("Prefix", "")
                    compression = s3_dest.get("CompressionFormat", "UNCOMPRESSED")
                    buffering = s3_dest.get("BufferingHints", {})
                    buf_size = buffering.get("SizeInMBs", "?")
                    buf_interval = buffering.get("IntervalInSeconds", "?")

                    sections.append(f"\n  Destination [{dest_id}]: S3")
                    sections.append(f"    Bucket: {bucket}")
                    if prefix:
                        sections.append(f"    Prefix: {prefix}")
                    sections.append(f"    Compression: {compression}")
                    sections.append(f"    Buffering: {buf_size} MB / {buf_interval}s")

                    # Error output
                    error_prefix = s3_dest.get("ErrorOutputPrefix", "")
                    if error_prefix:
                        sections.append(f"    Error prefix: {error_prefix}")

                    # CloudWatch logging
                    cw_opts = s3_dest.get("CloudWatchLoggingOptions", {})
                    if cw_opts.get("Enabled"):
                        cw_log_group = cw_opts.get("LogGroupName")
                        cw_log_stream = cw_opts.get("LogStreamName")
                        sections.append(f"    CW error logging: {cw_log_group}/{cw_log_stream}")
                    else:
                        sections.append(f"    CW error logging: disabled")

                    # Processing (Lambda transform)
                    proc = s3_dest.get("ProcessingConfiguration", {})
                    if proc.get("Enabled"):
                        processors = proc.get("Processors", [])
                        for p in processors:
                            p_type = p.get("Type", "?")
                            params = {pp["ParameterName"]: pp["ParameterValue"]
                                      for pp in p.get("Parameters", [])}
                            if "LambdaArn" in params:
                                sections.append(f"    Transform: {p_type} -> {params['LambdaArn']}")
                            else:
                                sections.append(f"    Transform: {p_type}")

                # Redshift destination
                rs_dest = dest.get("RedshiftDestinationDescription")
                if rs_dest:
                    sections.append(f"\n  Destination [{dest_id}]: Redshift")
                    sections.append(f"    Cluster: {rs_dest.get('ClusterJDBCURL', '?')}")
                    sections.append(f"    Table: {rs_dest.get('CopyCommand', {}).get('DataTableName', '?')}")

                # Elasticsearch destination
                es_dest = dest.get("ElasticsearchDestinationDescription")
                if es_dest:
                    sections.append(f"\n  Destination [{dest_id}]: Elasticsearch")
                    sections.append(f"    Domain: {es_dest.get('DomainARN', '?')}")
                    sections.append(f"    Index: {es_dest.get('IndexName', '?')}")

                # HTTP endpoint destination
                http_dest = dest.get("HttpEndpointDestinationDescription")
                if http_dest:
                    sections.append(f"\n  Destination [{dest_id}]: HTTP Endpoint")
                    ep = http_dest.get("EndpointConfiguration", {})
                    sections.append(f"    URL: {ep.get('Url', '?')}")
                    sections.append(f"    Name: {ep.get('Name', '?')}")

        except Exception as e:
            sections.append(f"  [ERROR] describe_delivery_stream failed: {e}")
            return

        # --- Key metrics ---
        self._append_metrics(cw, stream_name, hours, sections)

        # --- Error logs from CW ---
        if cw_log_group:
            self._append_error_logs(logs_client, cw_log_group, cw_log_stream, hours, sections)

    def _append_metrics(self, cw: Any, stream_name: str, hours: float,
                        sections: list[str]) -> None:
        """Fetch key Firehose metrics from CloudWatch."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        period = max(300, int(hours * 3600 / 12))

        metrics = [
            ("IncomingRecords", "Sum"),
            ("IncomingBytes", "Sum"),
            ("DeliveryToS3.Records", "Sum"),
            ("DeliveryToS3.Success", "Sum"),
            ("DeliveryToS3.DataFreshness", "Maximum"),
            ("ThrottledRecords", "Sum"),
        ]

        try:
            queries = []
            for i, (metric, stat) in enumerate(metrics):
                queries.append({
                    "Id": f"m{i}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/Firehose",
                            "MetricName": metric,
                            "Dimensions": [{"Name": "DeliveryStreamName", "Value": stream_name}],
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
                        label = f"{total:,.0f} (total)"
                        # Highlight throttling
                        if "Throttled" in metric and total > 0:
                            label += " [WARN: THROTTLING]"
                        sections.append(f"    {metric}: {label}")
                    else:
                        max_val = max(vals)
                        if "DataFreshness" in metric:
                            if max_val > 3600:
                                sections.append(f"    {metric}: {max_val / 3600:.1f}h [ALERT: STALE DATA]")
                            elif max_val > 300:
                                sections.append(f"    {metric}: {max_val / 60:.1f}m [WARN: ELEVATED]")
                            else:
                                sections.append(f"    {metric}: {max_val:.0f}s")
                        else:
                            sections.append(f"    {metric}: {max_val:,.0f} (max)")
                else:
                    sections.append(f"    {metric}: no data")

            # Delivery success rate
            incoming_result = next((r for r in resp["MetricDataResults"] if r["Id"] == "m0"), None)
            delivered_result = next((r for r in resp["MetricDataResults"] if r["Id"] == "m2"), None)
            if (incoming_result and incoming_result["Values"] and
                    delivered_result and delivered_result["Values"]):
                total_in = sum(incoming_result["Values"])
                total_out = sum(delivered_result["Values"])
                if total_in > 0:
                    rate = (total_out / total_in) * 100
                    label = f"    Delivery rate: {rate:.1f}%"
                    if rate < 95:
                        label += " [ALERT: LOW DELIVERY RATE]"
                    elif rate < 99:
                        label += " [WARN]"
                    sections.append(label)

        except Exception as e:
            sections.append(f"  [ERROR] Metrics fetch failed: {e}")

    def _append_error_logs(self, logs_client: Any, log_group: str,
                           log_stream: str | None, hours: float,
                           sections: list[str]) -> None:
        """Fetch error logs from the Firehose CW error log group."""
        try:
            sections.append(f"\n  Error logs ({log_group}):")
            start_ms = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000)
            end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

            filter_kwargs: dict[str, Any] = {
                "logGroupName": log_group,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 20,
            }
            if log_stream:
                filter_kwargs["logStreamNames"] = [log_stream]

            resp = logs_client.filter_log_events(**filter_kwargs)
            events = resp.get("events", [])
            if events:
                sections.append(f"    Found {len(events)} error entries:")
                for evt in events[:10]:
                    ts = datetime.fromtimestamp(evt["timestamp"] / 1000, tz=timezone.utc)
                    msg = evt.get("message", "").strip()[:200]
                    sections.append(f"    [{ts:%H:%M:%S}] {msg}")
                if len(events) > 10:
                    sections.append(f"    ... and {len(events) - 10} more")
            else:
                sections.append(f"    No error logs in last {hours}h")

        except Exception as e:
            sections.append(f"  [WARN] Could not fetch error logs: {e}")
