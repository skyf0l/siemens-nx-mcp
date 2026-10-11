"""Degrees of freedom of a sketch (domains/sketch/dof.py) and the constraint geometry rows
of nx_get_sketch, on the fake kit."""

import pytest

from nx_mcp.domains.sketch import dof
from tests.kit import run
from tests.unit.sketch.fakes import CT, Constraint, sketch_nx

UNDER, WELL = 2, 3  # the fake's Sketch.Status values


def dimension(sk, kind, geometry, reference=False):
    return sk._dimension(kind, geometry, "reference" if reference else "driving")


def con(sk, kind, *geometry):
    c = Constraint(kind, list(geometry), sk)
    sk.constraints.append(c)
    return c


def analysed(nx, sk):
    return dof.analyse(nx.tx, sk)


def rich(nx):
    """The live probe sketch: an L with a fixed corner, two concentric circles, an arc
    whose center slides on the base line, and a reference dimension."""
    sk = nx.sketch()
    base = nx.line(sk, (0, 0, 0), (40, 0, 0))
    side = nx.line(sk, (40, 0, 0), (40, 20, 0))
    inner = nx.arc(sk, (20, 10, 0), 4, 0, 360)
    arc = nx.arc(sk, (60, 0, 0), 5, 0, 90)
    outer = nx.arc(sk, (20, 10, 0), 6, 0, 360)
    con(sk, CT.Coincident, (side, "StartVertex"), (base, "EndVertex"))
    con(sk, CT.Horizontal, (base, "NotSet"))
    con(sk, CT.Perpendicular, (side, "NotSet"), (base, "NotSet"))
    con(sk, CT.Coincident, (outer, "ArcCenter"), (inner, "ArcCenter"))
    con(sk, CT.Fixed, (base, "StartVertex"))
    con(sk, CT.PointOnCurve, (arc, "ArcCenter"), (base, "NotSet"))
    dimension(sk, CT.ParallelDim, [(base, "StartPoint"), (base, "EndPoint")])
    dimension(sk, CT.RadiusDim, [(inner, "NotSet")])
    dimension(sk, CT.DiameterDim, [(outer, "NotSet")])
    dimension(sk, CT.HorizontalDim, [(base, "StartPoint"), (inner, "ArcCenter")])
    dimension(sk, CT.PerpendicularDim, [(base, "NotSet"), (inner, "ArcCenter")])
    dimension(sk, CT.ParallelDim, [(side, "EndPoint"), (arc, "ArcCenter")], reference=True)
    return sk, [base, side, inner, arc, outer]


def test_rich_sketch_dof_per_curve_and_total():
    nx = sketch_nx()
    sk, curves = rich(nx)
    found = analysed(nx, sk)
    assert found is not None
    # base: fixed start, horizontal, length; side: its length; inner/outer: fully
    # dimensioned; arc: center slides on the base line (5 - 1)
    assert [found["curves"][int(c.Tag)] for c in curves] == [0, 1, 0, 4, 0]
    assert found["dof"] == 5


def test_get_sketch_reports_dof_and_constraint_points_when_nx_agrees():
    nx = sketch_nx()
    sk, curves = rich(nx)
    nx.status = (UNDER, 2)  # NX: two curves not fully constrained
    ok = run(nx, "nx_get_sketch", sketch=sk.ref)
    assert ok.ok, ok
    assert ok.data["status"] == {
        "state": "under_constrained",
        "fully_constrained": False,
        "dof": 5,
        "free_curves": 2,
    }
    assert [r["dof"] for r in ok.data["curves"]] == [0, 1, 0, 4, 0]
    assert [r["constraints"] for r in ok.data["curves"]][:2] == [8, 3]
    ids = [r["curve"]["id"] for r in ok.data["curves"]]
    rows = ok.data["constraints"]
    coincident = rows[0]
    assert coincident["type"] == "coincident" and coincident["curves"] == [ids[1], ids[0]]
    assert coincident["geometry"] == [
        {"curve": ids[1], "point": "start"},
        {"curve": ids[0], "point": "end"},
    ]
    assert rows[1]["geometry"] == [{"curve": ids[0], "point": "curve"}]
    assert rows[5]["geometry"][0] == {"curve": ids[3], "point": "center"}
    length = rows[6]
    assert length["type"] == "parallel_dim" and length["curves"] == [ids[0]]
    assert [g["point"] for g in length["geometry"]] == ["start", "end"]
    assert "reference" not in length
    assert rows[-1]["reference"] is True and rows[-1]["curves"] == [ids[1], ids[3]]
    assert nx.session.ActiveSketch is None and nx.undo_marks == []


def test_geometry_only_still_carries_dof_and_skips_it_when_another_sketch_is_active():
    nx = sketch_nx()
    sk, _ = rich(nx)
    nx.status = (UNDER, 2)
    ok = run(nx, "nx_get_sketch", sketch=sk.ref, include=["geometry"])
    assert [r["dof"] for r in ok.data["curves"]] == [0, 1, 0, 4, 0]
    assert "status" not in ok.data
    nx.sketch(active=True)
    busy = run(nx, "nx_get_sketch", sketch=sk.ref, include=["geometry"])
    assert busy.ok and all("dof" not in r for r in busy.data["curves"])


def test_fully_constrained_sketch():
    nx = sketch_nx()
    sk = nx.sketch()
    line = nx.line(sk, (0, 0, 0), (25, 0, 0))
    con(sk, CT.Fixed, (line, "StartVertex"))
    con(sk, CT.Horizontal, (line, "NotSet"))
    dimension(sk, CT.ParallelDim, [(line, "StartPoint"), (line, "EndPoint")])
    nx.status = (WELL, 0)
    ok = run(nx, "nx_get_sketch", sketch=sk.ref, include=["geometry", "status"])
    assert ok.data["status"] == {
        "state": "well_constrained",
        "fully_constrained": True,
        "dof": 0,
        "free_curves": 0,
    }
    assert ok.data["curves"][0]["dof"] == 0


def test_free_geometry_and_simple_relations():
    nx = sketch_nx()
    sk = nx.sketch()
    a = nx.line(sk, (0, 0, 0), (10, 0, 0))
    b = nx.line(sk, (10, 0, 0), (10, 8, 0))
    circle = nx.arc(sk, (30, 0, 0), 3, 0, 360)
    arc = nx.arc(sk, (50, 0, 0), 3, 0, 90)
    found = analysed(nx, sk)
    assert [found["curves"][int(c.Tag)] for c in (a, b, circle, arc)] == [4, 4, 3, 5]
    assert found["dof"] == 16
    con(sk, CT.Coincident, (a, "EndVertex"), (b, "StartVertex"))
    found = analysed(nx, sk)
    assert found["dof"] == 14 and found["curves"][int(a.Tag)] == 4  # shared freedom
    con(sk, CT.Perpendicular, (a, "NotSet"), (b, "NotSet"))
    assert analysed(nx, sk)["dof"] == 13


@pytest.mark.parametrize(
    ("kind", "dof_after"),
    [(CT.Tangent, 5), (CT.EqualRadius, 5), (CT.Concentric, 4)],
)
def test_circle_relations(kind, dof_after):
    nx = sketch_nx()
    sk = nx.sketch()
    one = nx.arc(sk, (0, 0, 0), 5, 0, 360)
    two = nx.arc(sk, (8, 0, 0), 3, 0, 360)
    con(sk, kind, (one, "NotSet"), (two, "NotSet"))
    assert analysed(nx, sk)["dof"] == dof_after


def test_line_tangent_to_circle_and_angle_dimension():
    nx = sketch_nx()
    sk = nx.sketch()
    line = nx.line(sk, (-10, 5, 0), (10, 5, 0))
    circle = nx.arc(sk, (0, 0, 0), 5, 0, 360)
    other = nx.line(sk, (0, 0, 0), (10, 10, 0))
    con(sk, CT.Tangent, (line, "NotSet"), (circle, "NotSet"))
    dimension(sk, CT.AngularDim, [(line, "NotSet"), (other, "NotSet")])
    assert analysed(nx, sk)["dof"] == 4 + 3 + 4 - 2


def test_unmodelled_constraints_and_disagreement_withhold_the_dof():
    nx = sketch_nx()
    sk = nx.sketch()
    a = nx.line(sk, (0, 0, 0), (10, 0, 0))
    b = nx.line(sk, (0, 5, 0), (10, 5, 0))
    assert dof.checked(nx.tx, sk, 2)["dof"] == 8
    assert dof.checked(nx.tx, sk, 1) is None  # NX counts otherwise
    assert dof.checked(nx.tx, sk, None) is None  # NX gave no count
    con(sk, CT.Mirror, (a, "NotSet"), (b, "NotSet"))
    assert analysed(nx, sk) is None


def test_reference_dimensions_do_not_constrain():
    nx = sketch_nx()
    sk = nx.sketch()
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    dimension(sk, CT.ParallelDim, [(line, "StartPoint"), (line, "EndPoint")], reference=True)
    assert analysed(nx, sk)["dof"] == 4
    assert not dof.driving(nx.tx, sk.constraints[0])
