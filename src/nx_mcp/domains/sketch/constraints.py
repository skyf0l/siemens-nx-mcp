"""Sketch constraints and dimensions, 1-50 per call (tool-surface 3.5).

NX 2406 findings kept here (modern solver, ``Sketch.UsesLegacySolver`` False):

- ``Create<Kind>Constraint`` makes a persistent constraint but does not move
  geometry; the ``SketchMake<Kind>Builder`` relations (parallel, perpendicular,
  equal, coincident, concentric, tangent, symmetric) move the second curve.
  Horizontal, vertical, midpoint and point-on-curve are solved by a native drag
  (``kit.sketch.drag``); every constraint is then checked by its geometric residual.
- NX's midpoint constraint only makes a point equidistant from the target's ends
  (perpendicular bisector); with a point-on-curve constraint it is the midpoint.
  ``MidVertex`` is refused as a target: the target is the curve itself.
- ``fixed`` on a curve uses ``CreateFullyFixedConstraints``: NX's plain Fixed
  constraint on a line holds only the infinite line, so a later length dimension
  still slid its end (the reported "fixed line moved" bug). Every constraint and
  dimension call also refuses to move geometry that carries a Fixed constraint.
- A reference dimension has no expression on NX 2406 (``AssociatedExpression``
  is None; ``AssociatedDimension.ComputedSize`` measures it).
- Dimensions solve on ``EditExpression`` + ``LocalUpdate`` (``kit.sketch.solve``);
  an unreachable value leaves the geometry and an OverConstrained state, a
  duplicate dimension is refused with "Persistent relation already exists"
  (910023): both roll back as ``solver_failed``.
- The solver may delete an existing persistent constraint instead of failing;
  that rolls back too.
- Relation builders and drags also maintain relations NX *finds* (a Make Equal
  resized an unrelated circle of the same radius); every found relation is
  relaxed (``kit.sketch.relax_found_relations``).
- A request whose solve collapses curves (a point held on two parallel sides of one
  rectangle, or on two sides meeting at a corner; a corner onto the far side; both
  ends of one curve made coincident) makes NX delete them behind the modal "Edit
  Sketch: The edit causes curves or dimensions to be deleted." box, which blocks NX
  for everyone (live, 2026-10-12). Such items are refused before NX solves
  (``kit.sketch.collapse_risks``); a solve that still deleted a curve rolls back.
"""

from __future__ import annotations

import contextlib
import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, classify, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    CurveRef,
    Doc,
    Items,
    Point2,
    SketchPoint,
    SketchRef,
    Text,
    convert_quantity,
    forbid_extra,
)
from nx_mcp.domains.sketch import common
from nx_mcp.domains.sketch import dof as kdof
from nx_mcp.domains.sketch.common import XY, Loose
from nx_mcp.kit import builders, expr, geom, readback, result, units
from nx_mcp.kit import sketch as ksketch

VERTEX = ("start", "end", "center")


class Vertex(TypedDict):
    """A point of a sketch curve for a constraint (NX has no midpoint vertex)."""

    curve: CurveRef
    point: Literal["start", "end", "center"]


# ---------------------------------------------------------------------------
# Geometric constraints: variant sub-schemas and the flat listing schema


class _Fixed(TypedDict):
    type: Literal["fixed"]


class FixedConstraint(_Fixed, total=False):
    curves: Annotated[list[CurveRef], Items(1, 1), Doc("Fix the whole curve")]
    points: Annotated[list[Vertex], Items(1, 1), Doc("Or fix only this point")]


class OneLine(TypedDict):
    type: Literal["horizontal", "vertical"]
    curves: Annotated[list[CurveRef], Items(1, 1)]


class TwoCurves(TypedDict):
    type: Literal[
        "parallel",
        "perpendicular",
        "collinear",
        "equal_length",
        "equal_radius",
        "concentric",
        "tangent",
    ]
    curves: Annotated[list[CurveRef], Items(2, 2)]


class Coincident(TypedDict):
    type: Literal["coincident"]
    points: Annotated[list[Vertex], Items(2, 2)]


class PointOn(TypedDict):
    type: Literal["midpoint", "point_on_curve"]
    points: Annotated[list[Vertex], Items(1, 1), Doc("The point to move")]
    curves: Annotated[
        list[CurveRef], Items(1, 1), Doc("midpoint: the line or arc whose middle; else target")
    ]


class Symmetric(TypedDict):
    type: Literal["symmetric"]
    curves: Annotated[list[CurveRef], Items(2, 2)]
    centerline: CurveRef


CONSTRAINT_TYPES = {
    "fixed": FixedConstraint,
    "horizontal": OneLine,
    "vertical": OneLine,
    "parallel": TwoCurves,
    "perpendicular": TwoCurves,
    "collinear": TwoCurves,
    "equal_length": TwoCurves,
    "equal_radius": TwoCurves,
    "concentric": TwoCurves,
    "tangent": TwoCurves,
    "coincident": Coincident,
    "midpoint": PointOn,
    "point_on_curve": PointOn,
    "symmetric": Symmetric,
}


class _ConstraintType(TypedDict):
    type: (
        Literal[
            "fixed",
            "horizontal",
            "vertical",
            "parallel",
            "perpendicular",
            "collinear",
            "equal_length",
            "equal_radius",
            "concentric",
            "tangent",
            "coincident",
            "midpoint",
            "point_on_curve",
            "symmetric",
        ]
        | Loose
    )


class ConstraintSpec(_ConstraintType, total=False):
    curves: Annotated[
        list[CurveRef],
        Items(1, 2),
        Doc("symmetric: the two mirrored curves; midpoint/point_on_curve: the target curve"),
    ]
    points: Annotated[
        list[Vertex],
        Items(1, 2),
        Doc("coincident: two; midpoint/point_on_curve: one, it moves; fixed: one (or curves)"),
    ]
    centerline: Annotated[CurveRef, Doc("symmetric only (required): a straight line")]


# Synonyms agents use; case, spaces and hyphens are ignored anyway.
CONSTRAINT_ALIASES = {
    "fix": "fixed",
    "lock": "fixed",
    "equal_radii": "equal_radius",
    "same_radius": "equal_radius",
    "equal_lengths": "equal_length",
    "same_length": "equal_length",
    "coincidence": "coincident",
    "concentricity": "concentric",
    "tangency": "tangent",
    "tangential": "tangent",
    "parallelism": "parallel",
    "colinear": "collinear",
    "collinearity": "collinear",
    "in_line": "collinear",
    "mid_point": "midpoint",
    "point_on": "point_on_curve",
    "on_curve": "point_on_curve",
    "point_on_line": "point_on_curve",
    "symmetry": "symmetric",
    "mirror": "symmetric",
}


forbid_extra(
    Vertex, FixedConstraint, OneLine, TwoCurves, Coincident, PointOn, Symmetric, ConstraintSpec
)


# ---- local geometry ----------------------------------------------------------------


def _local(tx, sk, curve, which):
    return ksketch.to_local(sk.frame, ksketch.point(tx, curve, which))


def _direction(tx, sk, line):
    return geom.sub(_local(tx, sk, line, "end"), _local(tx, sk, line, "start"))


def _cos(u, v):
    return geom.dot(u, v) / (geom.norm(u) * geom.norm(v))


def _line_distance(a, b, p):
    """Distance from local point ``p`` to the infinite line through ``a`` and ``b``."""
    d = geom.sub(b, a)
    return abs(geom.cross2(d, geom.sub(p, a))) / geom.norm(d)


def _foot(tx, sk, curve, p):
    """Closest point to ``p`` on the (extended) line or the circle of ``curve``, and the gap."""
    if ksketch.curve_kind(tx, curve) == "line":
        a, b = _local(tx, sk, curve, "start"), _local(tx, sk, curve, "end")
        d = geom.sub(b, a)
        foot = geom.add(a, geom.scale(d, geom.dot(geom.sub(p, a), d) / geom.dot(d, d)))
        return foot, geom.dist(p, foot)
    c = _local(tx, sk, curve, "center")
    r = float(curve.Radius)
    v = geom.sub(p, c)
    n = geom.norm(v)
    if n <= 1e-12:
        v, n = [1.0, 0.0], 1.0
    return geom.add(c, geom.scale(v, r / n)), abs(geom.dist(p, c) - r)


def _require(tx, curve, kinds, field, what):
    if ksketch.curve_kind(tx, curve) not in kinds:
        fail("NX_WRONG_GEOMETRY", f"Select {what}", field=field)
    return curve


# ---- residuals: 0 when the constraint holds (lengths in part units) ---------------


def _residual(tx, sk, kind, objs):
    """``(residual, tolerance)`` of an applied constraint, from the live geometry."""
    if kind in ("horizontal", "vertical"):
        d = _direction(tx, sk, objs["curves"][0])
        return abs(d[1] if kind == "horizontal" else d[0]), 1e-6 * max(1.0, geom.norm(d))
    if kind in ("parallel", "perpendicular"):
        c = abs(_cos(*(_direction(tx, sk, line) for line in objs["curves"])))
        return (1 - c if kind == "parallel" else c), 1e-9
    if kind == "collinear":
        a, b = objs["curves"]
        s, e = _local(tx, sk, a, "start"), _local(tx, sk, a, "end")
        ends = [_local(tx, sk, b, w) for w in ("start", "end")]
        return max(_line_distance(s, e, p) for p in ends), geom.tol(s, e, *ends) * 10
    if kind == "equal_length":
        a, b = (geom.norm(_direction(tx, sk, line)) for line in objs["curves"])
        return abs(a - b), geom.tol([a, b])
    if kind in ("equal_radius", "concentric"):
        a, b = objs["curves"]
        if kind == "equal_radius":
            return abs(float(a.Radius) - float(b.Radius)), geom.tol([a.Radius, b.Radius])
        p, q = _local(tx, sk, a, "center"), _local(tx, sk, b, "center")
        return geom.dist(p, q), geom.tol(p, q) * 10
    if kind == "tangent":
        a, b = objs["curves"]
        if ksketch.curve_kind(tx, b) == "line":
            a, b = b, a
        cb, rb = _local(tx, sk, b, "center"), float(b.Radius)
        if ksketch.curve_kind(tx, a) == "line":
            s, e = _local(tx, sk, a, "start"), _local(tx, sk, a, "end")
            return abs(_line_distance(s, e, cb) - rb), geom.tol(s, e, cb) * 10
        ca, ra = _local(tx, sk, a, "center"), float(a.Radius)
        d = geom.dist(ca, cb)
        return min(abs(d - ra - rb), abs(d - abs(ra - rb))), geom.tol(ca, cb, [ra, rb]) * 10
    if kind == "coincident":
        (c1, p1), (c2, p2) = objs["points"]
        a, b = _local(tx, sk, c1, p1), _local(tx, sk, c2, p2)
        return geom.dist(a, b), geom.tol(a, b) * 10
    if kind in ("midpoint", "point_on_curve"):
        (src, which), target = objs["points"][0], objs["curves"][0]
        p = _local(tx, sk, src, which)
        if kind == "point_on_curve":
            return _foot(tx, sk, target, p)[1], geom.tol(p) * 10
        mid = _local(tx, sk, target, "mid")
        return geom.dist(p, mid), geom.tol(p, mid) * 10
    if kind == "symmetric":
        (a, b), c = objs["curves"], objs["centerline"]
        o, t = _local(tx, sk, c, "start"), _direction(tx, sk, c)
        pa = [geom.reflect_across_line(_local(tx, sk, a, w), o, t) for w in ("start", "end")]
        pb = [_local(tx, sk, b, w) for w in ("start", "end")]
        error = min(
            max(geom.dist(x, y) for x, y in zip(pa, order, strict=True)) for order in (pb, pb[::-1])
        )
        return error, geom.tol(*pa, *pb) * 10
    return 0.0, 0.0  # fixed: held by the fixed-geometry guard


# ---- applying one constraint -----------------------------------------------------------


@contextlib.contextmanager
def _refusal(what, field=None):
    """NX's refusal of a duplicate or contradicting relation is ``solver_failed`` on the
    item's ``field`` (910023 "Persistent relation already exists": an equal relation, or
    one the existing ones imply, is there already)."""
    try:
        yield
    except NXError:
        raise
    except Exception as error:
        native_error = classify(error, default="NX_BUILD_FAILED")
        text = str(error).lower()
        if "already exists" in text:
            fail(
                "NX_SKETCH_OVERCONSTRAINED",
                f"NX refused the {what}: {error}",
                field=field,
                hint="The curves already have this relation, or the existing constraints "
                "imply it (nx_get_sketch lists them): drop the item",
                nx_code=native_error.nx_code,
            )
        if "constrain" in text:
            fail(
                "NX_SKETCH_OVERCONSTRAINED",
                f"NX refused the {what}: {error}",
                field=field,
                hint="Delete the constraint that contradicts it first (nx_get_sketch lists "
                "the constraints of these curves), or drop the item",
                nx_code=native_error.nx_code,
            )
        native_error.field = native_error.field or field
        raise native_error from error


def _make_relation(tx, sk, kind, objs, field=None):
    """The modern-solver ``SketchMake*Builder`` relations; the second curve moves."""
    nx, part = tx.nx, tx.part
    kept = ksketch.curve_labels(sk)
    names = {
        "parallel": "CreateSketchMakeParallelBuilder",
        "perpendicular": "CreateSketchMakePerpendicularBuilder",
        "collinear": "CreateSketchMakeCollinearBuilder",
        "equal_length": "CreateSketchMakeEqualBuilder",
        "equal_radius": "CreateSketchMakeEqualBuilder",
        "coincident": "CreateSketchMakeCoincidentBuilder",
        "concentric": "CreateSketchMakeCoincidentBuilder",
        "tangent": "CreateSketchMakeTangentBuilder",
    }
    with builders.build(tx, getattr(part.Sketches, names[kind])) as b:
        if kind in ("coincident", "concentric"):
            if kind == "concentric":
                (a, pa), (c, pc) = (objs["curves"][0], "center"), (objs["curves"][1], "center")
            else:
                (a, pa), (c, pc) = objs["points"]
            view = common.pick_view(tx, part)
            snaps = nx.InferSnapType.SnapType
            empty = nx.Point3d(0.0, 0.0, 0.0)
            b.StationaryObject.SetValue(
                getattr(snaps, pa.title()), a, view, nx.Point3d(*ksketch.point(tx, a, pa)),
                None, None, empty,
            )  # fmt: skip
            b.MotionPoints.Add(
                getattr(snaps, pc.title()), c, view, nx.Point3d(*ksketch.point(tx, c, pc)),
                None, None, empty,
            )  # fmt: skip
        else:
            a, c = objs["curves"]
            b.StationaryObject.Value = a
            b.MotionObjects.Add(c)
        if kind in ("equal_length", "equal_radius"):
            b.EqualType = getattr(
                nx.SketchMakeEqualBuilder.EqualTypes,
                "Radius" if kind == "equal_radius" else "Length",
            )
        b.SetCreateConstraints(True)
        ksketch.relax_found_relations(b)
        with _refusal(kind.replace("_", " ") + " constraint", field):
            b.Commit()
    ksketch.require_kept(sk, kept, field)


def _symmetric(tx, sk, objs, field=None):
    (a, c), centerline = objs["curves"], objs["centerline"]
    kept = ksketch.curve_labels(sk)
    with builders.build(tx, tx.part.Sketches.CreateSketchSymmetricBuilder) as b:
        b.StationaryObject.Value = a
        b.MotionObjects.Add(c)
        b.CenterLine.Value = centerline
        b.ConvertCenterlineToReference = False
        b.SetCreateConstraints(True)
        ksketch.relax_found_relations(b)
        with _refusal("symmetric constraint", field):
            b.Commit()
    ksketch.require_kept(sk, kept, field)


def _refuse_collapse(tx, sk, kind, objs, field):
    """Refuse an item whose solve would collapse curves: NX would delete them behind its
    modal "Edit Sketch" dialog, which blocks NX for every caller (module doc)."""
    if kind not in ("coincident", "point_on_curve", "midpoint"):
        return
    target = objs["curves"][0] if kind != "coincident" else None
    risks = ksketch.collapse_risks(tx, sk, kind, objs["points"], target)
    if not risks:
        return
    risk = risks[0]
    curves = [tx.ref(c, "curve")["id"] for c in risk["curves"]]
    details: dict = {"reason": risk["reason"], "curves": curves}
    hint = "Choose other geometry, or delete the conflicting constraint first (nx_check_sketch)"
    if risk.get("constraint") is not None:
        held = tx.ref(risk["constraint"], "sketch_constraint")["id"]
        details["constraint"] = held
        hint = (
            f"Delete constraint {held} first (nx_edit_sketch_curves {{type: delete, object: "
            f"{held!r}}}), or choose other geometry"
        )
    fail(
        "NX_SKETCH_OVERCONSTRAINED",
        f"NX cannot solve this {kind.replace('_', ' ')} constraint: {risk['reason']} "
        f"({', '.join(curves)}). NX would delete the collapsed curves behind a dialog that "
        "blocks NX, so the item was not sent to NX",
        field=field,
        hint=hint,
        details=details,
    )


def _create_and_drag(tx, sk, kind, objs, field=None):
    """Constraints the builders do not cover: create natively, then solve by a drag."""
    s = sk.sketch
    if kind in ("horizontal", "vertical"):
        line = objs["curves"][0]
        create = (
            s.CreateHorizontalConstraint if kind == "horizontal" else s.CreateVerticalConstraint
        )
        with _refusal(kind + " constraint", field):
            create(ksketch.constraint_geometry(tx, line))
        return ksketch.drag(tx, sk, line, "end")
    (src, which), target = objs["points"][0], objs["curves"][0]
    p = _local(tx, sk, src, which)
    goal = _local(tx, sk, target, "mid") if kind == "midpoint" else _foot(tx, sk, target, p)[0]
    nx = tx.nx
    point_geometry = ksketch.constraint_geometry(tx, src, which)
    target_geometry = ksketch.constraint_geometry(tx, target)
    help_ = nx.Sketch.ConstraintGeometryHelp()
    help_.Type = nx.Sketch.ConstraintGeometryHelpType.Point
    help_.Point = sk.point(goal)
    help_.Parameter = 0.0
    with _refusal(kind.replace("_", " ") + " constraint", field):
        if kind == "midpoint":
            s.CreateMidpointConstraint(point_geometry, target_geometry)
        s.CreatePointOnCurveConstraint(
            ksketch.constraint_geometry(tx, src, which), target_geometry, help_
        )
    return ksketch.drag(tx, sk, src, which, goal)


def _fix(tx, sk, objs, field=None):
    s = sk.sketch
    with _refusal("fixed constraint", field):
        if objs.get("points"):
            (curve, which) = objs["points"][0]
            s.CreateFixedConstraint(ksketch.constraint_geometry(tx, curve, which))
        else:
            # A plain Fixed constraint holds only the infinite line on NX 2406.
            s.CreateFullyFixedConstraints(ksketch.constraint_geometry(tx, objs["curves"][0]))


def _resolve_item(tx, sk, item, field):
    """Live objects of one constraint item, with the curve-type rules checked."""
    kind = item["type"]
    objs = {"curves": [], "points": []}
    for i, ref in enumerate(item.get("curves", [])):
        objs["curves"].append(common.owned_curve(tx, sk, ref, f"{field}/curves/{i}"))
    for i, point in enumerate(item.get("points", [])):
        where = f"{field}/points/{i}"
        curve = common.owned_curve(tx, sk, point["curve"], where + "/curve")
        ksketch.check_point(tx, curve, point["point"], where + "/point", VERTEX)
        objs["points"].append((curve, point["point"]))
    if "centerline" in item:
        objs["centerline"] = common.owned_curve(tx, sk, item["centerline"], field + "/centerline")
    lines, arcs = ("line",), ("arc", "circle")
    if kind == "fixed" and bool(objs["curves"]) == bool(objs["points"]):
        fail("NX_INVALID_ARGUMENT", "fixed takes one curve or one point", field=field + "/curves")
    if kind in ("horizontal", "vertical", "parallel", "perpendicular", "collinear", "equal_length"):
        for i, c in enumerate(objs["curves"]):
            _require(tx, c, lines, f"{field}/curves/{i}", "a sketch line")
    if kind in ("equal_radius", "concentric"):
        for i, c in enumerate(objs["curves"]):
            _require(tx, c, arcs, f"{field}/curves/{i}", "a sketch arc or circle")
    if kind == "tangent":
        a, c = objs["curves"]
        for i, curve in enumerate((a, c)):
            _require(tx, curve, lines + arcs, f"{field}/curves/{i}", "a line, arc or circle")
        if ksketch.curve_kind(tx, a) == ksketch.curve_kind(tx, c) == "line":
            fail("NX_WRONG_GEOMETRY", "Tangency needs an arc or circle", field=field + "/curves")
    if kind == "symmetric":
        for i, c in enumerate(objs["curves"]):
            _require(tx, c, lines, f"{field}/curves/{i}", "a sketch line")
        _require(tx, objs["centerline"], lines, field + "/centerline", "a straight centerline")
    if kind == "midpoint":
        _require(tx, objs["curves"][0], lines + ("arc",), field + "/curves/0", "a line or open arc")
    if kind == "point_on_curve":
        _require(tx, objs["curves"][0], lines + arcs, field + "/curves/0", "a line, arc or circle")
    tags = [int(c.Tag) for c in objs["curves"]] + [int(c.Tag) for c, _ in objs["points"]]
    if "centerline" in objs:
        tags.append(int(objs["centerline"].Tag))
    if kind not in ("fixed", "horizontal", "vertical", "coincident") and len(set(tags)) != len(
        tags
    ):
        fail("NX_INVALID_ARGUMENT", "Use different curves", field=field)
    if kind == "coincident" and objs["points"][0] == objs["points"][1]:
        fail("NX_INVALID_ARGUMENT", "Select two different points", field=field + "/points")
    return objs


def _not_created(tx, sk, kind, objs, field):
    """NX made no constraint: say whether it exists already or which constraint on these
    curves contradicts it, with every constraint on them in the details."""
    curves = list(objs["curves"]) + [c for c, _ in objs["points"]]
    if "centerline" in objs:
        curves.append(objs["centerline"])
    existing, seen = [], set()
    for curve in curves:
        for c in ksketch.attached(tx, sk, curve):
            if int(c.Tag) not in seen:
                seen.add(int(c.Tag))
                existing.append(
                    {
                        "constraint": tx.ref(c, "sketch_constraint")["id"],
                        "type": ksketch.constraint_type(tx, c),
                        "curve": tx.ref(curve, "curve")["id"],
                    }
                )
    same = [e for e in existing if e["type"] == kind]
    opposite = {"horizontal": "vertical", "vertical": "horizontal"}.get(kind)
    against = [e for e in existing if e["type"] in (opposite, "fixed")] if opposite else []
    if same:
        message = f"{same[0]['curve']} already has a {kind} constraint ({same[0]['constraint']})"
        hint = "Drop this item: the constraint exists already"
    elif against:
        e = against[0]
        message = (
            f"NX created no {kind} constraint: {e['curve']} is constrained {e['type']} "
            f"({e['constraint']})"
        )
        hint = (
            "Delete that constraint first (nx_edit_sketch_curves {type: delete, object: "
            f"{e['constraint']!r}}}), then add {kind}"
        )
    else:
        message = (
            f"NX created no {kind} constraint: it exists already or contradicts the "
            "constraints on these curves (details.existing)"
        )
        hint = "Drop the item, or delete the conflicting constraint with nx_edit_sketch_curves"
    fail(
        "NX_CONSTRAINT_UNSATISFIED",
        message,
        field=field,
        hint=hint,
        details={"existing": existing},
    )


# NX's name of each constraint type where it differs from the tool's.
NATIVE_TYPE = {"symmetric": "mirror"}


def _uses(objs) -> set:
    """``{(tag, point)}`` an item constrains: whole curves as ``curve``, vertices by name."""
    uses = {(int(c.Tag), "curve") for c in objs["curves"]}
    uses |= {(int(c.Tag), which) for c, which in objs["points"]}
    if "centerline" in objs:
        uses.add((int(objs["centerline"].Tag), "curve"))
    return uses


def _existing(tx, sk, kind, objs):
    """A persistent constraint of the same type on the same geometry as the item, or None:
    the item is then a no-op (NX would refuse it as "Persistent relation already exists")."""
    if kind == "fixed" and objs["curves"]:
        return None  # a plain Fixed on a line holds less than the fully fixed set
    want = _uses(objs)
    tags = {t for t, _w in want}
    points = {(int(c.Tag), w) for c, w in objs["points"]}
    native_kind = NATIVE_TYPE.get(kind, kind)
    curve = objs["curves"][0] if objs["curves"] else objs["points"][0][0]
    for c in ksketch.attached(tx, sk, curve):
        if ksketch.constraint_type(tx, c) != native_kind:
            continue
        have = {(int(o.Tag), w) for o, w in (kdof.geometry(tx, c) or [])}
        if {t for t, _w in have} == tags and points <= have:
            return c
    return None


class AppliedConstraint(TypedDict, total=False):
    item: int
    type: str
    constraints: list[result.Row]
    already_existed: Annotated[bool, Doc("The same constraint was there: nothing was added")]
    residual: float
    drag_status: str


class _ConstraintsData(TypedDict):
    sketch: result.Row
    constraints: list[AppliedConstraint]
    status: common.Status


class ConstraintsData(_ConstraintsData, total=False):
    moved: Annotated[list[str], Doc(common.MOVED_DOC)]


@tool(
    effect="model",
    label="Add sketch constraints",
    summary="Add {constraints} to sketch {sketch}",
    keywords=(
        "constraint",
        "parallel",
        "perpendicular",
        "tangent",
        "coincident",
        "collinear",
        "fix",
        "relation",
    ),
    requires=("work_part",),
    display_bound=True,  # activates the sketch: NX 2406 faults outside the shown part
    variants=CONSTRAINT_TYPES,
    cost="light",
    batchable=True,
    previewable=True,
    invalidates="deleted",
    example={"sketch": "s:3", "constraints": [{"type": "horizontal", "curves": ["c:7"]}]},
)
def nx_create_sketch_constraints(
    tx: Tx,
    *,
    sketch: SketchRef,
    constraints: Annotated[
        list[ConstraintSpec], Items(1, 50), Doc("Fields per type: nx_get_tool_schemas(tool#type)")
    ],
) -> ConstraintsData:
    """Add 1-50 geometric constraints to a sketch; each is solved and checked on the geometry.
    The second curve moves (moved: others the solve moved); fixed geometry never moves. An
    equal existing constraint is reused. Conflicts roll back as solver_failed."""
    named = common.natural_items(
        constraints, "type", list(CONSTRAINT_TYPES), "/constraints", CONSTRAINT_ALIASES
    )
    items = common.check_items(
        named, CONSTRAINT_TYPES, "type", "/constraints", "nx_create_sketch_constraints"
    )
    tx.frame = "sketch_local"
    with tx.checks():  # every ref and curve kind before the sketch is opened
        pre = ksketch.Editing(tx, ksketch.resolve(tx, sketch), False)
        for i, item in enumerate(items):
            _resolve_item(tx, pre, item, f"/constraints/{i}")
    with ksketch.editing(tx, sketch) as sk:
        before = {int(c.Tag) for c in ksketch.constraints(tx, sk)}
        fixed = common.fixed_positions(tx, sk)
        shapes = ksketch.shapes(tx, sk)
        applied: list[tuple] = []
        rows: list[AppliedConstraint] = []
        touched: list = []
        for i, item in enumerate(items):
            tx.checkpoint(done=i, total=len(items))
            field, kind = f"/constraints/{i}", item["type"]
            objs = _resolve_item(tx, sk, item, field)
            curves = _item_curves(objs)
            touched += curves
            row: AppliedConstraint = {"item": i, "type": kind}
            same = _existing(tx, sk, kind, objs)
            if same is not None:
                row["constraints"] = [tx.ref(same, "sketch_constraint")]
                row["already_existed"] = True
                row["residual"] = _residual(tx, sk, kind, objs)[0]
                rows.append(row)
                continue
            _refuse_collapse(tx, sk, kind, objs, field)
            known = {int(c.Tag) for c in ksketch.constraints(tx, sk)}
            try:
                _apply(tx, sk, kind, objs, row, field)
            except NXError as error:
                if error.code == "NX_SKETCH_OVERCONSTRAINED":
                    common.explain_conflict(tx, sk, error, curves, before, field)
                raise
            created = [c for c in ksketch.constraints(tx, sk) if int(c.Tag) not in known]
            if not created:
                _not_created(tx, sk, kind, objs, field)
            residual, tol = _residual(tx, sk, kind, objs)
            if not residual <= tol:
                fail(
                    "NX_CONSTRAINT_UNSATISFIED",
                    f"The {kind} constraint is not satisfied after the solve; rolled back",
                    field=field,
                    details={"residual": residual, "drag_status": row.get("drag_status")},
                )
            row["constraints"] = [tx.ref(c, "sketch_constraint") for c in created]
            row["residual"] = residual
            applied.append((kind, objs, field))
            rows.append(row)
            common.require_fixed_unmoved(tx, fixed, field)
            if kind == "fixed":
                fixed = common.fixed_positions(tx, sk)
        for kind, objs, field in applied:  # later constraints must not break earlier ones
            residual, tol = _residual(tx, sk, kind, objs)
            if not residual <= tol:
                fail(
                    "NX_CONSTRAINT_UNSATISFIED",
                    f"A later constraint broke the {kind} constraint; rolled back",
                    field=field,
                    details={"residual": residual},
                )
        status = common.require_solved(tx, sk, before, touched)
        data: ConstraintsData = {
            "sketch": tx.ref(sk.sketch, "sketch"),
            "constraints": rows,
            "status": status,
        }
        moved = common.report_moved(tx, sk, shapes, _named_ids(tx, applied))
        if moved:
            data["moved"] = moved
        return data


def _item_curves(objs) -> list:
    """The curves an item names (each once)."""
    out = list(objs["curves"]) + [c for c, _w in objs["points"]]
    if "centerline" in objs:
        out.append(objs["centerline"])
    return list({int(c.Tag): c for c in out}.values())


def _named_ids(tx, applied) -> set:
    """Ids of the curves the requests name (not reported as moved by the solve)."""
    return {tx.ref(c, "curve")["id"] for _kind, objs, _f in applied for c in _item_curves(objs)}


def _apply(tx, sk, kind, objs, row, field):
    """Create and solve one constraint item."""
    if kind == "fixed":
        _fix(tx, sk, objs, field)
    elif kind == "symmetric":
        _symmetric(tx, sk, objs, field)
    elif kind in ("horizontal", "vertical", "midpoint", "point_on_curve"):
        row["drag_status"] = _create_and_drag(tx, sk, kind, objs, field)
    else:
        _make_relation(tx, sk, kind, objs, field)
    with native(default="NX_UPDATE_FAILED"):
        sk.sketch.Update()


# ---------------------------------------------------------------------------
# Dimensions

Target = CurveRef | SketchPoint
DIM_KINDS = (
    "length",
    "horizontal",
    "vertical",
    "distance",
    "radius",
    "diameter",
    "angle",
    "parallel_distance",
    "perpendicular_distance",
)


# A number in part units (degrees for angle), a unit string ('40 mm', '1.5 in', '30 deg')
# or an NX expression name or formula ('width/2'), which keeps the dimension driven by it.
Value = Annotated[float | Annotated[str, Text(40)], Doc("Part units, '40 mm' or a formula")]
ExpName = Annotated[
    str, Text(64, r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"), Doc("Name of its expression, e.g. width")
]


class _DimKind(TypedDict):
    kind: (
        Literal[
            "length",
            "horizontal",
            "vertical",
            "distance",
            "radius",
            "diameter",
            "angle",
            "parallel_distance",
            "perpendicular_distance",
        ]
        | Loose
    )
    first: Annotated[Target, Doc("A curve, or a point {curve, point}")]


class DimensionSpec(_DimKind, total=False):
    second: Target
    value: Annotated[
        float | Annotated[str, Text(40)],
        Doc("Part units, '40 mm' or a formula ('w/2'); angle: deg or '30 deg'; omit: current size"),
    ]
    origin: XY
    reference: bool
    name: ExpName


# Synonyms agents use; case, spaces and hyphens are ignored anyway.
DIMENSION_ALIASES = {
    "linear": "length",
    "line_length": "length",
    "radial": "radius",
    "diametral": "diameter",
    "diametric": "diameter",
    "dia": "diameter",
    "angular": "angle",
    "horizontal_distance": "horizontal",
    "vertical_distance": "vertical",
    "point_to_point": "distance",
    "point_distance": "distance",
    "aligned": "distance",
    "perpendicular": "perpendicular_distance",
    "point_to_line": "perpendicular_distance",
    "line_to_line": "parallel_distance",
}


class _OneCurveDim(TypedDict):
    kind: Literal["length", "radius", "diameter"]
    first: Annotated[CurveRef, Doc("length: a line; radius/diameter: an arc or circle")]


class OneCurveDimension(_OneCurveDim, total=False):
    value: Value
    origin: Point2
    reference: bool
    name: ExpName


class _AxisDim(TypedDict):
    kind: Literal["horizontal", "vertical"]
    first: Annotated[Target, Doc("A line, or a point with second")]


class AxisDimension(_AxisDim, total=False):
    second: SketchPoint
    value: Value
    origin: Point2
    reference: bool
    name: ExpName


class _PointDim(TypedDict):
    kind: Literal["distance"]
    first: SketchPoint
    second: SketchPoint


class PointDimension(_PointDim, total=False):
    value: Value
    origin: Point2
    reference: bool
    name: ExpName


class _LinesDim(TypedDict):
    kind: Literal["angle", "parallel_distance"]
    first: Annotated[CurveRef, Doc("A line")]
    second: Annotated[CurveRef, Doc("A line (parallel for parallel_distance)")]


class LinesDimension(_LinesDim, total=False):
    value: Annotated[
        float | Annotated[str, Text(40)],
        Doc("Part units or '40 mm'; angle: between start->end directions, 0-180 deg"),
    ]
    origin: Point2
    reference: bool
    name: ExpName


class _PerpDim(TypedDict):
    kind: Literal["perpendicular_distance"]
    first: Annotated[CurveRef, Doc("A line")]
    second: SketchPoint


class PerpendicularDimension(_PerpDim, total=False):
    value: Value
    origin: Point2
    reference: bool
    name: ExpName


DIMENSION_TYPES = {
    "length": OneCurveDimension,
    "radius": OneCurveDimension,
    "diameter": OneCurveDimension,
    "horizontal": AxisDimension,
    "vertical": AxisDimension,
    "distance": PointDimension,
    "angle": LinesDimension,
    "parallel_distance": LinesDimension,
    "perpendicular_distance": PerpendicularDimension,
}
forbid_extra(*set(DIMENSION_TYPES.values()), DimensionSpec)


class _Dim:
    """One requested dimension, resolved: native type, geometry, measuring function."""

    def __init__(self, tx, sk, item, field):
        self.tx, self.sk, self.item, self.field = tx, sk, item, field
        self.kind = item["kind"]
        kind = self.kind
        first, second = item["first"], item.get("second")
        point_first = isinstance(first, dict)
        curve_first = ("length", "radius", "diameter", "angle", "parallel_distance")
        if point_first and kind in (*curve_first, "perpendicular_distance"):
            fail("NX_INVALID_ARGUMENT", f"{kind} needs a curve as first", field=field + "/first")
        if kind in ("length", "radius", "diameter") and second is not None:
            fail("NX_INVALID_ARGUMENT", f"{kind} takes no second", field=field + "/second")
        self.a = self._curve(first, "/first")
        self.pa = first["point"] if point_first else None
        self.b = self._curve(second, "/second") if second is not None else None
        self.pb = second["point"] if isinstance(second, dict) else None
        line, arcs = ("line",), ("arc", "circle")
        if kind == "length":
            _require(tx, self.a, line, field + "/first", "a sketch line")
        elif kind in ("radius", "diameter"):
            _require(tx, self.a, arcs, field + "/first", "a sketch arc or circle")
        elif kind in ("horizontal", "vertical"):
            if point_first:
                if second is None or not isinstance(second, dict):
                    fail(
                        "NX_INVALID_ARGUMENT",
                        "A point dimension needs a second point",
                        field=field + "/second",
                    )
            else:
                _require(tx, self.a, line, field + "/first", "a sketch line or a point")
                if second is not None:
                    fail(
                        "NX_INVALID_ARGUMENT",
                        "A line dimension takes no second",
                        field=field + "/second",
                    )
        elif kind == "distance":
            if not point_first or not isinstance(second, dict):
                fail(
                    "NX_INVALID_ARGUMENT", "distance is between two points", field=field + "/second"
                )
        elif kind in ("angle", "parallel_distance"):
            if second is None or isinstance(second, dict):
                fail("NX_INVALID_ARGUMENT", f"{kind} needs a second line", field=field + "/second")
            _require(tx, self.a, line, field + "/first", "a sketch line")
            _require(tx, self.b, line, field + "/second", "a sketch line")
        else:  # perpendicular_distance
            if not isinstance(second, dict):
                fail(
                    "NX_INVALID_ARGUMENT",
                    "perpendicular_distance needs a second point",
                    field=field + "/second",
                )
            _require(tx, self.a, line, field + "/first", "a sketch line")
        for which, curve, where in ((self.pa, self.a, "/first"), (self.pb, self.b, "/second")):
            if which is not None:
                ksketch.check_point(tx, curve, which, field + where + "/point")
        if self.b is not None and int(self.a.Tag) == int(self.b.Tag) and self.pa == self.pb:
            fail(
                "NX_INVALID_ARGUMENT",
                "Select two different curves or points",
                field=field + "/second",
            )
        if kind == "parallel_distance":
            u, v = _direction(tx, sk, self.a), _direction(tx, sk, self.b)
            if abs(geom.cross2(u, v)) > 1e-6 * geom.norm(u) * geom.norm(v):
                fail(
                    "NX_INVALID_ARGUMENT",
                    "parallel_distance needs parallel lines; make them parallel first",
                    field=field + "/second",
                )

    def _curve(self, target, where):
        ref = target["curve"] if isinstance(target, dict) else target
        return common.owned_curve(
            self.tx,
            self.sk,
            ref,
            self.field + where + ("/curve" if isinstance(target, dict) else ""),
        )

    # -- measuring the current geometry (sketch-local) --------------------------------
    def points(self):
        tx, sk = self.tx, self.sk
        if self.pa is not None:
            return _local(tx, sk, self.a, self.pa), _local(tx, sk, self.b, self.pb)
        return _local(tx, sk, self.a, "start"), _local(tx, sk, self.a, "end")

    def measure(self):
        tx, sk, kind = self.tx, self.sk, self.kind
        if kind in ("length", "distance"):
            p, q = self.points()
            return geom.dist(p, q)
        if kind in ("horizontal", "vertical"):
            p, q = self.points()
            return abs(q[0] - p[0]) if kind == "horizontal" else abs(q[1] - p[1])
        if kind in ("radius", "diameter"):
            r = float(self.a.Radius)
            return r if kind == "radius" else 2 * r
        a0, a1 = _local(tx, sk, self.a, "start"), _local(tx, sk, self.a, "end")
        if kind == "angle":
            u, v = geom.sub(a1, a0), _direction(tx, sk, self.b)
            return math.degrees(math.atan2(abs(geom.cross2(u, v)), geom.dot(u, v)))
        if kind == "parallel_distance":
            return _line_distance(a0, a1, _local(tx, sk, self.b, "start"))
        return _line_distance(a0, a1, _local(tx, sk, self.b, self.pb))

    def tolerance(self):
        if self.kind == "angle":
            return 1e-6
        p, q = self.points() if self.kind not in ("radius", "diameter") else ([0, 0], [0, 0])
        return geom.tol(p, q, [self.measure()]) * 10

    def matches(self, value, wanted):
        if self.kind == "angle":
            return min(abs(value - wanted), abs(180.0 - value - wanted)) <= self.tolerance()
        return abs(value - wanted) <= self.tolerance()

    def origin(self):
        """Label position: the request, else beside the measured geometry (cosmetic)."""
        if self.item.get("origin") is not None:
            return list(self.item["origin"])
        tx, sk, kind = self.tx, self.sk, self.kind
        if kind in ("radius", "diameter"):
            c = _local(tx, sk, self.a, "center")
            r = float(self.a.Radius)
            return [c[0] + 0.7 * r, c[1] + 0.7 * r]
        if kind == "angle":
            ma = geom.scale(geom.add(*self.points()), 0.5)
            mb = geom.scale(
                geom.add(_local(tx, sk, self.b, "start"), _local(tx, sk, self.b, "end")), 0.5
            )
            return geom.scale(geom.add(ma, mb), 0.5)
        if kind in ("parallel_distance", "perpendicular_distance"):
            ma = geom.scale(geom.add(*self.points()), 0.5)
            which = "start" if kind == "parallel_distance" else self.pb
            return geom.scale(geom.add(ma, _local(tx, sk, self.b, which)), 0.5)
        p, q = self.points()
        mid = geom.scale(geom.add(p, q), 0.5)
        offset = 0.15 * max(geom.dist(p, q), 1.0) + 1.0
        return [mid[0] + offset, mid[1]] if kind == "vertical" else [mid[0], mid[1] + offset]

    # -- creating the native dimension --------------------------------------------------
    def create(self):
        tx, sk, kind = self.tx, self.sk, self.kind
        nx = tx.nx
        mode = (
            nx.Sketch.DimensionOption.CreateAsReference
            if self.item.get("reference")
            else nx.Sketch.DimensionOption.CreateAsDriving
        )
        at = sk.point(self.origin())
        s = sk.sketch
        with _refusal(kind.replace("_", " ") + " dimension"):
            if kind in ("radius", "diameter"):
                fn = s.CreateRadialDimension if kind == "radius" else s.CreateDiameterDimension
                return fn(ksketch.dimension_geometry(tx, self.a), at, None, mode)
            if kind == "angle":
                one, two = (
                    ksketch.dimension_geometry(tx, self.a),
                    ksketch.dimension_geometry(tx, self.b),
                )
                native_type = "AngularDim"
            elif kind in ("parallel_distance", "perpendicular_distance"):
                one = ksketch.dimension_geometry(tx, self.a)
                which = "start" if kind == "parallel_distance" else self.pb
                two = ksketch.dimension_geometry(tx, self.b, which)
                native_type = "PerpendicularDim"
            else:
                if self.pa is not None:
                    one = ksketch.dimension_geometry(tx, self.a, self.pa)
                    two = ksketch.dimension_geometry(tx, self.b, self.pb)
                else:
                    one = ksketch.dimension_geometry(tx, self.a, "start")
                    two = ksketch.dimension_geometry(tx, self.a, "end")
                native_type = {"horizontal": "HorizontalDim", "vertical": "VerticalDim"}.get(
                    kind, "ParallelDim"
                )
            return s.CreateDimension(
                getattr(nx.Sketch.ConstraintType, native_type), one, two, at, None, mode
            )


class DimensionRow(TypedDict, total=False):
    item: int
    kind: str
    dimension: result.Row
    expression: common.ExpressionRow
    value_before: float
    measured: float
    supplement: Annotated[float, Doc("angle: the other side, 180 - measured")]
    reference: bool


class _DimensionsData(TypedDict):
    sketch: result.Row
    dimensions: list[DimensionRow]
    status: common.Status


class DimensionsData(_DimensionsData, total=False):
    moved: Annotated[list[str], Doc(common.MOVED_DOC)]


def _natural_dimension(item, field):
    """A point and a line given as ``distance`` are a perpendicular distance (line first);
    two curves are refused with what to send instead."""
    kind, first, second = item["kind"], item.get("first"), item.get("second")
    point_first, point_second = isinstance(first, dict), isinstance(second, dict)
    if kind in ("distance", "perpendicular_distance") and second is not None:
        if point_first and isinstance(second, str):  # point, line -> line, point
            return {**item, "kind": "perpendicular_distance", "first": second, "second": first}
        if kind == "distance" and isinstance(first, str) and point_second:
            return {**item, "kind": "perpendicular_distance"}
        if kind == "distance" and isinstance(first, str) and isinstance(second, str):
            fail(
                "NX_INVALID_ARGUMENT",
                "distance is between two points: give first and second as {curve, point}",
                field=field + "/first",
                hint="point is start, end, center or mid; between two parallel lines use "
                "parallel_distance, from a point to a line perpendicular_distance",
            )
    return item


def _value(tx, kind, value, field):
    """A dimension value as a float in part units (degrees for angle); an NX expression
    name or formula is evaluated and kept (a ``Formula``: the dimension follows it)."""
    if value is None or not isinstance(value, str):
        return value
    dim = "angle" if kind == "angle" else "length"
    try:
        return float(value)
    except ValueError:
        pass

    def evaluate(formula, dim_, to, field=None):
        return tx.services.evaluate_formula(formula, dim_, to, field=field, scratch=True)

    try:
        return convert_quantity(
            value,
            dim,
            "deg" if dim == "angle" else units.part_units(tx),
            evaluate,
            field + "/value",
        )
    except ValueError as error:
        example = "'30 deg' or '0.5 rad'" if dim == "angle" else "'40 mm' or '1.5 in'"
        fail(
            "NX_INVALID_ARGUMENT",
            f"value {value!r}: {error}; send a number, a string like {example}, or an "
            "expression formula ('width/2')",
            field=field + "/value",
        )


def _check_value(kind, value, field):
    if value is None:
        return
    if not value > 0 or not math.isfinite(value):
        fail("NX_INVALID_ARGUMENT", "value must be positive", field=field + "/value")
    if kind == "angle" and value >= 180.0:
        fail(
            "NX_INVALID_ARGUMENT", "angle must be between 0 and 180 degrees", field=field + "/value"
        )


def _check_names(tx, items):
    """Expression names: identifiers, unused in the part and in the call, driving only."""
    seen: dict[str, int] = {}
    for i, item in enumerate(items):
        name = item.get("name")
        if name is None:
            continue
        where = f"/dimensions/{i}/name"
        if item.get("reference"):
            fail(
                "NX_INVALID_ARGUMENT",
                "A reference dimension has no expression to name",
                field=where,
                hint="Drop name, or make the dimension driving",
            )
        if name.lower() in seen:
            fail(
                "NX_INVALID_ARGUMENT",
                f"name {name!r} is also given to dimension {seen[name.lower()]}",
                field=where,
            )
        seen[name.lower()] = i
        existing = next(
            (e for e in tx.part.Expressions if str(e.Name).lower() == name.lower()), None
        )
        if existing is not None:
            fail(
                "NX_NAME_CONFLICT",
                f"The part already has an expression {existing.Name}",
                field=where,
                hint="Choose another name; details.existing is that expression",
                details={"existing": tx.ref(existing, "expression")["id"]},
            )


def _fully_constrained(tx, sk, items):
    """Refuse a driving dimension on a fully constrained sketch before NX solves it."""
    driving = [i for i, item in enumerate(items) if not item.get("reference")]
    if not driving:
        return
    if ksketch.status(tx, sk)["status"] != "WellConstrained":
        return
    fail(
        "NX_SKETCH_OVERCONSTRAINED",
        "The sketch is fully constrained (no degree of freedom left): a driving dimension "
        "would over-constrain it",
        field=f"/dimensions/{driving[0]}",
        hint="Add it with reference: true (it measures only), or delete a driving dimension "
        "or constraint first (nx_edit_sketch_curves {type: delete, object})",
        details={"state": "well_constrained", "dof": 0},
    )


@tool(
    effect="model",
    label="Add sketch dimensions",
    summary="Dimension sketch {sketch}: {dimensions}",
    keywords=("dimension", "sketch dimension", "length", "radius", "angle", "distance", "driving"),
    requires=("work_part",),
    display_bound=True,  # activates the sketch: NX 2406 faults outside the shown part
    variants=DIMENSION_TYPES,
    cost="light",
    batchable=True,
    previewable=True,
    invalidates="deleted",
    example={"sketch": "s:3", "dimensions": [{"kind": "length", "first": "c:7", "value": 40}]},
)
def nx_create_sketch_dimensions(
    tx: Tx,
    *,
    sketch: SketchRef,
    dimensions: Annotated[
        list[DimensionSpec], Items(1, 50), Doc("Fields per kind: nx_get_tool_schemas(tool#kind)")
    ],
) -> DimensionsData:
    """Add 1-50 driving or reference sketch dimensions; each returns its expression. name names
    it, a formula value keeps it driven. The sketch is solved to every value and read back;
    fixed geometry never moves. horizontal/vertical measure the x/y extent (they do not make a
    line level). Edit a value later with nx_set_expression."""
    named = common.natural_items(
        dimensions, "kind", list(DIM_KINDS), "/dimensions", DIMENSION_ALIASES
    )
    named = [_natural_dimension(item, f"/dimensions/{i}") for i, item in enumerate(named)]
    items = common.check_items(
        named, DIMENSION_TYPES, "kind", "/dimensions", "nx_create_sketch_dimensions"
    )
    for i, item in enumerate(items):
        if item.get("value") is not None:
            item["value"] = _value(tx, item["kind"], item["value"], f"/dimensions/{i}")
        if item.get("reference") and getattr(item.get("value"), "formula", None):
            fail(
                "NX_INVALID_ARGUMENT",
                "A reference dimension measures only: it takes no formula",
                field=f"/dimensions/{i}/value",
            )
        _check_value(item["kind"], item.get("value"), f"/dimensions/{i}")
    tx.frame = "sketch_local"
    with tx.checks():  # every ref, curve kind and name before the sketch is opened
        pre = ksketch.Editing(tx, ksketch.resolve(tx, sketch), False)
        for i, item in enumerate(items):
            _Dim(tx, pre, item, f"/dimensions/{i}")
        _check_names(tx, items)
    with ksketch.editing(tx, sketch) as sk:
        _fully_constrained(tx, sk, items)
        before = {int(c.Tag) for c in ksketch.constraints(tx, sk)}
        fixed = common.fixed_positions(tx, sk)
        shapes = ksketch.shapes(tx, sk)
        rows: list[DimensionRow] = []
        made: list[tuple] = []
        touched: list = []
        for i, item in enumerate(items):
            tx.checkpoint(done=i, total=len(items))
            field = f"/dimensions/{i}"
            dim = _Dim(tx, sk, item, field)
            curves = [c for c in (dim.a, dim.b) if c is not None]
            touched += curves
            try:
                rows.append(_dimension(tx, sk, dim, item, i, made, fixed))
            except NXError as error:
                if error.code in ("NX_SKETCH_OVERCONSTRAINED", "NX_CONSTRAINT_UNSATISFIED"):
                    common.explain_conflict(
                        tx, sk, error, curves, before, field, reference=not item.get("reference")
                    )
                raise
        for i, (dim, target) in enumerate(made):  # later values must not break earlier ones
            measured = dim.measure()
            if not dim.matches(measured, target):
                fail(
                    "NX_CONSTRAINT_UNSATISFIED",
                    "A later dimension changed an earlier one; rolled back",
                    field=f"/dimensions/{i}",
                    details={"requested": target, "measured": measured},
                )
        status = common.require_solved(tx, sk, before, touched, reference=True)
        data: DimensionsData = {
            "sketch": tx.ref(sk.sketch, "sketch"),
            "dimensions": rows,
            "status": status,
        }
        moved = common.report_moved(tx, sk, shapes, {tx.ref(c, "curve")["id"] for c in touched})
        if moved:
            data["moved"] = moved
        return data


def _dimension(tx, sk, dim, item, i, made, fixed) -> DimensionRow:
    """Create one dimension, solve it to its value and read it back."""
    field = f"/dimensions/{i}"
    value, reference = item.get("value"), bool(item.get("reference"))
    size = dim.measure()
    if value is None and size <= dim.tolerance():
        fail(
            "NX_INVALID_ARGUMENT",
            "The geometry has no measurable size here; give a value",
            field=field + "/value",
        )
    if reference and value is not None and not dim.matches(size, value):
        fail(
            "NX_INVALID_ARGUMENT",
            f"A reference dimension measures {size:.6g}; it cannot drive {value:.6g}",
            field=field + "/value",
        )
    constraint = dim.create()
    # NX 2406: a reference dimension has no expression; ComputedSize measures it.
    exp = getattr(constraint, "AssociatedExpression", None)
    readback.expect(reference or exp is not None, f"dimensions/{i}/expression")
    nx_size = float(exp.Value if exp is not None else constraint.AssociatedDimension.ComputedSize)
    readback.expect(
        dim.matches(nx_size, size),
        f"dimensions/{i}/measured_by_nx",
        message="NX measured different geometry than requested",
        nx_value=nx_size,
        expected=size,
    )
    target = size if value is None or reference else float(value)
    if exp is not None and item.get("name"):
        with native(default="NX_INVALID_ARGUMENT"):
            tx.part.Expressions.Rename(exp, item["name"])
    if value is not None and not reference:
        with native(default="NX_INVALID_FORMULA"):
            tx.part.Expressions.EditExpression(exp, expr.rhs(value))
        ksketch.solve(tx, sk, [exp])
    measured = dim.measure()
    if not dim.matches(measured, target):
        fail(
            "NX_CONSTRAINT_UNSATISFIED",
            "NX could not solve the sketch to the requested value; rolled back",
            field=field + "/value",
            details={"requested": target, "measured": measured},
        )
    common.require_fixed_unmoved(tx, fixed, field)
    made.append((dim, target))
    row: DimensionRow = {
        "item": i,
        "kind": dim.kind,
        "dimension": tx.ref(constraint, "sketch_constraint"),
        "value_before": size,
        "measured": measured,
        "reference": reference,
    }
    if dim.kind == "angle":
        row["supplement"] = 180.0 - measured
    if exp is not None:
        row["expression"] = common.expression_row(tx, exp)
    return row
