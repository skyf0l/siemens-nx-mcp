"""``nx_list_objects``: one paged inventory for work-part objects (tool-surface 3.9).

Each domain registers ``@lister(kind)`` hooks for its own kinds. A lister is
``fn(tx, owner, owner_kind) -> [(obj, kind, extra)]``; ``extra`` holds the
kind-specific summary (``type`` is used by the ``type`` filter). Rows are only
built for the requested page, so large inventories register few references.
A ``type`` outside a kind's known set (``TYPES``) is refused, never an empty page; it is
matched without case, with spaces as underscores, a leading kind word dropped
(``face cylinder``), a plural ``s`` dropped and the adjectives in ``TYPE_ALIASES``
(``cylindrical``, ``arc``). ``owner`` takes a short ref or a result row's anchor.
``include`` adds per-row detail to the page: ``adjacency`` (a face's edge ids, an edge's
face ids) or ``views`` (a drawing sheet's views). Cylinder and cone face rows of the page
get their ``details`` here (``common.axial_facts``: radius, or radii and half-angle).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict, cast

from nx_mcp.core.catalogue import hooks, tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Offset, PageLimit, RefKind, Text
from nx_mcp.domains.inspection import common
from nx_mcp.domains.inspection.geometry import resolve_owner
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

# Natural words for the known types (checked against TYPES of the kind being listed).
TYPE_ALIASES = {
    "planar": "plane", "flat": "plane", "cylindrical": "cylinder", "conical": "cone",
    "spherical": "sphere", "toroidal": "torus", "linear": "line", "straight": "line",
    "arc": "circle", "circular": "circle", "elliptical": "ellipse", "solid_body": "solid",
    "sheet_body": "sheet", "datum_plane": "plane", "datum_axis": "axis",
    "datum_csys": "csys", "datum_point": "point", "coordinate_system": "csys",
}  # fmt: skip
OWNER_KINDS = ("body", "face", "feature")
OwnerRef = Annotated[str, Text(400), RefKind(*OWNER_KINDS), Doc("Ref/anchor: body/face/feature")]

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


def _normal_type(kind: str, value: str) -> str:
    """``value`` casefolded, spaces as underscores, without a leading kind word."""
    words = value.casefold().replace("-", " ").replace("_", " ").split()
    if len(words) > 1 and words[0].rstrip("s") in (kind, *kind.split("_")):
        words = words[1:]
    return "_".join(words)


def _known_type(kind: str, value: str) -> str:
    """The listed type ``value`` names (aliases and plurals mapped), or ``value``."""
    known = TYPES.get(kind)
    if not known:
        return value
    for candidate in (value, value[:-1] if value.endswith("s") else value):
        candidate = TYPE_ALIASES.get(candidate, candidate)
        if candidate in known:
            return candidate
    return value


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
        "list bodies",
        "list faces",
        "list edges",
        "list features",
        "list expressions",
        "assembly constraints status",
        "topology",
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
    owner: OwnerRef | None = None,
    name_contains: Annotated[str, Text(64), Doc("Case-insensitive name filter")] | None = None,
    type: Annotated[str, Text(32), Doc("e.g. cylinder, circle, solid")] | None = None,
    include: Include | None = None,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> ListData:
    """List work-part objects of one kind, filtered by owner, name or type, paged.
    Feature parameters: kind expression, owner the feature."""
    lister = hooks("lister").get(kind)
    if lister is None:
        fail("NX_UNSUPPORTED_OPTION", f"No lister for {kind}", field="/kind")
    if include is not None and kind not in _INCLUDE_KINDS[include]:
        fail("NX_UNSUPPORTED_OPTION", f"include={include} applies to "
             f"{'/'.join(_INCLUDE_KINDS[include])}, not {kind}", field="/include")  # fmt: skip
    if type:
        type = _known_type(kind, _normal_type(kind, type))
        _check_type(kind, type)
    owner_obj = owner_kind = None
    if owner is not None:
        owner_obj = resolve_owner(tx, owner, OWNER_KINDS, work_part=True)
        owner_kind = tx.kind_of(owner_obj)
    found = lister(tx, owner_obj, owner_kind)
    if name_contains:
        needle = name_contains.casefold()
        found = [f for f in found if needle in _name(f[0]).casefold()]
    if type:
        # Both sides normalised: dimension and annotation types are NX class names
        # (PmiNote), feature types have spaces ("simple hole").
        found = [f for f in found if _normal_type(kind, str(f[2].get("type") or "")) == type]
    selected, page = result.page(found, offset, limit)
    items: list[ObjectRow] = []
    for obj, k, extra in selected:
        row = cast(ObjectRow, {**tx.ref(obj, kind=k), **extra})  # a row plus the lister's summary
        if k == "face" and extra.get("type") in ("cylinder", "cone"):
            with native():
                axial = common.axial_facts(tx, obj, tx.uf.Modeling.AskFaceData(obj.Tag))
            row["details"] = {**(row.get("details") or {}), **axial}
        if include is not None:
            row["details"] = {**(row.get("details") or {}), **_expand(tx, obj, k)}
        items.append(row)
    return {"kind": kind, "items": items, "page": page}
