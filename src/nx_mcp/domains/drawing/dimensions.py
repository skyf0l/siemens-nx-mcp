"""Associative drawing dimensions: linear, radius, diameter, angle and ordinate; format.

Chamfer, thickness, arc-length and baseline/chain sets live in ``dimension_kinds``;
ISO limits and fits in ``fits``.

NX 2406 findings encoded here:

- ``OrdinateDimensionBuilder`` commits only the origin plus placeholder dimensions
  without a margin on 2406: ordinates use the UF drafting calls (ordinate origin ->
  margin -> ordinate dimension), whose placement points are in part units.
- A radius or diameter on a circle seen edge-on (or as an ellipse) is refused before
  NX; a full circle has no end vertices, so ordinate start/end snaps are refused.
- NX may dimension the acute angle between two edges, its supplement or the reflex
  equivalents; the angle is verified against all four.
- Default text positions lie outside the view border (TEXT_GAP_MM, in sheet units),
  stepped outward past annotations already there; an explicit origin is kept as given.
- A linear dimension on the body's outline that stops short of the body's extent (a
  filleted plate's straight edge: 110 of 120) adds a note naming the extent and the
  outer edges (``overall``).
"""

from __future__ import annotations

import contextlib
import math
import re
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    REF_PATTERN,
    DimensionRef,
    Doc,
    EdgeRef,
    FeatureRef,
    Items,
    SheetLength,
    SheetPoint,
    Text,
    forbid_extra,
)
from nx_mcp.domains.drawing import dimension_text, fits, overall
from nx_mcp.domains.drawing.common import (
    ViewArg,
    associated,
    edge_facts,
    geometry,
    kind_of_geometry,
    report_bounds,
    true_shape,
    uf_types,
    view_of,
)
from nx_mcp.kit import args, builders, drawing, facts, geom, readback, result, select, units

MAX_ORDINATES = 50
ANGLE_TOL_DEG = 1e-3
TEXT_GAP_MM = 10.0  # Default text: clear space outside the view border (and past others).
MARGIN_GAP_MM = 15.0  # Between the view border and a default ordinate margin.
SETS = ("baseline", "chain")
METHODS = {"aligned": "PointToPoint", "horizontal": "Horizontal", "vertical": "Vertical"}
UNITS = {"mm": "Millimeters", "in": "Inches", "um": "Micrometers", "m": "Meters"}
TOLERANCES = {
    "none": "NotSet",
    "bilateral": "BilateralTwoLines",
    "symmetric": "BilateralOneLine",
    "limits": "LimitTwoLines",
    "basic": "Basic",
    "reference": "Reference",
    "fit": "LimitsAndFits",
}
FIT_DISPLAY = {
    "symbol": "FitSymbols",
    "deviations": "TolerancesOnly",
    "symbol_and_deviations": "FitSymbolsAndTolerances",
    "symbol_and_limits": "FitSymbolsAndLimits",
}
FIT_TOL_MM = 1e-7  # NX's computed fit deviations against ISO 286.
NO_DEVIATIONS = ("none", "basic", "reference")  # tolerance types that show no upper/lower


class DimensionRow(result.Row, total=False):
    value: float
    expected: float


class DimensionData(TypedDict, total=False):
    dimension: DimensionRow
    dimensions: list[DimensionRow]
    ordinate_origin: result.Row
    origin: list[float]
    margin: float
    margin_line: float
    units: str
    callout: dict
    bounds: list[float]


# ---- pure helpers -----------------------------------------------------------------------


def acute_angle_deg(a, b):
    """Acute angle (0-90 degrees) between two 2D directions; None when one is degenerate."""
    na, nb = math.hypot(*a), math.hypot(*b)
    if na <= 1e-12 or nb <= 1e-12:
        return None
    cos = abs(a[0] * b[0] + a[1] * b[1]) / (na * nb)
    return math.degrees(math.acos(min(1.0, cos)))


def angle_matches(computed, acute, tol=ANGLE_TOL_DEG):
    """NX may dimension the acute angle, its supplement or the reflex equivalents."""
    value = float(computed) % 360.0
    return any(abs(value - c) <= tol for c in (acute, 180.0 - acute, 180.0 + acute, 360.0 - acute))


def linear_value(pa, pb, axes, orientation):
    """The model distance a linear dimension measures between two points in a view."""
    d = geom.sub(pb, pa)
    x, y = geom.dot(d, axes[0]), geom.dot(d, axes[1])
    if orientation == "horizontal":
        return abs(x)
    if orientation == "vertical":
        return abs(y)
    return math.hypot(x, y)


def linear_direction(a, b, orientation, box):
    """``(anchor, direction)`` of a linear dimension's default text: from the span's middle
    out of the view border ``box`` on the span's side (above/below for horizontal, right/
    left for vertical, along the span's normal for aligned)."""
    mid = [(a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0]
    centre = [(box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0]
    if orientation == "horizontal":
        return mid, [0.0, 1.0 if mid[1] >= centre[1] else -1.0]
    if orientation == "vertical":
        return mid, [1.0 if mid[0] >= centre[0] else -1.0, 0.0]
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy) or 1.0
    normal = [-dy / length, dx / length]
    side = (mid[0] - centre[0]) * normal[0] + (mid[1] - centre[1]) * normal[1]
    if side < 0 or (side == 0 and (normal[1] < 0 or (normal[1] == 0 and normal[0] < 0))):
        normal = [-normal[0], -normal[1]]  # away from the view centre (else up or right)
    return mid, normal


def placed(tx, sh, dimension, out):
    """``out`` with the dimension's ``bounds``; warns when its text lies over a view or
    off the sheet."""
    return report_bounds(tx, out, dimension, sh, "dimension text")


def text_spot(tx, sh, view, anchor, direction):
    """Default dimension text (sheet units): TEXT_GAP_MM outside the view border along
    ``direction`` from ``anchor``, stepped outward past annotations already there; the
    other way when that lands off the sheet or in another view."""
    box = drawing.borders(tx, view)
    gap = drawing.from_mm(sh.units, TEXT_GAP_MM)[0]
    sheet = [0.0, 0.0, *drawing.size(sh.sheet)]
    taken = drawing.sheet_annotation_bounds(tx, sh.sheet)
    others = [
        drawing.borders(tx, v) for v in sh.sheet.GetDraftingViews() if int(v.Tag) != int(view.Tag)
    ]
    spots = []
    for d in (direction, [-direction[0], -direction[1]]):
        spot = drawing.free_spot(drawing.clear_of(box, anchor, d, gap), d, gap, taken)
        if drawing.contains(sheet, spot) and not any(
            drawing.contains(b, spot, gap / 4) for b in others
        ):
            return spot
        spots.append(spot)
    return spots[0]


def on_sheet_line(sheet, value, axis, field):
    """Refuse a dimension line off the sheet (``axis`` 1: a sheet y, 0: a sheet x)."""
    limit = drawing.size(sheet)[axis]
    if not 0 <= value <= limit:
        fail(
            "NX_INVALID_ARGUMENT",
            f"The dimension line would lie off the sheet at {'xy'[axis]} = {value:g} "
            f"(sheet 0 to {limit:g})",
            field=field,
            hint="Use a smaller margin (the gap from the view border), or move the view",
            details={"sheet_size": drawing.size(sheet)},
        )


def set_bounds(tx, sh, out, members, taken, what):
    """``out`` with the members' joint ``bounds``; warns when they leave the sheet, or their
    texts lie over a view or over annotations that were there before (``taken``)."""
    boxes = [b for b in (drawing.annotation_box(tx, d, sh)[0] for d in members) if b is not None]
    if not boxes:
        return out
    box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
           max(b[3] for b in boxes)]  # fmt: skip
    out["bounds"] = box
    texts = [t for t in (drawing.annotation_box(tx, d, sh)[1] for d in members) if t is not None]
    views = {r["id"]: r for t in texts for r in drawing.crowding(tx, sh.sheet, t)[0]}
    if views:
        names = ", ".join(str(r.get("name") or r.get("id")) for r in list(views.values())[:5])
        tx.warn("NX_VIEW_OVERLAP", f"The {what} have text over view {names}: pass margin or "
                "origin to place them clear of it (see bounds)")  # fmt: skip
    if not drawing.inside(box, sh.sheet):
        tx.warn("NX_VIEW_OVERLAP", f"The {what} run off the sheet (bounds {box}): use a "
                "smaller margin")  # fmt: skip
    if any(drawing.overlap(t, other) for t in texts or [box] for other in taken):
        tx.warn("NX_VIEW_OVERLAP", f"The {what} lie over annotations already on the sheet: "
                "pass margin (gap from the view border) or origin to move them clear")  # fmt: skip
    return out


def ordinate_snap(info, snap, field):
    """Resolve the auto snap of an ordinate target and refuse impossible ones."""
    if snap == "auto":
        snap = "center" if info["kind"] == "arc" else "start"
    if snap == "center" and info["kind"] != "arc":
        fail("NX_WRONG_GEOMETRY", "center needs a circular edge", field=field)
    if info.get("closed") and snap in ("start", "end"):
        # NX measures a full circle at its center whatever end point is requested.
        fail("NX_WRONG_GEOMETRY", "A full circle has no end points: use center", field=field)
    return snap


def _row(tx, obj, value=None, expected=None):
    row = tx.ref(obj, "dimension")
    if value is not None:
        row["value"] = float(value)
    if expected is not None:
        row["expected"] = float(expected)
    return row


def _check_value(actual, expected, check="value"):
    tol = 1e-6 * max(1.0, abs(expected))
    readback.expect(
        abs(actual - expected) <= tol,
        check,
        f"NX measured {actual}, the model gives {expected}",
        computed=actual,
        expected=expected,
    )


def _ends(edge, field):
    points = [geom.xyz(v) for v in edge.GetVertices()]
    if len(points) < 2:
        fail("NX_WRONG_GEOMETRY", "This edge has no two end points (a full circle?)", field=field,
             hint="Give a second edge (a circle measures to its centre), or use kind diameter")  # fmt: skip
    return points


def _measured_point(tx, edge, field):
    """``(point, snap)`` a linear dimension measures on one of two edges: a full circle's
    centre (NX measures a circle there), else the edge's start vertex."""
    found = facts.edge(tx, edge)
    if found["kind"] == "arc" and found.get("closed"):
        return found["center"], "Center"
    return _ends(edge, field)[0], "Start"


# ---- create -----------------------------------------------------------------------------


def _summary(a, say):
    kind = a.get("kind")
    if kind == "hole_callout":
        return f"Hole callout on {say('hole') if a.get('hole') else say('first')}"
    if kind == "ordinate":
        return f"{len(a.get('objects') or [])} ordinate dimensions in {say('view')}"
    if kind == "angle":
        return f"Angle dimension {say('first')} to {say('second')}"
    if kind in ("radius", "diameter"):
        return f"{kind.title()} dimension on {say('first')}"
    if kind in SETS:
        return f"{kind.title()} set of {len(a.get('objects') or [])} objects in {say('view')}"
    if kind in ("chamfer", "thickness", "arc_length"):
        return f"{kind.replace('_', ' ').capitalize()} dimension on {say('first')}"
    what = f"{kind.title()} dimension" if kind in ("horizontal", "vertical") else "Dimension"
    if a.get("second"):
        return f"{what} {say('first')} to {say('second')}"
    return f"{what if kind in ('horizontal', 'vertical') else 'Length dimension'} on {say('first')}"


@tool(
    effect="model",
    label="Create drawing dimension",
    summary=_summary,
    keywords=(
        "linear dimension",
        "radius diameter hole callout",
        "angular",
        "ordinate",
        "chamfer dimension",
        "thickness",
        "arc length",
        "baseline chain",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="kind",
    required_by={  # first-only kinds left out: first is the field every edge kind takes
        "angle": ("first", "second"),
        "chamfer": ("first", "second"),
        "thickness": ("first", "second"),
        "ordinate": ("datum", "objects"),
        "baseline": ("objects",),
        "chain": ("objects",),
    },
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={"view": "v:7", "kind": "horizontal", "first": "e:12"},
)
def nx_create_drawing_dimension(
    tx: Tx,
    *,
    view: ViewArg,
    kind: Literal[
        "linear",
        "horizontal",
        "vertical",
        "radius",
        "diameter",
        "angle",
        "ordinate",
        "chamfer",
        "thickness",
        "arc_length",
        "baseline",
        "chain",
        "hole_callout",
    ],
    first: EdgeRef | None = None,
    second: EdgeRef | None = None,
    orientation: Literal["aligned", "horizontal", "vertical"] = "aligned",
    origin: SheetPoint | None = None,
    datum: EdgeRef | None = None,
    datum_point: Literal["auto", "start", "end", "center"] = "auto",
    objects: Annotated[list[EdgeRef], Items(1, MAX_ORDINATES)] | None = None,
    object_point: Literal["auto", "start", "end", "center"] = "auto",
    direction: Literal["horizontal", "vertical"] = "horizontal",
    margin: Annotated[SheetLength, Doc("Sets/ordinate: gap from the view border")] | None = None,
    hole: FeatureRef | None = None,
) -> DimensionData:
    """Create an associative dimension on edges in a drawing view, checked against the model.
    Linear: an edge's length or first to second (a full circle counts at its centre),
    oriented (or kind horizontal/vertical). Baseline/chain: datum + objects, 3+ points.
    Ordinate: objects from datum. Sets and ordinates: margin is the gap from the view
    border. Chamfer: edge, then reference. Text: outside the view."""
    if kind in ("horizontal", "vertical"):
        if orientation not in ("aligned", kind):
            fail("NX_INVALID_ARGUMENT", f"kind {kind} measures {kind}ly: drop orientation "
                 f"{orientation!r} or use kind linear", field="/orientation")  # fmt: skip
        orientation = "horizontal" if kind == "horizontal" else "vertical"
        kind = "linear"
    if margin is not None and kind not in (*SETS, "ordinate"):
        fail("NX_INVALID_ARGUMENT", "margin applies to baseline, chain and ordinate",
             field="/margin", hint="Place other dimensions with origin")  # fmt: skip
    if margin is not None and not float(margin) > 0:
        fail("NX_INVALID_ARGUMENT", "margin is a gap from the view border: give a positive "
             "length", field="/margin")  # fmt: skip
    if not re.match(REF_PATTERN, view):
        with tx.checks():
            shown, sheet = view_of(tx, view)
            target = tx.units.get("sheet_length")
            if margin is not None and target and drawing.Sheet(tx, sheet).units != target:
                fail("NX_INVALID_ARGUMENT", "Give the view as a ref (v:N) with margin: the "
                     "named view is on a sheet in other units", field="/view")  # fmt: skip
        view = shown
    if kind == "hole_callout":
        if (hole is None) == (first is None):
            both = hole is not None
            fail("NX_INVALID_ARGUMENT", "A hole callout takes exactly one of hole (the Hole "
                 "feature) or first (a circular edge of the hole)" + (", not both" if both else ""),
                 field="/first" if both else "/hole")  # fmt: skip
        if second is not None:
            fail("NX_INVALID_ARGUMENT", "A hole callout takes one hole", field="/second")
        from nx_mcp.domains.drawing import hole_callouts

        return hole_callouts.create_callout(tx, view, hole, first, origin)
    if kind in SETS:
        if margin is not None and origin is not None:
            fail("NX_INVALID_ARGUMENT", "Give origin or margin, not both", field="/margin")
        objects = args.required(objects, "objects")
        points = len(objects) + (datum is not None)
        if points < 3:
            fail(
                "NX_INVALID_ARGUMENT",
                f"A {kind} set needs 3 or more points (datum + 2 objects, or 3 objects) and "
                f"makes one dimension per step; got {points}",
                field="/objects",
                hint="For one distance use kind linear with first, second and orientation",
            )
        args.distinct("/objects", objects, "edges")
        if datum is not None and datum in objects:
            fail("NX_INVALID_ARGUMENT", "datum cannot also be in objects", field="/objects")
        from nx_mcp.domains.drawing import dimension_kinds as more

        return more.dimension_set(
            tx, view, kind, objects, object_point, direction, origin, (datum, datum_point), margin
        )
    if kind == "ordinate":
        args.need(datum=datum)
        objects = args.required(objects, "objects")
        args.distinct("/objects", objects, "edges")
        if datum in objects:
            fail("NX_INVALID_ARGUMENT", "datum cannot also be in objects", field="/objects")
        return _ordinate(tx, view, datum, datum_point, objects, object_point, direction, margin)
    args.need(first=first)
    if kind == "angle":
        args.need(second=second)
        if first == second:
            fail("NX_INVALID_ARGUMENT", "first and second must differ", field="/second")
        return _angle(tx, view, first, second, origin)
    if kind in ("radius", "diameter", "arc_length"):
        if second is not None:
            fail("NX_INVALID_ARGUMENT", f"A {kind} takes one edge", field="/second")
        if kind == "arc_length":
            from nx_mcp.domains.drawing import dimension_kinds as more

            return more.arc_length(tx, view, first, origin)
        return _radial(tx, view, first, kind, origin)
    if kind in ("chamfer", "thickness"):
        args.need(second=second)
        if first == second:
            fail("NX_INVALID_ARGUMENT", "first and second must differ", field="/second")
        from nx_mcp.domains.drawing import dimension_kinds as more

        return getattr(more, kind)(tx, view, first, second, origin)
    return linear(tx, view, first, second, orientation, origin)


def linear(tx, view_ref, first, second, orientation, origin, existing=None,
           fields=("/first", "/second")):  # fmt: skip
    """Create (or, with ``existing``, rebind) a linear dimension and verify its value."""
    with tx.checks():
        view, sheet = view_ref if isinstance(view_ref, tuple) else drawing.view(tx, view_ref)
        a = geometry(tx, first, ("edge",), fields[0])
        b = geometry(tx, second, ("edge",), fields[1]) if second else a
        if second:
            (pa, snap_a), (pb, snap_b) = (_measured_point(tx, e, f) for e, f in
                                          zip((a, b), fields, strict=True))  # fmt: skip
        else:
            pa, pb = _ends(a, fields[0])[:2]
            snap_a, snap_b = "Start", "End"
        expected = linear_value(pa, pb, drawing.axes(view), orientation)
        if expected <= 1e-9:
            what = "measured points of the two edges" if second else "ends of the edge"
            fail(
                "NX_INVALID_ARGUMENT",
                f"The {what} have no {orientation} distance in this view",
                field=fields[1] if second else fields[0],
                hint="One edge measures its own length: pick an edge not seen end-on (or across "
                "the orientation), or give two edges",
            )
    with drawing.on_sheet(tx, sheet) as sh:
        if origin is None and existing is None:
            a2, b2 = drawing.to_sheet(tx, view, pa), drawing.to_sheet(tx, view, pb)
            origin = text_spot(
                tx, sh, view, *linear_direction(a2, b2, orientation, drawing.borders(tx, view))
            )
        snap = tx.nx.InferSnapType.SnapType
        empty = tx.nx.Point3d(0.0, 0.0, 0.0)
        factory = tx.part.Dimensions.CreateLinearDimensionBuilder
        with builders.build(tx, factory, existing) as builder:
            builder.FirstAssociativity.SetValue(
                getattr(snap, snap_a), a, view, tx.nx.Point3d(*pa), None, None, empty
            )
            builder.SecondAssociativity.SetValue(
                getattr(snap, snap_b), b, view, tx.nx.Point3d(*pb), None, None, empty
            )
            methods = builder.Measurement.MeasurementMethod
            builder.Measurement.Method = getattr(methods, METHODS[orientation])
            if origin is not None:
                builder.Origin.OriginPoint = sh.point(origin)
            dimension = builder.commit()
        actual = result.measured(dimension.ComputedSize)
        _check_value(actual, expected)
        with contextlib.suppress(NXError):  # the check only adds a note
            overall.warn_short(tx, view, [a, b] if second else [a], pa, pb, orientation, actual)
        return placed(tx, sh, dimension, {
            "dimension": _row(tx, dimension, actual, expected),
            "origin": sh.to_sheet(dimension.AnnotationOrigin),
            "units": units.part_units(tx),
        })  # fmt: skip


def _radial(tx, view_ref, first, kind, origin):
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        edge = geometry(tx, first, ("edge",), "/first")
        found = edge_facts(tx, edge, "/first", want="arc")
        true_shape(view, found, "/first")
    center, radius = found["center"], found["radius"]
    on_arc = [c + radius * x for c, x in zip(center, found["x_axis"], strict=True)]
    with drawing.on_sheet(tx, sheet) as sh:
        if origin is None:  # out of the view on the side of the arc (up-right at its centre)
            c2 = drawing.to_sheet(tx, view, center)
            origin = text_spot(tx, sh, view, c2, drawing.outward(drawing.borders(tx, view), c2))
        with builders.build(tx, tx.part.Dimensions.CreateRadialDimensionBuilder, None) as b:
            methods = b.Measurement.MeasurementMethod
            b.Measurement.Method = methods.Radial if kind == "radius" else methods.Diametral
            b.FirstAssociativity.SetValue(
                tx.nx.InferSnapType.SnapType.NotSet,
                edge,
                view,
                tx.nx.Point3d(*on_arc),
                None,
                None,
                tx.nx.Point3d(0.0, 0.0, 0.0),
            )
            b.Origin.OriginPoint = sh.point(origin)
            dimension = b.commit()
        expected = radius if kind == "radius" else 2.0 * radius
        actual = result.measured(dimension.ComputedSize)
        _check_value(actual, expected)
        return placed(tx, sh, dimension, {
            "dimension": _row(tx, dimension, actual, expected),
            "origin": sh.to_sheet(dimension.AnnotationOrigin),
            "units": units.part_units(tx),
        })  # fmt: skip


def _angle(tx, view_ref, first, second, origin):
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        edges = [
            geometry(tx, first, ("edge",), "/first"),
            geometry(tx, second, ("edge",), "/second"),
        ]
        found = [
            edge_facts(tx, e, f, want="line")
            for e, f in zip(edges, ("/first", "/second"), strict=True)
        ]
        flat = [
            [drawing.to_sheet(tx, view, f["start"]), drawing.to_sheet(tx, view, f["end"])]
            for f in found
        ]
        acute = acute_angle_deg(*[[q[0] - p[0], q[1] - p[1]] for p, q in flat])
        if acute is None:
            fail("NX_UNSUPPORTED_GEOMETRY", "An edge is seen end-on in this view",
                 field="/second")  # fmt: skip
        if acute < ANGLE_TOL_DEG:
            fail("NX_INVALID_ARGUMENT", "The edges are parallel in this view", field="/second")
    mids = [geom.scale(geom.add(f["start"], f["end"]), 0.5) for f in found]
    with drawing.on_sheet(tx, sheet) as sh:
        if origin is None:
            m = [[(p[i] + q[i]) / 2.0 for i in range(2)] for p, q in flat]
            mid = [(m[0][i] + m[1][i]) / 2.0 for i in range(2)]
            origin = text_spot(tx, sh, view, mid, drawing.outward(drawing.borders(tx, view), mid))
        snap = tx.nx.InferSnapType.SnapType.NotSet
        empty = tx.nx.Point3d(0.0, 0.0, 0.0)
        with builders.build(tx, tx.part.Dimensions.CreateAngularDimensionBuilder, None) as b:
            b.FirstAssociativity.SetValue(
                snap, edges[0], view, tx.nx.Point3d(*mids[0]), None, None, empty
            )
            b.SecondAssociativity.SetValue(
                snap, edges[1], view, tx.nx.Point3d(*mids[1]), None, None, empty
            )
            b.Origin.OriginPoint = sh.point(origin)
            dimension = b.commit()
        actual = result.measured(dimension.ComputedSize, "angle")
        readback.expect(
            angle_matches(actual, acute),
            "value",
            "NX's angle differs from the angle between the edges in the view",
            computed=actual,
            view_angle=acute,
        )
        return placed(tx, sh, dimension, {
            "dimension": _row(tx, dimension, actual, acute),
            "origin": sh.to_sheet(dimension.AnnotationOrigin),
            "units": "deg",
        })  # fmt: skip


def _target(tx, ref, snap, field):
    edge = geometry(tx, ref, ("edge",), field)
    found = facts.edge(tx, edge)
    snap = ordinate_snap(found, snap, field)
    if snap == "center":
        return edge, found["center"], ("ARC_CENTER", 0)
    return edge, found[snap], ("END_POINT", 1 if snap == "start" else 2)


def _drf(U, edge, view, assoc):
    data = U.Drf.Object()
    data.ObjectTag = edge.Tag
    data.ObjectViewTag = view.Tag
    data.ObjectAssocType = getattr(U.Drf.AssocType, assoc[0])
    data.ObjectAssocModifier = assoc[1]
    return data


def _ordinate(tx, view_ref, datum, datum_point, objects, object_point, direction, margin):
    horizontal = direction == "horizontal"
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        origin_edge, origin_xyz, origin_assoc = _target(tx, datum, datum_point, "/datum")
        targets = [
            (ref, *_target(tx, ref, object_point, f"/objects/{i}")) for i, ref in enumerate(objects)
        ]
        U = uf_types(tx)
        # The margin line: above the view for horizontal, left of it for vertical ordinates.
        bounds = drawing.borders(tx, view)
        if margin is None:
            margin = drawing.from_mm(units.sheet_units(tx, sheet), MARGIN_GAP_MM)[0]
        at_line = bounds[3] + float(margin) if horizontal else bounds[0] - float(margin)
        on_sheet_line(sheet, at_line, 1 if horizontal else 0, "/margin")
    axis = drawing.axes(view)[0 if horizontal else 1]
    with drawing.on_sheet(tx, sheet) as sh:
        taken = drawing.sheet_annotation_bounds(tx, sheet)
        # UF_DRF placement points are in part units; mapped points are in sheet units.
        line = sh.to_part([at_line, at_line])[0]
        at = sh.to_part(drawing.to_sheet(tx, view, origin_xyz))
        drf = tx.uf.Drf
        origin_tag = drf.CreateOrdorigin(_drf(U, origin_edge, view, origin_assoc), 5, 2, 2, "")
        margin_tag = drf.CreateOrdinateMargin(
            1 if horizontal else 2,
            origin_tag,
            0,
            [at[0], line] if horizontal else [line, at[1]],
            [1.0, 0.0] if horizontal else [0.0, 1.0],
            0.0,
        )
        lookup = tx.nx.TaggedObjectManager.GetTaggedObject
        rows, made = [], []
        for i, (_ref, edge, xyz, assoc) in enumerate(targets):
            tx.checkpoint(done=i, total=len(targets))
            p = sh.to_part(drawing.to_sheet(tx, view, xyz))
            tag = drf.CreateOrddimension(
                margin_tag,
                1 if horizontal else 2,
                _drf(U, edge, view, assoc),
                0.0,
                0.0,
                U.Drf.Text(),
                2,
                [p[0], line, 0.0] if horizontal else [line, p[1], 0.0],
            )
            dimension = lookup(tag)
            expected = abs(geom.dot(geom.sub(xyz, origin_xyz), axis))
            actual = result.measured(dimension.ComputedSize)
            _check_value(actual, expected, f"value[{i}]")
            readback.expect(
                int(dimension.GetOrdinateOrigin().Tag) == int(origin_tag),
                f"origin[{i}]",
                "The ordinate dimension does not use the new ordinate origin",
            )
            rows.append(_row(tx, dimension, actual, expected))
            made.append(dimension)
        out: DimensionData = {
            "ordinate_origin": tx.ref(lookup(origin_tag), "dimension"),
            "dimensions": rows,
            "margin": float(margin),
            "margin_line": at_line,
            "units": units.part_units(tx),
        }
        return set_bounds(tx, sh, out, made, taken, "ordinate dimensions")


# ---- inspect and edit -------------------------------------------------------------------


class DimensionFormat(TypedDict, total=False):
    decimals: int
    trailing_zeros: bool
    units: Literal["mm", "in", "um"]
    decimal_separator: Literal["period", "comma"]


class DimensionTolerance(TypedDict, total=False):
    type: Literal["none", "bilateral", "symmetric", "limits", "basic", "reference", "fit"]
    upper: float
    lower: float
    decimals: int
    hole: Annotated[
        str,
        Text(4),
        Doc("Fit hole class D E F G H JS K M N P, grade 5-11 (K M N up to 8, P up to 7), e.g. H7"),
    ]
    shaft: Annotated[str, Text(4), Doc("Fit shaft class d e f g h js k m n p, grade 5-11, e.g. g6")]
    display: Literal["symbol", "deviations", "symbol_and_deviations", "symbol_and_limits"]


forbid_extra(DimensionFormat, DimensionTolerance)


class DimensionInfo(TypedDict, total=False):
    dimension: result.Row
    type: str
    value: float
    valid: bool
    associations: int
    objects: list[result.Row]
    text: Annotated[str, Doc("The nominal value as NX shows it")]
    display: Annotated[str, Doc("The whole callout: symbol, value, tolerance, appended text")]
    appended: Annotated[dict, Doc("Appended text lines: before, after, above, below")]
    view: result.Row
    format: dict
    tolerance: dict
    origin: list[float]
    sheet: result.Row
    units: str
    callout: dict
    bounds: list[float]


def _enum(value, enum, table=None):
    name = result.enum_name(value, enum)
    if table:
        return next((k for k, v in table.items() if v == name), name)
    return name


def bound_objects(tx, d):
    """Rows of the objects (edges) a dimension measures, in association order, each once
    (``associations`` counts the native associativities)."""
    rows, seen = [], set()
    for obj in associated(d):
        if int(obj.Tag) in seen:  # both ends of one edge (a dimension of its length)
            continue
        seen.add(int(obj.Tag))
        try:
            rows.append(tx.ref(obj, "edge" if kind_of_geometry(obj) == "edge" else None))
        except NXError:  # an object without a reference kind (a sketch point)
            continue
    return rows


def dimension_info(tx, d):
    A = tx.nx.Annotations
    mm = units.part_units(tx) == "mm"
    prefs = d.GetDimensionPreferences()
    fmt = prefs.GetUnitsFormatPreferences()
    try:
        out = {
            "dimension": tx.ref(d, "dimension"),
            "type": type(d).__name__,
            "value": float(d.ComputedSize),
            "valid": not bool(d.IsRetained),
            "associations": int(d.NumberOfAssociativities),
            "format": {
                "decimals": int(d.NominalDecimalPlaces),
                "trailing_zeros": bool(fmt.DisplayTrailingZeros),
                "units": _enum(fmt.PrimaryDimensionUnit, A.DimensionUnit, UNITS),
                "decimal_separator": _enum(
                    fmt.DecimalPointCharacter, A.DecimalPointCharacter
                ).lower(),
            },
            "tolerance": {
                "type": _enum(d.ToleranceType, A.ToleranceType, TOLERANCES),
                "upper": float(d.UpperMetricToleranceValue if mm else d.UpperToleranceValue),
                "lower": float(d.LowerMetricToleranceValue if mm else d.LowerToleranceValue),
                "decimals": int(d.ToleranceDecimalPlaces),
            },
            "units": units.part_units(tx),
        }
        if out["format"]["units"] == "in":  # NX 2406 shows inches with a period whatever
            out["format"]["decimal_separator"] = "period"  # the stored character
        if out["tolerance"]["type"] in NO_DEVIATIONS:  # NX keeps, but does not show, them
            del out["tolerance"]["upper"], out["tolerance"]["lower"]
        if out["tolerance"]["type"] == "fit":
            out["tolerance"].update(fit_info(tx, d, out["value"]))
    finally:
        fmt.Dispose()
        prefs.Dispose()
    with contextlib.suppress(Exception):  # the text NX shows (its decimal separator)
        out["text"] = " ".join(str(t) for t in d.GetDimensionText()[0])
    with contextlib.suppress(Exception):  # a dimension without appended text support
        out["appended"] = dimension_text.read_appended(d)
    shown = dimension_text.display_text(out)
    if shown is not None:
        out["display"] = shown
    with contextlib.suppress(Exception):  # a dimension NX keeps on no view (PMI)
        view = _rebind_view(d)
        if view is not None:
            out["view"] = tx.ref(view, "drawing_view")
    bound = bound_objects(tx, d)
    if bound:
        out["objects"] = bound
    if type(d).__name__ == "HoleDimension":
        from nx_mcp.domains.drawing import hole_callouts

        out["callout"] = hole_callouts.callout_info(tx, d)
    sheet = drawing.sheet_of_annotation(tx, d)
    if sheet is not None:
        sh = drawing.Sheet(tx, sheet)
        out["sheet"] = tx.ref(sheet, "drawing_sheet")
        out["origin"] = sh.to_sheet(d.AnnotationOrigin)
        bounds = drawing.annotation_box(tx, d, sh)[0]
        if bounds is not None:
            out["bounds"] = bounds
    return out


@tool(
    effect="read",
    label="Get dimension",
    summary="Dimension {dimension}",
    keywords=("dimension value", "dimension format", "tolerance", "decimal places", "retained"),
    requires=("work_part",),
    cost="light",
    example={"dimension": "dm:9"},
)
def nx_get_dimension(
    tx: Tx,
    *,
    dimension: DimensionRef,
) -> DimensionInfo:
    """Read a drawing dimension: measured value, format, tolerance and sheet position.
    valid is false for a retained dimension that lost its geometry."""
    return dimension_info(
        tx, select.resolve(tx, dimension, "dimension", field="/dimension", work_part=True)
    )


def _set_format(tx, d, fmt):
    A = tx.nx.Annotations
    prefs = d.GetDimensionPreferences()
    native = prefs.GetUnitsFormatPreferences()
    try:
        if "trailing_zeros" in fmt:
            native.DisplayTrailingZeros = bool(fmt["trailing_zeros"])
        if "units" in fmt:
            native.PrimaryDimensionUnit = getattr(A.DimensionUnit, UNITS[fmt["units"]])
        if "decimal_separator" in fmt:
            name = "Period" if fmt["decimal_separator"] == "period" else "Comma"
            native.DecimalPointCharacter = getattr(A.DecimalPointCharacter, name)
        prefs.SetUnitsFormatPreferences(native)
        d.SetDimensionPreferences(prefs)
    finally:
        native.Dispose()
        prefs.Dispose()
    if "decimals" in fmt:
        d.NominalDecimalPlaces = fmt["decimals"]
        d.MetricNominalDecimalPlaces = fmt["decimals"]


def _set_tolerance(tx, d, tol):
    A = tx.nx.Annotations
    if "type" in tol:
        d.ToleranceType = getattr(A.ToleranceType, TOLERANCES[tol["type"]])
    if "decimals" in tol:
        d.ToleranceDecimalPlaces = tol["decimals"]
        d.MetricToleranceDecimalPlaces = tol["decimals"]
    to_mm = 1.0 if units.part_units(tx) == "mm" else 25.4
    for key, native, metric in (
        ("upper", "UpperToleranceValue", "UpperMetricToleranceValue"),
        ("lower", "LowerToleranceValue", "LowerMetricToleranceValue"),
    ):
        if key in tol:
            value_mm = float(tol[key]) * to_mm
            setattr(d, native, value_mm / 25.4)
            setattr(d, metric, value_mm)


def fit_info(tx, d, size):
    """Classes, display and NX's computed deviations and limits of a fit tolerance."""
    A = tx.nx.Annotations
    shown = _enum(d.LimitFitEnhancedDisplayType, A.FitAnsiHoleType).lower()
    out = {"display": _enum(d.LimitFitDisplayStyle, A.FitDisplayStyle, FIT_DISPLAY)}
    if shown in ("hole", "fit"):
        out["hole"] = f"{d.LimitFitDeviation}{int(d.LimitFitGrade)}"
    if shown in ("shaft", "fit"):
        out["shaft"] = f"{d.LimitFitShaftDeviation}{int(d.LimitFitShaftGrade)}"
    return {**out, **_fit_deviations(tx, d, size, out)}


def _fit_deviations(tx, d, size, out):
    """Deviations NX computed for the classes and the resulting limits, in part units
    (NX keeps fit deviations in millimetres only: the metric fields)."""
    per_mm = 1.0 if units.part_units(tx) == "mm" else 1.0 / 25.4
    got = {}
    if "hole" in out:
        got["upper"] = float(d.UpperMetricToleranceValue) * per_mm
        got["lower"] = float(d.LowerMetricToleranceValue) * per_mm
    if "shaft" in out:
        got["shaft_upper"] = float(d.ShaftUpperToleranceValue) * per_mm
        got["shaft_lower"] = float(d.ShaftLowerToleranceValue) * per_mm
    limits = {}
    if "upper" in got:
        limits["hole"] = [size + got["lower"], size + got["upper"]]
    if "shaft_upper" in got:
        limits["shaft"] = [size + got["shaft_lower"], size + got["shaft_upper"]]
    return {**got, "limits": limits}


def _format_units(tx, d):
    """The primary units a dimension shows: mm, in, um or m."""
    prefs = d.GetDimensionPreferences()
    fmt = prefs.GetUnitsFormatPreferences()
    try:
        return _enum(fmt.PrimaryDimensionUnit, tx.nx.Annotations.DimensionUnit, UNITS)
    finally:
        fmt.Dispose()
        prefs.Dispose()


def _normalized(tol):
    """A copy of a tolerance request with fit classes in ISO case (hole H7, shaft g6)."""
    tol = dict(tol)
    for role, case in (("hole", str.upper), ("shaft", str.lower)):
        if role in tol:
            tol[role] = case(tol[role].strip())
    return tol


def _held(tx, d) -> tuple[float, float]:
    """The (upper, lower) deviations NX holds for ``d``, in part units."""
    if units.part_units(tx) == "mm":
        return float(d.UpperMetricToleranceValue), float(d.LowerMetricToleranceValue)
    return float(d.UpperToleranceValue), float(d.LowerToleranceValue)


def _deviations(tx, d, tol):
    """Check upper/lower against the tolerance type (the request's or the dimension's)
    and mirror a symmetric one (+/- upper). A switch to symmetric needs upper; bilateral
    and limits need upper > lower (the held value fills the one not given)."""
    held = _enum(d.ToleranceType, tx.nx.Annotations.ToleranceType, TOLERANCES)
    kind = tol.get("type") or held
    given = [k for k in ("upper", "lower") if k in tol]
    if kind in NO_DEVIATIONS and given:
        fail("NX_INVALID_ARGUMENT", f"A {kind} tolerance shows no deviations: drop "
             f"{given[0]}, or use type bilateral, symmetric or limits",
             field=f"/tolerance/{given[0]}")  # fmt: skip
    if kind == "symmetric":
        if len(given) == 2 and not math.isclose(tol["lower"], -tol["upper"], abs_tol=1e-12):
            fail("NX_INVALID_ARGUMENT", "A symmetric tolerance is +/- upper: give upper only",
                 field="/tolerance/lower")  # fmt: skip
        if not given and held != "symmetric":  # a switch: never reuse a stale upper
            fail("NX_INVALID_ARGUMENT", "A symmetric tolerance needs upper (the +/- value)",
                 field="/tolerance/upper", hint='e.g. {"type": "symmetric", "upper": 0.05}')  # fmt: skip
        value = abs(float(tol[given[0]] if given else _held(tx, d)[0]))  # still symmetric
        return {**tol, "upper": value, "lower": -value}
    if kind in ("bilateral", "limits"):
        upper, lower = _held(tx, d)
        upper = float(tol.get("upper", upper))
        lower = float(tol.get("lower", lower))
        if not upper > lower:
            field = "/tolerance/" + ("lower" if "lower" in tol else "upper")
            fail("NX_INVALID_ARGUMENT", f"A {kind} tolerance needs upper > lower (signed "
                 f"deviations); got upper {upper:g}, lower {lower:g}", field=field,
                 hint='e.g. {"type": "bilateral", "upper": 0.1, "lower": -0.05}')  # fmt: skip
    return tol


def _fit_request(tol):
    """Validate a fit tolerance before NX: classes, and no explicit deviations."""
    fit = tol.get("type") == "fit"
    for key in ("hole", "shaft", "display"):
        if key in tol and not fit:
            fail("NX_INVALID_ARGUMENT", f"{key} needs type fit", field=f"/tolerance/{key}")
    if not fit:
        return
    if "hole" not in tol and "shaft" not in tol:
        fail("NX_INVALID_ARGUMENT", "A fit needs hole and/or shaft", field="/tolerance/hole")
    for key in ("upper", "lower"):
        if key in tol:
            fail("NX_INVALID_ARGUMENT", "A fit takes its deviations from ISO 286, not "
                 f"{key}", field=f"/tolerance/{key}")  # fmt: skip
    for role in ("hole", "shaft"):
        if role in tol:
            parsed = fits.parse(tol[role], role)
            if isinstance(parsed, str):
                fail("NX_INVALID_ARGUMENT", parsed, field=f"/tolerance/{role}",
                     hint=fits.CLASS_HINT[role])  # fmt: skip


def _fit_expected(tx, d, tol, shown_units=None):
    """ISO 286 deviations (part units) of the requested classes at the dimension's size."""
    if "Angular" in type(d).__name__:
        fail("NX_UNSUPPORTED_OPTION", "Fits apply to sizes, not angles", field="/tolerance/type")
    if shown_units is None:
        shown_units = _format_units(tx, d)
    if shown_units != "mm":
        fail(
            "NX_UNSUPPORTED_OPTION",
            f"NX applies ISO fits to millimetre dimensions only (this one shows {shown_units}): "
            "add format units mm",
            field="/tolerance/type",
        )
    to_mm = 1.0 if units.part_units(tx) == "mm" else 25.4
    size = float(d.ComputedSize)
    out = {}
    for role, keys in (("hole", ("upper", "lower")), ("shaft", ("shaft_upper", "shaft_lower"))):
        if role in tol:
            found = fits.deviations(role, tol[role], size * to_mm)
            if isinstance(found, str):
                fail("NX_UNSUPPORTED_OPTION", found, field=f"/tolerance/{role}")
            out.update({k: v / 1000.0 / to_mm for k, v in zip(keys, found, strict=True)})
    return out


def _edit_style(tx, d, **values):
    """Commit dimension-style values through the annotation edit-settings builder."""
    factory = tx.part.SettingsManager.CreateAnnotationEditSettingsBuilder
    with builders.build(tx, factory, [d]) as b:
        style = b.AnnotationStyle.DimensionStyle
        for name, value in values.items():
            setattr(style, name, value)
        b.commit()


def _set_fit(tx, d, tol):
    """NX computes fit deviations only when the classes go through the style builder and
    change: a class the dimension already holds (H7/g6 by default) is first committed
    with another grade. In Fit mode NX computes only the shaft's deviations, so the
    hole class is committed alone first."""
    A = tx.nx.Annotations
    shown = A.FitAnsiHoleType
    display = getattr(A.FitDisplayStyle, FIT_DISPLAY[tol.get("display", "symbol")])
    common = {"ToleranceType": A.ToleranceType.LimitsAndFits, "LimitFitDisplayStyle": display}
    for role, mode, letter_key, grade_key in (
        ("hole", shown.Hole, "LimitFitDeviation", "LimitFitGrade"),
        ("shaft", shown.Fit if "hole" in tol else shown.Shaft, "LimitFitShaftDeviation",
         "LimitFitShaftGrade"),
    ):  # fmt: skip
        if role not in tol:
            continue
        letter, grade = fits.parse(tol[role], role)
        if str(getattr(d, letter_key)) == letter and int(getattr(d, grade_key)) == grade:
            other = grade + 1 if grade < fits.GRADES[1] else grade - 1
            _edit_style(tx, d, **common, LimitFitAnsiHoleType=mode, **{letter_key: letter,
                        grade_key: other})  # fmt: skip
        _edit_style(tx, d, **common, LimitFitAnsiHoleType=mode, **{letter_key: letter,
                    grade_key: grade})  # fmt: skip


def _origin(d):
    """The dimension's text origin [x, y, z] (model units), or None when NX has none."""
    point = getattr(d, "AnnotationOrigin", None)
    return geom.xyz(point) if point is not None else None


def _keep_origin(tx, d, origin) -> None:
    """Put the dimension text back where it was: NX's style builder can move it (a fit)."""
    now = _origin(d)
    if now is None or geom.dist(now, origin) > 1e-9:
        d.AnnotationOrigin = tx.nx.Point3d(*origin)


def _rebind_view(d):
    for i in range(1, int(d.NumberOfAssociativities) + 1):
        view = getattr(d.GetAssociativity(i), "ObjectView", None)
        if view is not None:
            return view
    return None


@tool(
    effect="model",
    label="Edit dimension",
    summary="Edit dimension {dimension}",
    keywords=("dimension format", "tolerance", "decimals", "reassociate", "rebind dimension"),
    requires=("work_part", "no_active_sketch"),
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={"dimension": "dm:9", "format": {"decimals": 2}},
)
def nx_edit_dimension(
    tx: Tx,
    *,
    dimension: DimensionRef,
    format: DimensionFormat | None = None,  # noqa: A002 - surface name
    tolerance: DimensionTolerance | None = None,
    edges: Annotated[list[EdgeRef], Items(1, 2)] | None = None,
    appended: Annotated[dimension_text.Appended, Doc(dimension_text.APPENDED_DOC)] | None = None,
) -> DimensionInfo:
    """Change a dimension's format, tolerance or appended text (REF, 2X). Or rebind a
    linear one (1-2 edges). upper/lower: signed numbers in part units, upper > lower;
    symmetric: upper (+/-). Fit: ISO 286 hole and/or shaft class on a size shown in mm (to
    500 mm, not an angle); NX computes the deviations (no upper/lower)."""
    if not format and not tolerance and not edges and not appended:
        fail("NX_INVALID_ARGUMENT", "Give format, tolerance, appended or edges")
    fit = bool(tolerance and tolerance.get("type") == "fit")
    if tolerance:
        tolerance = _normalized(tolerance)
        _fit_request(tolerance)
    if format and format.get("decimal_separator") == "comma" and format.get("units") == "in":
        fail("NX_UNSUPPORTED_OPTION", "NX shows inch dimensions with a period",
             field="/format/decimal_separator")  # fmt: skip
    for field, part in (("format", format), ("tolerance", tolerance)):
        if part and "decimals" in part and not 0 <= part["decimals"] <= 8:
            fail("NX_INVALID_ARGUMENT", "decimals must be 0-8", field=f"/{field}/decimals")
    first: str | None
    second: str | None
    first, second = (list(edges) + [None])[:2] if edges else (None, None)
    if second is not None and second == first:
        fail("NX_INVALID_ARGUMENT", "edges must differ", field="/edges/1")
    with tx.checks():
        d = select.resolve(tx, dimension, "dimension", field="/dimension", work_part=True)
        if tolerance and not fit:
            tolerance = _deviations(tx, d, tolerance)
        asked = format or {}
        if (
            asked.get("decimal_separator") == "comma"
            and "units" not in asked
            and _format_units(tx, d) == "in"
        ):
            fail("NX_UNSUPPORTED_OPTION", "NX shows inch dimensions with a period: add "
                 "format units mm", field="/format/decimal_separator")  # fmt: skip
        before = (float(d.ComputedSize), int(d.NumberOfAssociativities), bool(d.IsRetained))
        origin = _origin(d)
        if first is not None:
            view = _rebind_view(d)
            if view is None or not type(d).__name__.endswith(
                ("HorizontalDimension", "VerticalDimension", "ParallelDimension")
            ):
                fail("NX_UNSUPPORTED_OPTION", "Only linear dimensions can be rebound",
                     field="/edges")  # fmt: skip
            sheet = drawing.view_sheet(tx, view, "/dimension")
            orientation = {"HorizontalDimension": "horizontal", "VerticalDimension": "vertical"}
            mode = orientation.get(type(d).__name__, "aligned")
        elif fit:
            expected = _fit_expected(tx, d, tolerance, (format or {}).get("units"))
    if first is not None:
        linear(tx, (view, sheet), first, second, mode, None, existing=d,
               fields=("/edges/0", "/edges/1"))  # fmt: skip
        wanted_tags = {int(select.resolve(tx, e, "edge").Tag) for e in edges or []}
        bound = {int(o.Tag) for o in associated(d)}
        readback.expect(
            not bound or bound == wanted_tags,
            "edges",
            "NX kept the dimension on other objects",
            objects=bound_objects(tx, d),
        )
        if fit:
            expected = _fit_expected(tx, d, tolerance, (format or {}).get("units"))
    if format:
        _set_format(tx, d, format)
    if tolerance:
        _set_tolerance(tx, d, tolerance)
        if fit:
            _set_fit(tx, d, tolerance)
    if appended:
        dimension_text.write_appended(d, appended)
    if first is None and origin is not None:
        _keep_origin(tx, d, origin)
    d.RedisplayObject()
    out = dimension_info(tx, d)
    for side, text in (appended or {}).items():
        shown = " ".join(out.get("appended", {}).get(side, [])).strip()
        readback.expect(shown == str(text).strip(), "appended",
                        f"NX shows {shown!r} {side} the value, not {text!r}")  # fmt: skip
    if first is None and origin is not None:
        readback.expect_close(_origin(d), origin, "origin", tol=1e-6)
        after = (out["value"], out["associations"], not out["valid"])
        readback.expect(
            math.isclose(after[0], before[0], rel_tol=1e-9, abs_tol=1e-12)
            and after[1:] == before[1:],
            "value",
            "The format change altered the measured value or its association",
            before=list(before),
            after=list(after),
        )
    wanted = dict(format or {})
    got = {k: out["format"][k] for k in wanted}
    readback.expect(got == wanted, "format", "NX kept another format", **got)
    if tolerance:
        if "type" in tolerance:
            readback.expect(
                out["tolerance"]["type"] == tolerance["type"], "tolerance", **out["tolerance"]
            )
        if "decimals" in tolerance:
            readback.expect(
                out["tolerance"]["decimals"] == tolerance["decimals"],
                "tolerance",
                **out["tolerance"],
            )
        for key in ("upper", "lower"):
            if key in tolerance:
                readback.expect_close(out["tolerance"][key], tolerance[key], "tolerance", 1e-9)
        if fit:
            _check_fit(tx, out["tolerance"], tolerance, expected)
    return out


def _check_fit(tx, got, wanted, expected):
    """NX kept the classes and display, and computed the ISO 286 deviations."""
    for key in ("hole", "shaft"):
        readback.expect(got.get(key) == wanted.get(key), "fit", f"NX shows {key} "
                        f"{got.get(key)}, not {wanted.get(key)}", **got)  # fmt: skip
    display = wanted.get("display", "symbol")
    readback.expect(got.get("display") == display, "fit_display", display=got.get("display"))
    tol = FIT_TOL_MM / (1.0 if units.part_units(tx) == "mm" else 25.4)
    for key, value in expected.items():
        readback.expect(
            abs(got.get(key, math.nan) - value) <= tol,
            "fit_limits",
            f"NX's {key} deviation {got.get(key)} differs from ISO 286 ({value})",
            computed=got.get(key),
            expected=value,
        )
