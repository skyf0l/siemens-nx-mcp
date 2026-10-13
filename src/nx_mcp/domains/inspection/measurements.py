"""Exact measurements: distance, angle, mass properties, bounds and wall thickness
(tool-surface 3.9). Lengths are in work-part units, angles in degrees (or radians), mass in kg.

NX 2406 findings kept here:

- Volumes come from ``MeasureManager.NewMassProperties`` in mm^3, converted to part
  units (``kit.measure``); mass and inertia from UF ``AskMassProps3d`` (kg, m, WCS axes),
  rotated to absolute axes.
- ``AskBoundingBoxExact`` is aligned to the WCS: exact absolute bounds need a WCS
  whose axes are the absolute axes, otherwise the call is refused.
- Overlapping bodies are summed, not united.
- A feature reference measures the feature's whole bodies (``kit.bodies.of``), like the
  other measurements: the bounds of a boss are those of the body it was united into.
  ``nx_measure_section`` or ``nx_find_geometry`` with the feature as owner read the
  feature's own geometry.
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
from nx_mcp.kit import units as kunits

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
DISTANCE_DOC = "Ref 'fc:12'/'b:3': body/face/edge/curve/point/datum/feature/component"
ANGLE_KINDS = ("curve", "edge", "face", "datum_plane", "datum_axis")
TOUCHING = 1e-9  # part units: NX reports 0 for touching or overlapping objects
NOISE = 1e-12  # inertia terms below this fraction of the largest are rounding noise
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
    touching: bool
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
    target="assembly",
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
    first: Annotated[str, RefTo(*DISTANCE_KINDS, doc=DISTANCE_DOC)],
    second: Annotated[str, RefTo(*DISTANCE_KINDS, doc=DISTANCE_DOC)],
) -> DistanceData:
    """Measure the minimum distance and the closest points between two objects; features and
    components measure their bodies. touching is true at distance 0."""
    common.note_exploded(tx)
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
        "touching": distance <= TOUCHING,
        "points": [p1, p2],
        "first": tx.ref(x, kind=kx),
        "second": tx.ref(y, kind=ky),
        "pairs": len(a) * len(b),
    }


# ---------------------------------------------------------------------------
# Angle


class AngleData(TypedDict):
    angle: float
    units: str
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
        fail(
            "NX_WRONG_GEOMETRY",
            "Select a planar face",
            field=field,
            hint="For a cylinder's axis: nx_find_geometry gives its axis; measure a straight "
            "edge along it, or a datum axis made there (nx_create_datum_axis)"
            if code == select.FACE_CYLINDER
            else None,
        )
    return geom.unit(normal, field)


@tool(
    target="assembly",
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
    units: Literal["deg", "rad"] = "deg",
) -> AngleData:
    """Measure the angle (0-180) between lines, straight edges, planar faces or datums. Lines and
    edges run start to end; faces use outward normals: perpendicular 90, opposite parallel 180,
    180 minus the interior angle at a convex edge."""
    common.note_exploded(tx)
    directions = []
    for ref, field in ((first, "/first"), (second, "/second")):
        obj = tx.resolve(ref, *ANGLE_KINDS)
        directions.append(_direction(tx, obj, common.kind_of(tx, obj), field))
    cosine = max(-1.0, min(1.0, geom.dot(*directions)))
    angle = math.acos(cosine)
    if units == "rad":
        tx.units["angle"] = "rad"
    else:
        angle = math.degrees(angle)
    return {"angle": angle, "units": units, "directions": directions}


# ---------------------------------------------------------------------------
# Mass and bounds


class BodyVolume(TypedDict):
    body: result.Row
    volume: float


class MassData(TypedDict):
    volume: float
    area: float | None
    mass: float
    density: float | None
    center_of_gravity: list[float]
    inertia: list[list[float]]
    principal_moments: list[float]
    bodies: list[BodyVolume]


@tool(
    target="assembly",
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
    """Measure volume, area, mass, centre of gravity and inertia of solid bodies. Mass (kg) uses
    material densities (density: the kg/m^3 used; set with nx_set_material); inertia is
    kg*unit^2 about the centre of gravity."""
    common.note_exploded(tx)
    found = kbodies.solids(common.target_bodies(tx, bodies, scope), field="/bodies")
    found, skipped = kbodies.measurable(found, scoped=bodies is None)
    if skipped:
        tx.warn("NX_NOTE", f"Skipped {len(skipped)} empty or topology result bod"
                f"{'y' if len(skipped) == 1 else 'ies'} (an unsolved study's result is empty; "
                "pass a solved result's body to measure it)")  # fmt: skip
    kbodies.require(found, field="/bodies")
    rows: list[BodyVolume] = []
    area: float | None = 0.0
    for body in found:
        tx.checkpoint()
        props = measure.mass(tx, [body])
        rows.append({"body": tx.ref(body, kind="body"), "volume": props["volume"]})
        area = None if area is None or props["area"] is None else area + props["area"]
    physics = measure.inertia(tx, found)
    volume = sum(r["volume"] for r in rows)
    cubic_metres = volume * measure.M_PER[kunits.part_units(tx)] ** 3
    return {
        "volume": volume,
        "area": area,
        "mass": physics["mass"],
        "density": physics["mass"] / cubic_metres if cubic_metres > 0 else None,
        "center_of_gravity": physics["center"],
        "inertia": _denoise(physics["inertia"]),
        "principal_moments": _denoise([physics["principal"]])[0],
        "bodies": rows,
    }


def _denoise(rows):
    """Terms below ``NOISE`` times the largest magnitude are rounding noise: exact 0."""
    largest = max((abs(v) for row in rows for v in row), default=0.0)
    return [[0.0 if abs(v) <= NOISE * largest else v for v in row] for row in rows]


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
    target="assembly",
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
    Features and components count their whole bodies; conservative may exceed curves."""
    common.note_exploded(tx)
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


class FaceThickness(TypedDict):
    face: result.Row
    minimum: float | None
    maximum: float | None
    measured: int


class ThicknessData(TypedDict):
    body: result.Row
    minimum: float | None
    maximum: float | None
    measured: int
    unresolved: int
    skipped_outside_trim: int
    by_face: list[FaceThickness]
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
    summary=lambda a, say: "Wall thickness of " + (a.get("body") or "the solid body"),
    keywords=("wall thickness", "thin walls", "minimum thickness", "ray thickness", "molding"),
    requires=("work_part",),
    cost="heavy",
    example={"body": "b:3", "faces": ["fc:12"]},
)
def nx_measure_wall_thickness(
    tx: Tx,
    *,
    body: BodyRef | None = None,
    faces: Annotated[list[FaceRef], Items(1, 200)] | None = None,
    samples_per_axis: Annotated[int, Range(ge=1, le=20)] = 5,
    tolerance: Annotated[
        float, Range(gt=0, le=1), Doc("Inward ray start offset, part units")
    ] = 0.001,
) -> ThicknessData:
    """Measure sampled wall thickness of a solid (default: the part's only one). Rays run along
    inward normals from its faces; not a guaranteed minimum. A plate's side faces give its
    width: see by_face."""
    common.note_exploded(tx)
    target = _thickness_body(tx, body)
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
        "body": tx.ref(target, kind="body"),
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
        "measured": len(values),
        "unresolved": unresolved,
        "skipped_outside_trim": skipped,
        "by_face": _by_face(rows),
        "samples": rows,
    }


def _thickness_body(tx, ref):
    """The body to measure: ``ref``, or the work part's only solid body."""
    if ref is not None:
        target = select.one(tx, ref, "body", field="/body")
        if not getattr(target, "IsSolidBody", False):
            fail("NX_WRONG_GEOMETRY", "Select a solid body", field="/body")
        return target
    found = [b for b in kbodies.scope(tx, "part") if getattr(b, "IsSolidBody", False)]
    if len(found) == 1:
        return found[0]
    if not found:
        fail("NX_NOT_FOUND", "The work part has no solid body", field="/body")
    refs = ", ".join(tx.ref(b, kind="body")["id"] for b in found[:5])
    fail(
        "NX_INVALID_ARGUMENT",
        f"The work part has {len(found)} solid bodies; name one in body",
        field="/body",
        hint=f"body is one of {refs}" + (", ..." if len(found) > 5 else ""),
    )


def _by_face(rows) -> list[FaceThickness]:
    """Per-face thickness summary, thinnest face first (faces with no hit last)."""
    faces: dict[str, FaceThickness] = {}
    for row in rows:
        face = faces.setdefault(
            row["face"]["id"],
            {"face": row["face"], "minimum": None, "maximum": None, "measured": 0},
        )
        if "thickness" in row:
            value = row["thickness"]
            face["minimum"] = value if face["minimum"] is None else min(face["minimum"], value)
            face["maximum"] = value if face["maximum"] is None else max(face["maximum"], value)
            face["measured"] += 1
    return sorted(faces.values(), key=lambda f: (f["minimum"] is None, f["minimum"] or 0.0))
