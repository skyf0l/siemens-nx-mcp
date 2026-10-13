"""The model edges a drawing view draws, in sheet coordinates (to pick dimension edges).

NX 2406 findings encoded here (``kit.drawing.visible_edges``): a view's visible curves are
its drafting bodies' drafting curves, each with its model edge as parent; hidden lines are
not among them, and an edge seen end-on is drawn as no curve.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Offset, PageLimit
from nx_mcp.domains.drawing.common import ViewArg, view_of
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
    offset: Offset = 0,
    limit: PageLimit = 50,
) -> ViewEdgesData:
    """List model edges a drawing view draws visible, longest lines first, then circles
    (geometry_type circle: holes). Sheet start, end, center, length and orientation; hidden
    and end-on edges are left out. Pick dimension and centerline edges here: nx_find_geometry
    does not search drawing views."""
    shown, sheet = view_of(tx, view)
    sh = drawing.Sheet(tx, sheet)
    rows = []
    for edge, drawn in drawing.visible_edges(tx, shown):
        tx.checkpoint()
        facts = drawing.edge_in_view(tx, shown, edge, drawn, sh.scale)
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
        "scale": drawing.scale(tx, shown),
        "units": sh.units,
        "items": items,  # type: ignore[typeddict-item]  # a row plus the edge facts
        "page": page,
    }
