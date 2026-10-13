"""Datum features (tool-surface 3.4)."""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Length, Name, Point3, RefTo, Vec3
from nx_mcp.domains.modeling import common
from nx_mcp.kit import args, builders, expr, geom, readback, result, select

WORLD_Z = [0.0, 0.0, 1.0]


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
        target, ref_origin, want = select.plane(tx, common.principal_plane(reference), "/reference")
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
    for i, p in enumerate([origin] if method == "point_normal" else points or []):
        readback.expect_on_plane(p, got_origin, got_normal, check=f"on_plane[{i}]")
    if name:  # name the datum too: it is what a later call references
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


@tool(
    effect="model",
    label="Create datum axis",
    summary={
        "point_direction": "Datum axis through {origin}",
        "two_points": "Datum axis {start} to {end}",
    },
    keywords=("datum axis", "reference axis", "construction axis", "axis line", "rotation axis"),
    requires=("work_part", "no_active_sketch"),
    variants="method",
    required_by={"point_direction": ("origin", "direction"), "two_points": ("start", "end")},
    cost="light",
    previewable=True,
    invalidates="deleted",
    example={"method": "point_direction", "origin": [0, 0, 0], "direction": [0, 0, 1]},
)
def nx_create_datum_axis(
    tx: Tx,
    *,
    method: Literal["point_direction", "two_points"],
    origin: Point3 | None = None,
    direction: Vec3 | None = None,
    start: Point3 | None = None,
    end: Point3 | None = None,
    name: Name | None = None,
) -> DatumAxisData:
    """Create a fixed datum axis through origin along direction, or from start towards end.
    Its origin is origin or start (a helix on it starts there)."""
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
    if name:
        result.rename(tx, axis, name)
        result.rename(tx, feature, name)
    return {
        "feature": tx.ref(feature, kind="feature"),
        "datum_axis": tx.ref(axis, kind="datum_axis"),
        "origin": got_origin,
        "direction": got_direction,
    }


class DatumCsysData(TypedDict):
    feature: result.Row
    datum_csys: result.Row
    origin: Point3
    x_axis: Vec3
    y_axis: Vec3
    z_axis: Vec3


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
    x_axis, z = x cross y; omitted axes keep z near world Z (default: world axes)."""
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
    frames = [
        e for e in feature.GetEntities() if hasattr(e, "Orientation") and hasattr(e, "Origin")
    ]
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
    if name:  # name the frame too: it is what a later call references
        result.rename(tx, frames[0], name)
        result.rename(tx, feature, name)
    return {
        "feature": tx.ref(feature, kind="feature"),
        "datum_csys": tx.ref(frames[0], kind="datum_csys"),
        "origin": got["origin"],
        "x_axis": got["x_axis"],
        "y_axis": got["y_axis"],
        "z_axis": got["z_axis"],
    }


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
    if name:
        result.rename(tx, points[0], name)
        result.rename(tx, feature, name)
    return {
        "feature": tx.ref(feature, kind="feature"),
        "point": tx.ref(points[0], kind="point"),
        "position": got,
    }
