"""Tests for the monitors domain tools."""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx
import pytest

import prufa_mcp.http as http
from prufa_mcp.registry import REGISTRY
from prufa_mcp.tools import monitors


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


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")


def _capture_handler(captured: dict[str, Any], status: int, payload: dict):
    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["idem"] = request.headers.get("Idempotency-Key")
        if request.content:
            captured["body"] = json.loads(request.content)
        return httpx.Response(status, json=payload)

    return _handler


# --- registration -------------------------------------------------------------


def test_all_tools_registered() -> None:
    names = [
        "prufa_start_monitor",
        "prufa_get_monitor",
        "prufa_list_monitors",
        "prufa_pause_monitor",
        "prufa_resume_monitor",
        "prufa_trigger_monitor",
        "prufa_delete_monitor",
        "prufa_rotate_monitor_webhook",
        "prufa_list_monitor_deliveries",
    ]
    for name in names:
        td = REGISTRY.get(name)
        assert td is not None, f"{name} not registered"
        assert td.tier == "persistent"
        assert td.requires_auth is True


def test_mutating_tools_expose_idempotency_prop() -> None:
    for name in (
        "prufa_start_monitor",
        "prufa_pause_monitor",
        "prufa_resume_monitor",
        "prufa_trigger_monitor",
        "prufa_delete_monitor",
        "prufa_rotate_monitor_webhook",
    ):
        props = REGISTRY.get(name).input_schema["properties"]
        assert "idempotency_key" in props, f"{name} missing idempotency_key"


# --- start_monitor ------------------------------------------------------------


def test_start_monitor_happy_path() -> None:
    captured: dict[str, Any] = {}
    payload = {
        "id": "mon-1",
        "deploy_hook": {"url": "https://h", "header": "X-Prufa", "secret": "sk_x", "hint": "h"},
        "usage": {"tier": "pro", "available_credits": 500, "calls_included": 500},
    }
    with mock_api(_capture_handler(captured, 201, payload)):
        result = asyncio.run(
            monitors.t_start_monitor({"url": "https://example.com", "cadence": "hourly"})
        )
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/monitors"
    assert captured["body"] == {"url": "https://example.com", "cadence": "hourly"}
    assert captured["idem"] is not None
    sc = result["structuredContent"]
    assert sc["id"] == "mon-1"
    assert "shown only once" in sc["deploy_hook"]["note"]


def test_start_monitor_default_cadence_and_flow_id() -> None:
    captured: dict[str, Any] = {}
    with mock_api(_capture_handler(captured, 201, {"id": "mon-2"})):
        asyncio.run(
            monitors.t_start_monitor({"url": "https://ex.com", "flow_id": "flow-9"})
        )
    assert captured["body"] == {"url": "https://ex.com", "cadence": "daily", "flow_id": "flow-9"}


def test_start_monitor_no_note_without_secret() -> None:
    captured: dict[str, Any] = {}
    payload = {"id": "mon-3", "deploy_hook": {"url": "https://h", "header": "X"}}
    with mock_api(_capture_handler(captured, 201, payload)):
        result = asyncio.run(monitors.t_start_monitor({"url": "https://ex.com"}))
    assert "note" not in result["structuredContent"]["deploy_hook"]


def test_start_monitor_attaches_upsell() -> None:
    captured: dict[str, Any] = {}
    payload = {
        "id": "mon-4",
        "usage": {"tier": "agent_temp", "available_credits": 1, "calls_included": 50},
    }
    with mock_api(_capture_handler(captured, 201, payload)):
        result = asyncio.run(monitors.t_start_monitor({"url": "https://ex.com"}))
    assert result["structuredContent"]["upsell"]["upgrade_tool"] == "prufa_upgrade_plan"


def test_start_monitor_missing_url() -> None:
    result = asyncio.run(monitors.t_start_monitor({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_start_monitor_402_passthrough() -> None:
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
        result = asyncio.run(monitors.t_start_monitor({"url": "https://ex.com"}))
    body = result["structuredContent"]
    assert result["isError"] is True
    assert body["code"] == "tier_required"
    assert body["http_status"] == 402
    assert body["checkout_url"].endswith("/billing/checkout")


# --- get / list ---------------------------------------------------------------


def test_get_monitor_happy_path() -> None:
    captured: dict[str, Any] = {}
    with mock_api(_capture_handler(captured, 200, {"id": "mon-1", "status": "active"})):
        result = asyncio.run(monitors.t_get_monitor({"monitor_id": "mon-1"}))
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/monitors/mon-1"
    assert result["structuredContent"]["status"] == "active"


def test_get_monitor_missing_id() -> None:
    result = asyncio.run(monitors.t_get_monitor({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_get_monitor_404_passthrough() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": {"code": "not_found", "hint": "no monitor"}})

    with mock_api(_handler):
        result = asyncio.run(monitors.t_get_monitor({"monitor_id": "nope"}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "not_found"
    assert result["structuredContent"]["http_status"] == 404


def test_list_monitors_happy_path() -> None:
    captured: dict[str, Any] = {}
    with mock_api(_capture_handler(captured, 200, {"monitors": []})):
        result = asyncio.run(monitors.t_list_monitors({}))
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/monitors"
    assert result["structuredContent"]["monitors"] == []


# --- pause / resume -----------------------------------------------------------


def test_pause_monitor_happy_path() -> None:
    captured: dict[str, Any] = {}
    with mock_api(_capture_handler(captured, 200, {"id": "mon-1", "status": "paused"})):
        result = asyncio.run(monitors.t_pause_monitor({"monitor_id": "mon-1"}))
    assert captured["method"] == "PATCH"
    assert captured["path"] == "/api/v1/monitors/mon-1"
    assert captured["body"] == {"status": "paused"}
    assert captured["idem"] is not None
    assert result["structuredContent"]["status"] == "paused"


def test_pause_monitor_missing_id() -> None:
    result = asyncio.run(monitors.t_pause_monitor({}))
    assert result["isError"] is True


def test_resume_monitor_happy_path() -> None:
    captured: dict[str, Any] = {}
    with mock_api(_capture_handler(captured, 200, {"id": "mon-1", "status": "active"})):
        result = asyncio.run(monitors.t_resume_monitor({"monitor_id": "mon-1"}))
    assert captured["method"] == "PATCH"
    assert captured["path"] == "/api/v1/monitors/mon-1"
    assert captured["body"] == {"status": "active"}
    assert result["structuredContent"]["status"] == "active"


def test_resume_monitor_missing_id() -> None:
    result = asyncio.run(monitors.t_resume_monitor({}))
    assert result["isError"] is True


# --- trigger ------------------------------------------------------------------


def test_trigger_monitor_happy_path() -> None:
    captured: dict[str, Any] = {}
    with mock_api(_capture_handler(captured, 202, {"run_id": "r-1", "deduped": False})):
        result = asyncio.run(monitors.t_trigger_monitor({"monitor_id": "mon-1"}))
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/monitors/mon-1/run"
    assert captured["body"] == {}
    assert captured["idem"] is not None
    assert result["structuredContent"]["run_id"] == "r-1"


def test_trigger_monitor_deduped() -> None:
    captured: dict[str, Any] = {}
    with mock_api(_capture_handler(captured, 202, {"deduped": True})):
        result = asyncio.run(monitors.t_trigger_monitor({"monitor_id": "mon-1"}))
    assert result["structuredContent"]["deduped"] is True


def test_trigger_monitor_missing_id() -> None:
    result = asyncio.run(monitors.t_trigger_monitor({}))
    assert result["isError"] is True


# --- delete -------------------------------------------------------------------


def test_delete_monitor_happy_path() -> None:
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(204)

    with mock_api(_handler):
        result = asyncio.run(monitors.t_delete_monitor({"monitor_id": "mon-1"}))
    assert captured["method"] == "DELETE"
    assert captured["path"] == "/api/v1/monitors/mon-1"
    assert captured["idem"] is not None
    assert result["structuredContent"] == {"deleted": True, "monitor_id": "mon-1"}


def test_delete_monitor_missing_id() -> None:
    result = asyncio.run(monitors.t_delete_monitor({}))
    assert result["isError"] is True


# --- rotate webhook -----------------------------------------------------------


def test_rotate_webhook_happy_path() -> None:
    captured: dict[str, Any] = {}
    payload = {"monitor_id": "mon-1", "webhook_secret": "sk_new", "header": "X-Prufa", "hint": "h"}
    with mock_api(_capture_handler(captured, 200, payload)):
        result = asyncio.run(monitors.t_rotate_monitor_webhook({"monitor_id": "mon-1"}))
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/monitors/mon-1/webhook/rotate"
    assert captured["idem"] is not None
    assert result["structuredContent"]["webhook_secret"] == "sk_new"


def test_rotate_webhook_missing_id() -> None:
    result = asyncio.run(monitors.t_rotate_monitor_webhook({}))
    assert result["isError"] is True


# --- deliveries ---------------------------------------------------------------


def test_list_deliveries_happy_path() -> None:
    captured: dict[str, Any] = {}
    payload = {
        "deliveries": [],
        "never_fired": True,
        "snippets": {"curl": "curl ...", "github_actions": "...", "gitlab_ci": "..."},
    }
    with mock_api(_capture_handler(captured, 200, payload)):
        result = asyncio.run(monitors.t_list_monitor_deliveries({"monitor_id": "mon-1"}))
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/monitors/mon-1/deliveries"
    assert result["structuredContent"]["never_fired"] is True


def test_list_deliveries_missing_id() -> None:
    result = asyncio.run(monitors.t_list_monitor_deliveries({}))
    assert result["isError"] is True
