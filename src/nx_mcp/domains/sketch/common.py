"""Rows, item validation and solver checks shared by the sketch tools.

Batched tools (curves, edits, constraints, dimensions) take a flat item object
with a ``type``/``kind`` field; :func:`check_items` validates each item against
its variant ``TypedDict`` (the sub-schema ``nx_get_tool_schemas`` shows for
``tool#variant``), so the listing stays small and each item is still checked
strictly.
"""

from __future__ import annotations

import contextlib
import math
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import Param
from nx_mcp.core.errors import NXError, classify, fail
from nx_mcp.core.types import Doc, Point2, Point3, Range, Vec3
from nx_mcp.core.validate import typeddict_fields, validate
from nx_mcp.kit import expr, geom, result
from nx_mcp.kit import sketch as ksketch

# Item fields of batched tools: plain numbers in part units / degrees (the listing
# budget has no room for unit strings in every nested field).
XY = tuple[float, float]
Size = Annotated[float, Range(gt=0)]
Degrees = Annotated[float, Range(ge=-3600, le=3600)]
BAD_STATES = ("OverConstrained", "InconsistentlyConstrained")


class Frame(TypedDict):
    origin: Point3
    x_axis: Vec3
    y_axis: Vec3
    normal: Vec3


class CurveRow(TypedDict, total=False):
    curve: result.Row
    type: str
    start: Point2
    end: Point2
    center: Point2
    radius: float
    start_angle: float
    end_angle: float
    major_radius: float
    minor_radius: float
    rotation: float
    length: float
    closed: bool
    construction: bool
    item: int


class ExpressionRow(TypedDict):
    expression: result.Row
    name: str
    value: float
    formula: str
    units: str


class ConstraintRow(TypedDict, total=False):
    constraint: result.Row
    type: str
    curves: list[str]
    expression: ExpressionRow


class _SketchRowsRequired(TypedDict):
    sketch: result.Row


class SketchRows(_SketchRowsRequired, total=False):
    feature: result.Row


class Status(TypedDict):
    state: str
    dof: Annotated[int | None, Doc("NX indicator; 2406: curves not fully constrained")]


def frame_row(f) -> Frame:
    return {
        "origin": f["origin"],
        "x_axis": f["x_axis"],
        "y_axis": f["y_axis"],
        "normal": f["normal"],
    }


def sketch_rows(tx, sk) -> SketchRows:
    """``{"sketch": row, "feature": row}`` of a sketch."""
    out: SketchRows = {"sketch": tx.ref(sk, "sketch")}
    feature = getattr(sk, "Feature", None)
    if feature is not None:
        out["feature"] = tx.ref(feature, "feature")
    return out


def curve_row(tx, f, curve, *, item: int | None = None) -> CurveRow:
    """A curve reference with its geometry read back in the sketch frame ``f``."""
    row: CurveRow = {"curve": tx.ref(curve, "curve"), **ksketch.read(tx, f, curve)}
    if item is not None:
        row["item"] = item
    return row


def expression_row(tx, exp) -> ExpressionRow:
    value = expr.value(tx, exp)
    return {
        "expression": tx.ref(exp, "expression"),
        "name": str(exp.Name),
        "value": float(value if value is not None else exp.Value),
        "formula": str(exp.RightHandSide),
        # NX 2406 abbreviates degrees as the degree sign; results say "deg".
        "units": {"°": "deg", "Degrees": "deg"}.get(expr.units(exp), expr.units(exp)),
    }


def constraint_row(tx, constraint, curves=None) -> ConstraintRow:
    row: ConstraintRow = {
        "constraint": tx.ref(constraint, "sketch_constraint"),
        "type": ksketch.constraint_type(tx, constraint),
    }
    if curves is not None:
        row["curves"] = curves
    exp = getattr(constraint, "AssociatedExpression", None)
    if exp is not None:
        row["expression"] = expression_row(tx, exp)
    return row


@contextlib.contextmanager
def rejected_inputs(message, code="NX_INVALID_ARGUMENT"):
    """A native NX refusal inside the block becomes ``code`` with ``message`` (nx_code kept).

    Live NX 2406: 674275 "Invalid data for Sketch Project" (an edge normal to the
    plane), 3680011 "No intersection exists between selected faces and sketch plane".
    """
    try:
        yield
    except NXError:
        raise
    except Exception as error:
        native = classify(error)
        if native.nx_code is None or native.code == "NX_LICENSE_UNAVAILABLE":
            raise native from error
        fail(code, f"{message}: {error}", nx_code=native.nx_code)


def owned_curve(tx, sk, ref, field):
    """A curve of this sketch (``wrong_owner`` otherwise)."""
    return ksketch.owns(tx, sk, ref, field=field)


def check_items(items, variants, key, field):
    """Validate every flat item against its variant TypedDict; returns plain dicts."""
    out = []
    for i, item in enumerate(items):
        where = f"{field}/{i}"
        sub = variants[item[key]]
        params = {
            name: Param(name, hint, None, name in sub.__required_keys__)
            for name, hint in typeddict_fields(sub).items()
        }
        try:
            out.append(validate(params, item).args)
        except NXError as error:
            inner = error.field or ""
            message = error.message.replace(inner, where + inner, 1) if inner else error.message
            fail(
                "NX_INVALID_ARGUMENT",
                f"{message} ({item[key]})",
                field=where + inner,
            )
    return out


def contains(row, p, tol):
    """True when sketch-local ``p`` lies on the read-back line segment or arc ``row``."""
    if row["type"] == "line":
        a, b = row["start"], row["end"]
        d = geom.sub(b, a)
        length2 = geom.dot(d, d)
        t = geom.dot(geom.sub(p, a), d) / length2
        foot = geom.add(a, geom.scale(d, t))
        slack = tol / math.sqrt(length2)
        return -slack <= t <= 1 + slack and geom.dist(p, foot) <= tol
    if row["type"] in ("arc", "circle"):
        if abs(geom.dist(p, row["center"]) - row["radius"]) > tol:
            return False
        if row["type"] == "circle":
            return True
        angle = math.degrees(math.atan2(p[1] - row["center"][1], p[0] - row["center"][0]))
        delta = (angle - row["start_angle"]) % 360.0
        slack = math.degrees(tol / row["radius"])
        sweep = row["end_angle"] - row["start_angle"]
        return delta <= sweep + slack or delta >= 360.0 - slack
    fail("NX_WRONG_GEOMETRY", "Trim and extend support sketch lines, arcs and circles")


# ---- solver state ----------------------------------------------------------------


def status(tx, sk) -> Status:
    """``{"state": "under_constrained", "dof": n or None}`` for the whole sketch."""
    state = ksketch.status(tx, sk)
    return {"state": ksketch.snake(state["status"]), "dof": state["dof"]}


def explicit_conflicts(tx, sk) -> list[dict]:
    """Directly provable conflicts the native status can miss: a nonzero line that is
    both horizontal and vertical (NX 2406 reported UnderConstrained for it)."""
    pairs = []
    for curve in ksketch.geometry(sk):
        if ksketch.curve_kind(tx, curve) != "line":
            continue
        if geom.dist(geom.xyz(curve.StartPoint), geom.xyz(curve.EndPoint)) <= 1e-6:
            continue
        found: dict[str, list] = {"horizontal": [], "vertical": []}
        for c in ksketch.attached(tx, sk, curve):
            kind = ksketch.constraint_type(tx, c)
            if kind in found:
                found[kind].append(c)
        for a in found["horizontal"]:
            for b in found["vertical"]:
                pairs.append(
                    {
                        "curve": tx.ref(curve, "curve")["id"],
                        "constraints": [
                            tx.ref(a, "sketch_constraint")["id"],
                            tx.ref(b, "sketch_constraint")["id"],
                        ],
                        "reason": "A nonzero line cannot be both horizontal and vertical",
                    }
                )
    return pairs


def require_solved(tx, sk, before=None) -> Status:
    """Fail when the sketch is over/inconsistently constrained or lost a constraint.

    ``before`` is the set of constraint tags before the change: the modern solver
    may delete a persistent constraint instead of failing.
    """
    if before is not None:
        after = {int(c.Tag) for c in ksketch.constraints(tx, sk)}
        if not before <= after:
            fail(
                "NX_CONSTRAINT_UNSATISFIED",
                "The NX solver removed an existing constraint; the change was rolled back",
                details={"removed": len(before - after)},
            )
    state = ksketch.status(tx, sk)
    explicit = explicit_conflicts(tx, sk)
    if state["status"] in BAD_STATES or explicit:
        fail(
            "NX_SKETCH_OVERCONSTRAINED",
            f"The sketch is {ksketch.snake(state['status'])} after the change; rolled back",
            hint="Diagnose with nx_check_sketch and remove the conflicting constraint",
            details={"state": ksketch.snake(state["status"]), "explicit_conflicts": explicit},
        )
    return {"state": ksketch.snake(state["status"]), "dof": state["dof"]}


# ---- fixed geometry guard ----------------------------------------------------------


def _fixed_curves(tx, sk) -> list:
    out = []
    for curve in ksketch.geometry(sk):
        if any(ksketch.constraint_type(tx, c) == "fixed" for c in ksketch.attached(tx, sk, curve)):
            out.append(curve)
    return out


def _signature(tx, curve) -> list[float]:
    kind = ksketch.curve_kind(tx, curve)
    if kind == "line":
        return geom.xyz(curve.StartPoint) + geom.xyz(curve.EndPoint)
    if kind in ("arc", "circle"):
        values = geom.xyz(curve.CenterPoint) + [float(curve.Radius)]
        if kind == "arc":
            values += ksketch.point(tx, curve, "start") + ksketch.point(tx, curve, "end")
        return values
    return []


def fixed_positions(tx, sk) -> dict:
    """Positions of every curve that carries a Fixed constraint, by tag."""
    return {int(c.Tag): (c, _signature(tx, c)) for c in _fixed_curves(tx, sk)}


def require_fixed_unmoved(tx, positions) -> None:
    """Fixed curves must not move: NX's line fix holds only the infinite line."""
    moved = []
    for curve, before in positions.values():
        after = _signature(tx, curve)
        if before and len(after) == len(before):
            error = max(abs(a - b) for a, b in zip(after, before, strict=True))
            if error > geom.tol(before, after) * 10:
                moved.append({"curve": tx.ref(curve, "curve")["id"], "moved": error})
    if moved:
        fail(
            "NX_CONSTRAINT_UNSATISFIED",
            "The change would move fixed geometry; it was rolled back",
            hint="Constrain or dimension the free geometry instead, or remove the fix",
            details={"fixed_curves_moved": moved},
        )
