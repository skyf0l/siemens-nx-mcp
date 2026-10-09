"""Lofted, advanced and variational flanges, gussets, edge rips and closed corners of
nx_create_sheet_metal_feature on the shared fake, through the real kernel."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.sheet_metal import features
from tests.kit import run
from tests.kit.objects import Object
from tests.unit.sheet_metal.fakes import sm_nx

TOOL = "nx_create_sheet_metal_feature"


def create(nx, operation, **parameters):
    return run(nx, TOOL, operation=operation, parameters=parameters)


def flanged(nx, body, angle=90.0, radius=3.0, grow=3656.637):
    def hook(b):
        nx.add_bend(body, radius=radius, angle=angle)
        body.volume += grow
        return nx.made("SB_FLANGE", body)

    return hook


def profile(nx, *points):
    """A sketch of lines through ``points``; ``curve.ends`` reads ``line.ends``."""
    sk = nx.sketch()
    for a, b in zip(points, points[1:], strict=False):
        line = Object("LINE")
        line.ends = (a, b)
        sk.AddGeometry(line)
    return sk


def test_lofted_flange_takes_two_profiles_and_reads_the_thickness_back(monkeypatch):
    monkeypatch.setattr(features.curve, "ends", lambda tx, c: c.ends)
    nx = sm_nx()
    start = profile(nx, (60, 0, 0), (30, 0, 0), (0, 0, 0))
    end = profile(nx, (10, 0, 40), (50, 0, 40))
    nx.on_commit("LoftedFlangeFeatureBuilder", lambda b: nx.made("SB_LOFT", nx.tab(60, 40, 2)))
    ok = create(nx, "lofted_flange", section=start.ref, end_section=end.ref, thickness=2,
                reverse=True)  # fmt: skip
    assert ok.ok, ok
    b = nx.last("LoftedFlangeFeatureBuilder")
    assert b.StartSection is not b.EndSection
    assert b.GetThickness.calls  # the thickness expression is set
    # The section points are corresponding free ends: the lowest, and the nearest to it.
    xyz = lambda p: (p.X, p.Y, p.Z)  # noqa: E731
    assert xyz(b.StartSectionPoint) == (0, 0, 0) and xyz(b.EndSectionPoint) == (10, 0, 40)
    assert b.NumberOfBendSegments == 8
    assert b.ThicknessSide == "LoftedFlangeBuilderSectionSideOptions.Right"
    assert b.SetApplicationContext.calls[0][0][0] == "ApplicationContext.NxSheetMetal"
    assert ok.data["thickness"] == 2 and ok.data["volume"] == 4800
    bad = create(nx, "lofted_flange", section=start.ref, thickness=2)
    assert bad.error["field"] == "/parameters/end_section" and bad.mutation_outcome == "not_started"
    closed = profile(nx, (0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 0, 0))
    bad = create(nx, "lofted_flange", section=closed.ref, end_section=end.ref, thickness=2)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/parameters/section"
    assert bad.mutation_outcome == "rolled_back" and nx.live_builders == 0


@pytest.mark.parametrize(
    ("operation", "kind"),
    [
        ("advanced_flange", "AdvancedFlangeBuilder"),
        ("variational_flange", "VariationalFlangeBuilder"),
    ],
)
def test_flange_variants_set_edges_inside_length_and_angle(operation, kind):
    nx = sm_nx()
    body = nx.tab()
    edge = body.edges[0]
    nx.on_commit(kind, flanged(nx, body, radius=4.0))
    ok = create(nx, operation, edges=[edge.ref], length=20, radius=4, neutral_factor=0.4)
    assert ok.ok, ok
    b = nx.last(kind)
    assert b.Edges.ReplaceRules.calls[0][0][0] == [("EdgeDumb", [edge])]
    assert b.Length.RightHandSide == "20.0" and b.Angle.RightHandSide == "90.0"
    assert str(b.LengthReference).endswith(".Inside")
    if operation == "advanced_flange":
        assert b.Type == "AdvancedFlangeBuilderTypes.ByValue"
        assert b.BendOptions.BendRadius.RightHandSide == "4.0"
        assert b.BendOptions.UseGlobalNeutralFactor is False
    else:
        assert b.LengthLawType == b.AngleLawType == "VariationalFlangeBuilderLawTypes.Constant"
        assert b.Radius.RightHandSide == "4.0" and b.NeutralFactor.RightHandSide == "0.4"
    assert ok.data["bend_count"] == 1 and ok.data["volume"] == pytest.approx(19656.637)
    # A flange that adds no bend is not what was asked.
    nx.on_commit(kind, lambda b: nx.made("SB_FLANGE", body))
    bad = create(nx, operation, edges=[nx.ref_of(edge, "edge")], length=20)
    assert bad.error["details"]["check"] == "bend_count" and bad.mutation_outcome == "rolled_back"


def test_gusset_spans_the_middle_of_one_bend(monkeypatch):
    monkeypatch.setattr(features.measure, "bounds", lambda tx, objs: ((0, 2, 0), (100, 5, 3)))
    nx = sm_nx()
    nx.part.Planes = NS(CreatePlane=lambda origin, normal, _update: ("plane", origin, normal))
    body = nx.tab()
    bend = nx.add_bend(body)

    def stiffen(b):
        body.volume += 30
        return nx.made("SB_GUSSET", body)

    nx.on_commit("GussetBuilder", stiffen)
    ok = create(nx, "gusset", faces=[bend.ref], height=6, width=10)
    assert ok.ok, ok
    b = nx.last("GussetBuilder")
    assert b.BendFace.Value is bend
    assert b.Type == "GussetBuilderTypes.AutomaticProfile"
    assert b.PlacementType == "GussetBuilderPlacementTypes.Single"
    assert b.Depth.RightHandSide == "6.0" and b.Width.RightHandSide == "10.0"
    assert b.DatumPlane[0] == "plane"  # an NXOpen.Plane normal to the bend axis
    assert ok.data["volume"] == 16030
    web = body.face((0, 0, 1))
    bad = create(nx, "gusset", faces=[nx.ref_of(web, "face")], height=6, width=10)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/parameters/faces/0"


def test_edge_rip_cuts_along_a_sketch():
    nx = sm_nx()
    body = nx.tab()
    sk = nx.sketch()

    def rip(b):
        body.volume -= 20
        return nx.made("SB_EDGE_RIP", body)

    nx.on_commit("EdgeRipFeatureBuilder", rip)
    ok = create(nx, "edge_rip", section=sk.ref, width=0.5)
    assert ok.ok, ok
    b = nx.last("EdgeRipFeatureBuilder")
    assert b.UseSystemWidth is False and b.Width.RightHandSide == "0.5"
    assert ok.data["volume"] == 15980 and ok.data["body"]["id"] == body.ref


def test_closed_corner_pairs_two_bend_faces():
    nx = sm_nx()
    body = nx.tab()
    one, two = nx.add_bend(body), nx.add_bend(body)

    def close(b):
        body.volume += 50
        return nx.made("SB_CLOSED_CORNER", body)

    nx.on_commit("ClosedCornerFeatureBuilder", close)
    ok = create(nx, "closed_corner", faces=[one.ref, two.ref])
    assert ok.ok, ok
    assert nx.last("ClosedCornerFeatureBuilder").AddFacePair.calls[0][0] == (one, two)
    bad = create(nx, "closed_corner", faces=[nx.ref_of(one, "face")])
    assert bad.error["field"] == "/parameters/faces" and bad.mutation_outcome == "not_started"
