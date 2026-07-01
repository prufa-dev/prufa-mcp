"""monitors-domain tools: the persistent "watch this" primitive.

A monitor re-runs an audit on a URL on a cadence (daily|hourly). Given a
``flow_id`` it re-runs that confirmed flow instead of the plain audit. Every
delta fires the deploy hook (a webhook) so CI / the human hears about a
regression. All tools are Pro-tier (``persistent``) and require auth — the
hosted API enforces the gate and returns 402 with a checkout hint that passes
through automatically; we never gate client-side.

Shape mirrors ``prufa_mcp.tools.audit`` exactly: an ``async def t_<name>``
handler per tool, each wrapping the API call in ``try/except ApiError`` and
returning ``ok(...)`` / ``err_result(...)`` / ``api_err_result(...)``; mutating
tools forward ``idempotency_key=idem_key(arguments)`` and expose
``IDEMPOTENCY_PROP`` in their input schema.
"""

from __future__ import annotations

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


async def t_start_monitor(arguments: dict) -> dict:
    url = arguments.get("url")
    if not url:
        return err_result("invalid_arguments", "url is required")
    body: dict = {"url": url, "cadence": arguments.get("cadence", "daily")}
    flow_id = arguments.get("flow_id")
    if flow_id:
        body["flow_id"] = flow_id
    try:
        result = await api_post(
            "/api/v1/monitors", body, idempotency_key=idem_key(arguments)
        )
    except ApiError as exc:
        return api_err_result(exc)
    deploy_hook = result.get("deploy_hook")
    if isinstance(deploy_hook, dict) and deploy_hook.get("secret"):
        deploy_hook["note"] = (
            "store this secret NOW — it is shown only once; recover via "
            "prufa_rotate_monitor_webhook"
        )
    annotate_usage_result(result)
    return ok(result)


async def t_get_monitor(arguments: dict) -> dict:
    monitor_id = arguments.get("monitor_id")
    if not monitor_id:
        return err_result("invalid_arguments", "monitor_id is required")
    try:
        result = await api_get(f"/api/v1/monitors/{monitor_id}")
    except ApiError as exc:
        return api_err_result(exc)
    annotate_usage_result(result)
    return ok(result)


async def t_list_monitors(arguments: dict) -> dict:
    try:
        result = await api_get("/api/v1/monitors")
    except ApiError as exc:
        return api_err_result(exc)
    annotate_usage_result(result)
    return ok(result)


async def _patch_status(monitor_id: str, status: str, arguments: dict) -> dict:
    """Shared PATCH for pause/resume — both flip Monitor.status."""
    return await api_request(
        "PATCH",
        f"/api/v1/monitors/{monitor_id}",
        body={"status": status},
        idempotency_key=idem_key(arguments),
    )


async def t_pause_monitor(arguments: dict) -> dict:
    monitor_id = arguments.get("monitor_id")
    if not monitor_id:
        return err_result("invalid_arguments", "monitor_id is required")
    try:
        return ok(await _patch_status(monitor_id, "paused", arguments))
    except ApiError as exc:
        return api_err_result(exc)


async def t_resume_monitor(arguments: dict) -> dict:
    monitor_id = arguments.get("monitor_id")
    if not monitor_id:
        return err_result("invalid_arguments", "monitor_id is required")
    try:
        return ok(await _patch_status(monitor_id, "active", arguments))
    except ApiError as exc:
        return api_err_result(exc)


async def t_trigger_monitor(arguments: dict) -> dict:
    monitor_id = arguments.get("monitor_id")
    if not monitor_id:
        return err_result("invalid_arguments", "monitor_id is required")
    try:
        result = await api_post(
            f"/api/v1/monitors/{monitor_id}/run",
            {},
            idempotency_key=idem_key(arguments),
        )
    except ApiError as exc:
        return api_err_result(exc)
    annotate_usage_result(result)
    return ok(result)


async def t_delete_monitor(arguments: dict) -> dict:
    monitor_id = arguments.get("monitor_id")
    if not monitor_id:
        return err_result("invalid_arguments", "monitor_id is required")
    try:
        await api_request(
            "DELETE",
            f"/api/v1/monitors/{monitor_id}",
            idempotency_key=idem_key(arguments),
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok({"deleted": True, "monitor_id": monitor_id})


async def t_rotate_monitor_webhook(arguments: dict) -> dict:
    monitor_id = arguments.get("monitor_id")
    if not monitor_id:
        return err_result("invalid_arguments", "monitor_id is required")
    try:
        result = await api_post(
            f"/api/v1/monitors/{monitor_id}/webhook/rotate",
            {},
            idempotency_key=idem_key(arguments),
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok(result)


async def t_list_monitor_deliveries(arguments: dict) -> dict:
    monitor_id = arguments.get("monitor_id")
    if not monitor_id:
        return err_result("invalid_arguments", "monitor_id is required")
    try:
        result = await api_get(f"/api/v1/monitors/{monitor_id}/deliveries")
    except ApiError as exc:
        return api_err_result(exc)
    return ok(result)


_MONITOR_ID_SCHEMA: dict = {
    "type": "object",
    "required": ["monitor_id"],
    "properties": {"monitor_id": {"type": "string"}},
}


def _mutating_monitor_id_schema() -> dict:
    return {
        "type": "object",
        "required": ["monitor_id"],
        "properties": {"monitor_id": {"type": "string"}, **IDEMPOTENCY_PROP},
    }


def _register() -> None:
    register(
        ToolDef(
            "prufa_start_monitor",
            "Start a persistent monitor: re-run a QA audit on a URL on a cadence "
            "(daily|hourly) and fire a deploy hook on every regression delta. Pass "
            "flow_id to re-run a confirmed flow instead of the plain audit. 1-click "
            "setup — the response returns the deploy_hook (url + signing secret, "
            "shown ONLY once) which you should store immediately. [Pro] Free-tier "
            "workspaces get a 402 with a checkout hint that passes through; do not "
            "gate client-side. Idempotent.",
            "persistent",
            {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    "cadence": {
                        "type": "string",
                        "enum": ["daily", "hourly"],
                        "default": "daily",
                    },
                    "flow_id": {
                        "type": "string",
                        "description": "Optional confirmed flow to re-run instead of "
                        "the plain page audit.",
                    },
                    **IDEMPOTENCY_PROP,
                },
            },
            t_start_monitor,
        )
    )
    register(
        ToolDef(
            "prufa_get_monitor",
            "Get one monitor's config + latest state (status, cadence, last run, "
            "deploy-hook metadata). [Pro]",
            "persistent",
            _MONITOR_ID_SCHEMA,
            t_get_monitor,
        )
    )
    register(
        ToolDef(
            "prufa_list_monitors",
            "List every monitor in this workspace. [Pro]",
            "persistent",
            {"type": "object", "properties": {}},
            t_list_monitors,
        )
    )
    register(
        ToolDef(
            "prufa_pause_monitor",
            "Pause a monitor — it stops running on its cadence until resumed. "
            "History is kept. Idempotent. [Pro]",
            "persistent",
            _mutating_monitor_id_schema(),
            t_pause_monitor,
        )
    )
    register(
        ToolDef(
            "prufa_resume_monitor",
            "Resume a paused monitor. The next scheduled run fires immediately, "
            "then it returns to its cadence. Idempotent. [Pro]",
            "persistent",
            _mutating_monitor_id_schema(),
            t_resume_monitor,
        )
    )
    register(
        ToolDef(
            "prufa_trigger_monitor",
            "Trigger a monitor run right now (out of band). Rate-capped to 1 per "
            "60s; if a run is already queued or running the response is deduped "
            "(deduped:true) instead of starting a second one. Idempotent. [Pro]",
            "persistent",
            _mutating_monitor_id_schema(),
            t_trigger_monitor,
        )
    )
    register(
        ToolDef(
            "prufa_delete_monitor",
            "Delete a monitor (stops all future runs and revokes its deploy hook). "
            "Past run history is retained. Idempotent. [Pro]",
            "persistent",
            _mutating_monitor_id_schema(),
            t_delete_monitor,
        )
    )
    register(
        ToolDef(
            "prufa_rotate_monitor_webhook",
            "Rotate a monitor's deploy-hook signing secret. The new secret is "
            "returned ONCE in the response — store it now; the old secret dies "
            "immediately, so update your CI before the next delivery. Idempotent. "
            "[Pro]",
            "persistent",
            _mutating_monitor_id_schema(),
            t_rotate_monitor_webhook,
        )
    )
    register(
        ToolDef(
            "prufa_list_monitor_deliveries",
            "List a monitor's last 50 deploy-hook deliveries plus copy-paste CI "
            "snippets (curl, github_actions, gitlab_ci). never_fired:true powers "
            "the 'your webhook never fired' nudge — surface it to the human. [Pro]",
            "persistent",
            _MONITOR_ID_SCHEMA,
            t_list_monitor_deliveries,
        )
    )


_register()
