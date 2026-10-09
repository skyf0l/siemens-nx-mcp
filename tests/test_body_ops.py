"""Body operations (chamfer, variable blend, mirror, split, trim, scale): local contract tests.

Native behaviour is validated live on NX (acceptance/live/body_ops.json); these
tests cover the pure helpers, argument validation and the public schemas.
"""

import math
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.agent_guidance import MANIFEST
from nx_mcp.body_ops import (
    blend_points,
    chamfer_offsets,
    plane_box_range,
    plane_frame,
    plane_misses_box,
    scale_factor,
)
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace

NEW_TOOLS = {
    "nx_chamfer_advanced",
    "nx_variable_blend",
    "nx_mirror_body_about",
    "nx_split_body",
    "nx_trim_body",
    "nx_scale_body",
}


def test_chamfer_offsets():
    assert chamfer_offsets("symmetric", 2) == (2.0, 2.0, None)
    assert chamfer_offsets("two_offsets", 2, 5) == (2.0, 5.0, None)
    o1, o2, a = chamfer_offsets("offset_angle", 4, angle=30)
    assert (o1, a) == (4.0, 30.0) and o2 == pytest.approx(4 * math.tan(math.radians(30)))
    for args, message in [
        (("symmetric", 2, 3), "only offset1"),
        (("two_offsets", 2), "needs offset2"),
        (("two_offsets", 2, -1), "positive"),
        (("offset_angle", 2), "needs angle"),
        (("offset_angle", 2, None, 90), "between 0 and 90"),
        (("offset_angle", 2, 3, 45), "not offset2"),
        (("bevel", 2), "method must be"),
        (("symmetric", math.nan), "finite"),
    ]:
        with pytest.raises(NXToolError, match=message):
            chamfer_offsets(*args)


def test_plane_misses_box_rejects_planes_that_cannot_cut_the_body():
    lo, hi = [0, 0, 0], [40, 20, 10]
    assert plane_misses_box(lo, hi, [0, 0, 50], [0, 0, 1])  # live: NX 670030
    assert plane_misses_box(lo, hi, [0, 0, 50], [0, 0, -1])
    assert plane_misses_box(lo, hi, [0, 0, 0], [0, 0, 1])  # on the bottom face
    assert plane_misses_box(lo, hi, [40, 0, 0], [-1, 0, 0])
    assert not plane_misses_box(lo, hi, [0, 0, 4], [0, 0, 1])
    assert not plane_misses_box(lo, hi, [20, 10, 0], [1, 1, 0])  # oblique, live 4000+4000
    assert plane_box_range(lo, hi, [0, 0, 4], [0, 0, 2]) == (-4.0, 6.0)


def test_blend_points():
    pts = blend_points([{"parameter": 1, "radius": 3}, {"parameter": 0, "radius": 1}])
    assert pts == [(0.0, 1.0), (1.0, 3.0)]
    for points, message in [
        ([{"parameter": 0, "radius": 1}], "2-20"),
        ([{"parameter": i / 25, "radius": 1} for i in range(21)], "2-20"),
        ([{"parameter": 0, "radius": 1}, {"parameter": 1.5, "radius": 1}], "within 0..1"),
        ([{"parameter": 0, "radius": 1}, {"parameter": 1, "radius": 0}], "positive"),
        ([{"parameter": 0.5, "radius": 1}, {"parameter": 0.5, "radius": 2}], "distinct"),
        ([{"parameter": 0, "radius": 1}, {"parameter": 1}], "exactly"),
    ]:
        with pytest.raises(NXToolError, match=message):
            blend_points(points)


def test_scale_factor():
    assert scale_factor(2) == 2.0
    for value in [0, -2, 1, math.inf, True]:
        with pytest.raises(NXToolError):
            scale_factor(value)


@pytest.mark.parametrize("normal", [[0, 0, 1], [1, 0, 0], [0, -3, 0], [1, 2, 3]])
def test_plane_frame_is_right_handed_orthonormal(normal):
    x, y, z = plane_frame(normal)
    n = math.sqrt(sum(v * v for v in normal))
    assert z == pytest.approx([v / n for v in normal])
    for a in (x, y, z):
        assert sum(v * v for v in a) == pytest.approx(1)
    assert sum(a * b for a, b in zip(x, y, strict=True)) == pytest.approx(0, abs=1e-12)
    cross = [x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]]
    assert cross == pytest.approx(z)


@pytest.mark.parametrize(
    ("tool", "params", "message"),
    [
        ("nx_chamfer_advanced", {"edges": [], "method": "symmetric", "offset1": 1}, "non-empty"),
        (
            "nx_chamfer_advanced",
            {"edges": ["e"], "method": "two_offsets", "offset1": 1},
            "needs offset2",
        ),
        (
            "nx_chamfer_advanced",
            {"edges": ["e"], "method": "symmetric", "offset1": 1, "reverse": True},
            "reverse",
        ),
        ("nx_variable_blend", {"edge": "e", "points": []}, "2-20"),
        ("nx_mirror_body_about", {"body": "", "plane": "p"}, "object ID"),
        ("nx_split_body", {"body": "b", "tool": ""}, "object ID"),
        ("nx_split_body", {"body": "b", "tool": "b"}, "differ"),
        ("nx_trim_body", {"body": "b", "tool": "t", "reverse": "yes"}, "boolean"),
        ("nx_scale_body", {"bodies": ["b"], "factor": 1}, "factor 1"),
        ("nx_scale_body", {"bodies": ["b"], "factor": 2, "origin": [0, 0]}, "three numbers"),
        ("nx_scale_body", {"bodies": ["b", "b"], "factor": 2}, "duplicates"),
    ],
)
def test_invalid_requests_are_rejected_before_native_work(rig, tool, params, message):
    with pytest.raises(NXToolError, match=message) as error:
        rig.e.execute(tool, params)
    assert error.value.code == "NX_INVALID_ARGUMENT"
    outcome = error.value.details.get("mutation_outcome", "not_started")
    assert outcome in {"rolled_back", "not_started"}


@pytest.mark.asyncio
async def test_new_tools_are_public_typed_and_honestly_classified(tmp_path: Path):
    server = create_server(object(), Workspace(tmp_path), surface="full")
    async with create_connected_server_and_client_session(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) >= NEW_TOOLS
    chamfer = tools["nx_chamfer_advanced"].inputSchema
    assert chamfer["properties"]["method"]["enum"] == ["symmetric", "two_offsets", "offset_angle"]
    assert chamfer["additionalProperties"] is False and "operation_id" in chamfer["properties"]
    assert tools["nx_variable_blend"].inputSchema["required"] == ["edge", "points"]
    assert tools["nx_scale_body"].inputSchema["required"] == ["bodies", "factor"]
    assert tools["nx_trim_body"].inputSchema["required"] == ["body", "tool"]
    for name in NEW_TOOLS:
        assert MANIFEST["tools"][name]["status"] in {"experimental", "tested"}


def test_chamfer_panel_summary_shows_every_size():
    from nx_mcp.request_summary import summarize

    two = {"edges": ["a", "b"], "method": "two_offsets", "offset1": 2, "offset2": 4}
    angle = {"edges": ["a"], "method": "offset_angle", "offset1": 3, "angle": 30}
    assert summarize("nx_chamfer_advanced", two, units="mm").endswith("(two offsets, 2 mm x 4 mm)")
    assert "(offset angle, 3 mm at 30 deg)" in summarize("nx_chamfer_advanced", angle, units="mm")
