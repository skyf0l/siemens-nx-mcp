"""Chamfer, thickness and arc-length dimensions, and baseline/chain dimension sets.

``nx_create_drawing_dimension`` dispatches here; every value is checked against the
model as seen in the view.

NX 2406 findings encoded here:

- ``ChamferDimensionBuilder`` measures the chamfer leg along the reference edge and
  commits a ``ChamferDimension``; the form (``2 x 45°``) is read back with UF
  ``Drf.AskChamferDimensionData``.
- ``ThicknessDimensionBuilder`` commits a ``ConcentricCircleDimension``; only parallel
  straight edges or concentric arcs have one well-defined thickness.
- ``CurveLengthDimensionBuilder`` commits an ``ArcLengthDimension`` (r * sweep).
- Baseline and chain sets use ``DimensionSets.Create<Horizontal|Vertical><Baseline|
  Chain>Dimension`` with one associativity per object (UF ``CreateHorizontalChain
  Dimension`` takes a single object in the Python binding); the set is an annotation
  whose members are ordinary linear dimensions. With two points NX returns a set whose
  one member is never made alive (UF status condemned, not in ``part.Dimensions``) and
  never drawn: sets need 3+ points, refused before NX. A new set is drawn only at the
  next display update, so each member is redisplayed.
- ``ChamferDimensionBuilder`` refuses chamfers other than 45 degrees ("The second
  object associativity type is invalid"): they are refused before NX.
"""

from __future__ import annotations

import math

from nx_mcp.core.errors import fail
from nx_mcp.domains.drawing import dimensions as dims
from nx_mcp.domains.drawing.common import (
    edge_facts,
    geometry,
    on_sheet_point,
    true_shape,
    uf_types,
)
from nx_mcp.kit import builders, drawing, geom, readback, result, units

PARALLEL_TOL_DEG = 1e-3  # Thickness edges must be parallel in the view.
CONCENTRIC_TOL = 1e-6  # Thickness arcs: center offset relative to the radius.
SET_GAP_MM = 12.0  # Default distance of a set's first dimension line from the view.
CHAMFER_FORM = "SizeAngle"  # "2 x 45°"
CHAMFER_ANGLE = 45.0


# ---- pure helpers -----------------------------------------------------------------------


def in_view(vector, axes):
    """A model vector projected on a view's horizontal and vertical directions."""
    return [geom.dot(vector, axes[0]), geom.dot(vector, axes[1])]


def chamfer_values(chamfer, reference):
    """``(size, angle_deg)`` of a chamfer line against its reference line (2D vectors):
    the chamfer's leg along the reference and the acute angle between the two."""
    angle = dims.acute_angle_deg(chamfer, reference)
    if angle is None:
        return None, None
    unit = math.hypot(*reference)
    size = abs(chamfer[0] * reference[0] + chamfer[1] * reference[1]) / unit
    return size, angle


def line_gap(p, q, a):
    """Distance from 2D point ``a`` to the line through ``p`` and ``q``."""
    dx, dy = q[0] - p[0], q[1] - p[1]
    return abs(dx * (a[1] - p[1]) - dy * (a[0] - p[0])) / math.hypot(dx, dy)


def arc_sweep(found):
    """Sweep angle (radians) of an arc from its facts; evaluation runs uniformly in angle,
    so the start-to-1/3 angle (at most 120 degrees) is a third of the sweep."""
    if found.get("closed"):
        return 2.0 * math.pi
    c = found["center"]
    a, b = geom.sub(found["start"], c), geom.sub(found["samples"][0], c)
    cos = geom.dot(a, b) / (geom.norm(a) * geom.norm(b))
    return 3.0 * math.acos(max(-1.0, min(1.0, cos)))


def set_values(points, axis, kind):
    """Values of a baseline (from the first point) or chain (point to point) set."""
    along = [geom.dot(p, axis) for p in points]
    if kind == "baseline":
        return [abs(v - along[0]) for v in along[1:]]
    return [abs(b - a) for a, b in zip(along, along[1:], strict=False)]


# ---- chamfer, thickness, arc length -----------------------------------------------------


def _assoc(tx, builder, edges, view, points):
    snap = tx.nx.InferSnapType.SnapType.NotSet
    empty = tx.nx.Point3d(0.0, 0.0, 0.0)
    for assoc, edge, p in zip(
        (builder.FirstAssociativity, getattr(builder, "SecondAssociativity", None)),
        edges,
        points,
        strict=False,
    ):
        assoc.SetValue(snap, edge, view, tx.nx.Point3d(*p), None, None, empty)


def _default_origin(tx, view, sh, points):
    """Outside the view border, on the side of the dimensioned geometry's centre."""
    flat = [drawing.to_sheet(tx, view, p) for p in points]
    mid = [sum(p[i] for p in flat) / len(flat) for i in (0, 1)]
    return dims.text_spot(tx, sh, view, mid, drawing.outward(drawing.borders(tx, view), mid))


def _done(tx, sh, dimension, actual, expected, **extra):
    row = dims._row(tx, dimension, actual, expected)
    row.update(extra)
    return dims.placed(tx, sh, dimension, {
        "dimension": row,
        "origin": sh.to_sheet(dimension.AnnotationOrigin),
        "units": units.part_units(tx),
    })  # fmt: skip


def _lines(tx, view_ref, first, second):
    view, sheet = drawing.view(tx, view_ref)
    edges = [geometry(tx, first, ("edge",), "/first"), geometry(tx, second, ("edge",), "/second")]
    found = [
        edge_facts(tx, e, f, want="line") for e, f in zip(edges, ("/first", "/second"), strict=True)
    ]
    return view, sheet, edges, found


def chamfer(tx, view_ref, first, second, origin):
    """Chamfer edge ``first`` against reference edge ``second``: size x angle."""
    with tx.checks():
        view, sheet, edges, found = _lines(tx, view_ref, first, second)
        axes = drawing.axes(view)
        vectors = [in_view(geom.sub(f["end"], f["start"]), axes) for f in found]
        if min(math.hypot(*v) for v in vectors) <= 1e-9:
            fail("NX_UNSUPPORTED_GEOMETRY", "An edge is seen end-on in this view", field="/first")
        size, angle = chamfer_values(*vectors)
        if abs(angle - CHAMFER_ANGLE) > dims.ANGLE_TOL_DEG:
            fail(
                "NX_UNSUPPORTED_GEOMETRY",
                f"NX dimensions 45 degree chamfers only; this edge is at {angle:.4g} degrees "
                "to the reference edge in this view",
                field="/first",
            )
    mids = [geom.scale(geom.add(f["start"], f["end"]), 0.5) for f in found]
    with drawing.on_sheet(tx, sheet) as sh:
        if origin is None:
            origin = _default_origin(tx, view, sh, [found[0]["start"], found[0]["end"]])
        factory = tx.part.Dimensions.CreateChamferDimensionBuilder
        with builders.build(tx, factory, None) as b:
            _assoc(tx, b, edges, view, mids)
            b.Style.DimensionStyle.ChamferForm = getattr(
                tx.nx.Annotations.ChamferForm, CHAMFER_FORM
            )
            b.Origin.OriginPoint = sh.point(origin)
            dimension = b.commit()
        actual = result.measured(dimension.ComputedSize)
        dims._check_value(actual, size)
        form = uf_types(tx).Drf.ChamferDimensionForm.CHAMFER_FORM_SIZEANGLE
        data = tx.uf.Drf.AskChamferDimensionData(dimension.Tag)
        readback.expect(
            str(data.Form) == str(form),
            "form",
            "NX did not keep the size x angle chamfer form",
            form=str(data.Form),
        )
        return _done(tx, sh, dimension, actual, size, angle=angle)


def thickness(tx, view_ref, first, second, origin):
    """Thickness between two parallel straight edges or two concentric arcs."""
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        edges = [
            geometry(tx, first, ("edge",), "/first"),
            geometry(tx, second, ("edge",), "/second"),
        ]
        found = [edge_facts(tx, e, f) for e, f in zip(edges, ("/first", "/second"), strict=True)]
        kinds = {f["kind"] for f in found}
        axes = drawing.axes(view)
        if kinds == {"line"}:
            flat = [[in_view(f["start"], axes), in_view(f["end"], axes)] for f in found]
            vectors = [[q[0] - p[0], q[1] - p[1]] for p, q in flat]
            angle = dims.acute_angle_deg(*vectors)
            if angle is None:
                fail("NX_UNSUPPORTED_GEOMETRY", "An edge is seen end-on in this view",
                     field="/second")  # fmt: skip
            if angle > PARALLEL_TOL_DEG:
                fail("NX_UNSUPPORTED_GEOMETRY", "The edges are not parallel in this view",
                     field="/second")  # fmt: skip
            expected = line_gap(*flat[0], flat[1][0])
            picks = [geom.scale(geom.add(f["start"], f["end"]), 0.5) for f in found]
        elif kinds == {"arc"}:
            for f, field in zip(found, ("/first", "/second"), strict=True):
                true_shape(view, f, field)
            offset = in_view(geom.sub(found[1]["center"], found[0]["center"]), axes)
            if math.hypot(*offset) > CONCENTRIC_TOL * max(1.0, found[0]["radius"]):
                fail("NX_UNSUPPORTED_GEOMETRY", "The arcs are not concentric", field="/second")
            expected = abs(found[0]["radius"] - found[1]["radius"])
            picks = [f["start"] for f in found]
        else:
            fail(
                "NX_UNSUPPORTED_GEOMETRY",
                "A thickness needs two straight edges or two arcs",
                field="/second",
            )
        if expected <= 1e-9:
            fail("NX_INVALID_ARGUMENT", "The edges coincide in this view", field="/second")
    with drawing.on_sheet(tx, sheet) as sh:
        if origin is None:
            origin = _default_origin(tx, view, sh, picks)
        factory = tx.part.Dimensions.CreateThicknessDimensionBuilder
        with builders.build(tx, factory, None) as b:
            _assoc(tx, b, edges, view, picks)
            b.Origin.OriginPoint = sh.point(origin)
            dimension = b.commit()
        actual = result.measured(dimension.ComputedSize)
        dims._check_value(actual, expected)
        return _done(tx, sh, dimension, actual, expected)


def arc_length(tx, view_ref, first, origin):
    """Length along a circular edge seen true shape: radius * sweep."""
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        edge = geometry(tx, first, ("edge",), "/first")
        found = edge_facts(tx, edge, "/first", want="arc")
        true_shape(view, found, "/first")
    sweep = arc_sweep(found)
    expected = found["radius"] * sweep
    pick = found["samples"][0]
    with drawing.on_sheet(tx, sheet) as sh:
        if origin is None:
            origin = _default_origin(tx, view, sh, [pick])
        factory = tx.part.Dimensions.CreateCurveLengthDimensionBuilder
        with builders.build(tx, factory, None) as b:
            _assoc(tx, b, [edge], view, [pick])
            b.Origin.OriginPoint = sh.point(origin)
            dimension = b.commit()
        actual = result.measured(dimension.ComputedSize)
        dims._check_value(actual, expected)
        return _done(tx, sh, dimension, actual, expected, sweep=math.degrees(sweep))


# ---- baseline and chain sets ------------------------------------------------------------


def _set_assoc(tx, view, edge, xyz, assoc):
    """One member point of a set: an arc center, or the end of an edge nearest ``xyz``."""
    A = tx.nx.Annotations
    a = tx.part.Annotations.NewAssociativity()
    a.FirstObject = edge
    a.ObjectView = view
    a.PointOption = getattr(
        A.AssociativityPointOption, "ArcCenter" if assoc[0] == "ARC_CENTER" else "Control"
    )
    a.PickPoint = tx.nx.Point3d(*xyz)
    return a


def dimension_set(tx, view_ref, kind, objects, object_point, direction, origin,
                  datum=(None, None), margin=None):  # fmt: skip
    """Several horizontal or vertical dimensions from the first point (baseline) or each
    from the previous one (chain), placed as one NX dimension set. ``datum`` ``(ref,
    snap)``, when given, is the first point. The first dimension line lies ``margin`` (sheet
    units) above (horizontal) or right of (vertical) the view border; without margin or
    origin, SET_GAP_MM out and past annotations already there."""
    horizontal = direction == "horizontal"
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        targets = [
            dims._target(tx, ref, object_point, f"/objects/{i}") for i, ref in enumerate(objects)
        ]
        fields = [f"/objects/{i}" for i in range(len(objects))]
        if datum[0] is not None:
            targets.insert(0, dims._target(tx, datum[0], datum[1], "/datum"))
            fields.insert(0, "/datum")
        axis = drawing.axes(view)[0 if horizontal else 1]
        expected = set_values([t[1] for t in targets], axis, kind)
        for i, value in enumerate(expected, start=1):
            if value <= 1e-9:
                other = fields[0 if kind == "baseline" else i - 1].lstrip("/")
                fail(
                    "NX_INVALID_ARGUMENT",
                    f"{fields[i].lstrip('/')} is at the same {direction} position as {other}: "
                    "it would give a zero dimension; drop it or use the other direction",
                    field=fields[i],
                )
        taken = drawing.sheet_annotation_bounds(tx, sheet)
        if origin is None:
            origin = _set_origin(tx, view, sheet, targets, horizontal, margin, taken)
        else:
            on_sheet_point(sheet, origin, "/origin")
    with drawing.on_sheet(tx, sheet) as sh:
        sets = tx.part.Annotations.DimensionSets
        create = getattr(
            sets, f"Create{'Horizontal' if horizontal else 'Vertical'}{kind.title()}Dimension"
        )
        data = tx.part.Annotations.NewDimensionData()
        members = [_set_assoc(tx, view, *t) for t in targets]
        try:
            data.SetAssociativity(1, members)
            native = create(data, sh.point(origin))
        finally:
            for item in (*members, data):
                item.Dispose()
        found = list(native.GetAllSubDimensions())
        readback.expect(
            len(found) == len(expected),
            "count",
            f"NX made {len(found)} dimensions, not {len(expected)}",
            count=len(found),
        )
        alive = {d.Tag for d in tx.part.Dimensions}
        rows = []
        for i, (member, value) in enumerate(zip(found, expected, strict=True)):
            readback.expect(
                member.Tag in alive,
                f"shown[{i}]",
                "NX made a set member that is not a dimension of the part (not drawn)",
            )
            actual = result.measured(member.ComputedSize)
            dims._check_value(actual, value, f"value[{i}]")
            member.RedisplayObject()  # NX draws a new set only at the next display update
            rows.append(dims._row(tx, member, actual, value))
        out = {
            "dimension_set": tx.ref(native, "annotation"),
            "dimensions": rows,
            "origin": [float(v) for v in origin],
            "units": units.part_units(tx),
        }
        if margin is not None:
            out["margin"] = float(margin)
        return dims.set_bounds(tx, sh, out, found, taken, f"{kind} dimensions")


def _set_origin(tx, view, sheet, targets, horizontal, margin, taken):
    """The set's first dimension line: ``margin`` out of the view border (above for
    horizontal, right for vertical), else SET_GAP_MM out, stepped outward past annotations
    already there; refused off the sheet."""
    bounds = drawing.borders(tx, view)
    k = 1 if horizontal else 0  # the axis the line is placed along
    gap = drawing.from_mm(units.sheet_units(tx, sheet), SET_GAP_MM)[0]
    at = (bounds[3] if horizontal else bounds[2]) + (float(margin) if margin is not None else gap)
    flat = [drawing.to_sheet(tx, view, t[1]) for t in targets]
    if margin is None:  # clear of earlier sets and dimensions along the line
        lo, hi = min(p[1 - k] for p in flat), max(p[1 - k] for p in flat)
        for _ in range(20):
            band = [lo, at - gap / 4, hi, at + gap / 4]
            if not horizontal:
                band = [at - gap / 4, lo, at + gap / 4, hi]
            if not any(drawing.overlap(band, box) for box in taken):
                break
            at += gap
    dims.on_sheet_line(sheet, at, k, "/margin")
    return [flat[0][0], at] if horizontal else [at, flat[0][1]]
