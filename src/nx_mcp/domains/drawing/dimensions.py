"""Associative drawing dimensions: linear, radius, diameter, angle and ordinate; format.

NX 2406 findings encoded here:

- ``OrdinateDimensionBuilder`` commits only the origin plus placeholder dimensions
  without a margin on 2406: ordinates use the UF drafting calls (ordinate origin ->
  margin -> ordinate dimension), whose placement points are in part units.
- A radius or diameter on a circle seen edge-on (or as an ellipse) is refused before
  NX; a full circle has no end vertices, so ordinate start/end snaps are refused.
- NX may dimension the acute angle between two edges, its supplement or the reflex
  equivalents; the angle is verified against all four.
- Default text positions are defined in millimetres and converted to sheet units.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Items, RefTo, SheetLength, SheetPoint
from nx_mcp.domains.drawing.common import edge_facts, geometry, true_shape, uf_types
from nx_mcp.kit import args, builders, drawing, facts, geom, readback, result, select, units

MAX_ORDINATES = 50
ANGLE_TOL_DEG = 1e-3
TEXT_GAP_MM = 10.0  # Default linear/angular text offset from the measured geometry.
RADIAL_GAP_MM = 12.0  # Default radial text distance outside the arc.
MARGIN_GAP_MM = 15.0  # Between the view border and a default ordinate margin.
METHODS = {"aligned": "PointToPoint", "horizontal": "Horizontal", "vertical": "Vertical"}
UNITS = {"mm": "Millimeters", "in": "Inches", "um": "Micrometers", "m": "Meters"}
TOLERANCES = {
    "none": "NotSet",
    "bilateral": "BilateralTwoLines",
    "symmetric": "BilateralOneLine",
    "limits": "LimitTwoLines",
    "basic": "Basic",
    "reference": "Reference",
}

EdgeRef = RefTo("edge")


class DimensionRow(result.Row, total=False):
    value: float
    expected: float


class DimensionData(TypedDict, total=False):
    dimension: DimensionRow
    dimensions: list[DimensionRow]
    ordinate_origin: result.Row
    origin: list[float]
    margin: float
    units: str


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


def linear_origin(a, b, orientation, gap):
    """Default text position (sheet units): ``gap`` beside the measured span."""
    mid = [(a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0]
    if orientation == "horizontal":
        return [mid[0], max(a[1], b[1]) + gap]
    if orientation == "vertical":
        return [max(a[0], b[0]) + gap, mid[1]]
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy) or 1.0
    normal = [-dy / length, dx / length]
    if normal[1] < 0 or (normal[1] == 0 and normal[0] < 0):
        normal = [-normal[0], -normal[1]]  # above (or right of) the span
    return [mid[0] + gap * normal[0], mid[1] + gap * normal[1]]


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
        fail("NX_WRONG_GEOMETRY", "This edge has no two end points (a full circle?)", field=field)
    return points


# ---- create -----------------------------------------------------------------------------


def _summary(a, say):
    kind = a.get("kind")
    if kind == "ordinate":
        return f"{len(a.get('objects') or [])} ordinate dimensions in {say('view')}"
    if kind == "angle":
        return f"Angle dimension {say('first')} to {say('second')}"
    if kind in ("radius", "diameter"):
        return f"{kind.title()} dimension on {say('first')}"
    if a.get("second"):
        return f"Dimension {say('first')} to {say('second')}"
    return f"Length dimension on {say('first')}"


@tool(
    effect="model",
    label="Create drawing dimension",
    summary=_summary,
    keywords=("dimension", "radius", "diameter", "angular", "ordinate", "linear dimension"),
    requires=("work_part", "no_active_sketch"),
    variants="kind",
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={"view": "v:7", "kind": "linear", "first": "e:12", "orientation": "horizontal"},
)
def nx_create_drawing_dimension(
    tx: Tx,
    *,
    view: RefTo("drawing_view"),  # type: ignore[valid-type]
    kind: Literal["linear", "radius", "diameter", "angle", "ordinate"],
    first: EdgeRef | None = None,  # type: ignore[valid-type]
    second: EdgeRef | None = None,  # type: ignore[valid-type]
    orientation: Literal["aligned", "horizontal", "vertical"] = "aligned",
    origin: SheetPoint | None = None,
    datum: EdgeRef | None = None,  # type: ignore[valid-type]
    datum_point: Literal["start", "end", "center"] = "start",
    objects: Annotated[list[EdgeRef], Items(1, MAX_ORDINATES)] | None = None,  # type: ignore[valid-type]
    object_point: Literal["auto", "start", "end", "center"] = "auto",
    direction: Literal["horizontal", "vertical"] = "horizontal",
    margin: SheetLength | None = None,
) -> DimensionData:
    """Create an associative dimension on edges in a drawing view, checked against the model.
    Linear: one edge or two edges' start points; ordinate: objects from a datum point."""
    if kind == "ordinate":
        args.need(datum=datum, objects=objects)
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
    if kind in ("radius", "diameter"):
        if second is not None:
            fail("NX_INVALID_ARGUMENT", f"A {kind} takes one edge", field="/second")
        return _radial(tx, view, first, kind, origin)
    return linear(tx, view, first, second, orientation, origin)


def linear(tx, view_ref, first, second, orientation, origin, existing=None, field="/view"):
    """Create (or, with ``existing``, rebind) a linear dimension and verify its value."""
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref, field) if isinstance(view_ref, str) else view_ref
        a = geometry(tx, first, ("edge",), "/first")
        b = geometry(tx, second, ("edge",), "/second") if second else a
        ends_a = _ends(a, "/first")
        ends_b = _ends(b, "/second") if second else ends_a
        pa, pb = ends_a[0], (ends_b[0] if second else ends_a[1])
        expected = linear_value(pa, pb, drawing.axes(view), orientation)
        if expected <= 1e-9:
            fail("NX_INVALID_ARGUMENT", "The two points coincide in this view", field="/second")
    with drawing.on_sheet(tx, sheet) as sh:
        if origin is None and existing is None:
            gap = drawing.from_mm(sh.units, TEXT_GAP_MM)[0]
            origin = linear_origin(
                drawing.to_sheet(tx, view, pa), drawing.to_sheet(tx, view, pb), orientation, gap
            )
        snap = tx.nx.InferSnapType.SnapType
        empty = tx.nx.Point3d(0.0, 0.0, 0.0)
        factory = tx.part.Dimensions.CreateLinearDimensionBuilder
        with builders.build(tx, factory, existing) as builder:
            builder.FirstAssociativity.SetValue(
                snap.Start, a, view, tx.nx.Point3d(*pa), None, None, empty
            )
            builder.SecondAssociativity.SetValue(
                snap.Start if second else snap.End, b, view, tx.nx.Point3d(*pb), None, None, empty
            )
            methods = builder.Measurement.MeasurementMethod
            builder.Measurement.Method = getattr(methods, METHODS[orientation])
            if origin is not None:
                builder.Origin.OriginPoint = sh.point(origin)
            dimension = builder.commit()
        actual = result.measured(dimension.ComputedSize)
        _check_value(actual, expected)
        return {
            "dimension": _row(tx, dimension, actual, expected),
            "origin": sh.to_sheet(dimension.AnnotationOrigin),
            "units": units.part_units(tx),
        }


def _radial(tx, view_ref, first, kind, origin):
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        edge = geometry(tx, first, ("edge",), "/first")
        found = edge_facts(tx, edge, "/first", want="arc")
        true_shape(view, found, "/first")
    center, radius = found["center"], found["radius"]
    on_arc = [c + radius * x for c, x in zip(center, found["x_axis"], strict=True)]
    with drawing.on_sheet(tx, sheet) as sh:
        if origin is None:
            c2 = drawing.to_sheet(tx, view, center)
            reach = math.dist(c2, drawing.to_sheet(tx, view, on_arc))
            reach = (reach + drawing.from_mm(sh.units, RADIAL_GAP_MM)[0]) * math.sqrt(0.5)
            origin = [c2[0] + reach, c2[1] + reach]  # up-right at 45 degrees
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
        return {
            "dimension": _row(tx, dimension, actual, expected),
            "origin": sh.to_sheet(dimension.AnnotationOrigin),
            "units": units.part_units(tx),
        }


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
            gap = drawing.from_mm(sh.units, TEXT_GAP_MM)[0]
            m = [[(p[i] + q[i]) / 2.0 for i in range(2)] for p, q in flat]
            origin = [(m[0][i] + m[1][i]) / 2.0 + gap for i in range(2)]
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
        return {
            "dimension": _row(tx, dimension, actual, acute),
            "origin": sh.to_sheet(dimension.AnnotationOrigin),
            "units": "deg",
        }


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
    axis = drawing.axes(view)[0 if horizontal else 1]
    with drawing.on_sheet(tx, sheet) as sh:
        if margin is None:  # above the view for horizontal, left of it for vertical ordinates
            bounds = drawing.borders(tx, view)
            gap = drawing.from_mm(sh.units, MARGIN_GAP_MM)[0]
            margin = bounds[3] + gap if horizontal else bounds[0] - gap
        # UF_DRF placement points are in part units; mapped points are in sheet units.
        line = sh.to_part([margin, margin])[0]
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
        rows = []
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
        return {
            "ordinate_origin": tx.ref(lookup(origin_tag), "dimension"),
            "dimensions": rows,
            "margin": float(margin),
            "units": units.part_units(tx),
        }


# ---- inspect and edit -------------------------------------------------------------------


class DimensionFormat(TypedDict, total=False):
    decimals: int
    trailing_zeros: bool
    units: Literal["mm", "in", "um"]
    decimal_separator: Literal["period", "comma"]


class DimensionTolerance(TypedDict, total=False):
    type: Literal["none", "bilateral", "symmetric", "limits", "basic", "reference"]
    upper: float
    lower: float
    decimals: int


for _td in (DimensionFormat, DimensionTolerance):
    _td.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]


class DimensionInfo(TypedDict, total=False):
    dimension: result.Row
    type: str
    value: float
    valid: bool
    associations: int
    format: dict
    tolerance: dict
    origin: list[float]
    sheet: result.Row
    units: str


def _enum(value, enum, table=None):
    name = result.enum_name(value, enum)
    if table:
        return next((k for k, v in table.items() if v == name), name)
    return name


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
    finally:
        fmt.Dispose()
        prefs.Dispose()
    sheet = drawing.sheet_of_annotation(tx, d)
    if sheet is not None:
        out["sheet"] = tx.ref(sheet, "drawing_sheet")
        out["origin"] = drawing.Sheet(tx, sheet).to_sheet(d.AnnotationOrigin)
    return out


@tool(
    effect="read",
    label="Get dimension",
    summary="Dimension {dimension}",
    keywords=("dimension value", "dimension format", "tolerance", "decimal places", "retained"),
    requires=("work_part",),
    cost="instant",
    example={"dimension": "dm:9"},
)
def nx_get_dimension(
    tx: Tx,
    *,
    dimension: RefTo("dimension"),  # type: ignore[valid-type]
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
    dimension: RefTo("dimension"),  # type: ignore[valid-type]
    format: DimensionFormat | None = None,  # noqa: A002 - surface name
    tolerance: DimensionTolerance | None = None,
    edges: Annotated[list[EdgeRef], Items(1, 2)] | None = None,  # type: ignore[valid-type]
) -> DimensionInfo:
    """Change a dimension's format or tolerance, or rebind it to 1-2 edges."""
    if not format and not tolerance and not edges:
        fail("NX_INVALID_ARGUMENT", "Give format, tolerance or edges")
    for field, part in (("format", format), ("tolerance", tolerance)):
        if part and "decimals" in part and not 0 <= part["decimals"] <= 8:
            fail("NX_INVALID_ARGUMENT", "decimals must be 0-8", field=f"/{field}/decimals")
    first, second = (list(edges) + [None])[:2] if edges else (None, None)
    if second is not None and second == first:
        fail("NX_INVALID_ARGUMENT", "edges must differ", field="/edges/1")
    with tx.checks():
        d = select.resolve(tx, dimension, "dimension", field="/dimension", work_part=True)
        before = (float(d.ComputedSize), int(d.NumberOfAssociativities), bool(d.IsRetained))
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
    if first is not None:
        linear(tx, (view, sheet), first, second, mode, None, existing=d)
    if format:
        _set_format(tx, d, format)
    if tolerance:
        _set_tolerance(tx, d, tolerance)
    d.RedisplayObject()
    out = dimension_info(tx, d)
    if first is None:
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
    return out
