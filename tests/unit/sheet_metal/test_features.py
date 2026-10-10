"""nx_create_sheet_metal_feature on the shared fake, through the real kernel."""

import pytest

from nx_mcp.core import catalogue, summary
from tests.kit import run
from tests.kit.objects import Object, point
from tests.unit.sheet_metal.fakes import FLAT, sm_nx

TOOL = "nx_create_sheet_metal_feature"


def create(nx, operation, **parameters):
    return run(nx, TOOL, operation=operation, parameters=parameters)


def test_tab_sets_section_and_thickness_and_reads_the_thickness_back():
    nx = sm_nx()
    sk = nx.sketch()
    nx.on_commit("TabFeatureBuilder", lambda b: nx.made("SB_TAB", nx.tab(100, 80, 2)))
    ok = create(nx, "tab", section=sk.ref, thickness="2 mm")
    assert ok.ok, ok
    b = nx.last("TabFeatureBuilder")
    assert b.Thickness.RightHandSide == "2.0"
    assert b.SetApplicationContext.calls[0][0][0] == "ApplicationContext.NxSheetMetal"
    assert ok.data["thickness"] == 2 and ok.data["volume"] == 16000 and ok.data["bend_count"] == 0
    assert ok.mutation_outcome == "committed" and nx.live_builders == 0
    # NX built a 3 mm sheet for a 2 mm request: verification_failed, rolled back.
    nx.on_commit("TabFeatureBuilder", lambda b: nx.made("SB_TAB", nx.tab(100, 80, 3)))
    bad = create(nx, "tab", section=sk.ref, thickness=2)
    assert bad.error["code"] == "NX_VERIFICATION_FAILED"
    assert bad.error["details"]["check"] == "thickness" and bad.mutation_outcome == "rolled_back"
    assert nx.live_builders == 0


def test_inch_part_converts_thickness_to_inches():
    nx = sm_nx(units="in")
    sk = nx.sketch()
    nx.on_commit("TabFeatureBuilder", lambda b: nx.made("SB_TAB", nx.tab(4, 3, 0.1)))
    ok = create(nx, "tab", section=sk.ref, thickness="2.54 mm")
    assert ok.ok, ok
    assert nx.last("TabFeatureBuilder").Thickness.RightHandSide == "0.1"
    assert ok.units["length"] == "in"


def test_sheet_metal_features_need_the_sheet_metal_application():
    nx = sm_nx(application="modeling")
    sk = nx.sketch()
    bad = create(nx, "tab", section=sk.ref, thickness=2)
    assert bad.error["code"] == "NX_WRONG_APPLICATION"
    assert "nx_activate_application" in bad.error["hint"]
    assert bad.mutation_outcome == "not_started" and nx.builders == []


@pytest.mark.parametrize(
    ("operation", "extra", "field"),
    [
        ("tab", {"thickness": 2, "edges": "EDGES"}, "/parameters/edges"),
        ("tab", {}, "/parameters/thickness"),
        ("tab", {"thickness": -1}, "/parameters/thickness"),
        ("flange", {"edges": "EDGES"}, "/parameters/length"),
        ("flange", {"edges": "EDGES", "length": 20, "style": "open"}, "/parameters/style"),
        ("flange", {"edges": "EDGES", "length": 20, "angle": 180}, "/parameters/angle"),
        ("hem", {"edges": "EDGES", "length": 5, "style": "fillet"}, "/parameters/style"),
        ("break_corner", {"edges": "EDGES", "radius": 0}, "/parameters/radius"),
        (
            "resize_bend_angle",
            {"faces": "FACES2", "face": "FACE", "angle": 80},
            "/parameters/faces",
        ),
        ("flat_pattern", {"face": "FACE"}, "/parameters/edge"),
    ],
)
def test_parameters_are_checked_per_operation_before_nx(operation, extra, field):
    nx = sm_nx()
    body = nx.tab()
    sk = nx.sketch()
    names = {
        "EDGES": [body.edges[0].ref],
        "FACES2": [body.faces[0].ref, body.faces[1].ref],
        "FACE": body.faces[0].ref,
    }
    params = {k: names.get(v, v) if isinstance(v, str) else v for k, v in extra.items()}
    if operation == "tab":
        params["section"] = sk.ref
    bad = create(nx, operation, **params)
    assert bad.error["category"] == "invalid_argument", bad
    assert bad.error["field"] == field
    assert bad.mutation_outcome == "not_started" and nx.builders == []


def flanged(nx, body, angle=90.0, radius=3.0, grow=3656.637):
    def hook(b):
        nx.add_bend(body, radius=radius, angle=angle)
        body.volume += grow
        return nx.made("SB_FLANGE", body)

    return hook


def test_flange_is_one_inside_length_entry_with_bend_options_and_is_read_back():
    nx = sm_nx()
    body = nx.tab()
    edge = body.edges[0]
    nx.on_commit("MultiFlangeBuilder", flanged(nx, body, radius=4.0))
    ok = create(nx, "flange", edges=[edge.ref], length=20, radius="4 mm", neutral_factor=0.4)
    assert ok.ok, ok
    b = nx.last("MultiFlangeBuilder")
    entry = b.FlangePropertiesList.FeatureBendPropertiesList.Append.calls[0][0][0]
    assert entry.LengthReference == "FlangeBendPropertiesBuilderLengthReferences.Inside"
    assert entry.Length.RightHandSide == "20.0" and entry.Angle.RightHandSide == "90.0"
    assert entry.Edges.ReplaceRules.calls[0][0][0] == [("EdgeDumb", [edge])]
    assert entry.BendOptions.UseGlobalBendRadius is False
    assert entry.BendOptions.BendRadius.RightHandSide == "4.0"
    assert entry.BendOptions.NeutralFactor.RightHandSide == "0.4"
    assert ok.data["bend_count"] == 1 and ok.data["bends"][0]["angle"] == 90
    assert ok.data["volume"] == pytest.approx(16000 + 3656.637)


@pytest.mark.parametrize(
    ("hook_args", "check"),
    [({"angle": 80.0}, "angle"), ({"radius": 2.0}, "radius"), ({"grow": 0.0}, "volume")],
)
def test_flange_read_back_mismatch_rolls_back(hook_args, check):
    nx = sm_nx()
    body = nx.tab()
    nx.on_commit("MultiFlangeBuilder", flanged(nx, body, **hook_args))
    bad = create(nx, "flange", edges=[body.edges[0].ref], length=20, radius=3)
    assert bad.error["code"] == "NX_VERIFICATION_FAILED", bad
    assert bad.error["details"]["check"] == check and bad.mutation_outcome == "rolled_back"


def test_flange_that_adds_no_bend_fails_the_bend_count():
    nx = sm_nx()
    body = nx.tab()
    nx.on_commit("MultiFlangeBuilder", lambda b: nx.made("SB_FLANGE", body))
    bad = create(nx, "flange", edges=[body.edges[0].ref], length=20)
    assert bad.error["details"]["check"] == "bend_count"


def test_secondary_tab_uses_the_target_body_and_its_thickness():
    nx = sm_nx()
    body = nx.tab()
    sk = nx.sketch()
    bad = create(nx, "tab", section=sk.ref, body=body.ref, thickness=3)
    assert bad.error["field"] == "/parameters/thickness" and bad.mutation_outcome == "not_started"

    def grow(b):
        body.volume += 400
        return nx.made("SB_TAB", body)

    nx.on_commit("TabFeatureBuilder", grow)
    ok = create(nx, "tab", section=sk.ref, body=body.ref)
    assert ok.ok, ok
    b = nx.last("TabFeatureBuilder")
    assert b.IsSecondary is True and b.TargetBody is body
    assert ok.data["volume"] == 16400


def flat_setup(nx):
    body = nx.tab()
    up = body.face((0, 0, 1))

    def along_x_on_top(e):
        a, b = e.GetVertices()
        return a.Z == b.Z == 2 and a.Y == b.Y == 0

    edge = next(e for e in body.edges if along_x_on_top(e))
    up.GetEdges = lambda: [edge]
    return body, up, edge


class Line(Object):
    def __init__(self, start, end):
        super().__init__("LINE")
        self.StartPoint, self.EndPoint = point(*start), point(*end)


OUTLINE = [((0, 0, 2), (100, 0, 2)), ((100, 0, 2), (100, 97.749115, 2)),
           ((100, 97.749115, 2), (0, 97.749115, 2)), ((0, 97.749115, 2), (0, 0, 2)),
           ((0, 77.874558, 2), (100, 77.874558, 2))]  # fmt: skip


def test_flat_pattern_leaves_a_shown_drawing_for_the_commit_and_reopens_it():
    nx = sm_nx()
    body, up, edge = flat_setup(nx)
    sheet = nx.sheet("SHT1")
    sheet.Open()
    session = nx.session
    session.ApplicationName, session.Parts.Display = "UG_APP_SBSM", nx.part
    seen = {}

    def commit(b):
        seen["current"] = nx.part.DrawingSheets.CurrentDrawingSheet
        session.ApplicationName = "UG_APP_MODELING"  # should NX leave Sheet Metal
        b.FlatPatternViewName = "FLAT-PATTERN#1"
        feature = nx.made("FLAT_PATTERN", body, feature_type="FLAT_PATTERN")
        feature.GetCurves = lambda: [Line(a, b) for a, b in OUTLINE]
        return feature

    nx.on_commit("FlatPatternBuilder", commit)
    ok = create(nx, "flat_pattern", face=up.ref, edge=edge.ref)
    assert ok.ok, ok
    assert seen["current"] is None  # NX 2406 refuses the commit while a sheet is shown
    assert nx.part.DrawingSheets.CurrentDrawingSheet is sheet
    assert session.ApplicationName == "UG_APP_SBSM"  # re-entered before the sheet reopened
    b = nx.last("FlatPatternBuilder")
    assert b.UpwardFace.Value is up and b.XAxisEdge.Value is edge
    assert ok.data["view"] == "FLAT-PATTERN#1" and ok.data["body"]["id"] == body.ref
    assert ok.data["flat_extents"] == pytest.approx([100, 97.749115])


def test_flat_pattern_orientation_is_checked_before_nx():
    nx = sm_nx()
    body, up, edge = flat_setup(nx)
    other = body.edges[1]
    bad = create(nx, "flat_pattern", face=up.ref, edge=other.ref)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/parameters/edge"
    assert "nx_list_objects" in bad.error["hint"] and nx.builders == []
    edge.SolidEdgeType = nx.nx.Edge.EdgeType.Circular
    bad = create(nx, "flat_pattern", face=up.ref, edge=edge.ref)
    assert bad.error["field"] == "/parameters/edge"
    side = nx.face(normal=(1, 0, 0), kind="cylinder", body=body)
    bad = create(nx, "flat_pattern", face=side.ref, edge=edge.ref)
    assert bad.error["field"] == "/parameters/face"
    plain = nx.block(10, 10, 10)
    bad = create(nx, "flat_pattern", face=plain.faces[0].ref, edge=plain.edges[0].ref)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.mutation_outcome == "not_started"


def test_unbend_flattens_the_bends_and_is_read_back():
    nx = sm_nx()
    body = nx.tab()
    bend = nx.add_bend(body)
    web = body.face((0, 0, 1))

    def flatten(b):
        bend.bend.state = FLAT
        return nx.made("SB_UNBEND", body)

    nx.on_commit("UnbendFeatureBuilder", flatten)
    ok = create(nx, "unbend", faces=[bend.ref], face=web.ref)
    assert ok.ok, ok
    assert ok.data["bends"][0]["state"] == "flat"
    b = nx.last("UnbendFeatureBuilder")
    assert b.FaceCollector.rules == [("FaceDumb", [bend])] and b.ReferenceEntity is web
    # A rebend that leaves the bend flat is not what was asked.
    nx.on_commit("RebendFeatureBuilder", lambda b: nx.made("SB_REBEND", body))
    # Face references went stale with the commit: reacquire them.
    faces, face = [nx.ref_of(bend, "face")], nx.ref_of(web, "face")
    bad = create(nx, "rebend", faces=faces, face=face)
    assert bad.error["details"]["check"] == "rebend" and bad.mutation_outcome == "rolled_back"


def test_bend_operations_need_inner_bend_faces():
    nx = sm_nx()
    body = nx.tab()
    nx.add_bend(body)
    web = body.face((0, 0, 1))
    bad = create(nx, "unbend", faces=[web.ref], face=web.ref)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/parameters/faces/0"
    bend = body.bends[0]
    bad = create(nx, "unbend", faces=[bend.ref], face=bend.ref)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/parameters/face"
    assert bad.mutation_outcome == "not_started" and nx.builders == []


def test_resize_neutral_factor_sets_the_value_and_reads_it_back():
    nx = sm_nx()
    body = nx.tab()
    bend = nx.add_bend(body, nf=0.33)

    def resize(b):
        bend.bend.nf = 0.4
        return nx.made("SB_RESIZE_NF", body)

    nx.on_commit("ResizeNeutralFactorBuilder", resize)
    ok = create(nx, "resize_neutral_factor", faces=[bend.ref], neutral_factor=0.4)
    assert ok.ok, ok
    b = nx.last("ResizeNeutralFactorBuilder")
    assert b.NeutralFactor.RightHandSide == "0.4" and b.UseGlobal is False
    assert ok.data["bends"][0]["neutral_factor"] == 0.4


def test_resize_bend_angle_and_radius_are_read_back():
    nx = sm_nx()
    body = nx.tab()
    bend = nx.add_bend(body)
    web = body.face((0, 0, 1))

    def angle(b):
        bend.bend.angle = 110.0
        return nx.made("SB_RESIZE_ANGLE", body)

    nx.on_commit("ResizeBendAngleBuilder", angle)
    ok = create(nx, "resize_bend_angle", faces=[bend.ref], face=web.ref, angle=110)
    assert ok.ok, ok
    b = nx.last("ResizeBendAngleBuilder")
    assert b.BendFace.Value is bend and b.ReferenceFace.Value is web
    assert b.Angle.RightHandSide == "70.0" and b.KeepRadiusFixed is True  # included angle
    nx.on_commit("ResizeBendRadiusFeatureBuilder", lambda b: nx.made("SB_R", body))
    faces, face = [nx.ref_of(bend, "face")], nx.ref_of(web, "face")
    bad = create(nx, "resize_bend_radius", faces=faces, face=face, radius=5)
    assert bad.error["details"]["check"] == "resize_bend_radius"


def test_normal_cutout_cuts_through_all_and_must_remove_material():
    nx = sm_nx()
    body = nx.tab()
    sk = nx.sketch()

    def cut(b):
        body.volume -= 100
        return nx.made("SB_CUTOUT", body)

    nx.on_commit("NormalCutoutFeatureBuilder", cut)
    ok = create(nx, "normal_cutout", section=sk.ref, body=body.ref)
    assert ok.ok, ok
    b = nx.last("NormalCutoutFeatureBuilder")
    assert b.DepthType == "NormalCutoutBuilderDepthTypeOptions.ThroughAll"
    assert b.TargetBody is body and ok.data["volume"] == 15900
    nx.on_commit("NormalCutoutFeatureBuilder", lambda b: nx.made("SB_CUTOUT", body))
    bad = create(nx, "normal_cutout", section=sk.ref, body=body.ref)
    assert bad.error["details"]["check"] == "volume"


def test_a_form_finds_its_body_through_the_feature():
    nx = sm_nx()
    body = nx.tab()
    sk = nx.sketch()

    def dimple(b):
        body.volume += 12
        return nx.made("DIMPLE", body)

    nx.on_commit("DimpleFeatureBuilder", dimple)
    ok = create(nx, "dimple", section=sk.ref, height=3, reverse=True)
    assert ok.ok, ok
    b = nx.last("DimpleFeatureBuilder")
    assert b.DepthType == "DimpleBuilderDepthTypeOptions.SectionReverseNormalSide"
    assert ok.data["body"]["id"] == body.ref


@pytest.mark.parametrize(
    ("code", "expected"), [(948801, "NX_LICENSE_UNAVAILABLE"), (670015, "NX_BUILD_FAILED")]
)
def test_native_refusals_roll_back_and_destroy_the_builder(code, expected):
    nx = sm_nx()
    sk = nx.sketch()
    nx.refuse("TabFeatureBuilder", code)
    bad = create(nx, "tab", section=sk.ref, thickness=2)
    assert bad.error["code"] == expected and bad.error["nx_code"] == code
    assert nx.live_builders == 0 and bad.mutation_outcome in ("rolled_back", "not_started")


def test_invalid_builder_is_not_committed():
    nx = sm_nx()
    sk = nx.sketch()
    nx.on_commit("TabFeatureBuilder", lambda b: pytest.fail("committed"))
    original = nx.builder

    def invalid(kind, *args):
        b = original(kind, *args)
        object.__setattr__(b, "valid", False)
        return b

    nx.builder = invalid
    bad = create(nx, "tab", section=sk.ref, thickness=2)
    assert bad.error["code"] == "NX_BUILD_FAILED" and nx.live_builders == 0


EXAMPLES = {
    "tab": {"section": "s:1", "thickness": 2},
    "flange": {"edges": ["e:1"], "length": 20, "angle": 90},
    "contour_flange": {"section": "s:1", "thickness": 2, "length": 40},
    "bend": {"section": "s:1", "face": "fc:1", "angle": 90},
    "jog": {"section": "s:1", "face": "fc:1", "height": 10},
    "hem": {"edges": ["e:1"], "length": 10, "style": "closed"},
    "normal_cutout": {"section": "s:1", "body": "b:1"},
    "dimple": {"section": "s:1", "height": 3},
    "louver": {"section": "s:1", "height": 3, "width": 10},
    "drawn_cutout": {"section": "s:1", "height": 4},
    "break_corner": {"edges": ["e:1"], "radius": 3},
    "resize_bend_angle": {"faces": ["fc:1"], "face": "fc:2", "angle": 110},
    "resize_bend_radius": {"faces": ["fc:1"], "face": "fc:2", "radius": 5},
    "resize_neutral_factor": {"faces": ["fc:1"], "neutral_factor": 0.4},
    "unbend": {"faces": ["fc:1"], "face": "fc:2"},
    "rebend": {"faces": ["fc:1"], "face": "fc:2"},
    "flat_pattern": {"face": "fc:1", "edge": "e:1"},
    "lofted_flange": {"section": "s:1", "end_section": "s:2", "thickness": 2},
    "advanced_flange": {"edges": ["e:1"], "length": 20, "angle": 90},
    "variational_flange": {"edges": ["e:1"], "length": 20},
    "gusset": {"faces": ["fc:1"], "height": 6, "width": 10},
    "edge_rip": {"section": "s:1", "width": 0.5},
    "closed_corner": {"faces": ["fc:1", "fc:2"]},
    "three_bend_corner": {"faces": ["fc:1", "fc:2"]},
    "bridge_bend": {"edges": ["e:1", "e:2"]},
    "bend_taper": {"faces": ["fc:1"], "face": "fc:2", "length": 5},
    "joggle": {"faces": ["fc:1"], "section": "s:1", "height": 5},
    "bulge_relief": {"edges": ["e:1"], "height": 3},
    "bead": {"section": "s:1", "height": 2, "radius": 4},
    "convert": {"face": "fc:1"},
    "from_solid": {"faces": ["fc:1", "fc:2"], "thickness": 2},
    "flat_solid": {"face": "fc:1", "edge": "e:1"},
    "lightening_cutout": {"section": "s:1", "length": 4},
    "solid_punch": {"face": "fc:1", "tool": "b:1"},
}


def test_every_operation_has_a_panel_line_within_budget():
    spec = catalogue.load()[TOOL]
    assert set(EXAMPLES) == set(spec.variant_values())
    for operation, params in EXAMPLES.items():
        line = summary.render(
            spec, {"operation": operation, "parameters": params}, units={"length": "mm"},
            strict=True,
        )  # fmt: skip
        assert line and line != spec.label and len(line) <= 60, (operation, line)
