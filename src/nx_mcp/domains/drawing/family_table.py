"""Part family table on a drawing sheet of a part family template.

NX 2406 findings encoded here (``Annotations.PartFamilyTables.CreatePartFamilyTableBuilder``):

- Only a family template (``UF Part.IsFamilyTemplate``) gets a table: on another part the
  builder commits nothing (``Commit`` returns None), so the tool refuses first.
- The table lists one row per member: the member name, then one column per family
  column (the template expressions), as the family table holds them. It is a tabular
  note shown as a table section at the requested origin (its top-left corner).
- Rows are read back from the tabular note and compared with the family's member rows
  (``UF Fam.AskMemberRowData``).
- The builder commits the family table object; the table NX lists and reads as a drawing
  table is its first table section, which the result returns.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, SheetPoint
from nx_mcp.domains.drawing.common import (
    SheetArg,
    commit,
    on_sheet_point,
    open_or_only_sheet,
    report_bounds,
    set_origin,
    sheet_of,
    shown_table,
    update,
    verify_origin,
)
from nx_mcp.kit import builders, drawing, licenses, readback, result

NAME_COLUMN = "Part_Name"


class FamilyTableData(TypedDict, total=False):
    table: Annotated[result.Row, Doc("The table (kind drawing_table)")]
    sheet: result.Row
    origin: Annotated[list[float], Doc("Top-left corner of the table on the sheet")]
    columns: Annotated[list[str], Doc("Family columns: Part_Name, then the expressions")]
    rows: Annotated[list[list[str]], Doc("Cell texts per member row, as the table shows them")]
    bounds: list[float]


def _flag(answer):
    return bool(answer[0] if isinstance(answer, tuple) else answer)


def family_rows(tx, part):
    """``(columns, rows)`` of the work part's family table (UF reads; no manager)."""
    uf = tx.uf
    with native(default="NX_ERROR"):
        _count, families = uf.Part.AskFamilies(part.Tag)
        if not families:
            fail("NX_WRONG_KIND", "The work part has no part family")
        family = families[0]
        data = uf.Fam.AskFamilyData(family)
        columns = [str(uf.Fam.AskAttributeData(a).Name) for a in data.Attributes]
        rows = [
            [str(v) for v in uf.Fam.AskMemberRowData(family, row).Values]
            for row in range(int(data.MemberCount))
        ]
    return columns, rows


def _cells(tx, obj, sh):
    """Evaluated cell texts of the table, row by row (tabular note or its first section)."""
    tab = tx.uf.Tabnot
    note = obj.Tag
    with contextlib.suppress(Exception):  # the table is its own note when NX has no section
        if tab.AskNmSections(obj.Tag):
            note = tab.AskTabularNoteOfSection(tab.AskNthSection(obj.Tag, 0))
    out = []
    for i in range(int(tab.AskNmRows(note))):
        row = tab.AskNthRow(note, i)
        out.append([
            str(tab.AskEvaluatedCellText(tab.AskCellAtRowCol(row, tab.AskNthColumn(note, j))))
            for j in range(int(tab.AskNmColumns(note)))
        ])  # fmt: skip
    return out


def _same(cell, value):
    """A cell shows a family value: equal text or equal numbers; a formula value (not a
    plain number) shows NX's evaluation, which is not checked."""
    if cell.strip() == value.strip():
        return True
    try:
        wanted = float(value)
    except ValueError:
        return True
    try:
        return abs(float(cell) - wanted) <= 1e-9 * max(1.0, abs(wanted))
    except ValueError:
        return False


def _shows(row, member):
    """The table row shows the member: its name, then its values."""
    return (
        len(row) >= len(member)
        and bool(member)
        and row[0].strip() == member[0].strip()
        and all(_same(c, v) for c, v in zip(row[1:], member[1:], strict=False))
    )


def _member_rows(cells, members):
    """The table rows that show the members (header rows NX adds are skipped)."""
    names = [m[0] for m in members if m]
    start = next((i for i, row in enumerate(cells) if row and row[0] == (names or [""])[0]), None)
    return cells[start : start + len(members)] if start is not None else []


def _sections(part):
    return {int(s.Tag): s for s in getattr(part.Annotations, "TableSections", None) or []}


def _section(tx, before, table):
    """The table section NX made for the family table (the drawing table nx_list_objects
    lists and nx_get_drawing_table reads), else the table itself."""
    made = [s for tag, s in _sections(tx.part).items() if tag not in before]
    return made[0] if len(made) == 1 else shown_table(tx, table)


@tool(
    effect="model",
    label="Create part family table",
    summary="Part family table at {origin}",
    keywords=("part family table", "family table", "tabulated drawing", "member table", "variants"),
    requires=("work_part", "no_active_sketch"),
    cost="light",
    batchable=True,
    invalidates="deleted",
    example={"origin": [30, 250], "sheet": "SHT1"},
)
def nx_create_part_family_table(
    tx: Tx,
    *,
    origin: SheetPoint,
    sheet: SheetArg | None = None,
) -> FamilyTableData:
    """Place the work part's part family table on a drawing sheet: one row per member with
    its name and column values. The work part must be a family template (make one with
    nx_create_part_family). origin is the table's top-left corner, on the sheet; sheet
    defaults to the open or only one. Rows are read back against the family."""
    with tx.checks():
        part = tx.part
        try:
            template = _flag(tx.uf.Part.IsFamilyTemplate(part.Tag))
        except Exception:  # NX cannot tell
            template = False
        if not template:
            fail("NX_WRONG_KIND", "The work part is not a part family template",
                 hint="Create the family first with nx_create_part_family")  # fmt: skip
        columns, members = family_rows(tx, part)
        target = sheet_of(tx, sheet, "/sheet") if sheet is not None else open_or_only_sheet(tx)
        on_sheet_point(target, origin, "/origin")  # a table off the sheet is refused
    licenses.require(tx, ("drafting",), "Part family tables need a drafting license")
    A = tx.nx.Annotations
    factory = part.Annotations.PartFamilyTables.CreatePartFamilyTableBuilder
    before = _sections(part)
    with drawing.on_sheet(tx, target, part) as sh:
        with builders.build(tx, factory, A.Table.Null) as b:
            set_origin(tx, b.Origin, sh, origin)
            table = commit(b, "part family table")
        update(tx)
        anchor = drawing.anchor_of(tx, table)
        cells = _cells(tx, table, sh)
        shown = _member_rows(cells, members)
        readback.expect(
            len(shown) == len(members)
            and all(_shows(row, member) for row, member in zip(shown, members, strict=True)),
            "rows",
            "The table does not show the family's members",
            rows=cells[:20],
            members=members[:20],
        )
        out: FamilyTableData = {
            "table": tx.ref(_section(tx, before, table), "drawing_table"),
            "sheet": tx.ref(target, "drawing_sheet"),
            "origin": verify_origin(sh, anchor, origin),
            "columns": columns,
            "rows": cells,
        }
        return report_bounds(tx, out, table, sh, "table")
