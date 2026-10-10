"""``nx_get_sheet_metal``: sheet-metal bodies, actual thickness and bend parameters."""

from __future__ import annotations

from typing import Any, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import BodyRef, Offset, PageLimit
from nx_mcp.domains.sheet_metal import common
from nx_mcp.kit import result, select


class BodyRow(TypedDict, total=False):
    body: result.Row
    sheet_metal: bool
    thickness: float
    bend_count: int
    bends: list[common.BendRow]


class SheetMetalData(TypedDict):
    items: list[BodyRow]
    page: dict[str, Any]


def body_row(tx, body) -> BodyRow:
    row: BodyRow = {"body": tx.ref(body, "body"), "sheet_metal": common.is_sheet_metal(tx, body)}
    if row["sheet_metal"]:
        found = common.bends(tx, body)
        row.update(
            {
                "thickness": common.thickness(tx, body),
                "bend_count": len(found),
                "bends": common.bend_rows(tx, found),
            }
        )
    return row


@tool(
    effect="read",
    label="Get sheet metal",
    summary=lambda a, say: (
        f"Sheet metal of {say('body')}" if a.get("body") else "Sheet metal bodies"
    ),
    keywords=("sheet metal info", "thickness", "bend radius", "bend angle", "k factor"),
    requires=("work_part",),
    cost="light",
    example={},
)
def nx_get_sheet_metal(
    tx: Tx,
    *,
    body: BodyRef | None = None,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> SheetMetalData:
    """Read the sheet-metal facts of the work part's bodies, or of one body. Rows give the
    actual thickness and each bend's face, state (bent/flat), inner radius, angle and K."""
    bodies = [select.one(tx, body, "body", field="/body")] if body else list(tx.part.Bodies)
    selected, page = result.page(bodies, offset, limit)
    items: list[BodyRow] = []
    for b in selected:
        tx.checkpoint()
        items.append(body_row(tx, b))
    return {"items": items, "page": page}
