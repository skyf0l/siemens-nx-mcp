"""``nx_list_objects``: one paged inventory for work-part objects (tool-surface 3.9).

Each domain registers ``@lister(kind)`` hooks for its own kinds. A lister is
``fn(tx, owner, owner_kind) -> [(obj, kind, extra)]``; ``extra`` holds the
kind-specific summary (``type`` is used by the ``type`` filter). Rows are only
built for the requested page, so large inventories register few references.
A ``type`` outside a kind's known set (``TYPES``) is refused, never an empty page; it is
matched without case, with spaces as underscores, a leading kind word dropped
(``face cylinder``), a plural ``s`` dropped and the adjectives in ``TYPE_ALIASES``
(``cylindrical``, ``arc``). ``owner`` takes a short ref or a result row's anchor.
A component owner lists the bodies, faces and edges of its occurrence bodies (with its
children's, ``kit.bodies.of``) through the body lister, and the timeline features of its
part: NX 2406 has no feature occurrences (``Component.FindOccurrence(feature)`` is None),
so feature rows are the component part's own, with a note.
``include`` adds per-row detail to the page: ``adjacency`` (a face's edge ids, an edge's
face ids) or ``views`` (a drawing sheet's views). Cylinder and cone face rows of the page
get their ``details`` here (``common.axial_facts``: radius, or radii and half-angle).
A body a pattern made (Pattern Geometry or a pattern of body-creating features) carries
``details.pattern`` (the pattern feature) and ``details.instance`` (1 for the first copy,
in NX's order; the seed is not an instance). NX 2406 gives them through
``Feature.ContainerFeature`` (the body's feature, then its instance feature, up to the
pattern) and the pattern's ``GetImmediateContainedFeatures`` order, which is also the
``(2:5A)`` number in the copy's name. An instance has no angle or location of its own
(``Location``: "Feature has no origin"); nx_get_feature reads the pattern's layout.
``compact: true`` gives body, face and edge rows as id, name and the conservative box
(``min``, ``max``: UF ``AskBoundingBox``) only, so hundreds of bodies page in few tokens.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict, cast

from nx_mcp.core.catalogue import hooks, tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Offset, PageLimit, RefKind, Text
from nx_mcp.domains.inspection import common
from nx_mcp.domains.inspection.geometry import resolve_owner
from nx_mcp.kit import bodies, features, result

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
OWNER_KINDS = ("body", "face", "feature", "component")
OwnerRef = Annotated[
    str, Text(400), RefKind(*OWNER_KINDS), Doc("Ref/anchor: body/face/feature/component")
]
# Kinds a component owner lists (bodies, faces, edges through its occurrence bodies).
COMPONENT_KINDS = ("body", "face", "edge", "feature")

Include = Literal["adjacency", "views"]
# ``detail`` is a call key (result compaction, the sidecar's): this option is ``compact``.
Compact = Annotated[bool, Doc("Body/face/edge rows as id, name, min, max only")]
COMPACT_KINDS = ("body", "face", "edge")
# include value -> the kinds it applies to
_INCLUDE_KINDS = {"adjacency": ("face", "edge"), "views": ("drawing_sheet",)}


class ObjectRow(result.Row, total=False):
    type: str
    details: dict[str, Any]
    min: list[float]
    max: list[float]


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


def _of_component(tx, lister, component, kind: str) -> list:
    """Rows of a component occurrence: its occurrence bodies (and children's) through the
    body lister, or the features of its part."""
    if kind not in COMPONENT_KINDS:
        fail(
            "NX_WRONG_KIND",
            f"A component owner lists {', '.join(COMPONENT_KINDS)}, not {kind}",
            field="/owner",
        )
    if kind != "feature":
        return [row for body in bodies.of(tx, component) for row in lister(tx, body, "body")]
    prototype = None if bodies.suppressed(component) else getattr(component, "Prototype", None)
    if prototype is None:  # suppressed or not loaded
        return []
    found = []
    for feature, suppressed, active in features.states(tx, prototype).values():
        details = {"suppressed": suppressed, "timestamp": int(feature.Timestamp)}
        if not active:
            details["inactive"] = True
        found.append((feature, "feature", {"type": str(feature.FeatureType).lower(),
                                           "details": details}))  # fmt: skip
    tx.warn("NX_NOTE", f"Features of {component.Name} live in its part "
            f"{prototype.Leaf} (NX has no feature occurrences): make that part the work "
            "part (nx_activate_part) to use them")  # fmt: skip
    return found


def _expand(tx, obj, kind: str) -> dict:
    """The extra details ``include`` asks for on one row (its kind was checked)."""
    if kind == "face":
        return {"edges": [tx.ref(e, kind="edge")["id"] for e in obj.GetEdges()]}
    if kind == "edge":
        return {"faces": [tx.ref(f, kind="face")["id"] for f in obj.GetFaces()]}
    return {"views": [tx.ref(v, kind="drawing_view") for v in obj.GetDraftingViews()]}


@tool(
    target="assembly",
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
    name_contains: Annotated[str, Text(64), Doc("Name substring, any case")] | None = None,
    type: Annotated[str, Text(32), Doc("e.g. cylinder, solid")] | None = None,
    include: Include | None = None,
    compact: Compact = False,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> ListData:
    """List work-part objects of one kind by owner, name or type, paged.
    Feature parameters: kind expression, owner the feature. Pattern copies (bodies) give
    details.pattern and details.instance."""
    lister = hooks("lister").get(kind)
    if lister is None:
        fail("NX_UNSUPPORTED_OPTION", f"No lister for {kind}", field="/kind")
    if include is not None and kind not in _INCLUDE_KINDS[include]:
        fail("NX_UNSUPPORTED_OPTION", f"include={include} applies to "
             f"{'/'.join(_INCLUDE_KINDS[include])}, not {kind}", field="/include")  # fmt: skip
    if compact and (kind not in COMPACT_KINDS or include is not None):
        fail("NX_UNSUPPORTED_OPTION", "compact lists body, face or edge rows, without "
             "include", field="/compact" if include is None else "/include")  # fmt: skip
    if type:
        type = _known_type(kind, _normal_type(kind, type))
        _check_type(kind, type)
    owner_obj = owner_kind = None
    if owner is not None:
        owner_obj = resolve_owner(tx, owner, OWNER_KINDS, work_part=True)
        owner_kind = tx.kind_of(owner_obj)
    if owner_kind == "component":
        found = _of_component(tx, lister, owner_obj, kind)
    else:
        found = lister(tx, owner_obj, owner_kind)
    if name_contains:
        needle = name_contains.casefold()
        found = [f for f in found if needle in _name(f[0]).casefold()]
    if type:
        # Both sides normalised: dimension and annotation types are NX class names
        # (PmiNote), feature types have spaces ("simple hole").
        found = [f for f in found if _normal_type(kind, str(f[2].get("type") or "")) == type]
    selected, page = result.page(found, offset, limit)
    if compact:
        return {"kind": kind, "items": _compact(tx, selected), "page": page}
    items: list[ObjectRow] = []
    patterns: dict[int, list[int]] = {}  # pattern feature tag -> its instance features' tags
    for obj, k, extra in selected:
        row = cast(ObjectRow, {**tx.ref(obj, kind=k), **extra})  # a row plus the lister's summary
        if k == "face" and extra.get("type") in ("cylinder", "cone"):
            with native():
                axial = common.axial_facts(tx, obj, tx.uf.Modeling.AskFaceData(obj.Tag))
            row["details"] = {**(row.get("details") or {}), **axial}
        if k == "body":
            instance = _pattern_instance(tx, obj, patterns)
            if instance is not None:
                row["details"] = {**(row.get("details") or {}), **instance}
        if include is not None:
            row["details"] = {**(row.get("details") or {}), **_expand(tx, obj, k)}
        items.append(row)
    return {"kind": kind, "items": items, "page": page}


def _compact(tx, selected) -> list[ObjectRow]:
    """Compact rows: id, name and the conservative box of each object."""
    items: list[ObjectRow] = []
    for obj, k, _extra in selected:
        tx.checkpoint()
        row = tx.ref(obj, kind=k)
        with native():
            box = [float(v) for v in tx.uf.ModlGeneral.AskBoundingBox(obj.Tag)]
        items.append({"id": row["id"], "name": row["name"], "min": box[:3], "max": box[3:6]})
    return items


def _pattern_instance(tx, body, patterns) -> dict | None:
    """``{"pattern": feature id, "instance": n}`` of a body a pattern made, else None.
    ``patterns`` caches each pattern's instance features."""
    if getattr(body, "IsOccurrence", False):
        return None  # the features live in the component's part
    try:
        found = list(body.GetFeatures())
    except Exception:
        return None
    for feature in found:
        chain = [feature]
        while len(chain) < 8:
            container = getattr(chain[-1], "ContainerFeature", None)
            if container is None:
                break
            chain.append(container)
        top = chain[-1]
        if len(chain) < 2 or "pattern" not in str(getattr(top, "FeatureType", "")).casefold():
            continue
        key = int(top.Tag)
        if key not in patterns:
            with native():
                patterns[key] = [int(f.Tag) for f in top.GetImmediateContainedFeatures()]
        tag = int(chain[-2].Tag)
        if tag in patterns[key]:
            return {
                "pattern": tx.ref(top, kind="feature")["id"],
                "instance": patterns[key].index(tag) + 1,
            }
    return None
