"""Workspace-domain tools: bootstrap/read a workspace, usage + trial state, and
settings (billing/overage/auto-recharge + notification routing).

Same shape as the reference module ``prufa_mcp.tools.audit``: each tool is an
``async def t_<name>(arguments) -> dict`` handler that validates args, calls the
hosted API via ``prufa_mcp.http``, and returns ``ok(...)`` / ``err_result(...)``
/ ``api_err_result(...)``; a module-level ``_register()`` registers every tool.

These tools carry the trial -> paid conversion signal (``prufa_mcp.conversion``)
so the agent can see when an ``agent_temp`` trial is running low and script its
human toward upgrading or buying credits.
"""

from __future__ import annotations

from prufa_mcp import audit as _audit
from prufa_mcp.conversion import annotate_usage_result, trial_block, upsell
from prufa_mcp.http import (
    ApiError,
    api_err_result,
    api_get,
    api_request,
    err_result,
    idem_key,
    ok,
)
from prufa_mcp.registry import IDEMPOTENCY_PROP, ToolDef, register

# Settings accepted by prufa_workspace_settings, forwarded verbatim when present.
_SETTINGS_KEYS = (
    "display_name",
    "usage_webhook_url",
    "usage_webhook_secret",
    "opt_in_to_overage",
    "auto_recharge",
    "auto_recharge_credits",
    "auto_recharge_monthly_limit_cents",
    "email_alerts_enabled",
    "slack_alerts_enabled",
)


def _attach_trial(body: dict) -> dict:
    """Attach the trial block + upsell to a full ``/workspaces/current`` body."""
    body["trial"] = trial_block(
        body.get("usage"),
        is_in_trial=body.get("is_in_trial", False),
        trial_expires_at=body.get("trial_expires_at"),
    )
    annotate_usage_result(body)
    return body


async def t_setup_workspace(arguments: dict) -> dict:
    """Read the current workspace when a token is set, otherwise bootstrap a
    free no-card ``agent_temp`` trial workspace."""
    if _audit._api_token():
        try:
            body = await api_get("/api/v1/workspaces/current")
        except ApiError as exc:
            return api_err_result(exc)
        return ok(_attach_trial(body))

    owner_email = arguments.get("owner_email")
    if not owner_email:
        return err_result(
            "invalid_arguments",
            "owner_email is required to create a workspace — ask your human for "
            "their email",
        )
    payload = {
        "tier": "agent_temp",
        "owner_email": owner_email,
        "display_name": arguments.get("name") or "agent workspace",
    }
    try:
        response = await api_request(
            "POST",
            "/api/v1/workspaces",
            body=payload,
            idempotency_key=idem_key(arguments),
        )
    except ApiError as exc:
        return api_err_result(exc)
    result = dict(response)
    result["persist_instruction"] = (
        "Save api_token now as the PRUFA_API_TOKEN env var (or in "
        "~/.config/prufa/mcp.json) — it is shown ONCE and cannot be retrieved "
        "later. Then re-run tools with it set."
    )
    result["trial"] = trial_block(
        response.get("usage"), is_in_trial=True, trial_expires_at=None
    )
    return ok(result)


async def t_get_workspace(arguments: dict) -> dict:
    try:
        body = await api_get("/api/v1/workspaces/current")
    except ApiError as exc:
        return api_err_result(exc)
    return ok(_attach_trial(body))


async def t_get_usage(arguments: dict) -> dict:
    try:
        body = await api_get("/api/v1/workspaces/current")
    except ApiError as exc:
        return api_err_result(exc)
    out = dict(body.get("usage") or {})
    out["trial"] = trial_block(
        body.get("usage"),
        is_in_trial=body.get("is_in_trial", False),
        trial_expires_at=body.get("trial_expires_at"),
    )
    up = upsell(body.get("usage"), trial_expires_at=body.get("trial_expires_at"))
    if up is not None:
        out["upsell"] = up
    return ok(out)


async def t_workspace_settings(arguments: dict) -> dict:
    body = {k: arguments[k] for k in _SETTINGS_KEYS if k in arguments}
    if not body:
        return err_result("invalid_arguments", "provide at least one setting to change")
    try:
        result = await api_request(
            "PATCH",
            "/api/v1/workspaces/current",
            body=body,
            idempotency_key=idem_key(arguments),
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok(result)


async def t_set_notifications(arguments: dict) -> dict:
    cells = arguments.get("cells")
    if cells is None:
        return err_result("invalid_arguments", "cells is required")
    try:
        result = await api_request(
            "PATCH",
            "/api/v1/workspaces/current/notifications",
            body={"cells": cells},
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok(result)


def _register() -> None:
    register(
        ToolDef(
            "prufa_setup_workspace",
            "Bootstrap or read the Prufa workspace this agent uses. If a token is "
            "already configured it returns the current workspace (with trial + "
            "upsell state). If NO token is set it CREATES a free 'agent_temp' "
            "workspace: no card, no charge, a 7-day trial with an included credit "
            "budget. During the trial you can run audits, monitors, discovery, and "
            "the full gremlin; after it ends monitors/discovery/gremlin_full_run are "
            "gated. The 201 response returns an api_token shown ONCE — persist it as "
            "PRUFA_API_TOKEN (or in ~/.config/prufa/mcp.json) and re-run with it set. "
            "When credits run low or the trial nears its end, tell your human to "
            "upgrade (prufa_upgrade_plan) or buy credits (prufa_buy_credits). "
            "Idempotent.",
            "setup",
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Optional display name for the new workspace "
                        "(defaults to 'agent workspace'). Ignored when a token is set.",
                    },
                    "owner_email": {
                        "type": "string",
                        "description": "Human owner's email — REQUIRED to create a "
                        "workspace when no token is configured. Ignored when a token "
                        "is set.",
                    },
                    **IDEMPOTENCY_PROP,
                },
            },
            t_setup_workspace,
            requires_auth=False,
        )
    )
    register(
        ToolDef(
            "prufa_get_workspace",
            "Get the current workspace: tier, usage/credits, and trial state "
            "(days remaining, what gets gated when the trial ends). Includes an "
            "upsell block when the workspace is low on credits or near trial end.",
            "setup",
            {"type": "object", "properties": {}},
            t_get_workspace,
        )
    )
    register(
        ToolDef(
            "prufa_get_usage",
            "Get this workspace's credit usage (available/included) plus trial "
            "state and, when relevant, an upsell block. Use before a costly run to "
            "check the balance and relay the message_for_human if credits are low.",
            "setup",
            {"type": "object", "properties": {}},
            t_get_usage,
        )
    )
    register(
        ToolDef(
            "prufa_workspace_settings",
            "Update workspace settings — pass only the keys you want to change. "
            "Covers display_name, usage webhook (url/secret), overage opt-in, "
            "auto_recharge (auto-buys credits when a launch is blocked on an empty "
            "balance — needs a card on file), auto_recharge_credits / "
            "auto_recharge_monthly_limit_cents, and email/slack alert toggles. "
            "At least one setting is required. Idempotent. [Pro]",
            "persistent",
            {
                "type": "object",
                "properties": {
                    "display_name": {"type": "string"},
                    "usage_webhook_url": {"type": ["string", "null"]},
                    "usage_webhook_secret": {"type": ["string", "null"]},
                    "opt_in_to_overage": {"type": "boolean"},
                    "auto_recharge": {
                        "type": "boolean",
                        "description": "Auto-buy credits when a launch is blocked on "
                        "an empty balance. Requires a saved card.",
                    },
                    "auto_recharge_credits": {"type": "integer"},
                    "auto_recharge_monthly_limit_cents": {"type": "integer"},
                    "email_alerts_enabled": {"type": "boolean"},
                    "slack_alerts_enabled": {"type": "boolean"},
                    **IDEMPOTENCY_PROP,
                },
            },
            t_workspace_settings,
        )
    )
    register(
        ToolDef(
            "prufa_set_notifications",
            "Set the notification routing matrix: 'cells' is the 9-event x "
            "{email, slack} map of booleans deciding which channel each event fires "
            "on. The server validates keys and types (422 on unknown/missing keys or "
            "non-booleans) and returns {cells, locked_events} (events you can't turn "
            "off). [Pro]",
            "persistent",
            {
                "type": "object",
                "required": ["cells"],
                "properties": {
                    "cells": {
                        "type": "object",
                        "description": "9-event x {email, slack} routing matrix of "
                        "booleans.",
                    }
                },
            },
            t_set_notifications,
        )
    )


_register()
