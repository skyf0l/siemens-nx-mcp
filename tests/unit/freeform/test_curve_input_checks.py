"""Input checks and result details of the freeform curve tools on the shared fake: mirror
curves lying in the plane, offset strings that do not chain, law formula errors, conic
point order and isoparametric curve families."""

import math

import pytest

from tests.kit import NXException, run
from tests.unit.freeform import fakes
from tests.unit.freeform.fakes import face, flat
from tests.unit.freeform.support import curves_nx, made

MIRROR = "MirrorCurveBuilder"
OFF = "OffsetCurveBuilder"
LAW = "LawCurveBuilder"
CON = "GeneralConicBuilder"
ISO = "IsoparametricCurvesBuilder"


def curve(nx, *points):
    c = fakes.curve(nx, *points)
    c.GetLength = lambda: sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))
    return c


# ---- mirror ---------------------------------------------------------------------------
def test_mirror_skips_curves_lying_in_the_plane_and_refuses_when_all_do():
    nx = curves_nx()
    off = curve(nx, [10, 0, 0], [30, 0, 0])
    flat_one = curve(nx, [0, 0, 0], [0, 20, 0])  # in the yz plane
    nx.on_commit(MIRROR, lambda b: made(nx, "MIRROR_CURVE", [[[-10, 0, 0], [-30, 0, 0]]]))
    ok = run(nx, "nx_create_mirror_curve", curves=[off.ref, flat_one.ref], plane="yz")
    assert ok.ok and ok.data["skipped"] == [flat_one.ref], ok
    assert ok.data["count"] == 1 and ok.data["symmetry_gap"] == pytest.approx(0)
    rules = nx.last(MIRROR).Curve.AddToSection.calls[0][0][0]
    assert rules == [("CurveDumb", [off])]
    assert any(flat_one.ref in w["message"] for w in ok.warnings)
    nx.builders.clear()
    bad = run(nx, "nx_create_mirror_curve", curves=[flat_one.ref], plane="yz")
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/curves"
    assert bad.mutation_outcome == "not_started" and not nx.builders


# ---- offset curve -----------------------------------------------------------------------
def test_offset_curves_that_do_not_chain_are_refused_before_nx():
    nx = curves_nx()
    a = curve(nx, [0, 0, 0], [50, 0, 0])
    b = curve(nx, [0, 30, 0], [50, 30, 0])  # the opposite side, not connected
    bad = run(nx, "nx_create_offset_curve", curves=[a.ref, b.ref], distance=5)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/curves/1"
    assert "end to end" in bad.error["hint"] and bad.mutation_outcome == "not_started"
    assert not nx.builders


def test_offset_direction_failure_hint_names_the_cause():
    nx = curves_nx()
    make = nx.builder

    def builder(kind, *args):
        bld = make(kind, *args)
        if kind == OFF:

            def compute():
                raise NXException(670001, "Input curves must form continuous string.")

            bld.ComputeOffsetDirection = compute
        return bld

    nx.builder = builder
    a = curve(nx, [0, 0, 0], [50, 0, 0])
    b = curve(nx, [50, 0, 0], [50, 30, 0])
    bad = run(nx, "nx_create_offset_curve", curves=[a.ref, b.ref], distance=5)
    assert bad.error["code"] == "NX_BUILD_FAILED"
    assert "axial_3d" not in bad.error["hint"] and "end to end" in bad.error["hint"]


# ---- law curve formulas --------------------------------------------------------------------
def test_a_bad_law_formula_names_the_axis_field_and_the_unknown_name():
    nx = curves_nx()
    create = nx.part.Expressions.CreateNumberExpression

    def picky(spec, unit):
        if "foo" in spec:
            raise NXException(1050007, "The specified expression variable does not exist.")
        if spec.count("(") != spec.count(")"):
            raise NXException(3270008, "The specified string contains a syntax error")
        return create(spec, unit)

    nx.part.Expressions.CreateNumberExpression = picky
    nx.expression("amp", "20")
    bad = run(nx, "nx_create_law_curve", x={"start": 0, "end": 100},
              y={"formula": "amp*sin(360*t)*foo"})  # fmt: skip
    err = bad.error
    assert err["code"] == "NX_INVALID_FORMULA" and err["field"] == "/y/formula", bad
    assert err["details"]["unknown"] == ["foo"] and "foo" in err["message"]
    assert bad.mutation_outcome != "committed"
    bad = run(nx, "nx_create_law_curve", x={"start": 0, "end": 100},
              z={"formula": "20*sin(360*t"})  # fmt: skip
    assert bad.error["field"] == "/z/formula" and bad.error["details"]["unknown"] == []
    assert "not a valid NX formula" in bad.error["message"]


# ---- conic -------------------------------------------------------------------------------
def test_five_points_out_of_order_are_refused_before_nx():
    nx = curves_nx()
    zigzag = [[0, 0, 0], [10, 10, 0], [20, -10, 0], [30, 10, 0], [40, 0, 0]]
    bad = run(nx, "nx_create_general_conic", method="five_points", points=zigzag)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT" and bad.error["field"] == "/points/2"
    assert bad.mutation_outcome == "not_started" and not nx.builders
    line = run(nx, "nx_create_general_conic", method="five_points",
               points=[[0, 0, 0], [10, 0, 0], [20, 0, 0], [30, 0, 0], [40, 0, 0]])  # fmt: skip
    assert "five points are collinear" in line.error["message"]


def test_a_five_point_conic_missing_a_point_is_an_input_error():
    nx = curves_nx()
    pts = [[50, 0, 100], [30, 0, 120], [0, 0, 130], [-30, 0, 120], [-50, 0, 100]]
    nx.on_commit(CON, lambda b: made(nx, "GENERAL_CONIC", [[pts[0], [0, 0, 150], pts[4]]]))
    bad = run(nx, "nx_create_general_conic", method="five_points", points=pts)
    assert bad.error["category"] == "invalid_argument" and bad.error["field"] == "/points"
    assert bad.error["details"]["gap"] > 1 and bad.mutation_outcome == "rolled_back"


# ---- isoparametric families and seam duplicates -----------------------------------------
def test_iso_curves_report_their_family_and_the_seam_duplicate():
    nx = curves_nx()
    f = face(nx, flat())
    f.box = [-20, -20, 0, 20, 20, 60]
    # uv: three lines along z (u family; the first and last on the same seam) and a line
    # across (v family: the fake's face parameters are x and y)
    lines = [[[20, 0, 0], [20, 0, 60]], [[-20, 0, 0], [-20, 0, 60]], [[20, 0, 0], [20, 0, 60]]]
    ring = [[[-20, 5, 30], [20, 5, 30]]]
    nx.on_commit(ISO, lambda b: made(nx, "ISOPARAMETRIC_CURVES", lines + ring))
    ok = run(nx, "nx_create_isoparametric_curves", face=nx.ref_of(f, "face"), direction="uv",
             count=3)  # fmt: skip
    assert ok.ok, ok
    rows = ok.data["curves"]
    assert [r["family"] for r in rows] == ["u", "u", "u", "v"]
    assert [r["duplicate_of"] for r in rows] == [None, None, 0, None]
    assert rows[0]["parameter"] == pytest.approx(20) and ok.data["distinct"] == 3
    assert any("count 4 gives 3" in w["message"] for w in ok.warnings)
