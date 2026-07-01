"""gremlin-domain tools: chaos QA — an agent imitating a difficult user.

The gremlin drives a site with no script, playing a persona (confused newbie,
impatient, hostile, fat-finger, back-button masher). Plain-code detectors verify
what actually breaks; mutations are DRY-RUN unless the host is explicitly
authorized, and payments are NEVER executed.

Shape mirrors ``prufa_mcp.tools.audit``: each tool is an ``async def t_<name>``
handler that validates args, calls the hosted API via ``prufa_mcp.http``, and
returns ``ok(...)`` / ``err_result(...)`` / ``api_err_result(...)``. The two
POST-and-poll tools (run + rerun) share :func:`_run_and_maybe_poll`.
"""

from __future__ import annotations

import asyncio

from prufa_mcp.conversion import annotate_usage_result
from prufa_mcp.http import (
    ApiError,
    api_err_result,
    api_get,
    api_post,
    api_request,
    err_result,
    idem_key,
    ok,
)
from prufa_mcp.registry import IDEMPOTENCY_PROP, ToolDef, register

# Gremlin runs are long — chaos exploration can take up to ~5 min. Poll every 5s
# for up to 60 iterations. Tests monkeypatch _POLL_INTERVAL_S to 0.0 to skip the
# real waiting.
_POLL_INTERVAL_S = 5.0
_POLL_MAX_ITERS = 60

_TERMINAL = {"succeeded", "failed", "blocked", "timeout"}

_PERSONAS = [
    "confused_newbie",
    "impatient",
    "hostile",
    "fat_finger",
    "back_button_masher",
]

_CREDENTIALS_SCHEMA = {
    "type": "object",
    "required": ["email", "password"],
    "properties": {
        "email": {"type": "string"},
        "password": {"type": "string"},
    },
    "additionalProperties": False,
}


async def _run_and_maybe_poll(create_path: str, body: dict, arguments: dict) -> dict:
    """POST to ``create_path`` (idempotent), then optionally poll to terminal.

    Shared by ``prufa_run_gremlin`` and ``prufa_rerun_gremlin``. On create the API
    returns 202 ``{run_id, status, persona, mode, step_cap, report_url, usage?}``.
    If ``wait`` is False (or no run_id came back), annotate usage and return the
    queued state. Otherwise poll ``GET /api/v1/audits/{run_id}`` until terminal,
    then return the fetched ``report.json``.
    """
    try:
        result = await api_post(create_path, body, idempotency_key=idem_key(arguments))
    except ApiError as exc:
        return api_err_result(exc)

    run_id = result.get("run_id")
    wait = bool(arguments.get("wait", True))
    if not wait or not run_id:
        annotate_usage_result(result)
        return ok(result)

    try:
        for _ in range(_POLL_MAX_ITERS):
            await asyncio.sleep(_POLL_INTERVAL_S)
            run = await api_get(f"/api/v1/audits/{run_id}")
            if run.get("status") in _TERMINAL:
                report = await api_get(f"/api/v1/audits/{run_id}/report.json")
                return ok(report)
    except ApiError as exc:
        return api_err_result(exc)

    return err_result(
        "gremlin_timeout",
        "gremlin run did not complete in time",
        run_id=run_id,
    )


async def t_run_gremlin(arguments: dict) -> dict:
    url = arguments.get("url")
    if not url:
        return err_result("invalid_arguments", "url is required")

    body: dict = {"url": url}
    persona = arguments.get("persona")
    if persona:
        body["persona"] = persona
    direction = arguments.get("direction")
    if direction:
        body["direction"] = direction
    credentials = arguments.get("credentials")
    if credentials:
        body["credentials"] = credentials

    return await _run_and_maybe_poll("/api/v1/gremlin", body, arguments)


async def t_authorize_domain(arguments: dict) -> dict:
    host = arguments.get("host")
    if not host:
        return err_result("invalid_arguments", "host is required")

    body: dict = {"host": host, "allow_mutation": bool(arguments.get("allow_mutation", True))}
    note = arguments.get("note")
    if note is not None:
        body["note"] = note

    try:
        # Idempotent upsert — no Idempotency-Key header needed.
        result = await api_request("PUT", "/api/v1/gremlin/domains", body=body)
    except ApiError as exc:
        return api_err_result(exc)
    return ok(result)


async def t_list_gremlin_domains(arguments: dict) -> dict:
    try:
        return ok(await api_get("/api/v1/gremlin/domains"))
    except ApiError as exc:
        return api_err_result(exc)


async def t_rerun_gremlin(arguments: dict) -> dict:
    run_id = arguments.get("run_id")
    if not run_id:
        return err_result("invalid_arguments", "run_id is required")
    return await _run_and_maybe_poll(f"/api/v1/gremlin/{run_id}/rerun", {}, arguments)


async def t_gremlin_saved_logins(arguments: dict) -> dict:
    try:
        return ok(await api_get("/api/v1/gremlin/saved-logins"))
    except ApiError as exc:
        return api_err_result(exc)


async def t_promote_gremlin_path(arguments: dict) -> dict:
    share_token = arguments.get("share_token")
    if not share_token:
        return err_result("invalid_arguments", "share_token is required")
    path_index = arguments.get("path_index")
    if path_index is None:
        return err_result("invalid_arguments", "path_index is required")

    try:
        result = await api_post(
            f"/api/v1/gremlin/reports/{share_token}/flows",
            {"path_index": path_index},
            idempotency_key=idem_key(arguments),
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok(result)


def _register() -> None:
    register(
        ToolDef(
            "prufa_run_gremlin",
            "Run a gremlin chaos-QA session on a URL: an agent imitates a "
            "difficult user (no script) while plain-code detectors verify what "
            "breaks. Use to stress-test a real flow beyond the deterministic "
            "audit. Mutations are DRY-RUN unless the host is authorized via "
            "prufa_authorize_domain; payments are NEVER executed. Optional "
            "credentials (a real, non-payment login write) require a signed-in "
            "workspace AND mutation authorization for the host. Step budget "
            "depends on plan (free teaser 8, Starter 20, Pro 40, Team 60) — call "
            "prufa_get_usage for the cap. wait=true (default) blocks until the "
            "run completes (can take ~5 min) and returns the report; wait=false "
            "returns the queued state with run_id. [Pro]",
            "persistent",
            {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    "persona": {
                        "type": "string",
                        "enum": _PERSONAS,
                        "description": "Which difficult-user persona to play.",
                    },
                    "direction": {
                        "type": "string",
                        "description": "Freeform nudge for what the gremlin should try.",
                    },
                    "credentials": _CREDENTIALS_SCHEMA,
                    "wait": {"type": "boolean", "default": True},
                    **IDEMPOTENCY_PROP,
                },
            },
            t_run_gremlin,
            requires_auth=False,
        )
    )
    register(
        ToolDef(
            "prufa_authorize_domain",
            "Authorize REAL (non-payment) mutations for the gremlin on a host you "
            "own or a staging host — logging in and writing on that host stop "
            "being dry-run. Set allow_mutation=false to revoke. This is the "
            "gremlin mutation opt-in, NOT the discovery DNS-domain verification "
            "(a different system). host is a bare hostname (e.g. staging.acme.com). [Pro]",
            "persistent",
            {
                "type": "object",
                "required": ["host"],
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Bare hostname (no scheme/path), e.g. staging.acme.com.",
                    },
                    "allow_mutation": {
                        "type": "boolean",
                        "default": True,
                        "description": "true authorizes real mutations; false revokes.",
                    },
                    "note": {"type": "string", "description": "Optional operator note."},
                },
            },
            t_authorize_domain,
        )
    )
    register(
        ToolDef(
            "prufa_list_gremlin_domains",
            "List the hosts this workspace has authorized for real gremlin "
            "mutations, with each host's allow_mutation flag, note, and "
            "created_at, plus the default policy. [Pro]",
            "persistent",
            {"type": "object", "properties": {}},
            t_list_gremlin_domains,
        )
    )
    register(
        ToolDef(
            "prufa_rerun_gremlin",
            "Re-dispatch a past gremlin run with the SAME intent (original url, "
            "persona, direction, and saved login) under the current tier's step "
            "cap. Returns a NEW run_id. wait=true (default) blocks until the "
            "rerun completes and returns the report; wait=false returns the "
            "queued state. [Pro]",
            "persistent",
            {
                "type": "object",
                "required": ["run_id"],
                "properties": {
                    "run_id": {"type": "string", "description": "The original gremlin run_id."},
                    "wait": {"type": "boolean", "default": True},
                    **IDEMPOTENCY_PROP,
                },
            },
            t_rerun_gremlin,
        )
    )
    register(
        ToolDef(
            "prufa_gremlin_saved_logins",
            "SENSITIVE — returns DECRYPTED email+password for logins previously "
            "used by gremlin runs in THIS workspace, so a kickoff can reuse them. "
            "Only for the owning workspace. Never print these values into logs, "
            "transcripts, or files; pass them straight into prufa_run_gremlin's "
            "credentials. [Pro]",
            "persistent",
            {"type": "object", "properties": {}},
            t_gremlin_saved_logins,
        )
    )
    register(
        ToolDef(
            "prufa_promote_gremlin_path",
            "Import one reproduced gremlin path (by its path_index in the "
            "gremlin report) as a DRAFT flow. The flow still needs review + "
            "confirmation before it will run — this only creates the draft. "
            "Returns flow_id, status='draft', and a review_url. [Pro]",
            "persistent",
            {
                "type": "object",
                "required": ["share_token", "path_index"],
                "properties": {
                    "share_token": {
                        "type": "string",
                        "description": "The gremlin report's public share token.",
                    },
                    "path_index": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Zero-based index of the reproduced path in the report.",
                    },
                    **IDEMPOTENCY_PROP,
                },
            },
            t_promote_gremlin_path,
        )
    )


_register()
