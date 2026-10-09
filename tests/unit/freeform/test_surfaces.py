"""nx_create_mesh_surface and nx_create_bridge_surface on the shared fake."""

import pytest

from tests.kit import NXException, run
from tests.unit.freeform.fakes import curve, edge, face, flat, freeform_nx, made, sheet

# ---------------------------------------------------------------------------
# Mesh


def network(nx):
    p = [curve(nx, [0, 0, 0], [20, 0, 0]), curve(nx, [0, 10, 0], [20, 10, 0])]
    c = [curve(nx, [0, 0, 0], [0, 10, 0]), curve(nx, [20, 0, 0], [20, 10, 0])]
    return p, c


def curve_sections(nx, curves):
    return [[nx.ref_of(x, "curve")] for x in curves]


def mesh_commit(nx, box=(0, 0, 0, 20, 10, 0)):
    nx.on_commit("ThroughCurveMeshBuilder", lambda b: made(nx, "THRU_CURVE_MESH", [sheet(nx, box)]))


def test_mesh_builds_sections_with_scaled_tolerances_and_reads_back_bounds():
    nx = freeform_nx(units="in")
    p, c = network(nx)
    mesh_commit(nx)
    ok = run(nx, "nx_create_mesh_surface", primary=curve_sections(nx, p),
             cross=curve_sections(nx, c))  # fmt: skip
    assert ok.ok, ok
    b = nx.last("ThroughCurveMeshBuilder")
    assert b.PositionTolerance == pytest.approx(0.001 / 25.4) == b.IntersectionTolerance
    sections = [call[0][0] for call in b.PrimaryCurvesList.Append.calls]
    assert len(sections) == 2 and len(b.CrossCurvesList.Append.calls) == 2
    assert sections[0].tolerances[0] == pytest.approx(0.001 / 25.4)
    assert sections[0].rules == [("CurveDumb", [p[0]])] and sections[0].allowed == "only_curves"
    assert ok.data["bounds"] == {"min": [0, 0, 0], "max": [20, 10, 0]}
    assert ok.units["length"] == "in" and nx.live_builders == 0


def test_mesh_tolerance_argument_and_rejections():
    nx = freeform_nx()
    p, c = network(nx)
    mesh_commit(nx)
    ok = run(nx, "nx_create_mesh_surface", primary=curve_sections(nx, p),
             cross=curve_sections(nx, c), tolerance="0.01 mm")  # fmt: skip
    assert ok.ok and nx.last("ThroughCurveMeshBuilder").PositionTolerance == 0.01
    dup = run(nx, "nx_create_mesh_surface", primary=[[p[0].ref], [p[0].ref]],
              cross=curve_sections(nx, c))  # fmt: skip
    assert dup.error["field"] == "/primary/1/0" and dup.mutation_outcome == "not_started"
    neg = run(nx, "nx_create_mesh_surface", primary=curve_sections(nx, p),
              cross=curve_sections(nx, c), tolerance=-1)  # fmt: skip
    assert neg.error["field"] == "/tolerance"
    one = run(nx, "nx_create_mesh_surface", primary=[[p[0].ref]], cross=curve_sections(nx, c))
    assert one.error["category"] == "invalid_argument"


def test_mesh_native_refusal_and_missed_curve():
    nx = freeform_nx()
    p, c = network(nx)
    nx.refuse("ThroughCurveMeshBuilder", 670946, "Strings do not intersect within tolerance.")
    bad = run(nx, "nx_create_mesh_surface", primary=curve_sections(nx, p),
              cross=curve_sections(nx, c))  # fmt: skip
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 670946
    assert bad.mutation_outcome == "rolled_back" and nx.live_builders == 0
    nx.errors.clear()
    mesh_commit(nx, box=(0, 0, 0, 20, 5, 0))  # misses the y=10 primary
    bad = run(nx, "nx_create_mesh_surface", primary=curve_sections(nx, p),
              cross=curve_sections(nx, c))  # fmt: skip
    assert bad.error["details"]["check"] == "curve[1]"


# ---------------------------------------------------------------------------
# Bridge


def bridge_nx(bridge_derivatives=None, solid_edge=False):
    """Sheets z=0 (x 0..20) and z=5 (x 30..50) and their facing edges x=20 and x=30."""
    nx = freeform_nx()
    fa = face(nx, flat())
    fb = face(nx, flat())
    extra = [face(nx, flat())] if solid_edge else []
    a = edge(nx, [20, 0, 0], [20, 10, 0], [fa, *extra])
    b = edge(nx, [30, 10, 5], [30, 0, 5], [fb])
    sheet(nx, (0, 0, 0, 20, 10, 0), [fa], [a])
    sheet(nx, (30, 0, 5, 50, 10, 5), [fb], [b])

    def commit(builder):
        bridge_face = face(nx, bridge_derivatives or flat())
        ends = [
            edge(nx, [20, 0, 0], [20, 10, 0], [bridge_face]),
            edge(nx, [30, 0, 5], [30, 10, 5], [bridge_face]),
            edge(nx, [20, 0, 0], [30, 0, 5], [bridge_face]),
            edge(nx, [20, 10, 0], [30, 10, 5], [bridge_face]),
        ]
        body = sheet(nx, (20, 0, 0, 30, 10, 5), [bridge_face], ends)
        return made(nx, "BRIDGE", [body])

    nx.on_commit("ThroughCurvesBuilder", commit)
    nx.on_commit("BridgeSurfaceBuilder", commit)
    return nx, a, b, fa, fb


def unlicensed(nx):
    make = nx.builder

    def builder(kind, *args):
        if kind == "BridgeSurfaceBuilder":
            raise NXException(949895, "The requested license feature is not available")
        return make(kind, *args)

    nx.builder = builder


@pytest.mark.parametrize(("reverse", "start"), [(False, [30, 0, 5]), (True, [30, 10, 5])])
def test_bridge_without_studio_license_builds_through_curves(reverse, start):
    nx, a, b, fa, fb = bridge_nx()
    unlicensed(nx)
    ok = run(nx, "nx_create_bridge_surface", first=a.ref, second=b.ref, continuity="g2",
             reverse_second=reverse)  # fmt: skip
    assert ok.ok and ok.data["construction"] == "through_curves", ok
    tc = nx.last("ThroughCurvesBuilder")
    sections = [call[0][0] for call in tc.SectionsList.Append.calls]
    helps = [s.added[0][2] for s in sections]
    assert [[h.X, h.Y, h.Z] for h in helps] == [[20, 0, 0], start]
    assert sections[0].rules == [("EdgeDumb", [a])]
    g2 = nx.nx.GeometricUtilities.Continuity.ContinuityTypes.G2
    for end, adjacent in ((tc.FirstSectionContinuity, fa), (tc.LastSectionContinuity, fb)):
        assert end.ContinuityType is g2
        assert end.ConstraintFaces.ReplaceRules.calls[0][0][0] == [("FaceDumb", [adjacent])]
    continuity = ok.data["continuity"]
    for side in ("first", "second"):
        assert continuity[side] == pytest.approx({"gap": 0.0, "angle": 0.0, "curvature": 0.0})
    assert ok.data["bounds"] == {"min": [20, 0, 0], "max": [30, 10, 5]}
    assert nx.live_builders == 0


def test_bridge_with_license_uses_bridge_surface():
    nx, a, b, _fa, _fb = bridge_nx()
    ok = run(nx, "nx_create_bridge_surface", first=a.ref, second=b.ref, continuity="g1",
             reverse_second=True)  # fmt: skip
    assert ok.ok and ok.data["construction"] == "bridge_surface", ok
    bb = nx.last("BridgeSurfaceBuilder")
    g1 = nx.nx.GeometricUtilities.Continuity.ContinuityTypes.G1
    assert bb.FirstEdgeContinuity.ContinuityType is g1 is bb.SecondEdgeContinuity.ContinuityType
    assert bb.FirstEdgeSelection.Value is a and bb.IsSecondEdgeReversed is True
    assert not [x for x in nx.builders if x.kind == "ThroughCurvesBuilder"]


def test_g0_bridge_reports_the_crease_angle():
    nx, a, b, _fa, _fb = bridge_nx(bridge_derivatives=flat(du=(2, 0, 1)))
    unlicensed(nx)
    ok = run(nx, "nx_create_bridge_surface", first=a.ref, second=b.ref, continuity="g0")
    assert ok.ok, ok
    tc = nx.last("ThroughCurvesBuilder")
    assert not tc.FirstSectionContinuity.ConstraintFaces.ReplaceRules.calls
    assert ok.data["continuity"]["first"]["angle"] == pytest.approx(26.56505117707799)


def test_g1_readback_failure_rolls_back():
    nx, a, b, _fa, _fb = bridge_nx(bridge_derivatives=flat(du=(2, 0, 1)))
    unlicensed(nx)
    bad = run(nx, "nx_create_bridge_surface", first=a.ref, second=b.ref, continuity="g1")
    assert bad.error["category"] == "verification_failed", bad
    assert bad.error["details"]["check"] == "first.g1" and bad.mutation_outcome == "rolled_back"


def test_twisting_bridge_is_build_failed_with_hint():
    nx, a, b, _fa, _fb = bridge_nx()
    unlicensed(nx)
    nx.refuse("ThroughCurvesBuilder", 670929, "Cannot create B-surface by through curves.")
    bad = run(nx, "nx_create_bridge_surface", first=a.ref, second=b.ref, continuity="g1",
              reverse_second=True)  # fmt: skip
    assert bad.error["code"] == "NX_BUILD_FAILED" and bad.error["nx_code"] == 670929
    assert "reverse_second" in bad.error["hint"] and nx.live_builders == 0


def test_other_bridge_builder_errors_propagate():
    nx, a, b, _fa, _fb = bridge_nx()
    nx.refuse("BridgeSurfaceBuilder", 949895, "license lost at commit")
    bad = run(nx, "nx_create_bridge_surface", first=a.ref, second=b.ref, continuity="g0")
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE"
    assert not [x for x in nx.builders if x.kind == "ThroughCurvesBuilder"]


def test_bridge_argument_rejections():
    nx, a, b, _fa, _fb = bridge_nx(solid_edge=True)
    same = run(nx, "nx_create_bridge_surface", first=a.ref, second=a.ref, continuity="g0")
    assert same.error["field"] == "/second" and same.mutation_outcome == "not_started"
    solid = run(nx, "nx_create_bridge_surface", first=a.ref, second=b.ref, continuity="g2")
    assert solid.error["code"] == "NX_WRONG_GEOMETRY" and solid.error["field"] == "/first"
    assert not nx.builders
    upper = run(nx, "nx_create_bridge_surface", first=a.ref, second=b.ref, continuity="G1")
    assert upper.error["category"] == "invalid_argument"
