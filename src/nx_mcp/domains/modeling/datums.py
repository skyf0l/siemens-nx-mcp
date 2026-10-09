"""Datum features (tool-surface 3.4)."""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Items, Length, Name, Point3, RefTo, Vec3
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
    reference: RefTo("datum_plane", "face") | None = None,  # type: ignore[valid-type]
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
