"""Title-block cells: labels, lock status, raw and evaluated text; values and attribute links.

A title block (NX template sheets, ``nx_create_drawing_table kind=title_block``) groups
the cells of one or more table sections. Each defined cell has a unique label and a lock
status. NX 2406 findings encoded here:

- The cells are read from ``DefineTitleBlockBuilder.Cells`` (label, lock, table cell).
  ``EditTitleBlockBuilder`` writes only unlocked cells, by label
  (``SetCellValueForLabel``); setting ``EditableText`` on its cell builders is ignored,
  and ``Cells.GetContents()`` on it can crash NX when the part is not displayed.
- A work-part attribute reference is ``<W@TITLE>``. NX stores it as
  ``<WID<n>*0@TITLE>`` and re-evaluates it as soon as the attribute changes. The builder
  blanks a cell that links a missing attribute (and ``<W$=@TITLE>``), so links are checked
  first. Writing a title-block cell's
  tabular-note text directly leaves the reference unevaluated, so values always go
  through the builder.
- ``DefineTitleBlockBuilder.UpdateCells()`` defines no cells for a new table section:
  they are appended with ``CreateCellBuilder(tableCell, lock, label)``.
- Removing a table row under a defined cell breaks the next builder: a managed title
  block clears its cell definitions before its rows change and defines them again after.
"""

from __future__ import annotations

import re
from typing import Annotated, TypedDict

from nx_mcp.core.errors import fail
from nx_mcp.core.types import Doc, Range, Text, forbid_extra
from nx_mcp.kit import builders, readback

REFERENCE = re.compile(r"<W(?:ID\d+\*\d+)?@([^<>]+)>")
SINGLE = re.compile(r"^<W(?:ID\d+\*\d+)?@([^<>]+)>$")
# Every work-part attribute link NX evaluates: ``<W@X>``, bound ``<WID5*0@X>`` and the
# value form ``<W$=@X>``.
LINK = re.compile(r"<W(?:ID\d+\*\d+)?(?:\$=)?@([^<>]+)>")

Label = Annotated[str, Text(64, r"^\S(.{0,62}\S)?$"), Doc("Cell label")]
AttributeTitle = Annotated[
    str, Text(50, r"^[^\s<>@]([^<>@]{0,48}[^\s<>@])?$"), Doc("Work-part attribute title")
]
Index = Annotated[int, Range(ge=0, le=999)]


class CellEdit(TypedDict, total=False):
    label: Label
    section: Index
    row: Index
    column: Index
    text: Annotated[str, Text(1024), Doc("Value; may hold <W@TITLE> references")]
    attribute: AttributeTitle
    lock: bool


forbid_extra(CellEdit)


class CellInfo(TypedDict, total=False):
    label: str
    section: int
    row: int
    column: int
    locked: bool
    text: str
    value: str
    attribute: str


def normalize(text):
    """Raw cell text with NX's bound references (``<WID5*0@X>``) written as ``<W@X>``."""
    return REFERENCE.sub(lambda m: f"<W@{m.group(1)}>", str(text))


def reference(title):
    """The cell text that shows the work-part attribute ``title``."""
    return f"<W@{title}>"


def _collection(tx):
    return tx.part.DraftingManager.TitleBlocks


def _define(tx, title_block):
    return builders.build(tx, _collection(tx).CreateDefineTitleBlockBuilder, title_block)


def _index(tab, note):
    rows = {int(tab.AskNthRow(note, i)): i for i in range(tab.AskNmRows(note))}
    cols = {int(tab.AskNthColumn(note, j)): j for j in range(tab.AskNmColumns(note))}
    return rows, cols


def sections(tx, title_block):
    """Tabular-note tags of a title block's table sections, in definition order."""
    tab = tx.uf.Tabnot
    with _define(tx, title_block) as d:
        return [int(tab.AskTabularNoteOfSection(s.Tag)) for s in d.Components.GetArray()]


def read(tx, title_block):
    """The defined cells of a title block (``CellInfo`` rows plus ``cell``: the tag).

    ``section`` numbers the tables in the order of the cell definitions, which NX keeps;
    the order of ``Components`` changes from one session to the next."""
    tab = tx.uf.Tabnot
    out = []
    notes: list[int] = []
    index: dict = {}
    with _define(tx, title_block) as d:
        for c in d.Cells.GetContents():
            tag = c.Cell.Tag
            col = tab.AskColumnOfCell(tag)
            note = int(tab.AskTabularNoteOfColumn(col))
            if note not in index:
                notes.append(note)
                index[note] = _index(tab, note)
            rows, cols = index[note]
            raw = normalize(tab.AskCellText(tag))
            info = {
                "label": str(c.Label),
                "section": notes.index(note),
                "row": rows.get(int(tab.AskRowOfCell(tag)), -1),
                "column": cols.get(int(col), -1),
                "locked": bool(c.Lock),
                "text": raw,
                "value": str(tab.AskEvaluatedCellText(tag)),
                "cell": int(tag),
            }
            single = SINGLE.match(raw)
            if single:
                info["attribute"] = single.group(1)
            out.append(info)
    return out


def public(cells):
    return [{k: v for k, v in c.items() if k != "cell"} for c in cells]


def check_edits(edits):
    """Shape checks before NX: one address, something to change, no duplicate label."""
    for i, edit in enumerate(edits):
        field = f"/cells/{i}"
        by_label = "label" in edit
        by_place = "row" in edit or "column" in edit or "section" in edit
        if by_label == by_place or (by_place and not ("row" in edit and "column" in edit)):
            fail("NX_INVALID_ARGUMENT", "Address a cell by label, or by row and column "
                 "(section defaults to 0)", field=field)  # fmt: skip
        if "text" in edit and "attribute" in edit:
            fail("NX_INVALID_ARGUMENT", "Give text or attribute, not both", field=field)
        if not {"text", "attribute", "lock"} & set(edit):
            fail("NX_INVALID_ARGUMENT", "Give text, attribute or lock", field=field)


def check_attributes(tx, part, edits):
    """Refuse links (``<W@X>``, ``<W$=@X>``) to work-part attributes that do not exist (NX
    would blank the cell). Sheet fields (``$SH_...``) are NX's own."""
    kind = tx.nx.NXObject.AttributeType.Any
    for i, edit in enumerate(edits):
        text = wanted_text(edit) or ""
        for title in LINK.findall(text):
            if not title.startswith("$") and not part.HasUserAttribute(title, kind, -1):
                fail("NX_NOT_FOUND", f"The work part has no attribute {title!r}",
                     field=f"/cells/{i}", hint="Set it first with nx_set_attributes")  # fmt: skip


def resolve_edits(cells, edits):
    """``[(cell, edit)]`` for each edit; refuses unknown, repeated and locked cells."""
    by_label = {c["label"]: c for c in cells}
    by_place = {(c["section"], c["row"], c["column"]): c for c in cells}
    out, seen = [], set()
    for i, edit in enumerate(edits):
        field = f"/cells/{i}"
        if "label" in edit:
            cell = by_label.get(edit["label"])
        else:
            cell = by_place.get((edit.get("section", 0), edit["row"], edit["column"]))
        if cell is None:
            fail("NX_NOT_FOUND", "No such title-block cell", field=field,
                 hint="Read the cells with nx_get_drawing_table",
                 details={"labels": [c["label"] for c in cells][:60]})  # fmt: skip
        if cell["label"] in seen:
            fail("NX_INVALID_ARGUMENT", f"Cell {cell['label']!r} is edited twice", field=field)
        seen.add(cell["label"])
        writes = "text" in edit or "attribute" in edit
        if writes and cell["locked"] and edit.get("lock") is not False:
            fail("NX_READ_ONLY", f"Cell {cell['label']!r} is locked", field=field,
                 hint="Pass lock: false to unlock it; template labels and sheet fields are "
                 "locked on purpose")  # fmt: skip
        out.append((cell, edit))
    return out


def _set_locks(tx, title_block, locks):
    with _define(tx, title_block) as d:
        for c in d.Cells.GetContents():
            if str(c.Label) in locks:
                c.Lock = locks[str(c.Label)]
        d.commit()


def wanted_text(edit):
    if "attribute" in edit:
        return reference(edit["attribute"])
    return edit.get("text")


def apply(tx, title_block, pairs):
    """Unlock, write values through ``EditTitleBlockBuilder``, then lock (as requested)."""
    unlock = {c["label"]: False for c, e in pairs if e.get("lock") is False and c["locked"]}
    lock = {c["label"]: True for c, e in pairs if e.get("lock") is True and not c["locked"]}
    values = [(c["label"], wanted_text(e)) for c, e in pairs if wanted_text(e) is not None]
    if unlock:
        _set_locks(tx, title_block, unlock)
    if values:
        factory = _collection(tx).CreateEditTitleBlockBuilder
        with builders.build(tx, factory, [title_block]) as b:
            for label, text in values:
                b.SetCellValueForLabel(label, text)
            b.commit()
    if lock:
        _set_locks(tx, title_block, lock)


def verify(cells, pairs):
    by_label = {c["label"]: c for c in cells}
    for cell, edit in pairs:
        got = by_label.get(cell["label"])
        if got is None:
            readback.expect(False, "cells", missing=cell["label"])
            continue
        text = wanted_text(edit)
        if text is not None and got["text"] != normalize(text):
            readback.expect(False, "cells", label=cell["label"], text=got["text"])
        if "lock" in edit and got["locked"] != edit["lock"]:
            readback.expect(False, "cells", label=cell["label"], locked=got["locked"])


def clear_cells(tx, title_block):
    """Drop the cell definitions; ``{(row, column): (label, lock)}`` of the first section."""
    tab = tx.uf.Tabnot
    kept = {}
    with _define(tx, title_block) as d:
        contents = list(d.Cells.GetContents())
        if not contents:
            return kept
        note = int(tab.AskTabularNoteOfSection(d.Components.GetArray()[0].Tag))
        rows, cols = _index(tab, note)
        for c in contents:
            tag = c.Cell.Tag
            place = (rows.get(int(tab.AskRowOfCell(tag))), cols.get(int(tab.AskColumnOfCell(tag))))
            kept[place] = (str(c.Label), bool(c.Lock))
        d.Cells.Clear()
        d.commit()
    return kept


def define_cells(tx, title_block, kept=None):
    """Define every cell of a one-section title block: kept labels and locks by place, new
    cells unlocked as ``R<row>C<column>``."""
    kept = kept or {}
    tab = tx.uf.Tabnot
    collection = _collection(tx)
    with _define(tx, title_block) as d:
        if list(d.Cells.GetContents()):
            return
        note = int(tab.AskTabularNoteOfSection(d.Components.GetArray()[0].Tag))
        taken = {label for label, _ in kept.values()}
        for i in range(tab.AskNmRows(note)):
            row = tab.AskNthRow(note, i)
            for j in range(tab.AskNmColumns(note)):
                tag = tab.AskCellAtRowCol(row, tab.AskNthColumn(note, j))
                label, locked = kept.get((i, j), (f"R{i}C{j}", False))
                if (i, j) not in kept and label in taken:
                    label = f"R{i}C{j}_{len(taken)}"
                taken.add(label)
                cell = tx.nx.TaggedObjectManager.GetTaggedObject(tag)
                d.Cells.Append(collection.CreateCellBuilder(cell, locked, label))
        d.commit()
