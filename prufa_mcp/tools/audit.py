"""Audit-domain tools: health, one-shot audits, runs, reports, findings, alerts.

This is the reference domain module — every other ``prufa_mcp.tools.*`` module
follows the same shape:

  * an ``async def t_<name>(arguments: dict) -> dict`` handler that validates
    args, calls the hosted API via ``prufa_mcp.http``, and returns ``ok(...)``
    or ``err_result(...)`` / ``api_err_result(...)``;
  * a ``register(ToolDef(...))`` call per tool at import time.

The one-shot audit + report fetch reuse the battle-tested pollers in
``prufa_mcp.audit`` (they already block on ``wait=true``, poll to terminal,
retry on 429, and route run_id vs share_token) and just wrap the result.
"""

from __future__ import annotations

from prufa_mcp import audit as _audit
from prufa_mcp.http import ApiError, api_err_result, api_get, err_result, ok
from prufa_mcp.registry import ToolDef, register


async def t_health_check(arguments: dict) -> dict:
    return ok(
        {
            "status": "ok",
            "service": "prufa-mcp",
            "api": _audit._api_base(),
            "protocol_version": "2025-06-18",
            "authenticated": bool(_audit._api_token()),
        }
    )


async def t_run_audit(arguments: dict) -> dict:
    """One-shot audit. wait=true (default) blocks until the audit completes and
    returns the JSON report; wait=false returns immediately with the queued
    state + run_id + share_token."""
    url = arguments.get("url")
    if not url:
        return err_result("invalid_arguments", "url is required")
    data = await _audit.run_audit(url=url, wait=bool(arguments.get("wait", True)))
    # audit.run_audit already returns a clean data dict (report or queued state,
    # or a structured {error, hint} on missing token / not found).
    if isinstance(data, dict) and data.get("error"):
        return err_result(data["error"], data.get("hint", ""), **{
            k: v for k, v in data.items() if k not in ("error", "hint")
        })
    return ok(data)


async def t_get_run(arguments: dict) -> dict:
    run_id = arguments.get("run_id")
    if not run_id:
        return err_result("invalid_arguments", "run_id is required")
    try:
        return ok(await api_get(f"/api/v1/audits/{run_id}"))
    except ApiError as exc:
        return api_err_result(exc)


async def t_get_report(arguments: dict) -> dict:
    """Fetch a report by EITHER run_id (UUID) OR share_token (slug from
    /r/<token>). Reuses audit.get_report, which routes on the identifier shape."""
    run_id = arguments.get("run_id")
    share_token = arguments.get("share_token")
    report_id = share_token or run_id
    if not report_id:
        return err_result(
            "invalid_arguments",
            "run_id or share_token is required — run_id is the UUID from audit "
            "creation; share_token is the slug from /r/<token> in report_url",
        )
    data = await _audit.get_report(report_id=report_id)
    if isinstance(data, dict) and data.get("error"):
        return err_result(data["error"], data.get("hint", ""), **{
            k: v for k, v in data.items() if k not in ("error", "hint")
        })
    return ok(data)


async def t_list_runs(arguments: dict) -> dict:
    limit = min(int(arguments.get("limit", 10)), 100)
    try:
        return ok(await api_get(f"/api/v1/audits?limit={limit}"))
    except ApiError as exc:
        return api_err_result(exc)


async def t_get_finding(arguments: dict) -> dict:
    """Persisted findings for a run (flat, machine-readable). Pass finding_key
    to filter to one finding."""
    run_id = arguments.get("run_id")
    if not run_id:
        return err_result("invalid_arguments", "run_id is required")
    try:
        result = await api_get(f"/api/v1/audits/{run_id}/findings")
    except ApiError as exc:
        return api_err_result(exc)
    finding_key = arguments.get("finding_key")
    if finding_key and isinstance(result, dict):
        result["findings"] = [
            f for f in result.get("findings", []) if f.get("finding_key") == finding_key
        ]
    return ok(result)


async def t_list_alerts(arguments: dict) -> dict:
    try:
        return ok(await api_get("/api/v1/alerts"))
    except ApiError as exc:
        return api_err_result(exc)


def _register() -> None:
    register(
        ToolDef(
            "prufa_health_check",
            "Probe the Prufa API and MCP server. Always free, no token required.",
            "setup",
            {"type": "object", "properties": {}},
            t_health_check,
            requires_auth=False,
        )
    )
    register(
        ToolDef(
            "prufa_run_audit",
            "Run a one-shot public-page QA audit on a URL. Returns findings JSON "
            "(broken flows, JS console errors, tracking/consent, security headers, "
            "a11y, mobile) graded A-F. Idempotent. wait=true (default) blocks until "
            "the audit completes and returns the report; wait=false returns the "
            "queued state with run_id + share_token to poll via prufa_get_report.",
            "setup",
            {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    "wait": {"type": "boolean", "default": True},
                },
            },
            t_run_audit,
            requires_auth=False,
        )
    )
    register(
        ToolDef(
            "prufa_get_run",
            "Get a run's status by id (queued|running|succeeded|failed|blocked|timeout).",
            "setup",
            {
                "type": "object",
                "required": ["run_id"],
                "properties": {"run_id": {"type": "string"}},
            },
            t_get_run,
        )
    )
    register(
        ToolDef(
            "prufa_get_report",
            "Get the JSON report for a run. Accepts EITHER run_id (UUID from audit "
            "creation) OR share_token (the slug after /r/ in report_url). The "
            "share_token form is the recommended call shape — it is what the audit "
            "creation response returns.",
            "setup",
            {
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "Internal UUID. Optional if share_token is given.",
                    },
                    "share_token": {
                        "type": "string",
                        "description": "Public report slug (after /r/ in report_url). "
                        "Optional if run_id is given; the recommended call shape.",
                    },
                },
            },
            t_get_report,
            requires_auth=False,
        )
    )
    register(
        ToolDef(
            "prufa_list_runs",
            "List recent runs in this workspace (auth required).",
            "setup",
            {"type": "object", "properties": {"limit": {"type": "integer", "default": 10}}},
            t_list_runs,
        )
    )
    register(
        ToolDef(
            "prufa_get_finding",
            "Get the persisted findings for a run (flat, machine-readable). Pass "
            "finding_key to filter to a single finding.",
            "persistent",
            {
                "type": "object",
                "required": ["run_id"],
                "properties": {
                    "run_id": {"type": "string"},
                    "finding_key": {
                        "type": "string",
                        "description": "Optional stable finding key to filter by",
                    },
                },
            },
            t_get_finding,
        )
    )
    register(
        ToolDef(
            "prufa_list_alerts",
            "Alert history (delta-engine ledger), newest first — includes suppressed "
            "alerts with their suppression reason. [Pro]",
            "persistent",
            {"type": "object", "properties": {}},
            t_list_alerts,
        )
    )


_register()
