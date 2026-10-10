"""Datum features (tool-surface 3.4)."""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Length, Name, Point3, RefTo, Vec3
from nx_mcp.kit import args, builders, geom, readback, result, select


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
    cost="light",
    previewable=True,
    invalidates="deleted",
    example={"method": "offset", "reference": "fc:7", "distance": "15 mm"},
)
def nx_create_datum_plane(
    tx: Tx,
    *,
    method: Literal["offset", "point_normal", "three_points"],
    reference: Annotated[str, RefTo("datum_plane", "face")] | None = None,
    distance: Length | None = None,
    origin: Point3 | None = None,
    normal: Vec3 | None = None,
    points: Annotated[list[Point3], Items(3, 3)] | None = None,
    name: Name | None = None,
) -> DatumPlaneData:
    """Create a datum plane offset from a planar face or datum plane, through a point with a
    normal, or through three points. Offset planes follow the reference; a negative distance
    goes against its normal."""
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
        target, ref_origin, want = select.planar(tx, reference)  # planar + work-part owned
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
        if method == "offset":
            plane.Expression.RightHandSide = repr(distance)
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
    if name:
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
    """Create a fixed datum axis through a point along a direction, or from start to end."""
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
        if method == "point_direction":
            b.Type = types.PointAndDir
            b.Point = builders.point(tx, at)
            b.Vector = builders.direction(tx, want, at)
        else:
            # NX 2406: two points are Point1/Point2 (there is no StartPoint).
            b.Type = types.TwoPoints
            b.Point1 = builders.point(tx, start)
            b.Point2 = builders.point(tx, end)
        feature = b.commit()
    axis = feature.DatumAxis
    got_origin, got_direction = geom.xyz(axis.Origin), geom.unit(geom.xyz(axis.Direction))
    readback.expect_same_direction(got_direction, want, check="direction")
    # NX extends the axis: its origin is some point on the requested line.
    off = geom.norm(geom.cross(geom.sub(got_origin, at), want))
    readback.expect_close(off, 0.0, "on_line", tol=geom.tol(got_origin, at))
    if name:
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
    x_axis: Vec3,
    y_axis: Annotated[Vec3, Doc("Perpendicular to x_axis; z = x cross y")],
    name: Name | None = None,
) -> DatumCsysData:
    """Create a fixed datum coordinate system from an origin and two perpendicular axes.
    The axes are normalised and z = x cross y."""
    x = geom.unit(x_axis, field="/x_axis")
    y = geom.unit(y_axis, field="/y_axis")
    if abs(geom.dot(x, y)) > 1e-6:
        fail("NX_INVALID_ARGUMENT", "x_axis and y_axis must be perpendicular", field="/y_axis")
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
    if name:
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
    """Create a fixed point feature at a work-part position."""
    p = list(position)
    with builders.build(tx, tx.part.BaseFeatures.CreatePointFeatureBuilder, None) as b:
        b.Point = builders.point(tx, p)
        feature = b.commit()
    points = [e for e in feature.GetEntities() if hasattr(e, "Coordinates")]
    readback.expect(bool(points), "point", "NX built no point")
    got = readback.expect_close(geom.xyz(points[0].Coordinates), p, "position")
    if name:
        result.rename(tx, feature, name)
    return {"feature": tx.ref(feature, kind="feature"), "position": got}
