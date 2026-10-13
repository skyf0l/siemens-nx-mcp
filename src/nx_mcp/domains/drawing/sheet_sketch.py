"""Sketches on a drawing sheet: 2D layout curves (lines, polylines, arcs, circles,
rectangles) placed in sheet coordinates, read back on the sheet.

NX 2406 findings encoded here:

- ``SketchCollection.CreateSketchInDraftingBuilder`` (``View`` unset) commits a sketch on
  the open sheet, view dependent in the sheet's own view (``SHT1@0``); its frame is the
  sheet: origin at the lower-left corner, axes along the sheet, in part units. The commit
  leaves it inactive (and any other sketch active): curves are added after ``Activate``.
- NX re-activates a sheet's last activated sheet sketch whenever the sheet is opened
  from the modeling display (``Sheet.Open``, also in NX's own UI); reopening the shown
  sheet does not. The drawing tools deactivate it again (``common.quiet_sheet_sketch``).
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Doc,
    Items,
    Name,
    Range,
    SheetLength,
    SheetPoint,
    forbid_extra,
)
from nx_mcp.domains.drawing.common import (
    SheetArg,
    open_or_only_sheet,
    quiet_sheet_sketch,
    sheet_of,
)
from nx_mcp.kit import builders, drawing, geom, licenses, readback, result
from nx_mcp.kit import sketch as ksketch

Degrees = Annotated[float, Range(ge=-3600, le=3600)]
FULL = 360.0


class SheetLine(TypedDict):
    type: Literal["line"]
    start: SheetPoint
    end: SheetPoint


class _SheetPolyline(TypedDict):
    type: Literal["polyline"]
    points: Annotated[list[SheetPoint], Items(2, 100)]


class SheetPolyline(_SheetPolyline, total=False):
    closed: bool


class SheetArc(TypedDict):
    type: Literal["arc"]
    center: SheetPoint
    radius: SheetLength
    start_angle: Degrees
    end_angle: Degrees


class SheetCircle(TypedDict):
    type: Literal["circle"]
    center: SheetPoint
    radius: SheetLength


class SheetRectangle(TypedDict):
    type: Literal["rectangle"]
    corner1: SheetPoint
    corner2: SheetPoint


SHEET_CURVES = {
    "line": SheetLine,
    "polyline": SheetPolyline,
    "arc": SheetArc,
    "circle": SheetCircle,
    "rectangle": SheetRectangle,
}


class _SheetCurveType(TypedDict):
    type: Literal["line", "polyline", "arc", "circle", "rectangle"]


class SheetCurve(_SheetCurveType, total=False):
    start: SheetPoint
    end: SheetPoint
    points: Annotated[list[SheetPoint], Items(2, 100)]
    closed: bool
    center: SheetPoint
    radius: SheetLength
    start_angle: Degrees
    end_angle: Annotated[Degrees, Doc("Degrees CCW from +x; arcs run CCW")]
    corner1: SheetPoint
    corner2: SheetPoint


forbid_extra(*SHEET_CURVES.values(), SheetCurve)

NEEDS = {
    "line": ("start", "end"),
    "polyline": ("points",),
    "arc": ("center", "radius", "start_angle", "end_angle"),
    "circle": ("center", "radius"),
    "rectangle": ("corner1", "corner2"),
}


class SheetCurveRow(TypedDict, total=False):
    curve: result.Row
    item: int
    type: str
    start: list[float]
    end: list[float]
    center: list[float]
    radius: float
    start_angle: float
    end_angle: float
    closed: bool


class SheetSketchData(TypedDict, total=False):
    sketch: result.Row
    sheet: result.Row
    curves: list[SheetCurveRow]
    loops: list[dict]
    bounds: list[float]


# ---- plans: every item becomes lines and arcs (sheet units) ----------------------------


def _loop(points):
    return [("line", points[k], points[(k + 1) % len(points)]) for k in range(len(points))]


def _distinct(a, b, field):
    if geom.dist(a, b) <= geom.tol(a, b):
        fail("NX_INVALID_ARGUMENT", "Two consecutive points coincide", field=field)


def plan(item, field):
    """Primitive ``("line", a, b)`` / ``("arc", center, radius, start, end)`` tuples of one
    item, in sheet units (pure; refuses before NX)."""
    kind = item["type"]
    missing = [k for k in NEEDS[kind] if item.get(k) is None]
    if missing:
        fail("NX_INVALID_ARGUMENT", f"A {kind} needs {', '.join(NEEDS[kind])}",
             field=f"{field}/{missing[0]}")  # fmt: skip
    extra = sorted(set(item) - {"type", "closed", *NEEDS[kind]})
    if extra or (kind != "polyline" and "closed" in item):
        name = extra[0] if extra else "closed"
        fail("NX_INVALID_ARGUMENT", f"A {kind} takes only {', '.join(NEEDS[kind])}",
             field=f"{field}/{name}")  # fmt: skip
    if kind == "line":
        _distinct(item["start"], item["end"], field + "/end")
        return [("line", list(item["start"]), list(item["end"]))]
    if kind == "polyline":
        points = [list(p) for p in item["points"]]
        closed = bool(item.get("closed", False))
        if closed and len(points) > 2 and geom.dist(points[0], points[-1]) <= geom.tol(*points):
            points.pop()  # the first point repeated: the closing line is implied
        if closed and len(points) < 3:
            fail("NX_INVALID_ARGUMENT", "A closed polyline needs 3 or more points",
                 field=field + "/points")  # fmt: skip
        for k in range(1, len(points) + (1 if closed else 0)):
            _distinct(points[k - 1], points[k % len(points)], f"{field}/points/{k % len(points)}")
        lines = _loop(points)
        return lines if closed else lines[:-1]
    if kind == "rectangle":
        (x1, y1), (x2, y2) = item["corner1"], item["corner2"]
        if geom.close(x1, x2) or geom.close(y1, y2):
            fail("NX_INVALID_ARGUMENT", "Rectangle width and height must be nonzero",
                 field=field + "/corner2")  # fmt: skip
        return _loop([[x1, y1], [x2, y1], [x2, y2], [x1, y2]])
    radius = float(item["radius"])
    if not radius > 0:
        fail("NX_INVALID_ARGUMENT", "radius must be positive", field=field + "/radius")
    start, end = (0.0, FULL) if kind == "circle" else (item["start_angle"], item["end_angle"])
    if not 0 < end - start <= FULL:
        fail("NX_INVALID_ARGUMENT", "Require 0 < end_angle - start_angle <= 360",
             field=field + "/end_angle")  # fmt: skip
    return [("arc", list(item["center"]), radius, float(start), float(end))]


def _polar(center, radius, angle):
    t = math.radians(angle)
    return [center[0] + radius * math.cos(t), center[1] + radius * math.sin(t)]


# ---- build and read back -----------------------------------------------------------------


def _make(tx, sk, sh, prim):
    """One curve in the active drafting sketch (sheet units -> part units)."""
    part, nx = tx.part, tx.nx
    if prim[0] == "line":
        curve = part.Curves.CreateLine(sh.point(prim[1]), sh.point(prim[2]))
    else:
        _, center, radius, start, end = prim
        curve = part.Curves.CreateArc(
            sh.point(center),
            nx.Vector3d(1.0, 0.0, 0.0),
            nx.Vector3d(0.0, 1.0, 0.0),
            radius * sh.scale,
            math.radians(start),
            math.radians(end),
        )
    return ksketch.add(tx, sk, curve)


def _row(tx, sh, curve, item):
    """A curve read back in sheet units."""
    f = {"origin": [0.0, 0.0, 0.0], "x_axis": [1.0, 0.0, 0.0], "y_axis": [0.0, 1.0, 0.0],
         "normal": [0.0, 0.0, 1.0]}  # fmt: skip
    read = ksketch.read(tx, f, curve)
    row: SheetCurveRow = {
        "curve": tx.ref(curve, "curve"),
        "item": item,
        "type": read["type"],
        "start": [v / sh.scale for v in read["start"]],
        "end": [v / sh.scale for v in read["end"]],
    }
    if read.get("closed"):
        row["closed"] = True
    if "center" in read:
        row["center"] = [v / sh.scale for v in read["center"]]
        row["radius"] = read["radius"] / sh.scale
        if read["type"] == "arc":
            row["start_angle"], row["end_angle"] = read["start_angle"], read["end_angle"]
    return row


def _check(prim, row, check, tol):
    if prim[0] == "line":
        readback.expect_close(row["start"], prim[1], check + "/start", tol)
        readback.expect_close(row["end"], prim[2], check + "/end", tol)
        return
    _, center, radius, start, end = prim
    readback.expect_close(row["center"], center, check + "/center", tol)
    readback.expect_close(row["radius"], radius, check + "/radius", tol)
    if end - start < FULL - 1e-9:
        readback.expect_close(row["start"], _polar(center, radius, start), check + "/start", tol)
        readback.expect_close(row["end"], _polar(center, radius, end), check + "/end", tol)


def _extent(rows):
    xs, ys = [], []
    for row in rows:
        if "radius" in row and row["type"] == "circle":
            c, r = row["center"], row["radius"]
            xs += [c[0] - r, c[0] + r]
            ys += [c[1] - r, c[1] + r]
        else:
            xs += [row["start"][0], row["end"][0]]
            ys += [row["start"][1], row["end"][1]]
    return [min(xs), min(ys), max(xs), max(ys)]


def _warn_outside(tx, sheet, bounds):
    length, height = drawing.size(sheet)
    if not drawing.inside(bounds, sheet):
        tx.warn(
            "NX_VIEW_OVERLAP",
            f"The curves reach past the {length:g} x {height:g} sheet (bounds {bounds})",
        )


@tool(
    effect="model",
    label="Create sheet sketch",
    summary="Sketch {curves} on drawing sheet {sheet}",
    keywords=(
        "sheet sketch",
        "drafting curves",
        "layout lines",
        "sketch on drawing",
        "2D curves",
        "draw on sheet",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    variants=SHEET_CURVES,
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={
        "sheet": "sh:3",
        "curves": [{"type": "rectangle", "corner1": [300, 40], "corner2": [380, 80]}],
    },
)
def nx_create_sheet_sketch(
    tx: Tx,
    *,
    curves: Annotated[list[SheetCurve], Items(1, 100), Doc("Fields per type: see the description")],
    sheet: SheetArg | None = None,
    name: Name | None = None,
) -> SheetSketchData:
    """Draw 2D curves on a drawing sheet (default the open one) as a new sketch. Points are
    sheet coordinates (sheet units, origin at the sheet's lower-left corner). Fields: line start,
    end; polyline points, closed; arc center, radius, start_angle, end_angle (degrees CCW);
    circle center, radius; rectangle corner1, corner2. Closed loops (rectangle, closed
    polyline, circle) bound regions nx_create_hatch can fill."""
    plans = [plan(item, f"/curves/{i}") for i, item in enumerate(curves)]
    with tx.checks():
        target = sheet_of(tx, sheet, "/sheet") if sheet is not None else open_or_only_sheet(tx)
    licenses.require(tx, ("drafting", "solid_modeling"),
                     "Sketching on a drawing sheet needs a drafting or modeling license")  # fmt: skip
    part, nx = tx.part, tx.nx
    with drawing.on_sheet(tx, target, part) as sh:
        quiet_sheet_sketch(tx)
        with builders.build(tx, part.Sketches.CreateSketchInDraftingBuilder) as b:
            sk = b.commit()
        if sk is None:
            fail("NX_BUILD_FAILED", "NX returned no sketch")
        quiet_sheet_sketch(tx)
        made = []
        sk.Activate(nx.Sketch.ViewReorient.FalseValue)
        try:
            with native(default="NX_BUILD_FAILED"):
                for i, prims in enumerate(plans):
                    tx.checkpoint(done=i, total=len(plans))
                    made.append([_make(tx, sk, sh, prim) for prim in prims])
                if name:
                    sk.SetName(name)
        finally:
            sk.Deactivate(nx.Sketch.ViewReorient.FalseValue, nx.Sketch.UpdateLevel.Model)
        tol = 1e-6 * max(drawing.size(target))
        rows = []
        for i, (prims, group) in enumerate(zip(plans, made, strict=True)):
            for k, (prim, curve) in enumerate(zip(prims, group, strict=True)):
                row = _row(tx, sh, curve, i)
                _check(prim, row, f"curves/{i}/{k}", tol)
                rows.append(row)
        bounds = _extent(rows)
        _warn_outside(tx, target, bounds)
        return {
            "sketch": tx.ref(sk, "sketch"),
            "sheet": tx.ref(target, "drawing_sheet"),
            "curves": rows,
            "loops": ksketch.loops(rows),
            "bounds": bounds,
        }
