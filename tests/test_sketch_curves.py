"""Sketch curve tools: local contract tests.

Native behaviour is validated live on NX (acceptance/live/sketch_curves.json);
these tests cover the pure geometry helpers, argument validation and schemas.
"""

import math
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp import sketch_curves_server
from nx_mcp.agent_guidance import MANIFEST
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.sketch_curves import (
    distinct_ids,
    ellipse_args,
    ellipse_perimeter,
    on_arc,
    on_segment,
    polygon_vertices,
    reflect2,
)
from nx_mcp.workspace import Workspace

NEW_TOOLS = {
    "nx_sketch_polygon",
    "nx_sketch_ellipse",
    "nx_sketch_fillet",
    "nx_sketch_chamfer",
    "nx_sketch_offset",
    "nx_sketch_mirror",
    "nx_sketch_construction",
    "nx_sketch_project",
    "nx_sketch_intersection",
}


def test_polygon_vertices_inscribed_and_circumscribed():
    hexagon = polygon_vertices([10, 5], 6, 20)
    assert hexagon[0] == pytest.approx([30, 5])
    assert all(math.dist(p, [10, 5]) == pytest.approx(20) for p in hexagon)
    assert math.dist(hexagon[0], hexagon[1]) == pytest.approx(20)  # hexagon side = R
    square = polygon_vertices([0, 0], 4, 10, inscribed=False)
    mid = [(square[0][0] + square[1][0]) / 2, (square[0][1] + square[1][1]) / 2]
    assert math.hypot(*mid) == pytest.approx(10)  # edge midpoint touches the circle
    rotated = polygon_vertices([0, 0], 3, 1, rotation=90)
    assert rotated[0] == pytest.approx([0, 1])
    # Counterclockwise order: positive signed area.
    area = sum(
        hexagon[k][0] * hexagon[(k + 1) % 6][1] - hexagon[(k + 1) % 6][0] * hexagon[k][1]
        for k in range(6)
    )
    assert area > 0


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (([0, 0], 2, 10), "3-64"),
        (([0, 0], 65, 10), "3-64"),
        (([0, 0], 6.0, 10), "3-64"),
        (([0, 0], 6, 0), "positive"),
        (([0, 0, 0], 6, 10), r"\[x, y\]"),
        (([0, math.nan], 6, 10), "finite"),
    ],
)
def test_polygon_rejects_bad_arguments(args, message):
    with pytest.raises(NXToolError, match=message):
        polygon_vertices(*args)


def test_ellipse_arguments_and_perimeter():
    assert ellipse_args([1, 2], 20, 10, 30, 0, 360) == ([1.0, 2.0], 20.0, 10.0, 30.0, 0.0, 360.0)
    with pytest.raises(NXToolError, match=">= minor"):
        ellipse_args([0, 0], 5, 10, 0, 0, 360)
    with pytest.raises(NXToolError, match="end_angle"):
        ellipse_args([0, 0], 10, 5, 0, 90, 90)
    assert ellipse_perimeter(10, 10) == pytest.approx(2 * math.pi * 10)
    assert ellipse_perimeter(20, 10) == pytest.approx(96.88448220547, rel=1e-9)


def test_reflection_and_id_lists():
    assert reflect2([1, 0], [0, 0], [0, 1]) == pytest.approx([-1, 0])
    assert reflect2([2, 3], [0, 0], [1, 1]) == pytest.approx([3, 2])
    assert distinct_ids(["a", "b"], "curves") == ["a", "b"]
    with pytest.raises(NXToolError, match="duplicates"):
        distinct_ids(["a", "a"], "curves")
    with pytest.raises(NXToolError, match="object IDs"):
        distinct_ids([], "curves")


def test_trim_extent_predicates():
    assert on_segment([5, 0], [[0, 0], [10, 0]], 1e-9)
    assert not on_segment([11, 0], [[0, 0], [10, 0]], 1e-9)  # an extension, not a trim
    quarter = {"center": [0, 0], "radius": 10, "start": 0.0, "sweep": math.pi / 2, "direction": 1}
    assert on_arc([0, 10], quarter, 1e-9) and on_arc([10, 0], quarter, 1e-9)
    assert not on_arc([-10, 0], quarter, 1e-9) and not on_arc([5, 5], quarter, 1e-9)
    clockwise = dict(quarter, direction=-1)
    assert on_arc([0, -10], clockwise, 1e-9) and not on_arc([0, 10], clockwise, 1e-9)


@pytest.mark.parametrize(
    ("tool", "params", "message"),
    [
        (
            "nx_sketch_polygon",
            {"sketch_id": "s", "center": [0, 0], "sides": 2, "radius": 5},
            "3-64",
        ),
        (
            "nx_sketch_polygon",
            {"sketch_id": "s", "center": [0, 0], "sides": 6, "radius": -1},
            "positive",
        ),
        (
            "nx_sketch_ellipse",
            {"sketch_id": "s", "center": [0, 0], "major_radius": 2, "minor_radius": 3},
            ">= minor",
        ),
        (
            "nx_sketch_ellipse",
            {
                "sketch_id": "s",
                "center": [0, 0],
                "major_radius": 3,
                "minor_radius": 2,
                "start_angle": 10,
                "end_angle": 400,
            },
            "end_angle",
        ),
        (
            "nx_sketch_fillet",
            {"sketch_id": "s", "curve1": "a", "curve2": "b", "radius": 0},
            "positive",
        ),
        (
            "nx_sketch_fillet",
            {"sketch_id": "s", "curve1": "a", "curve2": "a", "radius": 1},
            "distinct",
        ),
        (
            "nx_sketch_chamfer",
            {"sketch_id": "s", "curve1": "a", "curve2": "b", "distance1": 1, "distance2": 0},
            "positive",
        ),
        (
            "nx_sketch_offset",
            {"sketch_id": "s", "curves": ["a"], "distance": 0},
            "positive",
        ),
        (
            "nx_sketch_offset",
            {"sketch_id": "s", "curves": ["a"], "distance": 1, "side": "up"},
            "side",
        ),
        (
            "nx_sketch_offset",
            {"sketch_id": "s", "curves": [], "distance": 1},
            "object IDs",
        ),
        (
            "nx_sketch_mirror",
            {"sketch_id": "s", "curves": ["a", "a"], "mirror_line": "m"},
            "duplicates",
        ),
        (
            "nx_sketch_mirror",
            {"sketch_id": "s", "curves": ["a"], "mirror_line": "a"},
            "mirror_line",
        ),
        (
            "nx_sketch_construction",
            {"sketch_id": "s", "curves": ["a"], "construction": "yes"},
            "true or false",
        ),
        ("nx_sketch_project", {"sketch_id": "s", "objects": []}, "object IDs"),
        ("nx_sketch_intersection", {"sketch_id": "s", "faces": ["f", "f"]}, "duplicates"),
    ],
)
def test_invalid_requests_are_rejected_before_native_work(rig, tool, params, message):
    with pytest.raises(NXToolError, match=message) as error:
        rig.e.execute(tool, params)
    assert error.value.code == "NX_INVALID_ARGUMENT"
    outcome = error.value.details.get("mutation_outcome", "not_started")
    assert outcome in {"rolled_back", "not_started"}


def test_summaries_render_one_line():
    from types import SimpleNamespace

    c = SimpleNamespace(
        obj=lambda v: f'"{v}"',
        count=lambda v, n: f"{len(v)} {n}s",
        length=lambda v: f"{v} mm",
    )
    params = {
        "sketch_id": "S",
        "sides": 6,
        "radius": 5,
        "major_radius": 4,
        "minor_radius": 2,
        "distance1": 1,
        "distance": 2,
        "curves": ["a"],
        "objects": ["e"],
        "faces": ["f"],
        "mirror_line": "m",
    }
    for template in sketch_curves_server.SUMMARIES.values():
        line = template(params, c)
        assert "\n" not in line and '"S"' in line


@pytest.mark.asyncio
async def test_new_tools_are_public_typed_and_honestly_classified(tmp_path: Path):
    server = create_server(object(), Workspace(tmp_path))
    async with create_connected_server_and_client_session(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) >= NEW_TOOLS
    polygon = tools["nx_sketch_polygon"].inputSchema
    assert polygon["required"] == ["sketch_id", "center", "sides", "radius"]
    assert polygon["additionalProperties"] is False
    offset = tools["nx_sketch_offset"].inputSchema
    assert offset["properties"]["side"]["enum"] == ["auto", "left", "right"]
    for name in NEW_TOOLS:
        assert MANIFEST["tools"][name]["status"] in {"experimental", "tested"}
