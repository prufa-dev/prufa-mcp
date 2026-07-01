"""Prufa MCP server — the QA agent for your vibe-coded app.

The complete agent surface: point it at Prufa and an agent (or a human in
Claude Code / any MCP client) can run audits, drive flows, watch monitors, run
gremlin chaos tests, run full-auto discovery, and manage the workspace + billing
— every capability the hosted product exposes over its versioned HTTP API.

This module is thin: it wires the official ``mcp`` SDK's stdio ``Server`` to the
tool :data:`~prufa_mcp.registry.REGISTRY`. Importing ``prufa_mcp.tools``
registers every domain's tools. Tool logic lives in ``prufa_mcp.tools.*`` over
the shared client in ``prufa_mcp.http``.

Apache-2.0. See LICENSE in the repo root.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from prufa_mcp import audit as _audit
from prufa_mcp import tools as _tools  # noqa: F401 — import registers every tool
from prufa_mcp.http import err_result
from prufa_mcp.registry import REGISTRY

server = Server("prufa-mcp")


def _missing_token_result() -> dict:
    return err_result(
        "missing_token",
        "This tool needs a Prufa workspace token. Set PRUFA_API_TOKEN (or add it "
        "to ~/.config/prufa/mcp.json), or call prufa_setup_workspace to create a "
        "free workspace and get one.",
        docs="https://prufa.dev/docs/mcp",
    )


def _to_call_result(data: dict) -> CallToolResult:
    """Convert a tool handler's dict ({content, structuredContent?, isError?})
    into the SDK's CallToolResult."""
    content = [
        TextContent(type=block.get("type", "text"), text=block.get("text", ""))
        for block in data.get("content", [])
    ]
    if not content:
        content = [TextContent(type="text", text="")]
    return CallToolResult(
        content=content,
        structuredContent=data.get("structuredContent"),
        isError=bool(data.get("isError", False)),
    )


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t["inputSchema"],
            annotations=None,
        )
        for t in REGISTRY.list_mcp()
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    tool = REGISTRY.get(name)
    if tool is None:
        return _to_call_result(err_result("unknown_tool", f"unknown tool: {name}"))
    if tool.requires_auth and not _audit._api_token():
        return _to_call_result(_missing_token_result())
    try:
        data = await tool.handler(arguments or {})
    except Exception as exc:  # noqa: BLE001 — never crash the loop on a tool bug
        data = err_result("tool_failed", f"{type(exc).__name__}: {exc}")
    return _to_call_result(data)


def main() -> None:
    """Sync entrypoint for the ``prufa-mcp`` console script."""
    parser = argparse.ArgumentParser(description="Prufa MCP server")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument(
        "--transport",
        choices=["stdio"],
        default="stdio",
        help="Transport. Only stdio is supported.",
    )
    args, _ = parser.parse_known_args()

    if args.version:
        try:
            from importlib.metadata import version

            print(version("prufa-mcp"))
        except Exception:  # noqa: BLE001
            from prufa_mcp import __version__

            print(__version__)
        sys.exit(0)

    asyncio.run(_amain())


async def _amain() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    main()
