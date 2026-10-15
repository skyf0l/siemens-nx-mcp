"""Read-back shared by the derived-curve tools (section, intersection, projected, combined
projection and offset curves; this domain only).

Every derived-curve feature is read back from ``Feature.GetEntities()`` (its curves; points
are skipped): each curve's length (``GetLength``, part units), end points (UF ``Eval``) and
whether it is closed, plus sampled distances that prove where the curves lie.
"""

from __future__ import annotations

from typing import TypedDict

from nx_mcp.core.errors import fail, native
from nx_mcp.core.types import Point3
from nx_mcp.kit import args, curve, geom, licenses, measure, readback, result, select, units

SAMPLES = 9  # points per curve, both ends included
MAX_SAMPLED = 400  # curves sampled for a read-back (the rest are listed only)
LICENSE = "solid_modeling"


class CurveRow(TypedDict):
    curve: result.Row
    length: float  # part units
    start: Point3
    end: Point3
    closed: bool


class CurvesData(TypedDict):
    feature: result.Row
    curves: list[CurveRow]
    count: int
    length: float  # total, part units


def licensed(tx) -> None:
    """``NX_LICENSE_UNAVAILABLE`` before any builder when the seat lacks ``solid_modeling``."""
    licenses.require(tx, (LICENSE,), f"These curve features need the {LICENSE} license")


def strings(tx, refs, field) -> list:
    """Curves or edges of the work part (each listed once)."""
    args.distinct(field, refs, "curves")
    return [select.one(tx, r, "curve", "edge", field=f"{field}/{i}") for i, r in enumerate(refs)]


def solids(tx, refs, field) -> tuple[list, list]:
    """``(objects, faces)``: bodies or faces of the work part, and all their faces."""
    args.distinct(field, refs, "objects")
    objs = [select.one(tx, r, "body", "face", field=f"{field}/{i}") for i, r in enumerate(refs)]
    faces: list = []
    for o in objs:
        faces.extend(o.GetFaces() if hasattr(o, "GetFaces") else [o])
    return objs, faces


def plane_object(tx, origin, normal):
    """A smart plane through ``origin`` with unit ``normal`` (a cutting or target plane)."""
    nx = tx.nx
    return tx.part.Planes.CreatePlane(
        nx.Point3d(*(float(v) for v in origin)),
        nx.Vector3d(*(float(v) for v in normal)),
        nx.SmartObject.UpdateOption.WithinModeling,
    )


def chain_problem(tx, objs, limit=None):
    """None when the curves chain end to end into one string (open or closed), else
    ``(index, kind, details)``: ``branch`` (three or more ends meet at
    ``details.branch_at``) or ``gap`` (separate strings; ``details.gap`` is the
    smallest distance between free ends of two of them, from ``open_at`` to ``to``)."""
    pts = [p for o in objs for p in curve.ends(tx, o)]
    limit = gap_tolerance(tx, *pts) if limit is None else limit
    count = len(pts)
    links: list[list[int]] = [[] for _ in range(count)]
    for a in range(count):
        for b in range(a + 1, count):
            if a // 2 != b // 2 and geom.dist(pts[a], pts[b]) <= limit:
                links[a].append(b)
                links[b].append(a)
    for a in range(count):
        if len(links[a]) > 1:
            meeting = sorted({a // 2, *(b // 2 for b in links[a])})
            return a // 2, "branch", {"branch_at": clean(pts[a]), "curves": meeting}
    group = list(range(len(objs)))

    def root(i):
        while group[i] != i:
            i = group[i]
        return i

    for a in range(count):
        for b in links[a]:
            group[root(a // 2)] = root(b // 2)
    if len({root(i) for i in range(len(objs))}) == 1:
        return None
    free = [a for a in range(count) if not links[a]]
    pairs = [(geom.dist(pts[a], pts[b]), a, b) for a in free for b in free
             if root(a // 2) != root(b // 2)]  # fmt: skip
    if not pairs:
        return 1, "gap", {"gap": None, "strings": len({root(i) for i in range(len(objs))})}
    gap, a, b = min(pairs)
    return b // 2, "gap", {"gap": gap, "open_at": clean(pts[a]), "to": clean(pts[b])}


def chain_normal(tx, objs, field):
    """Unit normal of the plane the curves lie in (sampled), or ``NX_INVALID_ARGUMENT``
    naming ``field`` when they are a straight line or not planar."""
    points = [p for c in objs for p in samples(tx, c)]
    first = points[0]
    far = max(points, key=lambda p: geom.dist(p, first))
    axis = geom.sub(far, first)
    third = max(points, key=lambda p: geom.norm(geom.cross(axis, geom.sub(p, first))))
    normal = geom.cross(axis, geom.sub(third, first))
    size = geom.dist(far, first)
    if size <= 0 or geom.norm(normal) <= 1e-9 * size * size:
        fail(
            "NX_INVALID_ARGUMENT",
            "The curves form a straight line: give the direction",
            field=field,
        )
    normal = geom.unit(normal)
    if plane_gap_points(points, first, normal) > gap_tolerance(tx, *points):
        fail("NX_INVALID_ARGUMENT", "The curves are not planar: give the direction", field=field)
    return normal


def plane_gap_points(points, origin, normal) -> float:
    return max(abs(geom.dot(geom.sub(p, origin), normal)) for p in points)


def clean(p):
    """A point with round-off (|v| < 1e-9) snapped to 0."""
    return [0.0 if abs(v) < 1e-9 else float(v) for v in p]


def output_curves(tx, feature) -> list:
    """The curves a committed feature built (NX must have built one)."""
    with native(default="NX_ERROR"):
        found = [e for e in feature.GetEntities() if hasattr(e, "GetLength")]
    readback.expect(bool(found), "curves", "NX built no curve")
    return found


def samples(tx, obj, n=SAMPLES) -> list[list[float]]:
    """``n`` points along a curve or edge, its ends included."""
    with curve.evaluator(tx, obj) as e:
        return [e.at(i / (n - 1))[0] for i in range(n)]


def gap_tolerance(tx, *points) -> float:
    """Read-back distance limit: 0.01 mm in part units plus coordinate round-off."""
    return units.part_tolerance(tx, 0.01) + geom.tol(*points)


def row(tx, obj) -> CurveRow:
    start, end = curve.ends(tx, obj)
    length = measure.length(tx, obj)
    return {
        "curve": tx.ref(obj, kind="curve"),
        "length": length,
        "start": clean(start),
        "end": clean(end),
        "closed": geom.dist(start, end) <= gap_tolerance(tx, start, end),
    }


def describe(tx, feature, curves) -> CurvesData:
    """``{feature, curves, count, length}`` of a derived-curve feature's curves."""
    rows = []
    for c in curves:
        tx.checkpoint()
        rows.append(row(tx, c))
    return {
        "feature": tx.ref(feature, kind="feature"),
        "curves": rows,
        "count": len(rows),
        "length": sum(r["length"] for r in rows),
    }


def worst_gap(tx, curves, targets) -> float:
    """Largest distance from the sample points of ``curves`` to the nearest of
    ``targets`` (faces, bodies, curves or edges)."""
    worst = 0.0
    for c in curves[:MAX_SAMPLED]:
        tx.checkpoint()
        for p in samples(tx, c):
            worst = max(worst, min(curve.point_distance(tx, t, p)[0] for t in targets))
    return worst


def plane_gap(tx, curves, origin, normal) -> float:
    """Largest distance of the sample points of ``curves`` from a plane."""
    return max(
        (plane_gap_points(samples(tx, c), origin, normal) for c in curves[:MAX_SAMPLED]),
        default=0.0,
    )


def off_line(p, q, direction) -> float:
    """Distance of ``q`` from the line through ``p`` along unit ``direction``."""
    return geom.norm(geom.cross(geom.sub(q, p), direction))


def line_gap(tx, obj, p, direction, steps=200) -> float:
    """Distance from the line through ``p`` along unit ``direction`` to a curve or edge:
    sampled, then refined by a ternary search around the best sample."""
    with curve.evaluator(tx, obj) as e:

        def f(t):
            return off_line(p, e.at(t)[0], direction)

        best = min(range(steps + 1), key=lambda i: f(i / steps))
        low, high = max(0.0, (best - 1) / steps), min(1.0, (best + 1) / steps)
        for _ in range(40):
            a, b = low + (high - low) / 3, high - (high - low) / 3
            if f(a) <= f(b):
                high = b
            else:
                low = a
        return min(f(best / steps), f((low + high) / 2))
