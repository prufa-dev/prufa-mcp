"""flows-domain tools: compile plain-language test cases into reviewable specs.

A *flow* is a plain-language test case that Prufa compiles into a reviewable
spec (a sequence of steps + ``{{VARIABLES}}`` placeholders). The spec is a
DRAFT until confirmed — **only CONFIRMED flows run**. Any edit returns a flow
to DRAFT (it must be re-confirmed before it can run again).

Lifecycle:

  1. ``prufa_create_flow``       — compile a test case -> DRAFT spec
  2. ``prufa_confirm_flow``      — review + confirm (optionally overriding the spec)
  3. ``prufa_run_flow``          — execute a confirmed flow (per-run credentials)
  4. ``prufa_set_flow_credentials`` — store reusable, write-only credentials
  5. ``prufa_list_flows`` / ``prufa_get_flow`` — inspect
  6. ``prufa_edit_flow``         — edit the spec (returns the flow to DRAFT)
  7. ``prufa_delete_flow``       — delete (409 if a monitor still uses it)

Every mutating tool forwards an ``Idempotency-Key`` (via ``idem_key``) and
exposes the optional ``idempotency_key`` input so agent retries are replay-safe.
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


async def t_create_flow(arguments: dict) -> dict:
    url = arguments.get("url")
    if not url:
        return err_result("invalid_arguments", "url is required")
    test_case = arguments.get("test_case")
    if not test_case:
        return err_result("invalid_arguments", "test_case is required")
    body: dict = {"url": url, "test_case": test_case}
    name = arguments.get("name")
    if name:
        body["name"] = name
    try:
        result = await api_post(
            "/api/v1/flows", body, idempotency_key=idem_key(arguments)
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok(result)


async def t_confirm_flow(arguments: dict) -> dict:
    flow_id = arguments.get("flow_id")
    if not flow_id:
        return err_result("invalid_arguments", "flow_id is required")
    spec = arguments.get("spec")
    body = {"spec": spec} if spec is not None else None
    try:
        result = await api_post(
            f"/api/v1/flows/{flow_id}/confirm",
            body,
            idempotency_key=idem_key(arguments),
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok(result)


async def t_run_flow(arguments: dict) -> dict:
    flow_id = arguments.get("flow_id")
    if not flow_id:
        return err_result("invalid_arguments", "flow_id is required")
    credentials = arguments.get("credentials")
    body = {"credentials": credentials} if credentials else {}
    try:
        result = await api_post(
            f"/api/v1/flows/{flow_id}/run",
            body,
            idempotency_key=idem_key(arguments),
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok(annotate_usage_result(result))


async def t_set_flow_credentials(arguments: dict) -> dict:
    flow_id = arguments.get("flow_id")
    credentials = arguments.get("credentials")
    if not flow_id or not isinstance(credentials, dict) or not credentials:
        return err_result(
            "invalid_arguments",
            "flow_id and a non-empty credentials object are required",
        )
    try:
        result = await api_request(
            "PUT",
            f"/api/v1/flows/{flow_id}/credentials",
            body={"credentials": credentials},
            idempotency_key=idem_key(arguments),
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok(result)


async def t_list_flows(arguments: dict) -> dict:
    try:
        return ok(await api_get("/api/v1/flows"))
    except ApiError as exc:
        return api_err_result(exc)


async def t_get_flow(arguments: dict) -> dict:
    flow_id = arguments.get("flow_id")
    if not flow_id:
        return err_result("invalid_arguments", "flow_id is required")
    try:
        return ok(await api_get(f"/api/v1/flows/{flow_id}"))
    except ApiError as exc:
        return api_err_result(exc)


async def t_edit_flow(arguments: dict) -> dict:
    flow_id = arguments.get("flow_id")
    if not flow_id:
        return err_result("invalid_arguments", "flow_id is required")
    spec = arguments.get("spec")
    if not isinstance(spec, dict):
        return err_result("invalid_arguments", "spec is required")
    try:
        result = await api_request(
            "PUT",
            f"/api/v1/flows/{flow_id}",
            body={"spec": spec},
            idempotency_key=idem_key(arguments),
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok(result)


async def t_delete_flow(arguments: dict) -> dict:
    flow_id = arguments.get("flow_id")
    if not flow_id:
        return err_result("invalid_arguments", "flow_id is required")
    try:
        await api_request(
            "DELETE",
            f"/api/v1/flows/{flow_id}",
            idempotency_key=idem_key(arguments),
        )
    except ApiError as exc:
        return api_err_result(exc)
    return ok({"deleted": True, "flow_id": flow_id})


def _register() -> None:
    register(
        ToolDef(
            "prufa_create_flow",
            "Compile a plain-language test case into a reviewable DRAFT flow spec. "
            "Pass the target url and a plain-text test_case (e.g. 'log in, add the "
            "first product to the cart, and check out'); optionally a name. Returns "
            "a 201 draft: the compiled step spec plus any {{VARIABLES}} it detected "
            "(logins, coupon codes) and a review instruction. The flow does NOT run "
            "yet — review the draft, then call prufa_confirm_flow. Only confirmed "
            "flows run. Idempotent.",
            "setup",
            {
                "type": "object",
                "required": ["url", "test_case"],
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    "test_case": {
                        "type": "string",
                        "description": "Plain-language description of the test to run.",
                    },
                    "name": {"type": "string", "description": "Optional flow name."},
                    **IDEMPOTENCY_PROP,
                },
            },
            t_create_flow,
        )
    )
    register(
        ToolDef(
            "prufa_confirm_flow",
            "Confirm a DRAFT flow so it becomes runnable — only confirmed flows run. "
            "Pass flow_id. Optionally pass a corrected spec object to override the "
            "compiled draft before confirming (omit it to confirm the draft as-is). "
            "Idempotent.",
            "setup",
            {
                "type": "object",
                "required": ["flow_id"],
                "properties": {
                    "flow_id": {"type": "string"},
                    "spec": {
                        "type": "object",
                        "description": "Optional corrected spec to confirm instead "
                        "of the current draft.",
                    },
                    **IDEMPOTENCY_PROP,
                },
            },
            t_confirm_flow,
        )
    )
    register(
        ToolDef(
            "prufa_run_flow",
            "Execute a CONFIRMED flow. Returns 202 with run_id + report_url; poll "
            "the report via prufa_get_report. Pass per-run credentials as an object "
            "mapping the spec's {{VARIABLES}} to values ({'EMAIL': ..., 'PASSWORD': "
            "...}) — these are encrypted in transit and NOT stored (use "
            "prufa_set_flow_credentials to store reusable ones). Fails if the flow "
            "is still a draft — confirm it first. Idempotent.",
            "setup",
            {
                "type": "object",
                "required": ["flow_id"],
                "properties": {
                    "flow_id": {"type": "string"},
                    "credentials": {
                        "type": "object",
                        "description": "Optional {VAR: value} map for this run only; "
                        "encrypted in transit, not stored.",
                    },
                    **IDEMPOTENCY_PROP,
                },
            },
            t_run_flow,
        )
    )
    register(
        ToolDef(
            "prufa_set_flow_credentials",
            "Store reusable, WRITE-ONLY credentials for a flow so later runs need "
            "not resend them. Pass flow_id and a non-empty credentials object "
            "mapping the spec's {{VARIABLES}} to values. The response lists the "
            "stored variable NAMES only — values are never returned. Idempotent.",
            "setup",
            {
                "type": "object",
                "required": ["flow_id", "credentials"],
                "properties": {
                    "flow_id": {"type": "string"},
                    "credentials": {
                        "type": "object",
                        "description": "Non-empty {VAR: value} map. Write-only; "
                        "values are never read back.",
                    },
                    **IDEMPOTENCY_PROP,
                },
            },
            t_set_flow_credentials,
        )
    )
    register(
        ToolDef(
            "prufa_list_flows",
            "List the flows in this workspace with their status (draft|confirmed) "
            "and metadata.",
            "setup",
            {"type": "object", "properties": {}},
            t_list_flows,
        )
    )
    register(
        ToolDef(
            "prufa_get_flow",
            "Get a single flow by id: its compiled spec, status (draft|confirmed), "
            "and the NAMES of any stored credentials (never their values).",
            "setup",
            {
                "type": "object",
                "required": ["flow_id"],
                "properties": {"flow_id": {"type": "string"}},
            },
            t_get_flow,
        )
    )
    register(
        ToolDef(
            "prufa_edit_flow",
            "Replace a flow's spec. Pass flow_id and the full spec object. NOTE: any "
            "edit returns the flow to DRAFT — you must call prufa_confirm_flow again "
            "before it can run. Idempotent.",
            "setup",
            {
                "type": "object",
                "required": ["flow_id", "spec"],
                "properties": {
                    "flow_id": {"type": "string"},
                    "spec": {"type": "object", "description": "The full replacement spec."},
                    **IDEMPOTENCY_PROP,
                },
            },
            t_edit_flow,
        )
    )
    register(
        ToolDef(
            "prufa_delete_flow",
            "Delete a flow by id. NOTE: a flow still used by a monitor returns 409 "
            "flow_in_use — pause or delete that monitor first, then retry. "
            "Idempotent.",
            "setup",
            {
                "type": "object",
                "required": ["flow_id"],
                "properties": {
                    "flow_id": {"type": "string"},
                    **IDEMPOTENCY_PROP,
                },
            },
            t_delete_flow,
        )
    )


_register()
