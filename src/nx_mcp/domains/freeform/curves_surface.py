"""Curves on faces: isoparametric curves (constant u or v), isocline curves (where a face
meets a direction at a given draft angle) and curves on surface (a spline on faces
through points).

NX 2406 lessons kept here (probed live on a cylinder d 40 h 60 and a sphere d 60):

- ``CreateIsoparametricCurvesBuilder``: ``SelectObject.Value`` is the face, ``Direction``
  ``IsoU``/``IsoV``/``IsoUV``. ``Uniform`` spreads ``Number`` curves per family from
  boundary to boundary, both included (a closed face repeats its seam: u 3 on the
  cylinder gives lines at 0, 180 and 360 degrees, length 60; v 3 circles at z 0, 30, 60,
  length 125.66371). ``ThroughPoints`` makes one curve per ``ConstraintManager`` point,
  ``BetweenPoints`` ``Number`` curves from the first point to the second, both included.
  ``Spacing`` (a percent, with ``IsSpacingEnabled``) is not exposed.
- ``FreeformCurveCollection.CreateIsoclineCurveBuilder`` (licensed with Modeling or a
  Studio freeform license): ``Face`` is a collector, ``ReferenceDirection`` a direction,
  ``IsoclineTypes`` ``Single`` (``IsoclineAngle``) or ``Multiple`` (``StartAngle``..
  ``EndAngle`` by ``IntervalAngle``). The angle is from the direction to the tangent
  plane, + where the outward normal leans toward the direction: a sphere R 30 about z at
  30 gives one circle at z = +15 of radius 30 cos 30 (length 163.24194); NX refuses
  angles outside (-90, 90). ``CreateIsocline`` is not needed before commit.
- ``CreateCurveOnSurfaceBuilder``: ``Faces`` collector, ``CurveType`` ``Spline``, points
  appended to ``ConstraintManager``; ``DistanceTolerance`` and ``AngleTolerance`` default
  to 0 and are set to the part tolerance and 0.5 degrees.

Read back: every curve lies on the faces (sampled UF minimum distance); isoclines meet
the direction at an asked angle (face normals at the samples); curves through points pass
through every point.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Angle, Direction, Doc, FaceRef, Items, Point3, Range
from nx_mcp.domains.freeform import curves_common as cc
from nx_mcp.kit import args, builders, curve, geom, licenses, readback, result, select, units

MAX_CURVES = 100
Faces = Annotated[list[FaceRef], Items(1, 100)]
ISOCLINE_LICENSES = ("solid_modeling", "studio_free_form")
ANGLE_TOLERANCE = 0.05  # degrees, isocline read-back
ISO_SAMPLES = 5  # face parameters read per isoparametric curve


class OnFaceData(cc.CurvesData):
    gap: float  # largest sampled distance of a curve from the faces


def _faces(tx, refs, field):
    args.distinct(field, refs, "faces")
    return select.faces(tx, refs, field)


def _on_points(tx, faces, points, field):
    """``points`` as lists, each on one of ``faces`` (else ``NX_INVALID_ARGUMENT``)."""
    out = [args.vec3(p) for p in points]
    for i, p in enumerate(out):
        gap = min(curve.point_distance(tx, f, p)[0] for f in faces)
        if gap > cc.gap_tolerance(tx, p):
            fail("NX_INVALID_ARGUMENT", f"Point is {gap:.6g} off the face",
                 field=f"{field}/{i}")  # fmt: skip
    return out


def _append_points(tx, manager, points):
    for p in points:
        data = manager.CreateGeometricConstraintData()
        data.Point = builders.point(tx, p)
        manager.Append(data)


def _on_faces(tx, curves, faces) -> float:
    gap = cc.worst_gap(tx, curves, faces)
    readback.expect(gap <= cc.gap_tolerance(tx), "faces", "A curve is off the faces", gap=gap)
    return gap


def _miss(tx, curves, points) -> float:
    """Largest distance from ``points`` to the nearest of ``curves``; fails over the limit."""
    miss = max(min(curve.point_distance(tx, c, p)[0] for c in curves) for p in points)
    readback.expect(
        miss <= cc.gap_tolerance(tx, *points), "points", "A curve misses a point", miss=miss
    )
    return miss


# ---------------------------------------------------------------------------
# Isoparametric curves


class IsoRow(cc.CurveRow):
    family: Literal["u", "v"]  # constant u or constant v
    parameter: float  # the constant face parameter (NX's u or v)
    duplicate_of: int | None  # index of an earlier curve this one coincides with (a seam)


class IsoData(TypedDict):
    feature: result.Row
    curves: list[IsoRow]
    count: int
    length: float
    gap: float  # largest sampled distance of a curve from the face
    distinct: int  # curves that are not duplicates


def _parms(tx, face, obj) -> list[list[float]]:
    """Face (u, v) of ``ISO_SAMPLES`` points along a curve on ``face``."""
    modeling = tx.uf.Modeling
    out = []
    for p in cc.samples(tx, obj, ISO_SAMPLES):
        with native(default="NX_ERROR"):
            uv = modeling.AskFaceParm(face.Tag, [float(v) for v in p])[0]
        out.append([float(uv[0]), float(uv[1])])
    return out


def _same(tx, a, b) -> bool:
    """Whether two curve rows run through the same points (either way round)."""
    limit = cc.gap_tolerance(tx, a["start"], a["end"])
    ends = (a["start"], a["end"])
    return abs(a["length"] - b["length"]) <= limit and all(
        min(geom.dist(p, q) for q in ends) <= limit for p in (b["start"], b["end"])
    )


def _iso_rows(tx, face, curves, rows, direction) -> list[IsoRow]:
    """Each curve row with its family, constant parameter and the earlier curve it
    coincides with (a closed face repeats its seam)."""
    out: list[IsoRow] = []
    for i, (obj, row) in enumerate(zip(curves, rows, strict=True)):
        tx.checkpoint()
        uv = _parms(tx, face, obj)
        spread = [max(p[k] for p in uv) - min(p[k] for p in uv) for k in (0, 1)]
        family = direction if direction != "uv" else ("u" if spread[0] <= spread[1] else "v")
        k = 0 if family == "u" else 1
        twin = next(
            (j for j in range(i) if out[j]["family"] == family and _same(tx, out[j], row)), None
        )
        out.append(
            {
                "curve": row["curve"],
                "length": row["length"],
                "start": row["start"],
                "end": row["end"],
                "closed": row["closed"],
                "family": "u" if family == "u" else "v",
                "parameter": sum(p[k] for p in uv) / len(uv),
                "duplicate_of": twin,
            }
        )
    return out


@tool(
    effect="model",
    label="Create isoparametric curves",
    summary={
        "uniform": "{count} isoparametric curves on {face}",
        "through_points": "Isoparametric curves on {face} through {points}",
        "between_points": "{count} isoparametric curves on {face} between {points}",
    },
    keywords=(
        "isoparametric curves",
        "iso curves",
        "uv lines on face",
        "u v lines",
        "isoparms",
        "constant parameter",
        "surface grid lines",
        "rulings",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    variants="method",
    required_by={
        "uniform": ("count",),
        "through_points": ("points",),
        "between_points": ("points", "count"),
    },
    cost="heavy",
    previewable=True,
    example={"face": "fc:7", "direction": "uv", "method": "uniform", "count": 4},
)
def nx_create_isoparametric_curves(
    tx: Tx,
    *,
    face: FaceRef,
    direction: Annotated[
        Literal["u", "v", "uv"], Doc("Family: constant u, constant v or both (NX's face u/v)")
    ] = "u",
    method: Literal["uniform", "through_points", "between_points"] = "uniform",
    count: Annotated[
        int, Range(ge=1, le=MAX_CURVES), Doc("uniform/between_points: curves per family")
    ]
    | None = None,
    points: Annotated[
        list[Point3], Items(1, MAX_CURVES), Doc("On the face. through_points: any; between: 2")
    ]
    | None = None,
) -> IsoData:
    """Create associative curves of constant u and/or v on a face. uniform: count curves
    per family from boundary to boundary, both included (a closed face repeats its seam:
    duplicate_of marks the copy); through_points: one curve through each point;
    between_points: count curves from the first point to the second, both included. u and
    v are NX's face parameters (on a cylinder one family runs along the axis, the other
    around): each curve row gives its family and parameter."""
    with tx.checks():
        target = select.one(tx, face, "face", field="/face")
        pts = []
        if method != "uniform":
            pts = _on_points(tx, [target], args.required(points, "points"), "/points")
        if method == "between_points" and len(pts) != 2:
            fail("NX_INVALID_ARGUMENT", "between_points takes exactly 2 points", field="/points")
        if method != "through_points":
            args.need(count=count)
    B = tx.nx.Features.IsoparametricCurvesBuilder
    with (
        cc.licensed(tx),
        builders.build(tx, tx.part.Features.CreateIsoparametricCurvesBuilder, None) as b,
    ):
        b.SelectObject.Value = target
        b.Associative = True
        b.Direction = getattr(
            B.DirectionTypes, {"u": "IsoU", "v": "IsoV", "uv": "IsoUV"}[direction]
        )
        b.Placement = getattr(B.PlacementTypes, {"uniform": "Uniform", "through_points":
                              "ThroughPoints", "between_points": "BetweenPoints"}[method])  # fmt: skip
        if count is not None and method != "through_points":
            b.Number = int(count)
        _append_points(tx, b.ConstraintManager, pts)
        feature = b.commit()
    curves = cc.output_curves(tx, feature)
    gap = _on_faces(tx, curves, [target])
    if pts:
        _miss(tx, curves, pts)
    data = cc.describe(tx, feature, curves)
    rows = _iso_rows(tx, target, curves, data["curves"], direction)
    copies = [r for r in rows if r["duplicate_of"] is not None]
    if copies:
        pairs = ", ".join(f"{rows.index(r)} = {r['duplicate_of']}" for r in copies)
        more = (
            f": count {(count or 0) + 1} gives {count} distinct curves"
            if method == "uniform"
            else ""
        )
        tx.warn("NX_NOTE", f"Curves {pairs} coincide on the closed face's seam (duplicate_of)"
                f"{more}")  # fmt: skip
    return {**data, "curves": rows, "gap": gap, "distinct": len(rows) - len(copies)}


# ---------------------------------------------------------------------------
# Isocline curves


class IsoclineData(OnFaceData):
    angles: list[float]  # the asked draft angles, degrees
    angle_error: float  # largest sampled difference from the nearest asked angle, degrees


def _face_normal(tx, face, point):
    """Unit normal of ``face`` at its point nearest ``point`` (UF face props)."""
    modeling = tx.uf.Modeling
    with native(default="NX_ERROR"):
        uv = modeling.AskFaceParm(face.Tag, [float(v) for v in point])[0]
        props = modeling.AskFaceProps(face.Tag, [float(v) for v in uv])
    return geom.unit([float(v) for v in props[5]], "normal")


def _draft(normal, along) -> float:
    """Degrees from ``along`` to the tangent plane, + where the normal leans toward it."""
    return math.degrees(math.asin(max(-1.0, min(1.0, geom.dot(normal, along)))))


def _angle_error(tx, curves, faces, along, angles) -> float:
    worst = 0.0
    for c in curves[: cc.MAX_SAMPLED]:
        tx.checkpoint()
        for p in cc.samples(tx, c):
            face = min(faces, key=lambda f: curve.point_distance(tx, f, p)[0])
            got = _draft(_face_normal(tx, face, p), along)
            worst = max(worst, min(abs(got - a) for a in angles))
    return worst


def _draft_value(value, field) -> float:
    angle = float(value)
    if not -90.0 < angle < 90.0:
        fail("NX_INVALID_ARGUMENT", "Draft angles lie between -90 and 90 (exclusive)",
             field=field)  # fmt: skip
    return angle


def _angles(method, angle, start_angle, end_angle, step) -> list[float]:
    if method == "single":
        return [_draft_value(args.required(angle, "angle"), "/angle")]
    args.need(start_angle=start_angle, end_angle=end_angle, step=step)
    low = _draft_value(start_angle, "/start_angle")
    high = _draft_value(end_angle, "/end_angle")
    size = float(step)
    if not size > 0:
        fail("NX_INVALID_ARGUMENT", "step must be positive", field="/step")
    if high <= low:
        fail("NX_INVALID_ARGUMENT", "end_angle must exceed start_angle", field="/end_angle")
    n = int(math.floor((high - low) / size + 1e-9)) + 1
    if n > MAX_CURVES:
        fail("NX_INVALID_ARGUMENT", f"At most {MAX_CURVES} angles", field="/step")
    return [low + i * size for i in range(n)]


Draft = Annotated[Angle, Doc("Degrees, between -90 and 90 (exclusive)")]


@tool(
    effect="model",
    label="Create isocline curves",
    summary={
        "single": "Isocline at {angle} on {faces}",
        "range": "Isoclines {start_angle} to {end_angle} on {faces}",
    },
    keywords=(
        "isocline curves",
        "draft lines",
        "parting line",
        "silhouette curve",
        "slope lines",
        "hull draft",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    variants="method",
    required_by={"single": ("angle",), "range": ("start_angle", "end_angle", "step")},
    cost="heavy",
    previewable=True,
    example={"faces": ["fc:7"], "direction": "z", "method": "single", "angle": 0},
)
def nx_create_isocline_curves(
    tx: Tx,
    *,
    faces: Annotated[Faces, Doc("Faces to search")],
    direction: Annotated[Direction, Doc("Reference (draw) direction")],
    method: Literal["single", "range"] = "single",
    angle: Annotated[Draft, Doc("single: draft angle")] | None = None,
    start_angle: Annotated[Draft, Doc("range: first angle")] | None = None,
    end_angle: Annotated[Draft, Doc("range: last angle (included when a step lands on it)")]
    | None = None,
    step: Annotated[Angle, Doc("range: angle step, > 0")] | None = None,
) -> IsoclineData:
    """Create associative curves where faces meet a direction at a draft angle. The angle
    is from the direction to the tangent plane, + where the outward normal leans toward
    the direction, within (-90, 90): 0 is the silhouette/parting line; on a sphere of
    radius R, angle a is a circle of radius R cos a. single: one angle; range:
    start_angle to end_angle by step. Returns the curves and the worst angle error."""
    with tx.checks():
        targets = _faces(tx, faces, "/faces")
        along = select.direction(tx, direction, "/direction")
        angles = _angles(method, angle, start_angle, end_angle, step)
    B = tx.nx.Features.IsoclineCurveBuilder
    collection = tx.part.Features.FreeformCurveCollection
    with (
        licenses.attempt(tx, ISOCLINE_LICENSES),
        builders.build(tx, collection.CreateIsoclineCurveBuilder, None) as b,
    ):
        builders.fill(tx, b.Face, targets, "Face")
        b.ReferenceDirection = builders.direction(tx, along)
        b.IsAssociative = True
        b.CurveFitData.Tolerance = units.part_tolerance(tx)
        if method == "single":
            b.IsoclineTypes = B.CurveTypes.Single
            b.IsoclineAngle.RightHandSide = repr(angles[0])
        else:
            b.IsoclineTypes = B.CurveTypes.Multiple
            b.StartAngle.RightHandSide = repr(float(start_angle))  # type: ignore[arg-type]
            b.EndAngle.RightHandSide = repr(float(end_angle))  # type: ignore[arg-type]
            b.IntervalAngle.RightHandSide = repr(float(step))  # type: ignore[arg-type]
        feature = b.commit()
    curves = cc.output_curves(tx, feature)
    gap = _on_faces(tx, curves, targets)
    error = _angle_error(tx, curves, targets, along, angles)
    readback.expect(error <= ANGLE_TOLERANCE, "angle", "A curve is off its angle", error=error)
    return {**cc.describe(tx, feature, curves), "gap": gap, "angles": angles,
            "angle_error": error}  # fmt: skip


# ---------------------------------------------------------------------------
# Curve on surface


class CurveOnSurfaceData(OnFaceData):
    miss: float  # largest distance of a given point from the curve


@tool(
    effect="model",
    label="Create curve on surface",
    summary="Curve on {faces} through {points}",
    keywords=(
        "curve on surface",
        "spline on face",
        "trim line",
        "curve on face",
        "surface spline",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"faces": ["fc:7"], "points": [[20, 0, 10], [10, 17.32051, 30], [-10, 17.32051, 50]]},
)
def nx_create_curve_on_surface(
    tx: Tx,
    *,
    faces: Annotated[Faces, Doc("Faces the curve lies on")],
    points: Annotated[list[Point3], Items(2, 100), Doc("In order, each on a face")],
    periodic: Annotated[bool, Doc("Closed: back to the first point")] = False,
) -> CurveOnSurfaceData:
    """Create an associative spline that lies on the faces and passes through the points
    in order (each must be on a face). Curves projected onto faces from points off them:
    nx_create_projected_curve."""
    with tx.checks():
        targets = _faces(tx, faces, "/faces")
        pts = _on_points(tx, targets, points, "/points")
        for i in range(1, len(pts)):
            if geom.dist(pts[i], pts[i - 1]) <= geom.tol(pts[i], pts[i - 1]):
                fail("NX_INVALID_ARGUMENT", "Consecutive points coincide", field=f"/points/{i}")
    tol = units.part_tolerance(tx)
    B = tx.nx.Features.CurveOnSurfaceBuilder
    with (
        cc.licensed(tx),
        builders.build(tx, tx.part.Features.CreateCurveOnSurfaceBuilder, None) as b,
    ):
        builders.fill(tx, b.Faces, targets, "Face")
        b.CurveType = B.CurveTypeEnum.Spline
        b.AssociativeToggle = True
        b.DistanceTolerance = tol
        b.AngleTolerance = 0.5
        b.CurveFitData.Tolerance = tol
        b.IsPeriodic = bool(periodic)
        _append_points(tx, b.ConstraintManager, pts)
        feature = b.commit()
    curves = cc.output_curves(tx, feature)
    gap = _on_faces(tx, curves, targets)
    miss = _miss(tx, curves, pts)
    return {**cc.describe(tx, feature, curves), "gap": gap, "miss": miss}
