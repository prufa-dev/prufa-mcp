"""Discovery-domain tool tests: DNS-verified domains + full-auto discovery runs.

Uses the ``prufa_mcp.http._TRANSPORT`` seam (an httpx.MockTransport) to record
the exact method/path/body each tool sends, without a live API.
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
from prufa_mcp.tools import discovery


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


# --- registration -------------------------------------------------------------


def test_all_tools_registered() -> None:
    for name in (
        "prufa_register_discovery_domain",
        "prufa_verify_discovery_domain",
        "prufa_list_discovery_domains",
        "prufa_revoke_discovery_domain",
        "prufa_run_discovery",
        "prufa_get_discovery",
    ):
        assert REGISTRY.get(name) is not None, f"{name} not registered"


def test_mutating_tools_expose_idempotency_prop() -> None:
    for name in (
        "prufa_register_discovery_domain",
        "prufa_verify_discovery_domain",
        "prufa_revoke_discovery_domain",
        "prufa_run_discovery",
    ):
        props = REGISTRY.get(name).input_schema["properties"]
        assert "idempotency_key" in props, f"{name} missing idempotency_key"
    # GETs do not carry it.
    for name in ("prufa_list_discovery_domains", "prufa_get_discovery"):
        props = REGISTRY.get(name).input_schema["properties"]
        assert "idempotency_key" not in props


def test_all_tools_persistent_and_authed() -> None:
    for name in (
        "prufa_register_discovery_domain",
        "prufa_verify_discovery_domain",
        "prufa_list_discovery_domains",
        "prufa_revoke_discovery_domain",
        "prufa_run_discovery",
        "prufa_get_discovery",
    ):
        td = REGISTRY.get(name)
        assert td.tier == "persistent"
        assert td.requires_auth is True


# --- prufa_register_discovery_domain ------------------------------------------


def test_register_domain_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["idem"] = request.headers.get("Idempotency-Key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "id": "dom-1",
                "domain": "app.example.com",
                "status": "pending",
                "verification": {
                    "method": "dns-txt",
                    "record_name": "_prufa.app.example.com",
                    "record_type": "TXT",
                    "record_value": "prufa-verify=abc",
                    "hint": "add this TXT record",
                },
            },
        )

    with mock_api(_handler):
        result = asyncio.run(
            discovery.t_register_discovery_domain(
                {"domain": "app.example.com", "test_payments_opt_in": True}
            )
        )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/discovery/domains"
    assert captured["idem"]  # forwarded
    assert captured["body"] == {
        "domain": "app.example.com",
        "test_payments_opt_in": True,
    }
    body = result["structuredContent"]
    assert body["verification"]["record_type"] == "TXT"
    assert body["status"] == "pending"


def test_register_domain_defaults_opt_in_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "dom-1"})

    with mock_api(_handler):
        asyncio.run(discovery.t_register_discovery_domain({"domain": "x.example.com"}))

    assert captured["body"]["test_payments_opt_in"] is False


def test_register_domain_missing_arg() -> None:
    result = asyncio.run(discovery.t_register_discovery_domain({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_register_domain_uses_explicit_idem_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(201, json={"id": "dom-1"})

    with mock_api(_handler):
        asyncio.run(
            discovery.t_register_discovery_domain(
                {"domain": "x.example.com", "idempotency_key": "fixed-key-42"}
            )
        )

    assert captured["idem"] == "fixed-key-42"


# --- prufa_verify_discovery_domain --------------------------------------------


def test_verify_domain_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"id": "dom-1", "status": "verified"})

    with mock_api(_handler):
        result = asyncio.run(
            discovery.t_verify_discovery_domain({"domain_id": "dom-1"})
        )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/discovery/domains/dom-1/verify"
    assert captured["idem"]
    assert result["structuredContent"]["status"] == "verified"


def test_verify_domain_missing_arg() -> None:
    result = asyncio.run(discovery.t_verify_discovery_domain({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


# --- prufa_list_discovery_domains ---------------------------------------------


def test_list_domains_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={"domains": [{"id": "dom-1", "domain": "a.example.com", "status": "verified"}]},
        )

    with mock_api(_handler):
        result = asyncio.run(discovery.t_list_discovery_domains({}))

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/discovery/domains"
    assert result["structuredContent"]["domains"][0]["id"] == "dom-1"


# --- prufa_revoke_discovery_domain --------------------------------------------


def test_revoke_domain_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"id": "dom-1", "identities_torn_down": 3})

    with mock_api(_handler):
        result = asyncio.run(
            discovery.t_revoke_discovery_domain({"domain_id": "dom-1"})
        )

    assert captured["method"] == "DELETE"
    assert captured["path"] == "/api/v1/discovery/domains/dom-1"
    assert captured["idem"]
    assert result["structuredContent"]["identities_torn_down"] == 3


def test_revoke_domain_missing_arg() -> None:
    result = asyncio.run(discovery.t_revoke_discovery_domain({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


# --- prufa_run_discovery ------------------------------------------------------


def test_run_discovery_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["idem"] = request.headers.get("Idempotency-Key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            202,
            json={
                "discovery_id": "disc-9",
                "status": "queued",
                "url": "https://app.example.com/",
                "result_url": "https://app.prufa.dev/d/disc-9",
            },
        )

    with mock_api(_handler):
        result = asyncio.run(
            discovery.t_run_discovery({"url": "https://app.example.com/"})
        )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/discoveries"
    assert captured["idem"]
    assert captured["body"] == {"url": "https://app.example.com/"}
    assert result["structuredContent"]["discovery_id"] == "disc-9"


def test_run_discovery_missing_arg() -> None:
    result = asyncio.run(discovery.t_run_discovery({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_run_discovery_403_domain_not_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """403 domain_not_authorized passes through with its hint."""
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "detail": {
                    "code": "domain_not_authorized",
                    "hint": "register + verify a discovery domain covering this host first",
                }
            },
        )

    with mock_api(_handler):
        result = asyncio.run(
            discovery.t_run_discovery({"url": "https://unowned.example.com/"})
        )

    body = result["structuredContent"]
    assert result["isError"] is True
    assert body["code"] == "domain_not_authorized"
    assert body["http_status"] == 403
    assert "verify" in body["hint"]


def test_run_discovery_402_tier_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """402 keeps its checkout_url extra for the human-facing upgrade path."""
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={
                "detail": {
                    "code": "tier_required",
                    "hint": "discovery needs a paid or trial plan",
                    "checkout_url": "https://app.prufa.dev/api/v1/billing/checkout",
                }
            },
        )

    with mock_api(_handler):
        result = asyncio.run(
            discovery.t_run_discovery({"url": "https://app.example.com/"})
        )

    body = result["structuredContent"]
    assert result["isError"] is True
    assert body["code"] == "tier_required"
    assert body["http_status"] == 402
    assert body["checkout_url"].endswith("/billing/checkout")


# --- prufa_get_discovery ------------------------------------------------------


def test_get_discovery_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "discovery_id": "disc-9",
                "status": "succeeded",
                "failure_reason": None,
                "url": "https://app.example.com/",
                "flows": [
                    {
                        "name": "signup",
                        "kind": "draft",
                        "entry_url": "https://app.example.com/signup",
                        "status": "draft",
                        "flow_id": "flow-1",
                        "eval_score": 0.9,
                        "detail": "email + password signup",
                        "confirm_url": "https://app.prufa.dev/flows/flow-1/confirm",
                    }
                ],
            },
        )

    with mock_api(_handler):
        result = asyncio.run(discovery.t_get_discovery({"discovery_id": "disc-9"}))

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/discoveries/disc-9"
    assert result["structuredContent"]["flows"][0]["flow_id"] == "flow-1"


def test_get_discovery_missing_arg() -> None:
    result = asyncio.run(discovery.t_get_discovery({}))
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"


def test_get_discovery_404_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"detail": {"code": "not_found", "hint": "no such discovery"}},
        )

    with mock_api(_handler):
        result = asyncio.run(discovery.t_get_discovery({"discovery_id": "nope"}))

    body = result["structuredContent"]
    assert result["isError"] is True
    assert body["code"] == "not_found"
    assert body["http_status"] == 404
