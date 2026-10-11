"""The overall-size check of linear dimensions: a dimension on the body's outline that
stops short of the body's extent along its measuring direction.

Rule (a note, the dimension is kept): a linear, horizontal or vertical dimension warns
when all of these hold, so feature sizes stay quiet:

- every edge it measures lies on the body's outline across the measuring direction (the
  edge reaches the body's lowest or highest point along the view direction normal to the
  measurement: the bottom edge of a plate seen from the top for a horizontal size);
- its span reaches neither end of the body's extent along the measurement (a filleted
  plate's straight edge between the tangent points: 110 of 120);
- it covers at least half that extent (a short step on the outline is a feature size).

Extents are exact: line ends, arc ends and the arc points farthest along the direction
(checked on the arc through UF ``Eval`` closest points), sampled for other curves, over
the edges of the body that owns the first edge. Bodies above ``MAX_EDGES`` edges are not
checked.
"""

from __future__ import annotations

import contextlib
import math

from nx_mcp.kit import curve, drawing, facts, geom

MAX_EDGES = 5000
SAMPLES = 64  # along a curve that is neither a line nor an arc
MIN_SHARE = 0.5  # of the extent: shorter spans are feature sizes


def edge_points(tx, edge, direction):
    """Points of ``edge`` that bound it along unit ``direction``: its ends, plus an arc's
    farthest points along the direction or samples of another curve."""
    found = facts.edge(tx, edge)
    points = [found["start"], found["end"]]
    if found["kind"] == "line":
        return points
    if found["kind"] == "arc" and found.get("normal") is not None:
        n = found["normal"]
        along = geom.sub(direction, geom.scale(n, geom.dot(direction, n)))
        if geom.norm(along) <= 1e-12:  # the arc is seen edge-on along the direction
            return points
        u = geom.scale(along, 1.0 / geom.norm(along))
        tol = 1e-6 * max(1.0, found["radius"])
        for sign in (1.0, -1.0):
            p = geom.add(found["center"], geom.scale(u, sign * found["radius"]))
            with contextlib.suppress(Exception):  # an evaluator that cannot project
                if math.dist(curve.closest(tx, edge, p)[0], p) <= tol:
                    points.append(p)
        return points
    with curve.evaluator(tx, edge) as e:
        points += [e.at(i / SAMPLES)[0] for i in range(1, SAMPLES)]
    return points


def extent(tx, edges, direction):
    """``(low, high)`` of ``edges`` along unit model ``direction``."""
    values = [geom.dot(p, direction) for e in edges for p in edge_points(tx, e, direction)]
    return min(values), max(values)


def directions(view, pa, pb, orientation):
    """``(along, across)``: unit model directions of the measurement and, in the view
    plane, normal to it; None when the points have no span in the view."""
    x, y, z = drawing.axes(view)
    if orientation == "horizontal":
        return x, y
    if orientation == "vertical":
        return y, x
    d = geom.sub(pb, pa)
    along = geom.sub(d, geom.scale(z, geom.dot(d, z)))
    if geom.norm(along) <= 1e-12:
        return None
    along = geom.scale(along, 1.0 / geom.norm(along))
    return along, geom.cross(z, along)


def shortfall(tx, view, edges, pa, pb, orientation, value):
    """``(extent, low, high, along)`` when the dimension looks like a short overall size
    (see the module rule), else None."""
    body = getattr(edges[0], "GetBody", lambda: None)()
    axes = directions(view, pa, pb, orientation)
    if body is None or axes is None:
        return None
    outline = list(body.GetEdges())
    if not outline or len(outline) > MAX_EDGES:
        return None
    along, across = axes
    low, high = extent(tx, outline, along)
    size = high - low
    tol = 1e-6 * max(1.0, size)
    span = sorted(geom.dot(p, along) for p in (pa, pb))
    if value >= size - tol or value < MIN_SHARE * size:
        return None
    if span[0] <= low + tol or span[1] >= high - tol:
        return None  # it reaches an end of the body: a size from that end
    lo_w, hi_w = extent(tx, outline, across)
    tol_w = 1e-6 * max(1.0, hi_w - lo_w)
    for edge in edges:
        a, b = extent(tx, [edge], across)
        if a > lo_w + tol_w and b < hi_w - tol_w:
            return None  # an inner edge: a feature size
    return size, low, high, along


def outer_edges(tx, view, along, low, high):
    """Visible straight edges seen across the measurement at the body's two ends
    (``[at low, at high]``, None where there is none)."""
    out: list = [None, None]
    tol = 1e-6 * max(1.0, high - low)
    for edge, _drawn in drawing.visible_edges(tx, view):
        with contextlib.suppress(Exception):  # an edge NX cannot evaluate
            found = facts.edge(tx, edge)
            if found["kind"] != "line":
                continue
            ends = [geom.dot(p, along) for p in (found["start"], found["end"])]
            if abs(ends[0] - ends[1]) > tol:
                continue  # not seen across the measurement
            for i, end in enumerate((low, high)):
                if out[i] is None and abs(ends[0] - end) <= tol:
                    out[i] = edge
    return out


def warn_short(tx, view, edges, pa, pb, orientation, value):
    """Warn (``NX_NOTE``) when a linear dimension stops short of the body's extent."""
    found = shortfall(tx, view, edges, pa, pb, orientation, value)
    if found is None:
        return
    size, low, high, along = found
    ends = outer_edges(tx, view, along, low, high)
    how = "two edges or points at the body's ends"
    if all(e is not None for e in ends):
        refs = [tx.ref(e, "edge")["id"] for e in ends]
        how = f"first {refs[0]} and second {refs[1]} (the outer edges at both ends)"
    tx.warn(
        "NX_NOTE",
        f"The dimension measures {value:g} but the body spans {size:g} along it: for the "
        f"overall size give {how}; nx_list_view_edges lists the drawn edges",
    )
