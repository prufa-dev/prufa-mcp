"""Shared HTTP client + MCP result helpers for every Prufa MCP tool.

This is the single foundation the domain tool modules (``prufa_mcp.tools.*``)
build on. It talks to the hosted Prufa API (``PRUFA_API_BASE``, default
``https://app.prufa.dev``) with the workspace bearer token
(``PRUFA_API_TOKEN`` or the ``~/.config/prufa/mcp.json`` file — resolved by
``prufa_mcp.audit``), and it shapes results the way the MCP layer expects.

Design invariants (ported from the monorepo server, kept identical so tool
handlers read the same on both sides):

- Every mutating call forwards an ``Idempotency-Key`` header. Pass an explicit
  ``idempotency_key`` argument to make retries replay-safe (24h window); omit it
  and a fresh key is generated per call.
- The API's structured ``{"detail": {code, hint, docs}}`` error bodies are parsed
  (never stringified blindly) and surface as ``isError`` tool results carrying the
  same ``code``/``hint``/``docs`` plus ``http_status`` — a 402's checkout-bearing
  hint passes through intact.
- The hosted API is rate-limited; a 429 is retried with exponential backoff
  (1s/2s/4s) before the response is returned to the caller.

Test seam: set ``prufa_mcp.http._TRANSPORT`` to an ``httpx.MockTransport`` to
record the exact requests tools make without a live API.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx

# Token / base-URL resolution lives in audit.py (env var wins over the JSON
# config file, base defaults to the hosted API). Reuse it so there is exactly
# one place that reads credentials.
from prufa_mcp.audit import _api_base as api_base
from prufa_mcp.audit import _api_token as api_token

__all__ = [
    "ApiError",
    "api_base",
    "api_token",
    "ok",
    "err_result",
    "api_err_result",
    "idem_key",
    "api_request",
    "api_post",
    "api_get",
    "share_token_from_report_url",
]


# --- Rate-limit retry ---------------------------------------------------------

# One initial attempt, then retries sleeping these backoffs on HTTP 429.
# Overridable in tests (set to (0.0, 0.0, 0.0) to skip real sleeping).
_RETRY_BACKOFFS_S: tuple[float, ...] = (1.0, 2.0, 4.0)

# Test seam: inject an httpx transport (e.g. MockTransport) so tests can record
# the exact requests the tools make without a live API.
_TRANSPORT: httpx.AsyncBaseTransport | None = None


def _client() -> httpx.AsyncClient:
    kwargs: dict[str, Any] = {"timeout": 60}
    if _TRANSPORT is not None:
        kwargs["transport"] = _TRANSPORT
    return httpx.AsyncClient(**kwargs)


def _headers(idempotency_key: str | None = None) -> dict[str, str]:
    h = {"Accept": "application/json"}
    token = api_token()
    if token:
        h["Authorization"] = f"Bearer {token}"
    if idempotency_key:
        h["Idempotency-Key"] = idempotency_key
    return h


# --- Structured API errors ----------------------------------------------------


class ApiError(RuntimeError):
    """The API's structured error: ``{"detail": {code, hint, docs?}}``.

    Parsed (never stringified blindly) so tool results can surface
    code + hint + docs to the agent — a 402's checkout-bearing hint
    passes through intact.
    """

    def __init__(self, status_code: int, detail: Any) -> None:
        self.status_code = status_code
        if isinstance(detail, dict):
            self.code = str(detail.get("code") or f"http_{status_code}")
            self.hint = str(detail.get("hint") or detail.get("message") or detail)
            self.docs = detail.get("docs")
            # Preserve billing extras (402 carries checkout_url / amount / tier).
            self.extra = {
                k: v
                for k, v in detail.items()
                if k not in {"code", "hint", "message", "docs"}
            }
        else:
            self.code = f"http_{status_code}"
            self.hint = str(detail)
            self.docs = None
            self.extra = {}
        super().__init__(f"HTTP {status_code}: {self.code}: {self.hint}")


# --- MCP result helpers (same dict shape as the monorepo server) --------------


def ok(content: str | list | dict, structured: dict | None = None) -> dict:
    """MCP tool result: text content + optional structuredContent.

    ``content`` may be a string (single text block), a list (passed through as
    content items), or a dict (rendered as JSON text AND promoted to
    structuredContent).
    """
    if isinstance(content, str):
        text_payload: list = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        text_payload = content
    else:
        text_payload = [{"type": "text", "text": json.dumps(content, indent=2)}]
    result: dict = {"content": text_payload}
    if structured is not None:
        result["structuredContent"] = structured
    elif isinstance(content, dict):
        result["structuredContent"] = content
    return result


def err_result(code: str, message: str, **details: Any) -> dict:
    """MCP tool error: ``isError=true`` with a structured body the agent can act on."""
    body = {"code": code, "message": message}
    body.update(details)
    return {
        "isError": True,
        "content": [{"type": "text", "text": json.dumps(body, indent=2)}],
        "structuredContent": body,
    }


def api_err_result(exc: ApiError) -> dict:
    """``ApiError`` -> MCP ``isError`` result, same ``{code, hint, docs}`` keys as
    the HTTP surface plus ``http_status`` (and any billing extras like
    ``checkout_url`` on a 402)."""
    details: dict[str, Any] = {"hint": exc.hint, "http_status": exc.status_code}
    if exc.docs:
        details["docs"] = exc.docs
    details.update(exc.extra)
    return err_result(exc.code, exc.hint, **details)


def idem_key(arguments: dict) -> str:
    """Every mutating call forwards an ``Idempotency-Key``. An explicit
    ``idempotency_key`` argument makes retries replay-safe (24h window);
    otherwise a fresh key is generated so each call executes."""
    return arguments.get("idempotency_key") or f"mcp-{uuid.uuid4()}"


def share_token_from_report_url(report_url: str | None) -> str | None:
    """Extract the ``share_token`` slug from a ``/r/<token>`` URL."""
    if not report_url or "/r/" not in report_url:
        return None
    tail = report_url.split("/r/", 1)[-1]
    return tail.split("/")[0].split("?")[0] or None


# --- HTTP request core --------------------------------------------------------


async def api_request(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Issue one API request, retrying on HTTP 429 with exponential backoff.
    Raises :class:`ApiError` on any >=400 response (after retries)."""
    headers = _headers(idempotency_key)
    url = f"{api_base()}{path}"
    async with _client() as client:
        r = await client.request(method, url, json=body, headers=headers)
        for backoff in _RETRY_BACKOFFS_S:
            if r.status_code != 429:
                break
            await asyncio.sleep(backoff)
            r = await client.request(method, url, json=body, headers=headers)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:  # noqa: BLE001
            detail = r.text
        raise ApiError(r.status_code, detail)
    return r.json() if r.content else {}


async def api_post(path: str, body: dict, idempotency_key: str | None = None) -> dict:
    return await api_request("POST", path, body=body, idempotency_key=idempotency_key)


async def api_get(path: str) -> dict:
    return await api_request("GET", path)
