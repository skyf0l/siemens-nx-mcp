"""In-sketch changes are undone by compensation: created objects deleted, moved curves set
back, removed constraints created again (domains/sketch/undo.py) on the fake kit."""

import pytest

from tests.kit import run
from tests.unit.sketch.fakes import CT, Constraint, P, sketch_nx


def _undo(nx, op=None):
    args = {"mode": "compensate"}
    if op is not None:
        args["operation"] = op
    return run(nx, "nx_undo_operation", **args)


def test_a_constraint_is_deleted_and_the_moved_line_set_back():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 30, 0), (10, 32, 0))
    ok = run(nx, "nx_create_sketch_constraints", sketch=sk.ref,
             constraints=[{"type": "horizontal", "curves": [nx.cref(line)]}])  # fmt: skip
    assert ok.ok and line.StartPoint.Y == 32, ok
    undo = _undo(nx)
    assert undo.ok and undo.data["compensated"], undo
    assert sk.constraints == [] and (line.StartPoint.Y, line.EndPoint.Y) == (30, 32)
    assert (
        "1 constraint(s) or dimension(s) deleted, 1 curve(s) set back" in undo.data["restored"][0]
    )


def test_a_curve_moved_since_is_never_overwritten():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 30, 0), (10, 32, 0))
    ok = run(nx, "nx_create_sketch_constraints", sketch=sk.ref,
             constraints=[{"type": "horizontal", "curves": [nx.cref(line)]}])  # fmt: skip
    assert ok.ok
    line.SetEndpoints(P([0, 40, 0]), P([10, 40, 0]))  # the engineer moved it since
    refused = _undo(nx)
    assert refused.error["code"] == "NX_DEPENDENCY_ORDER", refused
    assert len(sk.constraints) == 1 and line.StartPoint.Y == 40


def test_a_dimension_goes_with_its_expression():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    ok = run(nx, "nx_create_sketch_dimensions", sketch=sk.ref,
             dimensions=[{"kind": "length", "first": nx.cref(line), "value": 40}])  # fmt: skip
    assert ok.ok and pytest.approx(40) == line.EndPoint.X, ok
    undo = _undo(nx)
    assert undo.ok, undo
    assert sk.constraints == [] and pytest.approx(10) == line.EndPoint.X
    assert list(nx.part.Expressions) == []


def test_a_fillet_is_deleted_and_the_corner_rebuilt_with_its_coincident():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    bottom = nx.line(sk, (50, 0, 0), (90, 0, 0))
    right = nx.line(sk, (90, 0, 0), (90, 30, 0))
    corner = Constraint(CT.Coincident, [(bottom, "EndVertex"), (right, "StartVertex")], sk)
    sk.constraints.append(corner)
    plain = nx.fillet

    def fillet(sketch, a, b, radius):  # NX trims the lines and drops their coincident
        sketch.constraints.remove(corner)
        return plain(sketch, a, b, radius)

    nx.fillet = fillet
    ok = run(nx, "nx_create_sketch_corner", sketch=sk.ref, first=nx.cref(bottom),
             second=nx.cref(right), kind="fillet", radius=5)  # fmt: skip
    assert ok.ok and pytest.approx(85) == bottom.EndPoint.X, ok
    undo = _undo(nx)
    assert undo.ok, undo
    assert sk.geometry == [bottom, right]
    assert (bottom.EndPoint.X, right.StartPoint.Y) == (90, 0)
    (again,) = sk.constraints
    assert again.ConstraintType == CT.Coincident
    assert again.geometry == [(bottom, "EndVertex"), (right, "StartVertex")]
    assert "1 removed constraint(s) created again" in undo.data["restored"][0]


def test_offset_projection_and_added_curves_are_deleted():
    nx = sketch_nx()
    sk = nx.sketch(origin=(0, 0, 0), active=True)
    base = nx.line(sk, (0, 0, 0), (10, 0, 0))
    nx.on_commit("SketchOffsetBuilder", lambda b: nx.line(sk, (0, 3, 0), (10, 3, 0)))
    offset = run(nx, "nx_create_sketch_copy", sketch=sk.ref, curves=[nx.cref(base)],
                 mode="offset", distance=3)  # fmt: skip
    assert offset.ok, offset
    edge = nx.model_edge((140, -10, 0), (160, -10, 0))

    def project(b):
        object.__setattr__(b, "GetOperation", lambda: None)
        nx.line(sk, (140, -10, 0), (160, -10, 0))

    nx.on_commit("ProjectBuilder", project)
    projected = run(nx, "nx_create_sketch_projection", sketch=sk.ref, objects=[edge.ref])
    assert projected.ok, projected
    added = run(nx, "nx_create_sketch_curves", sketch=sk.ref,
                curves=[{"type": "circle", "center": [0, 50], "radius": 4}])  # fmt: skip
    assert added.ok and len(sk.geometry) == 4, added
    for op in (added, projected, offset):
        undo = _undo(nx, op.operation_id)
        assert undo.ok and undo.data["compensated"], undo
    assert sk.geometry == [base]


def test_the_undo_needs_the_sketch_part_displayed_and_no_other_open_sketch():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 30, 0), (10, 32, 0))
    ok = run(nx, "nx_create_sketch_constraints", sketch=sk.ref,
             constraints=[{"type": "horizontal", "curves": [nx.cref(line)]}])  # fmt: skip
    assert ok.ok
    shown = nx.session.Parts.Display
    nx.session.Parts.Display = None
    hidden = _undo(nx)
    assert hidden.error["code"] == "NX_WRONG_PART_CONTEXT", hidden
    assert "nx_activate_part" in hidden.error["message"] + str(hidden.error.get("hint"))
    nx.session.Parts.Display = shown
    sk.Deactivate()
    other = nx.sketch(active=True)
    busy = _undo(nx)
    assert busy.error["code"] == "NX_SKETCH_ACTIVE", busy
    other.Deactivate()
    assert _undo(nx).ok and sk.constraints == []


def test_a_call_that_deleted_curves_keeps_nothing_to_set_back():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    gone = run(nx, "nx_edit_sketch_curves", sketch=sk.ref,
               edits=[{"type": "delete", "object": nx.cref(line)}])  # fmt: skip
    assert gone.ok, gone
    refused = _undo(nx)
    assert not refused.ok and sk.geometry == [], refused


def test_a_reshape_is_set_back():
    nx = sketch_nx()
    sk = nx.sketch(active=True)
    line = nx.line(sk, (0, 0, 0), (10, 0, 0))
    ok = run(nx, "nx_edit_sketch_curves", sketch=sk.ref,
             edits=[{"type": "line", "curve": nx.cref(line), "end": [20, 5]}])  # fmt: skip
    assert ok.ok and line.EndPoint.X == 20, ok
    assert _undo(nx).ok and (line.EndPoint.X, line.EndPoint.Y) == (10, 0)


def _host_kinds(nx, monkeypatch):
    """Infer kinds from the classes, like the NX host (never from the registry): the fake
    ``Constraint`` stands for NXOpen's ``SketchConstraint``."""
    from nx_mcp.core.errors import fail
    from nx_mcp.host.services import _KIND_BY_CLASS
    from tests.kit import kernel

    names = {path.rsplit(".", 1)[-1]: kind for path, kind in _KIND_BY_CLASS}
    names["Constraint"] = names["SketchConstraint"]

    def infer_kind(obj, default="error"):
        for cls in type(obj).__mro__:
            if cls.__name__ in names:
                return names[cls.__name__]
        if default == "error":
            fail("NX_ERROR", f"Cannot infer the reference kind of {type(obj).__name__}")
        return default

    monkeypatch.setattr(kernel.services(nx), "infer_kind", infer_kind)


def test_the_undo_names_curves_and_constraints_by_their_nx_class(monkeypatch):
    """A fillet's undo lists the curves and constraints it made and the coincident it
    removed without asking the registry for their kind."""
    nx = sketch_nx()
    _host_kinds(nx, monkeypatch)
    sk = nx.sketch(active=True)
    bottom = nx.line(sk, (50, 0, 0), (90, 0, 0))
    right = nx.line(sk, (90, 0, 0), (90, 30, 0))
    corner = Constraint(CT.Coincident, [(bottom, "EndVertex"), (right, "StartVertex")], sk)
    sk.constraints.append(corner)
    plain = nx.fillet

    def fillet(sketch, a, b, radius):
        sketch.constraints.remove(corner)
        return plain(sketch, a, b, radius)

    nx.fillet = fillet
    ok = run(nx, "nx_create_sketch_corner", sketch=sk.ref, first=nx.cref(bottom),
             second=nx.cref(right), kind="fillet", radius=5)  # fmt: skip
    assert ok.ok, ok
    undo = _undo(nx)
    assert undo.ok and undo.data["compensated"], undo
    assert sk.geometry == [bottom, right] and (bottom.EndPoint.X, right.StartPoint.Y) == (90, 0)
    (again,) = sk.constraints
    assert again.ConstraintType == CT.Coincident


def test_curves_added_to_a_sketch_of_the_same_batch_go_with_the_sketch():
    """Undoing a batch that made a sketch, drew in it and finished it deletes the sketch:
    the curves step's in-sketch change is not set back first (no sketch activation, so a
    part that is not displayed needs none)."""
    nx = sketch_nx()
    steps = [
        {"tool": "nx_create_sketch", "arguments": {"placement": "xy", "name": "BATCH_SK"}},
        {"tool": "nx_create_sketch_curves", "arguments": {"sketch": "$0.data.sketch",
         "curves": [{"type": "line", "start": [0, 0], "end": [10, 0]}]}},
        {"tool": "nx_finish_sketch", "arguments": {"sketch": "$0.data.sketch"}},
    ]  # fmt: skip
    batch = run(nx, "nx_run_batch", operations=steps)
    assert batch.ok, batch
    assert len(nx.part.Sketches) == 1
    undo = _undo(nx, batch.operation_id)
    assert undo.ok and undo.data["compensated"], undo
    assert not any("sketch" in text for text in undo.data.get("restored") or []), undo
    assert len(nx.part.Sketches) == 0
