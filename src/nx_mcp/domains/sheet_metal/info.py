"""``nx_get_sheet_metal``: sheet-metal bodies, actual thickness and bend parameters.

NX 2406 findings encoded here:

- A Flat Pattern feature adds flat bodies that NX reports as sheet metal; their rows say
  ``flat_pattern: true`` (``common.is_flat_pattern``) so they are not taken for parts.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Offset, PageLimit, RefTo
from nx_mcp.domains.sheet_metal import common
from nx_mcp.kit import result, select


class BodyRow(TypedDict, total=False):
    body: result.Row
    sheet_metal: bool
    flat_pattern: bool
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
                "flat_pattern": common.is_flat_pattern(body),
                "thickness": common.thickness(tx, body),
                "bend_count": len(found),
                "bends": common.bend_rows(tx, found),
            }
        )
    return row


@tool(
    switch="work",  # NX 2406 answers in the work part's units: a bound read got mm for inch
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
    body: Annotated[str, RefTo("body", doc="Ref b:N; default: all")] | None = None,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> SheetMetalData:
    """Read the sheet-metal facts of the work part's bodies, or of one body. Rows give the
    actual thickness, each bend's face, state (bent/flat), inner radius, angle and K, and
    flat_pattern for Flat Pattern bodies."""
    bodies = [select.one(tx, body, "body", field="/body")] if body else list(tx.part.Bodies)
    selected, page = result.page(bodies, offset, limit)
    items: list[BodyRow] = []
    for b in selected:
        tx.checkpoint()
        items.append(body_row(tx, b))
    return {"items": items, "page": page}
