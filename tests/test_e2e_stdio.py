"""End-to-end: launch the real server as a stdio subprocess and drive it with
the official MCP client. Proves the protocol wiring (initialize, tools/list,
tools/call), the full tool surface, and the auth guard — no live API needed."""
from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _params(env_extra: dict | None = None) -> StdioServerParameters:
    env = {k: v for k, v in os.environ.items() if k not in ("PRUFA_API_TOKEN",)}
    # Point at an unreachable base so any accidental network call fails fast
    # rather than hitting the real API during the test.
    env["PRUFA_API_BASE"] = "http://127.0.0.1:9"
    env["PRUFA_CONFIG"] = "/nonexistent/prufa/mcp.json"
    if env_extra:
        env.update(env_extra)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "prufa_mcp.server"],
        env=env,
    )


async def _drive() -> dict:
    out: dict = {}
    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            out["server_version"] = init.serverInfo.version
            listed = await session.list_tools()
            out["tool_names"] = sorted(t.name for t in listed.tools)

            # health_check needs no token and no network.
            hc = await session.call_tool("prufa_health_check", {})
            out["health_ok"] = (hc.structuredContent or {}).get("service") == "prufa-mcp"
            out["health_is_error"] = bool(hc.isError)

            # A persistent tool with no token must hit the local auth guard
            # (missing_token) — never a network call.
            guarded = await session.call_tool("prufa_list_monitors", {})
            out["guard_is_error"] = bool(guarded.isError)
            out["guard_code"] = (guarded.structuredContent or {}).get("code")
    return out


def test_stdio_server_end_to_end() -> None:
    import prufa_mcp

    out = asyncio.run(asyncio.wait_for(_drive(), timeout=30))
    # serverInfo.version reports OUR package version, not the mcp SDK's.
    assert out["server_version"] == prufa_mcp.__version__, out["server_version"]
    # Full surface is served.
    assert len(out["tool_names"]) == 44, out["tool_names"]
    for expected in (
        "prufa_run_audit",
        "prufa_start_monitor",
        "prufa_run_gremlin",
        "prufa_run_discovery",
        "prufa_upgrade_plan",
        "prufa_setup_workspace",
    ):
        assert expected in out["tool_names"], f"{expected} missing from tools/list"
    # health_check round-trips with structured content.
    assert out["health_ok"] is True
    assert out["health_is_error"] is False
    # auth guard fires locally for a persistent tool with no token.
    assert out["guard_is_error"] is True
    assert out["guard_code"] == "missing_token", out
