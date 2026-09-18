"""Tests for the Lambda handler — PagerDuty webhook entry point.

All tests are pure unit tests or use mocks. No AWS calls, no LLM calls, no network.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.lambda_handler import _build_prompt, _parse_webhook, _response, handler


# ---------------------------------------------------------------------------
# _parse_webhook
# ---------------------------------------------------------------------------


class TestParseWebhook:
    """Tests for PagerDuty webhook payload parsing."""

    def _make_event(self, *, body_as_string: bool = False, **overrides) -> dict:
        """Build a mock PD V3 webhook event."""
        body = {
            "event": {
                "event_type": overrides.get("event_type", "incident.triggered"),
                "occurred_at": overrides.get("occurred_at", "2026-04-29T10:30:00Z"),
                "data": {
                    "id": overrides.get("incident_id", "P123ABC"),
                    "number": overrides.get("number", 42),
                    "title": overrides.get("title", "[PROD][XF] xf-ens [eu-central-1] Error Rate"),
                },
            }
        }
        if body_as_string:
            return {"body": json.dumps(body)}
        return {"body": body}

    def test_parse_webhook_valid(self):
        event = self._make_event()
        parsed = _parse_webhook(event)
        assert parsed["incident_title"] == "[PROD][XF] xf-ens [eu-central-1] Error Rate"
        assert parsed["incident_id"] == "P123ABC"
        assert parsed["incident_number"] == 42
        assert parsed["occurred_at"] == "2026-04-29T10:30:00Z"
        assert parsed["event_type"] == "incident.triggered"

    def test_parse_webhook_string_body(self):
        event = self._make_event(body_as_string=True)
        parsed = _parse_webhook(event)
        assert parsed["incident_title"] == "[PROD][XF] xf-ens [eu-central-1] Error Rate"
        assert parsed["incident_id"] == "P123ABC"

    def test_parse_webhook_empty_body(self):
        parsed = _parse_webhook({})
        assert parsed.get("incident_title", "") == ""

    def test_parse_webhook_malformed_json(self):
        parsed = _parse_webhook({"body": "not-valid-json{{"})
        assert parsed == {}

    def test_parse_webhook_missing_fields(self):
        event = {"body": {"event": {"data": {}}}}
        parsed = _parse_webhook(event)
        assert parsed["incident_title"] == ""
        assert parsed["incident_id"] == ""
        assert parsed["incident_number"] is None
        assert parsed["occurred_at"] == ""
        assert parsed["event_type"] == ""


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    """Tests for investigation prompt construction."""

    @pytest.fixture()
    def parsed(self) -> dict:
        return {
            "incident_title": "[PROD][XF] gateway [us-east-1] 5xx Rate",
            "occurred_at": "2026-04-29T12:00:00Z",
        }

    @pytest.fixture()
    def cfg_all(self):
        cfg = MagicMock()
        cfg.lambda_config.auto_email = True
        cfg.lambda_config.auto_publish = True
        return cfg

    def test_prompt_contains_alert_title(self, parsed, cfg_all):
        prompt = _build_prompt(parsed, cfg_all)
        assert "[PROD][XF] gateway [us-east-1] 5xx Rate" in prompt

    def test_prompt_contains_incident_time(self, parsed, cfg_all):
        prompt = _build_prompt(parsed, cfg_all)
        assert "2026-04-29T12:00:00Z" in prompt

    def test_prompt_with_auto_email_and_publish(self, parsed, cfg_all):
        prompt = _build_prompt(parsed, cfg_all)
        assert "send_email" in prompt
        assert "publish_confluence" in prompt

    def test_prompt_email_only(self, parsed):
        cfg = MagicMock()
        cfg.lambda_config.auto_email = True
        cfg.lambda_config.auto_publish = False
        prompt = _build_prompt(parsed, cfg)
        assert "send_email" in prompt
        assert "publish_confluence" not in prompt

    def test_prompt_publish_only(self, parsed):
        cfg = MagicMock()
        cfg.lambda_config.auto_email = False
        cfg.lambda_config.auto_publish = True
        prompt = _build_prompt(parsed, cfg)
        assert "send_email" not in prompt
        assert "publish_confluence" in prompt

    def test_prompt_no_auto_actions(self, parsed):
        cfg = MagicMock()
        cfg.lambda_config.auto_email = False
        cfg.lambda_config.auto_publish = False
        prompt = _build_prompt(parsed, cfg)
        assert "send_email" not in prompt
        assert "publish_confluence" not in prompt


# ---------------------------------------------------------------------------
# _response
# ---------------------------------------------------------------------------


class TestResponse:
    """Tests for Lambda proxy response formatting."""

    def test_response_format(self):
        resp = _response(200, {"status": "ok"})
        assert resp["statusCode"] == 200
        assert resp["headers"]["Content-Type"] == "application/json"
        body = json.loads(resp["body"])
        assert body["status"] == "ok"

    def test_response_serializes_non_string_values(self):
        resp = _response(200, {"count": 42, "items": [1, 2, 3]})
        body = json.loads(resp["body"])
        assert body["count"] == 42
        assert body["items"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# handler (integration-level with mocks)
# ---------------------------------------------------------------------------


class TestHandler:
    """Tests for the top-level Lambda handler function."""

    def _pd_event(self, title: str = "[PROD][XF] xf-ens [eu-central-1] Error Rate") -> dict:
        return {
            "body": json.dumps({
                "event": {
                    "event_type": "incident.triggered",
                    "occurred_at": "2026-04-29T10:30:00Z",
                    "data": {"id": "P1", "number": 1, "title": title},
                }
            })
        }

    def test_handler_empty_title_returns_skipped(self):
        event = self._pd_event(title="")
        resp = handler(event, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["status"] == "skipped"

    def test_handler_empty_body_returns_skipped(self):
        resp = handler({}, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        # Empty body → empty title → skipped
        assert body["status"] == "skipped"

    @patch("agent.lambda_handler._handle")
    def test_handler_exception_returns_200(self, mock_handle):
        """Any unhandled exception → still returns 200 with error status."""
        mock_handle.side_effect = RuntimeError("something broke")
        resp = handler(self._pd_event(), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["status"] == "error"
        assert "something broke" in body["message"]

    @patch("agent.lambda_handler._handle")
    def test_handler_success_returns_200(self, mock_handle):
        """Successful investigation → returns 200 with success status."""

        async def fake_handle(event):
            return {
                "status": "success",
                "incident_id": "P1",
                "incident_title": "[PROD][XF] xf-ens [eu-central-1] Error Rate",
                "response_preview": "## Summary\nThe service recovered.",
            }

        mock_handle.side_effect = fake_handle
        resp = handler(self._pd_event(), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["status"] == "success"
        assert body["incident_id"] == "P1"
        assert "Summary" in body["response_preview"]
