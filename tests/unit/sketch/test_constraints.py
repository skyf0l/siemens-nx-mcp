"""nx_create_sketch_constraints and nx_create_sketch_dimensions on the fake kit."""

import pytest

from tests.kit import NXException, run
from tests.unit.sketch.fakes import CT, Constraint, sketch_nx


def _constrain(nx, sk, *items):
    return run(nx, "nx_create_sketch_constraints", sketch=sk.ref, constraints=list(items))


def _dimension(nx, sk, *items):
    return run(nx, "nx_create_sketch_dimensions", sketch=sk.ref, dimensions=list(items))


def test_horizontal_is_solved_by_a_drag_and_checked():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 30, 0), (10, 32, 0))
    ok = _constrain(nx, sk, {"type": "horizontal", "curves": [nx.cref(line)]})
    assert ok.ok, ok
    row = ok.data["constraints"][0]
    assert row["type"] == "horizontal" and row["residual"] == 0 and row["drag_status"] == "Updated"
    assert line.StartPoint.Y == 32 and len(row["constraints"]) == 1
    assert nx.last("DragGeometryBuilder").destroyed and ok.data["status"]["state"]


def test_unsolved_constraint_rolls_back_as_solver_failed():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 2, 0))
    sk.solve = lambda: None  # the drag did not satisfy it
    nx.drag_status = 5
    bad = _constrain(nx, sk, {"type": "vertical", "curves": [nx.cref(line)]})
    assert bad.error["category"] == "solver_failed" and bad.error["field"] == "/constraints/0"
    assert bad.mutation_outcome == "rolled_back" and sk.constraints == []


def test_relations_move_the_second_curve():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    a = nx.line(sk, (0, 5, 0), (10, 5, 0))
    b = nx.line(sk, (0, 10, 0), (10, 12, 0))
    c = nx.line(sk, (20, 0, 0), (26, 0, 0))
    r1, r2 = nx.arc(sk, (40, 0, 0), 3, 0, 360), nx.arc(sk, (50, 0, 0), 2, 0, 360)
    d = nx.line(sk, (12, 7, 0), (15, 9, 0))
    ok = _constrain(
        nx,
        sk,
        {"type": "parallel", "curves": [nx.cref(a), nx.cref(b)]},
        {"type": "equal_length", "curves": [nx.cref(a), nx.cref(c)]},
        {"type": "equal_radius", "curves": [nx.cref(r1), nx.cref(r2)]},
        {"type": "concentric", "curves": [nx.cref(r1), nx.cref(r2)]},
        {"type": "coincident", "points": [
            {"curve": nx.cref(a), "point": "end"}, {"curve": nx.cref(d), "point": "start"}]},
    )  # fmt: skip
    assert ok.ok, ok
    assert pytest.approx(10) == b.EndPoint.Y and c.GetLength() == pytest.approx(10)
    assert r2.Radius == 3 and (r2.CenterPoint.X, r2.CenterPoint.Y) == (40, 0)
    assert (d.StartPoint.X, d.StartPoint.Y) == (10, 5)
    assert [r["type"] for r in ok.data["constraints"]] == [
        "parallel", "equal_length", "equal_radius", "concentric", "coincident",
    ]  # fmt: skip


def test_tangent_line_and_circle():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (-10, 0, 0), (10, 0, 0))
    circle = nx.arc(sk, (0, 3, 0), 2, 0, 360)
    ok = _constrain(nx, sk, {"type": "tangent", "curves": [nx.cref(line), nx.cref(circle)]})
    assert ok.ok, ok
    assert pytest.approx(2) == circle.CenterPoint.Y


def test_fixed_curve_uses_fully_fixed_and_point_fix_uses_the_vertex():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    other = nx.line(sk, (0, 5, 0), (10, 5, 0))
    ok = _constrain(
        nx,
        sk,
        {"type": "fixed", "curves": [nx.cref(line)]},
        {"type": "fixed", "points": [{"curve": nx.cref(other), "point": "start"}]},
    )
    assert ok.ok, ok
    assert len(ok.data["constraints"][0]["constraints"]) == 2  # curve and its points
    fixed = [c for c in sk.constraints if c.ConstraintType == CT.Fixed]
    assert fixed[-1].geometry == [(other, "StartVertex")]
    both = _constrain(
        nx,
        sk,
        {"type": "fixed", "curves": [nx.cref(line)], "points": [
            {"curve": nx.cref(line), "point": "end"}]},
    )  # fmt: skip
    assert both.error["field"] == "/constraints/0/curves"


def test_fixed_line_does_not_move_when_dimensioned():
    """The reported bug: a fixed line still moved when a later dimension was added."""
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    ok = _constrain(nx, sk, {"type": "fixed", "curves": [nx.cref(line)]})
    assert ok.ok, ok
    bad = _dimension(nx, sk, {"kind": "length", "first": nx.cref(line), "value": 20})
    assert bad.error["category"] == "solver_failed", bad
    assert bad.mutation_outcome == "rolled_back"
    assert (line.EndPoint.X, line.EndPoint.Y) == (10, 0)


def test_a_plain_fixed_line_that_slides_is_caught_by_the_guard():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    sk.constraints.append(Constraint(CT.Fixed, [(line, "NotSet")], sk))  # legacy single fix
    bad = _dimension(nx, sk, {"kind": "length", "first": nx.cref(line), "value": 20})
    assert bad.error["code"] == "NX_CONSTRAINT_UNSATISFIED", bad
    assert bad.error["details"]["fixed_curves_moved"][0]["curve"] == nx.cref(line)


def test_midpoint_and_point_on_curve_are_dragged_into_place():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    target = nx.line(sk, (0, 0, 0), (40, 0, 0))
    circle = nx.arc(sk, (5, 7, 0), 2, 0, 360)
    free = nx.line(sk, (30, 9, 0), (35, 12, 0))
    ok = _constrain(
        nx,
        sk,
        {"type": "midpoint", "points": [{"curve": nx.cref(circle), "point": "center"}],
         "curves": [nx.cref(target)]},
        {"type": "point_on_curve", "points": [{"curve": nx.cref(free), "point": "start"}],
         "curves": [nx.cref(target)]},
    )  # fmt: skip
    assert ok.ok, ok
    assert (circle.CenterPoint.X, circle.CenterPoint.Y) == (20, 0)
    assert pytest.approx(0) == free.StartPoint.Y
    kinds = [c.ConstraintType for c in sk.constraints]
    assert kinds.count(CT.Midpoint) == 1 and kinds.count(CT.PointOnCurve) == 2


def test_constraint_argument_rules():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    arc = nx.arc(sk, (0, 0, 0), 5, 0, 360)
    cases = [
        ({"type": "parallel", "curves": [nx.cref(line)]}, "/constraints/0/curves"),
        ({"type": "horizontal", "curves": [nx.cref(arc)]}, "/constraints/0/curves/0"),
        ({"type": "equal_radius", "curves": [nx.cref(line), nx.cref(arc)]},
         "/constraints/0/curves/0"),
        ({"type": "coincident", "points": [{"curve": nx.cref(arc), "point": "start"},
                                           {"curve": nx.cref(line), "point": "end"}]},
         "/constraints/0/points/0/point"),
        ({"type": "tangent", "curves": [nx.cref(line), nx.cref(line)]}, "/constraints/0/curves"),
        ({"type": "symmetric", "curves": [nx.cref(line), nx.cref(line)]},
         "/constraints/0/centerline"),
    ]  # fmt: skip
    for item, field in cases:
        bad = _constrain(nx, sk, item)
        assert bad.error["field"] == field, (item, bad.error)
        assert bad.mutation_outcome in ("not_started", "rolled_back")
    assert sk.constraints == []


def test_native_duplicate_relation_is_solver_failed():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    sk.refuse = NXException(910023, "Persistent relation already exists")
    bad = _constrain(nx, sk, {"type": "horizontal", "curves": [nx.cref(line)]})
    assert bad.error["code"] == "NX_SKETCH_OVERCONSTRAINED" and bad.error["nx_code"] == 910023


def test_dimensions_drive_and_return_expressions():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    arc = nx.arc(sk, (30, 0, 0), 5, 0, 360)
    ok = _dimension(
        nx,
        sk,
        {"kind": "length", "first": nx.cref(line), "value": 40},
        {"kind": "radius", "first": nx.cref(arc), "value": 8},
        {"kind": "diameter", "first": nx.cref(arc), "reference": True},
    )
    assert ok.ok, ok
    rows = ok.data["dimensions"]
    assert rows[0]["value_before"] == 10 and rows[0]["measured"] == pytest.approx(40)
    assert rows[0]["expression"]["value"] == 40 and rows[0]["expression"]["name"].startswith("p")
    assert rows[1]["measured"] == 8 and pytest.approx(40) == line.EndPoint.X
    assert rows[2]["reference"] is True and rows[2]["measured"] == 16
    assert "expression" not in rows[2]  # NX 2406: reference dimensions have no expression
    assert "LocalUpdate" in sk.updates  # the active sketch is solved now (NX 2406)


def test_point_dimensions_measure_and_keep_without_a_value():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    a = nx.line(sk, (0, 0, 0), (10, 0, 0))
    b = nx.line(sk, (25, 5, 0), (30, 5, 0))
    ok = _dimension(
        nx,
        sk,
        {"kind": "horizontal", "first": {"curve": nx.cref(a), "point": "end"},
         "second": {"curve": nx.cref(b), "point": "start"}},
        {"kind": "perpendicular_distance", "first": nx.cref(a),
         "second": {"curve": nx.cref(b), "point": "start"}, "value": 12},
    )  # fmt: skip
    assert ok.ok, ok
    assert ok.data["dimensions"][0]["measured"] == 15
    assert ok.data["dimensions"][1]["measured"] == pytest.approx(12)


@pytest.mark.parametrize(
    ("item", "field"),
    [
        ({"kind": "angle", "first": "LINE", "second": "LINE", "value": 180}, "/dimensions/0/value"),
        ({"kind": "length", "first": "LINE", "value": 0}, "/dimensions/0/value"),
        ({"kind": "radius", "first": "LINE", "value": 3}, "/dimensions/0/first"),
        ({"kind": "distance", "first": "LINE"}, "/dimensions/0/first"),
        ({"kind": "length", "first": "LINE", "second": "LINE"}, "/dimensions/0/second"),
        ({"kind": "parallel_distance", "first": "LINE", "second": "OTHER"},
         "/dimensions/0/second"),
        ({"kind": "length", "first": "LINE", "value": 25, "reference": True},
         "/dimensions/0/value"),
    ],
)  # fmt: skip
def test_dimension_argument_rules(item, field):
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    other = nx.line(sk, (0, 5, 0), (10, 9, 0))
    names = {"LINE": nx.cref(line), "OTHER": nx.cref(other)}
    item = {k: names.get(v, v) if isinstance(v, str) else v for k, v in item.items()}
    bad = _dimension(nx, sk, item)
    assert bad.error["field"] == field, bad.error
    assert sk.constraints == []


def test_unreachable_value_rolls_back():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    sk.over = True  # NX cannot reach any value: OverConstrained, geometry kept
    bad = _dimension(nx, sk, {"kind": "length", "first": nx.cref(line), "value": 25})
    assert bad.error["category"] == "solver_failed" and bad.mutation_outcome == "rolled_back"
