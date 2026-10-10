"""``nx_list_objects``: one paged inventory for work-part objects (tool-surface 3.9).

Each domain registers ``@lister(kind)`` hooks for its own kinds. A lister is
``fn(tx, owner, owner_kind) -> [(obj, kind, extra)]``; ``extra`` holds the
kind-specific summary (``type`` is used by the ``type`` filter). Rows are only
built for the requested page, so large inventories register few references.
A ``type`` outside a kind's known set (``TYPES``) is refused, never an empty page.
``include`` adds per-row detail to the page: ``adjacency`` (a face's edge ids, an edge's
face ids) or ``views`` (a drawing sheet's views).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict, cast

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
    "expression",
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

# kind -> every type its lister reports (empty: rows have no type). Kinds missing here
# report open-ended types (feature types, NX class names) and are not checked.
TYPES: dict[str, tuple[str, ...]] = {
    "body": ("solid", "sheet"),
    "face": ("plane", "cylinder", "cone", "sphere", "torus", "other"),
    "edge": ("line", "circle", "ellipse", "other"),
    "datum": ("plane", "axis", "csys", "point"),
    "drawing_table": ("parts_list", "bend", "hole", "title_block", "revision", "table"),
    "reference_set": ("custom",),
    "section_view": ("one_plane",),
    "sketch": (),
    "drawing_sheet": (),
    "explosion": (),
}

Include = Literal["adjacency", "views"]
# include value -> the kinds it applies to
_INCLUDE_KINDS = {"adjacency": ("face", "edge"), "views": ("drawing_sheet",)}


class ObjectRow(result.Row, total=False):
    type: str
    details: dict[str, Any]


class ListData(TypedDict):
    kind: str
    items: list[ObjectRow]
    page: dict[str, Any]


def _name(obj) -> str:
    return str(getattr(obj, "Name", "") or getattr(obj, "JournalIdentifier", "") or "")


def _check_type(kind: str, value: str) -> None:
    known = TYPES.get(kind)
    if known is None or value in known:
        return
    if not known:
        fail("NX_INVALID_ARGUMENT", f"{kind} rows have no type; omit type", field="/type")
    fail(
        "NX_INVALID_ARGUMENT",
        f"type {value!r} is not a {kind} type; use one of: {', '.join(known)}",
        field="/type",
    )


def _expand(tx, obj, kind: str) -> dict:
    """The extra details ``include`` asks for on one row (its kind was checked)."""
    if kind == "face":
        return {"edges": [tx.ref(e, kind="edge")["id"] for e in obj.GetEdges()]}
    if kind == "edge":
        return {"faces": [tx.ref(f, kind="face")["id"] for f in obj.GetFaces()]}
    return {"views": [tx.ref(v, kind="drawing_view") for v in obj.GetDraftingViews()]}


@tool(
    effect="read",
    label="List objects",
    summary="List {kind} objects",
    keywords=(
        "inventory",
        "list bodies",
        "list faces",
        "list features",
        "list expressions",
        "topology",
        "adjacency",
        "part navigator",
    ),
    requires=("work_part",),
    cost="light",
    example={"kind": "body"},
)
def nx_list_objects(
    tx: Tx,
    *,
    kind: ListKind,
    owner: Annotated[str, RefTo("body", "face", "feature")] | None = None,
    name_contains: Annotated[str, Text(64), Doc("Case-insensitive name filter")] | None = None,
    type: Annotated[
        str, Text(32, r"^[a-z_]+$"), Doc("Kind-specific type, e.g. face plane/cylinder/cone")
    ]
    | None = None,
    include: Include | None = None,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> ListData:
    """List work-part objects of one kind, filtered by owner, name or type, paged.
    Rows: a reference and a kind-specific summary."""
    lister = hooks("lister").get(kind)
    if lister is None:
        fail("NX_UNSUPPORTED_OPTION", f"No lister for {kind}", field="/kind")
    if include is not None and kind not in _INCLUDE_KINDS[include]:
        fail("NX_UNSUPPORTED_OPTION", f"include={include} applies to "
             f"{'/'.join(_INCLUDE_KINDS[include])}, not {kind}", field="/include")  # fmt: skip
    if type:
        _check_type(kind, type)
    owner_obj = owner_kind = None
    if owner is not None:
        owner_obj = tx.resolve(owner, "body", "face", "feature", work_part=True)
        owner_kind = tx.kind_of(owner_obj)
    found = lister(tx, owner_obj, owner_kind)
    if name_contains:
        needle = name_contains.casefold()
        found = [f for f in found if needle in _name(f[0]).casefold()]
    if type:
        # casefolded: dimension and annotation types are NX class names (PmiNote)
        found = [f for f in found if str(f[2].get("type") or "").casefold() == type]
    selected, page = result.page(found, offset, limit)
    items: list[ObjectRow] = []
    for obj, k, extra in selected:
        row = cast(ObjectRow, {**tx.ref(obj, kind=k), **extra})  # a row plus the lister's summary
        if include is not None:
            row["details"] = {**(row.get("details") or {}), **_expand(tx, obj, k)}
        items.append(row)
    return {"kind": kind, "items": items, "page": page}
