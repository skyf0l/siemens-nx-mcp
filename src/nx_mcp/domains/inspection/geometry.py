"""Find faces and edges by rule, recognise holes, anchor and re-resolve geometry
(tool-surface 3.9).

``nx_find_geometry`` ranks candidates by the NX minimum distance to a point
(``nearest``) or by the conservative bounding-box centre along an axis
(``highest``/``lowest``) and returns a ``selector``: the same rule, with a persistent
owner anchor, that ``nx_resolve_reference`` re-evaluates after edits or a reopen.
Without ``order``, ``nearest`` is used when ``near`` is given, else ``lowest``.
A feature owner limits the candidates to the feature's own faces or edges
(``Feature.GetFaces``/``GetEdges``), not every face of its bodies; a face owner gives its
edges (``kind: edge`` only, as ``nx_list_objects`` lists them), its selector a face handle
anchor. ``normal`` also
takes an axis word (``+z``, ``-x``, ``top`` = ``+z``, ``bottom`` = ``-z``).
``parallel`` keeps the line edges along an axis (either sense) or parallel to a principal
plane (``xy``: across z), within ``tolerance`` as for ``normal``: ``parallel: 'z'`` gives
a block's vertical edges, where ``axis`` only orders the rows.

Plane normals are UF ``AskFaceData`` directions (the outward normal on NX 2406);
a cylinder whose face sense is reversed (``AskFaceData`` sign -1) is a bore.
Anchors from ``nx_create_anchor`` are NX persistent handles
(``@<part>:<kind>=<handle>``): they survive edits that keep the entity, save and
reopen. ``nx_resolve_reference`` also takes the automatic feature, body and sketch
anchors of every result row (``@<part>:EXTRUDE(2)#0``); every ``owner`` of this domain
takes a short ref or such an anchor (``resolve_owner``).

``nx_find_holes`` groups coaxial bores into holes. Each hole's ends come from the
circular edges of its bores (their centres along the axis); an end is open when the
face beyond it faces away from the hole (an outer plane, a widening cone, a cylinder)
and closed by a floor plane facing back into the hole or a narrowing (drill tip) cone.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import PurePath
from typing import Annotated, Any, Literal, TypedDict, cast

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    REF_PATTERN,
    Aliases,
    Doc,
    Json,
    Length,
    Offset,
    PageLimit,
    Point3,
    Range,
    RefKind,
    RefTo,
    Text,
)
from nx_mcp.domains.inspection import common
from nx_mcp.kit import args, bodies, curve, geom, measure, parts, result, select
from nx_mcp.kit import facts as kfacts

FACE_TYPES = {22: "plane", 16: "cylinder", 17: "cone", 18: "sphere", 19: "torus"}
FACE_CONE = 17
EDGE_TYPES = {"Linear": "line", "Circular": "circle", "Elliptical": "ellipse"}
FACE_ONLY, EDGE_ONLY = {"plane", "cylinder", "cone"}, {"circle", "line"}
PLURAL = {"face": "faces", "edge": "edges", "body": "bodies"}
MAX_CANDIDATES = 20000
DEFAULT_TOLERANCE = 0.001
AXES = "xyz"
DRAWING_KINDS = ("drawing_view", "drawing_sheet")
_HANDLE_ANCHOR = re.compile(r"^@(?P<part>.+?):(?P<kind>body|face|edge)=(?P<handle>.+)$")
_JID_ANCHOR = re.compile(r"^@(?P<part>.+):(?P<jid>[^#]+?)(?:#(?P<index>\d+))?$")
# Axis words accepted for a normal (casefolded, spaces dropped).
DIRECTION_WORDS = {
    **{f"{s}{a}": [float(sign) if i == k else 0.0 for k in range(3)]
       for i, a in enumerate(AXES) for s, sign in (("", 1), ("+", 1), ("-", -1))},
    "top": [0.0, 0.0, 1.0], "up": [0.0, 0.0, 1.0],
    "bottom": [0.0, 0.0, -1.0], "down": [0.0, 0.0, -1.0],
}  # fmt: skip

Tolerance = Annotated[float, Range(gt=0, le=1)]
GeometryType = Literal["any", "plane", "cylinder", "cone", "circle", "line"]
Order = Literal["nearest", "highest", "lowest"]
NORMAL_DOC = Doc("[x,y,z] or +z, -x, top...")
Normal = Annotated[tuple[float, float, float], NORMAL_DOC] | Annotated[str, Text(8), NORMAL_DOC]
# nx_find_geometry's normal: which faces it applies to (agents put it on edges).
FIND_NORMAL_DOC = Doc("Planar faces facing [x,y,z] or +z, -x, top...")
FindNormal = (
    Annotated[tuple[float, float, float], FIND_NORMAL_DOC]
    | Annotated[str, Text(8), FIND_NORMAL_DOC]
)
# A short ref or a row's anchor (no pattern here: resolve_owner explains a bad value).
OwnerRef = Annotated[
    str, Text(400), RefKind("body", "feature", "component"),
    Doc("body/feature/component"),
]  # fmt: skip
# nx_find_geometry also takes a face: its edges (as nx_list_objects lists them).
FIND_OWNER_KINDS = (*common.TARGET_KINDS, "face")
FindOwnerRef = Annotated[
    str, Text(400), RefKind(*FIND_OWNER_KINDS),
    Doc("body/feature/component; a face: its edges (kind edge)"),
]  # fmt: skip
AnchorText = Annotated[
    str,
    Text(400),
    Doc("Anchor '@...' (nx_create_anchor, a result row), or a ref like fc:12 to check"),
]


SelectorText = Annotated[str, Text(2000), Doc("selector from nx_find_geometry (text or object)")]
SelectorObject = Annotated[
    dict, Json(max_keys=16, max_chars=2000), Doc("selector from nx_find_geometry (text or object)")
]
SELECTOR_KEYS = {
    "part", "kind", "geometry_type", "order", "axis", "tolerance",
    "owner", "owner_kind", "normal", "radius", "near", "role", "parallel",
}  # fmt: skip
OPTIONAL_SELECTOR_KEYS = {"owner", "owner_kind", "normal", "radius", "near", "role", "parallel"}
PLANE_NORMALS = {"xy": "z", "yz": "x", "xz": "y"}
Parallel = Annotated[
    Literal["x", "y", "z", "xy", "yz", "xz"],
    Aliases(zx="xz", yx="xy", zy="yz"),
    Doc("Line edges along an axis or in a plane"),
]  # an axis (lines along it) or a principal plane (lines across its normal)


class GeometryRow(result.Row, total=False):
    type: str
    volume: float
    min: list[float]
    max: list[float]
    owner: str
    center: list[float]
    distance: float
    point: list[float]
    normal: list[float]
    radius: float
    axis: list[float]
    axis_point: list[float]
    role: str
    half_angle: float
    radii: list[float]


class FindData(TypedDict):
    items: list[GeometryRow]
    selector: str | None
    page: dict[str, Any]


# ---------------------------------------------------------------------------
# Owners and directions


def resolve_owner(tx, owner, kinds, *, field="/owner", work_part=False):
    """The live object of ``owner``: a short ref (``f:12``) or an anchor (``@part:...``)."""
    if owner.startswith("@"):
        match = _HANDLE_ANCHOR.match(owner)
        obj, kind = _resolve_handle(tx, match, field) if match else _resolve_jid(tx, owner, field)
        if kind not in kinds:
            fail(
                "NX_WRONG_KIND",
                f"{owner} is a {kind}; expected {'/'.join(kinds)}",
                field=field,
            )
        return obj
    if not re.match(REF_PATTERN, owner):
        fail(
            "NX_INVALID_ARGUMENT",
            f"{owner!r} is not a reference: give an id such as f:12 or b:3, or an anchor "
            "'@part:EXTRUDE(2)', from a result row (names are not accepted)",
            field=field,
            hint="List the objects with nx_list_objects to get their ids",
        )
    try:
        return tx.resolve(owner, *kinds, work_part=work_part)
    except NXError as error:
        if error.code != "NX_WRONG_KIND" or error.details.get("kind") not in DRAWING_KINDS:
            raise
        model = "find edges with the model body, feature or component as owner"
        fail(
            "NX_WRONG_KIND",
            f"{owner} is a {error.details['kind']}; expected {'/'.join(kinds)}",
            field=field,
            hint=f"nx_list_view_edges lists the edges a drawing view shows, with sheet "
            f"coordinates; or {model}"
            if error.details["kind"] == "drawing_view"
            else f"A drawing shows model geometry: {model} (or no owner)",
            details=error.details,
        )


def direction(value, field="/normal"):
    """A unit vector from ``[x, y, z]`` or an axis word (``+z``, ``-X``, ``top``)."""
    if not isinstance(value, str):
        return geom.unit(value, field)
    found = DIRECTION_WORDS.get(value.casefold().replace(" ", ""))
    if found is None:
        fail(
            "NX_INVALID_ARGUMENT",
            f"{value!r} is not a direction: give [x, y, z] or one of "
            "+x, -x, +y, -y, +z, -z, top, bottom",
            field=field,
        )
    return list(found)


# ---------------------------------------------------------------------------
# Candidate scan


def implied_type(kind, geometry_type, normal, radius, role, lined=False):
    """``any`` narrowed by the filters that imply a type: a normal (plane), a role
    (cylinder), a radius (cylinder on faces, circle on edges) or a line direction
    (``lined``: ``parallel``, line edges)."""
    if geometry_type != "any":
        return geometry_type
    if kind == "edge" and lined:
        return "line"
    if kind == "face" and normal is not None:
        return "plane"
    if kind == "face" and (role is not None or radius is not None):
        return "cylinder"
    if kind == "edge" and radius is not None:
        return "circle"
    return geometry_type


def _check_rule(kind, geometry_type, normal, radius, near, order, role=None, lined=False):
    if kind == "body" and (geometry_type, normal, radius, role) != ("any", None, None, None):
        fail(
            "NX_INVALID_ARGUMENT",
            "geometry_type, normal, radius and role filter faces or edges, not bodies",
            field="/kind",
            hint="Rank bodies with near or order, or find their faces with the body as owner",
        )
    if kind == "face" and geometry_type in EDGE_ONLY:
        fail("NX_INVALID_ARGUMENT", f"{geometry_type} applies to edges", field="/geometry_type")
    if kind == "edge" and geometry_type in FACE_ONLY:
        fail("NX_INVALID_ARGUMENT", f"{geometry_type} applies to faces", field="/geometry_type")
    if normal is not None and (kind, geometry_type) != ("face", "plane"):
        fail(
            "NX_INVALID_ARGUMENT",
            f"normal selects planar faces by the direction they face; it does not apply to "
            f"{kind} {geometry_type}",
            field="/normal",
            hint='Use kind "face" with normal (geometry_type "plane" or "any"); for edges, '
            "filter by geometry_type, radius or near instead",
        )
    if radius is not None and geometry_type not in ("cylinder", "circle"):
        fail("NX_INVALID_ARGUMENT", "radius filters cylinders or circles", field="/radius")
    if role is not None and (kind, geometry_type) != ("face", "cylinder"):
        fail("NX_INVALID_ARGUMENT", "role filters cylindrical faces only", field="/role")
    if lined and (kind, geometry_type) != ("edge", "line"):
        fail(
            "NX_INVALID_ARGUMENT",
            f"parallel filters line edges by direction, not {kind} {geometry_type}",
            field="/parallel",
            hint='Use kind "edge" (geometry_type "line" or "any"); planar faces filter by '
            "normal, circles by radius and near",
        )
    if order == "nearest" and near is None:
        fail(
            "NX_INVALID_ARGUMENT",
            "order nearest needs a near point: give near, or choose order highest or lowest",
            field="/near",
        )


def _owner(tx, owner, kinds=common.TARGET_KINDS):
    if owner is None:
        return None, None
    obj = resolve_owner(tx, owner, kinds)
    return obj, tx.kind_of(obj)


def _check_face_owner(owner_kind, kind) -> None:
    """A face owner gives its edges only."""
    if owner_kind == "face" and kind != "edge":
        fail(
            "NX_WRONG_KIND",
            f"A face owner lists its edges, not {PLURAL[kind]}",
            field="/owner",
            hint='Use kind "edge" for its edges; for faces, give its body or feature as owner',
        )


def _candidates(tx, kind, owner_obj):
    """Faces or edges of the owner (a feature: only its own; a face: its edges) or of every
    body in scope;
    for ``body``, the owner's bodies (a component's occurrences) or every body in scope."""
    if kind == "body":
        return bodies.of(tx, owner_obj) if owner_obj is not None else bodies.scope(tx, "auto")
    if owner_obj is not None and tx.kind_of(owner_obj) in ("feature", "face"):
        # Feature.GetFaces/GetEdges: the feature's own, not its bodies'; a face's edges.
        found = [owner_obj]
    elif owner_obj is not None:
        found = bodies.of(tx, owner_obj)
    else:
        found = bodies.scope(tx, "auto")
    entities: dict[int, Any] = {}
    for source in found:
        tx.checkpoint()
        for obj in source.GetFaces() if kind == "face" else source.GetEdges():
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
    if kind == "body":
        facts["type"] = "solid" if getattr(obj, "IsSolidBody", False) else "sheet"
    elif kind == "face":
        with native():
            data = tx.uf.Modeling.AskFaceData(obj.Tag)
        code, direction, sign = int(data[0]), data[2], data[6]
        facts["type"] = FACE_TYPES.get(code, "other")
        if code == select.FACE_PLANE:
            facts["normal"] = geom.unit(common.xyz3(direction))
        elif code == select.FACE_CYLINDER:
            facts.update(
                common.axial_facts(tx, obj, data), role="bore" if float(sign) < 0 else "boss"
            )
        elif code == FACE_CONE:
            facts.update(common.axial_facts(tx, obj, data))
    else:
        name = result.enum_name(obj.SolidEdgeType, tx.nx.Edge.EdgeType)
        facts["type"] = EDGE_TYPES.get(name, "other")
        if facts["type"] == "circle":
            with native():
                facts["radius"] = float(tx.uf.Curve.AskArcData(obj.Tag).Radius)
    return facts


def _line_direction(tx, edge) -> list[float] | None:
    """The unit direction of a line edge from its vertices (UF Eval when NX gives none);
    None when degenerate."""
    try:
        start, end = (geom.xyz(v) for v in edge.GetVertices())
    except Exception:
        found = kfacts.edge(tx, edge)
        start, end = found["start"], found["end"]
    delta = geom.sub(end, start)
    length = math.sqrt(geom.dot(delta, delta))
    return geom.scale(delta, 1.0 / length) if length > 1e-12 else None


def _along(tx, obj, parallel, tolerance) -> bool:
    """Whether a line edge runs along the axis ``parallel`` (either sense) or across the
    normal of the principal plane ``parallel``, within the angle tolerance of ``normal``
    (cos >= 1 - tolerance)."""
    found = _line_direction(tx, obj)
    if found is None:
        return False
    if parallel in PLANE_NORMALS:
        across = math.sqrt(max(0.0, 1 - (1 - tolerance) ** 2))
        return abs(found[AXES.index(PLANE_NORMALS[parallel])]) <= across
    return abs(found[AXES.index(parallel)]) >= 1 - tolerance


def search(
    tx, *, owner_obj, kind, geometry_type, normal, radius, near, order, axis, tolerance, role=None,
    parallel=None,
):  # fmt: skip
    """Ranked ``[(rank, obj, facts)]`` of every face or edge matching the rule."""
    want_normal = direction(normal) if normal is not None else None
    target = args.vec3(near, "/near") if near is not None else None
    ranked = []
    for obj in _candidates(tx, kind, owner_obj):
        facts = _describe(tx, obj, kind)
        if geometry_type != "any" and facts["type"] != geometry_type:
            continue
        if want_normal is not None and geom.dot(want_normal, facts["normal"]) < 1 - tolerance:
            continue
        if parallel is not None and not _along(tx, obj, parallel, tolerance):
            continue
        if radius is not None and abs(facts["radius"] - radius) > tolerance:
            continue
        if role is not None and facts.get("role") != role:
            continue
        with native():
            box = [float(v) for v in tx.uf.ModlGeneral.AskBoundingBox(obj.Tag)]
        facts["center"] = [(box[i] + box[i + 3]) / 2 for i in range(3)]
        if kind == "body":
            facts.update(min=box[:3], max=box[3:6])
        if target is not None:
            distance, on_object = curve.point_distance(tx, obj, target)
            facts.update(distance=distance, point=on_object)
        rank = facts["distance"] if order == "nearest" else facts["center"][AXES.index(axis)]
        ranked.append((rank, obj, facts))
    sign = -1.0 if order == "highest" else 1.0
    ranked.sort(key=lambda item: (sign * item[0], int(item[1].Tag)))
    return ranked


def _body_facts(tx, body, row):
    """A page row's ``volume`` (solids) and ``owner`` (an occurrence's component id)."""
    if row.get("type") == "solid":
        row["volume"] = measure.volume(tx, body)
    component = getattr(body, "OwningComponent", None) if getattr(body, "IsOccurrence", 0) else None
    if component is not None:
        row["owner"] = tx.ref(component, kind="component")["id"]


def _anchor_of(tx, obj, kind):
    if kind == "component":
        return f"@{_part_path(tx)}:{obj.JournalIdentifier}"
    if kind == "face":  # a persistent handle, as nx_create_anchor makes
        with native():
            return f"@{_part_path(tx)}:face={tx.uf.Tag.AskHandleFromTag(obj.Tag)}"
    return tx.ref(obj, kind=kind).get("anchor")


def _part_path(tx):
    return tx.services.part_path(tx.part)


@tool(
    target="assembly",
    effect="read",
    label="Find geometry",
    summary=lambda a, say: (
        f"Find {PLURAL[a.get('kind') or 'face']}: {a.get('geometry_type') or 'any'}"
    ),
    keywords=(
        "select face",
        "vertical edges",
        "edges by direction",
        "nearest face",
        "top face",
        "list edges",
        "edges in drawing view",
        "find bodies",
    ),
    requires=("work_part",),
    cost="light",
    example={"kind": "face", "normal": "+z", "order": "highest"},
)
def nx_find_geometry(
    tx: Tx,
    *,
    owner: FindOwnerRef | None = None,
    kind: Literal["face", "edge", "body"] = "face",
    geometry_type: GeometryType = "any",
    normal: FindNormal | None = None,
    radius: Annotated[float, Range(gt=0)] | None = None,
    role: Literal["bore", "boss"] | None = None,
    near: Point3 | None = None,
    order: Order | None = None,
    axis: Literal["x", "y", "z"] | None = None,
    parallel: Parallel | None = None,
    tolerance: Tolerance = DEFAULT_TOLERANCE,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> FindData:
    """Find faces, edges or bodies by type, normal, radius or role. Ranked nearest to near,
    else outermost along normal, else lowest along axis. Drawing-view edges:
    nx_list_view_edges."""
    common.note_exploded(tx)
    want = direction(normal) if normal is not None else None
    # Without an axis: the normal's main axis, else z; without an order: nearest to near,
    # else the outermost faces along the normal first, else the lowest.
    if axis is None:
        axis = cast(Any, AXES[max(range(3), key=lambda i: abs(want[i]))] if want else "z")
    along = AXES.index(str(axis))
    if order is None:
        if near is not None:
            order = "nearest"
        else:
            order = "highest" if want and want[along] > 0 else "lowest"
    lined = parallel is not None
    geometry_type = implied_type(kind, geometry_type, normal, radius, role, lined)
    _check_rule(kind, geometry_type, normal, radius, near, order, role, lined)
    owner_obj, owner_kind = _owner(tx, owner, FIND_OWNER_KINDS)
    _check_face_owner(owner_kind, kind)
    ranked = search(
        tx,
        owner_obj=owner_obj,
        kind=kind,
        geometry_type=geometry_type,
        normal=want,
        radius=radius,
        near=near,
        order=order,
        axis=axis,
        tolerance=tolerance,
        role=role,
        parallel=parallel,
    )
    selected, page = result.page(ranked, offset, limit)
    items = [
        cast(GeometryRow, {**tx.ref(obj, kind=kind), **facts})  # a row plus _describe's facts
        for _rank, obj, facts in selected
    ]
    if kind == "body":
        for row, (_rank, obj, _facts) in zip(items, selected, strict=True):
            _body_facts(tx, obj, row)
    rule: dict[str, Any] = {
        "part": _part_path(tx),
        "kind": kind,
        "geometry_type": geometry_type,
        "order": order,
        "axis": axis,
        "tolerance": tolerance,
    }
    if want is not None:
        rule["normal"] = [float(v) for v in want]
    if radius is not None:
        rule["radius"] = float(radius)
    if role is not None:
        rule["role"] = role
    if parallel is not None:
        rule["parallel"] = parallel
    if near is not None:
        rule["near"] = [float(v) for v in near]
    selector: dict[str, Any] | None = rule
    if owner_obj is not None:
        anchor = _anchor_of(tx, owner_obj, owner_kind)
        # Without a persistent owner anchor the rule cannot be re-run on the same owner.
        selector = dict(rule, owner=anchor, owner_kind=owner_kind) if anchor else None
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


class HoleEnd(TypedDict):
    point: list[float]
    diameter: float
    open: bool | None


class HoleGroup(TypedDict):
    group: int
    axis: list[float]
    axis_point: list[float]
    radii: list[float]
    diameters: list[float]
    form: str
    length: float | None
    through: bool | None
    ends: list[HoleEnd]
    faces: list[str]


class HolesData(TypedDict):
    items: list[HoleRow]
    groups: list[HoleGroup]
    page: dict[str, Any]


def _group_holes(tx, bores, tol):
    groups: list[dict] = []
    for face, found in bores:
        axis = found["axis"]
        for group in groups:
            delta = geom.sub(found["axis_point"], group["axis_point"])
            along = geom.dot(delta, group["axis"])
            radial = math.sqrt(max(0.0, geom.dot(delta, delta) - along * along))
            if geom.parallel(axis, group["axis"], 1e-8) and radial <= tol:
                break
        else:
            group = {"group": len(groups), "axis": axis, "axis_point": found["axis_point"]}
            group.update(radii=[], members=[])
            groups.append(group)
        group["members"].append((face, found))
        if found["radius"] not in group["radii"]:
            group["radii"].append(found["radius"])
    return groups


def _edge_positions(tx, edge, axis, origin):
    """Axial positions of an edge: its centre for an arc, else its end points."""
    found = kfacts.edge(tx, edge)
    points = [found["center"]] if found["kind"] == "arc" else [found["start"], found["end"]]
    return [geom.dot(geom.sub(p, origin), axis) for p in points]


def _end_state(tx, edges, members, out, radius, tol):
    """``(open, sink)`` of a hole end from the faces beyond its edges: open is True, False
    (a floor or a drill tip) or None (unknown); sink marks a widening cone (countersink)."""
    states, sink = set(), False
    for edge in edges:
        for face in edge.GetFaces():
            if int(face.Tag) in members:
                continue
            found = _describe(tx, face, "face")
            if found["type"] == "plane":
                along = geom.dot(found["normal"], out)
                states.add(True if along > 1e-6 else False if along < -1e-6 else None)
            elif found["type"] == "cone":
                widening = max(found["radii"] or [0.0]) > radius + tol
                sink = sink or widening
                states.add(widening)
            elif found["type"] == "cylinder":
                states.add(True)  # an outer cylinder or a crossing bore
            else:
                states.add(None)
    if False in states:
        return False, sink
    return (True if states == {True} else None), sink


def _hole_shape(tx, group, tol):
    """``length``, ``ends`` (entry first), ``through`` and ``form`` of one hole group."""
    axis, origin = group["axis"], group["axis_point"]
    members = {int(face.Tag) for face, _ in group["members"]}
    rings = []  # (axial position, radius, edge)
    for face, found in group["members"]:
        tx.checkpoint()
        for edge in face.GetEdges():
            for at in _edge_positions(tx, edge, axis, origin):
                rings.append((at, found["radius"], edge))
    shape: dict[str, Any] = {"length": None, "through": None, "ends": []}
    ends: list[tuple[dict, bool]] = []  # (end, widening cone beyond it)
    if rings:
        low, high = min(r[0] for r in rings), max(r[0] for r in rings)
        shape["length"] = high - low
        for at, sign in ((high, 1.0), (low, -1.0))[: 1 if high - low <= tol else 2]:
            here = [r for r in rings if abs(r[0] - at) <= tol]
            radius = max(r[1] for r in here)
            edges = list({int(r[2].Tag): r[2] for r in here}.values())
            state, sink = _end_state(tx, edges, members, geom.scale(axis, sign), radius, tol)
            point = geom.add(origin, geom.scale(axis, at))
            ends.append(({"point": point, "diameter": 2 * radius, "open": state}, sink))
        # The entry first: an open end, then the wider one (a counterbore's mouth).
        ends.sort(key=lambda e: (e[0]["open"] is not True, -e[0]["diameter"]))
        shape["ends"] = [end for end, _ in ends]
        opened = [end["open"] for end in shape["ends"]]
        shape["through"] = False if False in opened else True if opened == [True, True] else None
    radii = sorted(group["radii"], reverse=True)
    if len(radii) == 1:
        shape["form"] = "countersink" if any(sink for _, sink in ends) else "simple"
    elif len(radii) == 2 and ends and abs(ends[0][0]["diameter"] - 2 * radii[0]) <= 2 * tol:
        shape["form"] = "counterbore"
    else:
        shape["form"] = "stepped"
    return shape


@tool(
    target="assembly",
    effect="read",
    label="Find holes",
    summary=lambda a, say: "Find holes in " + (say("owner") if a.get("owner") else "the part"),
    keywords=("recognize holes", "bores", "hole recognition", "cylindrical holes", "drilled"),
    requires=("work_part",),
    cost="light",
    example={"diameter": 6, "through": True},
)
def nx_find_holes(
    tx: Tx,
    *,
    owner: OwnerRef | None = None,
    diameter: Length | None = None,
    through: Annotated[bool, Doc("Only through (true) or blind (false) holes")] | None = None,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> HolesData:
    """Find holes (bores grouped by axis) with diameters, form, length, through and ends
    (entry first). No thread inference."""
    common.note_exploded(tx)
    if diameter is not None and float(diameter) <= 0:
        fail("NX_INVALID_ARGUMENT", "diameter must be > 0", field="/diameter")
    owner_obj, _ = _owner(tx, owner)
    tol = DEFAULT_TOLERANCE
    bores = []
    for obj in _candidates(tx, "face", owner_obj):
        found = _describe(tx, obj, "face")
        if found["type"] == "cylinder" and found["role"] == "bore":
            with native():
                u0, u1, _v0, _v1 = (float(v) for v in tx.uf.Modeling.AskFaceUvMinmax(obj.Tag))
            found["full"] = abs(abs(u1 - u0) - 2 * math.pi) <= 1e-6
            bores.append((obj, found))
    groups = []
    for group in _group_holes(tx, bores, tol):
        if diameter is not None and not any(
            abs(2 * r - float(diameter)) <= 2 * tol for r in group["radii"]
        ):
            continue
        group.update(_hole_shape(tx, group, tol))
        if through is not None and group["through"] is not through:
            continue
        groups.append(group)
    ordered = [(face, found, g["group"]) for g in groups for face, found in g["members"]]
    selected, page = result.page(ordered, offset, limit)
    keys = ("radius", "axis", "axis_point", "full")
    items = [
        cast(HoleRow, {**tx.ref(face, kind="face"), **{k: found[k] for k in keys}, "group": group})
        for face, found, group in selected
    ]
    out_groups: list[HoleGroup] = [
        {
            "group": g["group"],
            "axis": g["axis"],
            "axis_point": g["axis_point"],
            "radii": g["radii"],
            "diameters": sorted((2 * r for r in g["radii"]), reverse=True),
            "form": g["form"],
            "length": g["length"],
            "through": g["through"],
            "ends": g["ends"],
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
    target="assembly",
    effect="read",
    label="Create anchor",
    summary="Anchor {object}",
    keywords=("persistent reference", "anchor", "handle", "stable id", "survive edits"),
    requires=("work_part",),
    cost="light",
    example={"object": "fc:12"},
)
def nx_create_anchor(
    tx: Tx,
    *,
    object: Annotated[str, RefTo("body", "face", "edge", "feature", "sketch")],  # noqa: A002
) -> AnchorData:
    """Make a persistent anchor for a body, face or edge (a feature or sketch: its row
    anchor). It survives edits that keep the entity, save and reopen; resolve it with
    nx_resolve_reference."""
    obj = select.one(tx, object, "body", "face", "edge", "feature", "sketch", field="/object")
    kind = tx.kind_of(obj)
    if kind in ("feature", "sketch"):
        row = tx.ref(obj, kind=kind, anchor=True)
        anchor = row.get("anchor")
        if not anchor:
            fail("NX_UNSUPPORTED_OPTION", f"{object} has no journal identifier to anchor",
                 field="/object")  # fmt: skip
        return {"object": row, "anchor": str(anchor)}
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
            hint=f"Call again with part={PurePath(str(path)).name!r}, the part it was made in",
        )


def _resolve_handle(tx, match, field="/anchor"):
    _same_part(tx, match["part"], field)
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
            field=field,
            hint="Select replacement geometry with nx_find_geometry",
        )
    return found, kind


def _resolve_jid(tx, anchor, field="/anchor"):
    match = _JID_ANCHOR.match(anchor)
    if not match:
        fail(
            "NX_INVALID_ARGUMENT",
            f"{anchor!r} is not an anchor: anchors look like '@<part>:face=<handle>' "
            "(nx_create_anchor) or '@<part>:EXTRUDE(2)' (a result row's anchor)",
            field=field,
        )
    _same_part(tx, match["part"], field)
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
        field=field,
        hint="List the objects again with nx_list_objects",
    )


def _component_by_jid(tx, jid):
    found = [c for c, _ in parts.components(tx.part) if str(c.JournalIdentifier) == jid]
    return found[0] if len(found) == 1 else None


def _parse_selector(text):
    if isinstance(text, dict):
        value: Any = text
    else:
        try:
            value = json.loads(text)
        except ValueError:
            value = None
    required = SELECTOR_KEYS - OPTIONAL_SELECTOR_KEYS
    if (
        not isinstance(value, dict)
        or not required <= set(value) <= SELECTOR_KEYS
        or value.get("kind") not in ("face", "edge", "body")
        or value.get("order") not in ("nearest", "highest", "lowest")
        or value.get("axis") not in ("x", "y", "z")
        or value.get("role") not in (None, "bore", "boss")
        or value.get("parallel") not in (None, *AXES, *PLANE_NORMALS)
    ):
        missing = sorted(required - set(value)) if isinstance(value, dict) else []
        extra = sorted(set(value) - SELECTOR_KEYS) if isinstance(value, dict) else []
        what = f" (missing {', '.join(missing)})" if missing else ""
        what += f" (unknown {', '.join(extra)})" if extra else ""
        ref = isinstance(text, str) and re.match(REF_PATTERN, text.strip())
        fail(
            "NX_INVALID_ARGUMENT",
            f"Not a selector{what}: pass the selector of an nx_find_geometry result unchanged, "
            "as text or as an object",
            field="/selector",
            hint=f'A ref goes in anchor: {{"anchor": "{text.strip()}"}}'
            if ref
            else "Selectors are not written by hand: run nx_find_geometry and pass its selector",
        )
    return value


def _selector_owner(tx, selector):
    anchor = selector.get("owner")
    if anchor is None:
        return None
    kind = selector.get("owner_kind")
    match = _HANDLE_ANCHOR.match(anchor)
    if match:
        obj, found_kind = _resolve_handle(tx, match, "/selector")
    else:
        obj, found_kind = _resolve_jid(tx, anchor, "/selector")
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
    selector: SelectorText | SelectorObject | None = None,
    anchor: AnchorText | None = None,
    tie_tolerance: Annotated[float, Range(gt=0), Doc("Rank gap that counts as a tie")] = 0.001,
) -> ResolveData:
    """Turn a selector from nx_find_geometry (passed unchanged) or an anchor into a current
    reference. A selector re-runs its rule and fails on a tie. anchor also takes a ref such
    as fc:12: it is checked and returned if still valid."""
    # A value given to the other field by mistake is still unambiguous: an anchor starts
    # with '@', a selector is a JSON object.
    if anchor is None and isinstance(selector, str) and selector.lstrip().startswith("@"):
        anchor, selector = selector.strip(), None
    if selector is None and anchor is not None and anchor.lstrip().startswith("{"):
        selector, anchor = anchor, None
    args.exactly_one(selector=selector, anchor=anchor)
    if anchor is not None:
        anchor = anchor.strip()
        if re.match(REF_PATTERN, anchor):
            obj = tx.resolve(anchor)
            return {"object": tx.ref(obj, kind=tx.kind_of(obj)), "via": "reference"}
        match = _HANDLE_ANCHOR.match(anchor)
        obj, kind = _resolve_handle(tx, match) if match else _resolve_jid(tx, anchor)
        return {"object": tx.ref(obj, kind=kind), "via": "anchor"}
    parsed = _parse_selector(selector)
    _same_part(tx, parsed["part"], "/selector")
    owner_obj = _selector_owner(tx, parsed)
    rule = {k: parsed.get(k) for k in ("normal", "radius", "near")}
    role = parsed.get("role")
    parallel = parsed.get("parallel")
    _check_rule(parsed["kind"], parsed["geometry_type"], order=parsed["order"], role=role,
                lined=parallel is not None, **rule)  # fmt: skip
    ranked = search(
        tx,
        owner_obj=owner_obj,
        kind=parsed["kind"],
        geometry_type=parsed["geometry_type"],
        order=parsed["order"],
        axis=parsed["axis"],
        tolerance=parsed["tolerance"],
        role=role,
        **rule,
        parallel=parallel,
    )
    if not ranked:
        fail("NX_NOT_FOUND", "No geometry satisfies the selector now", field="/selector")
    if len(ranked) > 1 and abs(ranked[0][0] - ranked[1][0]) <= tie_tolerance:
        candidates = [{**tx.ref(o, kind=parsed["kind"]), "rank": r} for r, o, _ in ranked[:2]]
        fail(
            "NX_AMBIGUOUS_REFERENCE",
            "The best two candidates tie; narrow the selector or lower tie_tolerance",
            field="/selector",
            details={"candidates": candidates},
        )
    rank, obj, _facts = ranked[0]
    return {"object": tx.ref(obj, kind=parsed["kind"]), "via": "selector", "rank": rank}
