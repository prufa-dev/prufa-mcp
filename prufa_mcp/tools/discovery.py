"""Discovery-domain tools: DNS-verified domains + full-auto discovery runs.

Full-auto discovery crawls an AUTHORIZED site, infers user flows, and drafts the
meaningful ones as reviewable draft flows. It NEVER touches a domain the
workspace has not proven it controls via a DNS TXT record, so the domain
sub-flow (register -> publish TXT -> verify) is a prerequisite to
:func:`t_run_discovery`.

This DNS-ownership domain system is SEPARATE from gremlin's mutation-opt-in
domains — a domain verified here authorizes discovery crawling only.

Every tool requires a workspace token and is Pro-tier ("persistent"). The API
enforces the tier gate and returns 402 (with a checkout hint) or 403
(domain_not_authorized) / 404 (discovery not enabled) — all pass through
unchanged via :func:`api_err_result`; we never gate client-side.
"""

from __future__ import annotations

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


async def t_register_discovery_domain(arguments: dict) -> dict:
    """Register a domain for full-auto discovery. Returns the DNS TXT record to
    publish (for an unverified domain, under ``verification``); then call
    ``prufa_verify_discovery_domain``."""
    domain = arguments.get("domain")
    if not domain:
        return err_result("invalid_arguments", "domain is required")
    body = {
        "domain": domain,
        "test_payments_opt_in": bool(arguments.get("test_payments_opt_in", False)),
    }
    try:
        result = await api_post(
            "/api/v1/discovery/domains", body, idempotency_key=idem_key(arguments)
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok(result)


async def t_verify_discovery_domain(arguments: dict) -> dict:
    """Check the published DNS TXT proof now. Idempotent — returns the domain
    body with status ``verified`` or still pending."""
    domain_id = arguments.get("domain_id")
    if not domain_id:
        return err_result("invalid_arguments", "domain_id is required")
    try:
        result = await api_post(
            f"/api/v1/discovery/domains/{domain_id}/verify",
            {},
            idempotency_key=idem_key(arguments),
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok(result)


async def t_list_discovery_domains(arguments: dict) -> dict:
    """List this workspace's discovery domains with their verification status."""
    try:
        return ok(await api_get("/api/v1/discovery/domains"))
    except ApiError as exc:
        return api_err_result(exc)


async def t_revoke_discovery_domain(arguments: dict) -> dict:
    """De-authorize a domain: future discovery on it is refused and disposable
    identities are torn down."""
    domain_id = arguments.get("domain_id")
    if not domain_id:
        return err_result("invalid_arguments", "domain_id is required")
    try:
        result = await api_request(
            "DELETE",
            f"/api/v1/discovery/domains/{domain_id}",
            idempotency_key=idem_key(arguments),
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok(result)


async def t_run_discovery(arguments: dict) -> dict:
    """Start a full-auto discovery run. Needs a paid/trial tier AND a VERIFIED
    discovery domain covering the URL's host. Poll with ``prufa_get_discovery``."""
    url = arguments.get("url")
    if not url:
        return err_result("invalid_arguments", "url is required")
    try:
        result = await api_post(
            "/api/v1/discoveries", {"url": url}, idempotency_key=idem_key(arguments)
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok(result)


async def t_get_discovery(arguments: dict) -> dict:
    """Discovery run status + the draft/advisory flows it surfaced. Confirm a
    draft flow (``prufa_confirm_flow``) to make it runnable."""
    discovery_id = arguments.get("discovery_id")
    if not discovery_id:
        return err_result("invalid_arguments", "discovery_id is required")
    try:
        return ok(await api_get(f"/api/v1/discoveries/{discovery_id}"))
    except ApiError as exc:
        return api_err_result(exc)


def _register() -> None:
    register(
        ToolDef(
            "prufa_register_discovery_domain",
            "[Pro] Register a domain for full-auto discovery and get the DNS TXT "
            "record to publish. For an unverified domain the response carries a "
            "'verification' object {method, record_name, record_type:'TXT', "
            "record_value, hint} — publish that TXT record at your DNS provider, "
            "then call prufa_verify_discovery_domain. Set test_payments_opt_in "
            "to allow discovery to exercise test-mode payment flows. This "
            "DNS-ownership authorization is separate from gremlin mutation opt-in.",
            "persistent",
            {
                "type": "object",
                "required": ["domain"],
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Apex or subdomain to authorize, e.g. 'app.example.com'.",
                    },
                    "test_payments_opt_in": {
                        "type": "boolean",
                        "default": False,
                        "description": "Allow discovery to exercise test-mode payment flows.",
                    },
                    **IDEMPOTENCY_PROP,
                },
            },
            t_register_discovery_domain,
        )
    )
    register(
        ToolDef(
            "prufa_verify_discovery_domain",
            "[Pro] Check the published DNS TXT proof for a registered discovery "
            "domain now. Idempotent — returns the domain body with status "
            "'verified' once the TXT record resolves, or still 'pending' if DNS "
            "hasn't propagated yet (retry later).",
            "persistent",
            {
                "type": "object",
                "required": ["domain_id"],
                "properties": {
                    "domain_id": {
                        "type": "string",
                        "description": "id from prufa_register_discovery_domain / "
                        "prufa_list_discovery_domains.",
                    },
                    **IDEMPOTENCY_PROP,
                },
            },
            t_verify_discovery_domain,
        )
    )
    register(
        ToolDef(
            "prufa_list_discovery_domains",
            "[Pro] List this workspace's discovery domains with id, domain, "
            "status (pending|verified), verified_at, verification_method, "
            "test_payments_opt_in, and (for pending domains) the verification "
            "TXT record to publish.",
            "persistent",
            {"type": "object", "properties": {}},
            t_list_discovery_domains,
        )
    )
    register(
        ToolDef(
            "prufa_revoke_discovery_domain",
            "[Pro] De-authorize a discovery domain. Future discovery on it is "
            "refused and any disposable identities created for it are torn down "
            "(the response reports identities_torn_down). Idempotent.",
            "persistent",
            {
                "type": "object",
                "required": ["domain_id"],
                "properties": {
                    "domain_id": {
                        "type": "string",
                        "description": "id of the domain to revoke.",
                    },
                    **IDEMPOTENCY_PROP,
                },
            },
            t_revoke_discovery_domain,
        )
    )
    register(
        ToolDef(
            "prufa_run_discovery",
            "[Pro] Start a full-auto discovery run on a URL. Crawls the site, "
            "infers user flows, and drafts the meaningful ones as reviewable "
            "draft flows. Requires a paid/trial tier AND a VERIFIED discovery "
            "domain covering the URL's host — otherwise the API returns 403 "
            "domain_not_authorized (register + verify the domain first) or 404 "
            "if discovery isn't enabled on the deployment. Returns "
            "{discovery_id, status, url, result_url}; poll with "
            "prufa_get_discovery.",
            "persistent",
            {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    **IDEMPOTENCY_PROP,
                },
            },
            t_run_discovery,
        )
    )
    register(
        ToolDef(
            "prufa_get_discovery",
            "[Pro] Get a discovery run's status and the draft/advisory flows it "
            "surfaced (name, kind, entry_url, status, flow_id, eval_score, "
            "detail, confirm_url). Confirm a draft flow (prufa_confirm_flow) to "
            "make it runnable.",
            "persistent",
            {
                "type": "object",
                "required": ["discovery_id"],
                "properties": {"discovery_id": {"type": "string"}},
            },
            t_get_discovery,
        )
    )


_register()
