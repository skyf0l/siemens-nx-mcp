"""MCP server entry point — stdio transport."""

from __future__ import annotations

import asyncio
import logging
import sys

from mcp.server.fastmcp import FastMCP

from nx_mcp.bridge import DescriptorBridgeClient
from nx_mcp.workspace import Workspace, default_workspace

logger = logging.getLogger("nx_mcp")


def create_server(
    bridge=None,
    workspace: Workspace | None = None,
    *,
    surface: str | None = None,
    build=None,
) -> FastMCP:
    """Create the MCP server: the gateways and the catalogue tools.

    ``surface`` (or ``NX_MCP_SURFACE``) selects the profile, ``agent`` by default.
    ``build`` overrides the NX build read from the bridge descriptor (tests).
    """
    from nx_mcp.sidecar.server import install, profile_from_env

    if workspace is None:
        # The same default as the NX hosts: NX_MCP_WORKSPACE, else <home>/NX_MCP_WORKSPACE.
        workspace = Workspace(default_workspace())
    profile = profile_from_env(surface)
    bridge = bridge or DescriptorBridgeClient()
    server = FastMCP("nx-mcp")
    install(server, bridge, workspace, profile, build=build)
    return server


async def async_main() -> None:
    """Run the MCP server with stdio transport."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    server = create_server()
    logger.info("NX MCP Server starting (stdio transport)")
    await server.run_stdio_async()


def main() -> None:
    """Entry point for the nx-mcp console script."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
