"""Exact measurements: distance, angle, mass properties, bounds and wall thickness
(tool-surface 3.9). Lengths are in work-part units, angles in degrees (or radians), mass in kg.

NX 2406 findings kept here:

- ``nx_measure_mass`` reads each body's volume and area with one UF ``AskMassProps3d``
  (``kit.measure.solid_props``: the values of ``MeasureManager.NewMassProperties`` at a
  fraction of its time per body, so a part of hundreds of bodies is one light call), and
  mass and inertia with one ``AskMassProps3d`` of every body (kg, m, WCS axes), rotated
  to absolute axes.
- ``AskBoundingBoxExact`` is aligned to the WCS: exact absolute bounds need a WCS
  whose axes are the absolute axes, otherwise the call is refused.
- Overlapping bodies are summed, not united.
- Distances between sets of bodies (features, components) measure pairs in order of their
  bounding-box gap and stop once that gap, a lower bound, exceeds the best distance
  found: the same closest pair as measuring every pair, in few NX distance calls.
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
from nx_mcp.kit import geom, massprops, measure, result, select
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


BOXED = ("body", "face", "edge")  # kinds whose UF box bounds the distance


def _box(tx, obj, kind):
    """The conservative box of a body, face or edge; None for other kinds or when NX
    gives none (that object's pairs are then always measured)."""
    if kind not in BOXED:
        return None
    try:
        with native():
            return [float(v) for v in tx.uf.ModlGeneral.AskBoundingBox(obj.Tag)]
    except Exception:
        return None


def _pairs(tx, a, b):
    """``[(lower bound, i, j)]`` of every pair, nearest bound first (0 without boxes)."""
    if len(a) * len(b) == 1:
        return [(0.0, 0, 0)]
    boxes_a = [_box(tx, x, k) for x, k in a]
    boxes_b = [_box(tx, y, k) for y, k in b]
    return sorted(
        (common.box_gap(p, q) if p is not None and q is not None else 0.0, i, j)
        for i, p in enumerate(boxes_a)
        for j, q in enumerate(boxes_b)
    )


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
    """Minimum distance and closest points between two objects (features and components:
    their bodies). touching is true at distance 0, as for overlapping bodies:
    nx_check_interference tells a clash from contact."""
    common.note_exploded(tx)
    a = _measurables(tx, first, "/first")
    b = _measurables(tx, second, "/second")
    tx.scale_budget(len(a) + len(b))
    best = None  # (distance, i, j, p1, p2): the first pair in a-then-b order wins a tie
    for lower, i, j in _pairs(tx, a, b):
        tx.checkpoint()
        if best is not None and lower > best[0] + 1e-6 * max(1.0, best[0]):
            break  # every remaining pair is farther than its box gap
        with native():
            distance, p1, p2, _accuracy = tx.session.Measurement.GetMinimumDistance(
                a[i][0], b[j][0]
            )
        distance = result.measured(distance)
        if best is None or (distance, i, j) < best[:3]:
            best = (distance, i, j, geom.xyz(p1), geom.xyz(p2))
    assert best is not None  # _measurables never returns an empty list
    distance, i, j, p1, p2 = best
    (x, kx), (y, ky) = a[i], b[j]
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
    180 minus the interior angle at a convex edge. A line against a face gives the angle to
    its normal (0: the line is perpendicular to the face; line-to-plane is 90 minus it)."""
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
    mass: float
    density: float | None  # kg/m^3 of this body
    material: str | None  # None: NX's default solid density


class MassData(TypedDict):
    volume: float
    area: float | None
    mass: float
    density: float | None  # mean: total mass / total volume
    center_of_gravity: list[float]
    inertia: list[list[float]]
    principal_moments: list[float]
    bodies: list[BodyVolume]


@tool(
    target="assembly",
    effect="read",
    label="Measure mass properties",
    summary=lambda a, say: "Mass of " + _targets_text(a),
    keywords=(
        "volume",
        "mass",
        "weight",
        "centre of gravity",
        "inertia",
        "mass properties",
        "density",
        "inertia tensor",
    ),
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
    material densities (set with nx_set_material); density is the mean kg/m^3, bodies give
    each one's. inertia: kg*unit^2 tensor about the centre of gravity in absolute axes,
    products negated (Ixy = -sum m*x*y); its eigenvalues are principal_moments."""
    common.note_exploded(tx)
    found = kbodies.solids(common.target_bodies(tx, bodies, scope), field="/bodies")
    found, skipped = kbodies.measurable(found, scoped=bodies is None)
    if skipped:
        tx.warn("NX_NOTE", f"Skipped {len(skipped)} empty or topology result bod"
                f"{'y' if len(skipped) == 1 else 'ies'} (an unsolved study's result is empty; "
                "pass a solved result's body to measure it)")  # fmt: skip
    kbodies.require(found, field="/bodies")
    rows: list[BodyVolume] = []
    area = 0.0
    per_m3 = measure.M_PER[kunits.part_units(tx)] ** 3
    for body in found:
        tx.checkpoint()
        props = measure.solid_props(tx, body)
        cubic = props["volume"] * per_m3
        rows.append({
            "body": tx.ref(body, kind="body"),
            "volume": props["volume"],
            "mass": props["mass"],
            "density": props["mass"] / cubic if cubic > 0 else None,
            "material": massprops.material_name(body),
        })  # fmt: skip
        area += props["area"]
    _note_default_density(tx, rows)
    physics = measure.inertia(tx, found)
    volume = sum(r["volume"] for r in rows)
    cubic_metres = volume * per_m3
    return {
        "volume": volume,
        "area": area,
        "mass": physics["mass"],
        "density": physics["mass"] / cubic_metres if cubic_metres > 0 else None,
        "center_of_gravity": _denoise([physics["center"]])[0],
        "inertia": _denoise(physics["inertia"]),
        "principal_moments": _denoise([physics["principal"]])[0],
        "bodies": rows,
    }


def _note_default_density(tx, rows) -> None:
    """``NX_NOTE`` naming the bodies without a material: NX weighs them with its default
    solid density (a preference, 7830.64 kg/m^3 out of the box)."""
    bare = [r for r in rows if r["material"] is None]
    if not bare:
        return
    shown = ", ".join(r["body"]["id"] for r in bare[:5]) + (", ..." if len(bare) > 5 else "")
    density = bare[0]["density"]
    used = f" ({density:.6g} kg/m^3)" if density else ""
    tx.warn(
        "NX_NOTE",
        f"{len(bare)} of {len(rows)} bodies have no material ({shown}): NX used its default "
        f"solid density{used}; assign one with nx_set_material",
    )


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
    keywords=(
        "bounding box",
        "extents",
        "overall size",
        "overall dimensions",
        "size of part",
        "length width height",
        "envelope",
        "min max",
    ),
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


class FaceThickness(TypedDict, total=False):
    face: result.Row
    minimum: float | None
    maximum: float | None
    measured: int
    min_point: list[float]  # where this face's thinnest sample starts


class ThicknessData(TypedDict):
    body: result.Row
    minimum: float | None
    maximum: float | None
    thinnest: ThicknessSample | None  # the thinnest sample (samples are thinnest first)
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
    """Measure sampled wall thickness of a solid (default: the body of faces, or the part's only
    one). Rays run along inward normals from its faces; not a guaranteed minimum. Samples
    come thinnest first; a plate's side faces give its width: see by_face."""
    common.note_exploded(tx)
    chosen = select.faces(tx, faces) if faces is not None else None
    target = _thickness_body(tx, body, chosen)
    own = {int(f.Tag): f for f in target.GetFaces()}
    if chosen is None:
        selected = list(own.values())
    else:
        selected = chosen
        for i, face in enumerate(selected):
            if int(face.Tag) not in own:
                fail("NX_WRONG_OWNER", "The face is not on this body", field=f"/faces/{i}")
    samples, skipped = common.face_samples(tx, selected, samples_per_axis)
    common.note_unsampled(tx, selected, samples)
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
    by_face = _by_face(rows)
    rows.sort(key=lambda r: (r.get("thickness") is None, r.get("thickness") or 0.0))
    return {
        "body": tx.ref(target, kind="body"),
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
        "thinnest": rows[0] if values else None,
        "measured": len(values),
        "unresolved": unresolved,
        "skipped_outside_trim": skipped,
        "by_face": by_face,
        "samples": rows,
    }


def _thickness_body(tx, ref, faces=None):
    """The body to measure: ``ref``, the one body of ``faces``, or the part's only solid."""
    if ref is not None:
        target = select.one(tx, ref, "body", field="/body")
        if not getattr(target, "IsSolidBody", False):
            fail("NX_WRONG_GEOMETRY", "Select a solid body", field="/body")
        return target
    if faces:
        owners = {}
        for face in faces:
            with native():
                owner = face.GetBody()
            owners.setdefault(int(owner.Tag), owner)
        if len(owners) == 1:
            target = next(iter(owners.values()))
            if not getattr(target, "IsSolidBody", False):
                fail("NX_WRONG_GEOMETRY", "The faces are on a sheet body; select faces of a "
                     "solid", field="/faces")  # fmt: skip
            return target
        fail(
            "NX_INVALID_ARGUMENT",
            f"The faces lie on {len(owners)} bodies; measure one body per call",
            field="/faces",
            hint="Split the faces per body, or name the body in body",
        )
    name = common.part_name(tx.part)
    found = [b for b in kbodies.scope(tx, "part") if getattr(b, "IsSolidBody", False)]
    if len(found) == 1:
        return found[0]
    if not found:
        fail("NX_NOT_FOUND", f"{name} has no solid body", field="/body")
    refs = ", ".join(tx.ref(b, kind="body")["id"] for b in found[:5])
    fail(
        "NX_INVALID_ARGUMENT",
        f"{name} has {len(found)} solid bodies; name one in body or pass its faces",
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
            if face["minimum"] is None or value < face["minimum"]:
                face["min_point"] = row["point"]
            face["minimum"] = value if face["minimum"] is None else min(face["minimum"], value)
            face["maximum"] = value if face["maximum"] is None else max(face["maximum"], value)
            face["measured"] += 1
    return sorted(faces.values(), key=lambda f: (f["minimum"] is None, f["minimum"] or 0.0))
