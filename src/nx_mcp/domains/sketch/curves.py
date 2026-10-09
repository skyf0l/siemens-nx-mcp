"""Sketch curves (tool-surface 3.5): create 1-100 curves, edit 1-100 curves.

NX 2406 findings kept here:

- NX creates a zero-length line silently: coincident end points are refused
  before NX (``invalid_argument``).
- Ellipse start/end angles are polar angles (from the center, measured from the
  major axis), not parametric; full ellipses are checked against the exact AGM
  perimeter (Ramanujan II rejected valid thin ellipses).
- Closed primitives (rectangle, polygon, slot, rounded rectangle) are joined by
  coincident constraints, so later dimensions keep the profile closed.
- ``Curve.IsReference`` reads back the construction state set by
  ``ConvertToFromReferenceBuilder``.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Point2, Range, RefTo
from nx_mcp.domains.sketch import common
from nx_mcp.domains.sketch.common import XY, CurveRef, CurveRow, Degrees, Size, SketchRef
from nx_mcp.kit import builders, geom, readback, result
from nx_mcp.kit import sketch as ksketch

# ---------------------------------------------------------------------------
# Curve items: one flat schema in the listing, one sub-schema per type


class LineCurve(TypedDict):
    type: Literal["line"]
    start: Point2
    end: Point2


class ArcCurve(TypedDict):
    type: Literal["arc"]
    center: Point2
    radius: Size
    start_angle: Degrees
    end_angle: Annotated[float, Range(ge=-3600, le=3600), Doc("Degrees; arcs run CCW")]


class CircleCurve(TypedDict):
    type: Literal["circle"]
    center: Point2
    radius: Size


class _Ellipse(TypedDict):
    type: Literal["ellipse"]
    center: Point2
    major_radius: Size
    minor_radius: Size


class EllipseCurve(_Ellipse, total=False):
    rotation: Annotated[float, Range(ge=-3600, le=3600), Doc("Major axis direction, degrees")]
    start_angle: Annotated[float, Range(ge=-3600, le=3600), Doc("Polar angle from major axis")]
    end_angle: Degrees


class RectangleCurve(TypedDict):
    type: Literal["rectangle"]
    corner1: Point2
    corner2: Point2


class _Polygon(TypedDict):
    type: Literal["polygon"]
    center: Point2
    sides: Annotated[int, Range(ge=3, le=64)]
    radius: Size


class PolygonCurve(_Polygon, total=False):
    inscribed: Annotated[bool, Doc("Vertices on the radius (true) or edge midpoints (false)")]
    rotation: Annotated[float, Range(ge=-3600, le=3600), Doc("Direction of the first vertex")]


class SlotCurve(TypedDict):
    type: Literal["slot"]
    center: Point2
    width: Annotated[float, Range(gt=0), Doc("Overall length along sketch x")]
    height: Annotated[float, Range(gt=0), Doc("Width across; end radius is height/2")]


class RoundedRectangleCurve(TypedDict):
    type: Literal["rounded_rectangle"]
    center: Point2
    width: Size
    height: Size
    radius: Size


CURVE_TYPES = {
    "line": LineCurve,
    "arc": ArcCurve,
    "circle": CircleCurve,
    "ellipse": EllipseCurve,
    "rectangle": RectangleCurve,
    "polygon": PolygonCurve,
    "slot": SlotCurve,
    "rounded_rectangle": RoundedRectangleCurve,
}


class _CurveType(TypedDict):
    type: Literal[
        "line", "arc", "circle", "ellipse", "rectangle", "polygon", "slot", "rounded_rectangle"
    ]


class CurveSpec(_CurveType, total=False):
    start: XY
    end: XY
    center: XY
    corner1: XY
    corner2: XY
    radius: Size
    major_radius: Size
    minor_radius: Size
    width: Size
    height: Size
    start_angle: Degrees
    end_angle: Degrees
    rotation: Degrees
    sides: Annotated[int, Range(ge=3, le=64)]
    inscribed: bool


for _td in (*CURVE_TYPES.values(), CurveSpec):
    _td.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Plans: every item becomes primitive lines, arcs or one ellipse


def _vertices(c, sides, radius, inscribed, rotation):
    r = radius if inscribed else radius / math.cos(math.pi / sides)
    a0 = math.radians(rotation)
    return [
        [
            c[0] + r * math.cos(a0 + 2 * math.pi * k / sides),
            c[1] + r * math.sin(a0 + 2 * math.pi * k / sides),
        ]
        for k in range(sides)
    ]


def _loop(points):
    return [("line", points[k], points[(k + 1) % len(points)]) for k in range(len(points))]


def plan(item, field):
    """``(primitives, closed)`` for one validated item; pure, raises invalid_argument."""
    kind = item["type"]
    if kind == "line":
        if geom.dist(item["start"], item["end"]) <= geom.tol(item["start"], item["end"]):
            # NX 2406 creates a zero-length line silently.
            fail("NX_INVALID_ARGUMENT", "Line end points must differ", field=field + "/end")
        return [("line", list(item["start"]), list(item["end"]))], False
    if kind in ("arc", "circle"):
        start, end = item.get("start_angle", 0.0), item.get("end_angle", 360.0)
        if not 0 < end - start <= 360:
            fail(
                "NX_INVALID_ARGUMENT",
                "Require 0 < end_angle - start_angle <= 360",
                field=field + "/end_angle",
            )
        return [("arc", list(item["center"]), item["radius"], start, end)], kind == "circle"
    if kind == "ellipse":
        a, b = item["major_radius"], item["minor_radius"]
        if b > a:
            fail(
                "NX_INVALID_ARGUMENT",
                "major_radius must be >= minor_radius",
                field=field + "/minor_radius",
            )
        start, end = item.get("start_angle", 0.0), item.get("end_angle", 360.0)
        if not 0 < end - start <= 360:
            fail(
                "NX_INVALID_ARGUMENT",
                "Require 0 < end_angle - start_angle <= 360",
                field=field + "/end_angle",
            )
        prim = ("ellipse", list(item["center"]), a, b, item.get("rotation", 0.0), start, end)
        return [prim], end - start >= 360.0 - 1e-9
    if kind == "rectangle":
        (x1, y1), (x2, y2) = item["corner1"], item["corner2"]
        if geom.close(x1, x2) or geom.close(y1, y2):
            fail(
                "NX_INVALID_ARGUMENT",
                "Rectangle width and height must be nonzero",
                field=field + "/corner2",
            )
        return _loop([[x1, y1], [x2, y1], [x2, y2], [x1, y2]]), True
    if kind == "polygon":
        corners = _vertices(
            item["center"],
            item["sides"],
            item["radius"],
            item.get("inscribed", True),
            item.get("rotation", 0.0),
        )
        return _loop(corners), True
    cx, cy = item["center"]
    w, h = item["width"], item["height"]
    left, right, bottom, top = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
    if kind == "slot":
        if w <= h:
            fail("NX_INVALID_ARGUMENT", "A slot needs width > height", field=field + "/width")
        r = h / 2
        return [
            ("line", [left + r, bottom], [right - r, bottom]),
            ("arc", [right - r, cy], r, -90.0, 90.0),
            ("line", [right - r, top], [left + r, top]),
            ("arc", [left + r, cy], r, 90.0, 270.0),
        ], True
    r = item["radius"]
    if r >= min(w, h) / 2:
        fail(
            "NX_INVALID_ARGUMENT",
            "Corner radius must be less than half the shortest side",
            field=field + "/radius",
        )
    return [
        ("line", [left + r, bottom], [right - r, bottom]),
        ("arc", [right - r, bottom + r], r, 270.0, 360.0),
        ("line", [right, bottom + r], [right, top - r]),
        ("arc", [right - r, top - r], r, 0.0, 90.0),
        ("line", [right - r, top], [left + r, top]),
        ("arc", [left + r, top - r], r, 90.0, 180.0),
        ("line", [left, top - r], [left, bottom + r]),
        ("arc", [left + r, bottom + r], r, 180.0, 270.0),
    ], True


def _arc_end(c, r, angle):
    t = math.radians(angle)
    return [c[0] + r * math.cos(t), c[1] + r * math.sin(t)]


def _make(tx, sk, prim):
    """Create one primitive curve in the active sketch."""
    part, nx = tx.part, tx.nx
    f = sk.frame
    kind = prim[0]
    with native(default="NX_BUILD_FAILED"):
        if kind == "line":
            curve = part.Curves.CreateLine(sk.point(prim[1]), sk.point(prim[2]))
        elif kind == "arc":
            _, c, r, s, e = prim
            curve = part.Curves.CreateArc(
                sk.point(c),
                nx.Vector3d(*f["x_axis"]),
                nx.Vector3d(*f["y_axis"]),
                float(r),
                math.radians(s),
                math.radians(e),
            )
        else:
            _, c, a, b, rot, s, e = prim
            curve = part.Curves.CreateEllipse(
                sk.point(c),
                float(a),
                float(b),
                math.radians(s),
                math.radians(e),
                math.radians(rot),
                sk.sketch.Orientation,
            )
        return ksketch.add(tx, sk, curve)


def _check(prim, row, check):
    """The read-back row of a primitive equals the request (else verification_failed)."""
    kind = prim[0]
    if kind == "line":
        tol = geom.tol(prim[1], prim[2])
        readback.expect_close(row["start"], prim[1], check + "/start", tol)
        readback.expect_close(row["end"], prim[2], check + "/end", tol)
        return
    if kind == "arc":
        _, c, r, s, e = prim
        tol = geom.tol(c, [r, r])
        readback.expect_close(row["center"], c, check + "/center", tol)
        readback.expect_close(row["radius"], r, check + "/radius", tol)
        if e - s < 360.0 - 1e-9:
            readback.expect_close(row["start"], _arc_end(c, r, s), check + "/start", tol)
            readback.expect_close(row["end"], _arc_end(c, r, e), check + "/end", tol)
        return
    _, c, a, b, rot, s, e = prim
    tol = geom.tol(c, [a, b])
    readback.expect_close(row["center"], c, check + "/center", tol)
    readback.expect_close(row["major_radius"], a, check + "/major_radius", tol)
    readback.expect_close(row["minor_radius"], b, check + "/minor_radius", tol)
    if abs(a - b) > tol:
        error = (row["rotation"] - rot) % 180.0
        readback.expect(min(error, 180.0 - error) <= 1e-6, check + "/rotation", **row)
    if e - s < 360.0 - 1e-9:
        # An elliptical arc ends at the requested polar angles (either curve direction).
        want = [ksketch.ellipse_point(c, a, b, rot, s), ksketch.ellipse_point(c, a, b, rot, e)]
        got = [row["start"], row["end"]]
        readback.expect(
            any(
                all(geom.dist(p, q) <= tol for p, q in zip(got, order, strict=True))
                for order in (want, want[::-1])
            ),
            check + "/ends",
            expected_ends=want,
            **row,
        )
    elif "length" in row:
        perimeter = ksketch.ellipse_perimeter(a, b)
        readback.expect_close(row["length"], perimeter, check + "/perimeter", 1e-6 * perimeter)


def _join(tx, sk, curves):
    """Coincident constraints between consecutive curves of a closed primitive."""
    with native(default="NX_BUILD_FAILED"):
        for k, curve in enumerate(curves):
            nxt = curves[(k + 1) % len(curves)]
            sk.sketch.CreateCoincidentConstraint(
                ksketch.constraint_geometry(tx, curve, "end"),
                ksketch.constraint_geometry(tx, nxt, "start"),
            )


class CurvesData(TypedDict):
    sketch: result.Row
    curves: list[CurveRow]


@tool(
    effect="model",
    label="Create sketch curves",
    summary="Add {curves} to sketch {sketch}",
    keywords=("line", "arc", "circle", "rectangle", "polygon", "ellipse", "slot", "sketch curve"),
    requires=("work_part",),
    variants=CURVE_TYPES,
    cost="light",
    batchable=True,
    previewable=True,
    invalidates="deleted",
    example={
        "sketch": "s:3",
        "curves": [{"type": "rectangle", "corner1": [0, 0], "corner2": [40, 20]}],
    },
)
def nx_create_sketch_curves(
    tx: Tx,
    *,
    sketch: SketchRef,  # type: ignore[valid-type]
    curves: Annotated[
        list[CurveSpec], Items(1, 100), Doc("Fields per type: nx_get_tool_schemas(tool#type)")
    ],
    construction: Annotated[bool, Doc("Create construction (reference) curves")] = False,
) -> CurvesData:
    """Add 1-100 curves to a sketch: lines, arcs, circles, ellipses, rectangles, polygons, slots.
    Sketch-local coordinates, part units, degrees; closed shapes are joined by coincident
    constraints and every curve is read back."""
    items = common.check_items(curves, CURVE_TYPES, "type", "/curves")
    plans = [plan(item, f"/curves/{i}") for i, item in enumerate(items)]
    tx.frame = "sketch_local"
    with ksketch.editing(tx, sketch) as sk:
        made = []
        for i, (prims, closed) in enumerate(plans):
            tx.checkpoint(done=i, total=len(plans))
            group = [_make(tx, sk, prim) for prim in prims]
            if closed and len(group) > 1:
                _join(tx, sk, group)
            made.append(group)
        with native(default="NX_UPDATE_FAILED"):
            sk.sketch.Update()
        flat = [c for group in made for c in group]
        if construction:
            ksketch.set_construction(tx, flat, True)
        rows = []
        for i, ((prims, _closed), group) in enumerate(zip(plans, made, strict=True)):
            for k, (prim, curve) in enumerate(zip(prims, group, strict=True)):
                row = common.curve_row(tx, sk.frame, curve, item=i)
                _check(prim, row, f"curves/{i}/{k}")
                readback.expect(row["construction"] == construction, f"curves/{i}/construction")
                rows.append(row)
        return {"sketch": tx.ref(sk.sketch, "sketch"), "curves": rows}


# ---------------------------------------------------------------------------
# Edits


class LineEdit(TypedDict):
    type: Literal["line"]
    curve: CurveRef  # type: ignore[valid-type]
    start: Point2
    end: Point2


class ArcEdit(TypedDict):
    type: Literal["arc"]
    curve: CurveRef  # type: ignore[valid-type]
    center: Point2
    radius: Size
    start_angle: Degrees
    end_angle: Degrees


class TrimEdit(TypedDict):
    type: Literal["trim"]
    curve: CurveRef  # type: ignore[valid-type]
    boundaries: Annotated[list[CurveRef], Items(1, 20)]  # type: ignore[valid-type]
    pick: Annotated[Point2, Doc("A point on the piece to remove")]


class ExtendEdit(TypedDict):
    type: Literal["extend"]
    curve: CurveRef  # type: ignore[valid-type]
    boundaries: Annotated[list[CurveRef], Items(1, 20)]  # type: ignore[valid-type]
    pick: Annotated[Point2, Doc("A point near the end to extend")]


class ConstructionEdit(TypedDict):
    type: Literal["construction"]
    curves: Annotated[list[CurveRef], Items(1, 100)]  # type: ignore[valid-type]
    on: bool


class DeleteEdit(TypedDict):
    type: Literal["delete"]
    object: RefTo("curve", "sketch_constraint")  # type: ignore[valid-type]


EDIT_TYPES = {
    "line": LineEdit,
    "arc": ArcEdit,
    "trim": TrimEdit,
    "extend": ExtendEdit,
    "construction": ConstructionEdit,
    "delete": DeleteEdit,
}


class _EditType(TypedDict):
    type: Literal["line", "arc", "trim", "extend", "construction", "delete"]


class EditSpec(_EditType, total=False):
    curve: CurveRef  # type: ignore[valid-type]
    start: XY
    end: XY
    center: XY
    radius: Size
    start_angle: Degrees
    end_angle: Degrees
    boundaries: Annotated[list[CurveRef], Items(1, 20)]  # type: ignore[valid-type]
    pick: XY
    curves: Annotated[list[CurveRef], Items(1, 100)]  # type: ignore[valid-type]
    on: bool
    object: RefTo("curve", "sketch_constraint")  # type: ignore[valid-type]


for _td in (*EDIT_TYPES.values(), EditSpec):
    _td.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]


class EditRow(TypedDict, total=False):
    type: str
    curves: list[CurveRow]
    deleted: list[str]


class EditData(TypedDict):
    sketch: result.Row
    edits: list[EditRow]
    status: common.Status


def _alive(sk, obj):
    return int(obj.Tag) in ksketch.tags(sk)


def _length(curve):
    return float(curve.GetLength())


def _edit_shape(tx, sk, item, field):
    obj = common.owned_curve(tx, sk, item["curve"], field + "/curve")
    kind = ksketch.curve_kind(tx, obj)
    if item["type"] == "line":
        if kind != "line":
            fail("NX_WRONG_GEOMETRY", "Select a sketch line", field=field + "/curve")
        if geom.dist(item["start"], item["end"]) <= geom.tol(item["start"], item["end"]):
            fail("NX_INVALID_ARGUMENT", "Line end points must differ", field=field + "/end")
        with native(default="NX_BUILD_FAILED"):
            obj.SetEndpoints(sk.point(item["start"]), sk.point(item["end"]))
            sk.sketch.Update()
        prim = ("line", list(item["start"]), list(item["end"]))
    else:
        if kind not in ("arc", "circle"):
            fail("NX_WRONG_GEOMETRY", "Select a sketch arc or circle", field=field + "/curve")
        s, e = item["start_angle"], item["end_angle"]
        if not 0 < e - s <= 360:
            fail(
                "NX_INVALID_ARGUMENT",
                "Require 0 < end_angle - start_angle <= 360",
                field=field + "/end_angle",
            )
        with native(default="NX_BUILD_FAILED"):
            obj.SetParameters(
                float(item["radius"]), sk.point(item["center"]), math.radians(s), math.radians(e)
            )
            sk.sketch.Update()
        prim = ("arc", list(item["center"]), item["radius"], s, e)
    if not ksketch.attached(tx, sk, obj):
        _check(prim, common.curve_row(tx, sk.frame, obj), field.lstrip("/"))
        return {"type": item["type"], "curves": [common.curve_row(tx, sk.frame, obj)]}
    # NX 2406 sets the new shape even when it breaks the curve's constraints (live: a
    # horizontal line took slanted end points); a zero-length drag re-solves them.
    status = ksketch.drag(tx, sk, obj, "end" if kind == "line" else "center")
    row = common.curve_row(tx, sk.frame, obj)
    try:
        _check(prim, row, field.lstrip("/"))
    except NXError as error:
        fail(
            "NX_CONSTRAINT_UNSATISFIED",
            "The curve did not take the requested shape: its constraints hold it",
            hint="Delete or change the constraints on this curve first",
            field=field,
            details={"check": error.details.get("check"), "actual": row, "drag_status": status},
        )
    return {"type": item["type"], "curves": [row]}


def _trim_extend(tx, sk, item, field):
    obj = common.owned_curve(tx, sk, item["curve"], field + "/curve")
    limits = [
        common.owned_curve(tx, sk, ref, f"{field}/boundaries/{i}")
        for i, ref in enumerate(item["boundaries"])
    ]
    if any(int(b.Tag) == int(obj.Tag) for b in limits):
        fail("NX_INVALID_ARGUMENT", "curve cannot be its own boundary", field=field + "/boundaries")
    trim = item["type"] == "trim"
    check = field.lstrip("/")
    pick = sk.to_part(item["pick"])
    before, length = ksketch.tags(sk), _length(obj)
    part = tx.part
    sketches = part.Sketches
    factory = sketches.CreateQuickTrimBuilder if trim else sketches.CreateQuickExtendBuilder
    with builders.build(tx, factory) as b:
        b.BoundaryObjects.Add(limits)
        b.ExtendBound = False
        target = b.TrimmedCurves if trim else b.ExtendedCurves
        target.Add(obj, part.ModelingViews.WorkView, tx.nx.Point3d(*pick))
        b.Commit()
    with native(default="NX_UPDATE_FAILED"):
        sk.sketch.Update()
    alive = _alive(sk, obj)
    pieces = ([obj] if alive else []) + ksketch.new_geometry(sk, before)
    rows = [common.curve_row(tx, sk.frame, c) for c in pieces]
    tol = geom.tol(item["pick"]) * 10
    if trim:
        kept = [r["curve"]["id"] for r in rows if common.contains(r, item["pick"], tol)]
        readback.expect(not kept, check + "/trimmed", pick=item["pick"], curves=kept)
    else:
        readback.expect(alive and _length(obj) > length + tol, check + "/extended")
        boundaries = [common.curve_row(tx, sk.frame, c) for c in limits]
        row = rows[0]
        reached = any(
            common.contains(b, row[end], tol) for b in boundaries for end in ("start", "end")
        )
        readback.expect(reached, check + "/reaches_boundary", curve=row)
    out = {"type": item["type"], "curves": rows}
    if not alive:
        out["deleted"] = [item["curve"]]
    return out


def _construction(tx, sk, item, field):
    objs = [
        common.owned_curve(tx, sk, ref, f"{field}/curves/{i}")
        for i, ref in enumerate(item["curves"])
    ]
    ksketch.set_construction(tx, objs, item["on"])
    rows = [common.curve_row(tx, sk.frame, c) for c in objs]
    wrong = [r["curve"]["id"] for r in rows if r["construction"] != item["on"]]
    readback.expect(not wrong, field.lstrip("/") + "/construction", curves=wrong)
    return {"type": "construction", "curves": rows}


def _delete(tx, sk, item, field):
    obj = tx.resolve(item["object"], "curve", "sketch_constraint")
    owned = {int(c.Tag) for c in ksketch.constraints(tx, sk)} | ksketch.tags(sk)
    if int(obj.Tag) not in owned:
        fail("NX_WRONG_OWNER", "The object does not belong to this sketch", field=field + "/object")
    with native(default="NX_BUILD_FAILED"):
        errors = sk.sketch.DeleteObjects([obj])
    if errors is not None:
        try:
            count = int(errors.Length)
            messages = [str(errors.GetErrorInfo(i)) for i in range(count)]
        finally:
            errors.Dispose()
        if count:
            fail("NX_BUILD_FAILED", "NX did not delete the object: " + "; ".join(messages))
    owned = {int(c.Tag) for c in ksketch.constraints(tx, sk)} | ksketch.tags(sk)
    readback.expect(int(obj.Tag) not in owned, field.lstrip("/") + "/deleted")
    return {"type": "delete", "curves": [], "deleted": [item["object"]]}


EDITORS = {
    "line": _edit_shape,
    "arc": _edit_shape,
    "trim": _trim_extend,
    "extend": _trim_extend,
    "construction": _construction,
    "delete": _delete,
}


@tool(
    effect="model",
    label="Edit sketch curves",
    summary="Edit {edits} in sketch {sketch}",
    keywords=("trim", "extend", "move line", "construction line", "delete curve", "reshape"),
    requires=("work_part",),
    variants=EDIT_TYPES,
    cost="light",
    batchable=True,
    previewable=True,
    invalidates="deleted",
    example={
        "sketch": "s:3",
        "edits": [{"type": "line", "curve": "c:7", "start": [0, 0], "end": [50, 0]}],
    },
)
def nx_edit_sketch_curves(
    tx: Tx,
    *,
    sketch: SketchRef,  # type: ignore[valid-type]
    edits: Annotated[
        list[EditSpec], Items(1, 100), Doc("Fields per type: nx_get_tool_schemas(tool#type)")
    ],
) -> EditData:
    """Apply 1-100 edits to sketch curves in order. Reshape a line or arc, trim or extend to
    boundary curves, toggle construction, or delete a curve or constraint; a curve its
    constraints hold fails as solver_failed."""
    items = common.check_items(edits, EDIT_TYPES, "type", "/edits")
    tx.frame = "sketch_local"
    with ksketch.editing(tx, sketch) as sk:
        rows = []
        for i, item in enumerate(items):
            tx.checkpoint(done=i, total=len(items))
            rows.append(EDITORS[item["type"]](tx, sk, item, f"/edits/{i}"))
        # Trims and deletes legitimately remove constraints: only the state is checked.
        status = common.require_solved(tx, sk)
        return {"sketch": tx.ref(sk.sketch, "sketch"), "edits": rows, "status": status}
