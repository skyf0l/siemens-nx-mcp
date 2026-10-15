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

import contextlib
import math
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Angle,
    Doc,
    FaceRef,
    Items,
    Length,
    Range,
    RefTo,
    forbid_extra,
)
from nx_mcp.domains.inspection import common, geometry
from nx_mcp.kit import curve as kcurve
from nx_mcp.kit import geom, measure, result, select, surface

# ---------------------------------------------------------------------------
# Faces


class FaceSample(TypedDict, total=False):
    uv: list[float]
    point: list[float]
    normal: list[float]
    radii: list[float | None]
    curvatures: list[float | None]
    draft: float
    verdict: str


class FaceSummary(TypedDict, total=False):
    samples: int
    min_radius: float | None  # smallest absolute principal radius (None: flat everywhere)
    min_radius_point: list[float]
    max_curvature: float  # largest absolute principal curvature
    min_draft: float
    min_draft_point: list[float]
    below_minimum: int
    negative: int


class FaceSamples(TypedDict):
    face: result.Row
    summary: FaceSummary
    samples: list[FaceSample]


class FacesData(TypedDict):
    faces: list[FaceSamples]
    sample_count: int
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
    samples: Annotated[
        Literal["all", "violations", "none"],
        Doc("Sample rows kept; violations: draft below minimum_draft"),
    ] = "all",
) -> FacesData:
    """Sample normals and principal curvatures on a UV grid of each face, with a summary per
    face. pull_direction adds draft (90 faces the pull, 0 a wall along it, <0 undercut).
    Curvature is negative on convex, positive on concave faces; uv are NX face parameters."""
    pull = None
    if pull_direction is not None:  # [x, y, z] or an axis word: z, +Z, -x, top, up...
        pull = geometry.direction(pull_direction, "/pull_direction")
    if minimum_draft is not None:
        minimum_draft = float(minimum_draft)  # the kernel converted the quantity to degrees
    if minimum_draft is not None and pull is None:
        fail("NX_INVALID_ARGUMENT", "minimum_draft needs pull_direction", field="/minimum_draft")
    if minimum_draft is not None and not 0 <= minimum_draft < 90:
        fail("NX_INVALID_ARGUMENT", "minimum_draft must be 0 to under 90", field="/minimum_draft")
    if samples == "violations" and minimum_draft is None:
        fail("NX_INVALID_ARGUMENT", "samples violations needs minimum_draft and pull_direction",
             field="/samples")  # fmt: skip
    keep = samples
    selected = select.faces(tx, faces)
    found, skipped = common.face_samples(tx, selected, samples_per_axis)
    common.note_unsampled(tx, selected, found)
    groups: dict[int, FaceSamples] = {
        int(f.Tag): {"face": tx.ref(f, kind="face"), "summary": {"samples": 0}, "samples": []}
        for f in selected
    }
    below = 0
    for face, uv, props in found:
        normal = common.unit_or_none(props[5]) or [0.0, 0.0, 0.0]
        radii, curvatures = common.radii(props[6][:2])
        row: FaceSample = {
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
        group = groups[int(face.Tag)]
        _summarise(group["summary"], row)
        if keep == "all" or keep == "violations" and row.get("verdict", "ok") != "ok":
            group["samples"].append(row)
    return {
        "faces": list(groups.values()),
        "sample_count": len(found),
        "skipped_outside_trim": skipped,
        "pull_direction": pull,
        "below_minimum": below,
    }


def _summarise(summary: FaceSummary, row: FaceSample) -> None:
    """Fold one sample into its face's summary."""
    summary["samples"] = summary.get("samples", 0) + 1
    radii = [abs(r) for r in row["radii"] if r is not None]
    current = summary.get("min_radius")
    if radii and (current is None or min(radii) < current):
        summary["min_radius"], summary["min_radius_point"] = min(radii), row["point"]
    elif "min_radius" not in summary:
        summary["min_radius"] = None
    bends = [abs(k) for k in row["curvatures"] if k is not None]
    summary["max_curvature"] = max([summary.get("max_curvature", 0.0), *bends])
    if "draft" in row and ("min_draft" not in summary or row["draft"] < summary["min_draft"]):
        summary["min_draft"], summary["min_draft_point"] = row["draft"], row["point"]
    if "verdict" in row:
        summary["below_minimum"] = summary.get("below_minimum", 0) + (row["verdict"] != "ok")
        summary["negative"] = summary.get("negative", 0) + (row["verdict"] == "negative")


# ---------------------------------------------------------------------------
# Curves


class CurveSample(TypedDict, total=False):
    t: float
    point: list[float]
    tangent: list[float] | None
    normal: list[float] | None  # principal normal: the side the curve bends to
    curvature: float | None
    radius: float | None


class CurveData(TypedDict, total=False):
    curve: result.Row
    length: float
    samples: list[CurveSample]
    inflections: list[float]  # parameters where the bending side flips
    spline: dict[str, Any]


def _sample(tx, obj, t) -> CurveSample:
    with native():
        data = [float(v) for v in tx.uf.ModlGeneral.EvaluateCurve(obj.Tag, float(t), 2)]
    point, first, second = data[:3], data[3:6], data[6:9]
    speed = geom.norm(first)
    if speed <= 1e-12:
        return {"t": t, "point": point, "tangent": None, "normal": None, "curvature": None,
                "radius": None}  # fmt: skip
    curvature = geom.norm(geom.cross(first, second)) / speed**3
    radius = 1.0 / curvature if curvature > 1e-12 else None
    if radius is not None and radius >= common.FLAT_RADIUS:
        curvature, radius = 0.0, None
    tangent = [v / speed for v in first]
    bend = geom.sub(second, geom.scale(tangent, geom.dot(second, tangent)))
    return {
        "t": t,
        "point": point,
        "tangent": tangent,
        "normal": common.unit_or_none(bend) if radius is not None else None,
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
        return {"t": t, "point": point, "tangent": unit, "normal": None, "curvature": None,
                "radius": None}  # fmt: skip
    curvature = geom.norm(geom.sub(u1, u0)) / chord
    radius = 1.0 / curvature if curvature > 1e-12 else None
    if radius is None or radius >= common.FLAT_RADIUS:
        curvature, radius = 0.0, None
    normal = common.unit_or_none(geom.sub(u1, u0)) if radius is not None else None
    return {"t": t, "point": point, "tangent": unit, "normal": normal, "curvature": curvature,
            "radius": radius}  # fmt: skip


def _inflections(rows) -> list[float]:
    """Parameters (midway between samples) where the principal normal turns over: the
    curve's bending side flips (an S bend)."""
    out = []
    for a, b in zip(rows, rows[1:], strict=False):
        na, nb = a.get("normal"), b.get("normal")
        if na is not None and nb is not None and geom.dot(na, nb) < 0:
            out.append((a["t"] + b["t"]) / 2)
    return out


def _single_curve(tx, feature, ref):
    """The one curve a feature made (a spline or line feature), else ``NX_WRONG_KIND``."""
    try:
        with native():
            entities = list(feature.GetEntities() or [])
    except NXError:
        entities = []
    curves = [e for e in entities if common.kind_of(tx, e) == "curve"]
    if len(curves) != 1:
        fail(
            "NX_WRONG_KIND",
            f"{ref} is a feature with {len(curves)} curves; pass one curve or edge",
            field="/curve",
            hint="List its curves with nx_list_objects and pass one curve id",
        )
    return curves[0]


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
    curve: Annotated[str, RefTo("curve", "edge", "feature", doc="Curve, edge or 1-curve feature")],
    samples: Annotated[int, Range(ge=2, le=1000)] = 11,
) -> CurveData:
    """Sample points, unit tangents, principal normals and curvature along a curve or edge at
    even parameters 0..1. Adds length and inflections (normal flips); edge curvature is a
    tangent-difference estimate; splines add degree, knots and poles."""
    obj = select.one(tx, curve, "curve", "edge", "feature", field="/curve")
    if common.kind_of(tx, obj) == "feature":
        obj = _single_curve(tx, obj, curve)
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
    data: CurveData = {"curve": tx.ref(obj, kind=kind)}
    with contextlib.suppress(NXError):  # a curve NX cannot measure keeps its samples
        data["length"] = measure.length(tx, obj)
    data["samples"] = rows
    data["inflections"] = _inflections(rows)
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
APART = 10  # a gap over this many position tolerances: the two do not meet (no G1/G2)
MODE_DOC = "auto: surface for two sheet boundary edges, else curve"


class Junction(TypedDict):
    point: list[float]
    first_end: str
    second_end: str


class _ContinuityRequired(TypedDict):
    mode: str
    g0: bool
    g1: bool | None
    g2: bool | None
    gap: float
    angle: float | None
    curvature: float | None
    samples: int
    unresolved: int
    tolerances: dict[str, float]


class ContinuityData(_ContinuityRequired, total=False):
    junction: Junction


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
            f"{ref} is an edge of {len(faces)} faces; surface continuity needs an open "
            "boundary edge of a sheet body (one face), not a solid edge or a curve",
            field=field,
            hint='Use mode "curve" to compare the edges at their shared end, or make sheets '
            "with nx_create_extract (faces) or nx_create_loft (solid false) and pick their "
            "boundary edges with nx_find_geometry",
            details={"faces": len(faces)},
        )
    return faces[0]


def _one_face(obj, kind):
    """The face of a sheet boundary edge, else None (a curve, a solid or inner edge)."""
    if kind != "edge":
        return None
    faces = list(obj.GetFaces())
    return faces[0] if len(faces) == 1 else None


def _surface_mode(tx, objs, refs, samples):
    faces = [
        _boundary_face(o, ref, f)
        for o, ref, f in zip(objs, refs, ("/first", "/second"), strict=True)
    ]
    reports = []
    for side in (0, 1):
        tx.checkpoint(side, 2)
        reports.append(
            surface.edge_continuity(
                tx, objs[side], faces[side], objs[1 - side], faces[1 - side], samples
            )
        )
    unresolved = sum(r["unresolved"] for r in reports)
    angles = [r["angle"] for r in reports]
    complete = unresolved == 0 and None not in angles
    return {
        "gap": max(r["gap"] for r in reports),
        # angle and curvature are both set exactly when complete (kit.surface.edge_continuity)
        "angle": max(angles) if complete else None,
        "curvature": max(r["curvature"] for r in reports) if complete else None,
        "samples": 2 * samples,
        "unresolved": unresolved,
    }


def _curve_end(tx, obj, at):
    """``(point, unit tangent into the curve, curvature vector)`` at end ``at`` (0 start,
    1 end); tangent and curvature are None where they do not evaluate. The curvature
    vector dT/ds does not depend on the direction of travel; it is the change of the unit
    tangent over ``EDGE_STEP`` divided by the chord (exact magnitude on arcs)."""
    inner = EDGE_STEP if at == 0 else 1.0 - EDGE_STEP
    with kcurve.evaluator(tx, obj) as e:
        (point, tangent), (near, near_tangent) = e.at(float(at)), e.at(inner)
    unit, other = common.unit_or_none(tangent), common.unit_or_none(near_tangent)
    chord = geom.norm(geom.sub(near, point))
    if unit is None or other is None or chord <= 1e-12:
        return point, None, None
    earlier, later = (unit, other) if at == 0 else (other, unit)
    bend = geom.scale(geom.sub(later, earlier), 1.0 / chord)
    if geom.norm(bend) * common.FLAT_RADIUS < 1:
        bend = [0.0, 0.0, 0.0]
    return point, (unit if at == 0 else geom.scale(unit, -1.0)), bend


def _curve_mode(tx, objs):
    """Continuity at the nearest pair of ends of two curves or edges."""
    ends = [[_curve_end(tx, o, at) for at in (0, 1)] for o in objs]
    tx.checkpoint()
    best = None  # (gap, first end, second end): end-to-start first on a tie
    for a, b in ((1, 0), (0, 0), (1, 1), (0, 1)):
        gap = geom.dist(ends[0][a][0], ends[1][b][0])
        if best is None or gap < best[0] - 1e-12:
            best = (gap, a, b)
    assert best is not None
    gap, a, b = best
    (point, ta, ka), (_p, tb, kb) = ends[0][a], ends[1][b]
    angle = curvature = None
    if ta is not None and tb is not None:
        # smooth: the two curves leave the junction in opposite directions
        angle = math.degrees(math.acos(max(-1.0, min(1.0, -geom.dot(ta, tb)))))
        curvature = geom.norm(geom.sub(ka, kb))
    names = ("start", "end")
    return {
        "gap": gap,
        "angle": angle,
        "curvature": curvature,
        "samples": 2,
        "unresolved": 0 if angle is not None else 1,
        "junction": {"point": point, "first_end": names[a], "second_end": names[b]},
    }


CurveRef = Annotated[str, RefTo("edge", "curve", doc="Edge or curve 'e:21'/'c:7'")]


@tool(
    effect="read",
    label="Check continuity",
    summary="Continuity {first} to {second}",
    keywords=(
        "g0 g1 g2",
        "tangency",
        "curvature continuity",
        "surface continuity",
        "gap",
        "curve continuity",
    ),
    requires=("work_part",),
    cost="light",
    example={"first": "e:21", "second": "e:34", "tolerances": {"position": "0.01 mm"}},
)
def nx_check_continuity(
    tx: Tx,
    *,
    first: CurveRef,
    second: CurveRef,
    mode: Annotated[Literal["auto", "surface", "curve"], Doc(MODE_DOC)] = "auto",
    tolerances: Tolerances | None = None,
    samples: Annotated[int, Range(ge=2, le=200), Doc("Surface: per edge; counts both")] = 20,
) -> ContinuityData:
    """Check G0/G1/G2 continuity of two edges or curves. Surface mode samples two sheet
    boundary edges along their length; curve mode compares them at their nearest ends
    (junction). G1/G2 are null when a sample cannot be evaluated or the two do not meet."""
    limits = _limits(tolerances)
    objs, kinds = [], []
    for ref, f in ((first, "/first"), (second, "/second")):
        obj = select.one(tx, ref, "edge", "curve", field=f)
        objs.append(obj)
        kinds.append("edge" if common.kind_of(tx, obj) == "edge" else "curve")
    if int(objs[0].Tag) == int(objs[1].Tag):
        fail("NX_INVALID_ARGUMENT", "Select two different edges or curves", field="/second")
    if mode == "auto":
        faces = [_one_face(o, k) for o, k in zip(objs, kinds, strict=True)]
        sheet = faces[0] is not None and faces[1] is not None
        mode = "surface" if sheet and int(faces[0].Tag) != int(faces[1].Tag) else "curve"
    if mode == "surface":
        for k, f in zip(kinds, ("/first", "/second"), strict=True):
            if k != "edge":
                fail("NX_WRONG_GEOMETRY", "surface mode compares sheet boundary edges, not "
                     "curves", field=f, hint='Use mode "curve"')  # fmt: skip
        found = _surface_mode(tx, objs, (first, second), samples)
    else:
        found = _curve_mode(tx, objs)
    gap, angle, curvature = found["gap"], found["angle"], found["curvature"]
    g0 = gap <= limits["position"]
    apart = gap > APART * limits["position"]
    g1 = (g0 and angle <= limits["angle"]) if angle is not None and not apart else None
    g2 = (g1 and curvature <= limits["curvature"]) if curvature is not None and not apart else None
    if apart:
        tx.warn("NX_NOTE", f"The {'edges' if mode == 'surface' else 'curves'} do not meet (gap "
                f"{gap:.6g}, over {APART}x the position tolerance): g1 and g2 are null")  # fmt: skip
    out: ContinuityData = {
        "mode": mode,
        "g0": g0,
        "g1": g1,
        "g2": g2,
        "gap": gap,
        "angle": angle,
        "curvature": curvature,
        "samples": found["samples"],
        "unresolved": found["unresolved"],
        "tolerances": limits,
    }
    if "junction" in found:
        out["junction"] = found["junction"]
    return out
