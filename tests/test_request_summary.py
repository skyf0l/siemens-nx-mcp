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
            "Add circle R5 mm to the active sketch",
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
        (
            "nx_sketch_line",
            {"sketch_id": "obj_sketch", "start": {"x": 0, "y": 0}, "end": {"x": 30, "y": 40}},
            'Add 50 mm line to sketch "profile"',
        ),
        ("nx_boolean", {"boolean_type": "unite", "targets": ["a", "b"]}, "Unite 2 bodies"),
        ("nx_check_clearance", {"objects": list("abcd")}, "Check clearance between 4 objects"),
        ("nx_measure_volume", {"scope": "assembly"}, "Measure volume of the whole assembly"),
        ("nx_ui_control", {"mode": "status"}, "Check who controls NX"),
        (
            "nx_component_action",
            {"component": "obj_bolt", "action": "replace", "part_path": "parts/m8.prt"},
            'Replace component "BOLT M8" with parts/m8.prt',
        ),
        (
            "nx_native_component_pattern",
            {"component": "obj_bolt", "direction": [0, -1, 0], "spacing": 25, "count": 4},
            'Pattern "BOLT M8" 4 × 25 mm along -Y',
        ),
        (
            "nx_add_section_drawing_view",
            {"parent_view": "obj_front", "cut_object": "obj_edge", "scale": 0.5},
            'Add a section view of "FRONT@1" through "EDGE_1" at 1:2',
        ),
        (
            "nx_create_parts_list",
            {"drawing": "obj_sheet", "position": [10, 10], "scope": "top_level"},
            'Add a parts list (top level) to drawing "SHT1"',
        ),
        (
            "nx_edit_faces",
            {"faces": ["obj_face"], "action": "move", "distance": -2, "direction": [0, 0, 1]},
            'Move "FACE_7" -2 mm along +Z',
        ),
        (
            "nx_section_view",
            {"origin": [0, 0, 15], "normal": [1, 0, 0]},
            'Add section "MCP section" at (0, 0, 15) normal +X',
        ),
        (
            "nx_bend_table",
            {"view": "obj_flat", "position": [200, 40], "columns": ["BendID", "BendAngle"]},
            'Add bend table for view "FLAT@2" (2 columns)',
        ),
        (
            "nx_sketch_tangent",
            {"sketch_id": "obj_sketch", "curve1": "obj_edge", "curve2": "obj_arc"},
            'Make "ARC_2" tangent to "EDGE_1"',
        ),
        (
            "nx_copy_project",
            {"path": "smoke/rev_b", "prefix": "b_"},
            'Copy the assembly to smoke/rev_b with prefix "b_"',
        ),
        (
            "nx_set_display",
            {"objects": ["obj_bolt"], "color": "red", "transparency": 50},
            'Make "BOLT M8" red, 50% transparent',
        ),
    ],
)
def test_summaries_use_real_arguments_and_names(method, params, expected):
    assert describe(method, params) == expected


def test_batch_counts_steps_and_names_the_first():
    line = {"sketch_id": "obj_sketch", "start": {"x": 0, "y": 0}, "end": {"x": 40, "y": 0}}
    operations = [{"method": "nx_sketch_line", "params": line}] * 4 + [
        {"method": "nx_add_component", "params": {"part_path": "parts/bolt.prt"}}
    ]
    assert describe("nx_batch", {"operations": operations}) == (
        'Batch of 5 steps: Add 40 mm line to sketch "profile" (+4 more)'
    )
    one = describe("nx_batch", {"operations": operations[-1:]})
    assert one == "Batch of 1 step: Add component parts/bolt.prt"
    assert describe("nx_batch", {"operations": []}) == "Run an empty batch"


def test_batch_keeps_its_count_when_the_first_step_is_long():
    long_name = {"expression": "x" * 200, "formula": "1"}
    operations = [{"method": "nx_set_expression", "params": long_name}] * 12
    line = describe("nx_preview_change", {"operations": operations})
    assert len(line) <= MAX_LENGTH
    assert line.startswith("Preview 12 changes: Set xxx") and line.endswith("… (+11 more)")


def test_sketch_edits_and_sheet_metal_name_their_first_change_and_values():
    edits = [
        {"action": "add_line", "start": [0, 0], "end": [0, 25]},
        {"action": "constraint", "curve": "obj_edge", "type": "horizontal"},
        {"action": "delete", "object": "obj_arc"},
    ]
    assert describe("nx_edit_sketch", {"sketch_id": "obj_sketch", "operations": edits}) == (
        'Edit sketch "profile": add 25 mm line (+2 more)'
    )
    flange = {"operation": "advanced_flange", "parameters": {"length": 25, "angle": 90}}
    assert describe("nx_sheet_metal_feature", flange) == (
        "Add sheet-metal advanced flange, 25 mm long, 90°"
    )
    edit = {"operation": "tab", "feature": "obj_flange", "parameters": {"thickness": 2}}
    assert describe("nx_sheet_metal_feature", edit) == (
        'Edit sheet-metal tab "SB_FLANGE(4)", 2 mm thick'
    )


def test_feature_parameters_and_formats_show_the_values_that_change():
    values = {"obj_p12": "25", "p13": "4"}
    assert describe("nx_set_feature_parameters", {"feature": "obj_extrude", "values": values}) == (
        'Set a parameter = 25 (+1 more) on "EXTRUDE(2)"'
    )
    tolerance = {
        "dimension": "obj_edge",
        "decimal_places": 2,
        "tolerance_type": "bilateral",
        "upper_tolerance": 0.1,
        "lower_tolerance": -0.05,
    }
    assert describe("nx_edit_dimension_format", tolerance) == (
        'Format dimension "EDGE_1": 2 decimals, +0.1/-0.05 mm'
    )
    fcf = {
        "faces": ["obj_face"],
        "characteristic": "ProfileOfASurface",
        "tolerance": 0.2,
        "zone_shape": "none",
        "datums": ["a", "b"],
    }
    assert describe("nx_pmi_fcf", fcf) == (
        'Add profile of a surface tolerance 0.2 mm on "FACE_7" to 2 datums'
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
