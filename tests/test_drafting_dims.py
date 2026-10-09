"""Radial/angular/ordinate drawing dimensions and drawing sheets: local contract tests.

Native behaviour is validated live on NX 2406 (acceptance/live/drafting_dims.json);
these tests cover pure geometry helpers, argument validation and public schemas.
"""

from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.agent_guidance import MANIFEST
from nx_mcp.drafting_dims import (
    acute_angle_deg,
    angle_matches,
    ordinate_snap,
    projected_ordinate,
    sheet_xy,
    template_candidates,
)
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace

NEW_TOOLS = {
    "nx_add_radial_dimension",
    "nx_add_angular_dimension",
    "nx_add_ordinate_dimensions",
    "nx_create_drawing_sheet",
}


def test_acute_angle_and_supplement_matching():
    assert acute_angle_deg([1, 0], [1, 1]) == pytest.approx(45)
    assert acute_angle_deg([1, 0], [-1, 1]) == pytest.approx(45)
    assert acute_angle_deg([0, 2], [3, 0]) == pytest.approx(90)
    assert acute_angle_deg([0, 0], [1, 0]) is None
    assert angle_matches(45.0, 45.0) and angle_matches(135.0, 45.0)
    assert angle_matches(315.0, 45.0) and not angle_matches(60.0, 45.0)


def test_projected_ordinate_and_sheet_point():
    assert projected_ordinate([10, 5], [40, 25], [1, 0]) == pytest.approx(30)
    assert projected_ordinate([10, 5], [40, 25], [0, 1]) == pytest.approx(20)
    assert projected_ordinate([10, 5], [0, 0], [1, 0]) == pytest.approx(-10)
    assert projected_ordinate([0, 0, 0], [20, 20, 20], [0, 0, -1]) == pytest.approx(-20)
    assert sheet_xy(None) is None and sheet_xy([1, 2]) == [1.0, 2.0]
    for bad in ([1], [1, float("nan")], "1,2", [True, 2]):
        with pytest.raises(NXToolError):
            sheet_xy(bad)


def test_ordinate_snap_rules():
    assert ordinate_snap("arc", "auto", "o") == "center"
    assert ordinate_snap("line", "auto", "o") == "start"
    assert ordinate_snap("arc", "end", "o") == "end"
    with pytest.raises(NXToolError, match="circular") as error:
        ordinate_snap("line", "center", "objects[0]")
    assert error.value.code == "NX_OBJECT_TYPE_MISMATCH"


def test_installed_template_names(tmp_path):
    root = tmp_path / "DRAFTING" / "templates"
    assert template_candidates("a3", tmp_path) == [root / "Drawing-A3-Size2D-template.prt"]
    assert template_candidates("Drawing-A1-Size2D-template", tmp_path) == [
        root / "Drawing-A1-Size2D-template.prt"
    ]
    assert template_candidates("x.prt", tmp_path) == [root / "x.prt"]


@pytest.mark.parametrize(
    ("tool", "params", "message"),
    [
        ("nx_add_radial_dimension", {"view": "v", "edge": "e", "kind": "chord"}, "kind"),
        ("nx_add_radial_dimension", {"view": "v", "edge": "e", "origin": [1]}, "origin"),
        ("nx_add_radial_dimension", {"view": "", "edge": "e"}, "view"),
        ("nx_add_angular_dimension", {"view": "v", "first": "a", "second": "a"}, "different"),
        (
            "nx_add_angular_dimension",
            {"view": "v", "first": "a", "second": "b", "origin": [0, float("inf")]},
            "finite",
        ),
        (
            "nx_add_ordinate_dimensions",
            {"view": "v", "origin_object": "o", "objects": []},
            "1-50",
        ),
        (
            "nx_add_ordinate_dimensions",
            {"view": "v", "origin_object": "o", "objects": ["a", "a"]},
            "unique",
        ),
        (
            "nx_add_ordinate_dimensions",
            {"view": "v", "origin_object": "o", "objects": ["o"]},
            "origin_object",
        ),
        (
            "nx_add_ordinate_dimensions",
            {"view": "v", "origin_object": "o", "objects": ["a"], "direction": "diagonal"},
            "direction",
        ),
        (
            "nx_add_ordinate_dimensions",
            {"view": "v", "origin_object": "o", "objects": ["a"], "margin": True},
            "margin",
        ),
        (
            "nx_add_ordinate_dimensions",
            {"view": "v", "origin_object": "o", "objects": ["a"], "object_snap": "mid"},
            "object_snap",
        ),
        ("nx_create_drawing_sheet", {"projection": "second_angle"}, "projection"),
        ("nx_create_drawing_sheet", {"size": "B"}, "size"),
        ("nx_create_drawing_sheet", {"scale": -1}, "scale"),
        ("nx_create_drawing_sheet", {"units": "cm"}, "units"),
        ("nx_create_drawing_sheet", {"name": "  "}, "name"),
        ("nx_create_drawing_sheet", {"template": " "}, "template"),
        ("nx_create_drawing_sheet", {"template": "A3", "units": "in"}, "template"),
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
    server = create_server(object(), Workspace(tmp_path))
    async with create_connected_server_and_client_session(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) >= NEW_TOOLS
    radial = tools["nx_add_radial_dimension"].inputSchema
    assert radial["properties"]["kind"]["enum"] == ["radius", "diameter"]
    assert radial["required"] == ["view", "edge"]
    assert radial["additionalProperties"] is False
    assert tools["nx_add_angular_dimension"].inputSchema["required"] == ["view", "first", "second"]
    ordinate = tools["nx_add_ordinate_dimensions"].inputSchema
    assert ordinate["required"] == ["view", "origin_object", "objects"]
    assert ordinate["properties"]["direction"]["enum"] == ["horizontal", "vertical"]
    assert ordinate["properties"]["objects"]["minItems"] == 1
    assert ordinate["properties"]["objects"]["maxItems"] == 50
    for name in ("nx_add_radial_dimension", "nx_add_angular_dimension"):
        point = tools[name].inputSchema["properties"]["origin"]["anyOf"][0]
        assert point["minItems"] == 2 and point["maxItems"] == 2
    sheet = tools["nx_create_drawing_sheet"].inputSchema
    assert sheet["properties"]["projection"]["enum"] == ["first_angle", "third_angle"]
    assert "required" not in sheet or sheet["required"] == []
    for name in NEW_TOOLS:
        assert MANIFEST["tools"][name]["status"] in {"experimental", "tested"}
