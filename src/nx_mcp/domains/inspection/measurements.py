"""Exact measurements: distance, angle, mass properties, bounds and wall thickness
(tool-surface 3.9). Lengths are in work-part units, angles in degrees, mass in kg.

NX 2406 findings kept here:

- Volumes come from ``MeasureManager.NewMassProperties`` in mm^3, converted to part
  units (``kit.measure``); mass and inertia from UF ``AskMassProps3d`` (kg, m, WCS axes),
  rotated to absolute axes.
- ``AskBoundingBoxExact`` is aligned to the WCS: exact absolute bounds need a WCS
  whose axes are the absolute axes, otherwise the call is refused.
- Overlapping bodies are summed, not united.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import BodyRef, Doc, FaceRef, Items, Range, RefTo
from nx_mcp.domains.inspection import common
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import geom, measure, result, select

DISTANCE_KINDS = (
    "body",
    "face",
    "edge",
    "curve",
    "point",
    "datum_plane",
    "datum_axis",
    "feature",
    "component",
)
ANGLE_KINDS = ("curve", "edge", "face", "datum_plane", "datum_axis")
Scope = Annotated[Literal["auto", "part", "assembly"], Doc("Without bodies: part or assembly")]
Targets = Annotated[list[Annotated[str, RefTo("body", "feature", "component")]], Items(1, 500)]


def _targets_text(args):
    if args.get("bodies"):
        return common.count_text(args, "bodies", "object")
    return args.get("scope", "auto") + " bodies"


# ---------------------------------------------------------------------------
# Distance


class DistanceData(TypedDict):
    distance: float
    points: list[list[float]]
    first: result.Row
    second: result.Row
    pairs: int


def _measurables(tx, ref, field):
    obj = tx.resolve(ref, *DISTANCE_KINDS)
    kind = common.kind_of(tx, obj)
    if kind in ("feature", "component"):
        return [(b, "body") for b in kbodies.require(kbodies.of(tx, obj), field=field)]
    return [(obj, kind)]


@tool(
    effect="read",
    label="Measure distance",
    summary="Distance {first} to {second}",
    keywords=("minimum distance", "gap", "measure", "clearance between", "closest points"),
    requires=("work_part",),
    cost="light",
    example={"first": "fc:12", "second": "fc:14"},
)
def nx_measure_distance(
    tx: Tx,
    *,
    first: Annotated[str, RefTo(*DISTANCE_KINDS)],
    second: Annotated[str, RefTo(*DISTANCE_KINDS)],
) -> DistanceData:
    """Measure the minimum distance and the closest points between two objects; features and
    components measure their bodies. Zero means touching or overlapping."""
    a = _measurables(tx, first, "/first")
    b = _measurables(tx, second, "/second")
    best = None
    for x, kx in a:
        for y, ky in b:
            tx.checkpoint()
            with native():
                distance, p1, p2, _accuracy = tx.session.Measurement.GetMinimumDistance(x, y)
            distance = result.measured(distance)
            if best is None or distance < best[0]:
                best = (distance, geom.xyz(p1), geom.xyz(p2), (x, kx), (y, ky))
    assert best is not None  # _measurables never returns an empty list
    distance, p1, p2, (x, kx), (y, ky) = best
    return {
        "distance": distance,
        "points": [p1, p2],
        "first": tx.ref(x, kind=kx),
        "second": tx.ref(y, kind=ky),
        "pairs": len(a) * len(b),
    }


# ---------------------------------------------------------------------------
# Angle


class AngleData(TypedDict):
    angle: float
    directions: list[list[float]]


def _direction(tx, obj, kind, field):
    if kind == "curve":
        start, end = getattr(obj, "StartPoint", None), getattr(obj, "EndPoint", None)
        if start is None or end is None or hasattr(obj, "Radius"):
            fail(
                "NX_WRONG_GEOMETRY", "Select a line, a straight edge or a planar face", field=field
            )
        return geom.unit(geom.sub(geom.xyz(end), geom.xyz(start)), field)
    if kind == "edge":
        if obj.SolidEdgeType != tx.nx.Edge.EdgeType.Linear:
            fail("NX_WRONG_GEOMETRY", "Select a straight edge", field=field)
        start, end = (geom.xyz(p) for p in obj.GetVertices())
        return geom.unit(geom.sub(end, start), field)
    if kind == "datum_axis":
        return geom.unit(geom.xyz(obj.Direction), field)
    if kind == "datum_plane":
        return geom.unit(geom.xyz(obj.Normal), field)
    code, _point, normal = select.face_data(tx, obj)
    if code != select.FACE_PLANE:
        fail("NX_WRONG_GEOMETRY", "Select a planar face", field=field)
    return geom.unit(normal, field)


@tool(
    effect="read",
    label="Measure angle",
    summary="Angle {first} to {second}",
    keywords=("angle between", "measure angle", "inclination", "perpendicular check"),
    requires=("work_part",),
    cost="light",
    example={"first": "fc:12", "second": "fc:14"},
)
def nx_measure_angle(
    tx: Tx,
    *,
    first: Annotated[str, RefTo(*ANGLE_KINDS)],
    second: Annotated[str, RefTo(*ANGLE_KINDS)],
) -> AngleData:
    """Measure the angle (0-180 degrees) between lines, straight edges, planar faces or datums.
    Lines run start to end, edges vertex to vertex; faces use the outward normal."""
    directions = []
    for ref, field in ((first, "/first"), (second, "/second")):
        obj = tx.resolve(ref, *ANGLE_KINDS)
        directions.append(_direction(tx, obj, common.kind_of(tx, obj), field))
    cosine = max(-1.0, min(1.0, geom.dot(*directions)))
    return {"angle": math.degrees(math.acos(cosine)), "directions": directions}


# ---------------------------------------------------------------------------
# Mass and bounds


class BodyVolume(TypedDict):
    body: result.Row
    volume: float


class MassData(TypedDict):
    volume: float
    area: float | None
    mass: float
    center_of_gravity: list[float]
    inertia: list[list[float]]
    principal_moments: list[float]
    bodies: list[BodyVolume]


@tool(
    effect="read",
    label="Measure mass properties",
    summary=lambda a, say: "Mass of " + _targets_text(a),
    keywords=("volume", "mass", "weight", "centre of gravity", "inertia", "mass properties"),
    requires=("work_part",),
    cost="light",
    example={"bodies": ["b:3"]},
)
def nx_measure_mass(
    tx: Tx,
    *,
    bodies: Targets | None = None,
    scope: Scope = "auto",
) -> MassData:
    """Measure volume, area, mass, centre of gravity and inertia of solid bodies. Mass is kg
    from assigned densities; inertia is kg*unit^2 about the centre of gravity."""
    found = kbodies.solids(common.target_bodies(tx, bodies, scope), field="/bodies")
    rows: list[BodyVolume] = []
    area: float | None = 0.0
    for body in found:
        tx.checkpoint()
        props = measure.mass(tx, [body])
        rows.append({"body": tx.ref(body, kind="body"), "volume": props["volume"]})
        area = None if area is None or props["area"] is None else area + props["area"]
    physics = measure.inertia(tx, found)
    return {
        "volume": sum(r["volume"] for r in rows),
        "area": area,
        "mass": physics["mass"],
        "center_of_gravity": physics["center"],
        "inertia": physics["inertia"],
        "principal_moments": physics["principal"],
        "bodies": rows,
    }


class BodyBounds(TypedDict):
    body: result.Row
    min: list[float]
    max: list[float]


class BoundsData(TypedDict):
    min: list[float]
    max: list[float]
    size: list[float]
    precision: str
    bodies: list[BodyBounds]


@tool(
    effect="read",
    label="Measure bounding box",
    summary=lambda a, say: "Bounds of " + _targets_text(a),
    keywords=("bounding box", "extents", "overall size", "envelope", "min max"),
    requires=("work_part",),
    cost="light",
    example={"bodies": ["b:3"], "precision": "exact"},
)
def nx_measure_bounds(
    tx: Tx,
    *,
    bodies: Targets | None = None,
    scope: Scope = "auto",
    precision: Annotated[
        Literal["conservative", "exact"], Doc("exact needs a WCS with absolute axes")
    ] = "conservative",
) -> BoundsData:
    """Measure the absolute axis-aligned bounding box of bodies, the part or the assembly.
    conservative may exceed curved geometry; exact is tight."""
    found = common.target_bodies(tx, bodies, scope)
    exact = precision == "exact"
    if exact and not measure.wcs_is_absolute(tx):
        fail(
            "NX_UNSUPPORTED_OPTION",
            "Exact bounds need a WCS aligned with the absolute axes",
            field="/precision",
            hint="Use precision conservative or reset the WCS to absolute",
        )
    rows: list[BodyBounds] = []
    for body in found:
        tx.checkpoint()
        low, high = measure.bounds(tx, [body], exact=exact)
        rows.append({"body": tx.ref(body, kind="body"), "min": low, "max": high})
    low = [min(r["min"][i] for r in rows) for i in range(3)]
    high = [max(r["max"][i] for r in rows) for i in range(3)]
    return {
        "min": low,
        "max": high,
        "size": geom.sub(high, low),
        "precision": precision,
        "bodies": rows,
    }


# ---------------------------------------------------------------------------
# Wall thickness


class ThicknessSample(TypedDict, total=False):
    face: result.Row
    point: list[float]
    thickness: float
    opposite_point: list[float]
    opposite_face: result.Row
    status: str


class ThicknessData(TypedDict):
    minimum: float | None
    maximum: float | None
    measured: int
    unresolved: int
    skipped_outside_trim: int
    samples: list[ThicknessSample]


IDENTITY_4X4 = [1.0 if i % 5 == 0 else 0.0 for i in range(16)]


def _first_exit(tx, body, origin, direction):
    with native():
        _count, hits = tx.uf.Modeling.TraceARay(1, [body.Tag], origin, direction, IDENTITY_4X4, 0)
    ahead = [
        h for h in hits or [] if geom.dot(geom.sub(common.xyz3(h.HitPoint), origin), direction) > 0
    ]
    return min(ahead, key=lambda h: geom.dist(common.xyz3(h.HitPoint), origin), default=None)


@tool(
    effect="read",
    label="Measure wall thickness",
    summary="Wall thickness of {body}",
    keywords=("wall thickness", "thin walls", "minimum thickness", "ray thickness", "molding"),
    requires=("work_part",),
    cost="heavy",
    example={"body": "b:3"},
)
def nx_measure_wall_thickness(
    tx: Tx,
    *,
    body: BodyRef,
    faces: Annotated[list[FaceRef], Items(1, 200)] | None = None,
    samples_per_axis: Annotated[int, Range(ge=1, le=20)] = 5,
    tolerance: Annotated[
        float, Range(gt=0, le=1), Doc("Inward ray start offset, part units")
    ] = 0.001,
) -> ThicknessData:
    """Measure sampled wall thickness of a solid: from points on its faces along the inward
    normal to the first exit. Sampled, not a guaranteed global minimum."""
    target = select.one(tx, body, "body", field="/body")
    if not getattr(target, "IsSolidBody", False):
        fail("NX_WRONG_GEOMETRY", "Select a solid body", field="/body")
    own = {int(f.Tag): f for f in target.GetFaces()}
    if faces is None:
        selected = list(own.values())
    else:
        selected = select.faces(tx, faces)
        for i, face in enumerate(selected):
            if int(face.Tag) not in own:
                fail("NX_WRONG_OWNER", "The face is not on this body", field=f"/faces/{i}")
    samples, skipped = common.face_samples(tx, selected, samples_per_axis)
    modeling = tx.uf.Modeling
    rows: list[ThicknessSample] = []
    values: list[float] = []
    unresolved = 0
    for index, (face, _uv, props) in enumerate(samples):
        if index % 50 == 0:
            tx.checkpoint(index, len(samples))
        point, normal = common.xyz3(props[0]), common.xyz3(props[5])
        inward = geom.scale(geom.unit(normal), -1.0)
        origin = geom.add(point, geom.scale(inward, tolerance))
        row: ThicknessSample = {"face": tx.ref(face, kind="face"), "point": point}
        with native():
            inside = int(modeling.AskPointContainment(origin, target.Tag)) == common.INSIDE
        hit = _first_exit(tx, target, origin, inward) if inside else None
        if hit is None:
            unresolved += 1
            row["status"] = "unresolved"
        else:
            exit_point = common.xyz3(hit.HitPoint)
            thickness = geom.dist(point, exit_point)
            values.append(thickness)
            row["status"] = "measured"
            row["thickness"] = thickness
            row["opposite_point"] = exit_point
            opposite = own.get(int(hit.HitFace))
            if opposite is not None:
                row["opposite_face"] = tx.ref(opposite, kind="face")
        rows.append(row)
    return {
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
        "measured": len(values),
        "unresolved": unresolved,
        "skipped_outside_trim": skipped,
        "samples": rows,
    }
