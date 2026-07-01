"""Workspace-domain tool tests.

Exercise each tool through the ``prufa_mcp.http._TRANSPORT`` MockTransport seam
(copied from tests/test_foundation.py): assert method+path (+body for mutating
calls) and the returned MCP result shape, including trial/upsell conversion.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

import prufa_mcp.http as http
from prufa_mcp.registry import REGISTRY
from prufa_mcp.tools import workspace


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


def _no_token(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Ensure _api_token() resolves to '' — no env var, no config file."""
    monkeypatch.delenv("PRUFA_API_TOKEN", raising=False)
    monkeypatch.setenv("PRUFA_CONFIG", str(tmp_path / "absent.json"))


def _future(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days, hours=1)).isoformat()


# --- registration -------------------------------------------------------------


def test_all_tools_registered() -> None:
    for name in (
        "prufa_setup_workspace",
        "prufa_get_workspace",
        "prufa_get_usage",
        "prufa_workspace_settings",
        "prufa_set_notifications",
    ):
        assert REGISTRY.get(name) is not None, f"{name} not registered"


def test_setup_workspace_no_auth_required() -> None:
    assert REGISTRY.get("prufa_setup_workspace").requires_auth is False
    assert REGISTRY.get("prufa_get_workspace").requires_auth is True


def test_mutating_tools_have_idempotency_prop() -> None:
    props = REGISTRY.get("prufa_workspace_settings").input_schema["properties"]
    assert "idempotency_key" in props


# --- prufa_setup_workspace ----------------------------------------------------


def test_setup_workspace_with_token_reads_current(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "workspace_id": "ws1",
                "is_in_trial": True,
                "trial_expires_at": _future(5),
                "usage": {"tier": "agent_temp", "available_credits": 40, "calls_included": 50},
            },
        )

    with mock_api(_handler):
        result = asyncio.run(workspace.t_setup_workspace({}))

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/workspaces/current"
    body = result["structuredContent"]
    assert body["trial"]["days_remaining"] == 5
    assert body["trial"]["is_in_trial"] is True
    # healthy credits -> no upsell attached
    assert "upsell" not in body


def test_setup_workspace_no_token_missing_email(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _no_token(monkeypatch, tmp_path)
    result = asyncio.run(workspace.t_setup_workspace({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"
    assert "owner_email" in result["structuredContent"]["message"]


def test_setup_workspace_no_token_creates(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _no_token(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(
            201,
            json={
                "workspace_id": "ws-new",
                "api_token": "secret-token",
                "tier": "agent_temp",
                "usage": {"tier": "agent_temp", "available_credits": 50, "calls_included": 50},
            },
        )

    with mock_api(_handler):
        result = asyncio.run(
            workspace.t_setup_workspace({"owner_email": "a@b.com", "name": "My WS"})
        )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/workspaces"
    assert captured["body"] == {
        "tier": "agent_temp",
        "owner_email": "a@b.com",
        "display_name": "My WS",
    }
    assert captured["idem"]  # mutating call forwards an idempotency key
    body = result["structuredContent"]
    assert body["api_token"] == "secret-token"
    assert "persist_instruction" in body
    assert body["trial"]["is_in_trial"] is True


def test_setup_workspace_no_token_default_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _no_token(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"workspace_id": "w", "api_token": "t", "tier": "agent_temp", "usage": {}})

    with mock_api(_handler):
        asyncio.run(workspace.t_setup_workspace({"owner_email": "a@b.com"}))

    assert captured["body"]["display_name"] == "agent workspace"


# --- prufa_get_workspace ------------------------------------------------------


def test_get_workspace_attaches_trial_and_upsell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "is_in_trial": True,
                "trial_expires_at": _future(5),
                # low credits -> upsell fires
                "usage": {"tier": "agent_temp", "available_credits": 2, "calls_included": 50},
            },
        )

    with mock_api(_handler):
        result = asyncio.run(workspace.t_get_workspace({}))

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/workspaces/current"
    body = result["structuredContent"]
    assert body["trial"]["credits_remaining"] == 2
    assert body["upsell"]["upgrade_tool"] == "prufa_upgrade_plan"


def test_get_workspace_error_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": {"code": "not_found", "hint": "no workspace"}})

    with mock_api(_handler):
        result = asyncio.run(workspace.t_get_workspace({}))

    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "not_found"
    assert result["structuredContent"]["http_status"] == 404


# --- prufa_get_usage ----------------------------------------------------------


def test_get_usage_flattens_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "is_in_trial": True,
                "trial_expires_at": _future(1),  # <=2 days -> upsell fires
                "usage": {"tier": "agent_temp", "available_credits": 45, "calls_included": 50},
            },
        )

    with mock_api(_handler):
        result = asyncio.run(workspace.t_get_usage({}))

    body = result["structuredContent"]
    assert body["available_credits"] == 45  # usage flattened to top level
    assert body["trial"]["days_remaining"] == 1
    # trial ends soon -> upsell present even though credits are healthy
    assert body["upsell"]["reason"].startswith("trial ends") or "trial ends" in body["upsell"]["reason"]


def test_get_usage_no_upsell_when_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "is_in_trial": True,
                "trial_expires_at": _future(6),
                "usage": {"tier": "agent_temp", "available_credits": 48, "calls_included": 50},
            },
        )

    with mock_api(_handler):
        result = asyncio.run(workspace.t_get_usage({}))

    assert "upsell" not in result["structuredContent"]


# --- prufa_workspace_settings -------------------------------------------------


def test_workspace_settings_patches_only_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"display_name": "New", "auto_recharge": True})

    with mock_api(_handler):
        result = asyncio.run(
            workspace.t_workspace_settings(
                {"display_name": "New", "auto_recharge": True, "unknown_key": "x"}
            )
        )

    assert captured["method"] == "PATCH"
    assert captured["path"] == "/api/v1/workspaces/current"
    assert captured["body"] == {"display_name": "New", "auto_recharge": True}
    assert captured["idem"]
    assert result["structuredContent"]["display_name"] == "New"


def test_workspace_settings_requires_a_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    result = asyncio.run(workspace.t_workspace_settings({"unknown_key": "x"}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_workspace_settings_nullable_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    with mock_api(_handler):
        asyncio.run(workspace.t_workspace_settings({"usage_webhook_url": None}))

    assert captured["body"] == {"usage_webhook_url": None}


# --- prufa_set_notifications --------------------------------------------------


def test_set_notifications_patches_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}
    cells = {"audit_failed": {"email": True, "slack": False}}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"cells": cells, "locked_events": []})

    with mock_api(_handler):
        result = asyncio.run(workspace.t_set_notifications({"cells": cells}))

    assert captured["method"] == "PATCH"
    assert captured["path"] == "/api/v1/workspaces/current/notifications"
    assert captured["body"] == {"cells": cells}
    assert result["structuredContent"]["locked_events"] == []


def test_set_notifications_requires_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    result = asyncio.run(workspace.t_set_notifications({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_set_notifications_422_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"detail": {"code": "invalid_cells", "hint": "unknown event key"}}
        )

    with mock_api(_handler):
        result = asyncio.run(
            workspace.t_set_notifications({"cells": {"bad": {"email": True}}})
        )

    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_cells"
    assert result["structuredContent"]["http_status"] == 422
