"""Drawing hooks: sheet locators (gate 9), refreshers (``nx_update_annotations``), listers
(``nx_list_objects``) and deleters (``nx_delete_objects``) for drawing kinds.

Hook signatures: ``@sheet_locator(kind)`` is ``fn(services, obj) -> sheet or None``;
``@refresher(kind)`` is ``fn(tx, objects or None) -> [rows refreshed]``; ``@lister(kind)``
is ``fn(tx, owner, owner_kind) -> [(obj, kind, extra)]``; ``@deleter(kind)`` is
``fn(tx, objects) -> None`` (deletes through the update manager inside the caller's mark).
"""

from __future__ import annotations

import contextlib

from nx_mcp.core.catalogue import deleter, lister, refresher, sheet_locator
from nx_mcp.core.errors import fail
from nx_mcp.domains.drawing import title_blocks, views
from nx_mcp.domains.drawing.common import DETAIL, attribute, table_kind, update
from nx_mcp.domains.drawing.tables import cells
from nx_mcp.kit import builders, drawing

# ---- gate 9: the sheet an annotation, dimension or table lies on ----------------------------


def _locate(services, obj):
    part = services.work_part()
    if part is None or not hasattr(obj, "Tag"):
        return None
    return drawing.sheet_of_annotation(services, obj, part)


for _kind in ("annotation", "dimension", "drawing_table"):
    sheet_locator(_kind)(_locate)


# ---- refreshers --------------------------------------------------------------------------------


def _wanted(objects, values):
    if objects is None:
        return list(values)
    tags = {int(o.Tag) for o in objects}
    return [v for v in values if int(v.Tag) in tags]


@refresher("drawing_view")
def refresh_detail_views(tx, objects):
    """Re-map managed detail-view boundaries to their parents."""
    managed = [
        v for s in tx.part.DrawingSheets for v in s.GetDraftingViews() if attribute(tx, v, DETAIL)
    ]
    targets = _wanted(objects, managed)
    if not targets:
        return []
    return [tx.ref(v, "drawing_view") for v in views.refresh_details(tx, targets)]


@refresher("drawing_table")
def refresh_tables(tx, objects):
    """Update parts lists, rebuild automatic bend tables and re-evaluate title blocks
    (attribute links); the changed ones."""
    part = tx.part
    out = []
    for plist in _wanted(objects, part.Annotations.PartsLists):
        before = cells(tx, plist.Tag)
        tx.uf.Plist.Update(plist.Tag)
        if cells(tx, plist.Tag) != before or objects is not None:
            out.append(tx.ref(plist, "drawing_table"))
    tables = part.Annotations.BendTables
    for table in _wanted(objects, tables):
        with builders.build(tx, tables.CreateBendTableBuilder, table) as b:
            if not b.Style.BendTable.AutomaticUpdate:
                continue
            before = cells(tx, table.Tag)
            if not b.Validate():
                fail("NX_BUILD_FAILED", "An automatic bend table no longer validates",
                     details={"table": tx.ref(table, "drawing_table")})  # fmt: skip
            rebuilt = b.commit()
        if rebuilt is not None and (cells(tx, rebuilt.Tag) != before or objects is not None):
            out.append(tx.ref(rebuilt, "drawing_table"))
    blocks = getattr(getattr(part, "DraftingManager", None), "TitleBlocks", None) or []
    for block in _wanted(objects, blocks):
        before = [c["value"] for c in title_blocks.read(tx, block)]
        block.DoUpdate()
        if [c["value"] for c in title_blocks.read(tx, block)] != before or objects is not None:
            out.append(tx.ref(block, "drawing_table"))
    if out:
        update(tx)
    return out


# ---- listers -----------------------------------------------------------------------------------


def _whole_part(owner):
    if owner is not None:
        fail("NX_UNSUPPORTED_OPTION", "Drawing objects are listed for the whole part",
             field="/owner")  # fmt: skip


def _sheet_scale(sheet):
    try:
        return [float(x) for x in sheet.GetScale()]
    except Exception:  # a sheet whose scale NX cannot report
        return None


def _origin(obj):
    point = getattr(obj, "AnnotationOrigin", None)
    return None if point is None else [float(point.X), float(point.Y), float(point.Z)]


@lister("drawing_sheet")
def list_sheets(tx, owner, owner_kind):
    _whole_part(owner)
    current = drawing.current(tx.part)
    return [
        (
            s,
            "drawing_sheet",
            {
                "details": {
                    "active": current is not None and int(current.Tag) == int(s.Tag),
                    "size": drawing.size(s),
                    "scale": _sheet_scale(s),
                }
            },  # fmt: skip
        )
        for s in tx.part.DrawingSheets
    ]


@lister("drawing_view")
def list_views(tx, owner, owner_kind):
    _whole_part(owner)
    return [
        (
            v,
            "drawing_view",
            {"type": views.view_type(v), "details": {"position": drawing.position(v)}},
        )
        for s in tx.part.DrawingSheets
        for v in s.GetDraftingViews()
    ]


@lister("dimension")
def list_dimensions(tx, owner, owner_kind):
    _whole_part(owner)
    return [
        (
            d,
            "dimension",
            {
                "type": type(d).__name__,
                "details": {
                    "value": float(d.ComputedSize),
                    "valid": not bool(d.IsRetained),
                    "origin": _origin(d),
                },
            },
        )  # fmt: skip
        for d in tx.part.Dimensions
    ]


def _annotations(part):
    values = [*getattr(part, "Notes", []), *getattr(part, "Labels", [])]
    collections = getattr(part, "Annotations", None)
    for name in (
        "Datums",
        "Fcfs",
        "IdSymbols",
        "Centerlines",
        "DraftingSurfaceFinishSymbol",
        "Welds",
    ):
        try:
            values.extend(getattr(collections, name, None) or [])
        except Exception:  # a collection this NX build cannot enumerate
            continue
    return list({int(o.Tag): o for o in values}.values())


@lister("annotation")
def list_annotations(tx, owner, owner_kind):
    _whole_part(owner)
    out = []
    for a in _annotations(tx.part):
        details = {"origin": _origin(a)}
        if hasattr(a, "GetText"):
            with contextlib.suppress(Exception):  # an annotation without readable text
                details["text"] = [str(t) for t in a.GetText()]
        if hasattr(a, "IsRetained"):
            details["retained"] = bool(a.IsRetained)
        out.append((a, "annotation", {"type": type(a).__name__, "details": details}))
    return out


@lister("drawing_table")
def list_tables(tx, owner, owner_kind):
    _whole_part(owner)
    part = tx.part
    found = [
        *part.Annotations.PartsLists,
        *part.Annotations.BendTables,
        *part.Annotations.TableSections,
        *part.DraftingManager.TitleBlocks,
    ]
    out = []
    for obj in {int(o.Tag): o for o in found}.values():
        kind = table_kind(tx, obj)
        if kind:
            out.append((obj, "drawing_table", {"type": kind}))
    return out


# ---- deleters ----------------------------------------------------------------------------------


def _delete(tx, objects):
    tx.session.UpdateManager.AddToDeleteList(list(objects))
    update(tx)


for _kind in ("annotation", "dimension", "drawing_table", "drawing_view", "drawing_sheet"):
    deleter(_kind)(_delete)
