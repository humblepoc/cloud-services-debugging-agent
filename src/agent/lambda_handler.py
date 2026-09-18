"""AWS Lambda entry point for PagerDuty webhook-triggered investigations.

Receives a PagerDuty webhook event, runs the agent to investigate the alert,
and optionally sends an email report and publishes to Confluence.

The LLM determines the affected service, environment, and region from the
alert title — no regex parsing needed. The service catalog in the system
prompt gives it all the context it needs (same as the CLI).

Lambda handler always returns 200 to prevent PagerDuty from disabling
the webhook on transient errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# SQS delay bounds (AWS hard limits for per-message DelaySeconds)
_SQS_MIN_DELAY = 0
_SQS_MAX_DELAY = 900


def _resolve_delay(cfg: Any) -> int:
    """Resolve the pre-investigation delay in seconds.

    Precedence: AGENT_DELAY_SECONDS env var > cfg.lambda_config.delay_seconds > 300.
    Clamped to the SQS-supported range [0, 900].
    """
    raw = os.environ.get("AGENT_DELAY_SECONDS")
    if raw is not None and raw.strip():
        try:
            delay = int(raw)
        except ValueError:
            logger.warning("Invalid AGENT_DELAY_SECONDS=%r; falling back to config", raw)
            delay = getattr(cfg.lambda_config, "delay_seconds", 300)
    else:
        delay = getattr(cfg.lambda_config, "delay_seconds", 300)
    return max(_SQS_MIN_DELAY, min(delay, _SQS_MAX_DELAY))


def _enqueue_delayed(event: dict[str, Any], cfg: Any) -> bool:
    """Send the webhook event to SQS with a delay so the worker runs later.

    Returns True if the message was enqueued, False if SQS is not configured
    (no AGENT_SQS_QUEUE_URL) or the send failed — in which case the caller
    falls back to the existing self-invoke path.
    """
    queue_url = os.environ.get("AGENT_SQS_QUEUE_URL")
    if not queue_url:
        logger.info("AGENT_SQS_QUEUE_URL not set; skipping SQS enqueue")
        return False
    delay = _resolve_delay(cfg)
    try:
        import boto3
        sqs = boto3.client("sqs")
        sqs.send_message(
            QueueUrl=queue_url,
            DelaySeconds=delay,
            MessageBody=json.dumps(event, default=str),
        )
        logger.info("Enqueued investigation to SQS with %ds delay", delay)
        return True
    except Exception:
        logger.exception("Failed to enqueue to SQS; will fall back to self-invoke")
        return False


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda handler — PagerDuty webhook entry point.

    On synchronous invocation (from API Gateway), immediately returns 200
    and re-invokes itself asynchronously to do the actual investigation.
    On async invocation (self-triggered), performs the full investigation.
    """
    # Configure logging for Lambda — force level on root logger so all
    # child loggers (agent.core.loop, agent.tools.*, etc.) propagate.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        force=True,
    )

    # SQS event source mapping invocation — the delayed message has arrived.
    # Each record's body is the original webhook event (JSON) that the receiver enqueued.
    records = event.get("Records")
    if records and any(r.get("eventSource") == "aws:sqs" for r in records):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        for record in records:
            if record.get("eventSource") != "aws:sqs":
                continue
            try:
                original_event = json.loads(record.get("body", "{}"))
            except (json.JSONDecodeError, TypeError):
                logger.exception("Failed to parse SQS record body; skipping")
                continue
            try:
                loop.run_until_complete(_handle(original_event))
            except Exception:
                # Let it surface so SQS routes the message to the DLQ
                # (maxReceiveCount=1). Log first for diagnostics.
                logger.exception("Investigation failed for SQS record")
                raise
        return {}

    # If this is the async worker invocation, do the actual work
    if event.get("_async_worker"):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_handle(event))
        except Exception:
            logger.exception("Lambda handler error (async worker)")
        return {}

    # Synchronous invocation from API Gateway (PagerDuty webhook).
    # Preferred path: enqueue to SQS with a delay so the investigation starts later.
    # Fallback path (no queue configured or enqueue failed): self-invoke immediately,
    # preserving the original behavior so no alert is ever dropped.
    try:
        from agent.config import load_config
        cfg = load_config()
    except Exception:
        logger.exception("Failed to load config in receiver; proceeding with self-invoke fallback")
        cfg = None

    if cfg is not None and _enqueue_delayed(event, cfg):
        return _response(200, {"status": "accepted", "message": "Queued for delayed analysis"})

    # Fallback: original self-invoke behavior (immediate async worker)
    try:
        import boto3
        lambda_client = boto3.client("lambda")
        # Add marker so the async invocation knows to do the work
        async_event = dict(event)
        async_event["_async_worker"] = True
        function_name = context.function_name if context else "pagerduty-incident-aianalyzer"
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="Event",  # async
            Payload=json.dumps(async_event),
        )
        logger.info("Dispatched async worker invocation (fallback)")
    except Exception as exc:
        logger.exception("Failed to dispatch async invocation")
        return _response(200, {"status": "error", "message": str(exc)})

    return _response(200, {"status": "accepted", "message": "Analysing the query..."})


async def _handle(event: dict[str, Any]) -> dict[str, Any]:
    """Core handler — parse webhook, run agent, return summary."""

    # 1. Parse PagerDuty webhook
    parsed = _parse_webhook(event)
    if not parsed.get("incident_title"):
        logger.warning("Empty or missing incident title in webhook payload")
        return {"status": "skipped", "message": "No incident title in payload"}

    # Skip non-triggered events (acknowledged, escalated, resolved, etc.)
    event_type = parsed.get("event_type", "")
    if event_type and event_type != "incident.triggered":
        logger.info("Skipping non-triggered event: %s (id=%s)", event_type, parsed.get("incident_id", "?"))
        return {"status": "skipped", "message": f"Skipping event type: {event_type}"}

    logger.info(
        "PagerDuty alert received: %s (id=%s, time=%s)",
        parsed["incident_title"],
        parsed.get("incident_id", "?"),
        parsed.get("occurred_at", "?"),
    )

    # 2. Load config (reads config.yaml + creds_*.txt from Lambda package dir)
    from agent.config import load_config

    cfg = load_config()

    # 3. Build investigation prompt
    prompt = _build_prompt(parsed, cfg)

    # 4. Set up agent components (reuse setup helpers — no Rich dependency)
    from agent.setup import build_registry, get_system_prompt
    from agent.core.context import ConversationContext
    from agent.core.loop import run_agent
    from agent.core.types import Message, Role
    from agent.llm.registry import get_provider

    provider = get_provider(cfg.llm.provider, cfg)
    registry = build_registry(cfg)
    context = ConversationContext(system_prompt=get_system_prompt(cfg))
    context.add(Message(role=Role.USER, content=prompt))

    # 5. Run the agent
    logger.info("Starting agent investigation (max_iterations=%d)", cfg.max_iterations)
    result = await run_agent(provider, registry, context, max_iterations=cfg.max_iterations)

    response_text = result.content or ""
    logger.info("Agent investigation complete (%d chars)", len(response_text))

    return {
        "status": "success",
        "incident_id": parsed.get("incident_id", ""),
        "incident_title": parsed["incident_title"],
        "response_preview": response_text[:500],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_webhook(event: dict[str, Any]) -> dict[str, Any]:
    """Extract incident info from a PagerDuty V3 webhook event.

    Expected structure (API Gateway proxy integration):
        event.body.event.data.{id, number, title}
        event.body.event.{event_type, occurred_at}
    """
    body = event.get("body", "{}")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Failed to parse webhook body as JSON")
            return {}

    pd_event = body.get("event", {})
    data = pd_event.get("data", {})

    return {
        "incident_title": data.get("title", ""),
        "incident_id": data.get("id", ""),
        "incident_number": data.get("number"),
        "occurred_at": pd_event.get("occurred_at", ""),
        "event_type": pd_event.get("event_type", ""),
    }


def _build_prompt(parsed: dict[str, Any], cfg: Any) -> str:
    """Build the investigation prompt from the parsed PagerDuty alert."""
    title = parsed["incident_title"]
    time = parsed.get("occurred_at", "unknown")

    prompt = (
        f"A PagerDuty alert has fired:\n\n"
        f"Title: {title}\n"
        f"Incident time: {time}\n\n"
        f"Investigate this alert thoroughly using the available tools. "
        f"Check logs, metrics, ALB health, and any other relevant data sources "
        f"for the affected service, environment, and region.\n\n"
        f"Produce a structured investigation report with: "
        f"Summary, Root Cause Analysis, Timeline, and Recommended Next Steps.\n\n"
        f"IMPORTANT INVESTIGATION GUIDELINES:\n"
        f"- Parse the alert title carefully to identify the correct environment "
        f"(integ/prod), region (us-east-1/eu-central-1/ap-northeast-1), and service.\n"
        f"- If a tool returns 'No data points' or 'AccessDeniedException', do NOT "
        f"conclude the alert is a false positive. This likely means you are querying "
        f"the wrong account, environment, or region. Retry with correct parameters.\n"
        f"- Only conclude 'false positive' if you have POSITIVE EVIDENCE showing "
        f"the service is healthy (actual metric data points confirming normal operation, "
        f"queue depths at zero, no errors in logs, etc.).\n"
        f"- Always specify the environment and region parameters when calling tools."
    )

    lc = cfg.lambda_config
    if lc.auto_publish and lc.auto_email:
        prompt += (
            "\n\nAfter completing the investigation:\n"
            "1. FIRST, publish the report to Confluence using publish_confluence with a descriptive title.\n"
            "2. THEN, send the report via email using send_email. Include the service name "
            "AND pass the Confluence page URL (from the publish_confluence result) as the "
            "confluence_url parameter so the email includes a link to the full report."
        )
    elif lc.auto_publish:
        prompt += (
            "\n\nAlso publish the report to Confluence using the "
            "publish_confluence tool with a descriptive title."
        )
    elif lc.auto_email:
        prompt += (
            "\n\nAfter completing the investigation, send the report via email "
            "using the send_email tool. Include the service name so the right "
            "team gets notified."
        )

    return prompt


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    """Format a Lambda proxy integration response."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }
