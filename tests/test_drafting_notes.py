"""Drawing notes, center marks, centerlines, surface finish and weld symbols: local contract tests.

Native behaviour is validated live on NX (acceptance/live/drafting_notes.json);
these tests cover pure helpers, argument validation before any NXOpen work and
the public schemas.
"""

import math
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from nx_mcp.agent_guidance import MANIFEST
from nx_mcp.drafting_notes import (
    close2,
    distinct_ids,
    note_lines,
    number_text,
    positive,
    ra_text,
    sheet_point,
)
from nx_mcp.drafting_notes_server import SUMMARIES
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace

NEW_TOOLS = {
    "nx_add_note",
    "nx_add_center_mark",
    "nx_add_centerline",
    "nx_add_surface_finish",
    "nx_add_weld_symbol",
}


def test_note_lines_and_sheet_points():
    assert note_lines(["GENERAL NOTES", "", "1. BREAK SHARP EDGES"])[2] == "1. BREAK SHARP EDGES"
    for bad in ([], [""], ["  "], "text", ["a\nb"], [1], ["x"] * 51, ["x" * 1025]):
        with pytest.raises(NXToolError):
            note_lines(bad)
    assert sheet_point([10, 20.5]) == [10.0, 20.5]
    for bad in ([1], [1, 2, 3], [1, math.nan], [True, 1], "12"):
        with pytest.raises(NXToolError, match="origin"):
            sheet_point(bad)


def test_callout_text_and_numbers():
    assert ra_text(3.2) == "Ra 3.2" and ra_text(0.8) == "Ra 0.8" and ra_text(25) == "Ra 25"
    assert number_text(5.0) == "5" and number_text(1 / 3) == "0.333333"
    assert positive(2, "size") == 2.0
    for bad in (0, -1, math.inf, True, "3"):
        with pytest.raises(NXToolError):
            positive(bad, "size")
    with pytest.raises(NXToolError, match="at most 1000"):
        positive(1001, "roughness", 1000)
    assert close2([100.0, 50.0], [100.00001, 50.0]) and not close2([100.0, 50.0], [101.0, 50.0])


def test_distinct_ids():
    assert distinct_ids(["obj_a", "obj_b"], 2, 2, "objects") == ["obj_a", "obj_b"]
    for bad in ([], ["a", "a"], ["a", ""], ["a", "b", "c"], "a"):
        with pytest.raises(NXToolError):
            distinct_ids(bad, 1, 2, "objects")


@pytest.mark.parametrize(
    ("tool", "params", "message"),
    [
        ("nx_add_note", {"text": [], "origin": [10, 10]}, "1-50 lines"),
        ("nx_add_note", {"text": ["  "], "origin": [10, 10]}, "non-blank"),
        ("nx_add_note", {"text": ["A"], "origin": [10]}, "origin"),
        ("nx_add_note", {"text": ["A"], "origin": [10, 10], "leader_to": "obj_e"}, "needs view"),
        ("nx_add_note", {"text": ["A"], "origin": [10, 10], "view": "obj_v"}, "only used with"),
        ("nx_add_center_mark", {"view": "obj_v", "edges": []}, "1-50"),
        ("nx_add_center_mark", {"view": "obj_v", "edges": ["obj_e", "obj_e"]}, "distinct"),
        ("nx_add_centerline", {"view": "obj_v", "objects": ["a", "b"], "kind": "3d"}, "list 1"),
        ("nx_add_centerline", {"view": "obj_v", "objects": ["a"], "kind": "2d"}, "list 2"),
        ("nx_add_centerline", {"view": "obj_v", "objects": ["a"], "kind": "4d"}, "kind"),
        (
            "nx_add_surface_finish",
            {"view": "obj_v", "object": "obj_f", "roughness": 0, "origin": [1, 2]},
            "roughness",
        ),
        (
            "nx_add_surface_finish",
            {
                "view": "obj_v",
                "object": "obj_f",
                "roughness": 3.2,
                "origin": [1, 2],
                "symbol": "polished",
            },
            "symbol",
        ),
        (
            "nx_add_weld_symbol",
            {"view": "obj_v", "object": "obj_e", "weld": "plug", "size": 5, "origin": [1, 2]},
            "weld",
        ),
        (
            "nx_add_weld_symbol",
            {"view": "obj_v", "object": "obj_e", "weld": "fillet", "size": -5, "origin": [1, 2]},
            "size",
        ),
        (
            "nx_add_weld_symbol",
            {
                "view": "obj_v",
                "object": "obj_e",
                "weld": "fillet",
                "size": 5,
                "origin": [1, 2],
                "side": "top",
            },
            "side",
        ),
    ],
)
def test_invalid_requests_are_rejected_before_native_work(rig, tool, params, message):
    with pytest.raises(NXToolError, match=message) as error:
        rig.e.execute(tool, params)
    assert error.value.code == "NX_INVALID_ARGUMENT"
    outcome = error.value.details.get("mutation_outcome", "not_started")
    assert outcome in {"rolled_back", "not_started"}


def test_panel_summaries_are_readable():
    class C:
        def obj(self, value):
            return f'"{value}"'

        def count(self, values, noun):
            return f"{len(values)} {noun}s"

    assert SUMMARIES["nx_add_note"]({"text": ["NOTE", "two"]}, C()).startswith(
        'Add drawing note "NOTE" (+1 lines)'
    )
    assert "Ra 3.2" in SUMMARIES["nx_add_surface_finish"]({"roughness": 3.2, "object": "f"}, C())


@pytest.mark.asyncio
async def test_new_tools_are_public_typed_and_honestly_classified(tmp_path: Path):
    server = create_server(object(), Workspace(tmp_path))
    async with create_connected_server_and_client_session(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) >= NEW_TOOLS
    note = tools["nx_add_note"].inputSchema
    assert note["required"] == ["text", "origin"]
    assert note["additionalProperties"] is False and "operation_id" in note["properties"]
    centerline = tools["nx_add_centerline"].inputSchema["properties"]["kind"]
    assert centerline["enum"] == ["2d", "3d"]
    finish = tools["nx_add_surface_finish"].inputSchema
    assert finish["properties"]["symbol"]["enum"] == [
        "basic",
        "material_removal_required",
        "material_removal_prohibited",
    ]
    weld = tools["nx_add_weld_symbol"].inputSchema
    assert weld["properties"]["weld"]["enum"] == ["fillet", "square_butt", "v_butt"]
    assert weld["required"] == ["view", "object", "weld", "size", "origin"]
    for name in NEW_TOOLS:
        assert MANIFEST["tools"][name]["status"] in {"experimental", "tested"}


def test_view_geometry_helpers():
    from nx_mcp.drafting_notes import enum_name, line_distance, parallel2, uv_samples

    assert line_distance([128, 190], [120, 190], [80, 0]) == pytest.approx(0)
    assert line_distance([120, 100], [120, 90], [0, -100]) == pytest.approx(0)
    assert line_distance([125, 90], [120, 90], [0, -100]) == pytest.approx(5)
    assert parallel2([80, 0], [-72, 0]) and not parallel2([1, 0], [1, 1])
    samples = uv_samples()
    assert samples[0] == (0.5, 0.5) and len(samples) == 49
    assert all(0 < u < 1 and 0 < v < 1 for u, v in samples)

    class Kinds:
        Fillet = 13
        VButt = 4

    assert enum_name(Kinds, 13, ["VButt", "Fillet"]) == "Fillet"
    assert enum_name(Kinds, 0, ["Fillet", "None"]) == "None"
    assert enum_name(Kinds, 7, ["Fillet"]) == "7"


def test_annotation_inventory_includes_drafting_symbols(rig):
    from types import SimpleNamespace as NS

    marks = [NS(Tag=101), NS(Tag=102)]
    welds = [NS(Tag=201)]
    part = NS(
        Notes=[NS(Tag=1)],
        Labels=[],
        Annotations=NS(Centerlines=marks, DraftingSurfaceFinishSymbol=[NS(Tag=301)], Welds=welds),
    )
    tags = {int(a.Tag) for a in rig.e._documentation_annotations(part)}
    assert tags == {1, 101, 102, 201, 301}
