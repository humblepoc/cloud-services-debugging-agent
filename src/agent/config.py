"""Configuration loader — reads config.yaml and exposes typed objects.

Design: Services are the first-class organizing concept. Each service can have
its own Athena table (with custom column mappings), CloudWatch log groups,
Lambda functions, and RDS/DocDB clusters. Accounts and credentials are
defined separately and referenced by environment.

Inspired by AL Observer's config system, adapted for the agent use case.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import boto3
import yaml
from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)

# Cache for STS assumed-role credentials: (role_arn, region) -> (creds_dict, expiry_epoch)
_sts_cache: dict[tuple[str, str], tuple[dict[str, str], float]] = {}
_STS_REFRESH_MARGIN = 300  # refresh 5 min before expiry


def _assume_role(role_arn: str, region: str, session_name: str = "devops-agent") -> dict[str, str]:
    """Assume an IAM role via STS, returning temporary credentials.

    Results are cached and reused until close to expiry.
    """
    cache_key = (role_arn, region)
    cached = _sts_cache.get(cache_key)
    if cached:
        creds, expiry = cached
        if time.time() < expiry - _STS_REFRESH_MARGIN:
            return creds

    log.info("Assuming role %s in %s", role_arn, region)
    sts = boto3.client("sts", region_name=region)
    resp = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name, DurationSeconds=3600)
    rc = resp["Credentials"]
    creds = {
        "aws_access_key_id": rc["AccessKeyId"],
        "aws_secret_access_key": rc["SecretAccessKey"],
        "aws_session_token": rc["SessionToken"],
    }
    expiry = rc["Expiration"].timestamp()
    _sts_cache[cache_key] = (creds, expiry)
    return creds


def _exe_dir() -> Path:
    """Return the install directory containing config/cred files.

    When frozen (PyInstaller), the exe lives in a ``bin/`` subfolder
    (e.g. ``~/.devops-agent/bin/devops-agent.exe``), so we go up one
    level to reach the install dir (``~/.devops-agent/``).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.parent
    return Path.cwd()


# ---------------------------------------------------------------------------
# Credentials & Accounts
# ---------------------------------------------------------------------------
class RegionConfig(BaseModel):
    """AWS credentials for a specific region within an environment."""
    name: str = "us-east-1"
    account_id: str = ""
    role_name: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""

    def boto3_kwargs(self) -> dict[str, str]:
        """Return kwargs suitable for boto3.client().

        Priority:
        1. Explicit credentials (from creds file / env vars)
        2. STS AssumeRole (if account_id + role_name are set)
        3. Default boto3 credential chain (profile, env, instance role)
        """
        kw: dict[str, str] = {"region_name": self.name}
        if self.aws_access_key_id:
            kw["aws_access_key_id"] = self.aws_access_key_id
            kw["aws_secret_access_key"] = self.aws_secret_access_key
            if self.aws_session_token:
                kw["aws_session_token"] = self.aws_session_token
        elif self.account_id and self.role_name:
            role_arn = f"arn:aws:iam::{self.account_id}:role/{self.role_name}"
            creds = _assume_role(role_arn, self.name)
            kw.update(creds)
        return kw


class EnvironmentConfig(BaseModel):
    """An environment (e.g. integ, prod) with its AWS regions."""
    regions: list[RegionConfig] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Logging account (Athena lives here — separate AWS account)
# ---------------------------------------------------------------------------
class AthenaDefaults(BaseModel):
    """Global Athena defaults — can be overridden per service."""
    model_config = {"extra": "ignore"}
    workgroup: str = "primary"
    output_location: str = ""

class LoggingAccountRegionConfig(BaseModel):
    """Athena config for a specific region in the logging account."""
    athena: AthenaDefaults = Field(default_factory=AthenaDefaults)

class LoggingAccountConfig(BaseModel):
    """Credentials for the centralized logging/Athena account."""
    account_id: str = ""
    role_name: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""
    # Legacy single-region fields (kept for backward compat)
    region: str = "us-east-1"
    athena: AthenaDefaults = Field(default_factory=AthenaDefaults)
    # Multi-region support
    regions: dict[str, LoggingAccountRegionConfig] = Field(default_factory=dict)

    def boto3_kwargs(self, region: str | None = None) -> dict[str, str]:
        """Return kwargs suitable for boto3.client(). If region specified, use that.

        Priority:
        1. Explicit credentials (from creds file / env vars)
        2. STS AssumeRole (if account_id + role_name are set)
        3. Default boto3 credential chain
        """
        r = region or self.region
        kw: dict[str, str] = {"region_name": r}
        if self.aws_access_key_id:
            kw["aws_access_key_id"] = self.aws_access_key_id
            kw["aws_secret_access_key"] = self.aws_secret_access_key
            if self.aws_session_token:
                kw["aws_session_token"] = self.aws_session_token
        elif self.account_id and self.role_name:
            role_arn = f"arn:aws:iam::{self.account_id}:role/{self.role_name}"
            creds = _assume_role(role_arn, r)
            kw.update(creds)
        return kw

    def get_athena_config(self, region: str | None = None) -> AthenaDefaults:
        """Get Athena config for a specific region, falling back to default."""
        r = region or self.region
        if r in self.regions:
            return self.regions[r].athena
        return self.athena


# ---------------------------------------------------------------------------
# Per-service data source configurations
# ---------------------------------------------------------------------------
class ServiceAthenaConfig(BaseModel):
    """Athena configuration for a specific service + environment."""
    enabled: bool = True
    database: str = ""
    table: str = ""
    output_location: str = ""  # override global default


class CloudWatchRegionOverride(BaseModel):
    """Per-region override for CloudWatch log groups."""
    log_groups: list[str] = Field(default_factory=list)


class CloudWatchEnvironmentConfig(BaseModel):
    """Per-environment override for CloudWatch config (contains region overrides)."""
    regions: dict[str, CloudWatchRegionOverride] = Field(default_factory=dict)


class ServiceCloudWatchConfig(BaseModel):
    """CloudWatch Logs configuration for a service."""
    enabled: bool = True
    log_groups: list[str] = Field(default_factory=list)
    regions_only: list[str] = Field(default_factory=list)  # empty = all regions
    regions: dict[str, CloudWatchRegionOverride] = Field(default_factory=dict)
    environments: dict[str, CloudWatchEnvironmentConfig] = Field(default_factory=dict)

    def get_log_groups(self, region: str, environment: str = "") -> list[str]:
        """Return log groups for a region+environment.

        Lookup order:
        1. environments[env].regions[region]  (env+region specific)
        2. regions[region]                    (region-specific, shared across envs)
        3. flat log_groups                    (default)
        """
        if environment and environment in self.environments:
            env_cfg = self.environments[environment]
            if region in env_cfg.regions and env_cfg.regions[region].log_groups:
                return env_cfg.regions[region].log_groups
        if region in self.regions and self.regions[region].log_groups:
            return self.regions[region].log_groups
        return self.log_groups


class ALBConfig(BaseModel):
    """ALB and target group configuration for a service."""
    load_balancer: str = ""  # e.g. "app/k8s-myservice-abc123/deadbeef1234"
    target_groups: dict[str, str] = Field(default_factory=dict)  # name -> suffix

class MetricsRegionOverride(BaseModel):
    """Per-region override for metrics resources."""
    lambda_functions: list[str] = Field(default_factory=list)
    rds_clusters: list[str] = Field(default_factory=list)
    rds_instances: list[str] = Field(default_factory=list)
    docdb_clusters: list[str] = Field(default_factory=list)
    kinesis_streams: list[str] = Field(default_factory=list)
    firehose_streams: list[str] = Field(default_factory=list)
    sqs_queues: list[str] = Field(default_factory=list)
    alb: ALBConfig = Field(default_factory=ALBConfig)


class MetricsEnvironmentConfig(BaseModel):
    """Per-environment override for metrics config (contains region overrides)."""
    regions: dict[str, MetricsRegionOverride] = Field(default_factory=dict)


class ServiceMetricsConfig(BaseModel):
    """CloudWatch Metrics configuration for a service."""
    enabled: bool = True
    lambda_functions: list[str] = Field(default_factory=list)
    rds_clusters: list[str] = Field(default_factory=list)
    rds_instances: list[str] = Field(default_factory=list)
    docdb_clusters: list[str] = Field(default_factory=list)
    kinesis_streams: list[str] = Field(default_factory=list)
    firehose_streams: list[str] = Field(default_factory=list)
    sqs_queues: list[str] = Field(default_factory=list)
    alb: ALBConfig = Field(default_factory=ALBConfig)
    regions_only: list[str] = Field(default_factory=list)  # empty = all regions
    regions: dict[str, MetricsRegionOverride] = Field(default_factory=dict)
    environments: dict[str, MetricsEnvironmentConfig] = Field(default_factory=dict)

    def _env_region(self, environment: str, region: str) -> MetricsRegionOverride | None:
        """Get environment+region override if it exists and has data."""
        if environment and environment in self.environments:
            return self.environments[environment].regions.get(region)
        return None

    def get_lambda_functions(self, region: str, environment: str = "") -> list[str]:
        er = self._env_region(environment, region)
        if er and er.lambda_functions:
            return er.lambda_functions
        if region in self.regions and self.regions[region].lambda_functions:
            return self.regions[region].lambda_functions
        return self.lambda_functions

    def get_rds_clusters(self, region: str, environment: str = "") -> list[str]:
        er = self._env_region(environment, region)
        if er and er.rds_clusters:
            return er.rds_clusters
        if region in self.regions and self.regions[region].rds_clusters:
            return self.regions[region].rds_clusters
        return self.rds_clusters

    def get_rds_instances(self, region: str, environment: str = "") -> list[str]:
        er = self._env_region(environment, region)
        if er and er.rds_instances:
            return er.rds_instances
        if region in self.regions and self.regions[region].rds_instances:
            return self.regions[region].rds_instances
        return self.rds_instances

    def get_docdb_clusters(self, region: str, environment: str = "") -> list[str]:
        er = self._env_region(environment, region)
        if er and er.docdb_clusters:
            return er.docdb_clusters
        if region in self.regions and self.regions[region].docdb_clusters:
            return self.regions[region].docdb_clusters
        return self.docdb_clusters

    def get_kinesis_streams(self, region: str, environment: str = "") -> list[str]:
        er = self._env_region(environment, region)
        if er and er.kinesis_streams:
            return er.kinesis_streams
        if region in self.regions and self.regions[region].kinesis_streams:
            return self.regions[region].kinesis_streams
        return self.kinesis_streams

    def get_firehose_streams(self, region: str, environment: str = "") -> list[str]:
        er = self._env_region(environment, region)
        if er and er.firehose_streams:
            return er.firehose_streams
        if region in self.regions and self.regions[region].firehose_streams:
            return self.regions[region].firehose_streams
        return self.firehose_streams

    def get_sqs_queues(self, region: str, environment: str = "") -> list[str]:
        er = self._env_region(environment, region)
        if er and er.sqs_queues:
            return er.sqs_queues
        if region in self.regions and self.regions[region].sqs_queues:
            return self.regions[region].sqs_queues
        return self.sqs_queues

    def get_alb(self, region: str, environment: str = "") -> ALBConfig:
        er = self._env_region(environment, region)
        if er and er.alb and er.alb.load_balancer:
            return er.alb
        if region in self.regions and self.regions[region].alb and self.regions[region].alb.load_balancer:
            return self.regions[region].alb
        return self.alb


# Keys that indicate a legacy flat ServiceAthenaConfig dict (vs per-env dict)
_ATHENA_FLAT_KEYS = frozenset({
    "database", "table", "enabled", "output_location",
})


class ServiceConfig(BaseModel):
    """A single service's observability configuration."""
    description: str = ""
    architecture: str = ""  # Free-text request flow, e.g. "ALB → TG → K8s pod → DB"
    athena: dict[str, ServiceAthenaConfig] = Field(default_factory=dict)
    cloudwatch: ServiceCloudWatchConfig = Field(default_factory=ServiceCloudWatchConfig)
    metrics: ServiceMetricsConfig = Field(default_factory=ServiceMetricsConfig)
    tags: list[str] = Field(default_factory=list)
    email_recipients: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_athena_config(cls, values: Any) -> Any:
        """Normalize athena field to dict[str, ServiceAthenaConfig].

        Handles three input shapes:
        1. A ServiceAthenaConfig instance -> wrap as {"_default": instance}
        2. A flat dict with known config keys (legacy) -> wrap as {"_default": dict}
        3. A dict of env-name -> config dicts (per-env) -> already correct
        """
        if not isinstance(values, dict):
            return values

        raw = values.get("athena")
        if raw is None:
            return values

        if isinstance(raw, ServiceAthenaConfig):
            values["athena"] = {"_default": raw}
        elif isinstance(raw, dict) and raw.keys() & _ATHENA_FLAT_KEYS:
            values["athena"] = {"_default": raw}

        return values

    def athena_env(self, env: str) -> ServiceAthenaConfig:
        """Get the Athena config for a specific environment.

        Falls back to '_default' (legacy single-block), then returns
        a disabled config if nothing matches.
        """
        if env in self.athena:
            return self.athena[env]
        if "_default" in self.athena:
            return self.athena["_default"]
        return ServiceAthenaConfig(enabled=False)

    @property
    def athena_envs(self) -> list[str]:
        """Return list of configured environment keys."""
        keys = [k for k in self.athena if k != "_default"]
        if keys:
            return keys
        if "_default" in self.athena:
            return ["_default"]
        return []

    @property
    def has_cloudwatch(self) -> bool:
        if not self.cloudwatch.enabled:
            return False
        if self.cloudwatch.log_groups:
            return True
        # Check if any region override has log groups
        if any(r.log_groups for r in self.cloudwatch.regions.values()):
            return True
        # Check if any environment override has log groups
        return any(
            r.log_groups
            for env in self.cloudwatch.environments.values()
            for r in env.regions.values()
        )

    def has_cloudwatch_in_region(self, region: str | None) -> bool:
        """Check if CW log groups are available in the given region."""
        if not self.cloudwatch.enabled:
            return False
        if self.cloudwatch.regions_only:
            if region is not None and region not in self.cloudwatch.regions_only:
                return False
        # Check if there are log groups for this region
        if region is not None:
            return bool(self.cloudwatch.get_log_groups(region))
        return self.has_cloudwatch

    @property
    def has_athena(self) -> bool:
        return bool(self.athena)

    @property
    def has_metrics(self) -> bool:
        m = self.metrics
        if not m.enabled:
            return False
        if (m.lambda_functions or m.rds_clusters or m.rds_instances or m.docdb_clusters
                or m.kinesis_streams or m.firehose_streams or m.sqs_queues or m.alb.load_balancer):
            return True
        # Check if any region override has resources
        if any(
            r.lambda_functions or r.rds_clusters or r.rds_instances or r.docdb_clusters
            or r.kinesis_streams or r.firehose_streams or r.sqs_queues or r.alb.load_balancer
            for r in m.regions.values()
        ):
            return True
        # Check if any environment override has resources
        return any(
            r.lambda_functions or r.rds_clusters or r.rds_instances or r.docdb_clusters
            or r.kinesis_streams or r.firehose_streams or r.sqs_queues or r.alb.load_balancer
            for env in m.environments.values()
            for r in env.regions.values()
        )

    def has_metrics_in_region(self, region: str | None) -> bool:
        """Check if metrics resources are available in the given region."""
        if not self.metrics.enabled:
            return False
        if self.metrics.regions_only:
            if region is not None and region not in self.metrics.regions_only:
                return False
        if region is not None:
            m = self.metrics
            r_override = m.regions.get(region)
            has_flat = bool(m.lambda_functions or m.rds_clusters or m.rds_instances or m.docdb_clusters
                            or m.kinesis_streams or m.firehose_streams or m.sqs_queues or m.alb.load_balancer)
            has_override = r_override is not None and bool(
                r_override.lambda_functions or r_override.rds_clusters or r_override.rds_instances
                or r_override.docdb_clusters or r_override.kinesis_streams or r_override.firehose_streams
                or r_override.sqs_queues or r_override.alb.load_balancer
            )
            return has_flat or has_override
        return self.has_metrics


# ---------------------------------------------------------------------------
# LLM configuration
# ---------------------------------------------------------------------------

# Per-provider defaults for model and base_url.  Used by apply_provider_defaults()
# to fill in empty values so users only need to set ``provider`` in YAML.
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "custom":     {"model": "",                          "base_url": ""},
    "anthropic":  {"model": "claude-sonnet-4-6@default", "base_url": ""},
    "openai":     {"model": "",                          "base_url": ""},
    "gemini":     {"model": "",                          "base_url": ""},
    "copilot":    {"model": "claude-sonnet-4",           "base_url": ""},
}


def apply_provider_defaults(llm: "LLMConfig") -> None:
    """Fill in model/base_url defaults for the selected provider.

    Only fills values that are empty, so any explicit YAML / env / CLI
    setting always wins.  Safe to call repeatedly.  No-op for providers
    without an entry in PROVIDER_DEFAULTS.
    """
    defaults = PROVIDER_DEFAULTS.get(llm.provider, {})
    if not llm.model:
        llm.model = defaults.get("model", "")
    if not llm.base_url:
        llm.base_url = defaults.get("base_url", "")


class LLMConfig(BaseModel):
    provider: str = "custom"
    model: str = ""              # resolved by apply_provider_defaults()
    base_url: str = ""           # resolved by apply_provider_defaults()
    api_key: str = ""            # LLM gateway key
    custom_api_key: str = ""     # Custom gateway key; falls back to api_key
    pat: str = ""                # GitHub PAT with copilot scope (only used by copilot provider)
    temperature: float = 0.0
    max_tokens: int = 4096


# ---------------------------------------------------------------------------
# Confluence publishing config
# ---------------------------------------------------------------------------
class EmailConfig(BaseModel):
    """Configuration for sending email notifications via notification service."""
    notify_url: str = ""
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    from_application: str = "Debugging-Agent"
    sender: str = "Debugging Agent"
    subject_prefix: str = "[Debugging Agent]"
    admin_email: str = ""  # BCC'd on all emails

    @model_validator(mode="before")
    @classmethod
    def _coerce_none(cls, values: Any) -> Any:
        if isinstance(values, dict):
            for k, v in values.items():
                if v is None:
                    values[k] = ""
        return values


class ConfluenceConfig(BaseModel):
    """Configuration for publishing reports to Confluence Cloud."""
    url: str = "https://fdsone.atlassian.net"
    space_key: str = "X"
    parent_page_title: str = "Alerts RCA"
    parent_page_id: str = ""
    user_email: str = ""
    api_token: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_none(cls, values: Any) -> Any:
        if isinstance(values, dict):
            for k, v in values.items():
                if v is None:
                    values[k] = ""
        return values


class LambdaConfig(BaseModel):
    """Configuration for Lambda execution mode (PagerDuty webhook handler)."""
    auto_email: bool = True
    auto_publish: bool = True
    # Delay (seconds) between receiving the PagerDuty webhook and starting the
    # investigation. Enforced via SQS per-message DelaySeconds. Clamped to the
    # SQS-supported range [0, 900] at the use site (see lambda_handler._resolve_delay).
    delay_seconds: int = 300


# ---------------------------------------------------------------------------
# Top-level application config
# ---------------------------------------------------------------------------
class AgentConfig(BaseModel):
    model_config = {"populate_by_name": True}

    llm: LLMConfig = Field(default_factory=LLMConfig)
    environments: dict[str, EnvironmentConfig] = Field(default_factory=dict)
    logging_account: LoggingAccountConfig = Field(default_factory=LoggingAccountConfig)
    services: dict[str, ServiceConfig] = Field(default_factory=dict)
    confluence: ConfluenceConfig = Field(default_factory=ConfluenceConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    lambda_config: LambdaConfig = Field(default_factory=LambdaConfig, alias="lambda")
    flavor: str = "devops"
    max_iterations: int = 25
    log_level: str = "INFO"

    def get_services(
        self,
        name_filter: str | None = None,
        tag_filter: str | None = None,
    ) -> dict[str, ServiceConfig]:
        """Get services, optionally filtered by name or tag (comma-separated)."""
        result = dict(self.services)
        if name_filter:
            names = {n.strip().lower() for n in name_filter.split(",")}
            result = {k: v for k, v in result.items() if k.lower() in names}
        if tag_filter:
            tags = {t.strip().lower() for t in tag_filter.split(",")}
            result = {k: v for k, v in result.items()
                      if any(t.lower() in tags for t in v.tags)}
        return result

    def list_services(self) -> list[dict[str, Any]]:
        """Return a summary of all configured services for display."""
        out = []
        for name, svc in self.services.items():
            athena_cfg = svc.athena_env("_default")
            if not athena_cfg.table and svc.athena_envs:
                athena_cfg = svc.athena_env(svc.athena_envs[0])
            out.append({
                "name": name,
                "description": svc.description,
                "tags": svc.tags,
                "athena_envs": svc.athena_envs,
                "athena_table": athena_cfg.table or "-",
                "cw_log_groups": svc.cloudwatch.log_groups,
                "cw_regions_only": svc.cloudwatch.regions_only,
                "lambda_functions": svc.metrics.lambda_functions,
                "rds_clusters": svc.metrics.rds_clusters,
                "rds_instances": svc.metrics.rds_instances,
                "docdb_clusters": svc.metrics.docdb_clusters,
                "alb": svc.metrics.alb.load_balancer or "-",
                "target_groups": list(svc.metrics.alb.target_groups.keys()),
                "metrics_regions_only": svc.metrics.regions_only,
            })
        return out

    # Known EU environment suffixes → region mapping
    _REGION_SUFFIXES: dict[str, str] = {
        "-eu": "eu-central-1",
        "-euc1": "eu-central-1",
        "-ap": "ap-northeast-1",
    }

    def resolve_environment(self, environment: str) -> tuple[str, str | None]:
        """Resolve a compound environment name like 'prod-eu' into (env, region).

        Returns (environment_name, region) where environment_name is a key in
        self.environments and region is an AWS region string or None.

        Examples:
            'prod'     -> ('prod', None)              # default region
            'prod-eu'  -> ('prod', 'eu-central-1')
            'integ-eu' -> ('integ', 'eu-central-1')
            'prod-ap'  -> ('prod', 'ap-northeast-1')
        """
        # Direct match — return as-is
        if environment in self.environments:
            return (environment, None)

        # Try stripping region suffixes
        for suffix, region in self._REGION_SUFFIXES.items():
            if environment.endswith(suffix):
                base = environment[:-len(suffix)]
                if base in self.environments:
                    return (base, region)

        # No match — return as-is and let caller handle the error
        return (environment, None)

    def get_region_config(self, environment: str, region: str | None = None) -> RegionConfig | None:
        """Get region config for an environment. Auto-resolves compound names like 'prod-eu'."""
        # Auto-resolve compound environment names
        resolved_env, resolved_region = self.resolve_environment(environment)
        if region is None:
            region = resolved_region

        env = self.environments.get(resolved_env)
        if not env or not env.regions:
            return None
        if region:
            for rc in env.regions:
                if rc.name == region:
                    return rc
            return None
        return env.regions[0]

    @property
    def environment_names(self) -> list[str]:
        return list(self.environments.keys())


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
def _config_search_paths() -> list[Path]:
    """Config search paths: cwd → exe dir → ~/.devops-agent/."""
    paths = [Path("config.yaml"), Path("config.yml")]
    exe = _exe_dir()
    if exe != Path.cwd():
        paths.append(exe / "config.yaml")
        paths.append(exe / "config.yml")
    paths.append(Path.home() / ".devops-agent" / "config.yaml")
    return paths


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base. Override values win."""
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val
    return base


def _find_local_config(config_path: Path | None) -> Path | None:
    """Find config.local.yaml alongside the main config, or in search paths."""
    if config_path is not None:
        local = config_path.parent / "config.local.yaml"
        if local.exists():
            return local
    # Search standard paths
    search = [
        Path("config.local.yaml"),
        _exe_dir() / "config.local.yaml",
        Path.home() / ".devops-agent" / "config.local.yaml",
    ]
    return next((f for f in search if f.exists()), None)


def load_config(config_path: str | None = None) -> AgentConfig:
    """Load config from YAML file, then overlay environment variables."""
    raw: dict[str, Any] = {}

    # Find and load YAML
    if config_path:
        p = Path(config_path)
    else:
        env_path = os.environ.get("AGENT_CONFIG")
        if env_path:
            p = Path(env_path)
        else:
            p = next((f for f in _config_search_paths() if f.exists()), None)  # type: ignore

    if p is not None and p.exists():
        with open(p) as f:
            raw = yaml.safe_load(f) or {}

    # Merge config.local.yaml overrides (user-specific, never auto-updated)
    local_p = _find_local_config(p)
    if local_p is not None:
        with open(local_p) as f:
            local_raw = yaml.safe_load(f) or {}
        if local_raw:
            _deep_merge(raw, local_raw)

    cfg = AgentConfig(**raw)

    # Environment variable overrides (highest priority, LLM settings)
    _apply_env(cfg)

    # Build-time LLM key (lowest priority — YAML/env overrides win)
    _apply_build_llm(cfg)

    # Resolve provider-specific model/base_url defaults for empty values
    apply_provider_defaults(cfg.llm)

    # Auto-load credential paste files if they exist
    _apply_cred_files(cfg)

    # Build-time Confluence defaults (lowest priority — YAML/local overrides win)
    _apply_build_confluence(cfg)

    # Build-time email defaults (lowest priority — YAML/local overrides win)
    _apply_build_email(cfg)

    return cfg


def _apply_env(cfg: AgentConfig) -> None:
    """Apply environment variable overrides (highest priority — wins over YAML and build-time).

    Supported variables:
        LLM:          AGENT_API_KEY, AGENT_CUSTOM_API_KEY, AGENT_LLM_PROVIDER, AGENT_LLM_MODEL, AGENT_BASE_URL
        Agent:        AGENT_FLAVOR, AGENT_MAX_ITERATIONS, AGENT_LOG_LEVEL
        Confluence:   AGENT_CONFLUENCE_USER_EMAIL, AGENT_CONFLUENCE_API_TOKEN
        Email/notify:   AGENT_NOTIFY_URL, AGENT_NOTIFY_TOKEN_URL, AGENT_NOTIFY_CLIENT_ID,
                      AGENT_NOTIFY_CLIENT_SECRET, AGENT_NOTIFY_ADMIN_EMAIL
    """
    if v := os.environ.get("AGENT_API_KEY"):
        cfg.llm.api_key = v
    if v := os.environ.get("AGENT_CUSTOM_API_KEY"):
        cfg.llm.custom_api_key = v
    if v := os.environ.get("AGENT_LLM_PROVIDER"):
        cfg.llm.provider = v
    if v := os.environ.get("AGENT_LLM_MODEL"):
        cfg.llm.model = v
    if v := os.environ.get("AGENT_BASE_URL"):
        cfg.llm.base_url = v
    if v := os.environ.get("AGENT_FLAVOR"):
        cfg.flavor = v
    if v := os.environ.get("AGENT_MAX_ITERATIONS"):
        cfg.max_iterations = int(v)
    if v := os.environ.get("AGENT_LOG_LEVEL"):
        cfg.log_level = v

    # Confluence
    if v := os.environ.get("AGENT_CONFLUENCE_USER_EMAIL"):
        cfg.confluence.user_email = v
    if v := os.environ.get("AGENT_CONFLUENCE_API_TOKEN"):
        cfg.confluence.api_token = v

    # Email / notification
    if v := os.environ.get("AGENT_NOTIFY_URL"):
        cfg.email.notify_url = v
    if v := os.environ.get("AGENT_NOTIFY_TOKEN_URL"):
        cfg.email.token_url = v
    if v := os.environ.get("AGENT_NOTIFY_CLIENT_ID"):
        cfg.email.client_id = v
    if v := os.environ.get("AGENT_NOTIFY_CLIENT_SECRET"):
        cfg.email.client_secret = v
    if v := os.environ.get("AGENT_NOTIFY_ADMIN_EMAIL"):
        cfg.email.admin_email = v


def _apply_build_llm(cfg: AgentConfig) -> None:
    """Apply build-time LLM key defaults (lowest priority — YAML/env overrides win)."""
    try:
        from agent._build_info import CUSTOM_API_KEY
        if not cfg.llm.custom_api_key:
            cfg.llm.custom_api_key = CUSTOM_API_KEY
    except (ImportError, AttributeError):
        pass


def _apply_build_confluence(cfg: AgentConfig) -> None:
    """Apply build-time Confluence defaults (lowest priority — YAML overrides win)."""
    try:
        from agent._build_info import CONFLUENCE_API_TOKEN, CONFLUENCE_USER_EMAIL
        if not cfg.confluence.api_token:
            cfg.confluence.api_token = CONFLUENCE_API_TOKEN
        if not cfg.confluence.user_email:
            cfg.confluence.user_email = CONFLUENCE_USER_EMAIL
    except (ImportError, AttributeError):
        pass


def _apply_build_email(cfg: AgentConfig) -> None:
    """Apply build-time email defaults (lowest priority — YAML overrides win)."""
    try:
        from agent._build_info import (
            NOTIFY_CLIENT_ID, NOTIFY_CLIENT_SECRET, NOTIFY_URL, NOTIFY_TOKEN_URL, NOTIFY_ADMIN_EMAIL,
        )
        if not cfg.email.client_id:
            cfg.email.client_id = NOTIFY_CLIENT_ID
        if not cfg.email.client_secret:
            cfg.email.client_secret = NOTIFY_CLIENT_SECRET
        if not cfg.email.notify_url:
            cfg.email.notify_url = NOTIFY_URL
        if not cfg.email.token_url:
            cfg.email.token_url = NOTIFY_TOKEN_URL
        if not cfg.email.admin_email:
            cfg.email.admin_email = NOTIFY_ADMIN_EMAIL
    except (ImportError, AttributeError):
        pass


# ---------------------------------------------------------------------------
# Credential paste files — auto-load creds_logging.txt / creds_service.txt
# ---------------------------------------------------------------------------
def _cred_search_paths(filename: str) -> list[Path]:
    """Credential file search paths: ~/.devops-agent/ → exe dir → cwd."""
    install = Path.home() / ".devops-agent"
    paths = [install / filename]
    exe = _exe_dir()
    if exe.resolve() != install.resolve():
        paths.append(exe / filename)
    if Path.cwd().resolve() not in (exe.resolve(), install.resolve()):
        paths.append(Path.cwd() / filename)
    return paths


def _apply_cred_files(cfg: AgentConfig) -> None:
    """Read creds_logging.txt and creds_service.txt, apply to config."""
    from agent.credentials import parse_credentials

    # --- Logging account creds ---
    logging_file = next((f for f in _cred_search_paths("creds_logging.txt") if f.exists()), None)
    if logging_file:
        text = logging_file.read_text(encoding="utf-8")
        creds = parse_credentials(text)
        if creds.get("access_key_id"):
            cfg.logging_account.aws_access_key_id = creds["access_key_id"]
            cfg.logging_account.aws_secret_access_key = creds.get("secret_access_key", "")
            cfg.logging_account.aws_session_token = creds.get("session_token", "")

    # --- Service account creds (applied to ALL environments) ---
    service_file = next((f for f in _cred_search_paths("creds_service.txt") if f.exists()), None)
    if service_file:
        text = service_file.read_text(encoding="utf-8")
        creds = parse_credentials(text)
        if creds.get("access_key_id"):
            for env_name, env_cfg in cfg.environments.items():
                for region in env_cfg.regions:
                    region.aws_access_key_id = creds["access_key_id"]
                    region.aws_secret_access_key = creds.get("secret_access_key", "")
                    region.aws_session_token = creds.get("session_token", "")
