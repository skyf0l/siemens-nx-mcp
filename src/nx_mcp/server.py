"""MCP server entry point — stdio transport."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from nx_mcp.bridge import DescriptorBridgeClient
from nx_mcp.certified import BridgeCaller, create_certified_server
from nx_mcp.workspace import Workspace

logger = logging.getLogger("nx_mcp")


def create_server(
    bridge: BridgeCaller | None = None,
    workspace: Workspace | None = None,
    *,
    surface: str | None = None,
    build=None,
) -> FastMCP:
    """Create the MCP server: catalogue tools, gateways and the legacy tools.

    ``surface`` (or ``NX_MCP_SURFACE``) selects the profile, ``agent`` by default.
    ``build`` overrides the NX build read from the bridge descriptor (tests).
    """
    from nx_mcp.sidecar.server import install, profile_from_env

    if workspace is None and (workspace_root := os.environ.get("NX_MCP_WORKSPACE")):
        workspace = Workspace(Path(workspace_root))
    if workspace is None:
        raise ValueError(
            "Set NX_MCP_WORKSPACE to the CAD workspace directory shared with the NX bridge"
        )
    profile = profile_from_env(surface)
    bridge = bridge or DescriptorBridgeClient()
    server = create_certified_server(bridge, workspace)
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
