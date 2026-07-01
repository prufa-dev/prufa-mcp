"""Tool registry — the single source of truth for the MCP tool surface.

Each domain module (``prufa_mcp.tools.*``) calls :func:`register` at import
time. ``prufa_mcp.server`` imports ``prufa_mcp.tools`` (which imports every
domain module), then reads :data:`REGISTRY` to answer ``tools/list`` and route
``tools/call``. Adding a tool is a ``register(...)`` call in a domain module —
no central edit.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

# Shared JSON-schema fragment: every mutating tool accepts an optional
# idempotency_key so agent retries are replay-safe.
IDEMPOTENCY_PROP: dict = {
    "idempotency_key": {
        "type": "string",
        "description": (
            "Optional. Replays of the same key within 24h return the original "
            "response without re-executing — pass one to make retries safe. "
            "Omitted: a fresh key is generated, so each call executes."
        ),
    },
}


@dataclass
class ToolDef:
    name: str
    description: str
    tier: str  # "setup" | "persistent"
    input_schema: dict
    handler: Callable[[dict], Awaitable[dict]]
    # Whether the tool needs a workspace token. Tools that bootstrap a token
    # (setup_workspace) or need none (health_check) set this False; the
    # dispatcher short-circuits everything else with a clear missing_token
    # error when no token is configured.
    requires_auth: bool = True


@dataclass
class _Registry:
    tools: dict[str, ToolDef] = field(default_factory=dict)

    def register(self, tool: ToolDef) -> None:
        if tool.name in self.tools:
            raise ValueError(f"duplicate tool registration: {tool.name}")
        self.tools[tool.name] = tool

    def get(self, name: str) -> ToolDef | None:
        return self.tools.get(name)

    def list_mcp(self) -> list[dict]:
        """The ``tools/list`` payload — one page, everything."""
        out = []
        for tool in self.tools.values():
            out.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                    "annotations": {
                        "audience": ["assistant"],
                        "priority": 0.5,
                        "tier": tool.tier,
                    },
                }
            )
        return out


REGISTRY = _Registry()


def register(tool: ToolDef) -> None:
    REGISTRY.register(tool)
