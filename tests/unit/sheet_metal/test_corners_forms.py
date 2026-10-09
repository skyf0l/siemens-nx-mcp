"""Three-bend corners, bridge bends, bend tapers, joggles, bulge reliefs and beads of
nx_create_sheet_metal_feature on the shared fake, through the real kernel."""

from unittest.mock import Mock

from tests.kit import run
from tests.kit.objects import Object, point
from tests.unit.sheet_metal.fakes import sm_nx

TOOL = "nx_create_sheet_metal_feature"


def create(nx, operation, **parameters):
    return run(nx, TOOL, operation=operation, parameters=parameters)


def changes(nx, body, kind, volume=0.0, bends=0):
    def hook(b):
        body.volume += volume
        for _ in range(bends):
            nx.add_bend(body)
        return nx.made(kind, body)

    return hook


class Line(Object):
    def __init__(self, start, end):
        super().__init__("LINE")
        self.StartPoint, self.EndPoint = point(*start), point(*end)


def corner(volume=0.0, bends=0, kind="ThreeBendCornerFeatureBuilder"):
    nx = sm_nx()
    body = nx.tab()
    first, second = nx.add_bend(body), nx.add_bend(body)
    nx.on_commit(kind, changes(nx, body, "SM_3BEND_CORNER", volume, bends))
    return nx, body, first, second


def test_three_bend_corner_pairs_two_bend_faces_and_adds_material():
    nx, _body, first, second = corner(254.6)
    ok = create(nx, "three_bend_corner", faces=[first.ref, second.ref])
    assert ok.ok, ok
    b = nx.last("ThreeBendCornerFeatureBuilder")
    assert b.AddFacePair.calls[0][0] == (first, second)
    assert ok.data["volume"] == 16254.6 and ok.data["bend_count"] == 2


def test_three_bend_corner_read_back_and_inputs_are_checked():
    # A corner that changes nothing, or one that adds a bend, is not read back as done.
    nx, _body, first, second = corner()
    bad = create(nx, "three_bend_corner", faces=[first.ref, second.ref])
    assert bad.error["details"]["check"] == "volume" and bad.mutation_outcome == "rolled_back"
    nx, _body, first, second = corner(9, 1)
    bad = create(nx, "three_bend_corner", faces=[first.ref, second.ref])
    assert bad.error["details"]["check"] == "bend_count"
    # Inputs are checked before NX: two inner bend faces.
    nx, body, first, _second = corner()
    one = create(nx, "three_bend_corner", faces=[first.ref])
    assert one.error["field"] == "/parameters/faces" and one.mutation_outcome == "not_started"
    web = create(nx, "three_bend_corner", faces=[first.ref, body.face((0, 0, 1)).ref])
    assert web.error["code"] == "NX_WRONG_GEOMETRY" and web.error["field"] == "/parameters/faces/1"
    assert nx.live_builders == 0


def test_bridge_bend_joins_two_bodies_with_a_zu_transition():
    nx = sm_nx()
    lower, upper = nx.tab(100, 80, 2), nx.tab(100, 60, 2)
    start, end = lower.edges[0], upper.edges[0]

    def bridge(b):
        lower.volume += upper.volume + 5806.62
        nx.add_bend(lower), nx.add_bend(lower)
        return nx.made("FPC_Bridge_Transition", lower)

    nx.on_commit("BridgeTransitionBuilder", bridge)
    ok = create(nx, "bridge_bend", edges=[start.ref, end.ref])
    assert ok.ok, ok
    b = nx.last("BridgeTransitionBuilder")
    assert b.StartEdge.Value is start and b.EndEdge.Value is end
    assert b.Type == "BridgeTransitionBuilderTypeOptions.Zu"
    assert b.WidthType == "BridgeTransitionBuilderWidthOptions.FullBothEdges"
    assert b.SetApplicationContext.calls[0][0][0] == "ApplicationContext.NxSheetMetal"
    assert abs(ok.data["volume"] - 33806.62) < 1e-6 and ok.data["bend_count"] == 2


def test_bridge_bend_takes_two_edges_of_two_bodies():
    nx = sm_nx()
    lower, upper = nx.tab(), nx.tab()
    one = create(nx, "bridge_bend", edges=[lower.edges[0].ref])
    assert one.error["field"] == "/parameters/edges" and one.mutation_outcome == "not_started"
    same = create(nx, "bridge_bend", edges=[lower.edges[0].ref, lower.edges[1].ref])
    assert same.error["code"] == "NX_WRONG_GEOMETRY" and same.mutation_outcome == "not_started"
    # A bridge that leaves the two sheets apart fails the read-back.
    nx.on_commit("BridgeTransitionBuilder", changes(nx, lower, "FPC_Bridge_Transition", 10, 2))
    bad = create(nx, "bridge_bend", edges=[lower.edges[0].ref, upper.edges[0].ref])
    assert bad.error["details"]["check"] == "volume" and bad.mutation_outcome == "rolled_back"
    assert nx.builders[-1].destroyed


def test_bend_taper_tapers_both_sides_by_distance_from_a_stationary_face():
    nx = sm_nx()
    body = nx.tab()
    bend, web = nx.add_bend(body), body.face((0, 0, 1))
    nx.on_commit("BendTaperBuilder", changes(nx, body, "BEND_TAPER", -403.947))
    ok = create(nx, "bend_taper", faces=[bend.ref], face=web.ref, length=5)
    assert ok.ok, ok
    b = nx.last("BendTaperBuilder")
    assert b.BendTaperSelectBendFace.ReplaceRules.calls[0][0][0] == [("FaceDumb", [bend])]
    assert b.StationaryEntity is web and b.TaperSides == "BendTaperBuilderBendTaperSides.Both"
    assert b.BendTaperInputMethod1 == b.BendTaperInputMethod2
    assert str(b.BendTaperInputMethod1).endswith(".Distance")
    assert b.TaperDistance1.RightHandSide == b.TaperDistance2.RightHandSide == "5.0"
    assert ok.data["bend_count"] == 1
    nx = sm_nx()
    bend = nx.add_bend(nx.tab())
    moving = create(nx, "bend_taper", faces=[bend.ref], face=bend.ref, length=5)
    assert moving.error["field"] == "/parameters/face" and moving.mutation_outcome == "not_started"


def test_bulge_relief_sets_only_the_given_sizes_and_removes_material():
    nx = sm_nx()
    body = nx.tab()
    nx.add_bend(body)
    edge = body.edges[0]
    nx.on_commit("BulgeReliefBuilder", changes(nx, body, "SB_BulgeRelief", -30.9))
    ok = create(nx, "bulge_relief", edges=[edge.ref], height=3, width=6)
    assert ok.ok, ok
    b = nx.last("BulgeReliefBuilder")
    assert b.BendEdges.ReplaceRules.calls[0][0][0] == [("EdgeDumb", [edge])]
    assert b.Depth.RightHandSide == "3.0" and b.Width.RightHandSide == "6.0"
    assert b.WidthType == "BulgeReliefBuilderWidthTypes.Value"
    assert "Radius" not in b.__dict__  # NX's default radius stays
    nx = sm_nx()
    body = nx.tab()
    nx.on_commit("BulgeReliefBuilder", changes(nx, body, "SB_BulgeRelief", 5))
    bad = create(nx, "bulge_relief", edges=[body.edges[0].ref])
    assert bad.error["details"]["check"] == "volume"


def test_bead_is_a_circular_formed_bead_on_the_sketched_path():
    nx = sm_nx()
    body = nx.tab()
    sk = nx.sketch()
    nx.on_commit("BeadFeatureBuilder", changes(nx, body, "SB_BEAD", 98.65))
    ok = create(nx, "bead", section=sk.ref, height=2, radius=4, reverse=True)
    assert ok.ok, ok
    b = nx.last("BeadFeatureBuilder")
    assert b.CrossSectionType == "BeadBuilderCrossSectionTypeOptions.Circular"
    assert b.EndType == "BeadBuilderEndTypeOptions.Formed"
    assert b.Height.RightHandSide == "2.0" and b.Radius.RightHandSide == "4.0"
    assert b.PunchedWidth.RightHandSide == "8.0"
    assert b.HeightSide == "BeadBuilderHeightSideOptions.SectionReverseNormalSide"
    assert ok.data["body"]["id"] == body.ref and ok.data["bend_count"] == 0
    missing = create(nx, "bead", section=sk.ref, height=2)
    assert missing.error["field"] == "/parameters/radius"


def joggle_setup(nx):
    body = nx.tab()
    web = body.face((0, 0, 1))
    sk = nx.sketch()
    sk.geometry.append(Line((0, 40, 2), (100, 40, 2)))
    nx.part.Planes = Object("PLANES")
    nx.part.Planes.CreatePlane = Mock(return_value="PLANE")
    return body, web, sk


def test_joggle_offsets_the_faces_beyond_the_plane_through_the_line():
    nx = sm_nx()
    body, web, sk = joggle_setup(nx)
    nx.on_commit("JoggleBuilder", changes(nx, body, "SB_Joggle", 40, 2))
    ok = create(nx, "joggle", faces=[web.ref], section=sk.ref, height=5)
    assert ok.ok, ok
    b = nx.last("JoggleBuilder")
    item = b.InputList.Append.calls[0][0][0]
    assert item.Faces.ReplaceRules.calls[0][0][0] == [("FaceDumb", [web])]
    assert item.Depth.RightHandSide == "5.0" and item.ReverseDirection is False
    origin, normal, _ = nx.part.Planes.CreatePlane.call_args[0]
    assert [origin.X, origin.Y, origin.Z] == [0, 40, 2]
    assert [round(normal.X, 9), round(normal.Y, 9), round(normal.Z, 9)] == [0, -1, 0]
    assert b.StartPlane == "PLANE" and b.Side1Options.Runout.RightHandSide == "10.0"
    assert b.Side1Options.StationaryRadius.RightHandSide == "2.0"  # the sheet thickness


def test_joggle_needs_one_line_and_reports_the_missing_license():
    nx = sm_nx()
    _body, web, sk = joggle_setup(nx)
    sk.geometry.append(Line((0, 50, 2), (100, 50, 2)))
    two = create(nx, "joggle", faces=[web.ref], section=sk.ref, height=5)
    assert two.error["code"] == "NX_WRONG_GEOMETRY" and two.error["field"] == "/parameters/section"
    assert two.mutation_outcome == "not_started"
    sk.geometry.pop()
    nx.refuse("JoggleBuilder", 948801, "License Error")
    bad = create(nx, "joggle", faces=[web.ref], section=sk.ref, height=5)
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and bad.error["nx_code"] == 948801
    assert nx.live_builders == 0
