"""Billing-domain tests: checkout / credits / portal.

Each tool makes TWO requests: first GET /api/v1/workspaces/current to resolve
the authenticated workspace id, then a POST to the relevant Stripe endpoint
carrying that workspace_id. The MockTransport handler branches on method/path
to serve both and to capture the POST body.
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
from prufa_mcp.tools import billing


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


def _two_step_handler(captured: dict[str, Any], post_json: dict, *, post_status: int = 200):
    """Handler that answers GET /workspaces/current then a POST, recording both."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/workspaces/current":
            captured["ws_path"] = request.url.path
            captured["ws_method"] = request.method
            return httpx.Response(200, json={"workspace_id": "ws-1", "tier": "agent_temp"})
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        captured["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(post_status, json=post_json)

    return _handler


# --- registry -----------------------------------------------------------------


def test_registry_has_billing_tools() -> None:
    for name in ("prufa_upgrade_plan", "prufa_buy_credits", "prufa_billing_portal"):
        td = REGISTRY.get(name)
        assert td is not None, f"{name} not registered"
        assert td.tier == "persistent"
        assert td.requires_auth is True
        # mutating tools expose the shared idempotency_key input
        assert "idempotency_key" in td.input_schema["properties"]


# --- prufa_upgrade_plan -------------------------------------------------------


def test_upgrade_plan_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}
    handler = _two_step_handler(
        captured,
        {"checkout_url": "https://stripe.test/c/abc", "session_id": "cs_1", "tier": "pro"},
    )

    with mock_api(handler):
        result = asyncio.run(
            billing.t_upgrade_plan({"tier": "pro", "success_url": "https://x/ok"})
        )

    assert captured["ws_method"] == "GET"
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/billing/checkout"
    assert captured["body"] == {
        "workspace_id": "ws-1",
        "tier": "pro",
        "success_url": "https://x/ok",
    }
    assert captured["idem"]  # idempotency key forwarded
    body = result["structuredContent"]
    assert body["checkout_url"] == "https://stripe.test/c/abc"
    assert "card-first trial" in body["action_for_human"]


def test_upgrade_plan_omits_unset_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}
    handler = _two_step_handler(
        captured,
        {"checkout_url": "https://stripe.test/c/abc", "session_id": "cs_1", "tier": "starter"},
    )

    with mock_api(handler):
        asyncio.run(billing.t_upgrade_plan({"tier": "starter"}))

    assert captured["body"] == {"workspace_id": "ws-1", "tier": "starter"}
    assert "success_url" not in captured["body"]
    assert "cancel_url" not in captured["body"]


def test_upgrade_plan_missing_tier() -> None:
    result = asyncio.run(billing.t_upgrade_plan({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_upgrade_plan_idempotency_key_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}
    handler = _two_step_handler(
        captured, {"checkout_url": "u", "session_id": "s", "tier": "pro"}
    )

    with mock_api(handler):
        asyncio.run(billing.t_upgrade_plan({"tier": "pro", "idempotency_key": "fixed-key"}))

    assert captured["idem"] == "fixed-key"


# --- prufa_buy_credits --------------------------------------------------------


def test_buy_credits_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}
    handler = _two_step_handler(
        captured,
        {"checkout_url": "https://stripe.test/c/cr", "session_id": "cs_2", "credits": 500},
    )

    with mock_api(handler):
        result = asyncio.run(
            billing.t_buy_credits({"credits": 500, "cancel_url": "https://x/no"})
        )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/billing/credits/checkout"
    assert captured["body"] == {
        "workspace_id": "ws-1",
        "credits": 500,
        "cancel_url": "https://x/no",
    }
    body = result["structuredContent"]
    assert body["credits"] == 500
    assert "prepaid credit pack" in body["action_for_human"]


def test_buy_credits_missing_credits() -> None:
    result = asyncio.run(billing.t_buy_credits({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


# --- prufa_billing_portal -----------------------------------------------------


def test_billing_portal_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}
    handler = _two_step_handler(captured, {"portal_url": "https://stripe.test/p/xyz"})

    with mock_api(handler):
        result = asyncio.run(
            billing.t_billing_portal({"return_url": "https://x/back"})
        )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/billing/portal"
    assert captured["body"] == {"workspace_id": "ws-1", "return_url": "https://x/back"}
    body = result["structuredContent"]
    assert body["portal_url"] == "https://stripe.test/p/xyz"
    assert "manage the subscription" in body["action_for_human"]


def test_billing_portal_omits_return_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}
    handler = _two_step_handler(captured, {"portal_url": "https://stripe.test/p/xyz"})

    with mock_api(handler):
        asyncio.run(billing.t_billing_portal({}))

    assert captured["body"] == {"workspace_id": "ws-1"}


def test_billing_portal_no_stripe_customer_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 409 no_stripe_customer passes through as an isError with the code."""
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/workspaces/current":
            return httpx.Response(200, json={"workspace_id": "ws-1"})
        return httpx.Response(
            409,
            json={
                "detail": {
                    "code": "no_stripe_customer",
                    "hint": "subscribe first before opening the portal",
                }
            },
        )

    with mock_api(_handler):
        result = asyncio.run(billing.t_billing_portal({}))

    body = result["structuredContent"]
    assert result["isError"] is True
    assert body["code"] == "no_stripe_customer"
    assert body["http_status"] == 409


# --- ApiError passthrough (402 with checkout hint) ----------------------------


def test_upgrade_plan_402_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/workspaces/current":
            return httpx.Response(200, json={"workspace_id": "ws-1"})
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
        result = asyncio.run(billing.t_upgrade_plan({"tier": "team"}))

    body = result["structuredContent"]
    assert result["isError"] is True
    assert body["code"] == "tier_required"
    assert body["http_status"] == 402
    assert body["checkout_url"].endswith("/billing/checkout")


def test_workspace_resolve_error_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """If resolving the workspace fails, the ApiError is wrapped, not raised."""
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"detail": {"code": "not_found", "hint": "no workspace"}}
        )

    with mock_api(_handler):
        result = asyncio.run(billing.t_buy_credits({"credits": 10}))

    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "not_found"
