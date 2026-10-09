"""Native hole features: local contract tests.

Native behaviour is validated live on NX 2406 (acceptance/live/holes.json);
these tests cover the analytic volumes, argument validation and the schema.
"""

import math
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.agent_guidance import MANIFEST
from nx_mcp.holes import (
    countersink_height,
    hole_positions,
    hole_spec,
    hole_volume,
    match_thread_row,
    matches_nominal,
    off_plane,
    tip_height,
)
from nx_mcp.request_summary import summarize
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace

ROWS = [
    {
        "Standard": "Metric Coarse",
        "Unit": "Metric",
        "Size": "M10 x 1.5",
        "RadialEngage": engage,
        "TapDrillDia": drill,
    }
    for engage, drill in (("0.75", "8.5"), ("0.5", "9"), ("Custom", "8.5"))
] + [{"Standard": "Inch UNC", "Unit": "English", "Size": "1/4-20", "TapDrillDia": "0.201"}]


def test_analytic_volumes():
    tip = math.pi * 25 * tip_height(10, 118) / 3
    assert tip_height(10, 90) == pytest.approx(5)
    simple = hole_spec("simple", 10, depth=20)
    assert hole_volume(simple) == pytest.approx(math.pi * 25 * 20 + tip)
    cb = hole_spec("counterbore", 10, depth=20, counterbore_diameter=16, counterbore_depth=5)
    assert hole_volume(cb) == pytest.approx(math.pi * (64 * 5 + 25 * 15) + tip)
    cs = hole_spec("countersink", 10, depth=20, countersink_diameter=16, countersink_angle=90)
    h = countersink_height(10, 16, 90)
    assert h == pytest.approx(3)
    frustum = math.pi * 3 / 3 * (64 + 40 + 25)
    assert hole_volume(cs) == pytest.approx(frustum + math.pi * 25 * 17 + tip)


def test_removed_volume_flags_breakout():
    # Live NX 2406: a 4 x 30 blind hole in a 20 mm block removed 251.327 (pi*4*20),
    # nominal 382.025; a hole 2 mm from the side removed 142.157 of 158.361.
    assert matches_nominal(599.4645451643883, 599.4645451643939) is True
    assert matches_nominal(251.32741228718078, 382.0248765816242) is False
    assert matches_nominal(142.1567926311909, 158.36060317065628) is False
    assert matches_nominal(125.66, None) is None


def test_panel_summary_states_extent():
    line = summarize(
        "nx_hole_feature",
        {"face": "f", "positions": [[0, 0, 0]] * 2, "form": "simple", "diameter": 6, "depth": 10},
        units="mm",
    )
    assert line == 'Simple hole 6 mm x2, 10 mm deep, on "f"'
    line = summarize(
        "nx_hole_feature",
        {
            "face": "f",
            "positions": [[0, 0, 0]],
            "form": "threaded",
            "thread_size": "M8 x 1.25",
            "diameter": 6.8,
            "through_all": True,
        },
    )
    assert "M8 x 1.25 x1, through all" in line


def test_positions_and_plane_checks():
    assert hole_positions([[1, 2, 3], [4, 5, 6]]) == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    with pytest.raises(NXToolError, match="1-100"):
        hole_positions([])
    with pytest.raises(NXToolError, match="1-100"):
        hole_positions([[i, 0, 0] for i in range(101)])
    with pytest.raises(NXToolError, match="duplicates"):
        hole_positions([[1, 1, 0], [1, 1, 0]])
    with pytest.raises(NXToolError, match=r"\[x, y, z\]"):
        hole_positions([[1, 1]])
    pts = [[0, 0, 10], [5, 5, 10.5]]
    assert off_plane(pts, [0, 0, 10], [0, 0, 1], 1e-3) == [(1, pytest.approx(0.5))]


def test_thread_row_matching():
    row = match_thread_row(ROWS, "Metric Coarse", "M10 x 1.5", 9.0, "mm")
    assert row["RadialEngage"] == "0.5"
    assert match_thread_row(ROWS, "Metric Coarse", "M10 x 1.5", 8.5, "mm")["RadialEngage"] == "0.75"
    with pytest.raises(NXToolError, match=r"\[8.5, 9.0\]") as error:
        match_thread_row(ROWS, "Metric Coarse", "M10 x 1.5", 10, "mm")
    assert error.value.code == "NX_INVALID_ARGUMENT"
    with pytest.raises(NXToolError) as error:
        match_thread_row(ROWS, "Metric Coarse", "M12 x 1.75", 10.2, "mm")
    assert error.value.code == "NX_NOT_FOUND"
    with pytest.raises(NXToolError) as error:
        match_thread_row(ROWS, "Nope", "M10 x 1.5", 8.5, "mm")
    assert error.value.code == "NX_NOT_FOUND"
    with pytest.raises(NXToolError) as error:
        match_thread_row(ROWS, "Inch UNC", "1/4-20", 0.201, "mm")
    assert error.value.code == "NX_UNSUPPORTED_ARGUMENT"


BASE = {"face": "f", "positions": [[0, 0, 0]]}


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"form": "simple", "diameter": 5}, "either depth or through_all"),
        ({"form": "simple", "diameter": 5, "depth": 5, "through_all": True}, "either depth"),
        ({"form": "simple", "diameter": -5, "depth": 5}, "positive"),
        ({"form": "simple", "diameter": 5, "depth": 5, "tip_angle": 180}, "tip_angle"),
        ({"form": "conical", "diameter": 5, "depth": 5}, "form must be"),
        (
            {"form": "simple", "diameter": 5, "depth": 5, "counterbore_diameter": 8},
            "do not apply",
        ),
        ({"form": "counterbore", "diameter": 5, "depth": 5}, "counterbore needs"),
        (
            {
                "form": "counterbore",
                "diameter": 5,
                "depth": 5,
                "counterbore_diameter": 4,
                "counterbore_depth": 2,
            },
            "larger than diameter",
        ),
        (
            {
                "form": "counterbore",
                "diameter": 5,
                "depth": 5,
                "counterbore_diameter": 8,
                "counterbore_depth": 5,
            },
            "less than depth",
        ),
        ({"form": "countersink", "diameter": 5, "depth": 5}, "countersink needs"),
        (
            {"form": "countersink", "diameter": 5, "depth": 2, "countersink_diameter": 15},
            "shallower than depth",
        ),
        (
            {
                "form": "countersink",
                "diameter": 5,
                "depth": 9,
                "countersink_diameter": 8,
                "countersink_angle": 0,
            },
            "countersink_angle",
        ),
        ({"form": "threaded", "diameter": 8.5, "depth": 20}, "thread_standard"),
        (
            {
                "form": "threaded",
                "diameter": 8.5,
                "depth": 20,
                "thread_standard": "Metric Coarse",
                "thread_size": "M10 x 1.5",
                "thread_depth": 25,
            },
            "exceed depth",
        ),
        ({"form": "simple", "diameter": 5, "depth": 5, "positions": []}, "1-100"),
        (
            {"form": "simple", "diameter": 5, "depth": 5, "positions": [[0, 0, math.nan]]},
            "finite",
        ),
    ],
)
def test_invalid_requests_are_rejected_before_native_work(rig, params, message):
    with pytest.raises(NXToolError, match=message) as error:
        rig.e.execute("nx_hole_feature", {**BASE, **params})
    assert error.value.code == "NX_INVALID_ARGUMENT"
    outcome = error.value.details.get("mutation_outcome", "not_started")
    assert outcome in {"rolled_back", "not_started"}


@pytest.mark.asyncio
async def test_hole_feature_schema_is_public_and_typed(tmp_path: Path):
    server = create_server(object(), Workspace(tmp_path))
    async with create_connected_server_and_client_session(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    schema = tools["nx_hole_feature"].inputSchema
    assert schema["properties"]["form"]["enum"] == [
        "simple",
        "counterbore",
        "countersink",
        "threaded",
    ]
    assert schema["required"] == ["face", "positions", "form", "diameter"]
    assert schema["additionalProperties"] is False
    assert MANIFEST["tools"]["nx_hole_feature"]["status"] in {"experimental", "tested"}
