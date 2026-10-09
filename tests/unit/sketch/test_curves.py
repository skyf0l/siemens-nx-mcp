"""nx_create_sketch_curves and nx_edit_sketch_curves on the fake kit."""

import math

import pytest

from nx_mcp.kit.sketch import ellipse_perimeter, ellipse_point
from tests.kit import run
from tests.unit.sketch.fakes import CT, Constraint, Line, P, sketch_nx


def _curves(nx, sk, *curves, **kw):
    return run(nx, "nx_create_sketch_curves", sketch=sk.ref, curves=list(curves), **kw)


def test_every_curve_type_is_created_read_back_and_closed_shapes_are_joined():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    ok = _curves(
        nx,
        sk,
        {"type": "line", "start": [0, 0], "end": [10, 0]},
        {"type": "arc", "center": [0, 0], "radius": 5, "start_angle": 0, "end_angle": 90},
        {"type": "circle", "center": [50, 0], "radius": 3},
        {"type": "rectangle", "corner1": [0, 0], "corner2": [40, 20]},
        {"type": "polygon", "center": [0, 0], "sides": 6, "radius": 20},
        {"type": "slot", "center": [0, 50], "width": 30, "height": 10},
        {"type": "rounded_rectangle", "center": [0, 100], "width": 40, "height": 20, "radius": 4},
        {"type": "ellipse", "center": [0, 200], "major_radius": 20, "minor_radius": 10},
    )
    assert ok.ok, ok
    rows = ok.data["curves"]
    assert [r["item"] for r in rows].count(3) == 4 and [r["item"] for r in rows].count(4) == 6
    assert [r["item"] for r in rows].count(5) == 4 and [r["item"] for r in rows].count(6) == 8
    assert rows[1]["type"] == "arc" and rows[2]["type"] == "circle"
    hexagon = [r for r in rows if r["item"] == 4]
    assert hexagon[0]["start"] == pytest.approx([20, 0]) and hexagon[0]["length"] == pytest.approx(
        20
    )
    coincident = [c for c in sk.constraints if c.ConstraintType == CT.Coincident]
    assert len(coincident) == 4 + 6 + 4 + 8  # closed shapes only
    assert ok.frame == "sketch_local" and nx.session.ActiveSketch is sk  # stays active
    assert ok.data["curves"][-1]["major_radius"] == 20 and nx.live_builders == 0
    assert len(ok.changes["created"]) == len(rows)


def test_inactive_sketch_is_activated_for_the_call_and_restored():
    nx = sketch_nx()
    sk = nx.sketch(origin=(0, 0, 7), x_axis=(0, 1, 0), y_axis=(0, 0, 1))
    ok = _curves(nx, sk, {"type": "line", "start": [1, 2], "end": [3, 4]}, construction=True)
    assert ok.ok, ok
    line = sk.geometry[0]
    assert [line.EndPoint.X, line.EndPoint.Y, line.EndPoint.Z] == [0, 3, 11]
    assert ok.data["curves"][0]["construction"] is True and line.IsReference
    assert nx.session.ActiveSketch is None


def test_polar_ellipse_arc_and_perimeter():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    ok = _curves(
        nx,
        sk,
        {"type": "ellipse", "center": [300, 100], "major_radius": 20, "minor_radius": 10,
         "rotation": 90, "start_angle": -45, "end_angle": 45},
    )  # fmt: skip
    assert ok.ok, ok
    row = ok.data["curves"][0]
    want = ellipse_point([300, 100], 20, 10, 90, -45)
    assert row["start"] == pytest.approx(want, abs=1e-6)
    assert math.dist(want, [300, 100]) == pytest.approx(12.649, abs=1e-3)  # polar, not parametric
    assert ellipse_perimeter(20, 10) == pytest.approx(96.884482, abs=1e-6)
    assert ellipse_perimeter(50, 1) == pytest.approx(200.191958, abs=1e-6)


@pytest.mark.parametrize(
    ("curve", "field"),
    [
        ({"type": "line", "start": [1, 1], "end": [1, 1]}, "/curves/0/end"),
        ({"type": "line", "start": [0, 0]}, "/curves/0/end"),
        ({"type": "line", "start": [0, 0], "end": [1, 0], "radius": 3}, "/curves/0/radius"),
        ({"type": "arc", "center": [0, 0], "radius": 1, "start_angle": 90, "end_angle": 0},
         "/curves/0/end_angle"),
        ({"type": "circle", "center": [0, 0], "radius": 0}, "/curves/0/radius"),
        ({"type": "ellipse", "center": [0, 0], "major_radius": 1, "minor_radius": 2},
         "/curves/0/minor_radius"),
        ({"type": "rectangle", "corner1": [0, 0], "corner2": [5, 0]}, "/curves/0/corner2"),
        ({"type": "polygon", "center": [0, 0], "sides": 2, "radius": 1}, "/curves/0/sides"),
        ({"type": "slot", "center": [0, 0], "width": 5, "height": 10}, "/curves/0/width"),
        ({"type": "rounded_rectangle", "center": [0, 0], "width": 10, "height": 4, "radius": 2},
         "/curves/0/radius"),
    ],
)  # fmt: skip
def test_invalid_curves_are_refused_before_nx(curve, field):
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    bad = _curves(nx, sk, curve)
    assert bad.error["category"] == "invalid_argument" and bad.error["field"] == field, bad
    assert bad.mutation_outcome == "not_started" and sk.geometry == []


def test_readback_mismatch_rolls_back_and_wrong_owner_is_refused():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    create = nx.part.Curves.CreateLine
    nx.part.Curves.CreateLine = lambda a, b: create(a, P([b.X + 1, b.Y, b.Z]))
    bad = _curves(nx, sk, {"type": "line", "start": [0, 0], "end": [10, 0]})
    assert bad.error["category"] == "verification_failed", bad
    assert bad.error["details"]["check"] == "curves/0/0/end"
    assert bad.mutation_outcome == "rolled_back" and list(nx.part.Curves) == []
    nx.part.Curves.CreateLine = create
    other = nx.sketch()
    nx.session.ActiveSketch = sk
    busy = _curves(nx, other, {"type": "line", "start": [0, 0], "end": [1, 0]})
    assert busy.error["code"] == "NX_SKETCH_ACTIVE"


def _edit(nx, sk, *edits):
    return run(nx, "nx_edit_sketch_curves", sketch=sk.ref, edits=list(edits))


def test_reshape_line_and_arc_then_construction_and_delete():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    arc = nx.arc(sk, (20, 0, 0), 5, 0, 90)
    gone = nx.line(sk, (0, 5, 0), (1, 5, 0))
    ok = _edit(
        nx,
        sk,
        {"type": "line", "curve": nx.cref(line), "start": [0, 0], "end": [0, 12]},
        {"type": "arc", "curve": nx.cref(arc), "center": [20, 0], "radius": 7,
         "start_angle": 0, "end_angle": 180},
        {"type": "construction", "curves": [nx.cref(line)], "on": True},
        {"type": "delete", "object": nx.cref(gone)},
    )  # fmt: skip
    assert ok.ok, ok
    assert ok.data["edits"][0]["curves"][0]["end"] == pytest.approx([0, 12])
    assert ok.data["edits"][1]["curves"][0]["radius"] == 7
    assert ok.data["edits"][2]["curves"][0]["construction"] is True
    assert ok.data["edits"][3]["deleted"] and gone not in sk.geometry
    assert ok.data["status"]["state"] == "under_constrained"


def test_constrained_line_that_ignores_the_new_shape_is_solver_failed():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    sk.constraints.append(Constraint(CT.Horizontal, [(line, "NotSet")], sk))
    line.SetEndpoints = lambda a, b: None  # the solver keeps it
    bad = _edit(nx, sk, {"type": "line", "curve": nx.cref(line), "start": [0, 0], "end": [5, 5]})
    assert bad.error["category"] == "solver_failed" and bad.mutation_outcome == "rolled_back"


def test_trim_and_extend_are_verified_on_the_geometry():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    boundary = nx.line(sk, (0, -5, 0), (0, 5, 0))
    cut = nx.line(sk, (-5, 0, 0), (5, 0, 0))
    short = nx.line(sk, (-5, 3, 0), (-2, 3, 0))

    def trim(b):
        cut.StartPoint = P([0, 0, 0])

    def extend(b):
        short.EndPoint = P([0, 3, 0])

    nx.on_commit("QuickTrimBuilder", trim)
    nx.on_commit("QuickExtendBuilder", extend)
    ok = _edit(
        nx,
        sk,
        {"type": "trim", "curve": nx.cref(cut), "boundaries": [nx.cref(boundary)], "pick": [-3, 0]},
        {"type": "extend", "curve": nx.cref(short), "boundaries": [nx.cref(boundary)],
         "pick": [-2, 3]},
    )  # fmt: skip
    assert ok.ok, ok
    assert ok.data["edits"][0]["curves"][0]["start"] == pytest.approx([0, 0])
    assert ok.data["edits"][1]["curves"][0]["end"] == pytest.approx([0, 3])
    nx.on_commit("QuickTrimBuilder", lambda b: None)  # NX left the piece in place
    bad = _edit(
        nx,
        sk,
        {"type": "trim", "curve": nx.cref(cut), "boundaries": [nx.cref(boundary)], "pick": [3, 0]},
    )
    assert bad.error["details"]["check"] == "edits/0/trimmed"
    self_bound = _edit(
        nx,
        sk,
        {"type": "trim", "curve": nx.cref(cut), "boundaries": [nx.cref(cut)], "pick": [3, 0]},
    )
    assert self_bound.error["field"] == "/edits/0/boundaries"


def test_edits_refuse_foreign_curves_and_bad_items():
    nx = sketch_nx()
    sk, other = nx.sketch(active=True), nx.sketch()
    foreign = nx.line(other, (0, 0, 0), (1, 0, 0))
    bad = _edit(nx, sk, {"type": "construction", "curves": [nx.cref(foreign)], "on": True})
    assert bad.error["category"] == "wrong_owner" and bad.error["field"] == "/edits/0/curves/0"
    extra = _edit(nx, sk, {"type": "delete", "object": nx.cref(foreign), "on": True})
    assert extra.error["field"] == "/edits/0/on" and extra.mutation_outcome == "not_started"
    assert isinstance(foreign, Line)
