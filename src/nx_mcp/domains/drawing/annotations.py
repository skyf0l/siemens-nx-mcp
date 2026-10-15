"""Notes, center marks and centerlines, surface-finish and weld symbols; moving, renaming
and refreshing annotations.

Leaders attach to an edge or face as shown in one drawing view: the selection point is a
model point on the edge (its midpoint) or inside the face, clear of holes. Every leader
is read back from a reopened builder.

NX 2406 findings encoded here:

- Center marks need a circle seen true-shape (along its axis); an axis centerline needs a
  cylinder or cone whose axis lies in the view plane. Both are refused before NX.
- NX keeps associative annotations (center marks, centerlines) where their geometry puts
  them: a move is read back and refused when NX ignored it.
- An omitted symbol origin is placed up and right of the leader point.
"""

from __future__ import annotations

import contextlib
import math
import re
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import hooks, tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Doc,
    Items,
    Name,
    Point3,
    Range,
    Ref,
    RefTo,
    SheetPoint,
    Text,
)
from nx_mcp.domains.drawing.common import (
    SheetArg,
    ViewArg,
    add_leader,
    annotation_kind,
    associated,
    check_leader,
    commit,
    default_origin,
    distance_to_line,
    geometry,
    leader_row,
    report_bounds,
    set_origin,
    sheet_of,
    true_shape,
    update,
    verify_origin,
)
from nx_mcp.kit import args, builders, curve, drawing, facts, geom, readback, result, select

MAX_LINES = 50
MAX_RA = 1000.0  # um
FINISHES = {
    "any": "Basic",
    "required": "MaterialRemovalRequired",
    "prohibited": "MaterialRemovalProhibited",
}
WELDS = {"fillet": "Fillet", "square_butt": "SquareButt", "v_butt": "VButt"}
SIDES = {"arrow": ("arrow",), "other": ("other",), "both": ("arrow", "other")}
AXIAL_FACES = (select.FACE_CYLINDER, select.FACE_CONE)
EdgeOrFace = Annotated[str, RefTo("edge", "face")]
MAX_LINE = 1024
NoteText = Annotated[
    Annotated[list[Annotated[str, Text(MAX_LINE, r"^[^\r\n]*$")]], Items(1, MAX_LINES)]
    | Annotated[str, Text(4096)],
    Doc("Lines, or one string split at newlines"),
]


def note_lines(text, field="/text"):
    """The lines of a note: a list as given, or a string split at its line breaks."""
    lines = text.splitlines() if isinstance(text, str) else list(text)
    if not 1 <= len(lines) <= MAX_LINES:
        fail("NX_INVALID_ARGUMENT", f"A note takes 1-{MAX_LINES} lines", field=field)
    if any(len(line) > MAX_LINE for line in lines):
        fail("NX_INVALID_ARGUMENT", f"A note line takes at most {MAX_LINE} characters",
             field=field)  # fmt: skip
    if not any(line.strip() for line in lines):
        fail("NX_INVALID_ARGUMENT", "text needs a non-blank line", field=field)
    return lines


class AnnotationData(TypedDict, total=False):
    annotation: result.Row
    annotations: list[result.Row]
    sheet: result.Row
    origin: list[float]
    origins: list[list[float]]
    bounds: list[float]
    text: list[str]
    leader: dict
    symbol: dict
    center: Annotated[list[float], Doc("bolt_circle: its center on the sheet")]
    radius: Annotated[float, Doc("bolt_circle: its radius in the model (part units)")]


def note_text_head(text):
    """The start of a note's first line (for summaries)."""
    lines = (text or "").splitlines() if isinstance(text, str) else list(text or [""])
    return (lines or [""])[0][:30]


def number_text(value):
    """Shortest exact decimal for a callout (3.2 -> '3.2', 5.0 -> '5')."""
    return f"{float(value):.6g}"


def _target_sheet(tx, sheet, view):
    """``(sheet, view or None)`` for a note: view's sheet, the given sheet or the current one."""
    if view is not None:
        obj, owner = drawing.view(tx, view)
        if sheet is not None and int(sheet_of(tx, sheet, "/sheet").Tag) != int(owner.Tag):
            fail("NX_WRONG_OWNER", "view is not on the given sheet", field="/view")
        return owner, obj
    if sheet is not None:
        return sheet_of(tx, sheet, "/sheet"), None
    current = drawing.current(tx.part)
    if current is None:
        fail("NX_WRONG_APPLICATION", "No drawing sheet is open: pass sheet",
             hint="Pass sheet, or open one with nx_activate_drawing_sheet")  # fmt: skip
    return current, None


# ---- note ---------------------------------------------------------------------------------


@tool(
    effect="model",
    label="Create note",
    summary=lambda a, say: (
        f"Note {note_text_head(a.get('text'))!r}"
        + (f" to {say('leader_to')}" if a.get("leader_to") else "")
    ),
    keywords=("note", "text", "label", "leader", "callout", "drafting note"),
    requires=("work_part", "no_active_sketch"),
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={"text": ["BREAK ALL EDGES"], "origin": [40, 30], "sheet": "sh:3"},
)
def nx_create_note(
    tx: Tx,
    *,
    text: NoteText,
    origin: SheetPoint,
    sheet: SheetArg | None = None,
    view: ViewArg | None = None,
    leader_to: EdgeOrFace | None = None,
) -> AnnotationData:
    """Place a drawing note on a sheet (default the open one).
    leader_to adds a leader to an edge or face as shown in view."""
    text = note_lines(text)
    if (leader_to is None) != (view is None):
        fail("NX_INVALID_ARGUMENT", "leader_to and view go together", field="/view")
    with tx.checks():
        target, shown = _target_sheet(tx, sheet, view)
        obj = geometry(tx, leader_to, ("edge", "face"), "/leader_to") if leader_to else None
    A = tx.nx.Annotations
    part = tx.part
    with drawing.on_sheet(tx, target, part) as sh:
        with builders.build(
            tx, part.Annotations.CreateDraftingNoteBuilder, A.SimpleDraftingAid.Null
        ) as b:
            b.Origin.Anchor = A.OriginBuilder.AlignmentPosition.MidLeft
            b.Text.TextBlock.SetText(list(text))
            anchor = add_leader(tx, b.Leader, obj, shown) if obj is not None else None
            set_origin(tx, b.Origin, sh, origin)
            note = commit(b, "note")
        update(tx)
        stored = [str(s) for s in note.GetText()]
        readback.expect(stored == list(text), "text", "NX stored other note text", text=stored)
        out: AnnotationData = {
            "annotation": tx.ref(note, "annotation"),
            "sheet": tx.ref(target, "drawing_sheet"),
            "origin": verify_origin(sh, note, origin),
            "text": stored,
        }
        if obj is not None:
            with builders.build(tx, part.Annotations.CreateDraftingNoteBuilder, note) as reader:
                check_leader(reader, obj, shown)
            out["leader"] = leader_row(tx, obj, shown, anchor)
        return report_bounds(tx, out, note, sh, "note")


# ---- center marks and centerlines ---------------------------------------------------------


@tool(
    effect="model",
    label="Create centerline",
    summary={
        "center_mark": "Center marks on {objects} in {view}",
        "axis": "Axis centerline in {view}",
        "between_edges": "Centerline between 2 edges in {view}",
        "bolt_circle": "Bolt circle through {objects} in {view}",
        "symmetrical": "Symmetrical centerline on {objects} in {view}",
        "automatic": "Automatic center marks in {view}",
    },
    keywords=(
        "center mark",
        "centerline",
        "axis line",
        "symmetry line",
        "hole center",
        "bolt circle",
        "symmetrical centerline",
        "PCD",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    variants="kind",
    required_by={
        "center_mark": ("objects",),
        "axis": ("objects",),
        "between_edges": ("objects",),
        "bolt_circle": ("objects",),
        "symmetrical": ("objects",),
    },
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={"view": "v:7", "kind": "center_mark", "objects": ["e:21"]},
)
def nx_create_centerline(
    tx: Tx,
    *,
    view: ViewArg,
    kind: Literal[
        "center_mark", "axis", "between_edges", "bolt_circle", "symmetrical", "automatic"
    ],
    objects: Annotated[
        list[EdgeOrFace],
        Items(1, 50),
        Doc(
            "center_mark: circular edges; axis: 1 cylindrical/conical face; between_edges: 2 "
            "lines; bolt_circle: 3+ hole edges"
        ),
    ]
    | None = None,
) -> AnnotationData:
    """Create center marks, an axis line, a centerline between edges or a bolt circle. Kinds:
    center_mark on circular edges; axis on a cylindrical or conical face; between_edges on two
    edges that look parallel; bolt_circle through 3+ hole edges on one circle (center, radius
    read back); symmetrical (symmetry marks at its ends) on 1 face like axis or between the
    centers of 2 circular edges; automatic (no objects) marks each hole seen along its axis
    that has no mark. Circles must be seen along their axis."""
    if kind == "automatic":
        if objects is not None:
            fail("NX_INVALID_ARGUMENT", "automatic finds the holes itself: omit objects",
                 field="/objects")  # fmt: skip
        return _automatic(tx, view)
    objects = args.required(objects, "objects")
    args.distinct("/objects", objects, "objects")
    wanted = {"axis": 1, "between_edges": 2}.get(kind)
    if wanted and len(objects) != wanted:
        fail("NX_INVALID_ARGUMENT", f"{kind} takes {wanted} object(s)", field="/objects")
    if kind == "bolt_circle" and len(objects) < 3:
        fail("NX_INVALID_ARGUMENT", "bolt_circle takes 3 or more hole edges", field="/objects")
    if kind == "center_mark":
        return _center_marks(tx, view, objects)
    if kind == "bolt_circle":
        return _bolt_circle(tx, view, objects)
    if kind == "symmetrical":
        return _symmetrical(tx, view, objects)
    return _centerline(tx, view, kind, objects)


def _center_marks(tx, view_ref, refs):
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        edges = [geometry(tx, r, ("edge",), f"/objects/{i}") for i, r in enumerate(refs)]
        arcs = []
        for i, edge in enumerate(edges):
            f = facts.edge(tx, edge)
            if f["kind"] != "arc":
                fail("NX_WRONG_GEOMETRY", "Center marks need circular edges",
                     field=f"/objects/{i}")  # fmt: skip
            true_shape(view, f, f"/objects/{i}")
            arcs.append(f)
        existing = marks_in_view(tx, view, edges)
    A = tx.nx.Annotations
    part = tx.part
    tags = [int(e.Tag) for e in edges]
    marks: list = [None] * len(edges)
    origins: list = [None] * len(edges)
    new = [e for e in edges if int(e.Tag) not in existing]
    before = {int(c.Tag) for c in part.Annotations.Centerlines}
    with drawing.on_sheet(tx, sheet, part) as sh:
        for i, tag in enumerate(tags):
            if tag in existing:
                marks[i] = tx.ref(existing[tag], "annotation")
                origins[i] = sh.to_sheet(existing[tag].AnnotationOrigin)
        if len(new) < len(edges):
            tx.warn("NX_NO_EFFECT", f"{len(edges) - len(new)} of the edges already had a center "
                    "mark in this view: the existing mark is returned, no second one is made")  # fmt: skip
        if not new:
            return {"annotations": marks, "sheet": tx.ref(sheet, "drawing_sheet"),
                    "origins": origins}  # fmt: skip
        centers = [drawing.to_sheet(tx, view, f["center"]) for f in arcs]
        factory = part.Annotations.Centerlines.CreateCenterMarkBuilder
        with builders.build(tx, factory, A.CenterMark.Null) as b:
            b.MultipleCenterMarks = len(new) > 1
            for edge in new:
                b.Locations.Add(edge, view, tx.nx.Point3d(*curve.point_at(tx, edge, 0.5)))
            commit(b, "center mark")
        update(tx)
        created = [c for c in part.Annotations.Centerlines if int(c.Tag) not in before]
        readback.expect(len(created) == len(new), "count", created=len(created))
        for obj in created:
            index = next(
                (tags.index(int(o.Tag)) for o in associated(obj) if int(o.Tag) in tags), None
            )
            position = sh.to_sheet(obj.AnnotationOrigin)
            readback.expect(
                index is not None
                and marks[index] is None
                and geom.dist([*position, 0.0], [*centers[index], 0.0])
                <= 1e-3 * max(1.0, *map(abs, centers[index])),
                "center",
                "A center mark is not at its edge's center",
                center=position,
            )
            marks[index] = tx.ref(obj, "annotation")
            origins[index] = position
        return {
            "annotations": marks,
            "sheet": tx.ref(sheet, "drawing_sheet"),
            "origins": origins,
        }


def circle_through(points):
    """``(center, radius)`` of the circle through three 2D points, or None when they are
    collinear."""
    (ax, ay), (bx, by), (cx, cy) = points[:3]
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    span = max(abs(v) for v in (ax - bx, ay - by, ax - cx, ay - cy, bx - cx, by - cy))
    if abs(d) <= 1e-9 * max(1.0, span) ** 2:
        return None
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return [ux, uy], ((ax - ux) ** 2 + (ay - uy) ** 2) ** 0.5


def _bolt_circle(tx, view_ref, refs):
    """A full bolt-circle centerline through the centers of 3+ holes (NX ``Through3Points``),
    read back with UF ``Drf.AskCenterlineInfo`` (its arc: center and radius on the sheet)."""
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        edges = [geometry(tx, r, ("edge",), f"/objects/{i}") for i, r in enumerate(refs)]
        arcs = []
        for i, edge in enumerate(edges):
            f = facts.edge(tx, edge)
            if f["kind"] != "arc":
                fail("NX_WRONG_GEOMETRY", "A bolt circle needs circular hole edges",
                     field=f"/objects/{i}")  # fmt: skip
            true_shape(view, f, f"/objects/{i}")
            arcs.append(f)
        centers = [drawing.to_sheet(tx, view, f["center"]) for f in arcs]
        circle = circle_through(centers)
        if circle is None:
            fail("NX_INVALID_ARGUMENT", "The first three hole centers lie on one line in the "
                 "view: a bolt circle needs them on a circle", field="/objects")  # fmt: skip
        center, radius = circle
        for i, c in enumerate(centers[3:], start=3):
            off = abs(((c[0] - center[0]) ** 2 + (c[1] - center[1]) ** 2) ** 0.5 - radius)
            if off > 1e-4 * max(1.0, radius):
                fail("NX_INVALID_ARGUMENT", f"Hole {i} lies {off:.4g} off the circle through "
                     "the first three holes", field=f"/objects/{i}")  # fmt: skip
    A = tx.nx.Annotations
    part = tx.part
    with drawing.on_sheet(tx, sheet, part) as sh:
        factory = part.Annotations.Centerlines.CreateBoltCircleCenterlineBuilder
        with builders.build(tx, factory, A.BoltCircleCenterline.Null) as b:
            b.Type = A.CircleCenterlineBuilder.Types.Through3Points
            b.FullCircle = True
            for edge in edges:
                b.Locations.Add(edge, view, tx.nx.Point3d(*curve.point_at(tx, edge, 0.5)))
            obj = commit(b, "bolt circle")
        update(tx)
        _form, _point, info = tx.uf.Drf.AskCenterlineInfo(obj.Tag)
        arc = list(info.ArcInfo)[0]
        got_center = sh.to_sheet(list(arc.ArcCenter))
        got_radius = float(arc.Radius) / sh.scale
        tol = 1e-4 * max(1.0, radius, *map(abs, center))
        readback.expect(
            geom.dist([*got_center, 0.0], [*center, 0.0]) <= tol
            and abs(got_radius - radius) <= tol,
            "circle",
            "The bolt circle is not the circle through the hole centers",
            center=got_center,
            radius=got_radius,
        )
        scale = drawing.scale(tx, view)
        return {
            "annotation": tx.ref(obj, "annotation"),
            "sheet": tx.ref(sheet, "drawing_sheet"),
            "origin": got_center,
            "center": got_center,
            "radius": got_radius / scale * sh.scale,
        }


def marks_in_view(tx, view, edges):
    """``{edge tag: center mark}`` of the edges that already have a center mark in ``view``
    (NX would add a second one). Marks NX made with the view are associated with the
    view's drafting curve of the edge, ours with the edge itself."""
    tags = {int(e.Tag) for e in edges}
    found = {}
    for mark in tx.part.Annotations.Centerlines:
        if type(mark).__name__ != "CenterMark":
            continue
        for i in range(1, int(getattr(mark, "NumberOfAssociativities", 0) or 0) + 1):
            with contextlib.suppress(Exception):  # an associativity NX cannot report
                assoc = mark.GetAssociativity(i)
                seen = getattr(assoc, "ObjectView", None)
                if seen is None or int(seen.Tag) != int(view.Tag):
                    continue
                for tag in _edge_tags(tx, assoc.FirstObject) & tags:
                    found.setdefault(tag, mark)
    return found


def _edge_tags(tx, obj):
    """Model edge tags behind an associated object: an edge, or a drafting curve's parents."""
    if obj is None:
        return set()
    if facts.is_edge(obj):
        return {int(obj.Tag)}
    try:
        return {int(t) for t in tx.uf.Draw.AskDraftingCurveParents(obj.Tag)[1] or []}
    except Exception:  # not a drafting curve
        return set()


def _projected(tx, view, point, direction):
    """``(sheet point, sheet direction)`` of a model axis in ``view``; the direction is
    about zero when the axis points out of the view."""
    a = drawing.to_sheet(tx, view, point)
    b = drawing.to_sheet(tx, view, [p + d * 100.0 for p, d in zip(point, direction, strict=True)])
    return a, [b[0] - a[0], b[1] - a[1]]


def _axis_line(tx, view, sheet, face, field):
    code, point, direction = select.face_data(tx, face)
    if code not in AXIAL_FACES:
        fail("NX_WRONG_GEOMETRY", "An axis centerline needs a cylindrical or conical face",
             field=field)  # fmt: skip
    a, projected = _projected(tx, view, point, direction)
    if abs(projected[0]) + abs(projected[1]) < 1e-3:
        side = []  # views of the sheet that show the axis in their plane
        for other in sheet.GetDraftingViews():
            with contextlib.suppress(Exception):  # a view NX cannot map points into
                seen = _projected(tx, other, point, direction)[1]
                if abs(seen[0]) + abs(seen[1]) >= 1e-3:
                    side.append(tx.ref(other, "drawing_view"))
        fail(
            "NX_UNSUPPORTED_GEOMETRY",
            "The face axis points out of this view: use a center mark on its circular edge "
            "here, or an axis in a view that shows the cylinder from the side",
            field=field,
            hint="Views on this sheet that show the axis: details.views"
            if side
            else "Add a projected view beside this one with nx_create_drawing_view",
            details={"views": side},
        )
    return a, projected


def _centerline(tx, view_ref, kind, refs):
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        if kind == "axis":
            targets = [geometry(tx, refs[0], ("face",), "/objects/0")]
            line_point, line_direction = _axis_line(tx, view, sheet, targets[0], "/objects/0")
        else:
            targets = [geometry(tx, r, ("edge",), f"/objects/{i}") for i, r in enumerate(refs)]
            ends = []
            for i, edge in enumerate(targets):
                f = facts.edge(tx, edge)
                if f["kind"] != "line":
                    fail("NX_WRONG_GEOMETRY", "between_edges needs two straight edges",
                         field=f"/objects/{i}")  # fmt: skip
                ends.append([drawing.to_sheet(tx, view, f["start"]),
                             drawing.to_sheet(tx, view, f["end"])])  # fmt: skip
            (a0, a1), (b0, b1) = ends
            da, db = [a1[0] - a0[0], a1[1] - a0[1]], [b1[0] - b0[0], b1[1] - b0[1]]
            la, lb = (da[0] ** 2 + da[1] ** 2) ** 0.5, (db[0] ** 2 + db[1] ** 2) ** 0.5
            cross = abs(da[0] * db[1] - da[1] * db[0])
            if la == 0 or lb == 0 or cross > 1e-4 * la * lb or distance_to_line(b0, a0, da) < 1e-6:
                fail("NX_INVALID_ARGUMENT", "The edges must look parallel and distinct in the view",
                     field="/objects")  # fmt: skip
            line_point = [(a0[i] + a1[i] + b0[i] + b1[i]) / 4 for i in (0, 1)]
            line_direction = da
    A = tx.nx.Annotations
    part = tx.part
    collection = part.Annotations.Centerlines
    with drawing.on_sheet(tx, sheet, part) as sh:
        if kind == "axis":
            factory, null = collection.CreateCenterline3dBuilder, A.Centerline3d.Null
        else:
            factory, null = collection.CreateCenterline2dBuilder, A.Centerline2d.Null
        with builders.build(tx, factory, null) as b:
            if kind == "axis":
                face = targets[0]
                b.Face.Add(face, view, tx.nx.Point3d(*facts.face_point(tx, face)))
            else:
                b.Type = A.Centerline2dBuilder.Types.FromCurves
                for side, edge in zip((b.Side1, b.Side2), targets, strict=True):
                    side.SetValue(edge, view, tx.nx.Point3d(*curve.point_at(tx, edge, 0.5)))
            obj = commit(b, "centerline")
        update(tx)
        position = sh.to_sheet(obj.AnnotationOrigin)
        offset = distance_to_line(position, line_point, line_direction)
        readback.expect(
            offset <= 1e-3 * max(1.0, abs(position[0]), abs(position[1])),
            "axis",
            "The centerline is not on the expected axis",
            origin=position,
            offset=offset,
        )
        return {
            "annotation": tx.ref(obj, "annotation"),
            "sheet": tx.ref(sheet, "drawing_sheet"),
            "origin": position,
        }


def _symmetrical(tx, view_ref, refs):
    """A symmetrical centerline (``SymmetricalCenterlineBuilder``): ``FromFace`` on a
    cylindrical or conical face seen from the side (NX puts its origin on the projected
    axis), or ``StartAndEnd`` between two circle centers (origin at their midpoint)."""
    if len(refs) > 2:
        fail("NX_INVALID_ARGUMENT", "symmetrical takes 1 face or 2 circular edges",
             field="/objects")  # fmt: skip
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        if len(refs) == 1:
            targets = [geometry(tx, refs[0], ("face",), "/objects/0")]
            line_point, line_direction = _axis_line(tx, view, sheet, targets[0], "/objects/0")
        else:
            targets = [geometry(tx, r, ("edge",), f"/objects/{i}") for i, r in enumerate(refs)]
            centers = []
            for i, edge in enumerate(targets):
                f = facts.edge(tx, edge)
                if f["kind"] != "arc":
                    fail("NX_WRONG_GEOMETRY", "symmetrical takes 2 circular edges or 1 face",
                         field=f"/objects/{i}")  # fmt: skip
                true_shape(view, f, f"/objects/{i}")
                centers.append(drawing.to_sheet(tx, view, f["center"]))
            span = max(1.0, *map(abs, centers[0]))
            if geom.dist([*centers[0], 0.0], [*centers[1], 0.0]) <= 1e-6 * span:
                fail("NX_INVALID_ARGUMENT", "The two circles share their center in this view",
                     field="/objects")  # fmt: skip
            midpoint = [(centers[0][i] + centers[1][i]) / 2 for i in (0, 1)]
    A = tx.nx.Annotations
    part = tx.part
    factory = part.Annotations.Centerlines.CreateSymmetricalCenterlineBuilder
    with drawing.on_sheet(tx, sheet, part) as sh:
        with builders.build(tx, factory, A.SymmetricalCenterline.Null) as b:
            types = A.SymmetricalCenterlineBuilder.Types
            if len(targets) == 1:
                b.Type = types.FromFace
                face = targets[0]
                b.Face.SetValue(face, view, tx.nx.Point3d(*facts.face_point(tx, face)))
            else:
                b.Type = types.StartAndEnd
                for selection, edge in zip((b.Start, b.End), targets, strict=True):
                    selection.SetValue(edge, view, tx.nx.Point3d(*curve.point_at(tx, edge, 0.5)))
            obj = commit(b, "symmetrical centerline")
        update(tx)
        position = sh.to_sheet(obj.AnnotationOrigin)
        if len(targets) == 1:
            offset = distance_to_line(position, line_point, line_direction)
        else:
            offset = geom.dist([*position, 0.0], [*midpoint, 0.0])
        readback.expect(
            offset <= 1e-3 * max(1.0, abs(position[0]), abs(position[1])),
            "axis",
            "The centerline is not on the expected axis",
            origin=position,
            offset=offset,
        )
        return {
            "annotation": tx.ref(obj, "annotation"),
            "sheet": tx.ref(sheet, "drawing_sheet"),
            "origin": position,
        }


def _automatic(tx, view_ref):
    """NX's automatic centerlines (``AutomaticCenterlineBuilder``) in one view: a center
    mark on each hole seen along its axis that has none yet. Its ``Commit`` returns no
    object: the new marks are the centerlines that were not there before."""
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
    part = tx.part
    collection = part.Annotations.Centerlines
    before = {int(c.Tag) for c in collection}
    with drawing.on_sheet(tx, sheet, part) as sh:
        with builders.build(tx, collection.CreateAutomaticCenterlineBuilder) as b:
            b.Views.Add(view)
            b.commit(validate=True)
        update(tx)
        created = [c for c in collection if int(c.Tag) not in before]
        if not created:
            tx.warn("NX_NO_EFFECT", "NX found no hole without a center mark in this view: "
                    "holes need to be seen along their axis")  # fmt: skip
        return {
            "annotations": [tx.ref(c, "annotation") for c in created],
            "sheet": tx.ref(sheet, "drawing_sheet"),
            "origins": [sh.to_sheet(c.AnnotationOrigin) for c in created],
        }


# ---- symbols -------------------------------------------------------------------------------


@tool(
    effect="model",
    label="Create drawing symbol",
    summary={
        "surface_finish": "Surface finish Ra {roughness_ra} on {object}",
        "weld": "Weld symbol on {object}",
    },
    keywords=("surface finish", "roughness", "weld symbol", "fillet weld", "Ra"),
    requires=("work_part", "no_active_sketch"),
    variants="symbol",
    required_by={"surface_finish": ("roughness_ra",), "weld": ("weld", "size")},
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={
        "view": "v:7",
        "object": "fc:3",
        "symbol": "surface_finish",
        "roughness_ra": 3.2,
    },
)
def nx_create_drawing_symbol(
    tx: Tx,
    *,
    view: ViewArg,
    object: EdgeOrFace,  # noqa: A002
    symbol: Literal["surface_finish", "weld"],
    origin: SheetPoint | None = None,
    roughness_ra: Annotated[
        float | Annotated[str, Text(24)],  # range checked by roughness()
        Doc("Ra in um: 3.2 or '3.2 um'"),
    ]
    | None = None,
    removal: Literal["any", "required", "prohibited"] = "required",
    weld: Literal["fillet", "square_butt", "v_butt"] | None = None,
    size: Annotated[float, Range(gt=0, le=1000)] | None = None,
    side: Literal["arrow", "other", "both"] = "arrow",
) -> AnnotationData:
    """Place a surface-finish or weld symbol with a leader to an edge or face shown in a view.
    origin defaults to beside the leader."""
    if symbol == "weld":
        args.need(weld=weld, size=size)
    else:
        args.need(roughness_ra=roughness_ra)
        roughness_ra = roughness(roughness_ra)
    with tx.checks():
        shown, sheet = drawing.view(tx, view)
        obj = geometry(tx, object, ("edge", "face"), "/object")
    if symbol == "weld":
        return _weld(tx, shown, sheet, obj, origin, {"type": weld, "size": size, "side": side})
    return _finish(
        tx, shown, sheet, obj, origin, {"roughness_ra": roughness_ra, "removal": removal}
    )


_RA = re.compile(
    r"^\s*(?:ra\s*=?\s*)?(\d+(?:\.\d*)?|\.\d+)\s*(?:um|\u00b5m|\u03bcm|microns?|micrometers?|"
    r"micrometres?)?\s*$",
    re.IGNORECASE,
)


def roughness(value, field="/roughness_ra"):
    """Ra in micrometres from 3.2, '3.2', '3.2 um' or 'Ra 3.2'."""
    if isinstance(value, str):
        match = _RA.match(value)
        if match is None:
            fail("NX_INVALID_ARGUMENT", f"roughness_ra {value!r} is not an Ra in micrometres",
                 field=field, hint="Send a number in um, such as 3.2 or '3.2 um'")  # fmt: skip
        value = float(match.group(1))
    if not 0 < float(value) <= MAX_RA:
        fail("NX_INVALID_ARGUMENT", f"roughness_ra must be > 0 and <= {MAX_RA:g} um",
             field=field)  # fmt: skip
    return float(value)


def _finish(tx, view, sheet, obj, origin, spec):
    A = tx.nx.Annotations
    kinds = A.BaseSurfaceFinishSymbolBuilder.SymbolType
    native = FINISHES[spec.get("removal", "required")]
    text = "Ra " + number_text(spec["roughness_ra"])
    collection = tx.part.Annotations.DraftingSurfaceFinishSymbol
    factory = collection.CreateDraftingSurfaceFinishSymbolBuilder
    with drawing.on_sheet(tx, sheet) as sh:
        with builders.build(tx, factory, A.DraftingSurfaceFinishSymbol.Null) as b:
            b.Finish = getattr(kinds, native)
            b.A2 = text
            anchor = add_leader(tx, b.Leader, obj, view)
            origin = origin if origin is not None else default_origin(sh, anchor, view)
            set_origin(tx, b.Origin, sh, origin)
            symbol = commit(b, "surface finish symbol")
        update(tx)
        with builders.build(tx, factory, symbol) as reader:
            stored = {
                "finish": result.enum_name(reader.Finish, kinds, list(FINISHES.values())),
                "text": str(reader.A2),
            }
            check_leader(reader, obj, view)
        readback.expect(stored == {"finish": native, "text": text}, "symbol", **stored)
        out: AnnotationData = {
            "annotation": tx.ref(symbol, "annotation"),
            "sheet": tx.ref(sheet, "drawing_sheet"),
            "origin": verify_origin(sh, symbol, origin),
            "text": [text],
            "leader": leader_row(tx, obj, view, anchor),
            "symbol": {
                "type": "surface_finish",
                "roughness_ra": float(spec["roughness_ra"]),
                "removal": next(k for k, v in FINISHES.items() if v == stored["finish"]),
            },
        }
        return report_bounds(tx, out, symbol, sh, "symbol")


def _weld(tx, view, sheet, obj, origin, spec):
    A = tx.nx.Annotations
    symbols = A.LineWeldDataBuilder.WeldSymbolType
    size = number_text(spec["size"])
    sides = SIDES[spec["side"]]
    factory = tx.part.Annotations.Welds.CreateLineWeldBuilder
    with drawing.on_sheet(tx, sheet) as sh:
        with builders.build(tx, factory, A.LineWeld.Null) as b:
            for name in sides:
                data = b.ArrowSideWeldData if name == "arrow" else b.OtherSideWeldData
                data.WeldSymbol = getattr(symbols, WELDS[spec["type"]])
                data.WeldSize = size
            anchor = add_leader(tx, b.Leader, obj, view)
            origin = origin if origin is not None else default_origin(sh, anchor, view)
            set_origin(tx, b.Origin, sh, origin)
            weld = commit(b, "weld symbol")
        update(tx)
        with builders.build(tx, factory, weld) as reader:
            stored = {
                name: {
                    "symbol": result.enum_name(data.WeldSymbol, symbols, [*WELDS.values(), "None"]),
                    "size": str(data.WeldSize),
                }
                for name, data in (
                    ("arrow", reader.ArrowSideWeldData),
                    ("other", reader.OtherSideWeldData),
                )
            }
            check_leader(reader, obj, view)
        wanted = {
            name: {
                "symbol": WELDS[spec["type"]] if name in sides else "None",
                "size": size if name in sides else "",
            }
            for name in ("arrow", "other")
        }
        readback.expect(stored == wanted, "symbol", "NX stored another weld symbol",
                        actual=stored)  # fmt: skip
        out: AnnotationData = {
            "annotation": tx.ref(weld, "annotation"),
            "sheet": tx.ref(sheet, "drawing_sheet"),
            "origin": verify_origin(sh, weld, origin),
            "text": [size],
            "leader": leader_row(tx, obj, view, anchor),
            "symbol": {
                "type": "weld",
                "weld": spec["type"],
                "size": float(spec["size"]),
                "side": spec["side"],
            },
        }
        return report_bounds(tx, out, weld, sh, "symbol")


# ---- edit and refresh -----------------------------------------------------------------------

AnyAnnotation = Annotated[str, RefTo("annotation", "dimension", "drawing_table")]


def _is_note(tx, obj):
    """A drafting note (nx_create_note), not a PMI note, label or other annotation."""
    return type(obj).__name__ == "Note" or int(obj.Tag) in {int(n.Tag) for n in tx.part.Notes}


class EditData(TypedDict, total=False):
    annotation: result.Row
    name: str
    position: list[float]
    frame: str
    text: list[str]
    bounds: list[float]


@tool(
    switch="work",
    effect="model",
    label="Edit annotation",
    summary=lambda a, say: f"Edit {say('annotation')}",
    keywords=("move annotation", "rename annotation", "edit note text", "move note", "balloon"),
    requires=("work_part", "no_active_sketch"),
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={"annotation": "a:5", "position": [200, 120]},
)
def nx_edit_annotation(
    tx: Tx,
    *,
    annotation: AnyAnnotation,
    position: SheetPoint | Point3 | None = None,
    name: Name | None = None,
    text: NoteText | None = None,
) -> EditData:
    """Move or rename an annotation, dimension or table, or replace a note's text. position:
    sheet [x, y], or [x, y, z] for PMI; a move NX ignores (associative marks) is refused."""
    if position is None and name is None and text is None:
        fail("NX_INVALID_ARGUMENT", "Give position, name or text")
    lines = note_lines(text) if text is not None else None
    with tx.checks():
        obj = select.resolve(
            tx,
            annotation,
            "annotation",
            "dimension",
            "drawing_table",
            field="/annotation",
            work_part=True,
        )
        if lines is not None and not _is_note(tx, obj):
            dimension = type(obj).__name__.endswith("Dimension")
            fail("NX_UNSUPPORTED_OPTION", "Only notes take new text", field="/text",
                 hint="Add text before or after a dimension's value (REF, 2X) with "
                 "nx_edit_dimension appended" if dimension else
                 "Recreate the annotation with its create tool")  # fmt: skip
        sheet = drawing.sheet_of_annotation(tx, obj)
        anchor = drawing.anchor_of(tx, obj)
        if position is not None:
            if not hasattr(anchor, "AnnotationOrigin"):
                fail("NX_UNSUPPORTED_OPTION", "This object has no position", field="/position")
            if len(position) != (2 if sheet is not None else 3):
                fail("NX_INVALID_ARGUMENT",
                     "Use [x, y] for a sheet annotation, [x, y, z] for PMI",
                     field="/position")  # fmt: skip
    out: EditData = {}
    if lines is not None:
        factory = tx.part.Annotations.CreateDraftingNoteBuilder
        with builders.build(tx, factory, obj) as b:
            b.Text.TextBlock.SetText(lines)
            commit(b, "note")
        update(tx)
        stored = [str(line) for line in obj.GetText()]
        readback.expect(stored == lines, "text", "NX stored other note text", text=stored)
        out["text"] = stored
    if name is not None:
        out["name"] = result.rename(tx, obj, name)
    if position is not None:
        if sheet is not None:
            sh = drawing.Sheet(tx, sheet)
            anchor.AnnotationOrigin = sh.point(position)
        else:
            anchor.AnnotationOrigin = tx.nx.Point3d(*map(float, position))
        update(tx)
        if sheet is not None:
            actual = sh.to_sheet(anchor.AnnotationOrigin)
            out["frame"] = "sheet"
        else:
            actual = geom.xyz(anchor.AnnotationOrigin)
            out["frame"] = "work_part_absolute"
        tol = 1e-6 * max(1.0, *map(abs, position))
        if any(abs(a - float(b)) > tol for a, b in zip(actual, position, strict=True)):
            # Associative annotations (center marks, centerlines) stay on their geometry.
            fail(
                "NX_UNSUPPORTED_OPTION",
                "NX keeps this annotation where its associated geometry places it",
                field="/position",
                details={"requested": list(position), "actual": actual},
            )
        out["position"] = actual
    else:
        update(tx)
    out["annotation"] = tx.ref(obj, annotation_kind(tx, obj))
    if sheet is not None and (position is not None or lines is not None):
        report_bounds(tx, out, obj, drawing.Sheet(tx, sheet), annotation_kind(tx, obj))
    return out


class RefreshData(TypedDict, total=False):
    refreshed: list[result.Row]
    skipped: list[dict]
    note: str


NOT_MANAGED = (
    "Not managed: notes follow the model on their own; dimensions and views of a displayed "
    "part are up to date already, others refresh with their view"
)
DRAWING_KINDS = ("annotation", "dimension", "drawing_table", "drawing_view", "drawing_sheet")


@tool(
    effect="model",
    label="Update annotations",
    summary=lambda a, say: (
        f"Update {len(a['objects'])} annotations" if a.get("objects") else "Update all annotations"
    ),
    keywords=(
        "refresh",
        "update parts list",
        "bend table",
        "detail boundary",
        "BOM update",
        "out of date views",
    ),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    invalidates="deleted",
    example={},
)
def nx_update_annotations(
    tx: Tx,
    *,
    objects: Annotated[list[Ref], Items(1, 100)] | None = None,
) -> RefreshData:
    """Refresh managed annotations from the model: parts lists, bend tables, title blocks,
    detail boundaries and callouts. Also updates out-of-date views; dimensions whose value
    changed are listed as modified. Model edits of a displayed part refresh them already; call
    it after editing a part that was not displayed. Omit objects to refresh all of them;
    given objects nothing refreshes come back in skipped with a reason."""
    live = (
        [select.resolve(tx, r, field=f"/objects/{i}") for i, r in enumerate(objects)]
        if objects
        else None
    )
    measured = _dimension_values(tx)
    targets = None if live is None else [*live, *_dimension_views(tx, live)]
    refreshed: list[result.Row] = []
    for _kind, refresh in sorted(hooks("refresher").items()):
        tx.checkpoint()
        refreshed.extend(refresh(tx, targets) or [])
    done = {row.get("id") for row in refreshed}
    given = set(objects or [])
    for d, value in measured:
        tx.checkpoint()
        if not math.isclose(float(d.ComputedSize), value, rel_tol=1e-9, abs_tol=1e-12):
            row = tx.modified(d, "dimension")
            if row.get("id") in given and row.get("id") not in done:
                refreshed.append(row)
                done.add(row.get("id"))
    out: RefreshData = {"refreshed": [r for r in refreshed if live is None or r.get("id") in given]}
    skipped = [
        {"id": ref, "reason": _why_skipped(tx, obj)}
        for ref, obj in zip(objects or [], live or [], strict=True)
        if ref not in done
    ]
    if skipped:
        out["skipped"] = skipped
        out["note"] = NOT_MANAGED
    elif not refreshed:
        out["note"] = "Nothing needed a refresh. " + NOT_MANAGED
    return out


def _dimension_values(tx):
    """``[(dimension, value)]`` of the work part's dimensions before a refresh."""
    out = []
    for d in tx.part.Dimensions:
        with contextlib.suppress(Exception):  # a dimension NX cannot measure
            out.append((d, float(d.ComputedSize)))
    return out


def _dimension_views(tx, objects):
    """The views of the given dimensions (a dimension refreshes with its view)."""
    dimensions = {int(d.Tag) for d in tx.part.Dimensions}
    views = []
    for obj in objects:
        if int(obj.Tag) not in dimensions:
            continue
        for i in range(1, int(getattr(obj, "NumberOfAssociativities", 0) or 0) + 1):
            with contextlib.suppress(Exception):  # an associativity NX cannot report
                view = obj.GetAssociativity(i).ObjectView
                if view is not None and hasattr(view, "Tag"):
                    views.append(view)
                    break
    return views


def _why_skipped(tx, obj):
    try:
        kind = tx.kind_of(obj)
    except Exception:  # an object without a reference kind
        kind = None
    if kind not in DRAWING_KINDS:
        return "not a drawing object"
    return {
        "dimension": "value unchanged",
        "drawing_view": "up to date",
        "annotation": "follows the model on its own",
    }.get(kind, "nothing to refresh")
