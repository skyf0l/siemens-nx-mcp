"""Undo of in-sketch changes without NX's undo (the compensating undo, ``@restorer``).

NX's undo reverts a sketch tool's call only while no other part's work follows it; the
compensating undo (domains/session/compensate.py) then runs the tool's restorer. Every
tool that changes an existing sketch keeps what it changed (:func:`capture` before the
change, :func:`note` after it): the curves, constraints and dimensions it created, the
projection features it made, the curves it moved or switched to construction (their
shape before and after) and the constraints NX removed with the change (a fillet takes
the corner's coincident constraint).

The restorer activates the sketch (its part must be displayed: NX 2406 faulted when a
sketch with dimensions was activated in a part it did not display, ``kit/sketch``),
deletes what the call created (``Sketch.DeleteObjects``, as nx_edit_sketch_curves
delete does), sets the moved curves back, creates the removed constraints again and
verifies every shape. It refuses when the created objects are gone or the moved curves
moved again since (``NX_DEPENDENCY_ORDER``), when another sketch is open, and when the
part is not displayed.

Nothing is kept, so only NX's undo reverts the call, when it deleted curves, moved an
ellipse or spline, or removed a constraint NX cannot create again.
"""

from __future__ import annotations

import contextlib

from nx_mcp.core import switching
from nx_mcp.core.catalogue import restorer
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.domains.sketch import dof as kdof
from nx_mcp.kit import geom, readback
from nx_mcp.kit import sketch as ksketch

# The tools whose calls change an existing sketch (their restorer is :func:`restore`).
TOOLS = (
    "nx_create_sketch_curves",
    "nx_edit_sketch_curves",
    "nx_create_sketch_constraints",
    "nx_create_sketch_dimensions",
    "nx_create_sketch_copy",
    "nx_create_sketch_corner",
    "nx_create_sketch_projection",
)
# Constraint kinds NX creates again from their geometry alone: ``Sketch.<method>``.
RECREATE = {
    "coincident": "CreateCoincidentConstraint",
    "midpoint": "CreateMidpointConstraint",
    "point_on_curve": "CreatePointOnCurveConstraint",
    "horizontal": "CreateHorizontalConstraint",
    "vertical": "CreateVerticalConstraint",
    "fixed": "CreateFixedConstraint",
    "parallel": "CreateParallelConstraint",
    "perpendicular": "CreatePerpendicularConstraint",
    "collinear": "CreateCollinearConstraint",
    "equal_length": "CreateEqualLengthConstraint",
    "equal_radius": "CreateEqualRadiusConstraint",
    "concentric": "CreateConcentricConstraint",
    "tangent": "CreateTangentConstraint",
}
POINTS = ("curve", "start", "end", "center")


def _native(tx, curve) -> dict | None:
    """What sets a line, arc or point back (``SetEndpoints``/``SetParameters``/
    ``SetCoordinates`` values), with its construction state; None for other curves."""
    kind = ksketch.curve_kind(tx, curve)
    out: dict
    if ksketch.is_point(tx, curve):
        out = {"kind": "point", "at": geom.xyz(curve.Coordinates)}
    elif kind == "line":
        out = {"kind": "line", "start": geom.xyz(curve.StartPoint), "end": geom.xyz(curve.EndPoint)}
    elif kind in ("arc", "circle"):
        out = {
            "kind": "arc",
            "center": geom.xyz(curve.CenterPoint),
            "radius": float(curve.Radius),
            "angles": [float(curve.StartAngle), float(curve.EndAngle)],
        }
    else:
        return None
    out["construction"] = bool(getattr(curve, "IsReference", False))
    out["shape"] = ksketch.shape(tx, curve)
    return out


def _same_shape(a, b) -> bool:
    if len(a) != len(b):
        return False
    return not a or max(abs(x - y) for x, y in zip(a, b, strict=True)) <= geom.tol(a, b) * 10


def _same(a: dict, b: dict) -> bool:
    return a.get("construction") == b.get("construction") and _same_shape(a["shape"], b["shape"])


def capture(tx, sk) -> dict:
    """The sketch's state before a change: its geometry, the shape of each curve, its
    constraints with the geometry they use, and the part's expressions."""
    s = ksketch._sketch_of(sk)
    geometry = {int(c.Tag): c for c in ksketch.geometry(s)}
    constraints = {}
    for c in ksketch.constraints(tx, s):
        constraints[int(c.Tag)] = (ksketch.constraint_type(tx, c), kdof.geometry(tx, c))
    expressions: set = set()
    with contextlib.suppress(Exception):
        expressions = {int(e.Tag) for e in tx.part.Expressions}
    shapes = {}
    for tag, c in geometry.items():
        with contextlib.suppress(Exception):
            shapes[tag] = _native(tx, c)
    return {
        "geometry": geometry,
        "shapes": shapes,
        "constraints": constraints,
        "expressions": expressions,
    }


def _ref_id(tx, obj, kind=None) -> str:
    return str(tx.ref(obj, kind)["id"])


def _lost(tx, s, before: dict, alive: set) -> list | None:
    """The constraints of ``before`` NX removed, as kinds and geometry to create them again;
    None when one cannot be created again."""
    out = []
    for tag, (kind, uses) in before["constraints"].items():
        if tag in alive:
            continue
        method = RECREATE.get(kind)
        if method is None or getattr(s, method, None) is None or not uses:
            return None
        geometry = []
        for obj, point in uses:
            if point not in POINTS:
                return None
            try:
                geometry.append([_ref_id(tx, obj), point])
            except Exception:  # the curve went with it, or NX cannot reference it
                return None
        out.append({"type": kind, "geometry": geometry})
    return out


def note(tx, sk, before: dict, *, features=(), transient=()) -> bool:
    """Keep what the call changed in the sketch since :func:`capture` (``tx.note_restore``)
    so the compensating undo can set it back; ``features``: the projection features the
    call made; ``transient``: tags of new geometry the call deletes itself after the note
    (a corner's help points). Returns whether it was kept (see the module docstring for
    what is not)."""
    s = ksketch._sketch_of(sk)
    skipped = {int(t) for t in transient}
    now = {int(c.Tag): c for c in ksketch.geometry(s) if int(c.Tag) not in skipped}
    if any(tag not in now for tag in before["geometry"]):
        return False  # the call deleted curves: only NX's undo brings them back
    constraints = {int(c.Tag): c for c in ksketch.constraints(tx, s)}
    lost = _lost(tx, s, before, set(constraints))
    if lost is None:
        return False
    moved = []
    for tag, curve in before["geometry"].items():
        old = before["shapes"].get(tag)
        new = _native(tx, curve)
        if old is None or new is None:
            if (old is None) != (new is None) or not _same_shape(
                (old or {}).get("shape", []), ksketch.shape(tx, curve)
            ):
                return False  # an ellipse or spline that moved: no values to set it back
            continue
        if not _same(old, new):
            moved.append({"curve": _ref_id(tx, curve, "curve"), "before": old, "after": new})
    expressions = []
    with contextlib.suppress(Exception):
        expressions = [
            str(e.Name) for e in tx.part.Expressions if int(e.Tag) not in before["expressions"]
        ]
    data = {
        "sketch": _ref_id(tx, s, "sketch"),
        "curves": [
            _ref_id(tx, c, "curve") for tag, c in now.items() if tag not in before["geometry"]
        ],
        "constraints": [
            _ref_id(tx, c, "sketch_constraint")
            for tag, c in constraints.items()
            if tag not in before["constraints"]
        ],
        "features": [_ref_id(tx, f, "feature") for f in features if f is not None],
        "expressions": expressions,
        "moved": moved,
        "lost": lost,
    }
    tx.note_restore(data)
    return True


# ---------------------------------------------------------------------------
# The restorer


def _live(tx, ref, *kinds, what="it"):
    try:
        return tx.resolve(ref, *kinds)
    except NXError as error:
        if error.code != "NX_STALE_REFERENCE":
            raise
        fail(
            "NX_DEPENDENCY_ORDER",
            f"{ref}, which the operation made, was deleted since: {what} cannot be undone "
            "by compensation",
            details={"ref": ref},
        )


def _ready(tx, s, ref, *, apply: bool = True) -> None:
    """The sketch can be activated now: no other open sketch, its part displayed. The
    display is checked when the change is set back (``apply``): an undo or restore that
    sets an in-sketch change back displays the part for its call first (gate 4s), after
    the checks of ``nx_list_checkpoints`` and of its own plan."""
    other = ksketch.open_sketch(tx)
    if other is not None and not ksketch.same(other, s):
        fail(
            "NX_SKETCH_ACTIVE",
            f"Undoing the change of {ref} activates it, but another sketch of the part is open: "
            "finish that one with nx_finish_sketch, then undo again",
            hint="Finish the open sketch (details.open_sketch) with nx_finish_sketch, then undo",
            details=ksketch.open_details(tx, other),
        )
    part = getattr(s, "OwningPart", None) or tx.part
    if (
        apply
        and not bool(getattr(tx.session, "IsBatch", False))
        and not switching.displayed(tx.services, part)
    ):
        fail(
            "NX_WRONG_PART_CONTEXT",
            f"Undoing the change of {ref} activates the sketch, and NX activates sketches only "
            f"in the displayed part; {switching.file_name(part)} is not displayed: display it "
            "with nx_activate_part, then undo again",
            hint="Display the part (nx_activate_part), then undo again",
            details={"sketch": ref},
        )


def _created(tx, s, data) -> tuple[list, list]:
    """The live constraints and curves the call created (refused when one is gone)."""
    owned_curves = ksketch.tags(s)
    owned = {int(c.Tag) for c in ksketch.constraints(tx, s)}
    constraints, curves = [], []
    for ref in data.get("constraints") or []:
        obj = _live(tx, ref, "sketch_constraint")
        if int(obj.Tag) not in owned:
            fail("NX_DEPENDENCY_ORDER", f"{ref} is no longer in the sketch", details={"ref": ref})
        constraints.append(obj)
    for ref in data.get("curves") or []:
        obj = _live(tx, ref, "curve")
        if int(obj.Tag) not in owned_curves:
            fail("NX_DEPENDENCY_ORDER", f"{ref} is no longer in the sketch", details={"ref": ref})
        curves.append(obj)
    return constraints, curves


def _expressions(tx, data) -> list:
    """The created expressions that still exist; refused when another expression uses one."""
    names = list(data.get("expressions") or [])
    found = [e for e in tx.part.Expressions if str(e.Name) in names]
    tags = {int(e.Tag) for e in found}
    for exp in found:
        users = [str(u.Name) for u in _users(exp) if int(u.Tag) not in tags]
        if users:
            fail(
                "NX_DEPENDENCY_ORDER",
                f"Expression {exp.Name}, which the operation made, is used by {', '.join(users[:5])}",
                details={"name": str(exp.Name), "used_by": users[:20]},
            )
    return found


def _users(exp) -> list:
    query = getattr(exp, "GetReferencingExpressions", None)
    try:
        return list(query()) if query is not None else []
    except Exception:
        return []


def _delete(tx, e, objects, what) -> None:
    """Delete ``objects`` (``(tag, object)``: NX raises on any member of an object it
    deleted already, a constraint that went with its curve) that are still in the sketch."""
    alive = ksketch.tags(e) | {int(c.Tag) for c in ksketch.constraints(tx, e)}
    objects = [o for t, o in objects if t in alive]
    if not objects:
        return
    with native(default="NX_BUILD_FAILED"):
        errors = e.sketch.DeleteObjects(objects)
    if errors is None:
        return
    try:
        count = int(errors.Length)
        messages = [str(errors.GetErrorInfo(i)) for i in range(count)]
    finally:
        errors.Dispose()
    if count:
        fail("NX_BUILD_FAILED", f"NX did not delete the {what}: " + "; ".join(messages))


def _set_back(tx, e, curve, old: dict) -> None:
    nx = tx.nx
    with native(default="NX_BUILD_FAILED"):
        if old["kind"] == "point":
            curve.SetCoordinates(nx.Point3d(*old["at"]))
        elif old["kind"] == "line":
            curve.SetEndpoints(nx.Point3d(*old["start"]), nx.Point3d(*old["end"]))
        else:
            start, end = old["angles"]
            curve.SetParameters(float(old["radius"]), nx.Point3d(*old["center"]), start, end)
    if bool(getattr(curve, "IsReference", False)) != bool(old["construction"]):
        ksketch.set_construction(tx, [curve], bool(old["construction"]))


def _recreate(tx, e, row) -> None:
    s, nx = e.sketch, tx.nx
    geometry = []
    for ref, point in row["geometry"]:
        obj = _live(tx, ref, what="a removed constraint")
        geometry.append((obj, None if point == "curve" else point))
    parts = [ksketch.constraint_geometry(tx, obj, which) for obj, which in geometry]
    method = getattr(s, RECREATE[row["type"]])
    with native(default="NX_BUILD_FAILED"):
        if row["type"] == "point_on_curve":
            point = next(((o, w) for o, w in geometry if w is not None), geometry[0])
            help_ = nx.Sketch.ConstraintGeometryHelp()
            help_.Type = nx.Sketch.ConstraintGeometryHelpType.Point
            help_.Point = nx.Point3d(*ksketch.point(tx, point[0], point[1] or "start"))
            help_.Parameter = 0.0
            method(*parts, help_)
        else:
            method(*parts)


@restorer(TOOLS[0])
@restorer(TOOLS[1])
@restorer(TOOLS[2])
@restorer(TOOLS[3])
@restorer(TOOLS[4])
@restorer(TOOLS[5])
@restorer(TOOLS[6])
def restore(tx, data: dict, *, apply: bool) -> str:
    """Set an in-sketch change back (module docstring); with ``apply`` false only check."""
    ref = str(data["sketch"])
    s = _live(tx, ref, "sketch")
    _ready(tx, s, ref, apply=apply)
    constraints, curves = _created(tx, s, data)
    features = [_live(tx, f, "feature") for f in data.get("features") or []]
    features = [(int(f.Tag), f) for f in features]
    expressions = [(int(x.Tag), x) for x in _expressions(tx, data)]
    moved = list(data.get("moved") or [])
    lost = list(data.get("lost") or [])
    what = _summary(ref, constraints, curves, moved, lost)
    if not apply:
        return what
    with ksketch.editing(tx, s) as e:
        rows = [(_live(tx, m["curve"], "curve"), m) for m in moved]
        for curve, m in rows:
            now = _native(tx, curve)
            if now is None or not _same(now, m["after"]):
                fail(
                    "NX_DEPENDENCY_ORDER",
                    f"{m['curve']} changed since the operation: it is not set back",
                    details={"curve": m["curve"]},
                )
        made_curves = [(int(c.Tag), c) for c in curves]
        made_constraints = [(int(c.Tag), c) for c in constraints]
        refs = {t: _ref_id(tx, o) for t, o in made_curves + made_constraints}
        made = set(refs)
        kept = {
            t: (c, ksketch.shape(tx, c))
            for t, c in ((int(c.Tag), c) for c in ksketch.geometry(e))
            if t not in made and all(int(c.Tag) != int(r[0].Tag) for r in rows)
        }
        # curves first: their own constraints (an offset's, a mirror's) go with them
        _delete(tx, e, made_curves, "curves the operation created")
        _delete(tx, e, made_constraints, "constraints the operation created")
        for curve, m in rows:
            _set_back(tx, e, curve, m["before"])
        for row in lost:
            _recreate(tx, e, row)
        with native(default="NX_UPDATE_FAILED"):
            ksketch.update(tx, e)
        alive = ksketch.tags(e) | {int(c.Tag) for c in ksketch.constraints(tx, e)}
        left = [ref for t, ref in refs.items() if t in alive]
        readback.expect(not left, "deleted", f"NX did not delete {', '.join(left)}", objects=left)
        for curve, m in rows:
            now = _native(tx, curve)
            readback.expect(
                now is not None and _same(now, m["before"]),
                "set_back",
                f"{m['curve']} does not read its shape before the operation",
                curve=m["curve"],
            )
        shifted = [
            _ref_id(tx, c, "curve")
            for c, shape in kept.values()
            if not _same_shape(shape, ksketch.shape(tx, c))
        ]
        readback.expect(
            not shifted, "unmoved", f"Setting it back moved {', '.join(shifted)}", curves=shifted
        )
    _delete_features(tx, features)
    _delete_expressions(tx, expressions)
    return what


def _delete_features(tx, features) -> None:
    """Projection features left without curves (``(tag, feature)``): NX's Delete, the
    sketch no longer active. NX 2406 deletes a projection with its last curve (live), so
    this is a fallback; a feature that still makes curves or has children is refused."""
    alive = {int(f.Tag) for f in tx.part.Features}
    left = []
    for tag, feature in features:
        if tag not in alive:
            continue  # NX deleted it with its curves
        entities = list(feature.GetEntities() or [])
        children = list(feature.GetChildren() or [])
        if entities or children:
            fail(
                "NX_DEPENDENCY_ORDER",
                f"{_ref_id(tx, feature, 'feature')} still makes curves or has dependent features "
                "after its curves were deleted",
            )
        left.append(feature)
    if not left:
        return
    with native(default="NX_BUILD_FAILED"):
        tx.session.UpdateManager.AddToDeleteList(left)
    from nx_mcp.kit import model

    model.update(tx)


def _delete_expressions(tx, expressions) -> None:
    """Created expressions NX kept after their dimension went (unused: deleted)."""
    for tag, exp in expressions:
        with contextlib.suppress(Exception):
            if any(int(e.Tag) == tag for e in tx.part.Expressions) and not _users(exp):
                tx.part.Expressions.Delete(exp)


def _summary(ref, constraints, curves, moved, lost) -> str:
    parts = []
    if constraints:
        parts.append(f"{len(constraints)} constraint(s) or dimension(s) deleted")
    if curves:
        parts.append(f"{len(curves)} curve(s) deleted")
    if moved:
        parts.append(f"{len(moved)} curve(s) set back")
    if lost:
        parts.append(f"{len(lost)} removed constraint(s) created again")
    return f"sketch {ref}: " + (", ".join(parts) if parts else "nothing to set back")
