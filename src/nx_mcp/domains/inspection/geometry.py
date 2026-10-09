"""Find faces and edges by rule, recognise holes, anchor and re-resolve geometry
(tool-surface 3.9).

``nx_find_geometry`` ranks candidates by the NX minimum distance to a point
(``nearest``) or by the conservative bounding-box centre along an axis
(``highest``/``lowest``) and returns a ``selector``: the same rule, with a persistent
owner anchor, that ``nx_resolve_reference`` re-evaluates after edits or a reopen.

Plane normals are UF ``AskFaceData`` directions (the outward normal on NX 2406);
a cylinder whose face sense is reversed (``AskFaceData`` sign -1) is a bore.
Anchors from ``nx_create_anchor`` are NX persistent handles
(``@<part>:<kind>=<handle>``): they survive edits that keep the entity, save and
reopen. ``nx_resolve_reference`` also takes the automatic feature, body and sketch
anchors of every result row (``@<part>:EXTRUDE(2)#0``).
"""

from __future__ import annotations

import json
import math
import re
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Offset, PageLimit, Point3, Range, RefTo, Text, Vec3
from nx_mcp.domains.inspection import common
from nx_mcp.kit import args, bodies, curve, geom, parts, result, select

FACE_TYPES = {22: "plane", 16: "cylinder", 17: "cone", 18: "sphere", 19: "torus"}
EDGE_TYPES = {"Linear": "line", "Circular": "circle", "Elliptical": "ellipse"}
FACE_ONLY, EDGE_ONLY = {"plane", "cylinder"}, {"circle", "line"}
MAX_CANDIDATES = 20000
DEFAULT_TOLERANCE = 0.001
AXES = "xyz"
_HANDLE_ANCHOR = re.compile(r"^@(?P<part>.+?):(?P<kind>body|face|edge)=(?P<handle>.+)$")
_JID_ANCHOR = re.compile(r"^@(?P<part>.+):(?P<jid>[^#]+?)(?:#(?P<index>\d+))?$")

Tolerance = Annotated[float, Range(gt=0, le=1), Doc("Radius and normal tolerance")]
GeometryType = Literal["any", "plane", "cylinder", "circle", "line"]
Order = Literal["nearest", "highest", "lowest"]
AnchorText = Annotated[str, Text(400), Doc("Anchor from nx_create_anchor or a result row")]


SelectorText = Annotated[str, Text(2000), Doc("selector text from nx_find_geometry")]
SELECTOR_KEYS = {
    "part", "kind", "geometry_type", "order", "axis", "tolerance",
    "owner", "owner_kind", "normal", "radius", "near",
}  # fmt: skip


class GeometryRow(result.Row, total=False):
    type: str
    center: list[float]
    distance: float
    point: list[float]
    normal: list[float]
    radius: float
    axis: list[float]
    axis_point: list[float]
    role: str


class FindData(TypedDict):
    items: list[GeometryRow]
    selector: str | None
    page: dict[str, Any]


# ---------------------------------------------------------------------------
# Candidate scan


def _check_rule(kind, geometry_type, normal, radius, near, order):
    if kind == "face" and geometry_type in EDGE_ONLY:
        fail("NX_INVALID_ARGUMENT", f"{geometry_type} applies to edges", field="/geometry_type")
    if kind == "edge" and geometry_type in FACE_ONLY:
        fail("NX_INVALID_ARGUMENT", f"{geometry_type} applies to faces", field="/geometry_type")
    if normal is not None and (kind, geometry_type) != ("face", "plane"):
        fail("NX_INVALID_ARGUMENT", "normal filters planar faces only", field="/normal")
    if radius is not None and geometry_type not in ("cylinder", "circle"):
        fail("NX_INVALID_ARGUMENT", "radius filters cylinders or circles", field="/radius")
    if order == "nearest" and near is None:
        fail("NX_INVALID_ARGUMENT", "order nearest needs a near point", field="/near")


def _owner(tx, owner):
    if owner is None:
        return None, None
    obj = tx.resolve(owner, *common.TARGET_KINDS)
    return obj, tx.kind_of(obj)


def _candidates(tx, kind, owner_obj):
    found = bodies.of(tx, owner_obj) if owner_obj is not None else bodies.scope(tx, "auto")
    entities: dict[int, Any] = {}
    for body in found:
        tx.checkpoint()
        for obj in body.GetFaces() if kind == "face" else body.GetEdges():
            entities.setdefault(int(obj.Tag), obj)
        if len(entities) > MAX_CANDIDATES:
            fail(
                "NX_TOO_MANY_OBJECTS",
                f"More than {MAX_CANDIDATES} candidates; narrow the owner",
                field="/owner",
            )
    return list(entities.values())


def _describe(tx, obj, kind):
    """Geometry facts of one face or edge (no reference is registered here)."""
    facts: dict[str, Any] = {}
    if kind == "face":
        with native():
            data = tx.uf.Modeling.AskFaceData(obj.Tag)
        code, point, direction, sign = int(data[0]), data[1], data[2], data[6]
        facts["type"] = FACE_TYPES.get(code, "other")
        if code == select.FACE_PLANE:
            facts["normal"] = geom.unit(common.xyz3(direction))
        elif code == select.FACE_CYLINDER:
            facts.update(
                radius=float(data[4]),
                axis=geom.unit(common.xyz3(direction)),
                axis_point=common.xyz3(point),
                role="bore" if float(sign) < 0 else "boss",
            )
    else:
        name = result.enum_name(obj.SolidEdgeType, tx.nx.Edge.EdgeType)
        facts["type"] = EDGE_TYPES.get(name, "other")
        if facts["type"] == "circle":
            with native():
                facts["radius"] = float(tx.uf.Curve.AskArcData(obj.Tag).Radius)
    return facts


def search(tx, *, owner_obj, kind, geometry_type, normal, radius, near, order, axis, tolerance):
    """Ranked ``[(rank, obj, facts)]`` of every face or edge matching the rule."""
    want_normal = geom.unit(normal, "/normal") if normal is not None else None
    target = args.vec3(near, "/near") if near is not None else None
    ranked = []
    for obj in _candidates(tx, kind, owner_obj):
        facts = _describe(tx, obj, kind)
        if geometry_type != "any" and facts["type"] != geometry_type:
            continue
        if want_normal is not None and geom.dot(want_normal, facts["normal"]) < 1 - tolerance:
            continue
        if radius is not None and abs(facts["radius"] - radius) > tolerance:
            continue
        with native():
            box = [float(v) for v in tx.uf.ModlGeneral.AskBoundingBox(obj.Tag)]
        facts["center"] = [(box[i] + box[i + 3]) / 2 for i in range(3)]
        if target is not None:
            distance, on_object = curve.point_distance(tx, obj, target)
            facts.update(distance=distance, point=on_object)
        rank = facts["distance"] if order == "nearest" else facts["center"][AXES.index(axis)]
        ranked.append((rank, obj, facts))
    sign = -1.0 if order == "highest" else 1.0
    ranked.sort(key=lambda item: (sign * item[0], int(item[1].Tag)))
    return ranked


def _anchor_of(tx, obj, kind):
    if kind == "component":
        return f"@{_part_path(tx)}:{obj.JournalIdentifier}"
    return tx.ref(obj, kind=kind).get("anchor")


def _part_path(tx):
    return tx.services.part_path(tx.part)


@tool(
    effect="read",
    label="Find geometry",
    summary="Find {kind}s: {geometry_type}",
    keywords=("select face", "find edge", "pick geometry", "nearest face", "top face"),
    requires=("work_part",),
    cost="light",
    example={"kind": "face", "geometry_type": "plane", "order": "highest", "axis": "z"},
)
def nx_find_geometry(
    tx: Tx,
    *,
    owner: RefTo("body", "feature", "component") | None = None,  # type: ignore[valid-type]
    kind: Literal["face", "edge"] = "face",
    geometry_type: GeometryType = "any",
    normal: Vec3 | None = None,
    radius: Annotated[float, Range(gt=0), Doc("Part units")] | None = None,
    near: Point3 | None = None,
    order: Order = "nearest",
    axis: Literal["x", "y", "z"] = "z",
    tolerance: Tolerance = DEFAULT_TOLERANCE,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> FindData:
    """Find faces or edges by type, normal, radius, nearest point or extreme along an axis.
    The selector re-runs the rule in nx_resolve_reference."""
    _check_rule(kind, geometry_type, normal, radius, near, order)
    owner_obj, owner_kind = _owner(tx, owner)
    ranked = search(
        tx,
        owner_obj=owner_obj,
        kind=kind,
        geometry_type=geometry_type,
        normal=normal,
        radius=radius,
        near=near,
        order=order,
        axis=axis,
        tolerance=tolerance,
    )
    selected, page = result.page(ranked, offset, limit)
    items = [{**tx.ref(obj, kind=kind), **facts} for _rank, obj, facts in selected]
    selector: str | None = {
        "part": _part_path(tx),
        "kind": kind,
        "geometry_type": geometry_type,
        "order": order,
        "axis": axis,
        "tolerance": tolerance,
    }
    for key, value in (("normal", normal), ("radius", radius), ("near", near)):
        if value is not None:
            selector[key] = [float(v) for v in value] if key != "radius" else float(value)
    if owner_obj is not None:
        anchor = _anchor_of(tx, owner_obj, owner_kind)
        # Without a persistent owner anchor the rule cannot be re-run on the same owner.
        selector = dict(selector, owner=anchor, owner_kind=owner_kind) if anchor else None
    text = json.dumps(selector, separators=(",", ":")) if selector else None
    return {"items": items, "selector": text, "page": page}


# ---------------------------------------------------------------------------
# Holes


class HoleRow(result.Row, total=False):
    radius: float
    axis: list[float]
    axis_point: list[float]
    group: int
    full: bool


class HoleGroup(TypedDict):
    group: int
    axis: list[float]
    axis_point: list[float]
    radii: list[float]
    faces: list[str]


class HolesData(TypedDict):
    items: list[HoleRow]
    groups: list[HoleGroup]
    page: dict[str, Any]


def _group_holes(tx, bores, tol):
    groups: list[dict] = []
    for face, facts in bores:
        axis = facts["axis"]
        for group in groups:
            delta = geom.sub(facts["axis_point"], group["axis_point"])
            along = geom.dot(delta, group["axis"])
            radial = math.sqrt(max(0.0, geom.dot(delta, delta) - along * along))
            if geom.parallel(axis, group["axis"], 1e-8) and radial <= tol:
                break
        else:
            group = {"group": len(groups), "axis": axis, "axis_point": facts["axis_point"]}
            group.update(radii=[], members=[])
            groups.append(group)
        group["members"].append((face, facts))
        if facts["radius"] not in group["radii"]:
            group["radii"].append(facts["radius"])
    return groups


@tool(
    effect="read",
    label="Find holes",
    summary=lambda a, say: "Find holes in " + (say("owner") if a.get("owner") else "the part"),
    keywords=("recognize holes", "bores", "hole recognition", "cylindrical holes", "drilled"),
    requires=("work_part",),
    cost="light",
    example={},
)
def nx_find_holes(
    tx: Tx,
    *,
    owner: RefTo("body", "feature", "component") | None = None,  # type: ignore[valid-type]
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> HolesData:
    """Find holes: inward cylindrical faces grouped by common axis. Rows give radius, axis
    and whether the face is a full circle; no thread or blind/through inference."""
    owner_obj, _ = _owner(tx, owner)
    tol = DEFAULT_TOLERANCE
    bores = []
    for obj in _candidates(tx, "face", owner_obj):
        facts = _describe(tx, obj, "face")
        if facts["type"] == "cylinder" and facts["role"] == "bore":
            with native():
                u0, u1, _v0, _v1 = (float(v) for v in tx.uf.Modeling.AskFaceUvMinmax(obj.Tag))
            facts["full"] = abs(abs(u1 - u0) - 2 * math.pi) <= 1e-6
            bores.append((obj, facts))
    groups = _group_holes(tx, bores, tol)
    ordered = [(face, facts, g["group"]) for g in groups for face, facts in g["members"]]
    selected, page = result.page(ordered, offset, limit)
    keys = ("radius", "axis", "axis_point", "full")
    items = [
        {**tx.ref(face, kind="face"), **{k: facts[k] for k in keys}, "group": group}
        for face, facts, group in selected
    ]
    out_groups = [
        {
            "group": g["group"],
            "axis": g["axis"],
            "axis_point": g["axis_point"],
            "radii": g["radii"],
            "faces": [tx.ref(face, kind="face")["id"] for face, _ in g["members"]],
        }
        for g in groups
    ]
    return {"items": items, "groups": out_groups, "page": page}


# ---------------------------------------------------------------------------
# Anchors and resolution


class AnchorData(TypedDict):
    object: result.Row
    anchor: str


@tool(
    effect="read",
    label="Create anchor",
    summary="Anchor {object}",
    keywords=("persistent reference", "anchor", "handle", "stable id", "survive edits"),
    requires=("work_part",),
    cost="instant",
    example={"object": "fc:12"},
)
def nx_create_anchor(
    tx: Tx,
    *,
    object: RefTo("body", "face", "edge"),  # type: ignore[valid-type]  # noqa: A002
) -> AnchorData:
    """Make a persistent anchor for a body, face or edge of the work part. It survives edits
    that keep the entity, save and reopen; re-resolve it with nx_resolve_reference."""
    obj = select.one(tx, object, "body", "face", "edge", field="/object")
    kind = tx.kind_of(obj)
    with native():
        handle = str(tx.uf.Tag.AskHandleFromTag(obj.Tag))
    return {"object": tx.ref(obj, kind=kind), "anchor": f"@{_part_path(tx)}:{kind}={handle}"}


class ResolveData(TypedDict, total=False):
    object: result.Row
    via: str
    rank: float


def _same_part(tx, path, field):
    if str(path).casefold() != str(_part_path(tx)).casefold():
        fail(
            "NX_WRONG_OWNER",
            f"{path} is not the work part",
            field=field,
            hint="Activate the part the anchor or selector was made in",
        )


def _resolve_handle(tx, match):
    _same_part(tx, match["part"], "/anchor")
    kind = match["kind"]
    part = tx.part
    try:
        with native():
            tag = int(tx.uf.Tag.AskTagOfHandle(match["handle"]))
    except NXError:
        tag = None
    pool = list(part.Bodies)
    if kind != "body":
        pool = [x for b in pool for x in (b.GetFaces() if kind == "face" else b.GetEdges())]
    found = next((o for o in pool if tag is not None and int(o.Tag) == tag), None)
    if found is None:
        fail(
            "NX_STALE_REFERENCE",
            f"The anchored {kind} no longer exists",
            field="/anchor",
            hint="Select replacement geometry with nx_find_geometry",
        )
    return found, kind


def _resolve_jid(tx, anchor):
    match = _JID_ANCHOR.match(anchor)
    if not match:
        fail("NX_INVALID_ARGUMENT", "Not an anchor", field="/anchor")
    _same_part(tx, match["part"], "/anchor")
    for kind in ("body", "feature", "sketch"):
        row = tx.services.references.find_anchor(anchor, kind)
        if row is not None:
            return tx.resolve(row["id"], kind), kind
    component = _component_by_jid(tx, match["jid"])
    if component is not None:
        return component, "component"
    fail(
        "NX_STALE_REFERENCE",
        "Nothing matches the anchor now",
        field="/anchor",
        hint="List the objects again with nx_list_objects",
    )


def _component_by_jid(tx, jid):
    found = [c for c, _ in parts.components(tx.part) if str(c.JournalIdentifier) == jid]
    return found[0] if len(found) == 1 else None


def _parse_selector(text):
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    required = SELECTOR_KEYS - {"owner", "owner_kind", "normal", "radius", "near"}
    if (
        not isinstance(value, dict)
        or not required <= set(value) <= SELECTOR_KEYS
        or value.get("kind") not in ("face", "edge")
        or value.get("order") not in ("nearest", "highest", "lowest")
        or value.get("axis") not in ("x", "y", "z")
    ):
        fail(
            "NX_INVALID_ARGUMENT", "Pass the selector text from nx_find_geometry", field="/selector"
        )
    return value


def _selector_owner(tx, selector):
    anchor = selector.get("owner")
    if anchor is None:
        return None
    kind = selector.get("owner_kind")
    obj, found_kind = _resolve_jid(tx, anchor)
    if kind and found_kind != kind:
        fail("NX_STALE_REFERENCE", "The selector's owner changed kind", field="/selector")
    return obj


@tool(
    effect="read",
    label="Resolve reference",
    summary="Resolve a saved reference",
    keywords=("re-resolve", "reacquire", "selector", "anchor lookup", "stale reference"),
    requires=("work_part",),
    cost="light",
    example={"anchor": "@smoke/a/blk.prt:face=RMblk.prt R1234"},
)
def nx_resolve_reference(
    tx: Tx,
    *,
    selector: SelectorText | None = None,
    anchor: AnchorText | None = None,
    tie_tolerance: Annotated[float, Range(gt=0), Doc("Rank gap that counts as a tie")] = 0.001,
) -> ResolveData:
    """Turn a selector from nx_find_geometry or an anchor into a current reference. A
    selector re-runs its rule and fails when the best two candidates tie."""
    args.exactly_one(selector=selector, anchor=anchor)
    if anchor is not None:
        match = _HANDLE_ANCHOR.match(anchor)
        obj, kind = _resolve_handle(tx, match) if match else _resolve_jid(tx, anchor)
        return {"object": tx.ref(obj, kind=kind), "via": "anchor"}
    selector = _parse_selector(selector)
    _same_part(tx, selector["part"], "/selector")
    owner_obj = _selector_owner(tx, selector)
    rule = {k: selector.get(k) for k in ("normal", "radius", "near")}
    _check_rule(selector["kind"], selector["geometry_type"], order=selector["order"], **rule)
    ranked = search(
        tx,
        owner_obj=owner_obj,
        kind=selector["kind"],
        geometry_type=selector["geometry_type"],
        order=selector["order"],
        axis=selector["axis"],
        tolerance=selector["tolerance"],
        **rule,
    )
    if not ranked:
        fail("NX_NOT_FOUND", "No geometry satisfies the selector now", field="/selector")
    if len(ranked) > 1 and abs(ranked[0][0] - ranked[1][0]) <= tie_tolerance:
        candidates = [{**tx.ref(o, kind=selector["kind"]), "rank": r} for r, o, _ in ranked[:2]]
        fail(
            "NX_AMBIGUOUS_REFERENCE",
            "The best two candidates tie; narrow the selector or lower tie_tolerance",
            field="/selector",
            details={"candidates": candidates},
        )
    rank, obj, _facts = ranked[0]
    return {"object": tx.ref(obj, kind=selector["kind"]), "via": "selector", "rank": rank}
