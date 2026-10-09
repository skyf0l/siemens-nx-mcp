"""Convert, sheet metal from solid, flat solid, lightening cutout and solid punch of
nx_create_sheet_metal_feature on the shared fake, through the real kernel."""

import pytest

from tests.kit import run
from tests.unit.sheet_metal.fakes import FLAT, sm_nx

TOOL = "nx_create_sheet_metal_feature"


def create(nx, operation, **parameters):
    return run(nx, TOOL, operation=operation, parameters=parameters)


def to_sheet_metal(nx, body, t=2.0, bends=1):
    """A commit hook that turns the solid ``body`` into sheet metal ``t`` thick."""

    def hook(b):
        body.sheet_metal, body.thickness, body.bends = True, t, []
        for _ in range(bends):
            nx.add_bend(body, radius=3.0)
        body.volume -= 171.68
        return nx.made("SB_CONVERT_TO_SHEETMETAL", body)

    return hook


def test_convert_makes_the_solid_sheet_metal_from_its_base_face():
    nx = sm_nx()
    solid = nx.block(80, 50, 2)
    solid.volume = 8000.0
    base = solid.face((0, 0, -1))
    nx.on_commit("ConvertToSheetmetalFeatureBuilder", to_sheet_metal(nx, solid))
    ok = create(nx, "convert", face=base.ref)
    assert ok.ok, ok
    b = nx.last("ConvertToSheetmetalFeatureBuilder")
    assert b.BaseFace is base
    assert ok.data["thickness"] == 2 and ok.data["bend_count"] == 1
    assert ok.data["body"]["id"] == solid.ref


def test_convert_is_read_back_and_refuses_sheet_metal_input():
    nx = sm_nx()
    solid = nx.block(80, 50, 2)
    nx.on_commit("ConvertToSheetmetalFeatureBuilder", lambda b: nx.made("SB_CONVERT", solid))
    bad = create(nx, "convert", face=solid.face((0, 0, -1)).ref)
    assert bad.error["details"]["check"] == "sheet_metal" and bad.mutation_outcome == "rolled_back"
    sheet = nx.tab()
    bad = create(nx, "convert", face=sheet.face((0, 0, 1)).ref)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/parameters/face"
    assert bad.mutation_outcome == "not_started"


def from_solid_setup(nx, bends=1, t=2.0, radius=3.0):
    solid = nx.block(80, 50, 30)
    top, side = solid.face((0, 0, 1)), solid.face((0, -1, 0))
    shared = solid.edges[0]
    made = {}

    def hook(b):
        sheet = nx.tab(80, 50, t)
        sheet.bends = []
        for _ in range(bends):
            nx.add_bend(sheet, radius=radius)
        made["body"] = sheet
        return nx.made("SB_SheetMetalFromSolid", sheet)

    nx.on_commit("SheetMetalFromSolidBuilder", hook)
    return solid, top, side, shared, made


def test_from_solid_thickens_web_faces_with_bends_on_the_shared_edges():
    nx = sm_nx()
    _solid, top, side, shared, made = from_solid_setup(nx, radius=4.0)
    ok = create(nx, "from_solid", faces=[top.ref, side.ref], edges=[shared.ref], thickness=2,
                radius=4, reverse=True)  # fmt: skip
    assert ok.ok, ok
    b = nx.last("SheetMetalFromSolidBuilder")
    assert b.WebFaces.ReplaceRules.calls[0][0][0] == [("FaceDumb", [top, side])]
    assert b.BendEdges.ReplaceRules.calls[0][0][0] == [("EdgeDumb", [shared])]
    assert b.UseGlobalThickness is False and b.Thickness.RightHandSide == "2.0"
    assert b.ReverseDirection is True and b.BendOptions.BendRadius.RightHandSide == "4.0"
    assert ok.data["body"]["id"] == made["body"].ref and ok.data["bend_count"] == 1


def test_from_solid_takes_nx_bend_edges_by_default_and_is_read_back():
    # The automatic edges NX proposes are used when none are given.
    nx2 = sm_nx()
    _s, top2, side2, shared2, _m = from_solid_setup(nx2)
    original = nx2.builder

    def builder(kind, *args):
        b = original(kind, *args)
        if kind == "SheetMetalFromSolidBuilder":
            object.__setattr__(b, "GetAutomaticBendEdges", lambda: [shared2])
        return b

    nx2.builder = builder
    ok = create(nx2, "from_solid", faces=[top2.ref, side2.ref], thickness=2)
    assert ok.ok, ok
    assert nx2.last("SheetMetalFromSolidBuilder").BendEdges.ReplaceRules.calls[0][0][0] == [
        ("EdgeDumb", [shared2])
    ]
    # A sheet of the wrong thickness or bend count is not read back as done.
    nx3 = sm_nx()
    _s, top3, side3, shared3, _m = from_solid_setup(nx3, bends=2)
    bad = create(nx3, "from_solid", faces=[top3.ref, side3.ref], edges=[shared3.ref], thickness=2)
    assert bad.error["details"]["check"] == "bend_count" and bad.mutation_outcome == "rolled_back"
    nx4 = sm_nx()
    _s, top4, side4, shared4, _m = from_solid_setup(nx4, t=3.0)
    bad = create(nx4, "from_solid", faces=[top4.ref, side4.ref], edges=[shared4.ref], thickness=2)
    assert bad.error["details"]["check"] == "thickness"


def test_from_solid_inputs_are_checked_before_nx():
    nx = sm_nx()
    sheet = nx.tab()
    bad = create(nx, "from_solid", faces=[sheet.face((0, 0, 1)).ref], thickness=2)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/parameters/faces"
    solid, other = nx.block(10, 10, 10), nx.block(5, 5, 5, origin=(20, 0, 0))
    bad = create(nx, "from_solid", faces=[solid.faces[0].ref], edges=[other.edges[0].ref],
                 thickness=2)  # fmt: skip
    assert bad.error["code"] == "NX_WRONG_OWNER" and bad.error["field"] == "/parameters/edges"
    bad = create(nx, "from_solid", faces=[solid.faces[0].ref], thickness=2, style="open")
    assert bad.error["field"] == "/parameters/style" and nx.builders == []


def flat_solid_setup(nx, thickness=2.0):
    body = nx.tab()
    nx.add_bend(body)
    up = body.face((0, 0, 1))
    edge = next(e for e in body.edges if all(v.Z == 2 and v.Y == 0 for v in e.GetVertices()))
    up.GetEdges = lambda: [edge]

    def hook(b):
        flat = nx.block(100, 97.749115, thickness, origin=(0, 0, 0))
        flat.sheet_metal, flat.thickness, flat.bends = True, thickness, []
        nx.add_bend(flat, state=FLAT)
        flat.volume = 100 * 97.749115 * thickness
        return nx.made("SB_FLAT_SOLID", flat)

    nx.on_commit("FlatSolidFeatureBuilder", hook)
    return body, up, edge


def test_flat_solid_adds_a_flat_body_of_the_sheet_thickness():
    nx = sm_nx()
    body, up, edge = flat_solid_setup(nx)
    ok = create(nx, "flat_solid", face=up.ref, edge=edge.ref)
    assert ok.ok, ok
    b = nx.last("FlatSolidFeatureBuilder")
    assert b.StationaryFace.Value is up and b.XAxisEdge.Value is edge
    assert ok.data["body"]["id"] != body.ref and ok.data["thickness"] == 2
    assert ok.data["flat_extents"] == pytest.approx([100, 97.749115])
    assert ok.data["bends"][0]["state"] == "flat"


def test_flat_solid_is_read_back():
    nx = sm_nx()
    _body, up, edge = flat_solid_setup(nx, thickness=3.0)
    bad = create(nx, "flat_solid", face=up.ref, edge=edge.ref)
    assert bad.error["details"]["check"] == "thickness" and bad.mutation_outcome == "rolled_back"


def test_lightening_cutout_cuts_a_user_profile_with_a_formed_flange():
    nx = sm_nx()
    body = nx.tab()
    sk = nx.sketch()

    def cut(b):
        body.volume -= 500
        return nx.made("LIGHTENING_CUTOUT", body)

    nx.on_commit("LighteningCutoutBuilder", cut)
    ok = create(nx, "lightening_cutout", section=sk.ref, length=4, angle=45, radius=2)
    assert ok.ok, ok
    b = nx.last("LighteningCutoutBuilder")
    assert b.Type == "LighteningCutoutBuilderCutoutType.UserDefined"
    assert b.Length.RightHandSide == "4.0" and b.Angle.RightHandSide == "45.0"
    assert b.DieRadius.RightHandSide == "2.0" and b.ReverseBendDirection is False
    assert ok.data["volume"] == 15500


def test_solid_punch_forms_the_sheet_with_a_solid_tool():
    nx = sm_nx()
    sheet = nx.tab()
    tool = nx.block(20, 20, 6, origin=(40, 30, -6))
    target = sheet.face((0, 0, 1))

    def punch(b):
        sheet.volume += 829.38
        return nx.made("SOLID PUNCH", sheet)

    nx.on_commit("SolidPunchBuilder", punch)
    ok = create(nx, "solid_punch", face=target.ref, tool=tool.ref)
    assert ok.ok, ok
    b = nx.last("SolidPunchBuilder")
    assert b.TargetFace.Value is target and b.ToolBody.Value is tool
    assert ok.data["volume"] == pytest.approx(16829.38) and ok.data["bend_count"] == 0
    # The tool is a plain solid; a punch that changes nothing is not done.
    nx = sm_nx()
    sheet = nx.tab()
    tool = nx.block(20, 20, 6, origin=(40, 30, -6))
    target = sheet.face((0, 0, 1))
    bad = create(nx, "solid_punch", face=target.ref, tool=sheet.ref)
    assert bad.error["field"] == "/parameters/tool" and bad.mutation_outcome == "not_started"
    nx.on_commit("SolidPunchBuilder", lambda b: nx.made("SOLID PUNCH", sheet))
    bad = create(nx, "solid_punch", face=target.ref, tool=tool.ref)
    assert bad.error["details"]["check"] == "volume"
