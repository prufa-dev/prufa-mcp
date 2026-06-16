"""Prufa MCP server — the QA agent for your vibe-coded app.

Apache-2.0. See LICENSE in the repo root.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from prufa_mcp.audit import run_audit, get_report


server = Server("prufa-mcp")


OSS_TOOLS: list[Tool] = [
    Tool(
        name="prufa_run_audit",
        description=(
            "Run a public-page QA audit on a URL. Returns findings JSON: "
            "tracking pixels, broken flows, consent violations, console errors, "
            "compliance signals. Rate-limited; one call returns one audit. "
            "When wait=true (default), blocks until the audit completes and "
            "returns the JSON report. When wait=false, returns immediately with "
            "status='queued' and the run_id + share_token so you can poll later."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The public URL to audit. Must be authorized for the workspace.",
                },
                "wait": {
                    "type": "boolean",
                    "default": True,
                    "description": "Block until the audit completes (recommended for agents).",
                },
            },
            "required": ["url"],
        },
    ),
    Tool(
        name="prufa_get_report",
        description=(
            "Fetch a shareable report for a completed audit. Returns the JSON "
            "report payload (findings, status, url). The `report_id` argument "
            "accepts EITHER the internal run UUID (8-4-4-4-12 hex) OR the "
            "public share_token slug (the value after /r/ in report_url). "
            "The share_token is what you see in the audit creation response "
            "and is the recommended call shape."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "report_id": {
                    "type": "string",
                    "description": (
                        "Either the run UUID (8-4-4-4-12 hex) OR the share_token "
                        "slug (the value after /r/ in report_url). The slug is "
                        "preferred — it's what the audit creation response returns."
                    ),
                },
            },
            "required": ["report_id"],
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return OSS_TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name == "prufa_run_audit":
        result = await run_audit(
            url=arguments["url"],
            wait=arguments.get("wait", True),
        )
    elif name == "prufa_get_report":
        result = await get_report(report_id=arguments["report_id"])
    else:
        return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def main() -> None:
    """Sync entrypoint for the console_scripts system.

    Wraps :func:`_amain` in :func:`asyncio.run` so the `prufa-mcp` console
    script (which doesn't await coroutines) works correctly.
    """
    parser = argparse.ArgumentParser(description="Prufa MCP server (OSS)")
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the prufa-mcp version and exit",
    )
    args, _ = parser.parse_known_args()

    if args.version:
        try:
            from importlib.metadata import version

            print(version("prufa-mcp"))
        except Exception:
            from prufa_mcp import __version__

            print(__version__)
        sys.exit(0)

    asyncio.run(_amain())


async def _amain() -> None:
    parser = argparse.ArgumentParser(description="Prufa MCP server (OSS)")
    parser.add_argument(
        "--transport",
        choices=["stdio"],
        default="stdio",
        help="Transport. Only stdio is supported in the OSS build.",
    )
    args = parser.parse_args()

    if args.transport != "stdio":
        print("Only stdio transport is supported in the OSS build", file=sys.stderr)
        sys.exit(2)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    main()
