from types import SimpleNamespace

import pytest

from nx_mcp.runtime import NXToolError
from nx_mcp.visual_tools import unit_normal


def test_section_normal_rejects_degenerate_or_nonfinite_planes():
    for value in ([0, 0, 0], [float("nan"), 0, 1], [0, 1]):
        with pytest.raises(NXToolError):
            unit_normal(value)
    assert unit_normal([0, 0, 5]) == [0, 0, 1]


@pytest.mark.asyncio
async def test_display_tools_are_catalogue_tools_and_legacy_ones_are_gone(tmp_path):
    from nx_mcp.server import create_server
    from nx_mcp.workspace import Workspace

    server = create_server(SimpleNamespace(), Workspace(tmp_path), surface="full")
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
