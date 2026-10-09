"""The explicitly certified v0.2 MCP tool surface."""

from __future__ import annotations

from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP

from nx_mcp.workspace import Workspace


class BridgeCaller(Protocol):
    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


# The inventories that were certified here are catalogue tools now (nx_list_objects).
CERTIFIED_TOOL_NAMES: set[str] = set()


def create_certified_server(
    bridge: BridgeCaller,
    workspace: Workspace,
) -> FastMCP:
    mcp = FastMCP(
        "nx-mcp",
        instructions="Siemens NX integration. Runtime support varies by NX version; no general certification is claimed.",
    )

    # Part and file tools are catalogue tools (domains/parts_files); nx_extrude's
    # single public schema lives in authoring_server.py (configure() rejects duplicates).
    from nx_mcp.integration_server import configure

    configure(mcp, bridge, workspace)

    return mcp
