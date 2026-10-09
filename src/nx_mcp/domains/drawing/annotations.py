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
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import hooks, tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Name, Point3, Range, RefTo, SheetPoint, Text
from nx_mcp.domains.drawing.common import (
    add_leader,
    annotation_kind,
    associated,
    check_leader,
    commit,
    distance_to_line,
    geometry,
    leader_row,
    set_origin,
    true_shape,
    update,
    verify_origin,
)
from nx_mcp.kit import args, builders, curve, drawing, facts, geom, readback, result, select

MAX_LINES = 50
FINISHES = {
    "any": "Basic",
    "required": "MaterialRemovalRequired",
    "prohibited": "MaterialRemovalProhibited",
}
WELDS = {"fillet": "Fillet", "square_butt": "SquareButt", "v_butt": "VButt"}
SIDES = {"arrow": ("arrow",), "other": ("other",), "both": ("arrow", "other")}
AXIAL_FACES = (select.FACE_CYLINDER, select.FACE_CONE)


class AnnotationData(TypedDict, total=False):
    annotation: result.Row
    annotations: list[result.Row]
    sheet: result.Row
    origin: list[float]
    text: list[str]
    leader: dict


def number_text(value):
    """Shortest exact decimal for a callout (3.2 -> '3.2', 5.0 -> '5')."""
    return f"{float(value):.6g}"


def _target_sheet(tx, sheet, view):
    """``(sheet, view or None)`` for a note: view's sheet, the given sheet or the current one."""
    if view is not None:
        obj, owner = drawing.view(tx, view)
        if sheet is not None and int(
            select.resolve(tx, sheet, "drawing_sheet", field="/sheet").Tag
        ) != int(owner.Tag):
            fail("NX_WRONG_OWNER", "view is not on the given sheet", field="/view")
        return owner, obj
    if sheet is not None:
        return select.resolve(tx, sheet, "drawing_sheet", field="/sheet", work_part=True), None
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
        f"Note {(a.get('text') or [''])[0][:30]!r}"
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
    text: Annotated[list[Annotated[str, Text(1024, r"^[^\r\n]*$")]], Items(1, MAX_LINES)],
    origin: SheetPoint,
    sheet: RefTo("drawing_sheet") | None = None,  # type: ignore[valid-type]
    view: RefTo("drawing_view") | None = None,  # type: ignore[valid-type]
    leader_to: RefTo("edge", "face") | None = None,  # type: ignore[valid-type]
) -> AnnotationData:
    """Place a drawing note, one list item per line, on a sheet (default the open one).
    leader_to adds a leader to an edge or face as shown in view."""
    if not any(line.strip() for line in text):
        fail("NX_INVALID_ARGUMENT", "text needs a non-blank line", field="/text")
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
        out = {
            "annotation": tx.ref(note, "annotation"),
            "sheet": tx.ref(target, "drawing_sheet"),
            "origin": verify_origin(sh, note, origin),
            "text": stored,
        }
        if obj is not None:
            with builders.build(tx, part.Annotations.CreateDraftingNoteBuilder, note) as reader:
                check_leader(reader, obj, shown)
            out["leader"] = leader_row(tx, obj, shown, anchor)
        return out


# ---- center marks and centerlines ---------------------------------------------------------


@tool(
    effect="model",
    label="Create centerline",
    summary={
        "center_mark": "Center marks on {objects} in {view}",
        "axis": "Axis centerline in {view}",
        "between_edges": "Centerline between 2 edges in {view}",
    },
    keywords=("center mark", "centerline", "axis line", "symmetry line", "hole center"),
    requires=("work_part", "no_active_sketch"),
    variants="kind",
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={"view": "v:7", "kind": "center_mark", "objects": ["e:21"]},
)
def nx_create_centerline(
    tx: Tx,
    *,
    view: RefTo("drawing_view"),  # type: ignore[valid-type]
    kind: Literal["center_mark", "axis", "between_edges"],
    objects: Annotated[list[RefTo("edge", "face")], Items(1, 50)],  # type: ignore[valid-type]
) -> AnnotationData:
    """Create center marks on circular edges, an axis line on a cylindrical or conical face, or a
    centerline between two edges. Circles must be seen along their axis."""
    args.distinct("/objects", objects, "objects")
    wanted = {"axis": 1, "between_edges": 2}.get(kind)
    if wanted and len(objects) != wanted:
        fail("NX_INVALID_ARGUMENT", f"{kind} takes {wanted} object(s)", field="/objects")
    if kind == "center_mark":
        return _center_marks(tx, view, objects)
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
    A = tx.nx.Annotations
    part = tx.part
    before = {int(c.Tag) for c in part.Annotations.Centerlines}
    with drawing.on_sheet(tx, sheet, part) as sh:
        centers = [drawing.to_sheet(tx, view, f["center"]) for f in arcs]
        factory = part.Annotations.Centerlines.CreateCenterMarkBuilder
        with builders.build(tx, factory, A.CenterMark.Null) as b:
            b.MultipleCenterMarks = len(edges) > 1
            for edge in edges:
                b.Locations.Add(edge, view, tx.nx.Point3d(*curve.point_at(tx, edge, 0.5)))
            commit(b, "center mark")
        update(tx)
        created = [c for c in part.Annotations.Centerlines if int(c.Tag) not in before]
        readback.expect(len(created) == len(edges), "count", created=len(created))
        tags = [int(e.Tag) for e in edges]
        marks = [None] * len(edges)
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
        return {
            "annotations": marks,
            "sheet": tx.ref(sheet, "drawing_sheet"),
            "origin": centers[0],
        }


def _axis_line(tx, view, face, field):
    code, point, direction = select.face_data(tx, face)
    if code not in AXIAL_FACES:
        fail("NX_WRONG_GEOMETRY", "An axis centerline needs a cylindrical or conical face",
             field=field)  # fmt: skip
    a = drawing.to_sheet(tx, view, point)
    b = drawing.to_sheet(tx, view, [p + d * 100.0 for p, d in zip(point, direction, strict=True)])
    projected = [b[0] - a[0], b[1] - a[1]]
    if abs(projected[0]) + abs(projected[1]) < 1e-3:
        fail(
            "NX_UNSUPPORTED_GEOMETRY",
            "The face axis points out of this view: use a center mark on its circular edge",
            field=field,
        )
    return a, projected


def _centerline(tx, view_ref, kind, refs):
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        if kind == "axis":
            targets = [geometry(tx, refs[0], ("face",), "/objects/0")]
            line_point, line_direction = _axis_line(tx, view, targets[0], "/objects/0")
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
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={
        "view": "v:7",
        "object": "fc:3",
        "origin": [60, 40],
        "symbol": "surface_finish",
        "roughness_ra": 3.2,
    },
)
def nx_create_drawing_symbol(
    tx: Tx,
    *,
    view: RefTo("drawing_view"),  # type: ignore[valid-type]
    object: RefTo("edge", "face"),  # type: ignore[valid-type]  # noqa: A002
    origin: SheetPoint,
    symbol: Literal["surface_finish", "weld"],
    roughness_ra: Annotated[float, Range(gt=0, le=1000), Doc("Micrometres")] | None = None,
    removal: Literal["any", "required", "prohibited"] = "required",
    weld: Literal["fillet", "square_butt", "v_butt"] | None = None,
    size: Annotated[float, Range(gt=0, le=1000)] | None = None,
    side: Literal["arrow", "other", "both"] = "arrow",
) -> AnnotationData:
    """Place a surface-finish or weld symbol with a leader to an edge or face shown in a view. The
    symbol is read back."""
    if symbol == "weld":
        args.need(weld=weld, size=size)
    else:
        args.need(roughness_ra=roughness_ra)
    with tx.checks():
        shown, sheet = drawing.view(tx, view)
        obj = geometry(tx, object, ("edge", "face"), "/object")
    if symbol == "weld":
        return _weld(tx, shown, sheet, obj, origin, {"type": weld, "size": size, "side": side})
    return _finish(
        tx, shown, sheet, obj, origin, {"roughness_ra": roughness_ra, "removal": removal}
    )


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
        return {
            "annotation": tx.ref(symbol, "annotation"),
            "sheet": tx.ref(sheet, "drawing_sheet"),
            "origin": verify_origin(sh, symbol, origin),
            "text": [text],
            "leader": leader_row(tx, obj, view, anchor),
        }


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
        return {
            "annotation": tx.ref(weld, "annotation"),
            "sheet": tx.ref(sheet, "drawing_sheet"),
            "origin": verify_origin(sh, weld, origin),
            "text": [size],
            "leader": leader_row(tx, obj, view, anchor),
        }


# ---- edit and refresh -----------------------------------------------------------------------

AnyAnnotation = RefTo("annotation", "dimension", "drawing_table")


class EditData(TypedDict, total=False):
    annotation: result.Row
    name: str
    position: list[float]
    frame: str


@tool(
    effect="model",
    label="Edit annotation",
    summary=lambda a, say: f"Edit {say('annotation')}",
    keywords=("move annotation", "rename annotation", "redefine GD&T", "move note", "balloon"),
    requires=("work_part", "no_active_sketch"),
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={"annotation": "a:5", "position": [200, 120]},
)
def nx_edit_annotation(
    tx: Tx,
    *,
    annotation: AnyAnnotation,  # type: ignore[valid-type]
    position: SheetPoint | Point3 | None = None,
    name: Name | None = None,
) -> EditData:
    """Move or rename an annotation, dimension or table. position is [x, y] on the sheet, or [x,
    y, z] for PMI; a move NX ignores (associative marks) is refused."""
    if position is None and name is None:
        fail("NX_INVALID_ARGUMENT", "Give position or name")
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
        sheet = drawing.sheet_of_annotation(tx, obj)
        anchor = drawing.anchor_of(tx, obj)
        if position is not None:
            if not hasattr(anchor, "AnnotationOrigin"):
                fail("NX_UNSUPPORTED_OPTION", "This object has no position", field="/position")
            if len(position) != (2 if sheet is not None else 3):
                fail("NX_INVALID_ARGUMENT",
                     "Use [x, y] for a sheet annotation, [x, y, z] for PMI",
                     field="/position")  # fmt: skip
    out = {}
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
    return out


class RefreshData(TypedDict):
    refreshed: list[result.Row]


@tool(
    effect="model",
    label="Update annotations",
    summary=lambda a, say: (
        f"Update {len(a['objects'])} annotations" if a.get("objects") else "Update all annotations"
    ),
    keywords=("refresh", "update parts list", "bend table", "detail boundary", "BOM update"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    invalidates="deleted",
    example={},
)
def nx_update_annotations(
    tx: Tx,
    *,
    objects: Annotated[list[RefTo()], Items(1, 100)] | None = None,  # type: ignore[valid-type]
) -> RefreshData:
    """Refresh managed annotations from the model: parts lists, automatic bend tables, detail
    boundaries and measured callouts. Omit objects to refresh all of them."""
    live = (
        [select.resolve(tx, r, field=f"/objects/{i}") for i, r in enumerate(objects)]
        if objects
        else None
    )
    refreshed = []
    for _kind, refresh in sorted(hooks("refresher").items()):
        tx.checkpoint()
        refreshed.extend(refresh(tx, live) or [])
    return {"refreshed": refreshed}
