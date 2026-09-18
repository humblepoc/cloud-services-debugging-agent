"""Validate config.yaml structure, indentation, and required fields.

Runs in CI pipeline to catch misconfigurations before deployment.
Exit code 0 = pass, 1 = validation errors.
"""
from __future__ import annotations

import re
import sys

import yaml

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

LIST_METRIC_FIELDS = (
    "lambda_functions",
    "rds_clusters",
    "docdb_clusters",
    "sqs_queues",
    "kinesis_streams",
    "firehose_streams",
)


def _validate_alb(alb: dict, prefix: str, errors: list[str]) -> None:
    """Validate an ALB config block (load_balancer must be a string if present, target_groups a mapping)."""
    lb = alb.get("load_balancer")
    if lb is not None and not isinstance(lb, str):
        errors.append(
            f"{prefix}.alb.load_balancer: must be a string, got {type(lb).__name__} — "
            f"if the service has multiple ALBs, use only the primary one"
        )
    tg = alb.get("target_groups")
    if tg is not None and not isinstance(tg, dict):
        errors.append(f"{prefix}.alb.target_groups: must be a mapping")


def _validate_region_metrics(region_cfg: dict, rprefix: str, errors: list[str]) -> None:
    """Validate a single region's metrics block (ALB, SQS, Kinesis, etc.)."""
    for field in LIST_METRIC_FIELDS:
        val = region_cfg.get(field)
        if val is not None and not isinstance(val, list):
            errors.append(f"{rprefix}.{field}: must be a list")
    alb = region_cfg.get("alb")
    if alb and isinstance(alb, dict):
        _validate_alb(alb, rprefix, errors)


def validate(path: str) -> None:
    errors: list[str] = []

    # ---------------------------------------------------------------
    # 1. YAML syntax
    # ---------------------------------------------------------------
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"FATAL: YAML parse error in {path}:\n{e}")
        sys.exit(1)

    if not isinstance(data, dict):
        print(f"FATAL: config root must be a mapping, got {type(data).__name__}")
        sys.exit(1)

    print(f"[OK] YAML syntax valid")

    # ---------------------------------------------------------------
    # 2. Services section
    # ---------------------------------------------------------------
    services = data.get("services")
    if not services or not isinstance(services, dict):
        errors.append("Top-level 'services' key missing or empty")
    else:
        for name, svc in services.items():
            prefix = f"services.{name}"

            if not isinstance(svc, dict):
                errors.append(f"{prefix}: must be a mapping, got {type(svc).__name__}")
                continue

            # description (required)
            if not svc.get("description"):
                errors.append(f"{prefix}: missing 'description'")

            # tags
            tags = svc.get("tags")
            if tags is not None and not isinstance(tags, list):
                errors.append(f"{prefix}.tags: must be a list (got {type(tags).__name__})")

            # email_recipients
            recipients = svc.get("email_recipients")
            if recipients is not None:
                if not isinstance(recipients, list):
                    errors.append(
                        f"{prefix}.email_recipients: must be a list — "
                        f"likely YAML indentation error (got {type(recipients).__name__})"
                    )
                elif len(recipients) == 0:
                    errors.append(f"{prefix}.email_recipients: defined but empty")
                else:
                    for i, r in enumerate(recipients):
                        if not isinstance(r, str):
                            errors.append(
                                f"{prefix}.email_recipients[{i}]: expected string, got {type(r).__name__}"
                            )
                        elif not EMAIL_RE.match(r):
                            errors.append(f"{prefix}.email_recipients[{i}]: invalid email '{r}'")

            # cloudwatch
            cw = svc.get("cloudwatch")
            if cw and isinstance(cw, dict):
                lg = cw.get("log_groups")
                if lg is not None and not isinstance(lg, list):
                    errors.append(f"{prefix}.cloudwatch.log_groups: must be a list")

                # Check environment overrides
                envs = cw.get("environments")
                if envs and isinstance(envs, dict):
                    for env_name, env_cfg in envs.items():
                        if not isinstance(env_cfg, dict):
                            continue
                        regions = env_cfg.get("regions")
                        if regions and isinstance(regions, dict):
                            for region_name, region_cfg in regions.items():
                                if not isinstance(region_cfg, dict):
                                    continue
                                rlg = region_cfg.get("log_groups")
                                if rlg is not None and not isinstance(rlg, list):
                                    errors.append(
                                        f"{prefix}.cloudwatch.environments.{env_name}"
                                        f".regions.{region_name}.log_groups: must be a list"
                                    )

            # athena
            athena = svc.get("athena")
            if athena and isinstance(athena, dict):
                for env_name, env_cfg in athena.items():
                    if not isinstance(env_cfg, dict):
                        continue
                    if not env_cfg.get("database"):
                        errors.append(f"{prefix}.athena.{env_name}: missing 'database'")
                    if not env_cfg.get("table"):
                        errors.append(f"{prefix}.athena.{env_name}: missing 'table'")

            # metrics
            metrics = svc.get("metrics")
            if metrics and isinstance(metrics, dict):
                # Top-level metric fields
                for field in LIST_METRIC_FIELDS:
                    val = metrics.get(field)
                    if val is not None and not isinstance(val, list):
                        errors.append(f"{prefix}.metrics.{field}: must be a list")

                # Top-level ALB (if present)
                top_alb = metrics.get("alb")
                if top_alb and isinstance(top_alb, dict):
                    _validate_alb(top_alb, f"{prefix}.metrics", errors)

                # Top-level metrics.regions.* overrides
                regions = metrics.get("regions")
                if regions and isinstance(regions, dict):
                    for region_name, region_cfg in regions.items():
                        if not isinstance(region_cfg, dict):
                            continue
                        _validate_region_metrics(
                            region_cfg, f"{prefix}.metrics.regions.{region_name}", errors
                        )

                # Per-environment metrics.environments.*.regions.* overrides
                envs = metrics.get("environments")
                if envs and isinstance(envs, dict):
                    for env_name, env_cfg in envs.items():
                        if not isinstance(env_cfg, dict):
                            continue
                        env_regions = env_cfg.get("regions")
                        if env_regions and isinstance(env_regions, dict):
                            for region_name, region_cfg in env_regions.items():
                                if not isinstance(region_cfg, dict):
                                    continue
                                _validate_region_metrics(
                                    region_cfg,
                                    f"{prefix}.metrics.environments.{env_name}.regions.{region_name}",
                                    errors,
                                )

        print(f"[OK] {len(services)} services validated")

    # ---------------------------------------------------------------
    # 3. Email config
    # ---------------------------------------------------------------
    email_cfg = data.get("email")
    if email_cfg and isinstance(email_cfg, dict):
        admin = email_cfg.get("admin_email", "")
        if admin and not EMAIL_RE.match(admin):
            errors.append(f"email.admin_email: invalid email '{admin}'")
        if not email_cfg.get("notify_url"):
            errors.append("email.notify_url: missing")
        if not email_cfg.get("token_url"):
            errors.append("email.token_url: missing")
        if not email_cfg.get("client_id"):
            errors.append("email.client_id: missing")
        print("[OK] email config validated")
    else:
        errors.append("Top-level 'email' config section missing")

    # ---------------------------------------------------------------
    # 4. LLM config
    # ---------------------------------------------------------------
    llm_cfg = data.get("llm")
    if llm_cfg and isinstance(llm_cfg, dict):
        if not llm_cfg.get("provider"):
            errors.append("llm.provider: missing")
        if not llm_cfg.get("model"):
            errors.append("llm.model: missing")
        print("[OK] llm config validated")
    else:
        errors.append("Top-level 'llm' config section missing")

    # ---------------------------------------------------------------
    # 5. Confluence config (optional but validate if present)
    # ---------------------------------------------------------------
    confluence_cfg = data.get("confluence")
    if confluence_cfg and isinstance(confluence_cfg, dict):
        if not confluence_cfg.get("url"):
            errors.append("confluence.url: missing")
        if not confluence_cfg.get("space_key"):
            errors.append("confluence.space_key: missing")
        print("[OK] confluence config validated")

    # ---------------------------------------------------------------
    # 6. Lambda config (optional but validate if present)
    # ---------------------------------------------------------------
    lambda_cfg = data.get("lambda")
    if lambda_cfg and isinstance(lambda_cfg, dict):
        for field in ("auto_email", "auto_publish"):
            val = lambda_cfg.get(field)
            if val is not None and not isinstance(val, bool):
                errors.append(f"lambda.{field}: must be a boolean (got {type(val).__name__})")
        print("[OK] lambda config validated")

    # ---------------------------------------------------------------
    # 7. Pydantic model validation (catches type mismatches the above may miss)
    # ---------------------------------------------------------------
    try:
        sys.path.insert(0, "src")
        from agent.config import AgentConfig
        AgentConfig(**data)
        print("[OK] Pydantic model validation passed")
    except ImportError as e:
        # Runtime deps (boto3, etc.) not available in CI — skip Pydantic check.
        # The structural checks above still catch most issues.
        print(f"[SKIP] Pydantic validation skipped (missing dependency: {e.name})")
    except Exception as e:
        # Extract individual errors from Pydantic ValidationError
        err_str = str(e)
        if "validation error" in err_str.lower():
            for line in err_str.splitlines():
                line = line.strip()
                if line and not line.startswith("For further") and line != str(type(e).__name__):
                    errors.append(f"Pydantic: {line}")
        else:
            errors.append(f"Pydantic validation: {err_str}")

    # ---------------------------------------------------------------
    # Report
    # ---------------------------------------------------------------
    print()
    svc_count = len(services) if services and isinstance(services, dict) else 0
    recipients_count = sum(
        1
        for s in (services or {}).values()
        if isinstance(s, dict) and s.get("email_recipients")
    )

    if errors:
        print(f"VALIDATION FAILED ({len(errors)} errors):\n")
        for e in errors:
            print(f"  - {e}")
        print(f"\n{svc_count} services checked, {recipients_count} with email_recipients.")
        sys.exit(1)
    else:
        print(
            f"config.yaml PASSED: {svc_count} services, "
            f"{recipients_count} with email_recipients, 0 errors"
        )
        sys.exit(0)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    validate(path)
