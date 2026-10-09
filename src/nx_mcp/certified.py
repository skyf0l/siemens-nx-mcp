"""The explicitly certified v0.2 MCP tool surface."""

from __future__ import annotations

import json
from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError as MCPToolError

from nx_mcp.contracts import (
    NXToolError,
    ObjectListResult,
    OperationResult,
    StatusResult,
)
from nx_mcp.workspace import Workspace


class BridgeCaller(Protocol):
    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


CERTIFIED_TOOL_NAMES = {
    "nx_status",
    "nx_list_sketches",
    "nx_list_bodies",
    "nx_list_features",
    "nx_undo",
    "nx_fit_view",
}


def create_certified_server(
    bridge: BridgeCaller,
    workspace: Workspace,
) -> FastMCP:
    mcp = FastMCP(
        "nx-mcp",
        instructions="Siemens NX integration. Runtime support varies by NX version; no general certification is claimed.",
    )

    async def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await bridge.call(method, params)
        except NXToolError as error:
            raise MCPToolError(json.dumps(error.as_dict(), ensure_ascii=False)) from error

    @mcp.tool()
    async def nx_status() -> StatusResult:
        """Report bridge, NX version, and active-part status."""
        return StatusResult(**await call("nx_status", {}))

    @mcp.tool()
    async def nx_list_sketches() -> ObjectListResult:
        """List sketches in the active work part."""
        return ObjectListResult(**await call("nx_list_sketches", {}))

    @mcp.tool()
    async def nx_list_bodies() -> ObjectListResult:
        """List bodies in the active work part."""
        return ObjectListResult(**await call("nx_list_bodies", {}))

    @mcp.tool()
    async def nx_list_features() -> ObjectListResult:
        """List features in the active work part."""
        return ObjectListResult(**await call("nx_list_features", {}))

    @mcp.tool()
    async def nx_undo() -> OperationResult:
        """Undo the last visible NX MCP operation."""
        return OperationResult(**await call("nx_undo", {}))

    @mcp.tool()
    async def nx_fit_view() -> OperationResult:
        """Fit the active modeling view."""
        return OperationResult(**await call("nx_fit_view", {}))

    # Part and file tools are catalogue tools (domains/parts_files); nx_extrude's
    # single public schema lives in authoring_server.py (configure() rejects duplicates).
    from nx_mcp.integration_server import configure

    configure(mcp, bridge, workspace)

    return mcp
