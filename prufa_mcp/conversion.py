"""Trial -> paid conversion, made legible to the agent.

An ``agent_temp`` workspace is a no-card, 7-day trial with an included credit
budget. Prufa is interested in converting that trial's *human* to a paid plan,
so the agent must understand the trial's limits and be scripted to pull its
human toward subscribing before / when limits hit.

Two blocks do that:

- :func:`trial_block` — the honest state: tier, in-trial, days remaining,
  credits remaining/included, and what gets gated once the trial ends. Attached
  where we have full workspace info (``setup_workspace``, ``get_workspace``,
  ``get_usage``).
- :func:`upsell` — appears on any metered result when credits run low
  (< 20% of included, or a non-positive balance) OR the trial has <= 2 days
  left. It carries ``message_for_human`` (copy the agent relays verbatim) and
  the exact tool names to call next.

Every value is derived from what the API already returns (the ``usage`` object
from ``compute_usage`` and ``trial_expires_at`` from ``GET /workspaces/current``).
Nothing is hardcoded; a field we can't derive is omitted, never faked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Capabilities that require a paid plan (or the trial) — surfaced so the agent
# can tell its human what it loses when the trial ends. Sourced from the tier
# model, not invented per-call.
GATED_AFTER_TRIAL = ["monitors", "discovery", "gremlin_full_run"]

# Upsell fires when the balance drops below this fraction of the included
# allotment, or when the trial has this many days (or fewer) left.
_LOW_CREDIT_FRACTION = 0.20
_TRIAL_WARN_DAYS = 2


def _days_remaining(trial_expires_at: str | None) -> int | None:
    """Whole days until the trial ends, or None if not on a trial / unparseable."""
    if not trial_expires_at:
        return None
    try:
        exp = datetime.fromisoformat(trial_expires_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    delta = exp - datetime.now(UTC)
    return max(0, delta.days)


def trial_block(usage: dict | None, *, is_in_trial: bool, trial_expires_at: str | None) -> dict:
    """The agent-visible trial state. Safe to attach whenever we have the
    workspace's trial fields (from GET /workspaces/current or the
    setup_workspace response)."""
    usage = usage or {}
    days = _days_remaining(trial_expires_at)
    block: dict[str, Any] = {
        "tier": usage.get("tier"),
        "is_in_trial": is_in_trial,
        "trial_expires_at": trial_expires_at,
        "credits_remaining": usage.get("available_credits"),
        "credits_included": usage.get("calls_included"),
        "gated_after_trial": GATED_AFTER_TRIAL,
    }
    if days is not None:
        block["days_remaining"] = days
    return block


def _low_on_credits(usage: dict) -> bool:
    remaining = usage.get("available_credits")
    included = usage.get("calls_included")
    if remaining is None:
        return False
    if remaining <= 0:
        return True
    if isinstance(included, int) and included > 0:
        return remaining < _LOW_CREDIT_FRACTION * included
    return False


def upsell(usage: dict | None, *, trial_expires_at: str | None = None) -> dict | None:
    """Return an upsell block when the workspace is low on credits or near the
    end of its trial, else None. Attached to metered tool results.

    ``usage`` is the ``compute_usage`` object (credit fields, no trial expiry);
    pass ``trial_expires_at`` when the caller also has the workspace's trial
    field so the days-remaining trigger applies.
    """
    if not usage:
        return None
    # Paid workspaces don't get upsold.
    tier = usage.get("tier")
    if tier and tier not in ("free", "agent_temp"):
        return None

    days = _days_remaining(trial_expires_at)
    low_credits = _low_on_credits(usage)
    trial_ending = days is not None and days <= _TRIAL_WARN_DAYS
    if not (low_credits and True) and not trial_ending:
        # low_credits alone or trial_ending alone triggers; neither -> no upsell.
        if not low_credits:
            return None

    remaining = usage.get("available_credits")
    reasons = []
    if low_credits:
        reasons.append(f"low on credits ({remaining} left)")
    if trial_ending:
        reasons.append(f"trial ends in {days} day(s)")
    reason = " and ".join(reasons) or "approaching plan limits"

    message = (
        f"Heads up from Prufa: this workspace is {reason}. To keep audits, "
        "monitors, gremlin, and discovery running, ask me to upgrade the plan "
        "(prufa_upgrade_plan) or buy more credits (prufa_buy_credits) — I'll "
        "open a secure checkout link you complete with a card."
    )
    return {
        "reason": reason,
        "message_for_human": message,
        "upgrade_tool": "prufa_upgrade_plan",
        "buy_credits_tool": "prufa_buy_credits",
    }


def annotate_usage_result(data: dict) -> dict:
    """Attach an ``upsell`` block to a result dict that carries a ``usage``
    object (monitor/flow/gremlin responses). No-op when usage is absent or the
    workspace isn't a conversion target. Returns the same dict for chaining."""
    if not isinstance(data, dict):
        return data
    usage = data.get("usage")
    if isinstance(usage, dict):
        up = upsell(usage)
        if up is not None:
            data["upsell"] = up
    return data
