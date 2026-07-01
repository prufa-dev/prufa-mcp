"""Smoke test: the server returns a clear missing_token error without a token."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import httpx

from prufa_mcp.audit import (
    get_report,
    run_audit,
    _api_base,
    _api_token,
    _share_token_from_report_url,
)


def test_run_audit_anonymous_sends_no_auth_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Without a token the audit runs ANONYMOUSLY (no key, no card) — it makes
    the request with NO Authorization header instead of short-circuiting."""
    monkeypatch.delenv("PRUFA_API_TOKEN", raising=False)
    monkeypatch.setenv("PRUFA_CONFIG", str(tmp_path / "none.json"))  # no config token
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(202, json={"run_id": "r", "status": "queued", "report_url": "/r/s"})

    transport = httpx.MockTransport(_handler)
    result = asyncio.run(_probe_run_audit(transport, "https://example.com", wait=False))

    assert captured["auth"] is None, "anonymous audit must not send an Authorization header"
    assert result["status"] == "queued"
    assert result["share_token"] == "s"


def test_get_report_anonymous_sends_no_auth_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Without a token, a share_token report fetch works anonymously (the
    by-token endpoint is public) — no Authorization header, real data back."""
    monkeypatch.delenv("PRUFA_API_TOKEN", raising=False)
    monkeypatch.setenv("PRUFA_CONFIG", str(tmp_path / "none.json"))
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        captured["path"] = request.url.path
        return httpx.Response(200, json={"status": "succeeded", "findings": []})

    transport = httpx.MockTransport(_handler)
    result = asyncio.run(_probe_get_report(transport, "some-share-token"))

    assert captured["auth"] is None, "anonymous report fetch must not send an Authorization header"
    assert captured["path"] == "/api/v1/reports/by-token/some-share-token"
    assert result["status"] == "succeeded"


# --- Bug-fix regression tests (v0.1.1) ----------------------------------------
# In v0.1.0 the demo against studyjunkie.co surfaced three real bugs:
#   1. wait=true didn't block (the API ignores wait on creation; we polled
#      internally — but only after v0.1.1).
#   2. get_report called /api/v1/reports/{id} — a non-existent endpoint.
#   3. The agent had to fall back to WebFetch on /r/<token> because the
#      OSS server never returned the report data.
# These tests pin the fixes.


def test_share_token_extraction() -> None:
    """The /r/<slug> URL must be parsed into the share_token."""
    assert _share_token_from_report_url("/r/vP3fPPIXz27KbsVMSBaS96exZiIATtYX") == "vP3fPPIXz27KbsVMSBaS96exZiIATtYX"
    assert _share_token_from_report_url("https://app.prufa.dev/r/abc123") == "abc123"
    assert _share_token_from_report_url(None) is None
    assert _share_token_from_report_url("") is None
    assert _share_token_from_report_url("/api/v1/audits/123") is None  # no /r/ in path


def test_get_report_routes_uuid_to_legacy_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real 8-4-4-4-12 UUID goes to /api/v1/audits/{uuid}/report.json."""
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"run_id": "abc", "status": "succeeded", "findings": []})

    transport = httpx.MockTransport(_handler)
    asyncio.run(_probe_get_report(transport, "11111111-2222-3333-4444-555555555555"))

    assert captured["path"] == "/api/v1/audits/11111111-2222-3333-4444-555555555555/report.json"


def test_get_report_routes_share_token_to_by_token_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-UUID slug goes to /api/v1/reports/by-token/{slug} — fixing the
    404 the agent hit in v0.1.0 when it passed the slug to the wrong endpoint."""
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"run_id": "internal-uuid", "status": "succeeded", "findings": []})

    transport = httpx.MockTransport(_handler)
    asyncio.run(_probe_get_report(transport, "vP3fPPIXz27KbsVMSBaS96exZiIATtYX"))

    assert captured["path"] == "/api/v1/reports/by-token/vP3fPPIXz27KbsVMSBaS96exZiIATtYX"


def test_get_report_404_includes_actionable_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 from the API must surface with a clear hint naming both
    possible identifiers — not a raw HTTPError."""
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    transport = httpx.MockTransport(_handler)
    result = asyncio.run(_probe_get_report(transport, "does-not-exist"))

    assert result["error"] == "not_found"
    assert "share_token" in result["hint"]
    assert "run_id" in result["hint"]
    assert result["http_status"] == 404


def test_run_audit_polls_until_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """wait=true must actually block until the audit completes, then
    return the report. This was broken in v0.1.0 because the API ignored
    the wait flag and the OSS server returned 'queued' immediately."""
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    monkeypatch.setattr("prufa_mcp.audit._POLL_INTERVAL_S", 0.0)  # no real sleeping in tests

    final_report = {
        "run_id": "11111111-2222-3333-4444-555555555555",
        "url": "https://example.com",
        "status": "succeeded",
        "findings": [{"severity": "critical", "kind": "tracking", "detail": "GA4 missing"}],
    }
    poll_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/v1/audits":
            return httpx.Response(
                202,
                json={
                    "run_id": "11111111-2222-3333-4444-555555555555",
                    "status": "queued",
                    "events_url": "/api/audits/.../events",
                    "report_url": "/r/vP3fPPIX",
                },
            )
        if "/api/v1/audits/" in request.url.path and "/report.json" not in request.url.path:
            poll_count["n"] += 1
            if poll_count["n"] >= 2:
                return httpx.Response(200, json={"status": "succeeded", "failure_reason": None})
            return httpx.Response(200, json={"status": "running", "failure_reason": None})
        if request.url.path == "/api/v1/reports/by-token/vP3fPPIX":
            return httpx.Response(200, json=final_report)
        return httpx.Response(404, json={"detail": "not found"})

    transport = httpx.MockTransport(_handler)
    result = asyncio.run(_probe_run_audit(transport, "https://example.com", wait=True))

    assert result["status"] == "succeeded", f"Expected 'succeeded', got {result}"
    assert result["findings"][0]["detail"] == "GA4 missing"
    assert result.get("share_token") == "vP3fPPIX"
    assert result.get("run_id") == "11111111-2222-3333-4444-555555555555"
    assert poll_count["n"] >= 2, "must have polled at least twice before fetching the report"


def test_run_audit_no_wait_returns_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """wait=false must return the queued state with share_token so the
    agent can poll via get_report(share_token=...) later."""
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={
                "run_id": "11111111-2222-3333-4444-555555555555",
                "status": "queued",
                "events_url": "/api/audits/.../events",
                "report_url": "/r/abc-slug",
            },
        )

    transport = httpx.MockTransport(_handler)
    result = asyncio.run(_probe_run_audit(transport, "https://example.com", wait=False))

    assert result["status"] == "queued"
    assert result["share_token"] == "abc-slug"
    assert result["report_url"] == "/r/abc-slug"
    assert result["run_id"] == "11111111-2222-3333-4444-555555555555"


# --- Retry-with-backoff (issue #1) --------------------------------------------
# The hosted API is rate-limited. A 429 must trigger exponential-backoff retries
# (1s/2s/4s) rather than surfacing the error on the first hit.


def test_run_audit_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 on audit creation is retried; the next attempt succeeds."""
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    monkeypatch.setattr("prufa_mcp.audit._RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))  # no real sleeping
    attempts = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, json={"detail": "rate limited"})
        return httpx.Response(
            202,
            json={
                "run_id": "11111111-2222-3333-4444-555555555555",
                "status": "queued",
                "report_url": "/r/abc-slug",
            },
        )

    transport = httpx.MockTransport(_handler)
    result = asyncio.run(_probe_run_audit(transport, "https://example.com", wait=False))

    assert attempts["n"] == 2, "should have retried once after the 429"
    assert result["status"] == "queued"
    assert result["share_token"] == "abc-slug"


def test_get_report_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 on report fetch is retried with backoff before succeeding."""
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    monkeypatch.setattr("prufa_mcp.audit._RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))
    attempts = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            return httpx.Response(429, json={"detail": "rate limited"})
        return httpx.Response(200, json={"run_id": "x", "status": "succeeded", "findings": []})

    transport = httpx.MockTransport(_handler)
    result = asyncio.run(_probe_get_report(transport, "some-share-token"))

    assert attempts["n"] == 3, "two 429s then success"
    assert result["status"] == "succeeded"


def test_get_report_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persistent 429 is retried 3 times (4 attempts total), then surfaces
    as a clean error rather than retrying forever."""
    monkeypatch.setenv("PRUFA_API_TOKEN", "test-token")
    monkeypatch.setattr("prufa_mcp.audit._RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))
    attempts = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429, json={"detail": "rate limited"})

    transport = httpx.MockTransport(_handler)
    result = asyncio.run(_probe_get_report(transport, "some-share-token"))

    assert attempts["n"] == 4, "1 initial attempt + 3 retries"
    assert result["error"] == "not_found"
    assert result["http_status"] == 429


# --- JSON config file (issue #2) ----------------------------------------------
# The server reads PRUFA_API_BASE / PRUFA_API_TOKEN from env, but config-as-code
# users can drop a JSON file at ~/.config/prufa/mcp.json (PRUFA_CONFIG overrides
# the path). Env vars take precedence over the file.


def test_config_file_provides_token_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """With no env token, the api_token is read from the config file."""
    monkeypatch.delenv("PRUFA_API_TOKEN", raising=False)
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"api_token": "from-config", "api_base": "https://cfg.example"}))
    monkeypatch.setenv("PRUFA_CONFIG", str(cfg))

    assert _api_token() == "from-config"
    assert _api_base() == "https://cfg.example"


def test_env_overrides_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Env vars win over the config file, so existing setups never change."""
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"api_token": "from-config", "api_base": "https://cfg.example"}))
    monkeypatch.setenv("PRUFA_CONFIG", str(cfg))
    monkeypatch.setenv("PRUFA_API_TOKEN", "from-env")
    monkeypatch.setenv("PRUFA_API_BASE", "https://env.example")

    assert _api_token() == "from-env"
    assert _api_base() == "https://env.example"


def test_api_base_defaults_when_nothing_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """No env, no config file → the hosted default."""
    monkeypatch.delenv("PRUFA_API_BASE", raising=False)
    monkeypatch.setenv("PRUFA_CONFIG", str(tmp_path / "does-not-exist.json"))
    assert _api_base() == "https://app.prufa.dev"


def test_malformed_config_falls_back_without_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: Any
) -> None:
    """A malformed config file is ignored with a stderr warning (no silent
    failure), and resolution falls back to env/defaults."""
    monkeypatch.delenv("PRUFA_API_TOKEN", raising=False)
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{not valid json")
    monkeypatch.setenv("PRUFA_CONFIG", str(cfg))

    assert _api_token() == ""  # falls back, doesn't raise
    assert _api_base() == "https://app.prufa.dev"
    assert "ignoring unreadable config" in capsys.readouterr().err


def test_config_token_flows_through_run_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """An audit authenticated purely via the config file uses both the
    config token (Bearer header) and config api_base (request host)."""
    monkeypatch.delenv("PRUFA_API_TOKEN", raising=False)
    monkeypatch.delenv("PRUFA_API_BASE", raising=False)
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"api_token": "cfg-token", "api_base": "https://cfg.example"}))
    monkeypatch.setenv("PRUFA_CONFIG", str(cfg))
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["host"] = request.url.host
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            202, json={"run_id": "r", "status": "queued", "report_url": "/r/slug"}
        )

    transport = httpx.MockTransport(_handler)
    result = asyncio.run(_probe_run_audit(transport, "https://example.com", wait=False))

    assert result["status"] == "queued"
    assert captured["host"] == "cfg.example"
    assert captured["auth"] == "Bearer cfg-token"


# --- Test helpers --------------------------------------------------------------


async def _probe_get_report(transport: httpx.MockTransport, report_id: str) -> dict[str, Any]:
    """Run get_report against a mock transport. Monkeypatches httpx inside
    audit.py for the duration of the call."""
    import prufa_mcp.audit as audit_mod
    import httpx as _httpx

    original_async_client = audit_mod.httpx.AsyncClient
    audit_mod.httpx.AsyncClient = lambda **kw: original_async_client(transport=transport, **kw)
    try:
        return await audit_mod.get_report(report_id=report_id)
    finally:
        audit_mod.httpx.AsyncClient = original_async_client


async def _probe_run_audit(transport: httpx.MockTransport, url: str, wait: bool) -> dict[str, Any]:
    import prufa_mcp.audit as audit_mod
    import httpx as _httpx

    original_async_client = audit_mod.httpx.AsyncClient
    audit_mod.httpx.AsyncClient = lambda **kw: original_async_client(transport=transport, **kw)
    try:
        return await audit_mod.run_audit(url=url, wait=wait)
    finally:
        audit_mod.httpx.AsyncClient = original_async_client
