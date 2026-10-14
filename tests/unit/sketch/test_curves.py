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


# ---- polyline and loops ------------------------------------------------------


def test_closed_polyline_makes_joined_lines_and_one_closed_loop():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    points = [[0, 0], [40, 0], [40, 10], [20, 30], [0, 10]]
    ok = _curves(nx, sk, {"type": "polyline", "points": points, "closed": True})
    assert ok.ok, ok
    rows = ok.data["curves"]
    assert len(rows) == 5 and all(r["type"] == "line" and r["item"] == 0 for r in rows)
    assert rows[-1]["start"] == pytest.approx([0, 10]) and rows[-1]["end"] == pytest.approx([0, 0])
    assert "closed" not in rows[0]  # a line never closes on itself; loops say it
    coincident = [c for c in sk.constraints if c.ConstraintType == CT.Coincident]
    assert len(coincident) == 5
    assert ok.data["loops"] == [{"closed": True, "curves": 5}]


def test_open_polyline_is_a_chain_with_two_open_ends():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    ok = _curves(nx, sk, {"type": "polyline", "points": [[0, 0], [10, 0], [10, 5], [20, 5]]})
    assert ok.ok, ok
    assert len(ok.data["curves"]) == 3
    assert len([c for c in sk.constraints if c.ConstraintType == CT.Coincident]) == 2
    assert ok.data["loops"] == [{"closed": False, "curves": 3, "open_ends": 2}]


def test_repeated_first_point_closes_the_polyline_once():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    square = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    ok = _curves(nx, sk, {"type": "polyline", "points": square, "closed": True})
    assert ok.ok, ok
    assert len(ok.data["curves"]) == 4 and ok.data["loops"] == [{"closed": True, "curves": 4}]


def test_loops_cover_the_whole_sketch_across_calls():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    _curves(nx, sk, {"type": "polyline", "points": [[0, 0], [10, 0], [10, 10]]})
    ok = _curves(
        nx,
        sk,
        {"type": "polyline", "points": [[10, 10], [0, 10], [0, 0]]},
        {"type": "circle", "center": [50, 0], "radius": 3},
    )
    assert ok.ok, ok
    assert ok.data["loops"] == [{"closed": True, "curves": 4}, {"closed": True, "curves": 1}]


@pytest.mark.parametrize(
    ("item", "field"),
    [
        ({"type": "polyline", "points": [[0, 0], [0, 0], [5, 5]]}, "/curves/0/points/1"),
        ({"type": "polyline", "points": [[0, 0], [5, 5]], "closed": True}, "/curves/0"),
        ({"type": "polyline", "points": [[0, 0]]}, "/curves/0/points"),
    ],
)
def test_bad_polylines_are_refused_before_nx(item, field):
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    bad = _curves(nx, sk, item)
    assert not bad.ok and bad.error["code"] == "NX_INVALID_ARGUMENT", bad
    assert bad.error["field"] == field and not sk.geometry


def test_loops_flag_branches_and_open_ends():
    from nx_mcp.kit.sketch import loops

    tee = [
        {"start": [0, 0], "end": [10, 0]},
        {"start": [10, 0], "end": [20, 0]},
        {"start": [10, 0], "end": [10, 10]},
    ]
    assert loops(tee) == [{"closed": False, "curves": 3, "open_ends": 3, "branched": True}]
    assert loops([{"start": [0, 0], "end": [0, 0], "closed": True}]) == [
        {"closed": True, "curves": 1}
    ]
    assert loops([]) == []


def test_circles_and_arcs_take_a_diameter_instead_of_a_radius():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    ok = _curves(
        nx,
        sk,
        {"type": "circle", "center": [0, 0], "diameter": 8},
        {"type": "arc", "center": [20, 0], "diameter": 10, "start_angle": 0, "end_angle": 90},
    )
    assert ok.ok, ok
    assert [r["radius"] for r in ok.data["curves"]] == pytest.approx([4, 5])


@pytest.mark.parametrize(
    ("curve", "field", "text"),
    [
        ({"type": "circle", "center": [0, 0], "radius": 4, "diameter": 8}, "/curves/0/diameter",
         "not both"),
        ({"type": "circle", "center": [0, 0]}, "/curves/0/radius", "radius (or diameter)"),
        ({"type": "arc", "center": [0, 0], "start_angle": 0, "end_angle": 90},
         "/curves/0/radius", "radius (or diameter)"),
    ],
)  # fmt: skip
def test_circle_size_needs_exactly_one_of_radius_and_diameter(curve, field, text):
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    bad = _curves(nx, sk, curve)
    assert bad.error["field"] == field and text in bad.error["message"], bad
    assert bad.mutation_outcome == "not_started" and sk.geometry == []


# ---- natural edits -----------------------------------------------------------------


def test_shape_edits_keep_omitted_fields_and_take_natural_types():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    circle = nx.arc(sk, (20, 0, 0), 5, 0, 360)
    arc = nx.arc(sk, (40, 0, 0), 5, 0, 90)
    ok = _edit(
        nx,
        sk,
        {"type": "Line", "curve": nx.cref(line), "end": [0, 12]},
        {"type": "circle", "curve": nx.cref(circle), "radius": 7},
        {"type": "ARC", "curve": nx.cref(arc), "center": [45, 0]},
    )
    assert ok.ok, ok
    rows = [e["curves"][0] for e in ok.data["edits"]]
    assert rows[0]["start"] == pytest.approx([0, 0]) and rows[0]["end"] == pytest.approx([0, 12])
    assert rows[1]["center"] == pytest.approx([20, 0]) and rows[1]["radius"] == 7
    assert rows[2]["center"] == pytest.approx([45, 0]) and rows[2]["end_angle"] == pytest.approx(90)
    half = _edit(nx, sk, {"type": "arc", "curve": nx.cref(arc), "start_angle": 0})
    assert half.error["field"] == "/edits/0/end_angle", half
    empty = _edit(nx, sk, {"type": "line", "curve": nx.cref(line)})
    assert empty.error["field"] == "/edits/0" and "Nothing to change" in empty.error["message"]
    wrong = _edit(nx, sk, {"type": "arc", "curve": nx.cref(line), "radius": 3})
    assert wrong.error["code"] == "NX_WRONG_GEOMETRY" and "type line" in wrong.error["hint"]


def test_extend_picks_the_end_facing_the_boundary_and_says_when_nothing_grew():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    boundary = nx.line(sk, (0, -5, 0), (0, 5, 0))
    short = nx.line(sk, (-5, 3, 0), (-2, 3, 0))
    nx.on_commit("QuickExtendBuilder", lambda b: setattr(short, "EndPoint", P([0, 3, 0])))
    ok = _edit(
        nx, sk, {"type": "extend", "curve": nx.cref(short), "boundaries": [nx.cref(boundary)]}
    )
    assert ok.ok, ok
    pick = nx.last("QuickExtendBuilder").ExtendedCurves.Add.calls[-1][0][2]
    assert pytest.approx([-2.15, 3]) == [pick.X, pick.Y]
    nx.on_commit("QuickExtendBuilder", lambda b: None)  # no boundary on the extension
    bad = _edit(
        nx, sk, {"type": "extend", "curve": nx.cref(short), "boundaries": [nx.cref(boundary)],
                 "pick": [-4, 3]},
    )  # fmt: skip
    assert bad.error["category"] == "invalid_argument", bad
    assert bad.error["field"] == "/edits/0/boundaries" and "did not extend" in bad.error["message"]
    assert bad.mutation_outcome == "rolled_back"


def test_trim_that_would_delete_the_whole_curve_is_refused():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    boundary = nx.line(sk, (40, 0, 0), (40, 10, 0))
    cut = nx.line(sk, (-5, -5, 0), (41, -5, 0))
    nx.on_commit("QuickTrimBuilder", lambda b: sk.geometry.remove(cut))  # NX deletes it
    bad = _edit(
        nx, sk, {"type": "trim", "curve": nx.cref(cut), "boundaries": [nx.cref(boundary)],
                 "pick": [41, -5]},
    )  # fmt: skip
    assert bad.error["category"] == "invalid_argument", bad
    assert "No boundary crosses" in bad.error["message"] and "delete" in bad.error["hint"]
    assert bad.mutation_outcome == "rolled_back" and cut in sk.geometry
    no_pick = _edit(
        nx, sk, {"type": "trim", "curve": nx.cref(cut), "boundaries": [nx.cref(boundary)]}
    )
    assert no_pick.error["field"] == "/edits/0/pick", no_pick
    assert no_pick.error["hint"].startswith("trim takes curve, boundaries, pick (required)")


# ---------------------------------------------------------------------------
# Splines

AIRFOIL = [[0, 0], [10, 3], [25, 4], [40, 2], [50, 0]]


def test_open_spline_passes_through_its_points_with_the_end_tangent():
    nx = sketch_nx()
    sk = nx.sketch(origin=(0, 0, 5), active=True)
    ok = _curves(nx, sk, {"type": "spline", "points": AIRFOIL, "start_tangent": [10, 3]})
    assert ok.ok, ok
    b = nx.last("StudioSplineBuilderEx")
    assert b.IsAssociative is False and b.Degree == 3 and b.IsPeriodic is False
    assert b.Type == "ThroughPoints" and b.destroyed
    data = [args[0] for args, _kw in b.ConstraintManager.Append.calls]
    assert [d.Point.Coordinates.Z for d in data] == [5.0] * 5  # on the sketch plane
    assert pytest.approx(10 / math.hypot(10, 3)) == data[0].TangentDirection.Vector.X
    row = ok.data["curves"][0]
    assert row["type"] == "spline" and row["start"] == pytest.approx([0, 0])
    assert row["end"] == pytest.approx([50, 0]) and sk.geometry == [b.Curve]
    assert ok.data["loops"] == [{"closed": False, "curves": 1, "open_ends": 2}]


def test_closed_spline_is_periodic_and_makes_a_closed_loop():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    ring = [[0, 0], [20, 0], [20, 10], [0, 10], [0, 0]]  # the repeated first point is dropped
    ok = _curves(nx, sk, {"type": "spline", "points": ring, "closed": True})
    assert ok.ok, ok
    b = nx.last("StudioSplineBuilderEx")
    assert b.IsPeriodic is True and len(b.ConstraintManager.Append.calls) == 4
    assert ok.data["loops"] == [{"closed": True, "curves": 1}]


def test_one_call_sketch_takes_a_spline_profile():
    nx = sketch_nx()
    ok = run(nx, "nx_create_sketch", placement={"principal": "xy"}, finish=True,
             curves=[{"type": "spline", "points": AIRFOIL[:4], "closed": True}])  # fmt: skip
    assert ok.ok, ok
    assert ok.data["loops"] == [{"closed": True, "curves": 1}] and ok.data["active"] is False


def test_spline_tangent_nx_did_not_take_rolls_back():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    bad = _curves(nx, sk, {"type": "spline", "points": AIRFOIL, "end_tangent": [0, 1]})
    assert bad.error["code"] == "NX_VERIFICATION_FAILED", bad
    assert bad.error["details"]["check"] == "curves/0/0/tangent"
    assert bad.mutation_outcome == "rolled_back" and nx.live_builders == 0


@pytest.mark.parametrize(
    ("item", "field"),
    [
        ({"points": [[0, 0], [0, 0], [5, 5]]}, "/curves/0/points/1"),
        ({"points": [[0, 0], [5, 5]], "closed": True}, "/curves/0"),
        ({"points": AIRFOIL, "closed": True, "start_tangent": [1, 0]}, "/curves/0/start_tangent"),
        ({"points": AIRFOIL, "end_tangent": [0, 0]}, "/curves/0/end_tangent"),
        ({"points": AIRFOIL, "radius": 3}, "/curves/0/radius"),
    ],
)
def test_spline_errors_are_refused_before_nx(item, field):
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    bad = _curves(nx, sk, {"type": "spline", **item})
    assert bad.error["field"] == field and bad.mutation_outcome == "not_started", bad
    assert not any(b.kind == "StudioSplineBuilderEx" for b in nx.builders)


def test_open_spline_repeating_its_first_point_is_a_closed_loop():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    lens = [[0, 0], [10, 4], [20, 0], [10, -4], [0, 0]]
    ok = _curves(nx, sk, {"type": "spline", "points": lens})
    assert ok.ok, ok
    assert nx.last("StudioSplineBuilderEx").IsPeriodic is False
    assert ok.data["loops"] == [{"closed": True, "curves": 1}]
