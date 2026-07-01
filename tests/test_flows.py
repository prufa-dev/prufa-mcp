"""Tests for the flows-domain tools.

Uses the ``prufa_mcp.http._TRANSPORT`` seam (an httpx.MockTransport) to record
the exact request each tool makes and assert its method + path + body, plus the
returned MCP result shape.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx
import pytest

import prufa_mcp.http as http
from prufa_mcp.registry import REGISTRY
from prufa_mcp.tools import flows


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


def _capture(status: int = 200, json_body: dict | None = None):
    """A handler that records method/path/body and returns a fixed response."""
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["idem"] = request.headers.get("Idempotency-Key")
        if request.content:
            captured["body"] = json.loads(request.content)
        else:
            captured["body"] = None
        return httpx.Response(status, json=json_body if json_body is not None else {})

    return captured, _handler


# --- registration -------------------------------------------------------------


def test_all_flow_tools_registered() -> None:
    for name in (
        "prufa_create_flow",
        "prufa_confirm_flow",
        "prufa_run_flow",
        "prufa_set_flow_credentials",
        "prufa_list_flows",
        "prufa_get_flow",
        "prufa_edit_flow",
        "prufa_delete_flow",
    ):
        assert REGISTRY.get(name) is not None, f"{name} not registered"


def test_mutating_tools_expose_idempotency_prop() -> None:
    for name in (
        "prufa_create_flow",
        "prufa_confirm_flow",
        "prufa_run_flow",
        "prufa_set_flow_credentials",
        "prufa_edit_flow",
        "prufa_delete_flow",
    ):
        props = REGISTRY.get(name).input_schema["properties"]
        assert "idempotency_key" in props, f"{name} missing idempotency_key prop"


# --- create_flow --------------------------------------------------------------


def test_create_flow_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured, handler = _capture(201, {"flow_id": "f1", "status": "draft"})
    with mock_api(handler):
        result = asyncio.run(
            flows.t_create_flow(
                {"url": "https://x.dev", "test_case": "log in and check out", "name": "checkout"}
            )
        )
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/flows"
    assert captured["body"] == {
        "url": "https://x.dev",
        "test_case": "log in and check out",
        "name": "checkout",
    }
    assert captured["idem"]  # idempotency key forwarded
    assert result["structuredContent"]["status"] == "draft"


def test_create_flow_omits_optional_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured, handler = _capture(201, {"flow_id": "f1"})
    with mock_api(handler):
        asyncio.run(flows.t_create_flow({"url": "https://x.dev", "test_case": "tc"}))
    assert captured["body"] == {"url": "https://x.dev", "test_case": "tc"}


def test_create_flow_missing_url() -> None:
    result = asyncio.run(flows.t_create_flow({"test_case": "tc"}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_create_flow_missing_test_case() -> None:
    result = asyncio.run(flows.t_create_flow({"url": "https://x.dev"}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


# --- confirm_flow -------------------------------------------------------------


def test_confirm_flow_no_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured, handler = _capture(200, {"flow_id": "f1", "status": "confirmed"})
    with mock_api(handler):
        result = asyncio.run(flows.t_confirm_flow({"flow_id": "f1"}))
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/flows/f1/confirm"
    assert captured["body"] is None  # no body when spec omitted
    assert result["structuredContent"]["status"] == "confirmed"


def test_confirm_flow_with_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured, handler = _capture(200, {"status": "confirmed"})
    spec = {"steps": [{"action": "click"}]}
    with mock_api(handler):
        asyncio.run(flows.t_confirm_flow({"flow_id": "f1", "spec": spec}))
    assert captured["body"] == {"spec": spec}


def test_confirm_flow_missing_id() -> None:
    result = asyncio.run(flows.t_confirm_flow({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


# --- run_flow -----------------------------------------------------------------


def test_run_flow_with_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured, handler = _capture(
        202, {"run_id": "r1", "report_url": "https://x.dev/r/tok", "usage": {}}
    )
    creds = {"EMAIL": "a@b.c", "PASSWORD": "pw"}
    with mock_api(handler):
        result = asyncio.run(flows.t_run_flow({"flow_id": "f1", "credentials": creds}))
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/flows/f1/run"
    assert captured["body"] == {"credentials": creds}
    assert result["structuredContent"]["run_id"] == "r1"


def test_run_flow_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured, handler = _capture(202, {"run_id": "r1"})
    with mock_api(handler):
        asyncio.run(flows.t_run_flow({"flow_id": "f1"}))
    assert captured["body"] == {}


def test_run_flow_attaches_upsell_on_low_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    _, handler = _capture(
        202,
        {
            "run_id": "r1",
            "usage": {"tier": "agent_temp", "available_credits": 1, "calls_included": 50},
        },
    )
    with mock_api(handler):
        result = asyncio.run(flows.t_run_flow({"flow_id": "f1"}))
    up = result["structuredContent"]["upsell"]
    assert up["upgrade_tool"] == "prufa_upgrade_plan"
    assert "message_for_human" in up


def test_run_flow_no_upsell_when_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    _, handler = _capture(
        202,
        {
            "run_id": "r1",
            "usage": {"tier": "agent_temp", "available_credits": 45, "calls_included": 50},
        },
    )
    with mock_api(handler):
        result = asyncio.run(flows.t_run_flow({"flow_id": "f1"}))
    assert "upsell" not in result["structuredContent"]


def test_run_flow_missing_id() -> None:
    result = asyncio.run(flows.t_run_flow({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


# --- set_flow_credentials -----------------------------------------------------


def test_set_credentials_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured, handler = _capture(200, {"flow_id": "f1", "stored": ["EMAIL", "PASSWORD"]})
    creds = {"EMAIL": "a@b.c", "PASSWORD": "pw"}
    with mock_api(handler):
        result = asyncio.run(
            flows.t_set_flow_credentials({"flow_id": "f1", "credentials": creds})
        )
    assert captured["method"] == "PUT"
    assert captured["path"] == "/api/v1/flows/f1/credentials"
    assert captured["body"] == {"credentials": creds}
    assert result["structuredContent"]["stored"] == ["EMAIL", "PASSWORD"]


def test_set_credentials_empty_object_rejected() -> None:
    result = asyncio.run(flows.t_set_flow_credentials({"flow_id": "f1", "credentials": {}}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_set_credentials_missing_flow_id() -> None:
    result = asyncio.run(flows.t_set_flow_credentials({"credentials": {"A": "1"}}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


# --- list / get ---------------------------------------------------------------


def test_list_flows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured, handler = _capture(200, {"flows": [{"flow_id": "f1"}]})
    with mock_api(handler):
        result = asyncio.run(flows.t_list_flows({}))
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/flows"
    assert result["structuredContent"]["flows"] == [{"flow_id": "f1"}]


def test_get_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured, handler = _capture(200, {"flow_id": "f1", "status": "confirmed"})
    with mock_api(handler):
        result = asyncio.run(flows.t_get_flow({"flow_id": "f1"}))
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/flows/f1"
    assert result["structuredContent"]["status"] == "confirmed"


def test_get_flow_missing_id() -> None:
    result = asyncio.run(flows.t_get_flow({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


# --- edit_flow ----------------------------------------------------------------


def test_edit_flow_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured, handler = _capture(200, {"flow_id": "f1", "status": "draft"})
    spec = {"steps": []}
    with mock_api(handler):
        result = asyncio.run(flows.t_edit_flow({"flow_id": "f1", "spec": spec}))
    assert captured["method"] == "PUT"
    assert captured["path"] == "/api/v1/flows/f1"
    assert captured["body"] == {"spec": spec}
    # editing returns the flow to draft
    assert result["structuredContent"]["status"] == "draft"


def test_edit_flow_missing_spec() -> None:
    result = asyncio.run(flows.t_edit_flow({"flow_id": "f1"}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_edit_flow_missing_id() -> None:
    result = asyncio.run(flows.t_edit_flow({"spec": {}}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


# --- delete_flow --------------------------------------------------------------


def test_delete_flow_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured, handler = _capture(204)  # 204 no content -> {}
    with mock_api(handler):
        result = asyncio.run(flows.t_delete_flow({"flow_id": "f1"}))
    assert captured["method"] == "DELETE"
    assert captured["path"] == "/api/v1/flows/f1"
    assert result["structuredContent"] == {"deleted": True, "flow_id": "f1"}


def test_delete_flow_missing_id() -> None:
    result = asyncio.run(flows.t_delete_flow({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_delete_flow_in_use_409_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"detail": {"code": "flow_in_use", "hint": "pause the monitor first"}},
        )

    with mock_api(_handler):
        result = asyncio.run(flows.t_delete_flow({"flow_id": "f1"}))
    body = result["structuredContent"]
    assert result["isError"] is True
    assert body["code"] == "flow_in_use"
    assert body["http_status"] == 409


# --- ApiError passthrough (404) -----------------------------------------------


def test_get_flow_404_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"detail": {"code": "not_found", "hint": "no such flow"}}
        )

    with mock_api(_handler):
        result = asyncio.run(flows.t_get_flow({"flow_id": "nope"}))
    body = result["structuredContent"]
    assert result["isError"] is True
    assert body["code"] == "not_found"
    assert body["http_status"] == 404
