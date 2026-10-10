"""Sidecar tool surface: path arguments are workspace-scoped and checked before NX."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace


@pytest.mark.asyncio
async def test_authoring_tool_schemas_are_explicit_and_path_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    bridge = NS(call=AsyncMock(return_value={"ok": True, "data": {}}))
    server = create_server(bridge, Workspace(tmp_path), surface="full")
    tools = {t.name: t for t in await server.list_tools()}
    assert tools["nx_find_geometry"].annotations.readOnlyHint
    await server.call_tool("nx_export_inspection_report", {"path": "reports/review.zip"})
    args = bridge.call.call_args.args[1]
    assert args["path"] == str(tmp_path / "reports" / "review.zip") and args["operation_id"]


@pytest.mark.asyncio
async def test_display_tools_are_catalogue_tools_and_the_replaced_ones_are_gone(tmp_path):
    server = create_server(NS(), Workspace(tmp_path), surface="full")
    tools = {t.name: t for t in await server.list_tools()}
    assert tools["nx_set_visibility"].inputSchema["properties"]["mode"]["enum"] == [
        "show",
        "hide",
        "isolate",
    ]
    replaced = {
        "nx_screenshot", "nx_render_view", "nx_view_info", "nx_set_view", "nx_fit_view",
        "nx_set_datum_visibility", "nx_set_display", "nx_display_info", "nx_highlight_objects",
        "nx_clear_highlights", "nx_restore_presentation", "nx_section_view",
        "nx_section_control", "nx_list_sections",
    }  # fmt: skip
    assert not replaced & set(tools)
    assert "inline" in tools["nx_capture_view"].description
    assert not tools["nx_set_appearance"].annotations.readOnlyHint


@pytest.mark.asyncio
async def test_sidecar_path_errors_are_not_started(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    bridge = AsyncMock()
    server = create_server(bridge, Workspace(tmp_path), surface="full")
    result = await server.call_tool("nx_export_inspection_report", {"path": "../outside.zip"})
    assert result.structuredContent["error"]["code"] == "NX_PATH_OUTSIDE_WORKSPACE"
    assert result.structuredContent["mutation_outcome"] == "not_started"
    bridge.call.assert_not_called()
    bridge.call.side_effect = NXError("NX_BRIDGE_UNAVAILABLE", "lost")
    result = await server.call_tool("nx_export_inspection_report", {"path": "r.zip"})
    assert result.structuredContent["mutation_outcome"] == "unknown"
