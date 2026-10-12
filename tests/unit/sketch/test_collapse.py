"""Constraint requests NX can only solve by deleting curves are refused before NX solves.

Live NX 2406 (2026-10-12): such a solve made NX open its modal "Edit Sketch: The edit
causes curves or dimensions to be deleted." box on the builder's Commit or Destroy,
which blocked NX and every caller for 34 minutes. Each refused case below was seen to
delete curves in batch NX (kit/sketch.py, ``collapse_risks``); each allowed one was not.
"""

import pytest

from tests.kit import run
from tests.unit.sketch.fakes import sketch_nx


def _constrain(nx, sk, *items):
    return run(nx, "nx_create_sketch_constraints", sketch=sk.ref, constraints=list(items))


def _rectangle():
    """The incident's sketch: a 10 x 10 rectangle (bottom, right, top, left), a circle
    inside, two free parallel lines and a line crossing the bottom side."""
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    corners = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)]
    sides = [nx.line(sk, corners[i], corners[(i + 1) % 4]) for i in range(4)]
    circle = nx.arc(sk, (5, 5, 0), 2, 0, 360)
    free = [nx.line(sk, (30, 0, 0), (40, 0, 0)), nx.line(sk, (30, 5, 0), (40, 5, 0))]
    crossing = nx.line(sk, (3, -5, 0), (6, 5, 0))
    return nx, sk, sides, circle, free, crossing


def _point(nx, curve, which):
    return {"curve": nx.cref(curve), "point": which}


def _on(kind, nx, curve, which, target):
    return {"type": kind, "points": [_point(nx, curve, which)], "curves": [nx.cref(target)]}


def _refused(result, field, words):
    assert not result.ok, result
    error = result.error
    assert error["code"] == "NX_SKETCH_OVERCONSTRAINED" and error["field"] == field, error
    assert words in error["details"]["reason"], error
    assert "not sent to NX" in error["message"]
    assert result.mutation_outcome == "rolled_back"


def test_the_incident_midpoint_on_the_opposite_side_is_refused_before_nx_solves():
    nx, sk, (bottom, _r, top, _l), circle, _f, _c = _rectangle()
    held = _constrain(nx, sk, _on("point_on_curve", nx, circle, "center", bottom))
    assert held.ok, held
    drags = sum(b.kind == "DragGeometryBuilder" for b in nx.builders)
    bad = _constrain(nx, sk, _on("midpoint", nx, circle, "center", top))
    _refused(bad, "/constraints/0", "parallel line 10 away")
    assert sum(b.kind == "DragGeometryBuilder" for b in nx.builders) == drags  # no NX solve
    assert bad.error["details"]["curves"] == [nx.cref(bottom)]
    held_by = bad.error["details"]["constraint"]  # the point-on-curve that holds it
    assert held_by in bad.error["hint"] and "nx_edit_sketch_curves" in bad.error["hint"]


def test_both_holds_in_one_call_are_refused_at_the_second_item():
    nx, sk, (bottom, _r, top, _l), circle, _f, _c = _rectangle()
    bad = _constrain(
        nx,
        sk,
        _on("point_on_curve", nx, circle, "center", bottom),
        _on("point_on_curve", nx, circle, "center", top),
    )
    _refused(bad, "/constraints/1", "parallel line")
    assert sk.constraints == []  # the first item rolled back too


def test_parallel_lines_no_curve_joins_stay_allowed():
    nx, sk, _s, circle, (a, b), _c = _rectangle()
    ok = _constrain(nx, sk, _on("point_on_curve", nx, circle, "center", a))
    assert ok.ok, ok
    risks_free = _constrain(nx, sk, _on("point_on_curve", nx, circle, "center", b))
    assert risks_free.error is None or risks_free.error["code"] != "NX_SKETCH_OVERCONSTRAINED"


def test_a_point_held_on_a_side_cannot_go_on_the_side_meeting_it_at_a_corner():
    nx, sk, (bottom, right, _t, _l), circle, _f, crossing = _rectangle()
    assert _constrain(nx, sk, _on("point_on_curve", nx, circle, "center", bottom)).ok
    for kind in ("point_on_curve", "midpoint"):
        bad = _constrain(nx, sk, _on(kind, nx, circle, "center", right))
        _refused(bad, "/constraints/0", "meets this one at a corner")
    crossing_ok = _constrain(nx, sk, _on("point_on_curve", nx, circle, "center", crossing))
    assert crossing_ok.error is None or crossing_ok.error["code"] != "NX_SKETCH_OVERCONSTRAINED"


@pytest.mark.parametrize("kind", ["point_on_curve", "midpoint"])
def test_a_corner_onto_the_far_side_would_fold_a_side(kind):
    nx, sk, (_b, right, _t, left), _c, (free, _f2), _x = _rectangle()
    bad = _constrain(nx, sk, _on(kind, nx, right, "start", left))
    _refused(bad, "/constraints/0", "fold onto the line")
    ok = _constrain(nx, sk, _on(kind, nx, right, "start", free))  # a free line: fine
    assert ok.error is None or ok.error["code"] != "NX_SKETCH_OVERCONSTRAINED"


def test_a_midpoint_at_one_end_of_the_line_is_refused():
    nx, sk, (bottom, right, _t, _l), _c, _f, _x = _rectangle()
    bad = _constrain(nx, sk, _on("midpoint", nx, right, "start", bottom))
    _refused(bad, "/constraints/0", "end of this line")


def test_coincident_ends_of_one_curve_are_refused():
    nx, sk, (bottom, right, top, _l), _c, (free, _f2), _x = _rectangle()
    arc = nx.arc(sk, (60, 0, 0), 5, 0, 90)
    for a, b in (
        (_point(nx, free, "start"), _point(nx, free, "end")),  # a line's own ends
        (_point(nx, arc, "start"), _point(nx, arc, "end")),  # an arc's own ends
        (_point(nx, top, "start"), _point(nx, bottom, "end")),  # both ends of the right side
    ):
        bad = _constrain(nx, sk, {"type": "coincident", "points": [a, b]})
        _refused(bad, "/constraints/0", "ends of the same curve")
    corners = _constrain(  # opposite corners: no curve between them collapses
        nx,
        sk,
        {"type": "coincident", "points": [_point(nx, bottom, "start"), _point(nx, right, "end")]},
    )
    assert corners.ok, corners


def test_a_solve_that_still_deleted_a_curve_rolls_back():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 2, 0))
    victim = nx.line(sk, (20, 0, 0), (30, 0, 0))
    victim.JournalIdentifier = "Line2"
    commit = nx.hooks["DragGeometryBuilder"]

    def deleting(builder):  # batch NX: the solve deletes a collapsed curve silently
        result = commit(builder)
        sk.geometry.remove(victim)
        return result

    nx.on_commit("DragGeometryBuilder", deleting)
    bad = _constrain(nx, sk, {"type": "horizontal", "curves": [nx.cref(line)]})
    assert bad.error["code"] == "NX_SKETCH_OVERCONSTRAINED", bad
    assert bad.error["details"]["deleted_curves"] == ["Line2"]
    assert "deleting 1 sketch curve" in bad.error["message"]
    assert bad.mutation_outcome == "rolled_back"
