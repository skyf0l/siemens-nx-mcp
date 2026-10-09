"""Sketch point/curve dimensions, midpoint and point-on-curve: local contract tests.

Native behaviour is validated live on NX (acceptance/live/sketch_constraints.json);
these tests cover the pure geometry, argument validation and public schemas.
"""

import math
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.agent_guidance import MANIFEST
from nx_mcp.request_summary import summarize
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.sketch_constraints import (
    angle_label_origin,
    are_parallel,
    bisector_projection,
    check_value,
    direction_angle,
    equidistance_residual,
    line_intersection,
    line_point_distance,
    point_metric,
)
from nx_mcp.workspace import Workspace

NEW_TOOLS = {
    "nx_sketch_point_dimension",
    "nx_sketch_curve_dimension",
    "nx_sketch_midpoint",
    "nx_sketch_point_on_curve",
}


def test_point_metrics():
    assert point_metric("distance", [0, 0], [3, 4]) == pytest.approx(5)
    assert point_metric("horizontal", [1, 0], [-3, 4]) == pytest.approx(4)
    assert point_metric("vertical", [1, 2], [-3, -4]) == pytest.approx(6)


def test_line_geometry():
    assert line_point_distance([0, 0], [10, 0], [3, -7]) == pytest.approx(7)
    with pytest.raises(NXToolError, match="Degenerate"):
        line_point_distance([1, 1], [1, 1], [0, 0])
    assert direction_angle([1, 0], [1, 1]) == pytest.approx(45)
    assert direction_angle([1, 0], [-1, 0]) == pytest.approx(180)
    assert are_parallel([1, 0], [-3, 0]) and not are_parallel([1, 0], [1, 0.01])
    assert line_intersection([0, 0], [1, 0], [5, -1], [5, 1]) == pytest.approx([5, 0])
    assert line_intersection([0, 0], [1, 0], [0, 1], [1, 1]) is None


def test_angle_label_sits_inside_the_angle():
    origin = angle_label_origin([0, 0], [10, 0], [0, 0], [10, 10])
    assert 0 < math.degrees(math.atan2(origin[1], origin[0])) < 45


def test_bisector_projection_and_equidistance():
    p = bisector_projection([7, 3], [0, 0], [10, 0])
    assert p == pytest.approx([5, 3])
    assert equidistance_residual(p, [0, 0], [10, 0]) == pytest.approx(0, abs=1e-12)
    assert equidistance_residual([7, 3], [0, 0], [10, 0]) > 1
    with pytest.raises(NXToolError, match="coincide"):
        bisector_projection([1, 1], [2, 2], [2, 2])


def test_value_checks():
    assert check_value("distance", None) is None
    assert check_value("angle", 90) == 90.0
    for kind, bad in [("distance", 0), ("distance", -1), ("angle", 180), ("distance", math.nan)]:
        with pytest.raises(NXToolError):
            check_value(kind, bad)
    with pytest.raises(NXToolError):
        check_value("distance", True)


@pytest.mark.parametrize(
    ("tool", "params", "message"),
    [
        (
            "nx_sketch_point_dimension",
            {
                "sketch_id": "s",
                "first": "a",
                "first_point": "start",
                "second": "b",
                "second_point": "end",
                "kind": "diagonal",
            },
            "kind must be",
        ),
        (
            "nx_sketch_point_dimension",
            {
                "sketch_id": "s",
                "first": "a",
                "first_point": "start",
                "second": "b",
                "second_point": "end",
                "value": -2,
            },
            "positive",
        ),
        (
            "nx_sketch_curve_dimension",
            {"sketch_id": "s", "first": "a", "second": "b", "kind": "angle", "value": 190},
            "between 0 and 180",
        ),
        (
            "nx_sketch_curve_dimension",
            {"sketch_id": "s", "first": "a", "second": "b", "kind": "radius"},
            "kind must be",
        ),
        (
            "nx_sketch_curve_dimension",
            {
                "sketch_id": "s",
                "first": "a",
                "second": "b",
                "kind": "angle",
                "second_point": "center",
            },
            "only to perpendicular_distance",
        ),
        (
            "nx_sketch_curve_dimension",
            {
                "sketch_id": "s",
                "first": "a",
                "second": "b",
                "kind": "perpendicular_distance",
                "second_point": "quadrant",
            },
            "second_point must be",
        ),
        (
            "nx_sketch_midpoint",
            {"sketch_id": "s", "point_curve": "a", "point": "mid", "target_curve": "b"},
            "start, end or center",
        ),
        (
            "nx_sketch_midpoint",
            {
                "sketch_id": "s",
                "point_curve": "a",
                "point": "start",
                "target_curve": "b",
                "on_curve": "yes",
            },
            "boolean",
        ),
        (
            "nx_sketch_point_on_curve",
            {"sketch_id": "s", "point_curve": "a", "point": "mid", "curve": "b"},
            "start, end or center",
        ),
    ],
)
def test_invalid_requests_are_rejected_before_native_work(rig, tool, params, message):
    with pytest.raises(NXToolError, match=message) as error:
        rig.e.execute(tool, params)
    assert error.value.code == "NX_INVALID_ARGUMENT"
    outcome = error.value.details.get("mutation_outcome", "not_started")
    assert outcome in {"rolled_back", "not_started"}


def test_unknown_ids_fail_as_not_found(rig):
    with pytest.raises(NXToolError) as error:
        rig.e.execute(
            "nx_sketch_midpoint",
            {"sketch_id": "obj_missing", "point_curve": "a", "point": "start", "target_curve": "b"},
        )
    assert error.value.code in {"NX_NOT_FOUND", "NX_OBJECT_STALE"}


def test_panel_summaries():
    line = summarize(
        "nx_sketch_curve_dimension",
        {"sketch_id": "s", "first": "a", "second": "b", "kind": "angle", "value": 30},
    )
    assert "angle" in line and "30" in line
    line = summarize("nx_sketch_point_dimension", {"kind": "distance", "value": None})
    assert "current size" in line
    line = summarize("nx_sketch_point_dimension", {"kind": "vertical", "value": 12}, units="mm")
    assert line.endswith("to 12 mm")
    # on_curve=false only makes the point equidistant from the target's ends.
    line = summarize(
        "nx_sketch_midpoint",
        {"point_curve": "a", "point": "end", "target_curve": "b", "on_curve": False},
    )
    assert "equidistant from the ends of" in line and "midpoint" not in line
    line = summarize(
        "nx_sketch_midpoint", {"point_curve": "a", "point": "end", "target_curve": "b"}
    )
    assert "at the midpoint of" in line


@pytest.mark.asyncio
async def test_new_tools_are_public_typed_and_classified(tmp_path: Path):
    server = create_server(object(), Workspace(tmp_path))
    async with create_connected_server_and_client_session(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) >= NEW_TOOLS
    point = tools["nx_sketch_point_dimension"].inputSchema
    assert point["properties"]["kind"]["enum"] == ["distance", "horizontal", "vertical"]
    assert point["additionalProperties"] is False
    assert point["required"] == ["sketch_id", "first", "first_point", "second", "second_point"]
    curve = tools["nx_sketch_curve_dimension"].inputSchema
    assert curve["properties"]["kind"]["enum"] == [
        "angle",
        "parallel_distance",
        "perpendicular_distance",
    ]
    mid = tools["nx_sketch_midpoint"].inputSchema
    assert mid["properties"]["point"]["enum"] == ["start", "end", "center"]
    for name in NEW_TOOLS:
        assert MANIFEST["tools"][name]["status"] in {"experimental", "tested"}
