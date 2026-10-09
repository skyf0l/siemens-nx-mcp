"""Workspace path rules of the drawing tools in the sidecar (gate 2, S)."""

from pathlib import Path
from unittest.mock import AsyncMock

from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace


async def test_template_and_export_paths_are_resolved_or_refused_before_nx(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    bridge = AsyncMock()
    bridge.call.return_value = {"ok": True, "tool": "nx_create_drawing_sheet", "data": {}}
    server = create_server(bridge, Workspace(tmp_path), surface="full")
    outside = await server.call_tool("nx_create_drawing_sheet", {"template_path": "../a3.prt"})
    assert outside.structuredContent["error"]["code"] == "NX_PATH_OUTSIDE_WORKSPACE"
    assert outside.structuredContent["mutation_outcome"] == "not_started"
    bridge.call.assert_not_called()
    await server.call_tool("nx_create_drawing_sheet", {"template_path": "templates/a3.prt"})
    sent = bridge.call.call_args.args[1]
    assert Path(sent["template_path"]) == tmp_path / "templates" / "a3.prt"
    await server.call_tool("nx_create_drawing_sheet", {"template": "A3"})
    assert bridge.call.call_args.args[1]["template"] == "A3"  # an installed name is no path
    reserved = await server.call_tool("nx_export_drawing", {"path": ".nx-mcp/x.pdf"})
    assert reserved.structuredContent["error"]["code"] == "NX_PATH_RESERVED"
