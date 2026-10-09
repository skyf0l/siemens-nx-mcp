"""Sampled field analysis: face normals, curvature and draft; curve curvature; edge
continuity (tool-surface 3.9). Every value is sampled, not a certified global extremum.

NX 2406: a straight principal direction comes back as a radius of about 3.937e8 mm
(1e10 inch); ``common.radii`` reports it as radius None, curvature 0. UF
``ModlGeneral.EvaluateCurve`` (point and two derivatives) refuses solid edges
("Invalid object type"), and UF ``Eval`` gives unit tangents only, so curve analysis
takes curves.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Angle, Doc, Items, Range, RefTo, Vec3
from nx_mcp.domains.inspection import common
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
    example={"faces": ["fc:12"], "pull_direction": [0, 0, 1], "minimum_draft": 3},
)
def nx_analyze_faces(
    tx: Tx,
    *,
    faces: Annotated[list[RefTo("face")], Items(1, 200)],  # type: ignore[valid-type]
    samples_per_axis: Annotated[int, Range(ge=1, le=20)] = 5,
    pull_direction: Vec3 | None = None,
    minimum_draft: Annotated[Angle, Doc("Needs pull_direction; 0 to under 90 degrees")]
    | None = None,
) -> FacesData:
    """Sample normals and principal curvatures on a UV grid of faces; with a pull direction
    also signed draft angles. Points outside trimmed faces are skipped."""
    pull = geom.unit(pull_direction, "/pull_direction") if pull_direction is not None else None
    if minimum_draft is not None and pull is None:
        fail("NX_INVALID_ARGUMENT", "minimum_draft needs pull_direction", field="/minimum_draft")
    if minimum_draft is not None and not 0 <= minimum_draft < 90:
        fail("NX_INVALID_ARGUMENT", "minimum_draft must be 0 to under 90", field="/minimum_draft")
    selected = select.faces(tx, faces)
    samples, skipped = common.face_samples(tx, selected, samples_per_axis)
    rows, below = [], 0
    for face, uv, props in samples:
        normal = common.unit_or_none(props[5]) or [0.0, 0.0, 0.0]
        radii, curvatures = common.radii(props[6][:2])
        row: dict[str, Any] = {
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


def _sample(tx, obj, t):
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


@tool(
    effect="read",
    label="Analyze curve",
    summary="Analyze curve {curve}",
    keywords=("curve curvature", "spline analysis", "tangent", "knots", "poles"),
    requires=("work_part",),
    cost="light",
    example={"curve": "c:7", "samples": 20},
)
def nx_analyze_curve(
    tx: Tx,
    *,
    curve: RefTo("curve"),  # type: ignore[valid-type]
    samples: Annotated[int, Range(ge=2, le=1000)] = 20,
) -> CurveData:
    """Sample points, unit tangents and curvature along a curve (not a solid edge).
    Parameters are evenly spaced 0..1; splines add degree, knots and poles."""
    obj = select.one(tx, curve, "curve", field="/curve")
    rows = []
    for i in range(samples):
        if i % 100 == 0:
            tx.checkpoint(i, samples)
        rows.append(_sample(tx, obj, i / (samples - 1)))
    data: dict[str, Any] = {"curve": tx.ref(obj, kind="curve"), "samples": rows}
    if hasattr(obj, "Get3DPoles"):
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
    position: Annotated[float, Range(gt=0), Doc("G0 gap")]
    angle: Annotated[float, Range(gt=0, lt=90), Doc("G1 degrees")]
    curvature: Annotated[float, Range(gt=0), Doc("G2, 1/part unit")]


Tolerances.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]
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


def _boundary_face(edge, field):
    faces = list(edge.GetFaces())
    if len(faces) != 1:
        fail(
            "NX_AMBIGUOUS_REFERENCE",
            "Select a boundary edge of a sheet: it must have exactly one face",
            field=field,
        )
    return faces[0]


@tool(
    effect="read",
    label="Check continuity",
    summary="Continuity {first} to {second}",
    keywords=("g0 g1 g2", "tangency", "curvature continuity", "surface continuity", "gap"),
    requires=("work_part",),
    cost="light",
    example={"first": "e:21", "second": "e:34"},
)
def nx_check_continuity(
    tx: Tx,
    *,
    first: RefTo("edge"),  # type: ignore[valid-type]
    second: RefTo("edge"),  # type: ignore[valid-type]
    tolerances: Tolerances | None = None,
    samples: Annotated[int, Range(ge=2, le=200), Doc("Samples per edge")] = 20,
) -> ContinuityData:
    """Check G0/G1/G2 continuity between two sheet boundary edges, sampled from both sides.
    G1 and G2 are null when a sample cannot be evaluated."""
    limits = {**DEFAULTS, **(tolerances or {})}
    edges = [
        select.one(tx, ref, "edge", field=f) for ref, f in ((first, "/first"), (second, "/second"))
    ]
    if int(edges[0].Tag) == int(edges[1].Tag):
        fail("NX_INVALID_ARGUMENT", "Select two different edges", field="/second")
    faces = [_boundary_face(e, f) for e, f in zip(edges, ("/first", "/second"), strict=True)]
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
    g1 = (g0 and angle <= limits["angle"]) if complete else None
    g2 = (g1 and curvature <= limits["curvature"]) if complete else None
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
