"""Engineer-readable summaries shown in the NX MCP panel while a request waits."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nx_mcp.bridge import MainThreadDispatcher, ObjectRegistry
from nx_mcp.request_summary import MAX_LENGTH, humanize, relative_path, summarize
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace

NAMES = {
    "obj_sketch": "profile",
    "obj_edge": "EDGE_1",
    "obj_arc": "ARC_2",
    "obj_face": "FACE_7",
    "obj_bolt": "BOLT M8",
    "obj_front": "FRONT@1",
    "obj_flat": "FLAT@2",
    "obj_sheet": "SHT1",
    "obj_flange": "SB_FLANGE(4)",
    "obj_extrude": "EXTRUDE(2)",
}


def describe(method, params, **kwargs):
    return summarize(method, params, name_of=NAMES.get, units="mm", **kwargs)


@pytest.mark.parametrize(
    ("method", "params", "expected"),
    [
        (
            "nx_rename_object",
            {"object_id": "obj_gone", "name": "A"},
            'Rename an object from before the last handover to "A"',
        ),
        (
            "nx_rename_object",
            {"object_id": "obj_extrude", "name": "A"},
            'Rename "EXTRUDE(2)" to "A"',
        ),
    ],
)
def test_summaries_use_real_arguments_and_names(method, params, expected):
    assert describe(method, params) == expected


def test_paths_are_shown_relative_to_the_workspace(tmp_path: Path):
    show = relative_path(tmp_path)
    target = str(tmp_path / "smoke" / "view.json")
    assert show(target) == "smoke/view.json"
    assert show(r"Z:\elsewhere\a.prt") == r"Z:\elsewhere\a.prt"


def test_summaries_never_raise_and_stay_short():
    assert describe("nx_rename_object", {"object_id": object()}).startswith("Rename")
    assert describe("nx_rename_object", "not a dict") == "Rename object"
    assert len(describe("nx_rename_object", {"object_id": "x" * 300})) == MAX_LENGTH
    assert humanize("nx_measure_distance") == "Measure distance"


def test_registry_display_name_never_touches_nx():
    registry = ObjectRegistry()
    ref = registry.register(object(), kind="sketch", name="profile", part_id="p")
    unnamed = registry.register(object(), kind="edge", name="", part_id="p")
    assert registry.display_name(ref.id) == "profile"
    assert registry.display_name(unnamed.id) == "edge"
    assert registry.display_name("obj_unknown") is None


def test_dispatcher_runs_selected_call_and_rejects_held_ones():
    ran = []
    dispatcher = MainThreadDispatcher(lambda method, params: ran.append(method) or {"ok": 1})
    from nx_mcp.bridge import _PendingBridgeCall

    held = _PendingBridgeCall("nx_extrude", {})
    status = _PendingBridgeCall("nx_get_status", {})
    for pending in (held, status):
        dispatcher._calls.put(pending)
    assert dispatcher.waiting() == [held, status]
    assert dispatcher.drain_where(lambda p: p.method == "nx_get_status") == 1
    assert ran == ["nx_get_status"] and dispatcher.waiting() == [held]
    error = NXToolError("NX_UI_DECLINED", "no", details={"x": 1})
    assert dispatcher.reject_where(lambda p: True, error) == 1
    assert held.error.code == "NX_UI_DECLINED" and held.error.details == {
        "x": 1,
        "mutation_outcome": "not_started",
    }
    assert ran == ["nx_get_status"] and dispatcher.waiting() == []


@pytest.mark.asyncio
async def test_invoke_intent_reaches_the_bridge_but_not_the_tool_arguments(tmp_path):
    bridge = AsyncMock()
    bridge.call.return_value = {"status": "success"}
    server = create_server(bridge, Workspace(tmp_path), surface="agent")
    await server.call_tool(
        "nx_invoke",
        {"tool": "nx_checkpoint", "arguments": {}, "intent": "  Frame the   new boss  "},
    )
    method, params = bridge.call.call_args.args
    assert method == "nx_checkpoint" and params["intent"] == "Frame the new boss"
    await server.call_tool("nx_invoke", {"tool": "nx_checkpoint", "arguments": {}})
    assert "intent" not in bridge.call.call_args.args[1]  # Never leaks into a later call.
    too_long = await server.call_tool(
        "nx_invoke", {"tool": "nx_checkpoint", "arguments": {}, "intent": "x" * 121}
    )
    assert too_long.isError


def test_executor_ignores_intent(rig):
    result = rig.e.execute("nx_list_bodies", {"intent": "Check bodies"})
    assert result["status"] == "success"
