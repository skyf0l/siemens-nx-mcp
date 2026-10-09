"""Sketch activation, local <-> part coordinates, solving and curve ownership.

    with sketch.editing(tx, ref) as sk:
        line = tx.part.Curves.CreateLine(sk.point(p0), sk.point(p1))
        ...
        state = sketch.solve(tx, sk, edited_expressions)

NX 2406 findings encoded here:

- ``Sketch.Update()`` (and a model update) leaves an *active* sketch unsolved
  after a driving expression changed; ``LocalUpdate()`` solves it now.
- ``GetStatus()`` returns NX's degrees-of-freedom indicator, which on 2406 is
  the number of curves that are not fully constrained, not scalar DOF.
- Solver status needs the work region set to the entire sketch; that change is
  made under an invisible undo mark and undone.
"""

from __future__ import annotations

import contextlib

from nx_mcp.core.errors import fail
from nx_mcp.kit import geom
from nx_mcp.kit.result import enum_name

FAILED_STATUSES = frozenset({"OverConstrained", "InconsistentlyConstrained"})
SOLVED_DOF_STATUSES = frozenset({"UnderConstrained", "WellConstrained"})


def frame(sketch):
    """Sketch frame in part coordinates: origin, x_axis, y_axis, normal."""
    m = sketch.Orientation.Element
    return {
        "origin": geom.xyz(sketch.Origin),
        "x_axis": [float(m.Xx), float(m.Xy), float(m.Xz)],
        "y_axis": [float(m.Yx), float(m.Yy), float(m.Yz)],
        "normal": [float(m.Zx), float(m.Zy), float(m.Zz)],
    }


def to_part(f, p2):
    """Part (work-part absolute) coordinates of a sketch-local [x, y]."""
    return [f["origin"][i] + p2[0] * f["x_axis"][i] + p2[1] * f["y_axis"][i] for i in range(3)]


def to_local(f, p3):
    """Sketch-local [x, y] of a part point (list or anything with X, Y, Z)."""
    d = geom.sub(geom.xyz(p3) if hasattr(p3, "X") else list(p3), f["origin"])
    return [geom.dot(d, f["x_axis"]), geom.dot(d, f["y_axis"])]


def height(f, p3):
    """Signed distance of a part point from the sketch plane."""
    d = geom.sub(geom.xyz(p3) if hasattr(p3, "X") else list(p3), f["origin"])
    return geom.dot(d, f["normal"])


class Editing:
    """An activated sketch: its object, frame and coordinate conversions."""

    def __init__(self, tx, sketch, activated):
        self.tx = tx
        self.sketch = sketch
        self.activated = activated
        self.frame = frame(sketch)

    def to_part(self, p2):
        return to_part(self.frame, p2)

    def to_local(self, p3):
        return to_local(self.frame, p3)

    def point(self, p2):
        """An NX ``Point3d`` for a sketch-local point."""
        return self.tx.nx.Point3d(*self.to_part(p2))


def resolve(tx, ref, field="/sketch"):
    """A sketch of the work part (ref or live object)."""
    sk = tx.resolve(ref, "sketch") if isinstance(ref, str) else ref
    if getattr(sk, "IsOccurrence", False) or getattr(sk, "OwningPart", tx.part) != tx.part:
        fail("NX_WRONG_OWNER", "Select a sketch of the work part", field=field)
    return sk


@contextlib.contextmanager
def editing(tx, ref, field="/sketch"):
    """Activate a sketch for editing and restore the previous state afterwards.

    A different active sketch is ``NX_SKETCH_ACTIVE``. A sketch that was
    already active stays active; one activated here is deactivated with a
    model update level, even when the block fails.
    """
    sk = resolve(tx, ref, field)
    session, nx = tx.session, tx.nx
    active = session.ActiveSketch
    if active and active != sk:
        fail(
            "NX_SKETCH_ACTIVE",
            "Finish the other active sketch first",
            hint="Finish the active sketch, then retry",
        )
    activated = not active
    if activated:
        sk.Activate(nx.Sketch.ViewReorient.FalseValue)
    try:
        yield Editing(tx, sk, activated)
    finally:
        if activated and session.ActiveSketch == sk:
            sk.Deactivate(nx.Sketch.ViewReorient.FalseValue, nx.Sketch.UpdateLevel.Model)


def _sketch_of(sk):
    return sk.sketch if isinstance(sk, Editing) else sk


def _expression_closure(expressions):
    """Edited expressions plus every expression that references them (by tag)."""
    tags, pending = set(), list(expressions)
    while pending and len(tags) < 10000:
        exp = pending.pop()
        if int(exp.Tag) not in tags:
            tags.add(int(exp.Tag))
            query = getattr(exp, "GetReferencingExpressions", None)
            pending.extend(list(query()) if query is not None else [])
    return tags


def update(tx, sk):
    """Solve geometry now: ``LocalUpdate()`` for the active sketch, then ``Update()``."""
    s = _sketch_of(sk)
    if tx.session.ActiveSketch == s and hasattr(s, "LocalUpdate"):
        s.LocalUpdate()
    s.Update()


def status(tx, sk):
    """``{"status": name, "dof": n or None, "native_dof": n}`` for the whole sketch."""
    s = _sketch_of(sk)
    session, nx = tx.session, tx.nx
    mark = session.SetUndoMark(nx.Session.MarkVisibility.Invisible, "NX MCP sketch status")
    try:
        region = tx.part.Sketches.CreateWorkRegionBuilder()
        try:
            region.Scope = nx.SketchWorkRegionBuilder.ScopeType.EntireSketch
            region.Commit()
        finally:
            region.Destroy()
        s.CalculateStatus()
        value, dof = s.GetStatus()
    finally:
        session.UndoToMark(mark, None)
        session.DeleteUndoMark(mark, None)
    name = enum_name(value, nx.Sketch.Status)
    return {"status": name, "dof": dof if name in SOLVED_DOF_STATUSES else None, "native_dof": dof}


def solve(tx, sk, expressions=()):
    """Solve the sketch and verify it; returns ``status()``.

    Each driving dimension whose expression is in ``expressions`` (or depends
    on one) must measure its value afterwards, else ``NX_CONSTRAINT_UNSATISFIED``.
    An over- or inconsistently constrained sketch is ``NX_SKETCH_OVERCONSTRAINED``.
    """
    s = _sketch_of(sk)
    update(tx, s)
    if expressions:
        tags = _expression_closure(expressions)
        nx = tx.nx
        for c in s.GetAllConstraintsOfType(
            nx.Sketch.ConstraintClass.Dimension, nx.Sketch.ConstraintType.NoCon
        ):
            exp = getattr(c, "AssociatedExpression", None)
            if exp is None or int(exp.Tag) not in tags:
                continue
            wanted = float(exp.Value)
            measured = float(c.AssociatedDimension.ComputedSize)
            if abs(measured - wanted) > 1e-6 * max(1.0, abs(wanted)):
                fail(
                    "NX_CONSTRAINT_UNSATISFIED",
                    "NX could not solve the sketch to the new dimension value",
                    details={"expression": exp.Name, "requested": wanted, "measured": measured},
                )
    state = status(tx, s)
    if state["status"] in FAILED_STATUSES:
        fail(
            "NX_SKETCH_OVERCONSTRAINED",
            f"The sketch is {state['status']} after the change",
            hint="Diagnose with nx_check_sketch and remove the conflicting constraint",
            details=state,
        )
    return state


def owns(tx, sk, curve, field="/curve"):
    """``curve`` (ref or object) when it is geometry of the sketch, else ``wrong_owner``."""
    s = _sketch_of(sk)
    obj = tx.resolve(curve, "curve") if isinstance(curve, str) else curve
    if int(obj.Tag) not in {int(c.Tag) for c in s.GetAllGeometry()}:
        fail("NX_WRONG_OWNER", "The curve is not owned by this sketch", field=field)
    return obj
