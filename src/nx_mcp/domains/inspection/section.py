"""``nx_measure_section``: the profile where a plane cuts bodies, read without changing
the model (tool-surface 3.9).

The plane is a principal plane with an offset or a point and normal. Its 2D frame
(``frame``): principal planes keep world axes, xy (u=X, v=Y), yz (u=Y, v=Z), xz (u=X,
v=Z), so a body revolved about Z cut by xz reads as (radius, height); a general plane
takes u along world X projected into it (world Y when the normal is near X) and
v = normal x u. Local coordinates are measured from the plane's origin.

Each body is cut alone (UF ``SectionFromPlanes``, unassociated curves in a group) on a
temporary UF plane; everything is made under ``tx.scratch`` and undone in the same call,
which also checks that the object count is back. NX 2406 measured: a plane that
misses a body raises "No section curves created." (no loops for that body); the section curves
of a revolved nested-rectangle profile are 20 unordered, arbitrarily directed lines that
vanish with the undo. Curves are chained end to end into loops; a loop's area is exact
for lines and arcs (Green's theorem) and polygonal (64 samples) for other curves.
Loops inside an odd number of other loops of the same body are holes; ``area`` is
material area (outer minus holes).

``radial_axis`` reads an axisymmetric profile: the cut plane contains that world axis
through ``origin`` (z: xz plane, x and y: xy plane) and each axial station gives the
material spans on the positive radial side (exact for lines and arcs).
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Point3, Quantity, Range, RefTo, Vec3
from nx_mcp.domains.inspection import common
from nx_mcp.kit import curve, geom, measure

TAU = 2 * math.pi
SAMPLES = 64  # polygon points of a curve that is neither a line nor an arc
SECTION_TOLERANCE = 0.001  # UF section curve fit tolerance, part units
MAX_LOOPS = 2000
PRINCIPAL = {  # plane -> (u axis, v axis, index of the offset axis)
    "xy": ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], 2),
    "yz": ([0.0, 1.0, 0.0], [0.0, 0.0, 1.0], 0),
    "xz": ([1.0, 0.0, 0.0], [0.0, 0.0, 1.0], 1),
}
RADIAL = {"z": ("xz", 1, 0), "x": ("xy", 0, 1), "y": ("xy", 1, 0)}  # plane, axial, radial
FORMS = "{plane:'xy', offset:5}, {origin:[0,0,5], normal:[0,0,1]} or {radial_axis:'z', at:[10]}"

Targets = Annotated[list[Annotated[str, RefTo("body", "feature", "component")]], Items(1, 100)]


class Frame(TypedDict):
    origin: list[float]
    u: list[float]
    v: list[float]
    normal: list[float]


class Segment(TypedDict, total=False):
    type: str
    start: list[float]
    end: list[float]
    center: list[float]
    radius: float


class Loop(TypedDict, total=False):
    body: str
    closed: bool
    role: str
    area: float | None
    min: list[float]
    max: list[float]
    segments: list[Segment]


class Station(TypedDict):
    at: float
    min: float | None
    max: float | None
    spans: list[list[float]]


class SectionData(TypedDict, total=False):
    frame: Frame
    area: float
    loops: list[Loop]
    profile: list[Station]


# ---------------------------------------------------------------------------
# Plane and curves


def _frame(plane, offset, origin, normal) -> Frame:
    if plane is not None:
        u, v, index = PRINCIPAL[plane]
        at = [float(c) for c in origin] if origin is not None else [0.0, 0.0, 0.0]
        at[index] += float(offset)
        return {"origin": at, "u": u, "v": v, "normal": geom.cross(u, v)}
    n = geom.unit(normal, "/normal")
    seed = [0.0, 1.0, 0.0] if abs(n[0]) > 0.9 else [1.0, 0.0, 0.0]
    u = geom.unit(geom.sub(seed, geom.scale(n, geom.dot(seed, n))))
    at = [float(c) for c in origin] if origin is not None else [0.0, 0.0, 0.0]
    return {"origin": at, "u": u, "v": geom.cross(n, u), "normal": n}


def _local(frame, p) -> list[float]:
    d = geom.sub(p, frame["origin"])
    return [geom.dot(d, frame["u"]), geom.dot(d, frame["v"])]


def _segment(tx, frame, obj) -> dict:
    """One section curve in plane-local 2D: line, arc (with signed sweep) or polyline."""
    with curve.evaluator(tx, obj) as e:
        start, end = _local(frame, e.at(0.0)[0]), _local(frame, e.at(1.0)[0])
        if e.ev.IsLine(e.handle):
            return {"type": "line", "start": start, "end": end}
        if e.ev.IsArc(e.handle):
            arc = e.ev.AskArc(e.handle)
            center, radius = _local(frame, common.xyz3(arc.Center)), float(arc.Radius)
            quarter = _local(frame, e.at(0.25)[0])
            a0 = math.atan2(start[1] - center[1], start[0] - center[0])
            a1 = math.atan2(end[1] - center[1], end[0] - center[0])
            aq = math.atan2(quarter[1] - center[1], quarter[0] - center[0])
            ccw = (a1 - a0) % TAU
            if math.dist(start, end) <= 1e-9 * max(1.0, radius):
                ccw = TAU  # a full circle
            sweep = ccw if (aq - a0) % TAU < ccw else ccw - TAU
            return {
                "type": "arc",
                "start": start,
                "end": end,
                "center": center,
                "radius": radius,
                "a0": a0,
                "sweep": sweep,
            }
        points = [_local(frame, e.at(i / SAMPLES)[0]) for i in range(SAMPLES + 1)]
        return {"type": "curve", "start": start, "end": end, "points": points}


def _reverse(seg: dict) -> dict:
    out = {**seg, "start": seg["end"], "end": seg["start"]}
    if seg["type"] == "arc":
        out.update(a0=seg["a0"] + seg["sweep"], sweep=-seg["sweep"])
    elif seg["type"] == "curve":
        out["points"] = list(reversed(seg["points"]))
    return out


def _chain(segments: list[dict], tol: float) -> list[tuple[list[dict], bool]]:
    """``[(segments in order, closed)]``: curves joined end to end."""
    left = list(segments)
    loops = []
    while left:
        loop = [left.pop(0)]
        while math.dist(loop[-1]["end"], loop[0]["start"]) > tol:
            tail = loop[-1]["end"]
            found = None
            for i, seg in enumerate(left):
                if math.dist(seg["start"], tail) <= tol:
                    found = left.pop(i)
                    break
                if math.dist(seg["end"], tail) <= tol:
                    found = _reverse(left.pop(i))
                    break
            if found is None:
                head = loop[0]["start"]  # grow the open chain backwards too
                for i, seg in enumerate(left):
                    if math.dist(seg["end"], head) <= tol:
                        loop.insert(0, left.pop(i))
                        break
                    if math.dist(seg["start"], head) <= tol:
                        loop.insert(0, _reverse(left.pop(i)))
                        break
                else:
                    break
                continue
            loop.append(found)
        loops.append((loop, math.dist(loop[-1]["end"], loop[0]["start"]) <= tol))
    return loops


# ---------------------------------------------------------------------------
# 2D geometry of loops


def _arc_point(seg, angle) -> list[float]:
    c, r = seg["center"], seg["radius"]
    return [c[0] + r * math.cos(angle), c[1] + r * math.sin(angle)]


def _in_sweep(seg, angle) -> float | None:
    """Fraction 0..1 of the arc's sweep at ``angle``, None when off the arc."""
    sweep = seg["sweep"]
    delta = (angle - seg["a0"]) % TAU if sweep > 0 else (seg["a0"] - angle) % TAU
    return delta / abs(sweep) if delta <= abs(sweep) + 1e-12 else None


def _signed_area(loop) -> float:
    total = 0.0
    for seg in loop:
        (u0, v0), (u1, v1) = seg["start"], seg["end"]
        if seg["type"] == "arc":
            c, r = seg["center"], seg["radius"]
            total += r * r * seg["sweep"] + c[0] * (v1 - v0) - c[1] * (u1 - u0)
        elif seg["type"] == "curve":
            p = seg["points"]
            total += sum(p[i][0] * p[i + 1][1] - p[i + 1][0] * p[i][1] for i in range(len(p) - 1))
        else:
            total += u0 * v1 - u1 * v0
    return total / 2


def _polygon(loop) -> list[list[float]]:
    out: list[list[float]] = []
    for seg in loop:
        if seg["type"] == "arc":
            n = max(2, int(abs(seg["sweep"]) / TAU * SAMPLES))
            out += [_arc_point(seg, seg["a0"] + seg["sweep"] * i / n) for i in range(n)]
        elif seg["type"] == "curve":
            out += seg["points"][:-1]
        else:
            out.append(seg["start"])
    return out


def _box(loop) -> tuple[list[float], list[float]]:
    points = [seg["start"] for seg in loop] + [seg["end"] for seg in loop]
    for seg in loop:
        if seg["type"] == "arc":
            points += [
                _arc_point(seg, k * math.pi / 2)
                for k in range(4)
                if _in_sweep(seg, k * math.pi / 2) is not None
            ]
        elif seg["type"] == "curve":
            points += seg["points"]
    return (
        [min(p[0] for p in points), min(p[1] for p in points)],
        [max(p[0] for p in points), max(p[1] for p in points)],
    )


def _inside(point, polygon) -> bool:
    x, y = point
    inside = False
    for i in range(len(polygon)):
        (x0, y0), (x1, y1) = polygon[i - 1], polygon[i]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def _crossings(loop, axial: int, at: float) -> list[float]:
    """Radial coordinates where a closed loop crosses the station line axial == at."""
    radial = 1 - axial
    out = []
    for seg in loop:
        if seg["type"] == "arc":
            c, r = seg["center"], seg["radius"]
            d = at - c[axial]
            if abs(d) >= r:
                continue
            h = math.sqrt(r * r - d * d)
            for sign in (1.0, -1.0):
                p = [0.0, 0.0]
                p[axial], p[radial] = at, c[radial] + sign * h
                fraction = _in_sweep(seg, math.atan2(p[1] - c[1], p[0] - c[0]))
                if fraction is not None and fraction < 1.0:  # half-open: [start, end)
                    out.append(p[radial])
            continue
        points = seg["points"] if seg["type"] == "curve" else [seg["start"], seg["end"]]
        for a, b in zip(points, points[1:], strict=False):
            if (a[axial] <= at < b[axial]) or (b[axial] <= at < a[axial]):
                t = (at - a[axial]) / (b[axial] - a[axial])
                out.append(a[radial] + t * (b[radial] - a[radial]))
    return out


def _station(loops, axial: int, at: float) -> Station:
    values = sorted(x for loop in loops for x in _crossings(loop, axial, at))
    spans = []
    for lo, hi in zip(values[0::2], values[1::2], strict=False):
        lo = max(lo, 0.0)  # the positive radial side only
        if hi > lo:
            spans.append([lo, hi])
    return {
        "at": at,
        "min": spans[0][0] if spans else None,
        "max": spans[-1][1] if spans else None,
        "spans": spans,
    }


def _stations(loops, axial: int, at, count: int, tol: float) -> list[float]:
    if at is not None:
        wanted = [float(t) for t in at]
    else:
        values = [p[axial] for loop in loops for p in _polygon(loop)]
        if not values:
            return []
        lo, hi = min(values), max(values)
        wanted = [lo + (i + 0.5) * (hi - lo) / count for i in range(count)]
    vertices = {seg["start"][axial] for loop in loops for seg in loop}
    nudged = []
    for t in wanted:  # a station through a vertex would count it twice: move it off
        nudged.append(t + 10 * tol if any(abs(t - v) <= tol for v in vertices) else t)
    return nudged


def _public(seg) -> Segment:
    row: Segment = {"type": seg["type"], "start": seg["start"], "end": seg["end"]}
    if seg["type"] == "arc":
        row["center"], row["radius"] = seg["center"], seg["radius"]
    return row


# ---------------------------------------------------------------------------
# NX: section curves under a scratch mark


def _cut(tx, frame, body) -> list[dict]:
    """Section curves of one body as 2D segments (temporary curves, read in place)."""
    uf = tx.uf
    general = uf.Curve.SectionGeneralData()
    general.Associate = 0
    general.Grouping = 0
    general.JoinType = 0
    general.Tolerance = SECTION_TOLERANCE
    general.Objects = [body.Tag]
    general.NumObjects = 1
    planes = uf.Curve.SectionPlanesData()
    planes.Planes = [uf.Modeling.CreatePlane(frame["origin"], frame["normal"])]
    planes.NumPlanes = 1
    try:
        group = uf.Curve.SectionFromPlanes(general, planes)
    except Exception as error:  # NX 2406: a plane that misses the body raises
        if "no section curves" in str(error).lower():
            return []
        raise
    members = uf.Group.AskGroupData(group)
    tags = members[0] if isinstance(members, tuple) else members
    manager = tx.nx.TaggedObjectManager
    return [_segment(tx, frame, manager.GetTaggedObject(t)) for t in list(tags or [])]


def _span(tx, frame, cut_plane, found) -> str:
    """Where the bodies lie against the plane, for the empty-cut warning."""
    try:
        low, high = measure.bounds(tx, found)
    except Exception:  # only a hint: the warning stands without it
        return ""
    if cut_plane is not None:  # a principal plane: world coordinates on its offset axis
        i = PRINCIPAL[cut_plane][2]
        axis = "xyz"[i]
        return (
            f"; the plane is at {axis} = {frame['origin'][i]:g}, the bodies span"
            f" {axis} {low[i]:g} to {high[i]:g}"
        )
    n, o = frame["normal"], geom.dot(frame["origin"], frame["normal"])
    corners = [[(low, high)[(k >> i) & 1][i] for i in range(3)] for k in range(8)]
    along = [geom.dot(c, n) - o for c in corners]
    return f"; the bodies lie {min(along):g} to {max(along):g} from the plane along normal"


def _summary(a, say):
    if a.get("radial_axis"):
        return f"Radial profile about {a['radial_axis']}"
    if a.get("plane"):
        return f"Section on {a['plane']}" + (f" at {a['offset']}" if a.get("offset") else "")
    return "Section on a plane"


@tool(
    effect="read",
    label="Measure section",
    summary=_summary,
    keywords=("section profile", "cross section", "cut plane", "radial profile", "loop area"),
    requires=("work_part",),
    cost="heavy",
    example={"plane": "xy", "offset": 5},
)
def nx_measure_section(
    tx: Tx,
    *,
    plane: Literal["xy", "yz", "xz"] | None = None,
    offset: Annotated[
        float | str, Quantity("length"), Doc("Along z, x, y for xy, yz, xz; or '5 mm'")
    ] = 0.0,
    origin: Point3 | None = None,
    normal: Vec3 | None = None,
    radial_axis: Literal["x", "y", "z"] | None = None,
    stations: Annotated[int, Range(ge=1, le=200)] = 20,
    at: Annotated[list[float], Items(1, 200)] | None = None,
    bodies: Targets | None = None,
) -> SectionData:
    """Cut bodies with one of plane (+ offset), normal (+ origin) or radial_axis; the model is
    unchanged. Loops: role, area, box, 2D lines/arcs. radial_axis z cuts xz, x or y cut xy:
    radius spans per axial station (at)."""
    common.note_exploded(tx)
    given = [n for n, v in (("plane", plane), ("normal", normal), ("radial_axis", radial_axis))
             if v is not None]  # fmt: skip
    if not given:
        fail(
            "NX_INVALID_ARGUMENT",
            "Give the cut: one of plane, normal or radial_axis",
            field="/plane",
            hint="e.g. " + FORMS,
        )
    if len(given) > 1:
        fail(
            "NX_INVALID_ARGUMENT",
            f"plane, normal and radial_axis are mutually exclusive; got {' and '.join(given)}",
            field="/" + given[1],
            hint="Keep one: " + FORMS,
        )
    offset = float(offset)
    if offset and plane is None:
        fail(
            "NX_INVALID_ARGUMENT",
            "offset goes with plane; with normal, move origin instead",
            field="/offset",
            hint="e.g. " + FORMS,
        )
    if at is not None and radial_axis is None:
        fail(
            "NX_INVALID_ARGUMENT",
            "at lists the axial stations of a radial_axis profile",
            field="/at",
            hint="For a plane at a position use {plane, offset}; for stations use "
            "{radial_axis, at}",
        )
    axial = 0
    cut_plane: str | None = plane
    if radial_axis is not None:
        cut_plane, axial, _ = RADIAL[radial_axis]
    frame = _frame(cut_plane, offset, origin, normal)
    with tx.checks():
        found = common.target_bodies(tx, bodies, "part")
    tx.frame = "work_part_absolute"
    cut: list[tuple[Any, list[dict]]] = []
    with tx.scratch(), native():
        for body in found:
            tx.checkpoint()
            cut.append((body, _cut(tx, frame, body)))
    scale = max([1.0] + [abs(c) for _, segs in cut for s in segs for c in s["start"]])
    tol = 1e-6 * scale
    loops: list[Loop] = []
    material = []
    total = 0.0
    for body, segments in cut:
        chained = _chain(segments, tol)
        polygons = [_polygon(segs) if closed else [] for segs, closed in chained]
        for i, (segs, closed) in enumerate(chained):
            low, high = _box(segs)
            row: Loop = {"body": tx.ref(body, kind="body")["id"], "closed": closed, "area": None}
            if closed:
                depth = sum(
                    1
                    for j, poly in enumerate(polygons)
                    if j != i and poly and _inside(segs[0]["start"], poly)
                )
                area = abs(_signed_area(segs))
                row["role"], row["area"] = ("hole" if depth % 2 else "outer"), area
                total += -area if depth % 2 else area
                material.append(segs)
            row["min"], row["max"] = low, high
            row["segments"] = [_public(s) for s in segs]
            loops.append(row)
    if len(loops) > MAX_LOOPS:
        fail("NX_TOO_MANY_OBJECTS", f"More than {MAX_LOOPS} loops", field="/bodies")
    if not loops:
        tx.warn(
            "NX_UPDATE_WARNING",
            "The plane does not cut the bodies: no loops" + _span(tx, frame, cut_plane, found),
        )
    out: SectionData = {"frame": frame, "area": total, "loops": loops}
    if radial_axis is not None:
        out["profile"] = [
            _station(material, axial, t)
            for t in _stations(material, axial, at, stations, max(tol, 1e-9))
        ]
    return out
