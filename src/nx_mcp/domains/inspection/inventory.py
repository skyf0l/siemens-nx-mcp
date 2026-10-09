"""``nx_list_objects``: one paged inventory for work-part objects (tool-surface 3.9).

Each domain registers ``@lister(kind)`` hooks for its own kinds. A lister is
``fn(tx, owner, owner_kind) -> [(obj, kind, extra)]``; ``extra`` holds the
kind-specific summary (``type`` is used by the ``type`` filter). Rows are only
built for the requested page, so large inventories register few references.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import hooks, tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Offset, PageLimit, RefTo, Text
from nx_mcp.kit import result

# Every kind some domain registered a @lister for (a test keeps the two in step: a domain
# that registers a new lister adds its kind here).
ListKind = Literal[
    "body",
    "face",
    "edge",
    "datum",
    "feature",
    "sketch",
    "drawing_sheet",
    "drawing_view",
    "dimension",
    "annotation",
    "drawing_table",
    "reference_set",
    "explosion",
    "component_pattern",
    "assembly_constraint",
    "section_view",
]


class ObjectRow(result.Row, total=False):
    type: str
    details: dict[str, Any]


class ListData(TypedDict):
    kind: str
    items: list[ObjectRow]
    page: dict[str, Any]


def _name(obj) -> str:
    return str(getattr(obj, "Name", "") or getattr(obj, "JournalIdentifier", "") or "")


@tool(
    effect="read",
    label="List objects",
    summary="List {kind} objects",
    keywords=("inventory", "list bodies", "list faces", "list features", "part navigator"),
    requires=("work_part",),
    cost="light",
    example={"kind": "body"},
)
def nx_list_objects(
    tx: Tx,
    *,
    kind: ListKind,
    owner: RefTo("body", "face", "feature") | None = None,  # type: ignore[valid-type]
    name_contains: Annotated[str, Text(64), Doc("Case-insensitive name filter")] | None = None,
    type: Annotated[str, Text(32, r"^[a-z_]+$"), Doc("Kind-specific type, e.g. plane")]
    | None = None,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> ListData:
    """List objects of one kind in the work part, optionally of one owner, filtered by name or
    type, one page at a time. Rows carry a reference and a short kind-specific summary."""
    lister = hooks("lister").get(kind)
    if lister is None:
        fail("NX_UNSUPPORTED_OPTION", f"No lister for {kind}", field="/kind")
    owner_obj = owner_kind = None
    if owner is not None:
        owner_obj = tx.resolve(owner, "body", "face", "feature", work_part=True)
        owner_kind = tx.kind_of(owner_obj)
    found = lister(tx, owner_obj, owner_kind)
    if name_contains:
        needle = name_contains.casefold()
        found = [f for f in found if needle in _name(f[0]).casefold()]
    if type:
        found = [f for f in found if f[2].get("type") == type]
    selected, page = result.page(found, offset, limit)
    items = [{**tx.ref(obj, kind=k), **extra} for obj, k, extra in selected]
    return {"kind": kind, "items": items, "page": page}
