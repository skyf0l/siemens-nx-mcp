"""The model edges a drawing view draws, in sheet coordinates (to pick dimension edges).

NX 2406 findings encoded here (``kit.drawing.visible_edges``): a view's curves are its
drafting bodies' drafting curves, each with its model edge as parent. Hidden edges and
switched-off smooth (tangent) edges have drafting curves too, whole and in the visible
font, so each edge is traced toward the viewer (UF ``Modeling.TraceARay``) and smooth
edges are dropped when the view hides them. A detail view's curves run past its boundary
circle: they are clipped to it. A circle seen edge-on is drawn as a line.
"""

from __future__ import annotations

import json
import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Offset, PageLimit
from nx_mcp.domains.drawing import style as view_style
from nx_mcp.domains.drawing.common import DETAIL, ViewArg, attribute, view_of
from nx_mcp.kit import drawing, result


class EdgeRow(result.Row, total=False):
    type: str
    start: list[float]
    end: list[float]
    center: list[float]
    length: Annotated[float, Doc("Drawn length, sheet units")]
    model_length: Annotated[float, Doc("Length seen, part units: an aligned dimension's value")]
    orientation: str | None
    radius: float
    partial: bool
    hidden: bool
    edge_on: bool


class ViewEdgesData(TypedDict):
    view: result.Row
    scale: float
    units: Annotated[str, Doc("Sheet units of start, end, center and length")]
    items: list[EdgeRow]
    page: dict


def _rank(row):
    """Lines first, longest first; then circles, largest first; ties left to right, then
    bottom to top (stable across sessions, unlike tags)."""
    size = -row["length"] if row["type"] == "line" else -row.get("radius", 0.0)
    return (row["type"] != "line", round(size, 6), *(round(v, 6) for v in row["center"]))


def cut_away(view):
    """True for a view that shows the model cut (section views)."""
    return type(view).__name__ == "SectionView"


def boundary(tx, view):
    """``(center, radius)`` on the sheet of a detail view's boundary circle, else None:
    the recorded boundary (center and radius in the model), or the circle inside the
    view's border for a detail view made in NX."""
    if type(view).__name__ != "DetailView":
        return None
    record = attribute(tx, view, DETAIL)
    if record:
        found = json.loads(record)
        center = drawing.to_sheet(tx, view, found["center"])
        x_axis = drawing.axes(view, tx)[0]
        rim = [c + found["radius"] * a for c, a in zip(found["center"], x_axis, strict=True)]
        return center, math.dist(center, drawing.to_sheet(tx, view, rim))
    box = drawing.borders(tx, view)
    middle = [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]
    return middle, min(box[2] - box[0], box[3] - box[1]) / 2


def _clip_other(row, center, radius, tol):
    """A circle or other curve: kept whole when it reaches inside the boundary, flagged
    ``partial`` when it also reaches outside; None when it lies outside."""
    if row["type"] == "circle" and "radius" in row:
        r = math.dist(row["center"], row["start"])
        gap = math.dist(row["center"], center)
        if gap >= radius + r - tol:
            return None
        if gap + r > radius + tol:
            row["partial"] = True
        return row
    inside = [math.dist(p, center) <= radius + tol for p in (row["start"], row["end"])]
    if not any(inside):
        return None
    if not all(inside):
        row["partial"] = True
    return row


def clip(row, circle, view_scale, part_per_sheet):
    """``row`` cut to a detail boundary ``circle`` (``partial`` when cut), or None when it
    lies outside it. Lines are cut exactly."""
    center, radius = circle
    tol = 1e-9 * max(1.0, radius)
    if row["type"] != "line" or "length" not in row:
        return _clip_other(row, center, radius, tol)
    a, b = row["start"], row["end"]
    d = [b[0] - a[0], b[1] - a[1]]
    f = [a[0] - center[0], a[1] - center[1]]
    qa = d[0] ** 2 + d[1] ** 2
    if qa <= 1e-18:
        return row if math.dist(a, center) <= radius + tol else None
    qb = 2 * (f[0] * d[0] + f[1] * d[1])
    qc = f[0] ** 2 + f[1] ** 2 - radius**2
    disc = qb * qb - 4 * qa * qc
    if disc <= 0:
        return None
    root = math.sqrt(disc)
    t0, t1 = max(0.0, (-qb - root) / (2 * qa)), min(1.0, (-qb + root) / (2 * qa))
    if t1 - t0 <= 1e-12:
        return None
    if t0 > 1e-12 or t1 < 1 - 1e-12:
        start = [a[0] + t0 * d[0], a[1] + t0 * d[1]]
        end = [a[0] + t1 * d[0], a[1] + t1 * d[1]]
        row.update(start=start, end=end, center=[(start[0] + end[0]) / 2, (start[1] + end[1]) / 2])
        row.update(drawing.line_facts(start, end, view_scale, part_per_sheet))
        row["partial"] = True
    return row


@tool(
    effect="read",
    label="List view edges",
    summary="Edges drawn in {view}",
    keywords=(
        "edges in drawing view",
        "visible edges",
        "pick edge for dimension",
        "overall length edge",
        "view geometry sheet coordinates",
        "hidden edges",
    ),
    requires=("work_part",),
    cost="light",
    example={"view": "v:7", "orientation": "horizontal", "limit": 5},
)
def nx_list_view_edges(
    tx: Tx,
    *,
    view: ViewArg,
    geometry_type: Literal["any", "line", "circle"] = "any",
    orientation: Literal["any", "horizontal", "vertical"] = "any",
    hidden: Annotated[bool, Doc("Also list edges behind material, flagged hidden")] = False,
    offset: Offset = 0,
    limit: PageLimit = 50,
) -> ViewEdgesData:
    """List model edges a drawing view draws visible, longest lines first, then circles
    (geometry_type circle: holes). Sheet start, end, center, length and orientation, cut to
    a detail view's boundary. Hidden, switched-off smooth and point-like edges are left out;
    a circle seen edge-on is a line. Pick dimension and centerline edges here:
    nx_find_geometry does not search drawing views."""
    shown, sheet = view_of(tx, view)
    sh = drawing.Sheet(tx, sheet)
    settings = view_style.read_style(tx, shown)
    found = drawing.visible_edges(
        tx,
        shown,
        smooth_edges=bool(settings.get("smooth_edges", True)),
        # A section view removes the material in front of its cut: a ray toward the viewer
        # would meet it, so only NX's drafting tells there.
        hidden_removal=bool(settings.get("hidden_lines", True)) and not cut_away(shown),
        keep_hidden=hidden,
    )
    tx.scale_budget(len(found))
    circle = boundary(tx, shown)
    view_scale = drawing.scale(tx, shown)
    rows = []
    for edge, drawn, state in found:
        tx.checkpoint()
        facts = drawing.edge_in_view(tx, shown, edge, drawn, sh.scale)
        if state in ("partial", "hidden"):
            facts[state] = True
        if circle is not None:
            facts = clip(facts, circle, view_scale, sh.scale)
            if facts is None:
                continue
        if geometry_type != "any" and facts["type"] != geometry_type:
            continue
        if orientation != "any" and facts.get("orientation") != orientation:
            continue
        if facts["type"] == "line" and facts["orientation"] is None:
            continue  # seen end-on: a point in this view
        rows.append((edge, facts))
    rows.sort(key=lambda item: (_rank(item[1]), int(item[0].Tag)))
    selected, page = result.page(rows, offset, limit)
    items = [{**tx.ref(edge, "edge"), **facts} for edge, facts in selected]
    return {
        "view": tx.ref(shown, "drawing_view"),
        "scale": view_scale,
        "units": sh.units,
        "items": items,  # type: ignore[typeddict-item]  # a row plus the edge facts
        "page": page,
    }
