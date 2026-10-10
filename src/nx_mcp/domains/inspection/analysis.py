"""Sampled field analysis: face normals, curvature and draft; curve curvature; edge
continuity (tool-surface 3.9). Every value is sampled, not a certified global extremum.

NX 2406: a straight principal direction comes back as a radius of about 3.937e8 mm
(1e10 inch); ``common.radii`` reports it as radius None, curvature 0. UF
``ModlGeneral.EvaluateCurve`` (point and two derivatives) refuses solid edges
("Invalid object type"), and UF ``Eval`` gives unit tangents only, so curves use
``EvaluateCurve`` and edges a central difference of UF ``Eval`` unit tangents over a
1e-4 parameter step (chord-normalised: exact for arcs, O(step^2) otherwise).
"""

from __future__ import annotations

import math
from typing import Annotated, Any, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Angle,
    Doc,
    EdgeRef,
    FaceRef,
    Items,
    Length,
    Range,
    RefTo,
    forbid_extra,
)
from nx_mcp.domains.inspection import common, geometry
from nx_mcp.kit import curve as kcurve
from nx_mcp.kit import geom, result, select, surface

# ---------------------------------------------------------------------------
# Faces


class FaceSample(TypedDict, total=False):
    face: result.Row
    uv: list[float]
    point: list[float]
    normal: list[float]
    radii: list[float | None]
    curvatures: list[float | None]
    draft: float
    verdict: str


class FacesData(TypedDict):
    samples: list[FaceSample]
    skipped_outside_trim: int
    pull_direction: list[float] | None
    below_minimum: int


@tool(
    effect="read",
    label="Analyze faces",
    summary=lambda a, say: "Analyze " + common.count_text(a, "faces", "face"),
    keywords=("curvature", "draft analysis", "face normals", "surface analysis", "mold draft"),
    requires=("work_part",),
    cost="light",
    example={"faces": ["fc:12"], "pull_direction": "z", "minimum_draft": "3 deg"},
)
def nx_analyze_faces(
    tx: Tx,
    *,
    faces: Annotated[list[FaceRef], Items(1, 200)],
    samples_per_axis: Annotated[int, Range(ge=1, le=20)] = 5,
    pull_direction: geometry.Normal | None = None,
    minimum_draft: Annotated[Angle, Doc("Needs pull_direction; 0 to under 90 degrees")]
    | None = None,
) -> FacesData:
    """Sample normals and principal curvatures on a UV grid of faces; trimmed-out points are
    skipped. pull_direction adds draft (90 faces the pull, 0 a wall along it, <0 undercut)."""
    pull = None
    if pull_direction is not None:  # [x, y, z] or an axis word: z, +Z, -x, top, up...
        pull = geometry.direction(pull_direction, "/pull_direction")
    if minimum_draft is not None:
        minimum_draft = float(minimum_draft)  # the kernel converted the quantity to degrees
    if minimum_draft is not None and pull is None:
        fail("NX_INVALID_ARGUMENT", "minimum_draft needs pull_direction", field="/minimum_draft")
    if minimum_draft is not None and not 0 <= minimum_draft < 90:
        fail("NX_INVALID_ARGUMENT", "minimum_draft must be 0 to under 90", field="/minimum_draft")
    selected = select.faces(tx, faces)
    samples, skipped = common.face_samples(tx, selected, samples_per_axis)
    rows: list[FaceSample] = []
    below = 0
    for face, uv, props in samples:
        normal = common.unit_or_none(props[5]) or [0.0, 0.0, 0.0]
        radii, curvatures = common.radii(props[6][:2])
        row: FaceSample = {
            "face": tx.ref(face, kind="face"),
            "uv": [float(v) for v in uv],
            "point": common.xyz3(props[0]),
            "normal": normal,
            "radii": radii,
            "curvatures": curvatures,
        }
        if pull is not None:
            draft = math.degrees(math.asin(max(-1.0, min(1.0, geom.dot(normal, pull)))))
            row["draft"] = draft
            if minimum_draft is not None:
                verdict = (
                    "negative"
                    if draft < -1e-7
                    else "below_minimum"
                    if draft < minimum_draft
                    else "ok"
                )
                below += verdict != "ok"
                row["verdict"] = verdict
        rows.append(row)
    return {
        "samples": rows,
        "skipped_outside_trim": skipped,
        "pull_direction": pull,
        "below_minimum": below,
    }


# ---------------------------------------------------------------------------
# Curves


class CurveSample(TypedDict):
    t: float
    point: list[float]
    tangent: list[float] | None
    curvature: float | None
    radius: float | None


class CurveData(TypedDict, total=False):
    curve: result.Row
    samples: list[CurveSample]
    spline: dict[str, Any]


def _sample(tx, obj, t) -> CurveSample:
    with native():
        data = [float(v) for v in tx.uf.ModlGeneral.EvaluateCurve(obj.Tag, float(t), 2)]
    point, first, second = data[:3], data[3:6], data[6:9]
    speed = geom.norm(first)
    if speed <= 1e-12:
        return {"t": t, "point": point, "tangent": None, "curvature": None, "radius": None}
    curvature = geom.norm(geom.cross(first, second)) / speed**3
    radius = 1.0 / curvature if curvature > 1e-12 else None
    if radius is not None and radius >= common.FLAT_RADIUS:
        curvature, radius = 0.0, None
    return {
        "t": t,
        "point": point,
        "tangent": [v / speed for v in first],
        "curvature": curvature,
        "radius": radius,
    }


# Normalised parameter step of an edge's tangent difference (see the module docstring).
EDGE_STEP = 1e-4


def _edge_sample(e, t) -> CurveSample:
    """Point and unit tangent from UF Eval; curvature from the change of the unit tangent
    over the chord between ``t - EDGE_STEP`` and ``t + EDGE_STEP`` (clamped to 0..1)."""
    point, tangent = e.at(t)
    (p0, t0), (p1, t1) = e.at(max(0.0, t - EDGE_STEP)), e.at(min(1.0, t + EDGE_STEP))
    unit, u0, u1 = (common.unit_or_none(v) for v in (tangent, t0, t1))
    chord = geom.norm(geom.sub(p1, p0))
    if unit is None or u0 is None or u1 is None or chord <= 1e-12:
        return {"t": t, "point": point, "tangent": unit, "curvature": None, "radius": None}
    curvature = geom.norm(geom.sub(u1, u0)) / chord
    radius = 1.0 / curvature if curvature > 1e-12 else None
    if radius is None or radius >= common.FLAT_RADIUS:
        curvature, radius = 0.0, None
    return {"t": t, "point": point, "tangent": unit, "curvature": curvature, "radius": radius}


@tool(
    effect="read",
    label="Analyze curve",
    summary="Analyze curve {curve}",
    keywords=("curve curvature", "edge curvature", "spline analysis", "tangent", "knots"),
    requires=("work_part",),
    cost="light",
    example={"curve": "c:7", "samples": 11},
)
def nx_analyze_curve(
    tx: Tx,
    *,
    curve: Annotated[str, RefTo("curve", "edge")],
    samples: Annotated[int, Range(ge=2, le=1000)] = 11,
) -> CurveData:
    """Sample points, unit tangents and curvature along a curve or edge at evenly spaced
    parameters 0..1. Edge curvature is a tangent-difference estimate; splines add degree,
    knots and poles."""
    obj = select.one(tx, curve, "curve", "edge", field="/curve")
    kind = "edge" if common.kind_of(tx, obj) == "edge" else "curve"
    rows: list[CurveSample] = []
    if kind == "edge":
        with kcurve.evaluator(tx, obj) as e:
            for i in range(samples):
                if i % 100 == 0:
                    tx.checkpoint(i, samples)
                rows.append(_edge_sample(e, i / (samples - 1)))
    else:
        for i in range(samples):
            if i % 100 == 0:
                tx.checkpoint(i, samples)
            rows.append(_sample(tx, obj, i / (samples - 1)))
    data: CurveData = {"curve": tx.ref(obj, kind=kind), "samples": rows}
    if kind == "curve" and hasattr(obj, "Get3DPoles"):
        with native():
            data["spline"] = {
                "degree": int(obj.Order) - 1,
                "periodic": bool(obj.Periodic),
                "knots": [float(k) for k in obj.GetKnots()],
                "poles": [geom.xyz(p) for p in obj.Get3DPoles()],
            }
    return data


# ---------------------------------------------------------------------------
# Continuity


class Tolerances(TypedDict, total=False):
    position: Annotated[Length, Doc("G0 gap")]
    angle: Annotated[Angle, Doc("G1, under 90 degrees")]
    curvature: Annotated[float, Range(gt=0), Doc("G2, 1/part unit")]


forbid_extra(Tolerances)
DEFAULTS = {"position": 0.001, "angle": 0.1, "curvature": 0.01}


class ContinuityData(TypedDict):
    g0: bool
    g1: bool | None
    g2: bool | None
    gap: float
    angle: float | None
    curvature: float | None
    samples: int
    unresolved: int
    tolerances: dict[str, float]


def _limits(tolerances) -> dict[str, float]:
    """The tolerances over ``DEFAULTS``; the kernel converted lengths and angles."""
    limits = {**DEFAULTS, **{k: float(v) for k, v in (tolerances or {}).items()}}
    if limits["position"] <= 0:
        fail("NX_INVALID_ARGUMENT", "position must be above 0", field="/tolerances/position")
    if not 0 < limits["angle"] < 90:
        fail("NX_INVALID_ARGUMENT", "angle must be 0 to 90 degrees", field="/tolerances/angle")
    return limits


def _boundary_face(edge, ref, field):
    faces = list(edge.GetFaces())
    if len(faces) != 1:
        fail(
            "NX_WRONG_GEOMETRY",
            f"{ref} is an edge of {len(faces)} faces; continuity needs an open boundary "
            "edge of a sheet body (one face), not a solid edge or a curve",
            field=field,
            hint="Make sheets with nx_create_extract (faces) or nx_create_loft (solid false) "
            "and pick their boundary edges with nx_find_geometry",
            details={"faces": len(faces)},
        )
    return faces[0]


@tool(
    effect="read",
    label="Check continuity",
    summary="Continuity {first} to {second}",
    keywords=("g0 g1 g2", "tangency", "curvature continuity", "surface continuity", "gap"),
    requires=("work_part",),
    cost="light",
    example={"first": "e:21", "second": "e:34", "tolerances": {"position": "0.01 mm"}},
)
def nx_check_continuity(
    tx: Tx,
    *,
    first: EdgeRef,
    second: EdgeRef,
    tolerances: Tolerances | None = None,
    samples: Annotated[int, Range(ge=2, le=200), Doc("Per edge; result counts both")] = 20,
) -> ContinuityData:
    """Check G0/G1/G2 continuity between two sheet-body boundary edges (not curves or solid
    edges), sampled from both sides. G1 and G2 are null when a sample cannot be evaluated."""
    limits = _limits(tolerances)
    edges = [
        select.one(tx, ref, "edge", field=f) for ref, f in ((first, "/first"), (second, "/second"))
    ]
    if int(edges[0].Tag) == int(edges[1].Tag):
        fail("NX_INVALID_ARGUMENT", "Select two different edges", field="/second")
    faces = [
        _boundary_face(e, ref, f)
        for e, ref, f in zip(edges, (first, second), ("/first", "/second"), strict=True)
    ]
    reports = []
    for side in (0, 1):
        tx.checkpoint(side, 2)
        reports.append(
            surface.edge_continuity(
                tx, edges[side], faces[side], edges[1 - side], faces[1 - side], samples
            )
        )
    gap = max(r["gap"] for r in reports)
    unresolved = sum(r["unresolved"] for r in reports)
    angles = [r["angle"] for r in reports]
    curvatures = [r["curvature"] for r in reports]
    complete = unresolved == 0 and None not in angles
    angle = max(angles) if complete else None
    curvature = max(curvatures) if complete else None
    g0 = gap <= limits["position"]
    # angle and curvature are both set exactly when complete (kit.surface.edge_continuity)
    g1 = (g0 and angle <= limits["angle"]) if angle is not None else None
    g2 = (g1 and curvature <= limits["curvature"]) if curvature is not None else None
    return {
        "g0": g0,
        "g1": g1,
        "g2": g2,
        "gap": gap,
        "angle": angle,
        "curvature": curvature,
        "samples": 2 * samples,
        "unresolved": unresolved,
        "tolerances": limits,
    }
