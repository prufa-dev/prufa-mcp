"""Thin client for Prufa's hosted audit API.

The OSS MCP server is a thin client. Real audit execution, deterministic
checks (BeaconEvent analyzers, consent rules), Playwright orchestration,
and the human-readable report live in the hosted product. This file
proxies audit-trigger and report-fetch calls to that hosted API.

The audit API is asynchronous: POST /api/v1/audits always returns 202
with status "queued". To honor `wait=True`, this client polls the run
status endpoint until the audit reaches a terminal state, then fetches
the report. The shared public-by-token endpoint (`/api/v1/reports/by-
token/{share_token}`) is the canonical way to read the report when only
the public slug is known — matching the slug the agent sees in the
audit creation response (`report_url: /r/<slug>`).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx


_DEFAULT_API_BASE = "https://app.prufa.dev"


def _config_path() -> Path:
    """Location of the optional JSON config file.

    Defaults to ``~/.config/prufa/mcp.json`` (honoring ``XDG_CONFIG_HOME``).
    ``PRUFA_CONFIG`` overrides the path outright — handy for tests and for
    pinning a config in non-standard layouts.
    """
    override = os.environ.get("PRUFA_CONFIG")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "prufa" / "mcp.json"


def _load_config() -> dict[str, Any]:
    """Read the optional JSON config file.

    Returns an empty dict when the file is absent. A present-but-malformed
    file is not swallowed silently (per the no-silent-failures invariant):
    we warn on stderr — which is safe, since the MCP protocol owns stdout —
    and fall back to env/defaults rather than crashing the server.
    """
    path = _config_path()
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"prufa-mcp: ignoring unreadable config at {path}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"prufa-mcp: ignoring config at {path}: expected a JSON object", file=sys.stderr)
        return {}
    return data


def _api_token() -> str:
    """Resolve the API token fresh on every call.

    Precedence: ``PRUFA_API_TOKEN`` env var (so runtime env always wins and
    token rotation needs no restart), then the ``PRUFA_API_KEY`` alias, then
    ``api_token`` (or the ``api_key`` alias) from the config file, then empty.
    Module-level capture (the v0.1.0 pattern) broke tests that set the env var
    after import — reading per-call avoids that.

    ``PRUFA_API_KEY`` is accepted as an alias because early integration docs
    (and the CLI) name the variable that way; honoring it means a config that
    sets ``PRUFA_API_KEY`` authenticates instead of silently running
    anonymous (no-silent-failures). ``PRUFA_API_TOKEN`` is canonical.
    """
    env = os.environ.get("PRUFA_API_TOKEN") or os.environ.get("PRUFA_API_KEY")
    if env:
        return env
    cfg = _load_config()
    return str(cfg.get("api_token") or cfg.get("api_key") or "")


def _api_base() -> str:
    """Resolve the API base URL.

    Precedence mirrors :func:`_api_token`: ``PRUFA_API_BASE`` env var, then
    ``api_base`` from the config file, then the hosted default.
    """
    env = os.environ.get("PRUFA_API_BASE")
    if env:
        return env
    return str(_load_config().get("api_base") or _DEFAULT_API_BASE)


# The audit API takes ~25-60s for typical sites; cap polling at 90s
# to match the monorepo MCP server's behavior.
_POLL_INTERVAL_S = 3.0
_POLL_MAX_ITERS = 30

# The hosted API is rate-limited. On HTTP 429 the OSS client retries with
# exponential backoff before giving up: one initial attempt, then retries
# sleeping 1s, 2s, 4s. Non-429 responses are returned immediately; network
# errors are not swallowed (they propagate to the caller).
_RETRY_BACKOFFS_S = (1.0, 2.0, 4.0)


async def _send_with_retry(send: Any, *args: Any, **kwargs: Any) -> httpx.Response:
    """Issue an httpx request, retrying on HTTP 429 with exponential backoff.

    `send` is a bound httpx method (e.g. ``client.get`` / ``client.post``).
    Makes one initial attempt, then up to ``len(_RETRY_BACKOFFS_S)`` retries,
    sleeping the scheduled backoff before each one. Returns the final
    response — including a 429 if every attempt was rate-limited — so the
    caller's existing status handling still applies.
    """
    response = await send(*args, **kwargs)
    for backoff in _RETRY_BACKOFFS_S:
        if response.status_code != 429:
            return response
        await asyncio.sleep(backoff)
        response = await send(*args, **kwargs)
    return response


_UUID_LIKE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _share_token_from_report_url(report_url: str | None) -> str | None:
    """Extract the share_token slug from a /r/<token> URL.

    Same helper as the monorepo MCP server — duplicated here so the
    OSS package is self-contained.
    """
    if not report_url or "/r/" not in report_url:
        return None
    tail = report_url.split("/r/", 1)[-1]
    return tail.split("/")[0].split("?")[0] or None


def _auth_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Attach the Bearer header only when a token is configured.

    The public audit + report endpoints work anonymously (POST /api/v1/audits
    and the /reports/by-token/ read take no auth — the free 60-second audit is
    the first tool call, no key, no card). So a token-less call proceeds
    UNAUTHENTICATED instead of short-circuiting; a token, when present, just
    lifts the anonymous per-IP rate limit and attributes the run.
    """
    headers: dict[str, str] = dict(extra or {})
    token = _api_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def run_audit(*, url: str, wait: bool = True) -> dict[str, Any]:
    """Trigger a public-page audit on a URL.

    When `wait` is True (default), blocks until the audit reaches a
    terminal state and returns the JSON report. When False, returns
    immediately with the queued state — caller polls via `get_report`.

    No token required: the audit runs anonymously (rate-limited per IP). A
    PRUFA_API_TOKEN, when set, attributes the run to the workspace and lifts
    the anonymous limit.
    """
    headers = _auth_headers({"Idempotency-Key": f"mcp-{url}"})
    async with httpx.AsyncClient(timeout=30.0) as client:
        # The API ignores `wait` on creation — it always returns 202 queued.
        create = await _send_with_retry(
            client.post,
            f"{_api_base()}/api/v1/audits",
            json={"url": url},
            headers=headers,
        )
        create.raise_for_status()
        created = create.json()

    run_id = created.get("run_id")
    report_url = created.get("report_url")
    share_token = _share_token_from_report_url(report_url)
    base: dict[str, Any] = {
        "run_id": run_id,
        "status": created.get("status"),
        "report_url": report_url,
    }
    if share_token:
        base["share_token"] = share_token

    if not wait or not run_id:
        return base

    # Poll the run until terminal, then fetch the report via the
    # by-token endpoint (public, no auth quirks).
    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(_POLL_MAX_ITERS):
            await asyncio.sleep(_POLL_INTERVAL_S)
            try:
                run_resp = await _send_with_retry(
                    client.get,
                    f"{_api_base()}/api/v1/audits/{run_id}",
                    headers=_auth_headers(),
                )
            except httpx.HTTPError:
                continue
            if run_resp.status_code != 200:
                continue
            run = run_resp.json()
            if run.get("status") in {"succeeded", "failed", "blocked", "timeout"}:
                if share_token:
                    rep_resp = await _send_with_retry(
                        client.get,
                        f"{_api_base()}/api/v1/reports/by-token/{share_token}",
                        headers=_auth_headers(),
                    )
                else:
                    rep_resp = await _send_with_retry(
                        client.get,
                        f"{_api_base()}/api/v1/audits/{run_id}/report.json",
                        headers=_auth_headers(),
                    )
                if rep_resp.status_code == 200:
                    report = rep_resp.json()
                    if isinstance(report, dict):
                        report.setdefault("run_id", run_id)
                        report.setdefault("report_url", report_url)
                        if share_token:
                            report.setdefault("share_token", share_token)
                    return report
                # Report not ready yet — return a status object so the
                # agent has the identifiers and can poll manually.
                return {
                    "run_id": run_id,
                    "status": run.get("status"),
                    "report_url": report_url,
                    "share_token": share_token,
                    "failure_reason": run.get("failure_reason"),
                    "report_not_ready": True,
                }
    # Timeout — return base info so the agent can poll manually.
    return {
        "run_id": run_id,
        "status": "timeout",
        "report_url": report_url,
        "share_token": share_token,
        "hint": "audit did not complete within 90s; poll with get_report(report_id=share_token)",
    }


async def get_report(*, report_id: str) -> dict[str, Any]:
    """Fetch a shareable report.

    `report_id` may be either the internal run UUID or the public
    share_token slug (from /r/<token>). The slug is what the agent
    sees in the audit creation response — it's the recommended call
    shape. UUIDs (8-4-4-4-12 hex) are routed to the legacy auth
    endpoint; everything else is treated as a share_token.
    """
    if not report_id:
        return {"error": "invalid_arguments", "hint": "report_id is required"}

    # No token required for the public share_token report. The UUID report
    # endpoint may still require auth; a token-less UUID fetch surfaces the
    # API's 401/404 cleanly rather than short-circuiting here.
    headers = _auth_headers()

    # Validate-or-route: real UUIDs hit the legacy /audits/{uuid}/report.json
    # endpoint; everything else is a share_token and hits the by-token
    # endpoint. We validate the UUID format rather than catching errors
    # so the agent gets a clear 404 (not a 422 schema error).
    is_uuid = bool(_UUID_LIKE.match(report_id)) or _looks_like_uuid(report_id)
    if is_uuid:
        path = f"/api/v1/audits/{report_id}/report.json"
    else:
        path = f"/api/v1/reports/by-token/{report_id}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await _send_with_retry(client.get, f"{_api_base()}{path}", headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 404 on the UUID endpoint means "no run with that UUID";
            # 404 on the by-token endpoint means "no run with that
            # share_token". Surface the original status so the agent
            # can tell which identifier was wrong.
            return {
                "error": "not_found",
                "hint": (
                    f"no report found for {report_id!r} "
                    f"(path: {path}). Pass the share_token from the "
                    "audit creation response (the slug after /r/ in report_url), "
                    "or the run_id UUID."
                ),
                "http_status": exc.response.status_code,
            }
        return response.json()


def _looks_like_uuid(s: str) -> bool:
    """Best-effort UUID detection for values that don't pass the strict
    8-4-4-4-12 regex (e.g. ULID-style ids the API has historically emitted)."""
    try:
        uuid.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False
