"""Billing-domain tools: upgrade plan, buy credits, open the Stripe portal.

Every tool here returns a URL a *human* opens in a browser — the agent never
takes a card, and no payment instrument ever touches this process. Prufa is a
card-first, prepaid, "no money out before money in" product; these tools hand
the human a secure Stripe link and stop there.

Each Stripe endpoint is scoped to the authenticated workspace, so every tool
first resolves the caller's own workspace id (``_current_workspace_id``) and
passes it in the body. All three are mutating POSTs (they create a Stripe
Checkout / portal session), so each forwards an ``Idempotency-Key`` and exposes
the shared ``idempotency_key`` input. All are Pro-tier ("persistent"); the API
enforces the gate and returns a 402 with a checkout hint that passes through
untouched — we do not gate client-side.
"""

from __future__ import annotations

from prufa_mcp.http import (
    ApiError,
    api_err_result,
    api_get,
    api_post,
    err_result,
    idem_key,
    ok,
)
from prufa_mcp.registry import IDEMPOTENCY_PROP, ToolDef, register


async def _current_workspace_id() -> str:
    """Resolve the authenticated workspace id.

    GET /api/v1/workspaces/current — every Stripe endpoint requires a
    ``workspace_id`` in the body that must equal the authenticated workspace.
    Lets :class:`ApiError` propagate; callers wrap it in ``api_err_result``.
    """
    resp = await api_get("/api/v1/workspaces/current")
    return resp["workspace_id"]


async def t_upgrade_plan(arguments: dict) -> dict:
    tier = arguments.get("tier")
    if not tier:
        return err_result("invalid_arguments", "tier is required")
    try:
        wsid = await _current_workspace_id()
        body: dict = {"workspace_id": wsid, "tier": tier}
        if arguments.get("success_url"):
            body["success_url"] = arguments["success_url"]
        if arguments.get("cancel_url"):
            body["cancel_url"] = arguments["cancel_url"]
        resp = await api_post(
            "/api/v1/billing/checkout", body, idempotency_key=idem_key(arguments)
        )
        return ok(
            {
                **resp,
                "action_for_human": (
                    "Open checkout_url in a browser to start the subscription "
                    "(7-day card-first trial)."
                ),
            }
        )
    except ApiError as exc:
        return api_err_result(exc)


async def t_buy_credits(arguments: dict) -> dict:
    credits = arguments.get("credits")
    if credits is None:
        return err_result("invalid_arguments", "credits is required")
    try:
        wsid = await _current_workspace_id()
        body: dict = {"workspace_id": wsid, "credits": credits}
        if arguments.get("success_url"):
            body["success_url"] = arguments["success_url"]
        if arguments.get("cancel_url"):
            body["cancel_url"] = arguments["cancel_url"]
        resp = await api_post(
            "/api/v1/billing/credits/checkout", body, idempotency_key=idem_key(arguments)
        )
        return ok(
            {
                **resp,
                "action_for_human": "Open checkout_url to buy the prepaid credit pack.",
            }
        )
    except ApiError as exc:
        return api_err_result(exc)


async def t_billing_portal(arguments: dict) -> dict:
    try:
        wsid = await _current_workspace_id()
        body: dict = {"workspace_id": wsid}
        if arguments.get("return_url"):
            body["return_url"] = arguments["return_url"]
        resp = await api_post(
            "/api/v1/billing/portal", body, idempotency_key=idem_key(arguments)
        )
        return ok(
            {
                **resp,
                "action_for_human": (
                    "Open portal_url to manage the subscription, update the card, "
                    "or view invoices."
                ),
            }
        )
    except ApiError as exc:
        return api_err_result(exc)


def _register() -> None:
    register(
        ToolDef(
            "prufa_upgrade_plan",
            "Subscribe or upgrade this workspace to a paid plan (starter | pro | "
            "team). Returns a Stripe checkout_url the HUMAN opens in a browser to "
            "start the 7-day card-first trial — the agent never enters card "
            "details. Mutating + idempotent. [Pro]",
            "persistent",
            {
                "type": "object",
                "required": ["tier"],
                "properties": {
                    "tier": {
                        "type": "string",
                        "enum": ["starter", "pro", "team"],
                        "description": "Target paid plan.",
                    },
                    "success_url": {
                        "type": "string",
                        "format": "uri",
                        "description": "Optional URL Stripe redirects to on success.",
                    },
                    "cancel_url": {
                        "type": "string",
                        "format": "uri",
                        "description": "Optional URL Stripe redirects to on cancel.",
                    },
                    **IDEMPOTENCY_PROP,
                },
            },
            t_upgrade_plan,
        )
    )
    register(
        ToolDef(
            "prufa_buy_credits",
            "Buy a one-time prepaid credit pack for this workspace (no subscription "
            "change). Returns a Stripe checkout_url the HUMAN opens to pay by card. "
            "Use when the workspace is low on credits but you don't want to change "
            "the plan. Mutating + idempotent. [Pro]",
            "persistent",
            {
                "type": "object",
                "required": ["credits"],
                "properties": {
                    "credits": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Number of prepaid credits to purchase (> 0).",
                    },
                    "success_url": {
                        "type": "string",
                        "format": "uri",
                        "description": "Optional URL Stripe redirects to on success.",
                    },
                    "cancel_url": {
                        "type": "string",
                        "format": "uri",
                        "description": "Optional URL Stripe redirects to on cancel.",
                    },
                    **IDEMPOTENCY_PROP,
                },
            },
            t_buy_credits,
        )
    )
    register(
        ToolDef(
            "prufa_billing_portal",
            "Open the Stripe customer portal for this workspace. Returns a "
            "portal_url the HUMAN opens to manage the subscription, update the "
            "card, or view invoices. A workspace with no Stripe customer yet "
            "returns 409 no_stripe_customer (pass that back to the human — they "
            "must subscribe first via prufa_upgrade_plan). Mutating + idempotent. "
            "[Pro]",
            "persistent",
            {
                "type": "object",
                "properties": {
                    "return_url": {
                        "type": "string",
                        "format": "uri",
                        "description": "Optional URL the portal returns the human to.",
                    },
                    **IDEMPOTENCY_PROP,
                },
            },
            t_billing_portal,
        )
    )


_register()
