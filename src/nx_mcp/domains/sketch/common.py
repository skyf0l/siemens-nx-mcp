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
from nx_mcp.core.types import Doc, Point2, Point3, Range, Text, Vec3
from nx_mcp.core.validate import typeddict_fields, validate
from nx_mcp.domains.sketch import dof as kdof
from nx_mcp.kit import curve as kcurve
from nx_mcp.kit import expr, geom, result
from nx_mcp.kit import sketch as ksketch
from nx_mcp.kit.args import choice as choice  # natural option spellings

# Item fields of batched tools: plain numbers in part units / degrees (the listing
# budget has no room for unit strings in every nested field).
XY = tuple[float, float]
Size = Annotated[float, Range(gt=0)]
Degrees = Annotated[float, Range(ge=-3600, le=3600)]
BAD_STATES = ("OverConstrained", "InconsistentlyConstrained")
# A free spelling next to a Literal (``Literal[...] | Loose``): the schema still lists the
# canonical values and :func:`choice` maps 'Horizontal' or 'equal radius' onto them.
Loose = Annotated[str, Text(32)]


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


class GeometryUse(TypedDict):
    curve: str
    point: Annotated[str, Doc("start, end, center, mid or curve (the whole curve)")]


class ConstraintRow(TypedDict, total=False):
    constraint: result.Row
    type: str
    curves: list[str]
    geometry: list[GeometryUse]
    reference: bool
    expression: ExpressionRow


class _SketchRowsRequired(TypedDict):
    sketch: result.Row


class SketchRows(_SketchRowsRequired, total=False):
    feature: result.Row


class _Status(TypedDict):
    state: str
    fully_constrained: bool
    dof: Annotated[int | None, Doc("Remaining degrees of freedom; None: see dof_note")]
    free_curves: Annotated[int | None, Doc("Curves not fully constrained (NX)")]


class Status(_Status, total=False):
    dof_note: Annotated[str, Doc("Why dof is None (e.g. a curve kind not modelled)")]


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


def _geometry_ref(tx, obj, sketch_tags) -> str | None:
    if sketch_tags is not None and int(obj.Tag) in sketch_tags:
        return tx.ref(obj, "curve")["id"]
    try:
        return tx.ref(obj)["id"]  # geometry outside the sketch (datum axis, edge...)
    except Exception:
        return None


def constraint_row(tx, constraint, curves=None, sketch_tags=None) -> ConstraintRow:
    """A constraint or dimension with the curves it uses, in NX order, and which point of
    each (``geometry``; ``curves`` lists each once); ``curves`` is the fallback when NX
    does not say. Reference dimensions say ``reference``."""
    row: ConstraintRow = {
        "constraint": tx.ref(constraint, "sketch_constraint"),
        "type": ksketch.constraint_type(tx, constraint),
    }
    uses = kdof.geometry(tx, constraint) if sketch_tags is not None else None
    if uses:
        geometry: list[GeometryUse] = []
        for obj, point in uses:
            ref = _geometry_ref(tx, obj, sketch_tags)
            if ref is not None:
                geometry.append({"curve": ref, "point": point})
        row["curves"] = list(dict.fromkeys(g["curve"] for g in geometry))
        row["geometry"] = geometry
    elif curves is not None:
        row["curves"] = curves
    if kdof.is_dimension(constraint) and not kdof.driving(tx, constraint):
        row["reference"] = True
    exp = getattr(constraint, "AssociatedExpression", None)
    if exp is not None:
        row["expression"] = expression_row(tx, exp)
    return row


@contextlib.contextmanager
def rejected_inputs(message, code="NX_INVALID_ARGUMENT", field=None, messages=None):
    """A native NX refusal inside the block becomes ``code`` on ``field`` with ``message``,
    or the message ``messages`` gives its nx_code (nx_code and NX's text kept).

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
        text = (messages or {}).get(native.nx_code, message)
        fail(code, f"{text}: {error}", field=field, nx_code=native.nx_code)


def owned_curve(tx, sk, ref, field):
    """A curve of this sketch (``wrong_owner`` otherwise)."""
    return ksketch.owns(tx, sk, ref, field=field)


def natural_items(items, key, options, field, aliases=None):
    """Batch items with their ``key`` (type/kind) mapped to the canonical spelling."""
    out = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            fail("NX_INVALID_ARGUMENT", "expected an object", field=f"{field}/{i}")
        value = choice(item.get(key), options, f"{field}/{i}/{key}", aliases, key)
        out.append({**item, key: value})
    return out


def fields_hint(name, sub, key="type", tool="tool"):
    """``"symmetric takes curves, centerline (required); ..."`` for a variant TypedDict;
    ``tool`` names the tool whose variant schema shows it (``nx_create_sketch_curves``)."""
    fields = [k for k in typeddict_fields(sub) if k != key]
    required = [k for k in fields if k in sub.__required_keys__]
    optional = [k for k in fields if k not in sub.__required_keys__]
    text = f"{name} takes " + ", ".join(required or ["no other field"]) + " (required)"
    if optional:
        text += "; optional: " + ", ".join(optional)
    return text + f". Full schema: nx_get_tool_schemas({tool}#{name})"


def _prefixed(details, where):
    """Validation details with every field pointer moved under the item's ``where``."""
    out = dict(details or {})
    errors = out.get("errors")
    if isinstance(errors, list):
        out["errors"] = [
            {**e, "field": where + str(e.get("field") or "")} if isinstance(e, dict) else e
            for e in errors
        ]
    return out


def check_items(items, variants, key, field, tool="tool"):
    """Validate every flat item against its variant TypedDict; returns plain dicts.

    A failure names the item's variant and lists the fields that variant takes (the
    schema hint names ``tool``); ``details.errors`` keeps every invalid field."""
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
                hint=fields_hint(item[key], sub, key, tool),
                details=_prefixed(error.details, where),
            )
    return out


def samples(tx, f, obj) -> list:
    """Sketch-local points of model edge or curve ``obj`` (start, middle, end) projected
    onto the sketch plane."""
    return [ksketch.to_local(f, kcurve.point_at(tx, obj, t)) for t in (0.0, 0.5, 1.0)]


def on_row(row, points, tol) -> bool:
    """True when every sketch-local point lies on the line, arc or circle ``row``."""
    return row["type"] in ("line", "arc", "circle") and all(contains(row, p, tol) for p in points)


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


# ---- solver side effects ---------------------------------------------------------

MOVED_DOC = "Other curves the solve moved to keep their constraints (also changes.modified)"


def report_moved(tx, sk, before, named=()) -> list[str]:
    """Every curve that changed shape since ``before`` (``ksketch.shapes``) goes to
    ``changes.modified``; returns the ids of those the request did not name (``named``:
    curve ids), which the solver moved."""
    out = []
    for curve in ksketch.moved(tx, sk, before):
        row = tx.modified(curve, "curve")
        if row["id"] not in named:
            out.append(row["id"])
    return out


# ---- solver state ----------------------------------------------------------------


def status_row(tx, sk, state, *, dof: bool = True) -> tuple[Status, dict | None]:
    """The Status of a native ``ksketch.status`` result and the checked DOF analysis
    (``dof.checked``: per curve and total, None when not computable or not asked)."""
    name = ksketch.snake(state["status"])
    count = state["dof"]  # NX 2406: curves not fully constrained (None unless solved)
    why: list[str] = []
    found = kdof.checked(tx, sk, count, why) if dof else None
    row: Status = {
        "state": name,
        "fully_constrained": name == "well_constrained",
        "dof": found["dof"] if found else None,
        "free_curves": count,
    }
    if dof and found is None and why:
        row["dof_note"] = why[0]
    return row, found


def status(tx, sk, *, dof: bool = True) -> Status:
    """``{"state", "fully_constrained", "dof", "free_curves"}`` for the whole sketch;
    ``dof=False`` skips the DOF analysis (``dof`` None)."""
    return status_row(tx, sk, ksketch.status(tx, sk), dof=dof)[0]


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


def require_solved(tx, sk, before=None, curves=None, reference=False) -> Status:
    """Fail when the sketch is over/inconsistently constrained or lost a constraint.

    ``before`` is the set of constraint tags before the change: the modern solver
    may delete a persistent constraint instead of failing. A conflict names the existing
    constraints (on ``curves``, default all) whose removal alone would solve it
    (``details.relieving``, found before the rollback).
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
        error = NXError(
            "NX_SKETCH_OVERCONSTRAINED",
            f"The sketch is {ksketch.snake(state['status'])} after the change; rolled back",
            details={"state": ksketch.snake(state["status"]), "explicit_conflicts": explicit},
        )
        explain_conflict(tx, sk, error, curves, before, reference=reference)
        raise error
    return status_row(tx, sk, state)[0]


# ---- conflicts: which existing constraints relieve them ----------------------------

CONFLICT_TRIALS = 20


def trial(tx, sk, constraint) -> dict:
    """Remove ``constraint`` on trial (invisible mark, always undone) and read the state:
    ``{"state", "relieves_conflict"}`` or ``{"error"}``."""
    session, nx = tx.session, tx.nx
    out: dict = {"relieves_conflict": False}
    mark = session.SetUndoMark(nx.Session.MarkVisibility.Invisible, "NX MCP conflict trial")
    try:
        errors = ksketch._sketch_of(sk).DeleteObjects([constraint])
        if errors is not None:
            errors.Dispose()
        state = status(tx, sk, dof=False)["state"]
        out["state"] = state
        out["relieves_conflict"] = state in (
            "under_constrained",
            "well_constrained",
        ) and not explicit_conflicts(tx, sk)
    except Exception as error:  # a trial failure is reported, not raised
        out["error"] = str(error)
    finally:
        session.UndoToMark(mark, None)
        session.DeleteUndoMark(mark, None)
    return out


def relieving(tx, sk, curves=None, before=None, limit=CONFLICT_TRIALS) -> list[str]:
    """Ids of the constraints whose removal alone solves a conflicting sketch, tried on
    the constraints of ``curves`` (default every one) that existed ``before`` (tags)."""
    if curves is None:
        candidates = ksketch.constraints(tx, sk)
    else:
        seen: dict = {}
        for curve in curves:
            for c in ksketch.attached(tx, sk, curve):
                seen.setdefault(int(c.Tag), c)
        candidates = list(seen.values())
    if before is not None:
        candidates = [c for c in candidates if int(c.Tag) in before]
    out = []
    for constraint in candidates[:limit]:
        tx.checkpoint()
        if trial(tx, sk, constraint).get("relieves_conflict"):
            out.append(tx.ref(constraint, "sketch_constraint")["id"])
    return out


def explain_conflict(tx, sk, error, curves=None, before=None, field=None, reference=False):
    """Complete a conflict error before the rollback (``NX_SKETCH_OVERCONSTRAINED``, or an
    unreachable value while the sketch is in conflict): its field, the
    existing constraints that relieve the conflict (``details.relieving``, while the sketch
    is still in conflict) and a hint that does not need the rolled-back state."""
    if error.field is None and field:
        error.field = field
    found: list[str] = []
    try:
        if ksketch.status(tx, sk)["status"] in BAD_STATES or explicit_conflicts(tx, sk):
            found = relieving(tx, sk, curves, before)
    except NXError:
        raise
    except Exception:  # the explanation is best effort; the conflict error stands
        found = []
    conflict = error.code == "NX_SKETCH_OVERCONSTRAINED"
    if not found and not conflict:
        return error  # an unreachable value, not a conflict: the error stands as it is
    error.details["relieving"] = found
    other = "add it as reference: true" if reference else "drop this item"
    if found:
        error.hint = (
            f"Delete one of the existing constraints in details.relieving first "
            f"(nx_edit_sketch_curves {{type: delete, object: {found[0]!r}}}), or {other}"
        )
    else:
        error.hint = (
            "The curves are already held by their constraints (nx_get_sketch lists them): "
            f"delete one of those first, or {other}"
        )
    return error


# ---- fixed geometry guard ----------------------------------------------------------


# A Fixed constraint on one vertex holds only that point (``CreateFixedConstraint`` with
# a vertex); on the curve itself (or NX's fully fixed set) it holds the whole curve.
FIXED_POINTS = {"StartVertex": "start", "EndVertex": "end", "ArcCenter": "center"}


def _fixed_point(tx, constraint, curve):
    """The vertex (start/end/center) of ``curve`` a Fixed constraint holds, or None when it
    holds the whole curve (also when NX does not say)."""
    get = getattr(constraint, "GetGeometry", None)
    if get is None:
        return None
    try:
        geometry = list(get() or [])
    except Exception:
        return None
    for g in geometry:
        held = getattr(g, "Geometry", None)
        if held is not None and int(held.Tag) == int(curve.Tag):
            name = result.enum_name(g.PointType, tx.nx.Sketch.ConstraintPointType)
            return FIXED_POINTS.get(name)
    return None


def _signature(tx, curve, points=None) -> list[float]:
    """Coordinates that must not move: the fixed ``points`` of ``curve``, else its shape."""
    if points:
        return [v for which in sorted(points) for v in ksketch.point(tx, curve, which)]
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
    """What every Fixed constraint holds, by curve tag: ``(curve, points, signature,
    constraints)`` with ``points`` None for the whole curve, else the fixed vertices."""
    out = {}
    for curve in ksketch.geometry(sk):
        holders = [
            c for c in ksketch.attached(tx, sk, curve) if ksketch.constraint_type(tx, c) == "fixed"
        ]
        if not holders:
            continue
        held = [_fixed_point(tx, c, curve) for c in holders]
        points = None if None in held else set(held)
        out[int(curve.Tag)] = (curve, points, _signature(tx, curve, points), holders)
    return out


def require_fixed_unmoved(tx, positions, field=None) -> None:
    """Fixed geometry must not move: NX's line fix holds only the infinite line, and a
    fixed vertex must stay put while the rest of its curve may move."""
    moved = []
    for curve, points, before, holders in positions.values():
        after = _signature(tx, curve, points)
        if before and len(after) == len(before):
            error = max(abs(a - b) for a, b in zip(after, before, strict=True))
            if error > geom.tol(before, after) * 10:
                moved.append(
                    {
                        "curve": tx.ref(curve, "curve")["id"],
                        "moved": error,
                        "fixed": sorted(points) if points else "curve",
                        "fixed_by": [tx.ref(c, "sketch_constraint")["id"] for c in holders],
                    }
                )
    if moved:
        what = ", ".join(
            m["curve"] + ("" if m["fixed"] == "curve" else " (" + "/".join(m["fixed"]) + ")")
            for m in moved
        )
        fail(
            "NX_CONSTRAINT_UNSATISFIED",
            f"The change would move fixed geometry {what}; it was rolled back",
            hint="Constrain or dimension the free geometry instead, or delete the Fixed "
            "constraint first (nx_edit_sketch_curves {type: delete, object: <fixed_by id>})",
            field=field,
            details={"fixed_curves_moved": moved},
        )


def pick_view(tx, part):
    """The model view a pick (a point on a curve: coincident and concentric snaps, trim and
    extend) is made in: the work view of the displayed part. NX 2406 refuses
    ``ModelingViews.WorkView`` of a part that is not displayed (a sketch in a quiet
    background work part, gate 4s: "only on a displayed part"); its modeling views still
    answer, and a pick point that lies on the curve picks the same piece in any of them."""
    shown = tx.session.Parts.Display
    if shown is not None and int(shown.Tag) == int(part.Tag):
        return part.ModelingViews.WorkView
    for view in part.ModelingViews:
        return view
    return part.ModelingViews.WorkView  # no modeling view: NX's own refusal
