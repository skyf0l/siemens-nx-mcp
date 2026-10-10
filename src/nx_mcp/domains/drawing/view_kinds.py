"""Auxiliary, broken, half/stepped/revolved section and break-out drawing views.

NX 2406 findings encoded here:

- Auxiliary: ``ProjectedViewBuilder`` with a *Defined* hinge line whose vector is in
  sheet coordinates (a model-space direction is read as a sheet vector and gives the
  wrong view). The hinge edge is mapped into the parent view; the view looks normal to
  it, so the hinge edge is seen true length (read back).
- Broken: ``BrokenViewBuilder`` with sheet-space anchor points leaves the break lines
  "undefined" and NX opens a modal error dialog on update; model anchors work. A
  centerline inside the removed band opens a modal "hidden centerline" notice: refused
  before NX.
- Half, stepped and revolved sections: UF ``CreateHalfSxview`` (the wrapper wants
  ``numSxsegs`` structs), ``CreateSteppedSxview``, ``CreateRevolvedSxview``. Revolved
  legs on one line crash NX (memory access violation): refused before NX.
- Break-out: UF ``CreateBreakout`` with a boundary drawn in the expanded member view
  (``View.ConvertToView`` refuses model curves) and a smart base point (scalars).
"""

from __future__ import annotations

import math
from typing import Literal, TypedDict

from nx_mcp.core.errors import fail
from nx_mcp.core.types import RefTo
from nx_mcp.domains.drawing.common import edge_facts, geometry, uf_types
from nx_mcp.kit import builders, drawing, geom, readback, units

DEFAULT_GAP_MM = 10.0
AXES = {"x": 0, "y": 1}
SXSEG_ARRAYS = {"half": "CreateHalfSxview", "stepped": "CreateSteppedSxview"}


class _EdgePointRequired(TypedDict):
    edge: RefTo("edge")  # type: ignore[valid-type]


class EdgePoint(_EdgePointRequired, total=False):
    at: Literal["start", "end", "center"]


EdgePoint.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]


# ---- section line points and cut planes ---------------------------------------------------


def edge_point(tx, edge_ref, at, field, at_field):
    """``(edge, at, model point)`` for an edge point: an arc center or a line end."""
    edge = geometry(tx, edge_ref, ("edge",), field)
    found = edge_facts(tx, edge, field, "arc" if at == "center" else None)
    if at != "center" and found.get("closed"):
        fail("NX_WRONG_GEOMETRY", "A full circle has no end points: use its center",
             field=at_field)  # fmt: skip
    point = found["center"] if at == "center" else found[at]
    return edge, at, [float(v) for v in point]


def drf_object(tx, edge, at, view):
    """A UF ``Drf.Object`` for an arc center or a line end seen in ``view``."""
    U = uf_types(tx)
    data = U.Drf.Object()
    data.ObjectTag = edge.Tag
    data.ObjectViewTag = view.Tag
    if at == "center":
        data.ObjectAssocType, data.ObjectAssocModifier = U.Drf.AssocType.ARC_CENTER, 0
    else:
        data.ObjectAssocType = U.Drf.AssocType.END_POINT
        data.ObjectAssocModifier = 1 if at == "start" else 2
    return data


def _segment(tx, kind, point, view):
    U = uf_types(tx)
    seg = U.Draw.SxlineSxsegs()
    seg.SxsegType = getattr(U.Draw.SxsegType, kind)
    seg.SxsegObject = drf_object(tx, point[0], point[1], view)
    seg.SxsegAngle = 0.0
    return seg


def revolved_legs(tx, parent, rotation, cuts):
    """Refuse revolved legs that lie on one line in the parent view (NX 2406 crashes)."""
    r = drawing.to_sheet(tx, parent, rotation[2])
    legs = [geom.sub(drawing.to_sheet(tx, parent, c[2]), r) for c in cuts]
    lengths = [math.hypot(*leg) for leg in legs]
    size = max(lengths)
    if min(lengths) <= 1e-6 * max(1.0, size):
        fail("NX_INVALID_ARGUMENT", "A cut point lies on the rotation point", field="/cuts")
    if abs(geom.cross2(*legs)) <= 1e-6 * lengths[0] * lengths[1]:
        fail("NX_INVALID_ARGUMENT",
             "The two cut points and the rotation point lie on one line: use a section view",
             field="/cuts")  # fmt: skip


def create_sxview(tx, kind, sheet, parent, scale, step, arrow, points, position):
    """Create a half, stepped or revolved section view through UF; returns its tag.

    ``points``: half (cut, bend), stepped (cut, ...), revolved (rotation, leg 1, leg 2),
    each ``(edge, at, model point)``.
    """
    U = uf_types(tx)
    draw = tx.uf.Draw
    where = [float(position[0]), float(position[1])]
    if kind == "half":
        half = U.Draw.HalfSxsegs()
        half.CutObject = drf_object(tx, points[0][0], points[0][1], parent)
        half.BendObject = drf_object(tx, points[1][0], points[1][1], parent)
        # The Python wrapper reads one struct per segment count (2 = cut and bend).
        return draw.CreateHalfSxview(sheet.Tag, scale, step, arrow, parent.Tag, 2,
                                     [half, half], where)  # fmt: skip
    if kind == "stepped":
        segs = [_segment(tx, "SXSEG_CUT", p, parent) for p in points]
        return draw.CreateSteppedSxview(sheet.Tag, scale, step, arrow, parent.Tag, len(segs),
                                        segs, where)  # fmt: skip
    rotation = drf_object(tx, points[0][0], points[0][1], parent)
    segs = [_segment(tx, "SXSEG_CUT", p, parent) for p in points[1:]]
    return draw.CreateRevolvedSxview(sheet.Tag, scale, step, arrow, parent.Tag, rotation,
                                     len(segs), 1, segs, where)  # fmt: skip


SXLINE_QUERIES = ("AskSimpleSxline", "AskHalfSxline", "AskSteppedSxline", "AskRevolvedSxline")


def _segment_tags(tx, line):
    """The segment tags of a section line (the last list its UF query returns)."""
    draw = tx.uf.Draw
    for name in SXLINE_QUERIES:
        try:
            answer = getattr(draw, name)(line)
        except Exception:  # noqa: S112 - wrong query for this section-line type
            continue
        lists = [v for v in answer if isinstance(v, (list, tuple))]
        if lists:
            return list(lists[-1])
    return []


def cut_planes(tx, sh, parent, view, arrow):
    """The cut planes of section view ``view``: ``[{normal, offset}]`` in part coordinates.

    Each cut segment of the section line (a sheet line in ``parent``) with the parent's
    viewing direction spans a plane; ``normal`` points along the arrow side and
    ``offset`` = normal . p for any point p of the plane.
    """
    U = uf_types(tx)
    draw = tx.uf.Draw
    x_axis, y_axis, _ = drawing.axes(parent)
    out = []
    for tag in _segment_tags(tx, draw.AskSxlineOfSxview(view.Tag)):
        info, curve, _ = draw.AskSxlineSxseg(tag)
        if info.SxsegType != U.Draw.SxsegType.SXSEG_CUT or not curve:
            continue
        line = tx.nx.TaggedObjectManager.GetTaggedObject(curve)
        a, b = sh.to_sheet(line.StartPoint), sh.to_sheet(line.EndPoint)
        d = geom.sub(b, a)
        length = math.hypot(*d)
        if length <= 0:
            continue
        n = [-d[1] / length, d[0] / length]
        if n[0] * arrow[0] + n[1] * arrow[1] < 0:
            n = [-n[0], -n[1]]
        normal = [n[0] * x + n[1] * y for x, y in zip(x_axis, y_axis, strict=True)]
        origin = [float(v) for v in tx.uf.View.MapDrawingToModel(parent.Tag, a)][:3]
        out.append({"normal": normal, "offset": geom.dot(normal, origin)})
    readback.expect(bool(out), "cut_planes", "NX reports no cut segment")
    return out


# ---- auxiliary ---------------------------------------------------------------------------


def hinge_of(tx, parent, hinge_ref, scale):
    """``(edge facts, sheet unit vector)`` of a straight hinge edge seen in ``parent``."""
    edge = geometry(tx, hinge_ref, ("edge",), "/hinge")
    found = edge_facts(tx, edge, "/hinge", "line")
    a, b = (drawing.to_sheet(tx, parent, found[k]) for k in ("start", "end"))
    d = geom.sub(b, a)
    seen = math.hypot(*d)
    if seen <= 1e-6 * max(1.0, math.dist(found["start"], found["end"]) * scale):
        fail("NX_WRONG_GEOMETRY", "The hinge edge is seen end-on in the parent view",
             field="/hinge")  # fmt: skip
    return found, [d[0] / seen, d[1] / seen, 0.0]


def create_auxiliary(tx, sh, parent, hinge, position):
    """An auxiliary view of ``parent`` normal to the sheet vector ``hinge``."""
    with builders.build(tx, tx.part.DraftingViews.CreateProjectedViewBuilder, None) as b:
        b.Parent.View.Value = parent
        b.Style.ViewStyleProjected.Scale = True  # inherit the parent scale
        line = b.Style.ViewStyleOrientation.HingeLine
        line.VectorOption = tx.nx.Drawings.HingeLineBuilder.Hingeline.Defined
        line.SpecifyVector = builders.direction(tx, hinge, field="/hinge")
        line.Associative = False
        b.Placement.Associative = True
        b.Placement.Placement.SetValue(None, None, sh.point(position))
        return b.commit()


def seen_length(tx, view, found, part_per_sheet):
    """The edge's length as drawn in ``view``, back in part units (scale removed)."""
    a, b = (drawing.to_sheet(tx, view, found[k]) for k in ("start", "end"))
    return math.dist(a, b) / drawing.scale(tx, view) * part_per_sheet


# ---- broken ------------------------------------------------------------------------------


def break_bands(tx, view, sheet, axis, breaks, gap):
    """Validated ``[(axis index, start, end)]`` bands (sheet units) of a broken view."""
    bounds = drawing.borders(tx, view)
    k = AXES[axis]
    bands = []
    for i, item in enumerate(breaks):
        field = f"/breaks/{i}"
        lo, hi = sorted(float(v) for v in item)
        if not bounds[k] < lo < hi < bounds[k + 2]:
            fail("NX_INVALID_ARGUMENT", "Both break lines must lie inside the view",
                 field=field, details={"bounds": bounds})  # fmt: skip
        if hi - lo <= gap:
            fail("NX_INVALID_ARGUMENT", "The removed band must be wider than the gap",
                 field=field, details={"band": hi - lo, "gap": gap})  # fmt: skip
        for j, (_, a, b) in enumerate(bands):
            if lo < b and a < hi:
                fail("NX_INVALID_ARGUMENT", f"Break {i} overlaps break {j}", field=field)
        bands.append((k, lo, hi))
    _refuse_hidden_centerlines(tx, sheet, bounds, bands)
    return bands


def _refuse_hidden_centerlines(tx, sheet, bounds, bands):
    """NX 2406 opens a modal notice when a break hides a centerline: refuse it first."""
    sh = drawing.Sheet(tx, sheet)
    for line in list(getattr(getattr(tx.part, "Annotations", None), "Centerlines", None) or []):
        try:
            x, y = sh.to_sheet(line.AnnotationOrigin)
        except Exception:  # noqa: S112 - centerline without an origin
            continue
        if not (bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]):
            continue
        for i, (k, lo, hi) in enumerate(bands):
            if lo <= (x, y)[k] <= hi:
                fail(
                    "NX_INVALID_ARGUMENT",
                    "The break would hide a centerline (NX then waits on a dialog): move the "
                    "break or delete the centerline",
                    field=f"/breaks/{i}",
                    details={"centerline": tx.ref(line, "annotation"), "at": [x, y]},
                )


def break_anchors(tx, view, bands):
    """Model anchors of each band's two break lines, all mapped from the view as it is
    before the first break (a committed break changes the view's sheet-to-model map)."""
    center = drawing.position(view)
    out = []
    for k, lo, hi in bands:
        pair = []
        for value in (lo, hi):
            p = list(center)
            p[k] = value
            pair.append([float(v) for v in tx.uf.View.MapDrawingToModel(view.Tag, p)][:3])
        out.append(pair)
    return out


def add_break(tx, sh, view, band, gap, anchors):
    """One regular break with straight lines across the view at model ``anchors``."""
    k = band[0]
    D = tx.nx.Drawings.BrokenViewBuilder
    with builders.build(tx, tx.part.DraftingViews.CreateBrokenViewBuilder, None) as b:
        b.MasterView.Value = view
        b.BreakType = D.TypeBreak.Regular
        b.DirectionType = D.TypeDirection.Vector
        b.DirectionVector = builders.direction(tx, [1.0, 0.0, 0.0] if k == 0 else [0.0, 1.0, 0.0])
        b.BreakLineType = D.BreakLineStyle.Simple
        b.BreakLine1ModelAnchor = builders.point(tx, anchors[0])
        b.BreakLine2ModelAnchor = builders.point(tx, anchors[1])
        b.IsBreakLine1Associative = False
        b.IsBreakLine2Associative = False
        b.Gap.RightHandSide = repr(float(gap) * sh.scale)  # part units, like the anchors
        b.commit()


def default_gap(sheet_units):
    return drawing.from_mm(sheet_units, DEFAULT_GAP_MM)[0]


def expected_bounds(before, bands, gap):
    """The view border after removing each band and inserting ``gap``: [x0, y0, x1, y1]."""
    out = list(before)
    for k, lo, hi in bands:
        out[k + 2] -= hi - lo - gap
    return out


# ---- break-out ---------------------------------------------------------------------------


def create_break_out(tx, view, center, radius, depth):
    """A circular break-out section of ``view``; returns its tag."""
    U = uf_types(tx)
    part = tx.part
    z_axis = drawing.axes(view)[2]  # toward the viewer
    base = [c - depth * z for c, z in zip(center, z_axis, strict=True)]
    tx.uf.View.ExpandView(view.Tag)
    try:  # curves made in the expanded member view are dependent on it
        boundary = part.Curves.CreateArc(
            tx.nx.Point3d(*(float(v) for v in center)),
            part.NXMatrices.Create(view.Matrix),
            float(radius),
            0.0,
            2 * math.pi,
        )
    finally:
        tx.uf.View.UnexpandWorkView()
    length = tx.nx.Scalar.DimensionalityType.Length
    update = tx.nx.SmartObject.UpdateOption.WithinModeling
    scalars = [part.Scalars.CreateScalar(float(v), length, update) for v in base]
    point = part.Points.CreatePoint(*scalars, update)  # NX needs a smart point here
    data = U.Draw.BreakoutData()
    data.BasePoint = point.Tag
    data.Curves = [boundary.Tag]
    data.NumCurves = 1
    data.ConstStatus = [False]
    return tx.uf.Draw.CreateBreakout(view.Tag, data)


def break_out_depth(tx, tag, center, view):
    """Read back the floor depth of a break-out below ``center`` along the view normal."""
    owner, data = tx.uf.Draw.AskBreakoutData(tag)
    readback.expect(int(owner) == int(view.Tag), "break_out", "NX put it in another view")
    point = tx.nx.TaggedObjectManager.GetTaggedObject(data.BasePoint).Coordinates
    floor = [float(point.X), float(point.Y), float(point.Z)]
    return geom.dot(geom.sub(center, floor), drawing.axes(view)[2]), int(data.NumCurves)


def sheet_tolerance(tx, sheet):
    return units.tolerance(units.sheet_units(tx, sheet), 1e-4)
