"""Foundation tests: registry wiring, http client, dispatch, auth guard, conversion.

These pin the shared contract every domain module builds on. They use the
``prufa_mcp.http._TRANSPORT`` seam (an httpx.MockTransport) to exercise tools
without a live API.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
import pytest

import prufa_mcp.http as http
from prufa_mcp.conversion import trial_block, upsell
from prufa_mcp.registry import REGISTRY
from prufa_mcp.tools import audit as audit_tools


@contextlib.contextmanager
def mock_api(handler):
    """Route all http.* calls through a MockTransport for the duration."""
    prev = http._TRANSPORT
    prev_backoffs = http._RETRY_BACKOFFS_S
    http._TRANSPORT = httpx.MockTransport(handler)
    http._RETRY_BACKOFFS_S = (0.0, 0.0, 0.0)
    try:
        yield
    finally:
        http._TRANSPORT = prev
        http._RETRY_BACKOFFS_S = prev_backoffs


def test_registry_has_audit_tools() -> None:
    for name in ("prufa_health_check", "prufa_run_audit", "prufa_get_report", "prufa_get_run"):
        assert REGISTRY.get(name) is not None, f"{name} not registered"


def test_list_mcp_shape() -> None:
    listed = {t["name"]: t for t in REGISTRY.list_mcp()}
    hc = listed["prufa_health_check"]
    assert set(hc) == {"name", "description", "inputSchema", "annotations"}
    assert hc["annotations"]["tier"] == "setup"


def test_health_check_no_auth_needed() -> None:
    assert REGISTRY.get("prufa_health_check").requires_auth is False
    result = asyncio.run(audit_tools.t_health_check({}))
    assert result["structuredContent"]["service"] == "prufa-mcp"


def test_get_run_hits_correct_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"status": "succeeded"})

    with mock_api(_handler):
        result = asyncio.run(audit_tools.t_get_run({"run_id": "abc-123"}))

    assert captured["path"] == "/api/v1/audits/abc-123"
    assert captured["auth"] == "Bearer test-token"
    assert result["structuredContent"]["status"] == "succeeded"


def test_get_run_missing_arg() -> None:
    result = asyncio.run(audit_tools.t_get_run({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_api_error_passthrough_402(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 402 keeps its code, hint, and checkout_url extra."""
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={
                "detail": {
                    "code": "tier_required",
                    "hint": "upgrade to run this",
                    "checkout_url": "https://prufa.dev/api/v1/billing/checkout",
                }
            },
        )

    with mock_api(_handler):
        result = asyncio.run(audit_tools.t_list_runs({}))

    body = result["structuredContent"]
    assert result["isError"] is True
    assert body["code"] == "tier_required"
    assert body["http_status"] == 402
    assert body["checkout_url"].endswith("/billing/checkout")


def test_get_finding_filters_by_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"findings": [{"finding_key": "a"}, {"finding_key": "b"}]},
        )

    with mock_api(_handler):
        result = asyncio.run(audit_tools.t_get_finding({"run_id": "r", "finding_key": "b"}))

    findings = result["structuredContent"]["findings"]
    assert findings == [{"finding_key": "b"}]


# --- conversion layer ---------------------------------------------------------


def test_trial_block_derives_days() -> None:
    from datetime import datetime, timedelta, timezone

    exp = (datetime.now(timezone.utc) + timedelta(days=5, hours=1)).isoformat()
    block = trial_block(
        {"tier": "agent_temp", "available_credits": 40, "calls_included": 50},
        is_in_trial=True,
        trial_expires_at=exp,
    )
    assert block["days_remaining"] == 5
    assert block["credits_remaining"] == 40
    assert "monitors" in block["gated_after_trial"]


def test_upsell_fires_on_low_credits() -> None:
    up = upsell({"tier": "agent_temp", "available_credits": 2, "calls_included": 50})
    assert up is not None
    assert "prufa_upgrade_plan" == up["upgrade_tool"]
    assert "message_for_human" in up


def test_upsell_silent_when_healthy() -> None:
    assert upsell({"tier": "agent_temp", "available_credits": 45, "calls_included": 50}) is None


def test_upsell_never_for_paid() -> None:
    assert upsell({"tier": "pro", "available_credits": 0, "calls_included": 500}) is None


# --- free-audit -> workspace funnel -------------------------------------------


def test_anonymous_next_step_shape() -> None:
    from prufa_mcp.conversion import anonymous_next_step

    b = anonymous_next_step()
    assert b["setup_tool"] == "prufa_setup_workspace"
    assert b["state"] == "anonymous"
    assert len(b["unlocks"]) >= 3
    assert "message_for_human" in b


def test_anonymous_audit_attaches_workspace_unlock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token-less audit result carries the workspace_unlock funnel block."""
    from prufa_mcp.tools import audit as at

    async def fake_run_audit(*, url, wait=True):
        return {"status": "queued", "share_token": "s"}

    monkeypatch.setattr(at._audit, "run_audit", fake_run_audit)
    monkeypatch.setattr(at._audit, "_api_token", lambda: "")
    res = asyncio.run(at.t_run_audit({"url": "https://x.com", "wait": False}))
    unlock = res["structuredContent"]["workspace_unlock"]
    assert unlock["setup_tool"] == "prufa_setup_workspace"
    assert unlock["unlocks"]


def test_authed_audit_has_no_workspace_unlock(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a token set, the funnel block is NOT attached (already converted)."""
    from prufa_mcp.tools import audit as at

    async def fake_run_audit(*, url, wait=True):
        return {"status": "queued", "share_token": "s"}

    monkeypatch.setattr(at._audit, "run_audit", fake_run_audit)
    monkeypatch.setattr(at._audit, "_api_token", lambda: "tok")
    res = asyncio.run(at.t_run_audit({"url": "https://x.com", "wait": False}))
    assert "workspace_unlock" not in res["structuredContent"]
