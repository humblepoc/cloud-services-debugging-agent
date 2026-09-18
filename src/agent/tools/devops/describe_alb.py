"""ALB & Target Group discovery tool — hybrid static + dynamic.

Uses YAML config as baseline (known ALBs/TGs per service), plus live
ELBv2 API calls to discover current target group health, registered
targets, and any new/changed target groups.
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


class DescribeALBTool(Tool):
    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}

    def _get_client(self, environment: str | None = None, region: str | None = None) -> Any:
        """Get or create a boto3 ELBv2 client."""
        key = f"{environment or '_default'}:{region or '_default'}"
        if key not in self._clients:
            if environment:
                region_cfg = self._config.get_region_config(environment, region)
                if region_cfg:
                    self._clients[key] = boto3.client("elbv2", **region_cfg.boto3_kwargs())
                else:
                    self._clients[key] = boto3.client("elbv2")
            else:
                self._clients[key] = boto3.client("elbv2")
        return self._clients[key]

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="describe_alb",
            description=(
                "Describe ALB and target group health for a service. "
                "Shows target group states, healthy/unhealthy target counts, "
                "and registered targets. Uses config as baseline and enriches "
                "with live ELBv2 API data. Can also discover all ALBs/TGs in a region."
            ),
            parameters=[
                ToolParameter(
                    name="service",
                    type="string",
                    description="Service name from config (e.g. 'gateway', 'my-service'). "
                                "Resolves ALB/TGs from config then checks live health.",
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
                    description="AWS region (e.g. 'eu-central-1'). Defaults to environment's first region.",
                    required=False,
                ),
                ToolParameter(
                    name="load_balancer_arn",
                    type="string",
                    description="Full ALB ARN for raw lookup (bypasses config).",
                    required=False,
                ),
                ToolParameter(
                    name="target_group_arn",
                    type="string",
                    description="Full Target Group ARN for raw health check.",
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> str:
        if kwargs.get("service"):
            return await asyncio.to_thread(self._describe_service, **kwargs)
        elif kwargs.get("target_group_arn"):
            return await asyncio.to_thread(self._describe_tg_health, **kwargs)
        elif kwargs.get("load_balancer_arn"):
            return await asyncio.to_thread(self._describe_alb_raw, **kwargs)
        else:
            return "[ERROR] Provide 'service', 'load_balancer_arn', or 'target_group_arn'."

    def _describe_service(self, **kwargs: Any) -> str:
        """Describe ALB + TGs for a configured service with live health data."""
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

        alb_cfg = svc.metrics.get_alb(resolved_region or "", resolved_env)
        if not alb_cfg.load_balancer:
            return (f"[INFO] Service '{service_name}' has no ALB configured "
                    f"for region {resolved_region or 'default'}. "
                    f"Use load_balancer_arn parameter for raw lookup.")

        client = self._get_client(environment, region)
        sections: list[str] = []
        sections.append(f"=== ALB Health: {service_name} | Env: {environment} "
                        f"| Region: {resolved_region} ===\n")

        # Get ALB details
        alb_suffix = alb_cfg.load_balancer
        sections.append(f"ALB: {alb_suffix}")

        # Try to get ALB state via describe
        try:
            alb_resp = client.describe_load_balancers()
            matching_alb = None
            for lb in alb_resp.get("LoadBalancers", []):
                arn = lb.get("LoadBalancerArn", "")
                if alb_suffix in arn:
                    matching_alb = lb
                    break
            if matching_alb:
                sections.append(f"  State: {matching_alb.get('State', {}).get('Code', 'unknown')}")
                sections.append(f"  DNS: {matching_alb.get('DNSName', 'N/A')}")
                sections.append(f"  Scheme: {matching_alb.get('Scheme', 'N/A')}")
                sections.append(f"  Type: {matching_alb.get('Type', 'N/A')}")
                alb_arn = matching_alb["LoadBalancerArn"]
            else:
                sections.append(f"  [WARN] ALB not found via describe — using config suffix for TG lookup")
                alb_arn = None
        except Exception as e:
            sections.append(f"  [WARN] Could not describe ALB: {e}")
            alb_arn = None

        sections.append("")

        # Get target groups — from config + discover any new ones
        config_tgs = dict(alb_cfg.target_groups)  # name -> suffix
        sections.append(f"--- Target Groups (config: {len(config_tgs)}) ---")

        # Try to discover all TGs for this ALB
        discovered_tg_arns: list[str] = []
        if alb_arn:
            try:
                tg_resp = client.describe_target_groups(LoadBalancerArn=alb_arn)
                for tg in tg_resp.get("TargetGroups", []):
                    discovered_tg_arns.append(tg["TargetGroupArn"])
                    tg_name = tg.get("TargetGroupName", "unknown")
                    # Check if this TG is in config
                    in_config = any(tg_name in k or k in tg_name for k in config_tgs)
                    tag = "" if in_config else " [NEW - not in config]"
                    sections.append(f"\n  TG: {tg_name}{tag}")
                    sections.append(f"    ARN: {tg['TargetGroupArn']}")
                    sections.append(f"    Protocol: {tg.get('Protocol', 'N/A')} Port: {tg.get('Port', 'N/A')}")
                    sections.append(f"    Health Check: {tg.get('HealthCheckPath', 'N/A')} "
                                    f"({tg.get('HealthCheckProtocol', 'N/A')})")

                    # Get target health
                    self._append_target_health(client, tg["TargetGroupArn"], sections)

                if not tg_resp.get("TargetGroups"):
                    sections.append("  [WARN] No target groups found for this ALB")
            except Exception as e:
                sections.append(f"  [ERROR] Failed to describe target groups: {e}")
                # Fall back to config-based TG lookup
                self._query_config_tgs(client, config_tgs, sections)
        else:
            # No ALB ARN — use config TGs only
            self._query_config_tgs(client, config_tgs, sections)

        return "\n".join(sections)

    def _query_config_tgs(self, client: Any, config_tgs: dict[str, str],
                          sections: list[str]) -> None:
        """Fall back to querying TGs from config suffixes."""
        if not config_tgs:
            sections.append("  No target groups in config")
            return
        for tg_name, tg_suffix in config_tgs.items():
            sections.append(f"\n  TG: {tg_name} (config suffix: {tg_suffix})")
            # Try to find the full ARN
            try:
                all_tgs = client.describe_target_groups()
                for tg in all_tgs.get("TargetGroups", []):
                    if tg_suffix in tg["TargetGroupArn"]:
                        self._append_target_health(client, tg["TargetGroupArn"], sections)
                        break
                else:
                    sections.append(f"    [WARN] TG not found via suffix match")
            except Exception as e:
                sections.append(f"    [ERROR] {e}")

    def _append_target_health(self, client: Any, tg_arn: str,
                              sections: list[str]) -> None:
        """Query and append target health for a single TG."""
        try:
            health_resp = client.describe_target_health(TargetGroupArn=tg_arn)
            targets = health_resp.get("TargetHealthDescriptions", [])
            healthy = sum(1 for t in targets
                         if t.get("TargetHealth", {}).get("State") == "healthy")
            unhealthy = sum(1 for t in targets
                           if t.get("TargetHealth", {}).get("State") == "unhealthy")
            draining = sum(1 for t in targets
                          if t.get("TargetHealth", {}).get("State") == "draining")
            other = len(targets) - healthy - unhealthy - draining

            sections.append(f"    Targets: {len(targets)} total | "
                            f"{healthy} healthy | {unhealthy} unhealthy | "
                            f"{draining} draining | {other} other")

            # Show unhealthy targets in detail
            for t in targets:
                state = t.get("TargetHealth", {}).get("State", "unknown")
                if state != "healthy":
                    target_id = t.get("Target", {}).get("Id", "?")
                    port = t.get("Target", {}).get("Port", "?")
                    reason = t.get("TargetHealth", {}).get("Reason", "")
                    desc = t.get("TargetHealth", {}).get("Description", "")
                    sections.append(f"    [{state.upper()}] {target_id}:{port}"
                                    f"{' - ' + reason if reason else ''}"
                                    f"{' - ' + desc if desc else ''}")
        except Exception as e:
            sections.append(f"    [ERROR] Health check failed: {e}")

    def _describe_tg_health(self, **kwargs: Any) -> str:
        """Describe health of a single target group by ARN."""
        tg_arn: str = kwargs["target_group_arn"]
        environment: str = kwargs.get("environment", "prod")
        region: str | None = kwargs.get("region")

        client = self._get_client(environment, region)
        sections: list[str] = [f"=== Target Group Health ===\nARN: {tg_arn}\n"]
        self._append_target_health(client, tg_arn, sections)
        return "\n".join(sections)

    def _describe_alb_raw(self, **kwargs: Any) -> str:
        """Describe an ALB by full ARN and list all its target groups."""
        alb_arn: str = kwargs["load_balancer_arn"]
        environment: str = kwargs.get("environment", "prod")
        region: str | None = kwargs.get("region")

        client = self._get_client(environment, region)
        sections: list[str] = [f"=== ALB Details ===\nARN: {alb_arn}\n"]

        try:
            tg_resp = client.describe_target_groups(LoadBalancerArn=alb_arn)
            for tg in tg_resp.get("TargetGroups", []):
                sections.append(f"TG: {tg.get('TargetGroupName', 'unknown')}")
                sections.append(f"  ARN: {tg['TargetGroupArn']}")
                sections.append(f"  Protocol: {tg.get('Protocol', 'N/A')} Port: {tg.get('Port', 'N/A')}")
                self._append_target_health(client, tg["TargetGroupArn"], sections)
                sections.append("")
        except Exception as e:
            sections.append(f"[ERROR] {e}")

        return "\n".join(sections)
