"""Engineer-readable summaries shown in the NX MCP panel while a request waits."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nx_mcp.bridge import MainThreadDispatcher, ObjectRegistry
from nx_mcp.request_summary import MAX_LENGTH, humanize, relative_path, summarize
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace

NAMES = {"obj_sketch": "profile", "obj_edge": "EDGE_1"}


def describe(method, params, **kwargs):
    return summarize(method, params, name_of=NAMES.get, units="mm", **kwargs)


@pytest.mark.parametrize(
    ("method", "params", "expected"),
    [
        ("nx_extrude", {"sketch_id": "obj_sketch", "distance": 10.0}, 'Extrude "profile" 10 mm'),
        (
            "nx_extrude",
            {
                "sketch_id": "obj_sketch",
                "end_type": "through_all",
                "boolean": "subtract",
                "targets": ["b"],
            },
            'Extrude "profile" through all, subtract with 1 body',
        ),
        ("nx_blend", {"edges": ["obj_edge", "e2"], "radius": 3}, "Blend 2 edges R3 mm"),
        (
            "nx_sketch_rectangle",
            {"sketch_id": "obj_sketch", "corner1": {"x": 0, "y": 0}, "corner2": {"x": 40, "y": 20}},
            'Add 40 × 20 mm rectangle to sketch "profile"',
        ),
        (
            "nx_sketch_arc",
            {"cx": 0, "cy": 0, "radius": 5, "start_angle": 0, "end_angle": 360},
            "Add circle R5 mm to sketch the work part",
        ),
        (
            "nx_hole",
            {"diameter": 6.6, "depth": 12, "x": 0, "y": 0, "z": 0},
            "Drill hole Ø6.6 mm × 12 mm deep",
        ),
        ("nx_ui_control", {"mode": "agent"}, "Take control of NX"),
        (
            "nx_delete_feature",
            {"name": "obj_gone"},
            "Delete feature an object from before the last handover",
        ),
        ("nx_delete_feature", {"name": "EXTRUDE(2)"}, 'Delete feature "EXTRUDE(2)"'),
        ("nx_bend_table", {"anything": 1}, "Bend table"),
    ],
)
def test_summaries_use_real_arguments_and_names(method, params, expected):
    assert describe(method, params) == expected


def test_batch_names_first_step_and_counts_the_rest():
    operations = [
        {"method": "nx_blend", "params": {"edges": ["obj_edge"], "radius": 2}},
        {"method": "nx_fit_view", "params": {}},
    ]
    assert describe("nx_batch", {"operations": operations}) == (
        "Blend 1 edge R2 mm (+1 more in the same step)"
    )


def test_paths_are_shown_relative_to_the_workspace(tmp_path: Path):
    show = relative_path(tmp_path)
    target = str(tmp_path / "smoke" / "copy.prt")
    assert describe("nx_save_as", {"path": target}, path_of=show) == (
        "Save the work part as smoke/copy.prt"
    )
    assert show(r"Z:\elsewhere\a.prt") == r"Z:\elsewhere\a.prt"


def test_summaries_never_raise_and_stay_short():
    assert describe("nx_blend", {"edges": None, "radius": object()}).startswith("Blend")
    assert describe("nx_extrude", "not a dict") == "Extrude"
    assert (
        len(describe("nx_set_expression", {"expression": "x" * 300, "formula": "1"})) == MAX_LENGTH
    )
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
    status = _PendingBridgeCall("nx_status", {})
    for pending in (held, status):
        dispatcher._calls.put(pending)
    assert dispatcher.waiting() == [held, status]
    assert dispatcher.drain_where(lambda p: p.method == "nx_status") == 1
    assert ran == ["nx_status"] and dispatcher.waiting() == [held]
    error = NXToolError("NX_UI_DECLINED", "no", details={"x": 1})
    assert dispatcher.reject_where(lambda p: True, error) == 1
    assert held.error.code == "NX_UI_DECLINED" and held.error.details == {
        "x": 1,
        "mutation_outcome": "not_started",
    }
    assert ran == ["nx_status"] and dispatcher.waiting() == []


@pytest.mark.asyncio
async def test_invoke_intent_reaches_the_bridge_but_not_the_tool_arguments(tmp_path):
    bridge = AsyncMock()
    bridge.call.return_value = {"status": "success"}
    server = create_server(bridge, Workspace(tmp_path), surface="agent")
    await server.call_tool(
        "nx_invoke",
        {"tool": "nx_fit_view", "arguments": {}, "intent": "  Frame the   new boss  "},
    )
    method, params = bridge.call.call_args.args
    assert method == "nx_fit_view" and params["intent"] == "Frame the new boss"
    await server.call_tool("nx_invoke", {"tool": "nx_fit_view", "arguments": {}})
    assert "intent" not in bridge.call.call_args.args[1]  # Never leaks into a later call.
    too_long = await server.call_tool(
        "nx_invoke", {"tool": "nx_fit_view", "arguments": {}, "intent": "x" * 121}
    )
    assert too_long.isError


def test_executor_ignores_intent(rig):
    result = rig.e.execute("nx_list_bodies", {"intent": "Check bodies"})
    assert result["status"] == "success"
