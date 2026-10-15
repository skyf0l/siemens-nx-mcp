"""Datum features (tool-surface 3.4)."""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Length, Name, Point3, RefTo, Vec3
from nx_mcp.domains.modeling import common
from nx_mcp.kit import args, builders, expr, geom, readback, result, select, units

WORLD_Z = [0.0, 0.0, 1.0]
# The fields each datum plane method takes; the others are refused.
PLANE_FIELDS = {
    "offset": ("reference", "distance"),
    "point_normal": ("origin", "normal"),
    "three_points": ("points",),
}


def foreign(method: str, fields: dict, **given) -> None:
    """Refuse an argument that belongs to another method (``fields``: method -> names)."""
    takes = fields[method]
    for name, value in given.items():
        if value is not None and name not in takes:
            owners = [m for m, names in fields.items() if name in names]
            fail(
                "NX_INVALID_ARGUMENT",
                f"{name} is not used by method {method!r}"
                + (f" ({' or '.join(owners)} only)" if owners else ""),
                field=f"/{name}",
                hint=f"method {method} takes {' and '.join(takes)}; drop {name}",
            )


# Coordinates beyond this many millimetres lie far outside any part NX models well.
FAR_MM = 1e5


def _warn_far(tx, point, what) -> None:
    """Warn about a datum placed absurdly far from the origin (a typo such as 1e12)."""
    limit = units.part_tolerance(tx, FAR_MM)
    if max(abs(float(v)) for v in point) > limit:
        tx.warn(
            "NX_NOTE",
            f"The datum's {what} {[round(float(v), 6) for v in point]} lies more than "
            f"{limit:g} from the origin: check the coordinates",
        )


def _warn_named(tx, name) -> None:
    """Warn when a datum, coordinate system or point of the part already has ``name``
    (NX allows it; a later lookup by name would be ambiguous). Names the existing one."""
    part = tx.part
    found: list = []
    for collection in ("Datums", "CoordinateSystems", "Points"):
        try:
            found += list(getattr(part, collection, None) or [])
        except TypeError:  # not a listable collection
            continue
    same = [o for o in found if str(getattr(o, "Name", "") or "").casefold() == name.casefold()]
    if same:
        existing = tx.ref(same[0])
        tx.warn(
            "NX_NOTE",
            f"{existing['id']} is already named {name!r}: two objects now share the name; "
            "rename one (nx_set_name) to tell them apart",
        )


class DatumPlaneData(TypedDict):
    feature: result.Row
    datum_plane: result.Row
    origin: Point3
    normal: Vec3


@tool(
    effect="model",
    label="Create datum plane",
    summary={
        "offset": "Datum plane {distance} from {reference}",
        "point_normal": "Datum plane through {origin}",
        "three_points": "Datum plane through 3 points",
    },
    keywords=(
        "datum plane",
        "reference plane",
        "offset plane",
        "construction plane",
        "work plane",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="method",
    required_by={
        "offset": ("reference", "distance"),
        "point_normal": ("origin", "normal"),
        "three_points": ("points",),
    },
    cost="light",
    previewable=True,
    invalidates="deleted",
    example={"method": "offset", "reference": "fc:7", "distance": "15 mm"},
)
def nx_create_datum_plane(
    tx: Tx,
    *,
    method: Literal["offset", "point_normal", "three_points"],
    reference: Annotated[
        common.PlaneName | Annotated[str, RefTo("datum_plane", "face")],
        Doc("Face/datum plane ref, or world plane xy (+Z), yz (+X), xz (-Y)"),
    ]
    | None = None,
    distance: Length | None = None,
    origin: Point3 | None = None,
    normal: Vec3 | None = None,
    points: Annotated[list[Point3], Items(3, 3)] | None = None,
    name: Name | None = None,
) -> DatumPlaneData:
    """Create a datum plane offset from a face, datum plane or world plane, through a point
    and normal, or through 3 points. Face and datum offsets follow the reference; a negative
    distance goes against its normal. Datum refs: nx_list_objects kind=datum."""
    # 1. Pure checks first: failing here is "not_started" (tx untouched).
    foreign(
        method,
        PLANE_FIELDS,
        reference=reference, distance=distance, origin=origin, normal=normal, points=points,
    )  # fmt: skip
    if method == "offset":
        args.need(reference=reference, distance=distance)
    elif method == "point_normal":
        args.need(origin=origin, normal=normal)
        want = geom.unit(normal, field="/normal")
    else:
        args.need(points=points)
        want = geom.plane_through(points, field="/points")  # rejects collinear points
    # 2. NX work; distance is already a float in part units.
    if method == "offset":
        # A ref is a planar face or datum plane of the work part; a world plane has no
        # object, so the offset is measured from a fixed datum plane on it (the distance
        # stays a feature expression either way).
        with tx.checks():  # a wrong reference is refused before any change (not_started)
            target, ref_origin, want = select.plane(
                tx, common.principal_plane(reference), "/reference"
            )
        if target is None:
            target = builders.fixed_plane(tx, ref_origin, want)
        geometry, nx_method = [target], "Distance"
    elif method == "point_normal":
        geometry = [builders.point(tx, origin), builders.direction(tx, want, origin)]
        nx_method = "PointDir"
    else:
        assert points is not None  # checked by args.need
        geometry, nx_method = [builders.point(tx, p) for p in points], "ThreePoints"
    with builders.build(
        tx, tx.part.Features.CreateDatumPlaneBuilder, tx.nx.Features.Feature.Null
    ) as b:
        plane = b.GetPlane()
        plane.SetUpdateOption(tx.nx.SmartObject.UpdateOption.WithinModeling)
        plane.SetMethod(getattr(tx.nx.PlaneTypes.MethodType, nx_method))
        plane.SetGeometry(geometry)
        if nx_method == "Distance":
            expr.drive(plane.Expression, distance)  # an expression keeps the offset driven
        plane.Evaluate()
        feature = b.commit()
    # 3. Read-back: a mismatch raises verification_failed; the kernel rolls back.
    datum = feature.DatumPlane
    got_origin, got_normal = geom.xyz(datum.Origin), geom.unit(geom.xyz(datum.Normal))
    readback.expect_same_direction(got_normal, want, check="normal")
    if method == "offset":
        gap = geom.dot(geom.sub(got_origin, ref_origin), want)
        readback.expect_close(gap, distance, check="offset")
    checked = [origin] if method == "point_normal" else points if method == "three_points" else []
    for i, p in enumerate(checked or []):
        readback.expect_on_plane(p, got_origin, got_normal, check=f"on_plane[{i}]")
    _warn_far(tx, got_origin, "origin")
    if name:  # name the datum too: it is what a later call references
        _warn_named(tx, name)
        result.rename(tx, datum, name)
        result.rename(tx, feature, name)
    return {
        "feature": tx.ref(feature, kind="feature"),
        "datum_plane": tx.ref(datum, kind="datum_plane"),
        "origin": got_origin,
        "normal": got_normal,
    }


class DatumAxisData(TypedDict):
    feature: result.Row
    datum_axis: result.Row
    origin: Point3
    direction: Vec3


AXIS_FIELDS = {
    "point_direction": ("origin", "direction"),
    "two_points": ("start", "end"),
    "reference": ("reference",),
    "intersection": ("planes",),
}


def _plane_line(tx, planes) -> tuple[list, list[float], list[float]]:
    """``(objects, point, unit direction)`` of the line where two planes meet."""
    found = [select.planar(tx, ref, f"/planes/{i}") for i, ref in enumerate(planes)]
    (a, oa, na), (b, ob, nb) = found
    d = geom.cross(na, nb)
    if geom.norm(d) <= 1e-9:
        fail("NX_WRONG_GEOMETRY", "The two planes are parallel: they do not meet in a line",
             field="/planes/1", hint="Pass two planes that cross")  # fmt: skip
    # The point of the line nearest the world origin: p = (h1 nb x d + h2 d x na) / |d|^2.
    h1, h2 = geom.dot(na, oa), geom.dot(nb, ob)
    p = geom.scale(
        geom.add(geom.scale(geom.cross(nb, d), h1), geom.scale(geom.cross(d, na), h2)),
        1.0 / geom.dot(d, d),
    )
    return [a, b], p, geom.unit(d)


def _associative_axis(tx, method, reference, planes):
    """Build a datum axis on a face, edge or datum axis (``reference``) or where two planes
    meet; returns ``(feature, wanted point, wanted direction)``."""
    types = tx.nx.Features.DatumAxisBuilder.Types
    with tx.checks():  # a wrong reference is refused before any change (not_started)
        if method == "reference":
            obj, point, want = select.axis(tx, reference, "/reference")
            objects = [obj]
        else:
            objects, point, want = _plane_line(tx, planes)
    with builders.build(tx, tx.part.Features.CreateDatumAxisBuilder, None) as b:
        if method == "reference":
            b.Type = types.CurveOrFaceAxis
            b.CurveOrFace.Value = objects[0]
        else:
            b.Type = types.Intersection
            b.Object1.Value, b.Object2.Value = objects
        b.IsAssociative = True
        feature = b.commit()
    return feature, point, want


@tool(
    effect="model",
    label="Create datum axis",
    summary={
        "point_direction": "Datum axis through {origin}",
        "two_points": "Datum axis {start} to {end}",
        "reference": "Datum axis on {reference}",
        "intersection": "Datum axis where {planes} meet",
    },
    keywords=(
        "datum axis",
        "reference axis",
        "axis of cylinder",
        "axis along edge",
        "plane intersection line",
        "construction axis",
        "rotation axis",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="method",
    required_by={
        "point_direction": ("origin", "direction"),
        "two_points": ("start", "end"),
        "reference": ("reference",),
        "intersection": ("planes",),
    },
    cost="light",
    previewable=True,
    invalidates="deleted",
    example={"method": "point_direction", "origin": [0, 0, 0], "direction": [0, 0, 1]},
)
def nx_create_datum_axis(
    tx: Tx,
    *,
    method: Literal["point_direction", "two_points", "reference", "intersection"],
    origin: Point3 | None = None,
    direction: Vec3 | None = None,
    start: Point3 | None = None,
    end: Point3 | None = None,
    reference: Annotated[
        str, RefTo("face", "edge", "datum_axis"), Doc("reference: cylinder/cone face, edge")
    ]
    | None = None,
    planes: Annotated[
        list[Annotated[str, RefTo("face", "datum_plane")]],
        Items(2, 2),
        Doc("intersection: two planar faces or datum planes"),
    ]
    | None = None,
    name: Name | None = None,
) -> DatumAxisData:
    """Create a datum axis, fixed or following a face, edge or two planes. Fixed: through
    origin along direction, or from start towards end (its origin, where a helix starts).
    reference: on a cylinder or cone face's axis, a linear edge or datum axis;
    intersection: where two planes meet. Both follow their references."""
    foreign(
        method,
        AXIS_FIELDS,
        origin=origin, direction=direction, start=start, end=end, reference=reference,
        planes=planes,
    )  # fmt: skip
    if method in ("reference", "intersection"):
        args.need(**{AXIS_FIELDS[method][0]: reference if method == "reference" else planes})
        feature, at, want = _associative_axis(tx, method, reference, planes)
        axis = feature.DatumAxis
        got_origin, got_direction = geom.xyz(axis.Origin), geom.unit(geom.xyz(axis.Direction))
        readback.expect_parallel(got_direction, want, check="direction")
        off = geom.norm(geom.cross(geom.sub(got_origin, at), want))
        readback.expect_close(off, 0.0, "on_line", tol=10 * geom.tol(got_origin, at))
        if name:
            _warn_named(tx, name)
            result.rename(tx, axis, name)
            result.rename(tx, feature, name)
        return {
            "feature": tx.ref(feature, kind="feature"),
            "datum_axis": tx.ref(axis, kind="datum_axis"),
            "origin": got_origin,
            "direction": got_direction,
        }
    if method == "point_direction":
        origin = args.required(origin, "origin")
        args.need(direction=direction)
        at, want = list(origin), geom.unit(direction, field="/direction")
    else:
        start = args.required(start, "start")
        args.need(end=end)
        at = list(start)
        if geom.dist(start, end) <= geom.tol(start, end):
            fail("NX_INVALID_ARGUMENT", "start and end must differ", field="/end")
        want = geom.unit(geom.sub(end, start), field="/end")
    types = tx.nx.Features.DatumAxisBuilder.Types
    with builders.build(tx, tx.part.Features.CreateDatumAxisBuilder, None) as b:
        # Both methods build a point-and-direction axis. NX 2406's TwoPoints axis
        # (Point1/Point2) reports an origin beyond the start (measured: start [0, 0, 0],
        # end [0, 0, 10] gave origin [0, 0, -0.5]), so a helix on it began below start.
        b.Type = types.PointAndDir
        b.Point = builders.point(tx, at)
        b.Vector = builders.direction(tx, want, at)
        feature = b.commit()
    axis = feature.DatumAxis
    got_origin, got_direction = geom.xyz(axis.Origin), geom.unit(geom.xyz(axis.Direction))
    readback.expect_same_direction(got_direction, want, check="direction")
    # NX extends the axis: its origin is some point on the requested line.
    off = geom.norm(geom.cross(geom.sub(got_origin, at), want))
    readback.expect_close(off, 0.0, "on_line", tol=geom.tol(got_origin, at))
    _warn_far(tx, got_origin, "origin")
    if name:
        _warn_named(tx, name)
        result.rename(tx, axis, name)
        result.rename(tx, feature, name)
    return {
        "feature": tx.ref(feature, kind="feature"),
        "datum_axis": tx.ref(axis, kind="datum_axis"),
        "origin": got_origin,
        "direction": got_direction,
    }


class _DatumCsysData(TypedDict):
    feature: result.Row
    datum_csys: result.Row
    origin: Point3
    x_axis: Vec3
    y_axis: Vec3
    z_axis: Vec3


class DatumCsysData(_DatumCsysData, total=False):
    planes: Annotated[dict[str, result.Row], Doc("Its datum planes by name: xy, yz, xz")]
    axes: Annotated[dict[str, result.Row], Doc("Its datum axes by name: x, y, z")]


def _children(entities, axes) -> tuple[dict, dict]:
    """``({xy|yz|xz: plane}, {x|y|z: axis})`` of a datum CSYS feature's entities: each
    plane by its normal (z, x, y), each axis by its direction."""
    planes: dict = {}
    lines: dict = {}
    names = (("x", "yz"), ("y", "xz"), ("z", "xy"))
    for e in entities:
        if hasattr(e, "Normal") and hasattr(e, "Origin"):
            n = geom.xyz(e.Normal)
            for (_axis, plane), a in zip(names, axes, strict=True):
                if geom.parallel(n, a, 1e-6):
                    planes.setdefault(plane, e)
        elif hasattr(e, "Direction") and hasattr(e, "Origin"):
            d = geom.xyz(e.Direction)
            for (axis, _plane), a in zip(names, axes, strict=True):
                if geom.parallel(d, a, 1e-6):
                    lines.setdefault(axis, e)
    return planes, lines


def _csys_axes(tx, x_axis, y_axis):
    """Unit (x, y) of a datum CSYS. A missing axis keeps z as close to world Z as it can
    (x = y cross Z, y = Z cross x; world X/Y when the given axis is along Z); y_axis only
    fixes the XY plane, so it is made perpendicular to x_axis (with a warning)."""
    if x_axis is None and y_axis is None:
        return [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]
    if x_axis is None:
        y = geom.unit(y_axis, field="/y_axis")
        x = geom.cross(y, WORLD_Z)
        return (geom.unit(x) if geom.norm(x) > 1e-6 else [1.0, 0.0, 0.0]), y
    x = geom.unit(x_axis, field="/x_axis")
    if y_axis is None:
        y = geom.cross(WORLD_Z, x)
        return x, (geom.unit(y) if geom.norm(y) > 1e-6 else [0.0, 1.0, 0.0])
    y = geom.unit(y_axis, field="/y_axis")
    cos = geom.dot(x, y)
    if abs(cos) > 1e-6:
        rest = geom.sub(y, geom.scale(x, cos))
        if geom.norm(rest) <= 1e-6:
            fail(
                "NX_INVALID_ARGUMENT",
                "y_axis is parallel to x_axis, so it fixes no XY plane",
                field="/y_axis",
                hint="Give a y_axis that points away from x_axis (it is made perpendicular), "
                "or omit y_axis to keep z near world Z",
            )
        y = geom.unit(rest)
        tx.warn(
            "NX_APPROXIMATED",
            f"y_axis was {math.degrees(math.acos(max(-1.0, min(1.0, cos)))):.6g} deg from "
            f"x_axis; made perpendicular: {[round(v, 9) for v in y]}",
        )
    return x, y


@tool(
    effect="model",
    label="Create datum CSYS",
    summary="Datum CSYS at {origin}",
    keywords=("datum csys", "coordinate system", "reference frame", "datum coordinate system"),
    requires=("work_part", "no_active_sketch"),
    cost="light",
    previewable=True,
    invalidates="deleted",
    example={"origin": [0, 0, 0], "x_axis": [1, 0, 0], "y_axis": [0, 1, 0]},
)
def nx_create_datum_csys(
    tx: Tx,
    *,
    origin: Point3,
    x_axis: Vec3 | None = None,
    y_axis: Vec3 | None = None,
    name: Name | None = None,
) -> DatumCsysData:
    """Create a fixed datum coordinate system at origin. y_axis is made perpendicular to
    x_axis, z = x cross y; omitted axes keep z near world Z (default: world axes). Returns its
    datum planes and axes; a name also names them (NAME_XY, NAME_X...)."""
    x, y = _csys_axes(tx, x_axis, y_axis)
    z = geom.cross(x, y)
    o = list(origin)
    update = tx.nx.SmartObject.UpdateOption.WithinModeling
    part = tx.part
    nx = tx.nx
    xform = part.Xforms.CreateXform(nx.Point3d(*o), nx.Vector3d(*x), nx.Vector3d(*y), update, 1.0)
    csys = part.CoordinateSystems.CreateCoordinateSystem(xform, update)
    with builders.build(tx, part.Features.CreateDatumCsysBuilder, None) as b:
        b.Csys = csys
        feature = b.commit()
    entities = list(feature.GetEntities())
    frames = [e for e in entities if hasattr(e, "Orientation") and hasattr(e, "Origin")]
    readback.expect(bool(frames), "csys", "NX built no coordinate system")
    m = frames[0].Orientation.Element
    got = {
        "origin": geom.xyz(frames[0].Origin),
        "x_axis": [float(m.Xx), float(m.Xy), float(m.Xz)],
        "y_axis": [float(m.Yx), float(m.Yy), float(m.Yz)],
        "z_axis": [float(m.Zx), float(m.Zy), float(m.Zz)],
    }
    readback.expect_close(got["origin"], o, "origin")
    for key, want in (("x_axis", x), ("y_axis", y), ("z_axis", z)):
        readback.expect_close(got[key], want, key, tol=1e-6)
    _warn_far(tx, got["origin"], "origin")
    planes, lines = _children(entities, [got["x_axis"], got["y_axis"], got["z_axis"]])
    if name:  # name the frame too: it is what a later call references
        _warn_named(tx, name)
        result.rename(tx, frames[0], name)
        result.rename(tx, feature, name)
        for key, child in [*planes.items(), *lines.items()]:
            result.rename(tx, child, f"{name}_{key.upper()}")
    out: DatumCsysData = {
        "feature": tx.ref(feature, kind="feature"),
        "datum_csys": tx.ref(frames[0], kind="datum_csys"),
        "origin": got["origin"],
        "x_axis": got["x_axis"],
        "y_axis": got["y_axis"],
        "z_axis": got["z_axis"],
    }
    if planes:
        out["planes"] = {k: tx.ref(v, kind="datum_plane") for k, v in sorted(planes.items())}
    if lines:
        out["axes"] = {k: tx.ref(v, kind="datum_axis") for k, v in sorted(lines.items())}
    return out


class DatumPointData(TypedDict):
    feature: result.Row
    point: result.Row
    position: Point3


@tool(
    effect="model",
    label="Create point feature",
    summary="Point at {position}",
    keywords=("datum point", "point feature", "reference point", "construction point"),
    requires=("work_part", "no_active_sketch"),
    cost="light",
    previewable=True,
    invalidates="deleted",
    example={"position": [10, 0, 5]},
)
def nx_create_datum_point(
    tx: Tx,
    *,
    position: Point3,
    name: Name | None = None,
) -> DatumPointData:
    """Create a fixed point feature at a work-part position [x, y, z]. Coordinates are plain
    numbers in part units: '10 mm' strings and expression names are not taken here. Later
    calls select the returned point."""
    p = list(position)
    with builders.build(tx, tx.part.BaseFeatures.CreatePointFeatureBuilder, None) as b:
        b.Point = builders.point(tx, p)
        feature = b.commit()
    points = [e for e in feature.GetEntities() if hasattr(e, "Coordinates")]
    readback.expect(bool(points), "point", "NX built no point")
    got = readback.expect_close(geom.xyz(points[0].Coordinates), p, "position")
    _warn_far(tx, got, "position")
    if name:
        _warn_named(tx, name)
        result.rename(tx, points[0], name)
        result.rename(tx, feature, name)
    return {
        "feature": tx.ref(feature, kind="feature"),
        "point": tx.ref(points[0], kind="point"),
        "position": got,
    }
