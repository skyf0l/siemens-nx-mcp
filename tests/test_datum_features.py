"""Datum features, sketches on planes and circular patterns: local contract tests.

Native behaviour is validated live on NX (see CLAUDE.md, live development loop);
these tests cover the geometry checks, argument validation and public schemas.
"""

import json
import math
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.agent_guidance import MANIFEST
from nx_mcp.datum_features import (
    on_plane,
    orthonormal_frame,
    parallel,
    plane_through,
    same_direction,
)
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace

NEW_TOOLS = {
    "nx_datum_plane",
    "nx_datum_axis",
    "nx_datum_csys",
    "nx_datum_point",
    "nx_create_sketch_on",
    "nx_circular_pattern",
}


def test_plane_through_three_points_and_collinear_rejection():
    normal = plane_through([[0, 0, 5], [10, 0, 5], [0, 10, 5]])
    assert normal == pytest.approx([0, 0, 1])
    with pytest.raises(NXToolError, match="collinear"):
        plane_through([[0, 0, 0], [1, 1, 1], [2, 2, 2]])
    with pytest.raises(NXToolError, match="three points"):
        plane_through([[0, 0, 0], [1, 0, 0]])


def test_geometry_predicates():
    assert parallel([0, 0, 1], [0, 0, -1]) and not parallel([0, 0, 1], [0, 1, 0])
    assert on_plane([3, 4, 10], [0, 0, 10], [0, 0, 1], 1e-9)
    s = math.sqrt(0.5)
    assert orthonormal_frame([s, s, 0], [-s, s, 0])[2] == pytest.approx([0, 0, 1])
    with pytest.raises(NXToolError, match="orthonormal"):
        orthonormal_frame([1, 0, 0], [1, 0, 0])


def test_same_direction_rejects_reversed_normals_and_axes():
    # Live NX: Plane.SetFlip reverses only the normal, so the sense is verified too.
    assert same_direction([0, 0, 1], [0, 0, 1])
    assert not same_direction([0, 0, 1], [0, 0, -1])
    assert not same_direction([1, 0, 0], [0, 1, 0])


@pytest.mark.parametrize(
    ("tool", "params", "message"),
    [
        ("nx_datum_plane", {"method": "offset"}, "planar reference"),
        ("nx_datum_plane", {"method": "point_normal", "origin": [0, 0, 0]}, "origin and normal"),
        (
            "nx_datum_plane",
            {"method": "point_normal", "origin": [0, 0, 0], "normal": [0, 0, 0]},
            "nonzero",
        ),
        (
            "nx_datum_plane",
            {"method": "three_points", "points": [[0, 0, 0], [1, 0, 0], [2, 0, 0]]},
            "collinear",
        ),
        ("nx_datum_plane", {"method": "sideways"}, "method must be"),
        (
            "nx_datum_axis",
            {"method": "two_points", "start": [1, 1, 1], "end": [1, 1, 1]},
            "must differ",
        ),
        (
            "nx_datum_axis",
            {"method": "point_direction", "origin": [0, 0, 0]},
            "origin and direction",
        ),
        (
            "nx_datum_csys",
            {"origin": [0, 0, 0], "x_axis": [1, 0, 0], "y_axis": [0, 0, 2]},
            "orthonormal",
        ),
        ("nx_datum_point", {"position": [0, math.inf, 0]}, "finite"),
        ("nx_datum_point", {"position": [0, 0]}, "three numbers"),
        ("nx_datum_axis", {"method": "sideways"}, "method must be"),
        (
            "nx_datum_axis",
            {"method": "point_direction", "origin": [0, 0, 0], "direction": [0, 0, 0]},
            "nonzero",
        ),
        (
            "nx_circular_pattern",
            {
                "features": ["f"],
                "count": 4,
                "angle": 0,
                "axis_origin": [0, 0, 0],
                "axis_direction": [0, 0, 1],
            },
            "positive pitch",
        ),
        (
            "nx_circular_pattern",
            {
                "features": ["f"],
                "count": 4,
                "angle": 90,
                "axis": "a",
                "axis_origin": [0, 0, 0],
                "axis_direction": [0, 0, 1],
            },
            "either a datum axis",
        ),
        (
            "nx_circular_pattern",
            {
                "features": ["f"],
                "count": 7,
                "angle": 60,
                "axis_origin": [0, 0, 0],
                "axis_direction": [0, 0, 1],
            },
            "overlap",
        ),
        (
            "nx_circular_pattern",
            {
                "features": ["f"],
                "count": 1,
                "angle": 60,
                "axis_origin": [0, 0, 0],
                "axis_direction": [0, 0, 1],
            },
            "2-360",
        ),
        (
            "nx_circular_pattern",
            {"features": ["f"], "count": 4, "angle": 90},
            "either a datum axis",
        ),
        (
            "nx_circular_pattern",
            {"features": ["f"], "count": 4, "angle": 90, "axis_origin": [0, 0, 0]},
            "both required",
        ),
    ],
)
def test_invalid_requests_are_rejected_before_native_work(rig, tool, params, message):
    with pytest.raises(NXToolError, match=message) as error:
        rig.e.execute(tool, params)
    assert error.value.code == "NX_INVALID_ARGUMENT"
    # Early input checks fail before any undo mark exists: nothing started.
    outcome = error.value.details.get("mutation_outcome", "not_started")
    assert outcome in {"rolled_back", "not_started"}


@pytest.mark.asyncio
async def test_new_tools_are_public_typed_and_honestly_classified(tmp_path: Path):
    server = create_server(object(), Workspace(tmp_path), surface="full")
    async with create_connected_server_and_client_session(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) >= NEW_TOOLS
    plane = tools["nx_datum_plane"].inputSchema
    assert plane["properties"]["method"]["enum"] == ["offset", "point_normal", "three_points"]
    assert plane["additionalProperties"] is False and "operation_id" in plane["properties"]
    assert tools["nx_circular_pattern"].inputSchema["required"] == ["features", "count", "angle"]
    assert tools["nx_create_sketch_on"].inputSchema["required"] == ["target"]
    for name in NEW_TOOLS:
        assert MANIFEST["tools"][name]["status"] in {"experimental", "tested"}


def test_live_acceptance_script_uses_public_tools_only(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    steps = json.loads((root / "acceptance" / "live" / "modeling.json").read_text("utf-8"))
    assert steps[0]["tool"] == "nx_create_part"
    used = {step["tool"] for step in steps}
    assert used >= NEW_TOOLS
    fragment = json.loads(
        (root / "src" / "nx_mcp" / "capability_fragments" / "modeling.json").read_text("utf-8")
    )
    assert set(fragment) == NEW_TOOLS
    for entry in fragment.values():
        assert entry["status"] == "tested"
        assert entry["evidence_type"] == "real_NX_v2406_scoped"
        assert entry["scope"]


@pytest.mark.asyncio
async def test_live_acceptance_tools_exist_on_the_agent_surface(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    steps = json.loads((root / "acceptance" / "live" / "modeling.json").read_text("utf-8"))
    server = create_server(object(), Workspace(tmp_path), surface="full")
    async with create_connected_server_and_client_session(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {step["tool"] for step in steps} <= names


def test_datum_axis_or_csys_is_not_a_plane_reference():
    # Live NX 2406 (verify_a): a datum axis passed as offset reference / sketch target
    # reached AskFaceData and failed with raw NX_API_ERROR 820022.
    from types import SimpleNamespace

    from nx_mcp.datum_features import DatumFeaturesMixin

    point = SimpleNamespace(X=0.0, Y=0.0, Z=0.0)
    axis = SimpleNamespace(Origin=point, Direction=point, Tag=1)
    csys = SimpleNamespace(Origin=point, Orientation=None, Tag=2)
    for obj in (axis, csys):
        with pytest.raises(NXToolError) as error:
            DatumFeaturesMixin()._df_plane_of(obj)
        assert error.value.code == "NX_OBJECT_TYPE_MISMATCH"


def test_datum_tools_refuse_while_a_sketch_is_active():
    # Live NX 2406 (verify_a): committing a datum feature silently ended sketch mode.
    from types import SimpleNamespace

    from nx_mcp.datum_features import DatumFeaturesMixin

    mixin = DatumFeaturesMixin()
    mixin.session = SimpleNamespace(ActiveSketch=object())
    mixin._work_part = lambda: object()
    calls = [
        lambda: mixin._datum_plane("point_normal", origin=[0, 0, 0], normal=[0, 0, 1]),
        lambda: mixin._datum_axis("point_direction", origin=[0, 0, 0], direction=[0, 0, 1]),
        lambda: mixin._datum_csys([0, 0, 0], [1, 0, 0], [0, 1, 0]),
        lambda: mixin._datum_point([1, 2, 3]),
        lambda: mixin._circular_pattern(
            ["f"], 4, 90, axis_origin=[0, 0, 0], axis_direction=[0, 0, 1]
        ),
    ]
    for call in calls:
        with pytest.raises(NXToolError) as error:
            call()
        assert error.value.code == "NX_SKETCH_ACTIVE"
