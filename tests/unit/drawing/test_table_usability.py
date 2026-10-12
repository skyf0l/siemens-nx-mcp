"""Drawing tables: wrapped cells, tables past the sheet edge, row heights, bend-table views,
the tables of a sheet, balloon notes and the hole-table origin point."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.domains.drawing import hole_callouts, title_blocks
from tests.kit import run
from tests.kit.objects import Object, point
from tests.unit.drawing.fakes import Attributes, drawing_nx, make_view

CHAR_MM = 2.5  # the fake's text width: NX wraps a cell wider than its column


class TableSection(Attributes, Object):
    pass


class Sections(list):
    def __init__(self, nx):
        super().__init__()
        self.nx = nx

    def CreateTableSectionBuilder(self, existing):
        return self.nx.builder("TableSectionBuilder", existing)


class Grid:
    """UF tabular notes behind table sections: rows, columns, sizes and cell text."""

    def __init__(self, nx):
        self.nx, self.notes, self.tags = nx, {}, iter(range(50_000, 60_000))
        self.text, self.width, self.height = {}, {}, {}
        self.fits = {}  # cell -> fit methods; NX's default wraps and auto-sizes the row

    def prefs(self, cell):
        methods = self.fits.get(cell, [3, 6, 2])
        return NS(FitMethods=methods + [0] * (8 - len(methods)), NmFitMethods=len(methods))

    def set_prefs(self, cell, prefs):
        self.fits[cell] = list(prefs.FitMethods[: prefs.NmFitMethods])

    def new(self, rows, columns, height, width):
        note = next(self.tags)
        self.notes[note] = {"rows": [], "cols": []}
        for _ in range(columns):
            col = next(self.tags)
            self.width[col] = width
            self.notes[note]["cols"].append(col)
        for _ in range(rows):
            self.add_row(note, self.create_row(height), len(self.notes[note]["rows"]))
        return note

    def create_row(self, height):
        row = next(self.tags)
        self.height[row] = height
        return row

    def add_row(self, note, row, index):
        self.notes[note]["rows"].insert(index, row)

    def remove_row(self, row):
        for note in self.notes.values():
            if row in note["rows"]:
                note["rows"].remove(row)

    def shown(self, cell):
        text = self.text.get(cell, "")
        fits = int(self.width[cell[1]] / CHAR_MM)
        return text if len(text) <= fits else text[:fits] + "\n" + text[fits:]

    def tabnot(self, sections):
        n = self.notes
        return NS(
            AskTabularNoteOfSection=lambda tag: sections[int(tag)],
            AskNmSections=lambda tag: 0,
            AskNmRows=lambda note: len(n[note]["rows"]),
            AskNmColumns=lambda note: len(n[note]["cols"]),
            AskNthRow=lambda note, i: n[note]["rows"][i],
            AskNthColumn=lambda note, j: n[note]["cols"][j],
            CreateRow=self.create_row,
            AddRow=self.add_row,
            RemoveRow=self.remove_row,
            SetRowHeight=lambda row, h: self.height.__setitem__(row, float(h)),
            SetColumnWidth=lambda col, w: self.width.__setitem__(col, float(w)),
            AskRowHeight=lambda row: self.height[row],
            AskColumnWidth=lambda col: self.width[col],
            AskCellAtRowCol=lambda row, col: (row, col),
            SetCellText=lambda cell, text: self.text.__setitem__(cell, text),
            AskCellText=lambda cell: self.text.get(cell, ""),
            AskEvaluatedCellText=self.shown,
            AskCellPrefs=self.prefs,
            SetCellPrefs=self.set_prefs,
        )


def grid_nx():
    nx = drawing_nx()
    nx.sheet_obj = nx.make_sheet()
    grid, sections = Grid(nx), {}
    nx.part.Annotations = NS(PartsLists=[], BendTables=[], TableSections=Sections(nx))

    def commit(b):
        section = TableSection("SECTION")
        section.OwningPart, section.IsOccurrence = nx.part, False
        section.AnnotationOrigin = b.Origin.Origin.SetValue.calls[-1][0][2]
        sections[int(section.Tag)] = grid.new(
            b.NumberOfRows, b.NumberOfColumns, b.RowHeight, b.ColumnWidth
        )
        nx.dependent[section.Tag] = (1, "SHT1@0")
        nx.part.Annotations.TableSections.append(section)
        return section

    nx.on_commit("TableSectionBuilder", commit)
    nx.uf.Tabnot = grid.tabnot(sections)
    nx.uf.Obj = NS(DeleteObject=lambda tag: None)
    nx.nx.UF = NS(Tabnot=NS(FitMethod=NS(FIT_METHOD_AUTO_SIZE_ROW=6, FIT_METHOD_NONE=0)))
    nx.grid = grid
    return nx


def revision(nx, rows, widths, position=(250, 290), **extra):
    return run(nx, "nx_create_drawing_table", kind="revision", sheet=nx.sheet_obj.ref,
               position=list(position), rows=rows, widths=widths, **extra)  # fmt: skip


def test_a_cell_nx_wraps_is_named_in_a_warning():
    nx = grid_nx()
    ok = revision(nx, [["A", "2026-10-11"]], [10, 20])
    assert ok.ok, ok
    assert ok.data["rows"] == [["A", "2026-10-\n11"]]
    assert any("wraps 1 cell" in w["message"] and "[1]" in w["message"] for w in ok.warnings)
    wide = revision(nx, [["A", "2026-10-11"]], [10, 30], position=(250, 250))
    assert wide.ok and wide.data["rows"] == [["A", "2026-10-11"]] and not wide.warnings


def test_a_table_past_the_sheet_edge_is_warned_and_row_heights_are_read_back():
    nx = grid_nx()
    ok = revision(nx, [["A", "First"]], [40, 60], position=(415, 5), row_height=8)
    assert ok.ok, ok
    assert ok.data["row_height"] == pytest.approx(8)
    assert any("reaches x=515" in w["message"] for w in ok.warnings), ok.warnings
    edit = run(nx, "nx_edit_drawing_table", table=ok.data["table"]["id"], kind="revision",
               row_height="9 mm", position=[300, 50])  # fmt: skip
    assert edit.ok, edit
    assert edit.data["row_height"] == pytest.approx(9) and not edit.warnings
    # A given row height is kept: NX 2406 would size the row to its text otherwise.
    assert nx.grid.fits and all(6 not in m for m in nx.grid.fits.values())


def test_a_table_without_a_row_height_keeps_nx_auto_sized_rows():
    nx = grid_nx()
    ok = revision(nx, [["A", "First"]], [40, 60])
    assert ok.ok, ok
    assert nx.grid.fits == {}


def test_a_sheet_read_lists_every_table_on_it(monkeypatch):
    nx = grid_nx()
    rev = revision(nx, [["A", "First"]], [10, 60])
    block = type("TitleBlock", (Object,), {})("TB")
    block.OwningPart, block.IsOccurrence = nx.part, False
    block.AnnotationOrigin = point(400, 20, 0)
    nx.dependent[block.Tag] = (1, "SHT1@0")
    nx.part.DraftingManager.TitleBlocks = [block]
    monkeypatch.setattr(title_blocks, "read", lambda tx, obj: [])
    monkeypatch.setattr(title_blocks, "sections", lambda tx, obj: [])
    ok = run(nx, "nx_get_drawing_table", sheet=nx.sheet_obj.ref)
    assert ok.ok, ok
    assert ok.data["table"]["id"] == nx.ref_of(block, "drawing_table")
    kinds = {t["id"]: t["type"] for t in ok.data["tables"]}
    assert kinds == {rev.data["table"]["id"]: "revision", ok.data["table"]["id"]: "title_block"}


def test_a_bend_table_needs_a_flat_pattern_view():
    nx = grid_nx()
    nx.part.Features = []
    iso = make_view(nx, nx.sheet_obj, "ISOMETRIC@2")
    bad = run(nx, "nx_create_drawing_table", kind="bend", view=iso.ref, position=[300, 200])
    assert bad.error["code"] == "NX_WRONG_KIND" and bad.error["field"] == "/view"
    assert "flat_pattern" in bad.error["hint"] and bad.mutation_outcome == "not_started"


def test_balloons_refuse_other_tables_with_their_kind_and_note_when_nothing_is_new():
    nx = grid_nx()
    rev = revision(nx, [["A", "First"]], [10, 60])
    view = make_view(nx, nx.sheet_obj)
    bad = run(nx, "nx_create_balloons", parts_list=rev.data["table"]["id"], view=view.ref)
    assert bad.error["code"] == "NX_WRONG_KIND" and "not a revision" in bad.error["message"]
    assert "kind=parts_list" in bad.error["hint"] and bad.mutation_outcome == "not_started"
    plist = Object("PARTS_LIST")
    plist.OwningPart, plist.IsOccurrence = nx.part, False
    plist.ShowBalloonsInView = lambda shown: None
    nx.part.Annotations.PartsLists.append(plist)
    nx.part.Annotations.IdSymbols = []
    ok = run(nx, "nx_create_balloons", parts_list=nx.ref_of(plist, "drawing_table"),
             view=view.ref)  # fmt: skip
    assert ok.ok, ok
    assert ok.data["balloons"] == [] and "already has one" in ok.data["note"]


# ---- hole tables: the origin point NX measures from ------------------------------------------


class HolePackage:
    def __init__(self, centers, body):
        self.Tag, self._centers, self._body = 4711, centers, body

    def GetOrigins(self):
        return [point(*c) for c in self._centers]

    def GetDirections(self):
        return [point(0, 0, 1) for _ in self._centers]

    def GetBodies(self):
        return [self._body]


def hole_tx(nx):
    body = NS(Tag=99)
    nx.part.Features = [HolePackage([(10, 30, 0)], body)]
    warnings = []
    tx = NS(part=nx.part, warn=lambda code, message: warnings.append(message))
    return tx, body, warnings


def test_a_hole_table_measured_from_the_other_end_of_the_origin_edge():
    nx = drawing_nx()
    view = make_view(nx, nx.make_sheet())
    tx, body, warnings = hole_tx(nx)
    points = {"start": [0.0, 60.0, 0.0], "end": [0.0, 0.0, 0.0]}  # NX measured from the end
    out = {"holes": [{"position": [10.0, 30.0]}], "rows": [["1", "10.00", "30.00"]]}
    used = hole_callouts.verify_table(tx, view, points, [body], out, 1.0, auto=True)
    assert used == "end" and "end point" in warnings[0]
    with pytest.raises(NXError) as error:
        hole_callouts.verify_table(tx, view, points, [body], out, 1.0)
    assert "origin_point 'end'" in error.value.hint
    assert "start point [0.0, 60.0, 0.0]" in error.value.message
    assert hole_callouts.verify_table(tx, view, {"end": points["end"]}, [body], out, 1.0) == "end"


def test_a_hole_table_shows_unsigned_distances():
    nx = drawing_nx()
    view = make_view(nx, nx.make_sheet())
    tx, body, warnings = hole_tx(nx)
    points = {"start": [80.0, 0.0, 0.0], "end": [80.0, 60.0, 0.0]}  # the right edge
    out = {"holes": [{"position": [70.0, 30.0]}], "rows": [["1", "70.00", "30.00"]]}
    assert hole_callouts.verify_table(tx, view, points, [body], out, 1.0) == "start"
    assert not warnings


def test_a_tabular_note_reads_and_edits_as_the_table_that_shows_it():
    """NX lists a table's cells (a ``Table``) apart from the section that shows them; a ref
    of that note reads and edits the section, never an empty ``table``."""
    nx = grid_nx()
    rev = revision(nx, [["A", "First"]], [10, 60])
    section = nx.part.Annotations.TableSections[0]
    note = type("Table", (Object,), {})("NOTE")
    note.Tag = nx.uf.Tabnot.AskTabularNoteOfSection(section.Tag)
    note.OwningPart, note.IsOccurrence = nx.part, False
    ref = nx.ref_of(note, "drawing_table")
    ok = run(nx, "nx_get_drawing_table", table=ref)
    assert ok.ok, ok
    assert ok.data["table"]["id"] == rev.data["table"]["id"]
    assert ok.data["kind"] == "revision" and ok.data["rows"] == [["A", "First"]]
    edit = run(nx, "nx_edit_drawing_table", table=ref, kind="revision", rows=[["B", "Next"]])
    assert edit.ok, edit
    assert edit.data["table"]["id"] == rev.data["table"]["id"]
    assert edit.data["rows"] == [["B", "Next"]]
    both = run(nx, "nx_get_drawing_table", table=ref, sheet=nx.sheet_obj.ref)
    assert both.error["field"] == "/sheet" and "not both" in both.error["message"]
