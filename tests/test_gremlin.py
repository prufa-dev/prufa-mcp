"""Tests for the gremlin domain tools.

Uses the ``prufa_mcp.http._TRANSPORT`` seam (an httpx.MockTransport) to record
the exact requests each tool makes without a live API. Polling interval is
monkeypatched to 0.0 so the wait=True path runs instantly.
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
from prufa_mcp.tools import gremlin


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
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gremlin, "_POLL_INTERVAL_S", 0.0)


# --- registry wiring ----------------------------------------------------------


def test_all_tools_registered() -> None:
    for name in (
        "prufa_run_gremlin",
        "prufa_authorize_domain",
        "prufa_list_gremlin_domains",
        "prufa_rerun_gremlin",
        "prufa_gremlin_saved_logins",
        "prufa_promote_gremlin_path",
    ):
        assert REGISTRY.get(name) is not None, f"{name} not registered"


def test_run_gremlin_is_public() -> None:
    assert REGISTRY.get("prufa_run_gremlin").requires_auth is False


def test_mutating_tools_have_idempotency_prop() -> None:
    for name in ("prufa_run_gremlin", "prufa_rerun_gremlin", "prufa_promote_gremlin_path"):
        props = REGISTRY.get(name).input_schema["properties"]
        assert "idempotency_key" in props, f"{name} missing idempotency_key prop"


def test_run_gremlin_credentials_schema() -> None:
    creds = REGISTRY.get("prufa_run_gremlin").input_schema["properties"]["credentials"]
    assert creds["required"] == ["email", "password"]
    assert creds["additionalProperties"] is False


# --- prufa_run_gremlin --------------------------------------------------------


def test_run_gremlin_no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(
            202,
            json={"run_id": "g-1", "status": "queued", "persona": "hostile", "step_cap": 40},
        )

    with mock_api(_handler):
        result = asyncio.run(
            gremlin.t_run_gremlin(
                {
                    "url": "https://acme.com",
                    "persona": "hostile",
                    "direction": "break checkout",
                    "credentials": {"email": "a@b.co", "password": "pw"},
                    "wait": False,
                }
            )
        )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/gremlin"
    assert captured["body"] == {
        "url": "https://acme.com",
        "persona": "hostile",
        "direction": "break checkout",
        "credentials": {"email": "a@b.co", "password": "pw"},
    }
    assert captured["idem"]  # a key was forwarded
    assert result["structuredContent"]["run_id"] == "g-1"


def test_run_gremlin_body_omits_absent_optionals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(202, json={"run_id": "g-1", "status": "queued"})

    with mock_api(_handler):
        asyncio.run(gremlin.t_run_gremlin({"url": "https://acme.com", "wait": False}))

    assert captured["body"] == {"url": "https://acme.com"}


def test_run_gremlin_missing_url() -> None:
    result = asyncio.run(gremlin.t_run_gremlin({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_run_gremlin_wait_polls_to_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    calls: list[tuple[str, str]] = []
    state = {"polls": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/api/v1/gremlin":
            return httpx.Response(202, json={"run_id": "g-9", "status": "queued"})
        if request.url.path == "/api/v1/audits/g-9":
            state["polls"] += 1
            status = "running" if state["polls"] < 2 else "succeeded"
            return httpx.Response(200, json={"status": status})
        if request.url.path == "/api/v1/audits/g-9/report.json":
            return httpx.Response(200, json={"grade": "D", "paths": []})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    with mock_api(_handler):
        result = asyncio.run(gremlin.t_run_gremlin({"url": "https://acme.com"}))

    assert ("GET", "/api/v1/audits/g-9/report.json") in calls
    assert result["structuredContent"]["grade"] == "D"


def test_run_gremlin_wait_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    monkeypatch.setattr(gremlin, "_POLL_MAX_ITERS", 3)

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "g-2", "status": "queued"})
        return httpx.Response(200, json={"status": "running"})

    with mock_api(_handler):
        result = asyncio.run(gremlin.t_run_gremlin({"url": "https://acme.com"}))

    assert result["isError"] is True
    body = result["structuredContent"]
    assert body["code"] == "gremlin_timeout"
    assert body["run_id"] == "g-2"


def test_run_gremlin_attaches_upsell_when_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={
                "run_id": "g-3",
                "status": "queued",
                "usage": {"tier": "agent_temp", "available_credits": 1, "calls_included": 50},
            },
        )

    with mock_api(_handler):
        result = asyncio.run(gremlin.t_run_gremlin({"url": "https://acme.com", "wait": False}))

    body = result["structuredContent"]
    assert "upsell" in body
    assert body["upsell"]["upgrade_tool"] == "prufa_upgrade_plan"


def test_run_gremlin_402_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={
                "detail": {
                    "code": "tier_required",
                    "hint": "upgrade to run the full gremlin",
                    "checkout_url": "https://prufa.dev/api/v1/billing/checkout",
                }
            },
        )

    with mock_api(_handler):
        result = asyncio.run(gremlin.t_run_gremlin({"url": "https://acme.com", "wait": False}))

    body = result["structuredContent"]
    assert result["isError"] is True
    assert body["code"] == "tier_required"
    assert body["http_status"] == 402
    assert body["checkout_url"].endswith("/billing/checkout")


# --- prufa_authorize_domain ---------------------------------------------------


def test_authorize_domain_put(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"host": "staging.acme.com", "allow_mutation": True, "note": "mine", "warning": None},
        )

    with mock_api(_handler):
        result = asyncio.run(
            gremlin.t_authorize_domain({"host": "staging.acme.com", "note": "mine"})
        )

    assert captured["method"] == "PUT"
    assert captured["path"] == "/api/v1/gremlin/domains"
    assert captured["body"] == {"host": "staging.acme.com", "allow_mutation": True, "note": "mine"}
    assert result["structuredContent"]["host"] == "staging.acme.com"


def test_authorize_domain_revoke_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"host": "h", "allow_mutation": False})

    with mock_api(_handler):
        asyncio.run(gremlin.t_authorize_domain({"host": "h", "allow_mutation": False}))

    assert captured["body"] == {"host": "h", "allow_mutation": False}


def test_authorize_domain_missing_host() -> None:
    result = asyncio.run(gremlin.t_authorize_domain({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


# --- prufa_list_gremlin_domains -----------------------------------------------


def test_list_gremlin_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"domains": [], "default": "dry_run"})

    with mock_api(_handler):
        result = asyncio.run(gremlin.t_list_gremlin_domains({}))

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/gremlin/domains"
    assert result["structuredContent"]["default"] == "dry_run"


def test_list_gremlin_domains_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": {"code": "not_found", "hint": "nope"}})

    with mock_api(_handler):
        result = asyncio.run(gremlin.t_list_gremlin_domains({}))

    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "not_found"
    assert result["structuredContent"]["http_status"] == 404


# --- prufa_rerun_gremlin ------------------------------------------------------


def test_rerun_gremlin_no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(202, json={"run_id": "g-new", "status": "queued"})

    with mock_api(_handler):
        result = asyncio.run(gremlin.t_rerun_gremlin({"run_id": "g-old", "wait": False}))

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/gremlin/g-old/rerun"
    assert captured["idem"]
    assert result["structuredContent"]["run_id"] == "g-new"


def test_rerun_gremlin_missing_run_id() -> None:
    result = asyncio.run(gremlin.t_rerun_gremlin({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_rerun_gremlin_wait_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "g-r2", "status": "queued"})
        if request.url.path == "/api/v1/audits/g-r2":
            return httpx.Response(200, json={"status": "failed"})
        if request.url.path == "/api/v1/audits/g-r2/report.json":
            return httpx.Response(200, json={"grade": "F"})
        raise AssertionError(f"unexpected {request.url.path}")

    with mock_api(_handler):
        result = asyncio.run(gremlin.t_rerun_gremlin({"run_id": "g-old"}))

    assert result["structuredContent"]["grade"] == "F"


# --- prufa_gremlin_saved_logins -----------------------------------------------


def test_saved_logins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"logins": [{"run_id": "g-1", "email": "a@b.co"}]})

    with mock_api(_handler):
        result = asyncio.run(gremlin.t_gremlin_saved_logins({}))

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/gremlin/saved-logins"
    assert result["structuredContent"]["logins"][0]["run_id"] == "g-1"


# --- prufa_promote_gremlin_path -----------------------------------------------


def test_promote_gremlin_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(
            201, json={"flow_id": "f-1", "status": "draft", "review_url": "https://x/r"}
        )

    with mock_api(_handler):
        result = asyncio.run(
            gremlin.t_promote_gremlin_path({"share_token": "tok", "path_index": 0})
        )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/gremlin/reports/tok/flows"
    assert captured["body"] == {"path_index": 0}
    assert captured["idem"]
    assert result["structuredContent"]["status"] == "draft"


def test_promote_gremlin_path_missing_share_token() -> None:
    result = asyncio.run(gremlin.t_promote_gremlin_path({"path_index": 0}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_promote_gremlin_path_missing_index() -> None:
    result = asyncio.run(gremlin.t_promote_gremlin_path({"share_token": "tok"}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"
