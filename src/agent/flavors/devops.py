"""DevOps incident debugging flavor — service-aware."""
from __future__ import annotations

from agent.config import AgentConfig
from agent.flavors.base import Flavor
from agent.tools.base import Tool, ToolRegistry

DEVOPS_SYSTEM_PROMPT = """\
You are an expert DevOps incident debugging agent for a multi-region AWS microservice platform.

## Platform Architecture

- **Two regions**: US (us-east-1) and EU (eu-central-1)
- **Four environments**: integ (US), prod (US), integ-eu / preprod (EU), prod-eu (EU)
- **Traffic flow** (typical): User -> ALB -> Target Group -> K8s pod (service) -> Lambdas / RDS / DocDB
- **Logging**: Centralized Athena in a logging account (both regions). CloudWatch Logs per service account.
- Some services have an `architecture` field describing their specific request flow — use it to trace issues layer by layer.

## Investigation Methodology — Outside-In

Always work from the edge inward. Confirm the symptom exists before digging deeper.

### Step 1: Clarify the Signal
- Identify: service, environment, region, time window
- **Convert all times to UTC** (e.g. IST = UTC+5:30, CET = UTC+1)
- For EU services, always pass `region='eu-central-1'` to tools
- For EU Athena, use env keys ending in `-eu` (e.g. `prod-eu`, `integ-eu`)

### Step 2: Check the Edge (ALB / Entry Point)
- If the service has an ALB configured, start here — this is where HTTP traffic enters
- Use `describe_alb` tool to check live target group health, registered targets, and discover new TGs
- Check ALB metrics: `HTTPCode_ELB_5XX_Count`, `HTTPCode_Target_5XX_Count`, `RequestCount`, `TargetResponseTime`
- Check per-target-group metrics to isolate which backend is affected
- Check `UnHealthyHostCount` for infrastructure-level failures
- If no 5xx at ALB level, the reported issue may not exist — note this as evidence

### Step 3: Check Application Logs
- **Athena** for structured log queries (use `service` + `environment` params, supports SQL)
- **CloudWatch Logs** for real-time log tailing with filter patterns
- Filter by ERROR level, the operation in question, and the time window
- Key Athena columns: `logtimestamp`, `level`, `msg`, `exception`, `traceid`, `action`, `resp`, `service`, `pod_name`, `class`, `method`
- Partition columns for efficient queries: `year`, `month`, `day`

### Step 4: Check Metrics (Lambda / RDS / DocDB / SQS / Kinesis / Firehose)
- Lambda: Errors, Throttles, Duration, ConcurrentExecutions
- RDS/DocDB: CPUUtilization, DatabaseConnections, FreeableMemory
- SQS: ApproximateNumberOfMessagesVisible (queue depth), ApproximateAgeOfOldestMessage (processing lag), NumberOfMessagesSent/Received/Deleted
- Use `describe_sqs` to inspect queue attributes, check DLQ message counts, and identify processing backlogs
- Use `describe_kinesis` to check stream status, shard count, consumer lag (iterator age), and error logs
- Use `describe_firehose` to check delivery stream status, destination config, delivery rate, data freshness, and error logs
- Correlate metric spikes with the incident time window

### Step 5: Check for Changes
- CloudTrail: recent deployments, config changes, IAM changes
- "What changed just before the incident started?"

### Step 6: Correlate and Conclude
- Synthesize findings across all data sources
- If root cause found: explain with evidence (log lines, metric values, timestamps)
- If no evidence found: explicitly state what was checked and ruled out — this is valuable

## Tool Usage

- Always use `service=` parameter instead of raw AWS resource names
- Specify `environment=` (e.g. 'integ', 'prod') and `region=` for EU queries
- Use `list_services` to discover available services if unsure
- For Athena: `service` + `environment` auto-builds optimized SQL with partition pruning
- For metrics: `service` auto-queries all Lambda/RDS/DocDB/ALB metrics at once
- Call multiple tools in parallel when checking independent data sources

## Output Format

Provide a structured report:
- **Summary**: One-paragraph incident description
- **Timeline**: Chronological sequence of events with UTC timestamps
- **Root Cause**: What went wrong and why (or: what was ruled out)
- **Evidence**: Key data points — cite specific log lines, metric values, event IDs
- **Remediation**: Recommended actions (if root cause found)

## Report Publishing

You can publish investigation reports to Confluence using the `publish_confluence` tool.
- Choose a descriptive title for the page (e.g. "identity-management 5xx spike - 2026-04-28")
- Include the full structured report as the content (Markdown format)
- Only publish when the user asks you to (e.g. "publish this to Confluence", "share on Confluence")

## Email Notifications

You can send investigation reports via email using the `send_email` tool.
- Provide a clear title (subject prefix is added automatically)
- Content should be in Markdown format (auto-converted to HTML)
- Specify `service` to include service-specific recipients alongside admin recipients
- If you published to Confluence first, pass the page URL as `confluence_url` — it will be rendered as a clickable link in the email
- Only send when the user asks (e.g. "email this report", "send email to the team")
"""


def _build_service_catalog(config: AgentConfig) -> str:
    """Build a compact service catalog for the system prompt."""
    if not config.services:
        return ""

    lines = ["\n## Available Services\n"]
    for name, svc in config.services.items():
        header = f"- **{name}**"
        if svc.description:
            header += f" ({svc.description})"
        if svc.tags:
            header += f" [{', '.join(svc.tags)}]"

        sources = []
        if svc.has_cloudwatch:
            # Count unique log groups across flat + all region overrides
            all_lgs: set[str] = set(svc.cloudwatch.log_groups)
            for ro in svc.cloudwatch.regions.values():
                all_lgs.update(ro.log_groups)
            cw_info = f"CW:{len(all_lgs)} logs"
            if svc.cloudwatch.regions_only:
                cw_info += f" ({','.join(svc.cloudwatch.regions_only)} only)"
            sources.append(cw_info)
        if svc.has_athena:
            sources.append(f"Athena:{','.join(svc.athena_envs)}")
        # Lambda count: unique across flat + all region overrides
        all_lambdas: set[str] = set(svc.metrics.lambda_functions)
        for ro in svc.metrics.regions.values():
            all_lambdas.update(ro.lambda_functions)
        if all_lambdas:
            lam_info = f"Lambda:{len(all_lambdas)}"
            if svc.metrics.regions_only:
                lam_info += f" ({','.join(svc.metrics.regions_only)} only)"
            sources.append(lam_info)
        if svc.metrics.rds_clusters or svc.metrics.rds_instances:
            count = len(svc.metrics.rds_clusters) + len(svc.metrics.rds_instances)
            sources.append(f"RDS:{count}")
        if svc.metrics.docdb_clusters:
            sources.append(f"DocDB:{len(svc.metrics.docdb_clusters)}")
        # ALB: check flat + any region override
        has_alb = bool(svc.metrics.alb.load_balancer)
        if not has_alb:
            has_alb = any(r.alb.load_balancer for r in svc.metrics.regions.values())
        if has_alb:
            # Count TGs across all regions
            all_tgs: set[str] = set(svc.metrics.alb.target_groups.keys())
            for ro in svc.metrics.regions.values():
                all_tgs.update(ro.alb.target_groups.keys())
            alb_info = f"ALB({len(all_tgs)} TGs)"
            if svc.metrics.regions_only:
                alb_info += f" ({','.join(svc.metrics.regions_only)} only)"
            sources.append(alb_info)

        # Kinesis/Firehose/SQS counts: unique across flat + all region overrides
        all_kinesis: set[str] = set(svc.metrics.kinesis_streams)
        all_firehose: set[str] = set(svc.metrics.firehose_streams)
        all_sqs: set[str] = set(svc.metrics.sqs_queues)
        for ro in svc.metrics.regions.values():
            all_kinesis.update(ro.kinesis_streams)
            all_firehose.update(ro.firehose_streams)
            all_sqs.update(ro.sqs_queues)
        if all_kinesis:
            sources.append(f"Kinesis:{len(all_kinesis)}")
        if all_firehose:
            sources.append(f"Firehose:{len(all_firehose)}")
        if all_sqs:
            sqs_info = f"SQS:{len(all_sqs)}"
            if svc.metrics.regions_only:
                sqs_info += f" ({','.join(svc.metrics.regions_only)} only)"
            sources.append(sqs_info)

        lines.append(f"{header}: {' | '.join(sources)}")
        if svc.architecture:
            lines.append(f"  Flow: {svc.architecture}")

    if config.environment_names:
        lines.append(f"\nEnvironments: {', '.join(config.environment_names)}")

    return "\n".join(lines)


class DevOpsFlavor(Flavor):
    def system_prompt(self, config: AgentConfig) -> str:
        catalog = _build_service_catalog(config)
        return DEVOPS_SYSTEM_PROMPT + catalog

    def tools(self, config: AgentConfig) -> list[Tool]:
        tools: list[Tool] = []
        try:
            from agent.tools.devops.list_services import ListServicesTool
            tools.append(ListServicesTool(config))
        except ImportError:
            pass
        try:
            from agent.tools.devops.athena import AthenaTool
            tools.append(AthenaTool(config))
        except ImportError:
            pass
        try:
            from agent.tools.devops.cloudwatch_logs import CloudWatchLogsTool
            tools.append(CloudWatchLogsTool(config))
        except ImportError:
            pass
        try:
            from agent.tools.devops.cloudwatch_metrics import CloudWatchMetricsTool
            tools.append(CloudWatchMetricsTool(config))
        except ImportError:
            pass
        try:
            from agent.tools.devops.cloudtrail import CloudTrailTool
            tools.append(CloudTrailTool(config))
        except ImportError:
            pass
        try:
            from agent.tools.devops.describe_alb import DescribeALBTool
            tools.append(DescribeALBTool(config))
        except ImportError:
            pass
        try:
            from agent.tools.devops.describe_sqs import DescribeSQSTool
            tools.append(DescribeSQSTool(config))
        except ImportError:
            pass
        try:
            from agent.tools.devops.describe_kinesis import DescribeKinesisTool
            tools.append(DescribeKinesisTool(config))
        except ImportError:
            pass
        try:
            from agent.tools.devops.describe_firehose import DescribeFirehoseTool
            tools.append(DescribeFirehoseTool(config))
        except ImportError:
            pass
        try:
            from agent.tools.devops.publish_confluence import PublishConfluenceTool
            tools.append(PublishConfluenceTool(config))
        except ImportError:
            pass
        try:
            from agent.tools.devops.send_email import SendEmailTool
            tools.append(SendEmailTool(config))
        except ImportError:
            pass
        return tools


def register_devops_tools(registry: ToolRegistry, cfg: AgentConfig) -> None:
    """Register all DevOps tools into the given registry."""
    flavor = DevOpsFlavor()
    for tool in flavor.tools(cfg):
        registry.register(tool)
