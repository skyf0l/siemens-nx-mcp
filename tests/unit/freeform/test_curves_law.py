"""nx_create_law_curve and nx_create_general_conic on the shared fake."""

import pytest

from nx_mcp.core import evidence
from tests.kit import run
from tests.unit.freeform.support import Output, curves_nx, made

LAW = "LawCurveBuilder"
CON = "GeneralConicBuilder"


def test_ramps_and_constants_drive_the_laws_and_read_back_the_ends():
    nx = curves_nx()
    nx.on_commit(LAW, lambda b: made(nx, "SPLINE_LAW", [[[0, 0, 5], [50, 20, 5], [100, 40, 5]]]))
    ok = run(nx, "nx_create_law_curve", x={"start": 0, "end": 100},
             y={"start": 0, "end": 40, "cubic": True}, z=5)  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok
    b = nx.last(LAW)
    T = nx.nx.GeometricUtilities.LawBuilder.Type
    assert b.XLaw.LawType is T.Linear and b.YLaw.LawType is T.Cubic
    assert (b.XLaw.StartValue.RightHandSide, b.XLaw.EndValue.RightHandSide) == ("0.0", "100.0")
    assert b.ZLaw.LawType is T.Constant and b.ZLaw.Value.RightHandSide == "5.0"
    assert b.DistanceTolerance == 0.001
    assert ok.data["expected_start"] == [0, 0, 5] and ok.data["expected_end"] == [100, 40, 5]
    assert ok.data["gap"] == 0 and ok.data["expressions"] == [] and ok.data["count"] == 1


def test_formulas_create_t_and_function_expressions():
    nx = curves_nx()
    nx.expression("xt", "1")  # taken: the x function becomes xt_1
    nx.on_commit(LAW, lambda b: made(nx, "SPLINE_LAW", [[[0, 20, 0], [50, -20, 0], [100, 20, 0]]]))
    ok = run(nx, "nx_create_law_curve", x={"formula": "100*t"}, y={"formula": "20*cos(360*t)"})
    assert ok.ok, ok
    assert ok.data["expressions"] == ["t", "xt_1", "yt"]
    b = nx.last(LAW)
    T = nx.nx.GeometricUtilities.LawBuilder.Type
    assert b.XLaw.LawType is T.ByEquation and b.XLaw.Function == "xt_1"
    assert b.XLaw.Parameter == "t" and b.YLaw.Function == "yt"
    assert b.ZLaw.LawType is T.Constant and b.ZLaw.Value.RightHandSide == "0.0"
    made_exp = {e.Name: e for e in nx.part.Expressions}
    assert made_exp["yt"].RightHandSide == "20*cos(360*t)" and made_exp["t"].Units is None
    assert made_exp["yt"].Units.Name == "MilliMeter"
    assert ok.data["expected_start"] == [None, None, 0] and ok.data["gap"] == 0


def test_law_refusals():
    nx = curves_nx()
    point = run(nx, "nx_create_law_curve", x=1, y=2)
    assert point.error["field"] == "/x" and point.mutation_outcome == "not_started"
    t = nx.expression("t", "3")
    t.Units = nx.part.UnitCollection.FindObject("MilliMeter")
    taken = run(nx, "nx_create_law_curve", x={"formula": "100*t"})
    assert taken.error["code"] == "NX_NAME_CONFLICT" and nx.live_builders == 0
    nx.on_commit(LAW, lambda b: made(nx, "SPLINE_LAW", [[[0, 0, 0], [90, 0, 0]]]))
    off = run(nx, "nx_create_law_curve", x={"start": 0, "end": 100})
    assert off.error["details"]["check"] == "ends" and off.mutation_outcome == "rolled_back"


# ---------------------------------------------------------------------------
# General conic


class Parabola(Output):
    pass


def test_rho_conic_sets_its_points_and_reads_back_the_shoulder():
    nx = curves_nx()

    def parabola(b):
        f = made(nx, "GENERAL_CONIC", [])
        c = nx.remember(Parabola([[0, 300, 10], [50, 320, 10], [100, 300, 10]]))
        f.GetEntities = lambda: [c]
        return f

    nx.on_commit(CON, parabola)
    ok = run(nx, "nx_create_general_conic", method="rho", start=[0, 300, 10], end=[100, 300, 10],
             anchor=[50, 340, 10], rho=0.5)  # fmt: skip
    assert ok.ok and ok.data["kind"] == "parabola" and ok.data["gap"] == 0, ok
    b = nx.last(CON)
    G = nx.nx.Features.GeneralConicBuilder
    assert b.Type is G.Types.TwoPointsAnchorRho and b.Rho.RightHandSide == "0.5"
    assert b.DrawingPlaneOption is G.DrawingPlaneOptions.General
    assert b.GeneralDrawingPlane.normal == pytest.approx([0, 0, 1])
    a = b.AnchorPoint.Coordinates
    assert [a.X, a.Y, a.Z] == [50, 340, 10]
    nx.on_commit(CON, lambda b: made(nx, "GENERAL_CONIC", [[[0, 300, 10], [100, 300, 10]]]))
    flat = run(nx, "nx_create_general_conic", method="rho", start=[0, 300, 10],
               end=[100, 300, 10], anchor=[50, 340, 10], rho=0.5)  # fmt: skip
    assert flat.error["details"]["check"] == "points" and flat.mutation_outcome == "rolled_back"


def test_five_point_conic_and_its_checks():
    nx = curves_nx()
    pts = [[50, 0, 100], [30, 0, 120], [0, 0, 130], [-30, 0, 120], [-50, 0, 100]]
    nx.on_commit(CON, lambda b: made(nx, "GENERAL_CONIC", [pts]))
    ok = run(nx, "nx_create_general_conic", method="five_points", points=pts)
    assert ok.ok and ok.data["kind"] == "conic" and ok.data["gap"] == 0, ok
    b = nx.last(CON)
    assert b.Type is nx.nx.Features.GeneralConicBuilder.Types.FivePoints
    p = b.InteriorPoint3.Coordinates
    assert pts[3] == [p.X, p.Y, p.Z]
    assert b.GeneralDrawingPlane.normal == pytest.approx([0, 1, 0]) or (
        b.GeneralDrawingPlane.normal == pytest.approx([0, -1, 0])
    )
    skew = run(nx, "nx_create_general_conic", method="five_points",
               points=[*pts[:4], [-50, 5, 100]])  # fmt: skip
    assert skew.error["field"] == "/points" and skew.mutation_outcome == "not_started"
    line = run(nx, "nx_create_general_conic", method="rho", start=[0, 0, 0], end=[100, 0, 0],
               anchor=[50, 0, 0], rho=0.5)  # fmt: skip
    assert line.error["field"] == "/anchor"
    missing = run(nx, "nx_create_general_conic", method="rho", start=[0, 0, 0], end=[1, 0, 0])
    assert missing.error["category"] == "invalid_argument" and nx.live_builders == 0


def test_conic_license_refusal():
    nx = curves_nx()
    nx.licenses["solid_modeling"] = False
    bad = run(nx, "nx_create_general_conic", method="rho", start=[0, 0, 0], end=[100, 0, 0],
              anchor=[50, 20, 0], rho=0.3)  # fmt: skip
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE" and not nx.builders
    evidence.forget_licenses()
