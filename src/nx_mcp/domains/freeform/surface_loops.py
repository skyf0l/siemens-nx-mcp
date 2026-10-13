"""Closed boundary loops and boundary gaps for the bounded plane and fill hole tools.

``loops_of`` groups curves or edges into closed chains (end to end within a chaining
tolerance), ``plane_fit`` checks that sampled points are coplanar, and ``sampled_gap``
measures how far a curve set lies from the bodies a feature built.
"""

from __future__ import annotations

from nx_mcp.core.errors import fail
from nx_mcp.kit import curve, geom

SAMPLES = 9  # points per boundary curve


def chaining(tol: float) -> float:
    """Chaining tolerance: ten modeling tolerances with a floor for round-off."""
    return max(10 * tol, 1e-6)


def curve_points(tx, obj, count=SAMPLES):
    """``count + 1`` points along a curve or edge, both ends included."""
    with curve.evaluator(tx, obj) as e:
        return [e.at(i / count)[0] for i in range(count + 1)]


def loops_of(tx, objs, tol, field="/boundary"):
    """Index lists of the closed loops the curves form (``NX_WRONG_GEOMETRY`` when a
    curve does not close a loop). A closed curve (circle) is a loop of its own."""
    ends = [curve.ends(tx, c) for c in objs]
    free = list(range(len(objs)))
    loops: list[list[int]] = []
    while free:
        first = free.pop(0)
        loop = [first]
        start, here = ends[first]
        while geom.dist(start, here) > tol:
            best = None
            for k in free:
                for s in (0, 1):
                    gap = geom.dist(here, ends[k][s])
                    if best is None or gap < best[0]:
                        best = (gap, k, s)
            if best is None or best[0] > tol:
                fail(
                    "NX_WRONG_GEOMETRY",
                    "The boundary curves do not form closed loops",
                    field=field,
                    hint="List every curve or edge round each loop, end to end",
                    details={"open_at": here, "gap": None if best is None else best[0]},
                )
            _gap, k, s = best
            free.remove(k)
            loop.append(k)
            here = ends[k][1 - s]
        loops.append(loop)
    return loops


def plane_fit(points):
    """``(origin, unit normal, largest deviation)`` of the plane through the three most
    spread points; ``None`` normal when the points lie on a line."""
    origin = points[0]
    far = max(points, key=lambda p: geom.dist(p, origin))
    axis = geom.sub(far, origin)
    best = (0.0, None)
    for p in points:
        c = geom.cross(axis, geom.sub(p, origin))
        size = geom.norm(c)
        if size > best[0]:
            best = (size, c)
    span = geom.norm(axis)
    if best[1] is None or best[0] <= 1e-9 * max(span, 1.0) ** 2:
        return origin, None, 0.0
    normal = [v / best[0] for v in best[1]]
    deviation = max(abs(geom.dot(geom.sub(p, origin), normal)) for p in points)
    return origin, normal, deviation


def sampled_gap(tx, objs, bodies, count=SAMPLES):
    """Largest distance from points along each curve to the nearest body."""
    worst = 0.0
    for obj in objs:
        tx.checkpoint()
        for p in curve_points(tx, obj, count):
            worst = max(worst, min(curve.point_distance(tx, b, p)[0] for b in bodies))
    return worst


def points_gap(tx, points, bodies):
    """Largest distance from ``points`` to the nearest body (points sampled before a
    feature consumed the curves, as a sewn patch does to its rim edges)."""
    worst = 0.0
    for p in points:
        worst = max(worst, min(curve.point_distance(tx, b, p)[0] for b in bodies))
    return worst


def free_edges(bodies) -> list:
    """Edges of ``bodies`` with one face (open boundary edges of sheets)."""
    return [e for b in bodies for e in b.GetEdges() if len(list(e.GetFaces())) == 1]
