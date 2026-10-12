"""Drawing tables: parts lists, bend tables, title blocks and revision tables; balloons.

NX 2406 findings encoded here:

- With the site variable ``UGII_UPDATE_ALL_ID_SYMBOLS_WITH_PLIST`` on, NX allows one
  parts list per part and a second one fails with raw error 1175188: it is refused
  before NX (``NX_ALREADY_EXISTS``, the existing list in ``details``).
- A bend table needs an actual flat-pattern drafting view (its builder does not validate
  otherwise, and commits an empty table): a view that is not named after a flat-pattern
  model view (``FLAT-PATTERN#1@3``) is refused when the work part has no Flat Pattern.
  Automatic bend tables are rebuilt by ``nx_update_annotations``; the automatic flag alone
  can leave stale rows after reopening a part.
- NX wraps cell text that does not fit its column: the evaluated text then has line
  breaks the stored text lacks (a warning names the cells).
- Title blocks are defined from a table section; the section's tabular note keeps the
  cells (its handle is stored on the title block). Cell labels, locks and attribute
  links are handled in ``title_blocks``; created title blocks label their cells
  ``R<row>C<column>``, unlocked.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Doc,
    DrawingSheetRef,
    DrawingTableRef,
    DrawingViewRef,
    EdgeRef,
    Items,
    SheetLength,
    SheetPoint,
    Text,
    forbid_extra,
)
from nx_mcp.domains.drawing import hole_callouts, title_blocks
from nx_mcp.domains.drawing.common import (
    NATIVE_TABLE,
    TABLE_KIND,
    commit,
    shown_table,
    table_kind,
    table_tag,
    uf_types,
    update,
)
from nx_mcp.kit import args, builders, drawing, parts, readback, result, select

DEFAULT_ROW_HEIGHT_MM = 7.0
BendColumn = Literal["id", "name", "angle", "direction", "radius"]
BEND_COLUMNS: dict[BendColumn, str] = {
    "id": "BendID",
    "name": "BendName",
    "angle": "BendAngle",
    "direction": "BendDirection",
    "radius": "BendRadius",
}
ONE_PARTS_LIST = "UGII_UPDATE_ALL_ID_SYMBOLS_WITH_PLIST"
Cell = Annotated[str, Text(1024)]
Rows = Annotated[list[Annotated[list[Cell], Items(1, 20)]], Items(1, 100)]
Widths = Annotated[list[SheetLength], Items(1, 20)]
BendColumns = Annotated[list[BendColumn], Items(1, 5)]


class TableInfo(TypedDict, total=False):
    table: result.Row
    kind: str
    sheet: result.Row
    position: list[float]
    rows: list[list[str]]
    columns: list[dict]
    row_height: float
    origin: dict
    tables: list[result.Row]
    automatic: bool
    cells: list[title_blocks.CellInfo]
    holes: list[dict]
    units: str
    bounds: list[float]


# ---- reading --------------------------------------------------------------------------------


def cells(tx, tag, raw=False):
    """Evaluated cell texts by row; ``raw`` gives the stored text (``<W@X>`` references)."""
    tab = tx.uf.Tabnot
    columns = [tab.AskNthColumn(tag, i) for i in range(tab.AskNmColumns(tag))]
    if raw:
        return [
            [
                title_blocks.normalize(
                    tab.AskCellText(tab.AskCellAtRowCol(tab.AskNthRow(tag, i), c))
                )
                for c in columns
            ]
            for i in range(tab.AskNmRows(tag))
        ]
    return [
        [
            str(tab.AskEvaluatedCellText(tab.AskCellAtRowCol(tab.AskNthRow(tag, i), c)))
            for c in columns
        ]
        for i in range(tab.AskNmRows(tag))
    ]


def _expect_rows(tx, obj, rows):
    got = cells(tx, table_tag(tx, obj) or _single_note(tx, obj), raw=True)
    want = [[title_blocks.normalize(c) for c in r] for r in rows]
    readback.expect(got == want, "rows", rows=got)


def _is_title_block(obj):
    return type(obj).__name__ == "TitleBlock"


def _single_note(tx, obj):
    """The tabular note of a one-section title block (template blocks have several)."""
    if not _is_title_block(obj):
        return None
    notes = title_blocks.sections(tx, obj)
    return notes[0] if len(notes) == 1 else None


def _parts_list_columns(tx, tag, scale=1.0):
    uf = tx.uf
    tab = uf.Tabnot
    header = tab.AskNthHeaderRow(tag, 0) if tab.AskNmHeaderRows(tag) else None
    out = []
    for i in range(tab.AskNmColumns(tag)):
        col = tab.AskNthColumn(tag, i)
        prefs = uf.Plist.AskColPrefs(col)
        out.append(
            {
                "width": float(tab.AskColumnWidth(col)) / scale,
                "field": str(prefs.DefaultString),
                "key": bool(prefs.IsKeyField),
                "protected": bool(prefs.IsProtected),
                "title": str(tab.AskCellText(tab.AskCellAtRowCol(header, col))) if header else None,
            }
        )
    return out


def _bend_columns(tx, table):
    A = tx.nx.Annotations
    with builders.build(tx, tx.part.Annotations.BendTables.CreateBendTableBuilder, table) as b:
        order = list(b.Style.BendTable.GetColumnOrder())
        automatic = bool(b.Style.BendTable.AutomaticUpdate)
    names = list(BEND_COLUMNS.values())
    native = [result.enum_name(x, A.BendTableSettingsBuilder.ColumnType, names) for x in order]
    plain = [next((k for k, v in BEND_COLUMNS.items() if v == n), n) for n in native]
    return [{"field": p} for p in plain], automatic


def table_info(tx, obj, kind=None):
    kind = kind or table_kind(tx, obj)
    if kind is None:
        fail("NX_WRONG_KIND", "Select a drawing table", field="/table")
    tag = table_tag(tx, obj)
    out = {"table": tx.ref(obj, "drawing_table"), "kind": kind}
    if kind == "title_block" and _is_title_block(obj):
        out["cells"] = title_blocks.public(title_blocks.read(tx, obj))
        tag = tag or _single_note(tx, obj)
    if tag is not None:
        out["rows"] = cells(tx, tag)
    sheet = drawing.sheet_of_annotation(tx, obj)
    scale = 1.0  # part units per sheet unit (UF table widths are in part units)
    if sheet is not None:
        sh = drawing.Sheet(tx, sheet)
        scale = sh.scale
        out["sheet"] = tx.ref(sheet, "drawing_sheet")
        out["position"] = sh.to_sheet(drawing.anchor_of(tx, obj).AnnotationOrigin)
        bounds = drawing.annotation_box(tx, obj, sh)[0]
        if bounds is not None:
            out["bounds"] = bounds
    if kind == "parts_list":
        out["columns"] = _parts_list_columns(tx, tag, scale)
        out["automatic"] = bool(tx.uf.Plist.AskPrefs(obj.Tag).AutoUpdate)
    elif kind == "bend":
        out["columns"], out["automatic"] = _bend_columns(tx, obj)
    elif kind == "hole":  # X/Y in the sheet's units
        out["holes"] = hole_callouts.parse_hole_table(out.get("rows") or [])
        if sheet is not None:
            out["units"] = sh.units
    elif tag is not None:
        tab = tx.uf.Tabnot
        out["columns"] = [
            {"width": float(tab.AskColumnWidth(tab.AskNthColumn(tag, i))) / scale}
            for i in range(tab.AskNmColumns(tag))
        ]
        if tab.AskNmRows(tag):
            out["row_height"] = float(tab.AskRowHeight(tab.AskNthRow(tag, 0))) / scale
    if tag is not None and kind not in ("hole", "bend"):
        _warn_wrapped(tx, tag, out.get("rows") or [])
    return out


def _warn_wrapped(tx, tag, rows):
    """Warn about cells NX wraps: line breaks in the shown text, none in the stored text."""
    raw = cells(tx, tag, raw=True)
    wrapped = [
        (i, j)
        for i, row in enumerate(rows)
        for j, shown in enumerate(row)
        if "\n" in shown and i < len(raw) and j < len(raw[i]) and "\n" not in raw[i][j]
    ]
    if wrapped:
        i, j = wrapped[0]
        columns = sorted({j for _, j in wrapped})
        tx.warn(
            "NX_UPDATE_WARNING",
            f"NX wraps {len(wrapped)} cell(s) whose text is wider than its column (row {i}, "
            f"column {j}: {raw[i][j]!r}); rows show the wrapped text. Widen widths {columns}",
        )


def _warn_views(tx, out, sheet):
    """Warn (NX_VIEW_OVERLAP) when a table's bounds lie over a drawing view's border."""
    if sheet is None or out.get("bounds") is None:
        return
    views = drawing.crowding(tx, sheet, out["bounds"])[0]
    if views:
        names = ", ".join(str(r.get("name") or r.get("id")) for r in views[:5])
        tx.warn("NX_VIEW_OVERLAP", f"The table lies over view {names}: move it with "
                "nx_edit_drawing_table position (see bounds)")  # fmt: skip


def _warn_off_sheet(tx, out, sheet):
    """Warn when a grid table's origin is off the sheet or its columns run past the edge."""
    _warn_views(tx, out, sheet)
    position, columns = out.get("position"), out.get("columns") or []
    if position is None or sheet is None:
        return
    length, height = drawing.size(sheet)
    right = position[0] + sum(c.get("width", 0.0) for c in columns)
    if position[0] < 0 or right > length or not 0 <= position[1] <= height:
        tx.warn(
            "NX_UPDATE_WARNING",
            f"The table at {position} reaches x={right:g}, past the {length:g} x {height:g} "
            "sheet: move it with position",
        )


# ---- create ---------------------------------------------------------------------------------


def _summary(a, say):
    kind = a.get("kind")
    where = say("view") if a.get("view") else say("sheet")
    return {
        "parts_list": f"Parts list on {where}",
        "bend": f"Bend table for {where}",
        "hole": f"Hole table for {where}",
        "title_block": f"Title block on {where}",
        "revision": f"Revision table on {where}",
    }.get(kind, "Drawing table")


@tool(
    effect="model",
    label="Create drawing table",
    summary=_summary,
    keywords=("parts list", "bom", "bend table", "title block", "revision table", "hole table"),
    requires=("work_part", "no_active_sketch"),
    variants="kind",
    required_by={
        "hole": ("view", "origin"),
        "bend": ("view",),
        "parts_list": ("sheet",),
        "title_block": ("sheet", "rows", "widths"),
        "revision": ("sheet", "rows", "widths"),
    },
    cost="heavy",
    invalidates="deleted",
    example={"kind": "parts_list", "sheet": "sh:3", "position": [300, 200]},
)
def nx_create_drawing_table(
    tx: Tx,
    *,
    kind: Literal["parts_list", "bend", "title_block", "revision", "hole"],
    position: SheetPoint,
    sheet: DrawingSheetRef | None = None,
    view: DrawingViewRef | None = None,
    scope: Literal["leaves", "top_level", "all"] = "leaves",
    columns: BendColumns | None = None,
    automatic: bool = True,
    rows: Rows | None = None,
    widths: Widths | None = None,
    row_height: SheetLength | None = None,
    origin: EdgeRef | None = None,
    origin_point: Literal["auto", "start", "end", "center"] = "auto",
) -> TableInfo:
    """Place a parts list, bend, title block, revision or hole table. Parts lists need an
    assembly, bend tables a flat-pattern view, hole tables a view and an origin edge; title
    blocks and revisions take rows and widths."""
    if kind == "hole":
        args.need(view=view, origin=origin)
        return hole_callouts.create_table(tx, view, position, origin, origin_point, table_info)
    if kind == "bend":
        args.need(view=view)
        if columns:
            args.distinct("/columns", columns, "columns")
        return _create_bend(tx, view, position, columns, automatic)
    args.need(sheet=sheet)
    if kind == "parts_list":
        return _create_parts_list(tx, sheet, position, scope)
    args.need(rows=rows, widths=widths)
    _check_grid(rows, widths)
    return _create_grid(tx, kind, sheet, position, rows, widths, row_height)


def _check_grid(rows, widths):
    if any(len(r) != len(widths) for r in rows):
        fail("NX_INVALID_ARGUMENT", "Every row needs one cell per width", field="/rows")
    if any(not w > 0 for w in widths):
        fail("NX_INVALID_ARGUMENT", "Widths must be positive", field="/widths")


def _create_parts_list(tx, sheet_ref, position, scope):
    with tx.checks():
        sheet = select.resolve(tx, sheet_ref, "drawing_sheet", field="/sheet", work_part=True)
        part = tx.part
        if not parts.components(part):
            fail("NX_WRONG_PART_CONTEXT", "A parts list needs an assembly", field="/sheet")
        existing = list(part.Annotations.PartsLists)
        single = getattr(tx.session, "GetEnvironmentVariableValue", lambda _: "")(ONE_PARTS_LIST)
        if existing and str(single or "").strip().upper() not in {"", "0", "OFF", "NO", "FALSE"}:
            fail(
                "NX_ALREADY_EXISTS",
                f"This NX allows one parts list per part ({ONE_PARTS_LIST} is on)",
                hint="Edit the existing parts list with nx_edit_drawing_table",
                details={"table": tx.ref(existing[0], "drawing_table")},
            )
    uf = tx.uf
    with drawing.on_sheet(tx, sheet, part) as sh:
        P = uf_types(tx).Plist
        prefs = uf.Plist.AskDefaultPrefs()
        prefs.AutoUpdate = True
        prefs.CreateNewRowsAsLocked = False
        prefs.InitialCalloutField = "1"
        prefs.MainSymbolText = "$~C"
        prefs.SymbolType = P.SymbolType.SYMBOL_TYPE_ID_SYMBOL_CIRCLE
        tag = uf.Plist.Create(prefs, sh.to_part(position))
        settings = uf.Plist.AskTraversalSettings(tag)
        settings.LeavesOnly = scope == "leaves"
        settings.TopLevelOnly = scope == "top_level"
        uf.Plist.SetTraversalSettings(tag, settings)
        uf.Plist.Update(tag)
        obj = next(o for o in part.Annotations.PartsLists if int(o.Tag) == int(tag))
        update(tx)
        out = table_info(tx, obj, "parts_list")
        readback.expect_close(out.get("position", position), list(position), "position", 1e-3)
        _warn_views(tx, out, sheet)
        return out


def _flat_pattern_view(tx, view):
    """Refuse a view that cannot be a flat pattern: not named after a flat-pattern model
    view while the work part has no Flat Pattern feature."""
    named = str(getattr(view, "Name", "")).upper().startswith("FLAT-PATTERN")
    features = getattr(tx.part, "Features", None) or []
    if not named and not any(str(f.FeatureType) == "FLAT_PATTERN" for f in features):
        fail(
            "NX_WRONG_KIND",
            "A bend table needs a flat-pattern view, and the work part has no Flat Pattern",
            field="/view",
            hint="Create the Flat Pattern of the sheet-metal part, then a view of it with "
            "nx_create_drawing_view type=flat_pattern",
        )


def _create_bend(tx, view_ref, position, columns, automatic):
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref)
        _flat_pattern_view(tx, view)
    A = tx.nx.Annotations
    factory = tx.part.Annotations.BendTables.CreateBendTableBuilder
    with drawing.on_sheet(tx, sheet) as sh:
        with builders.build(tx, factory, None) as b:
            b.FlatPatternView.Value = view
            b.Style.BendTable.AutomaticUpdate = bool(automatic)
            if columns:
                kinds = A.BendTableSettingsBuilder.ColumnType
                b.Style.BendTable.SetColumnOrder([getattr(kinds, BEND_COLUMNS[c]) for c in columns])
            b.AnnotationOrigin.Origin.SetValue(None, None, sh.point(position))
            if not b.Validate():
                fail("NX_WRONG_KIND", "A bend table needs a flat-pattern view", field="/view")
            table = commit(b, "bend table")
        update(tx)
        out = table_info(tx, table, "bend")
        if columns:
            got = [c["field"] for c in out["columns"]]
            readback.expect(got == list(columns), "columns", columns=got)
        readback.expect(out["automatic"] == bool(automatic), "automatic")
        return out


def _fixed_height(tx, cell):
    """Drop the cell's auto-size-row fit method: NX 2406 otherwise sizes the row to its
    text on update and drops a given row height (9 mm became 7.01 for one text line)."""
    tab = tx.uf.Tabnot
    prefs = tab.AskCellPrefs(cell)
    count = int(prefs.NmFitMethods)
    methods = list(prefs.FitMethods)
    auto = uf_types(tx).Tabnot.FitMethod.FIT_METHOD_AUTO_SIZE_ROW
    kept = [m for m in methods[:count] if m != auto]
    if len(kept) == count:
        return
    none = uf_types(tx).Tabnot.FitMethod.FIT_METHOD_NONE
    prefs.FitMethods = kept + [none] * (len(methods) - len(kept))
    prefs.NmFitMethods = len(kept)
    tab.SetCellPrefs(cell, prefs)


def _fill(tx, tag, rows, widths, height, fixed=False):
    """Rows, widths and row height of a table; ``fixed`` keeps the height whatever the
    text (a row height the caller gave)."""
    tab = tx.uf.Tabnot
    if tab.AskNmColumns(tag) != len(widths):
        fail("NX_UNSUPPORTED_OPTION", "Keep the table's column count", field="/widths",
             details={"columns": tab.AskNmColumns(tag)})  # fmt: skip
    while tab.AskNmRows(tag) < len(rows):
        tab.AddRow(tag, tab.CreateRow(height), tab.AskNmRows(tag))
    while tab.AskNmRows(tag) > len(rows):
        old = tab.AskNthRow(tag, tab.AskNmRows(tag) - 1)
        tab.RemoveRow(old)
        tx.uf.Obj.DeleteObject(old)
    for i, values in enumerate(rows):
        row = tab.AskNthRow(tag, i)
        tab.SetRowHeight(row, height)
        for j, value in enumerate(values):
            col = tab.AskNthColumn(tag, j)
            tab.SetColumnWidth(col, widths[j])
            cell = tab.AskCellAtRowCol(row, col)
            if fixed:
                _fixed_height(tx, cell)
            tab.SetCellText(cell, value)
        if fixed:
            tab.SetRowHeight(row, height)


def _create_grid(tx, kind, sheet_ref, position, rows, widths, row_height):
    with tx.checks():
        sheet = select.resolve(tx, sheet_ref, "drawing_sheet", field="/sheet", work_part=True)
    part = tx.part
    with drawing.on_sheet(tx, sheet, part) as sh:
        height = (
            row_height
            if row_height is not None
            else drawing.from_mm(sh.units, DEFAULT_ROW_HEIGHT_MM)[0]
        )
        widths_part = [sh.to_part([w, 0])[0] for w in widths]
        height_part = sh.to_part([height, 0])[0]
        factory = part.Annotations.TableSections.CreateTableSectionBuilder
        with builders.build(tx, factory, None) as b:
            b.NumberOfRows = len(rows)
            b.NumberOfColumns = len(widths)
            b.RowHeight = height_part
            b.ColumnWidth = widths_part[0]
            b.Origin.Origin.SetValue(None, None, sh.point(position))
            section = b.commit()
        section.SetAttribute(TABLE_KIND, kind)
        tag = tx.uf.Tabnot.AskTabularNoteOfSection(section.Tag)
        _fill(tx, tag, rows, widths_part, height_part, fixed=row_height is not None)
        section.AnnotationOrigin = sh.point(position)
        table = section
        if kind == "title_block":
            factory = part.DraftingManager.TitleBlocks.CreateDefineTitleBlockBuilder
            with builders.build(tx, factory, tx.nx.Annotations.TitleBlock.Null) as b:
                b.Components.Add(section)
                b.UpdateCells()
                table = b.commit()
            table.AnnotationOrigin = sh.point(position)
            table.SetAttribute(TABLE_KIND, kind)
            table.SetAttribute(NATIVE_TABLE, tx.uf.Tag.AskHandleFromTag(tag))
            title_blocks.define_cells(tx, table)
        update(tx)
        _expect_rows(tx, table, rows)
        out = table_info(tx, table, kind)
        _warn_off_sheet(tx, out, sheet)
        return out


# ---- edit and inspect -----------------------------------------------------------------------


class _Column(TypedDict):
    action: Literal["edit", "append", "remove"]


class ColumnEdit(_Column, total=False):
    index: int
    title: Annotated[str, Text(1024)]
    width: SheetLength
    field: Annotated[str, Text(1024)]
    key: bool
    protected: bool


forbid_extra(ColumnEdit)


@tool(
    effect="model",
    label="Edit drawing table",
    summary={
        "parts_list": "Edit parts list {table}",
        "bend": "Edit bend table {table}",
        "title_block": "Edit title block {table}",
        "revision": "Edit revision table {table}",
        "hole": "Edit hole table {table}",
    },
    variants="kind",
    variant_from=("table", table_kind),  # kind may be left out: the table's own
    keywords=(
        "edit parts list",
        "bom column",
        "table rows",
        "revision entry",
        "bend columns",
        "fill title block",
        "title block attribute",
    ),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    invalidates="deleted",
    example={"table": "t:4", "kind": "revision", "rows": [["A", "First release"]]},
)
def nx_edit_drawing_table(
    tx: Tx,
    *,
    table: DrawingTableRef,
    kind: Literal["parts_list", "bend", "title_block", "revision", "hole"],
    rows: Rows | None = None,
    widths: Widths | None = None,
    row_height: SheetLength | None = None,
    position: SheetPoint | None = None,
    columns: Annotated[list[ColumnEdit], Items(1, 20)] | None = None,
    bend_columns: BendColumns | None = None,
    automatic: bool | None = None,
    cells: Annotated[list[title_blocks.CellEdit], Items(1, 100)] | None = None,
) -> TableInfo:
    """Edit a drawing table of the given kind, or move it. Title blocks and revisions take rows,
    widths and row height; title blocks also cells (text, <W@TITLE> attribute link or lock,
    by label or row/column); parts lists columns; bend tables column order and update mode."""
    if all(
        v is None
        for v in (rows, widths, row_height, position, columns, bend_columns, automatic, cells)
    ):
        fail("NX_INVALID_ARGUMENT", "Give an edit")
    if cells:
        title_blocks.check_edits(cells)
    pairs = None
    with tx.checks():
        obj = shown_table(tx, select.resolve(
            tx, table, "drawing_table", "annotation", field="/table", work_part=True
        ))  # fmt: skip
        actual = table_kind(tx, obj)
        if actual != kind:
            fail("NX_WRONG_KIND", f"The table is a {actual or 'non-table'}, not a {kind}",
                 field="/kind", details={"kind": actual})  # fmt: skip
        allowed = {
            "parts_list": {"columns", "position"},
            "bend": {"bend_columns", "automatic", "position"},
            "hole": {"position"},
            "title_block": {"rows", "widths", "row_height", "position", "cells"},
        }.get(kind, {"rows", "widths", "row_height", "position"})
        given = {
            k
            for k, v in {
                "rows": rows,
                "widths": widths,
                "row_height": row_height,
                "position": position,
                "columns": columns,
                "bend_columns": bend_columns,
                "automatic": automatic,
                "cells": cells,
            }.items()
            if v is not None
        }
        if given - allowed:
            field = sorted(given - allowed)[0]
            fail("NX_UNSUPPORTED_OPTION", f"{field} does not apply to a {kind} table",
                 field=f"/{field}")  # fmt: skip
        if columns:
            _check_columns(tx, obj, columns)
        if kind == "title_block":
            obj = _title_block_of(tx, obj)
            grid = {"rows", "widths", "row_height"} & given
            if grid and table_tag(tx, obj) is None:
                fail("NX_UNSUPPORTED_OPTION", "This title block spans several tables: edit its "
                     "cells", field=f"/{sorted(grid)[0]}")  # fmt: skip
        if cells:
            title_blocks.check_attributes(tx, tx.part, cells)
            current = title_blocks.read(tx, obj)
            if current and rows is None:
                pairs = title_blocks.resolve_edits(current, cells)
        sheet = drawing.sheet_of_annotation(tx, obj)
    with drawing.on_sheet(tx, sheet) as sh:
        if kind == "parts_list" and columns:
            for i, edit in enumerate(columns):
                tx.checkpoint(done=i, total=len(columns))
                _edit_column(tx, obj, edit, sh)
            tx.uf.Plist.Update(obj.Tag)
        if kind == "bend" and (bend_columns or automatic is not None):
            _edit_bend(tx, obj, bend_columns, automatic)
        if kind not in ("parts_list", "bend") and (rows or widths or row_height is not None):
            _edit_grid(tx, obj, rows, widths, row_height, sh)
        if cells:
            if pairs is None:
                title_blocks.define_cells(tx, obj)
                pairs = title_blocks.resolve_edits(title_blocks.read(tx, obj), cells)
            title_blocks.apply(tx, obj, pairs)
        if position is not None:
            drawing.anchor_of(tx, obj).AnnotationOrigin = sh.point(position)
        update(tx)
        if rows is not None:
            _expect_rows(tx, obj, rows)
        if pairs:
            title_blocks.verify(title_blocks.read(tx, obj), pairs)
        out = table_info(tx, obj, kind)
    if position is not None:
        readback.expect_close(out.get("position"), list(position), "position", 1e-3)
    if kind in ("title_block", "revision"):
        if row_height is not None and abs(out.get("row_height", row_height) - row_height) > 1e-3:
            tx.warn("NX_UPDATE_WARNING", f"NX keeps the first row {out['row_height']:g} high, "
                    f"not {row_height:g}: its text needs more height")  # fmt: skip
        _warn_off_sheet(tx, out, sheet)
    return out


def _title_block_of(tx, obj):
    """The title block itself (a managed title block's table section maps to it)."""
    if _is_title_block(obj):
        return obj
    tag = int(obj.Tag)
    for block in tx.part.DraftingManager.TitleBlocks:
        if tag in {int(s) for s in _section_tags(tx, block)}:
            return block
    fail("NX_WRONG_KIND", "Select the title block, not its table section", field="/table")


def _section_tags(tx, block):
    with builders.build(
        tx, tx.part.DraftingManager.TitleBlocks.CreateDefineTitleBlockBuilder, block
    ) as d:
        return [s.Tag for s in d.Components.GetArray()]


def _check_columns(tx, obj, edits):
    count = tx.uf.Tabnot.AskNmColumns(obj.Tag)
    for i, edit in enumerate(edits):
        field = f"/columns/{i}"
        extra = {k for k in edit if k not in ("action", "index")}
        if edit["action"] == "append":
            if "index" in edit:
                fail("NX_INVALID_ARGUMENT", "append takes no index", field=f"{field}/index")
            for need in ("title", "width", "field"):
                if need not in edit:
                    fail("NX_INVALID_ARGUMENT", f"append needs {need}", field=f"{field}/{need}")
            count += 1
            continue
        index = edit.get("index")
        if not isinstance(index, int) or not 0 <= index < count:
            fail("NX_INVALID_ARGUMENT", f"index must be 0-{count - 1}", field=f"{field}/index")
        if edit["action"] == "remove":
            if extra or count <= 1:
                fail("NX_INVALID_ARGUMENT", "remove takes only index and keeps one column",
                     field=field)  # fmt: skip
            count -= 1
        elif not extra:
            fail("NX_INVALID_ARGUMENT", "edit needs a change", field=field)
        if "width" in edit and not edit["width"] > 0:
            fail("NX_INVALID_ARGUMENT", "width must be positive", field=f"{field}/width")


def _edit_column(tx, obj, edit, sh):
    uf = tx.uf
    tab = uf.Tabnot
    width = sh.to_part([edit["width"], 0])[0] if "width" in edit else None
    if edit["action"] == "append":
        prefs = uf.Plist.AskDefaultColPrefs()
        prefs.DefaultString = edit["field"]
        prefs.IsKeyField = bool(edit.get("key", False))
        prefs.IsProtected = bool(edit.get("protected", False))
        general = uf_types(tx).Plist.ColumnType.COLUMN_TYPE_GENERAL
        col = uf.Plist.CreateColumn(width, prefs, general)
        tab.AddColumn(obj.Tag, col, tab.AskNmColumns(obj.Tag))
    else:
        col = tab.AskNthColumn(obj.Tag, edit["index"])
    if edit["action"] == "remove":
        tab.RemoveColumn(col)
        uf.Obj.DeleteObject(col)
        return
    prefs = uf.Plist.AskColPrefs(col)
    for attr, key in (
        ("DefaultString", "field"),
        ("IsKeyField", "key"),
        ("IsProtected", "protected"),
    ):
        if key in edit:
            setattr(prefs, attr, edit[key])
    uf.Plist.SetColPrefs(col, prefs)
    if width is not None:
        tab.SetColumnWidth(col, width)
    if "title" in edit:
        if not tab.AskNmHeaderRows(obj.Tag):
            fail("NX_NOT_FOUND", "The parts list has no header row", field="/columns")
        tab.SetCellText(tab.AskCellAtRowCol(tab.AskNthHeaderRow(obj.Tag, 0), col), edit["title"])


def _edit_bend(tx, obj, columns, automatic):
    A = tx.nx.Annotations
    with builders.build(tx, tx.part.Annotations.BendTables.CreateBendTableBuilder, obj) as b:
        if automatic is not None:
            b.Style.BendTable.AutomaticUpdate = bool(automatic)
        if columns:
            kinds = A.BendTableSettingsBuilder.ColumnType
            b.Style.BendTable.SetColumnOrder([getattr(kinds, BEND_COLUMNS[c]) for c in columns])
        if not b.Validate():
            fail("NX_BUILD_FAILED", "The bend table no longer validates")
        b.commit()


def _edit_grid(tx, obj, rows, widths, row_height, sh):
    tag = table_tag(tx, obj)
    tab = tx.uf.Tabnot
    current = cells(tx, tag, raw=True)
    count = tab.AskNmColumns(tag)
    if widths is not None and len(widths) != count:
        fail("NX_UNSUPPORTED_OPTION", "Keep the table's column count", field="/widths")
    if rows is not None and any(len(r) != count for r in rows):
        fail("NX_UNSUPPORTED_OPTION", "Keep the table's column count", field="/rows")
    if widths is not None and any(not w > 0 for w in widths):
        fail("NX_INVALID_ARGUMENT", "Widths must be positive", field="/widths")
    native_widths = (
        [sh.to_part([w, 0])[0] for w in widths]
        if widths is not None
        else [float(tab.AskColumnWidth(tab.AskNthColumn(tag, j))) for j in range(count)]
    )
    if row_height is not None:
        height = sh.to_part([row_height, 0])[0]
    else:
        height = (
            float(tab.AskRowHeight(tab.AskNthRow(tag, 0)))
            if tab.AskNmRows(tag)
            else sh.to_part([drawing.from_mm(sh.units, DEFAULT_ROW_HEIGHT_MM)[0], 0])[0]
        )
    kept = title_blocks.clear_cells(tx, obj) if _is_title_block(obj) else None
    _fill(
        tx, tag, rows if rows is not None else current, native_widths, height,
        fixed=row_height is not None,
    )  # fmt: skip
    if kept is not None:
        title_blocks.define_cells(tx, obj, kept)


@tool(
    effect="read",
    label="Get drawing table",
    summary=lambda a, say: (
        f"Table {say('table')}" if a.get("table") else f"Title block of {say('sheet')}"
    ),
    keywords=(
        "parts list rows",
        "bom contents",
        "table cells",
        "bend table rows",
        "columns",
        "title block cells",
    ),
    requires=("work_part",),
    cost="light",
    example={"table": "t:4"},
)
def nx_get_drawing_table(
    tx: Tx,
    *,
    table: DrawingTableRef | None = None,
    sheet: DrawingSheetRef | None = None,
) -> TableInfo:
    """Read a drawing table, or the title block of a sheet with tables: every table on it. Gives
    kind, evaluated rows, columns, update mode and position; title blocks list cells (label,
    lock, text with <W@TITLE> links, value)."""
    if (table is None) == (sheet is None):
        both = table is not None
        fail("NX_INVALID_ARGUMENT", "Give table or sheet" + (", not both" if both else ""),
             field="/sheet" if both else "/table")  # fmt: skip
    if table is not None:
        obj = select.resolve(
            tx, table, "drawing_table", "annotation", field="/table", work_part=True
        )
        return table_info(tx, shown_table(tx, obj))
    target = select.resolve(tx, sheet, "drawing_sheet", field="/sheet", work_part=True)
    on_sheet = [
        (obj, kind)
        for obj, kind in all_tables(tx)
        if (on := drawing.sheet_of_annotation(tx, obj)) is not None
        and int(on.Tag) == int(target.Tag)
    ]
    rows = [{**tx.ref(obj, "drawing_table"), "type": kind} for obj, kind in on_sheet]
    found = [obj for obj, _ in on_sheet if _is_title_block(obj)]
    if not found:
        fail("NX_NOT_FOUND", "The sheet has no title block", field="/sheet",
             hint="Read one of details.tables by table, create the sheet from a template, or "
             "use nx_create_drawing_table kind=title_block", details={"tables": rows})  # fmt: skip
    if len(found) > 1:
        fail("NX_AMBIGUOUS_REFERENCE", "The sheet has several title blocks: pass table",
             field="/sheet", details={"tables": [tx.ref(b, "drawing_table") for b in found]})  # fmt: skip
    out = table_info(tx, found[0], "title_block")
    out["tables"] = rows
    return out


def all_tables(tx):
    """``[(table, kind)]`` of the work part: parts lists, bend and hole tables, managed
    tables and title blocks; the table section of a title block, parts list, bend or
    hole table is not listed apart."""
    part = tx.part
    blocks = list(getattr(getattr(part, "DraftingManager", None), "TitleBlocks", None) or [])
    owners = [
        *part.Annotations.PartsLists,
        *part.Annotations.BendTables,
        *(getattr(part.Annotations, "HoleTables", None) or []),
    ]
    found = [*owners, *(getattr(part.Annotations, "TableSections", None) or []), *blocks]
    owners += blocks
    inside = {int(t) for b in owners if (t := table_tag(tx, b)) is not None}
    out = []
    for obj in {int(o.Tag): o for o in found}.values():
        kind = table_kind(tx, obj)
        if not kind or (type(obj).__name__ == "TableSection" and inside
                        and int(table_tag(tx, obj)) in inside):  # fmt: skip
            continue
        out.append((obj, kind))
    return out


class BalloonData(TypedDict, total=False):
    balloons: Annotated[list[result.Row], Doc("Each with its sheet bounds")]
    parts_list: result.Row
    note: str


@tool(
    effect="model",
    label="Create balloons",
    summary="Balloons of {parts_list} in {view}",
    keywords=("balloon", "callout", "item number", "id symbol", "find number"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    invalidates="deleted",
    example={"parts_list": "t:4", "view": "v:7"},
)
def nx_create_balloons(
    tx: Tx,
    *,
    parts_list: DrawingTableRef,
    view: DrawingViewRef,
) -> BalloonData:
    """Create parts-list balloons for the components shown in a drawing view. One per parts-list
    row; rows already ballooned in the view get none."""
    with tx.checks():
        obj = select.resolve(
            tx, parts_list, "drawing_table", "annotation", field="/parts_list", work_part=True
        )
        kind = table_kind(tx, obj)
        if kind != "parts_list":
            fail("NX_WRONG_KIND", f"parts_list must be a parts list, not a {kind or 'non-table'}",
                 field="/parts_list", details={"kind": kind},
                 hint="Pass the table of nx_create_drawing_table kind=parts_list; "
                 "nx_list_objects kind=drawing_table lists the tables")  # fmt: skip
        shown, sheet = drawing.view(tx, view)
    part = tx.part
    before = {int(a.Tag) for a in part.Annotations.IdSymbols}
    with drawing.on_sheet(tx, sheet, part) as sh:
        obj.ShowBalloonsInView(shown)
        update(tx)
        created = [a for a in part.Annotations.IdSymbols if int(a.Tag) not in before]
        rows, crowded = [], 0
        for balloon in created:
            row = tx.ref(balloon, "annotation")
            bounds, text = drawing.annotation_box(tx, balloon, sh)
            if bounds is not None:
                row["bounds"] = bounds  # type: ignore[typeddict-unknown-key]  # extra row field
                views, off = drawing.crowding(tx, sheet, text or bounds)
                crowded += bool(views or off)
            rows.append(row)
    if crowded:
        tx.warn("NX_VIEW_OVERLAP", f"{crowded} balloon(s) lie over a view or off the sheet: "
                "move them with nx_edit_annotation position (see bounds)")  # fmt: skip
    out: BalloonData = {
        "balloons": rows,
        "parts_list": tx.ref(obj, "drawing_table"),
    }
    if not created:
        out["note"] = (
            "No new balloons: every parts-list row shown in this view already has one "
            "(one balloon per row, not per component)"
        )
    return out
