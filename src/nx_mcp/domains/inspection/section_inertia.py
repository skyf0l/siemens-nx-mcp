"""``nx_measure_section_inertia``: area, centroid, second moments, principal axes and
section moduli of the section where a plane cuts bodies (keel, stringer, rudder stock),
read without changing the model.

The cut and its 2D frame are ``nx_measure_section``'s: the same plane, offset, origin and
normal, UF ``SectionFromPlanes`` curves under ``tx.scratch`` (undone in the call), chained
into loops; a loop inside an odd number of loops of the same body is a hole. Open chains
are left out with a warning. The integrals are Green's theorem with 5-point
Gauss-Legendre quadrature per line, per arc piece of at most 22.5 degrees and per span of
a 64-point polyline (other curves): exact for lines, to rounding for arcs. They run about
the centre of the section's box, then move to the centroid.

``i_u`` is the second moment about the centroidal axis parallel to frame u (the integral
of (v - v_c)^2 dA: bending about u), ``i_v`` about the axis parallel to v, ``i_uv`` the
product (integral of (u - u_c)(v - v_c) dA). Principal ``angle`` turns u towards v onto
the axis of ``i_max``. ``extreme_v`` holds the distances from the centroid to the farthest
material on the -v and +v sides (exact for lines and arcs); ``modulus_u`` is ``i_u`` over
the larger one (elastic section modulus for bending about u).

NX cross-check: ``Session.Measurement.GetSectionInertia`` (license None, NX 2212+) on
the same curves, when the host has it: its area and principal moments are returned as
``nx`` and a difference over 1e-6 relative warns.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Point3, Quantity, Vec3
from nx_mcp.domains.inspection import common, section
from nx_mcp.kit import geom
from nx_mcp.kit import units as kunits

# 5-point Gauss-Legendre on [-1, 1]: exact for polynomials of degree 9.
_A, _B = math.sqrt(5 - 2 * math.sqrt(10 / 7)) / 3, math.sqrt(5 + 2 * math.sqrt(10 / 7)) / 3
_WA, _WB = (322 + 13 * math.sqrt(70)) / 900, (322 - 13 * math.sqrt(70)) / 900
GAUSS = ((0.0, 128 / 225), (-_A, _WA), (_A, _WA), (-_B, _WB), (_B, _WB))
ARC_PIECE = math.pi / 8  # the largest arc piece one quadrature covers
CROSS_CHECK = 1e-6  # relative difference to NX's own section inertia that warns
FORMS = "{plane:'yz', offset:500} or {origin:[0,0,5], normal:[0,0,1]}"
# GetSectionInertia units: point, area moment, length, area (names probed on NX 2406).
UNIT_NAMES = {
    "mm": ("MilliMeter", "MilliMeterFourth", "MilliMeter", "SquareMilliMeter"),
    "in": ("Inch", "InchFourth", "Inch", "SquareInch"),
}


class Principal(TypedDict):
    angle: float
    i_max: float
    i_min: float
    axis_max: list[float]
    axis_min: list[float]
    modulus_max: float | None
    modulus_min: float | None


class NxCheck(TypedDict):
    area: float
    i_max: float
    i_min: float


class _InertiaRequired(TypedDict):
    frame: section.Frame
    area: float
    loops: int
    open_chains: int


class InertiaData(_InertiaRequired, total=False):
    centroid: list[float]
    centroid_uv: list[float]
    i_u: float
    i_v: float
    i_uv: float
    polar: float
    principal: Principal
    gyration_u: float
    gyration_v: float
    extreme_u: list[float]
    extreme_v: list[float]
    modulus_u: float | None
    modulus_v: float | None
    nx: NxCheck


# ---------------------------------------------------------------------------
# Green's theorem on 2D segments (nx_measure_section's line / arc / curve rows)


def _pieces(seg, ref):
    """``[(point(s), derivative(s))]`` callables over s in -1..1 covering ``seg``, in
    coordinates relative to ``ref``."""
    ru, rv = ref
    if seg["type"] == "arc":
        (cu, cv), r = seg["center"], seg["radius"]
        cu, cv = cu - ru, cv - rv
        n = max(1, math.ceil(abs(seg["sweep"]) / ARC_PIECE))
        step = seg["sweep"] / n
        out = []
        for k in range(n):
            mid, half = seg["a0"] + step * (k + 0.5), step / 2

            def at(s, mid=mid, half=half):
                a = mid + half * s
                return (cu + r * math.cos(a), cv + r * math.sin(a)), (
                    -r * math.sin(a) * half,
                    r * math.cos(a) * half,
                )

            out.append(at)
        return out
    points = seg["points"] if seg["type"] == "curve" else [seg["start"], seg["end"]]
    out = []
    for (u0, v0), (u1, v1) in zip(points, points[1:], strict=False):
        a, b = (u0 - ru, v0 - rv), (u1 - ru, v1 - rv)

        def at(s, a=a, b=b):
            t = (s + 1) / 2
            du, dv = (b[0] - a[0]) / 2, (b[1] - a[1]) / 2
            return (a[0] + 2 * t * du, a[1] + 2 * t * dv), (du, dv)

        out.append(at)
    return out


def _integrals(loop, ref) -> list[float]:
    """Signed ``[A, Su, Sv, Iuu, Ivv, Iuv]`` of a closed loop about ``ref``: the area,
    first moments (integrals of u, v dA) and the integrals of v^2, u^2 and uv dA;
    positive for a counter-clockwise loop."""
    total = [0.0] * 6
    for seg in loop:
        for at in _pieces(seg, ref):
            for s, w in GAUSS:
                (u, v), (du, dv) = at(s)
                total[0] += w * (u * dv - v * du) / 2
                total[1] += w * u * u * dv / 2
                total[2] -= w * v * v * du / 2
                total[3] -= w * v**3 * du / 3
                total[4] += w * u**3 * dv / 3
                total[5] += w * u * u * v * dv / 2
    return total


def _extent(loops, centre, direction) -> list[float]:
    """``[most negative, most positive]`` of (p - centre) . direction over the loops; exact
    for lines and arcs (an arc adds the points where its tangent is normal to direction)."""
    du, dv = direction
    values = []
    for loop in loops:
        for seg in loop:
            points = list(seg.get("points") or [seg["start"], seg["end"]])
            if seg["type"] == "arc":
                (cu, cv), r = seg["center"], seg["radius"]
                for angle in (math.atan2(dv, du), math.atan2(-dv, -du)):
                    if section._in_sweep(seg, angle) is not None:
                        points.append([cu + r * math.cos(angle), cv + r * math.sin(angle)])
            values += [(p[0] - centre[0]) * du + (p[1] - centre[1]) * dv for p in points]
    return [min(values), max(values)]


def _modulus(moment, extent) -> float | None:
    far = max(abs(extent[0]), abs(extent[1]))
    return moment / far if far > 0 else None


def properties(loops: list[list[dict]], holes: list[bool]) -> dict:
    """Section properties of closed 2D loops (``holes[i]``: loop i is a hole)."""
    points = [p for loop in loops for seg in loop for p in (seg["start"], seg["end"])]
    ref = [
        (min(p[0] for p in points) + max(p[0] for p in points)) / 2,
        (min(p[1] for p in points) + max(p[1] for p in points)) / 2,
    ]
    total = [0.0] * 6
    for loop, hole in zip(loops, holes, strict=True):
        values = _integrals(loop, ref)
        sign = (-1.0 if values[0] < 0 else 1.0) * (-1.0 if hole else 1.0)
        total = [t + sign * v for t, v in zip(total, values, strict=True)]
    area, su, sv, iuu, ivv, iuv = total
    if area <= 0:
        fail(
            "NX_WRONG_GEOMETRY",
            f"The section's material area is {area:g}: no closed material loops",
            field="/bodies",
            hint="Cut through solid bodies; nx_measure_section shows the loops",
        )
    uc, vc = su / area, sv / area
    i_u, i_v, i_uv = iuu - area * vc * vc, ivv - area * uc * uc, iuv - area * uc * vc
    mean, radius = (i_u + i_v) / 2, math.hypot((i_u - i_v) / 2, i_uv)
    angle = 0.5 * math.atan2(-2 * i_uv, i_u - i_v) if radius > 1e-12 * mean else 0.0
    centre = [0.0, 0.0]  # moments about the centroid in ref coordinates
    shifted = [
        [{**s, **_shift(s, ref, uc, vc)} for s in loop] for loop in loops
    ]  # coordinates relative to the centroid
    major = (math.cos(angle), math.sin(angle))
    minor = (-math.sin(angle), math.cos(angle))
    extreme_u = _extent(shifted, centre, (1.0, 0.0))
    extreme_v = _extent(shifted, centre, (0.0, 1.0))
    return {
        "area": area,
        "centroid_uv": [ref[0] + uc, ref[1] + vc],
        "i_u": i_u,
        "i_v": i_v,
        "i_uv": i_uv,
        "polar": i_u + i_v,
        "angle": angle,
        "i_max": mean + radius,
        "i_min": mean - radius,
        # bending about the major axis: distances along the minor direction, and back
        "modulus_max": _modulus(mean + radius, _extent(shifted, centre, minor)),
        "modulus_min": _modulus(mean - radius, _extent(shifted, centre, major)),
        "extreme_u": [-extreme_u[0], extreme_u[1]],
        "extreme_v": [-extreme_v[0], extreme_v[1]],
        "modulus_u": _modulus(i_u, extreme_v),
        "modulus_v": _modulus(i_v, extreme_u),
        "gyration_u": math.sqrt(max(i_u, 0.0) / area),
        "gyration_v": math.sqrt(max(i_v, 0.0) / area),
    }


def _shift(seg, ref, uc, vc) -> dict:
    """The coordinates of ``seg`` relative to the centroid (``ref`` + (uc, vc))."""
    ou, ov = ref[0] + uc, ref[1] + vc

    def move(p):
        return [p[0] - ou, p[1] - ov]

    out: dict[str, Any] = {"start": move(seg["start"]), "end": move(seg["end"])}
    if seg["type"] == "arc":
        out["center"] = move(seg["center"])
    elif seg["type"] == "curve":
        out["points"] = [move(p) for p in seg["points"]]
    return out


# ---------------------------------------------------------------------------
# NX cross-check


def _nx_check(tx, curves) -> NxCheck | None:
    """Area and principal moments from NX's ``Measurement.GetSectionInertia``, or None
    when the host has no such call or it refuses these curves."""
    measurement = getattr(tx.session, "Measurement", None)
    method = getattr(measurement, "GetSectionInertia", None)
    if method is None or not curves:
        return None
    try:
        collection = tx.part.UnitCollection
        units = [collection.FindObject(n) for n in UNIT_NAMES[kunits.part_units(tx)]]
        values = method(curves, units, True, 1.0, False)
        _centre, i_max, i_min, _b, _c, _length, area, _rect = values
        return {"area": float(area), "i_max": float(i_max), "i_min": float(i_min)}
    except Exception:  # only a cross-check: the computed values stand without it
        return None


# ---------------------------------------------------------------------------
# The tool


def _summary(a, say):
    if a.get("plane"):
        return f"Section inertia on {a['plane']}" + (
            f" at {a['offset']}" if a.get("offset") else ""
        )
    return "Section inertia on a plane"


@tool(
    target="assembly",
    effect="read",
    label="Measure section inertia",
    summary=_summary,
    keywords=(
        "section inertia",
        "second moment of area",
        "section modulus",
        "centroid",
        "principal axes",
        "moment of inertia",
    ),
    requires=("work_part",),
    cost="heavy",
    example={"plane": "yz", "offset": 500},
)
def nx_measure_section_inertia(
    tx: Tx,
    *,
    plane: Literal["xy", "yz", "xz"] | None = None,
    offset: Annotated[
        float | str, Quantity("length"), Doc("Along z, x, y for xy, yz, xz; or '5 mm'")
    ] = 0.0,
    origin: Point3 | None = None,
    normal: Vec3 | None = None,
    bodies: section.Targets | None = None,
) -> InertiaData:
    """Measure area, centroid, second moments, principal axes and section moduli of a plane
    cut. Cut: plane (+ offset) or normal (+ origin); bodies default to all; model unchanged.
    Frame: xy u=X v=Y, yz u=Y v=Z, xz u=X v=Z; normal: u = X projected (Y if normal near X).
    i_u, i_v: about centroidal axes along u, v; modulus_u = i_u / farthest fibre along v.
    Part units (moments length^4)."""
    common.note_exploded(tx)
    if (plane is None) == (normal is None):
        fail(
            "NX_INVALID_ARGUMENT",
            "Give the cut: one of plane or normal"
            + ("" if plane is None else "; they are mutually exclusive"),
            field="/plane" if plane is None else "/normal",
            hint="e.g. " + FORMS,
        )
    offset = float(offset)
    if offset and plane is None:
        fail(
            "NX_INVALID_ARGUMENT",
            "offset goes with plane; with normal, move origin instead",
            field="/offset",
            hint="e.g. " + FORMS,
        )
    frame = section._frame(plane, offset, origin, normal)
    with tx.checks():
        found = common.target_bodies(tx, bodies, "part")
    tx.frame = "work_part_absolute"
    cut: list[list[dict]] = []
    nx_values = None
    with tx.scratch(), native():
        curves: list = []
        for body in found:
            tx.checkpoint()
            segments, objects = _cut_with_curves(tx, frame, body)
            cut.append(segments)
            curves += objects
        nx_values = _nx_check(tx, curves)
    scale = max([1.0] + [abs(c) for segs in cut for s in segs for c in s["start"]])
    tol = 1e-6 * scale
    loops, holes, open_chains = [], [], 0
    for segments in cut:
        chained = section._chain(segments, tol)
        closed = [segs for segs, ok in chained if ok]
        open_chains += len(chained) - len(closed)
        polygons = [section._polygon(segs) for segs in closed]
        for i, segs in enumerate(closed):
            depth = sum(
                1
                for j, poly in enumerate(polygons)
                if j != i and section._inside(segs[0]["start"], poly)
            )
            loops.append(segs)
            holes.append(depth % 2 == 1)
    out: InertiaData = {
        "frame": frame,
        "area": 0.0,
        "loops": len(loops),
        "open_chains": open_chains,
    }
    if open_chains:
        tx.warn(
            "NX_UPDATE_WARNING",
            f"{open_chains} open section chains are left out (sheet bodies or gaps)",
        )
    if not loops:
        tx.warn(
            "NX_UPDATE_WARNING",
            "The plane does not cut the bodies: no loops" + section._span(tx, frame, plane, found),
        )
        return out
    p = properties(loops, holes)
    u, v, angle = frame["u"], frame["v"], p["angle"]
    centre = [frame["origin"][k] + p["centroid_uv"][0] * u[k] + p["centroid_uv"][1] * v[k]
              for k in range(3)]  # fmt: skip
    axis_max = geom.add(geom.scale(u, math.cos(angle)), geom.scale(v, math.sin(angle)))
    principal: Principal = {
        "angle": math.degrees(angle),
        "i_max": p["i_max"],
        "i_min": p["i_min"],
        "axis_max": axis_max,
        "axis_min": geom.cross(frame["normal"], axis_max),
        "modulus_max": p["modulus_max"],
        "modulus_min": p["modulus_min"],
    }
    out["area"], out["centroid"], out["principal"] = p["area"], centre, principal
    for key in ("centroid_uv", "i_u", "i_v", "i_uv", "polar", "gyration_u", "gyration_v",
                "extreme_u", "extreme_v", "modulus_u", "modulus_v"):  # fmt: skip
        out[key] = p[key]
    if nx_values is not None:
        out["nx"] = nx_values
        mine = {"area": p["area"], "i_max": p["i_max"], "i_min": p["i_min"]}
        for key in mine:
            theirs = float(nx_values[key])  # type: ignore[literal-required]
            if abs(mine[key] - theirs) > CROSS_CHECK * max(abs(mine[key]), abs(theirs), 1e-12):
                tx.warn(
                    "NX_UPDATE_WARNING",
                    f"NX's own section inertia gives {key} {theirs:g}, computed {mine[key]:g}",
                )
    return out


def _cut_with_curves(tx, frame, body) -> tuple[list[dict], list]:
    """``section._cut`` that also returns the temporary NX curves."""
    uf = tx.uf
    general = uf.Curve.SectionGeneralData()
    general.Associate = 0
    general.Grouping = 0
    general.JoinType = 0
    general.Tolerance = section.SECTION_TOLERANCE
    general.Objects = [body.Tag]
    general.NumObjects = 1
    planes = uf.Curve.SectionPlanesData()
    planes.Planes = [uf.Modeling.CreatePlane(frame["origin"], frame["normal"])]
    planes.NumPlanes = 1
    try:
        group = uf.Curve.SectionFromPlanes(general, planes)
    except Exception as error:  # NX 2406: a plane that misses the body raises
        if "no section curves" in str(error).lower():
            return [], []
        raise
    members = uf.Group.AskGroupData(group)
    tags = members[0] if isinstance(members, tuple) else members
    manager = tx.nx.TaggedObjectManager
    objects = [manager.GetTaggedObject(t) for t in list(tags or [])]
    return [section._segment(tx, frame, obj) for obj in objects], objects
