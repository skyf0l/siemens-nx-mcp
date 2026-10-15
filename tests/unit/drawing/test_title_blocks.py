"""Title-block cells: read by sheet or table, fill by label or row/column, attribute links,
locks, and the refresh after an attribute change."""

import re
from types import SimpleNamespace as NS

from nx_mcp.domains.drawing import title_blocks
from tests.kit import run
from tests.kit.objects import Object
from tests.unit.drawing.fakes import drawing_nx

BOUND = re.compile(r"<W@([^<>]+)>")


class TitleBlock(Object):
    def DoUpdate(self):
        self.updates = getattr(self, "updates", 0) + 1


class Cell(NS):
    """A defined title-block cell: label, lock, stored text, place (note, row, column)."""


class Model:
    """NX's title blocks over UF tabular notes, as far as the drawing tools use them."""

    def __init__(self, nx, layout):
        self.nx, self.attrs = nx, {}
        self.block = TitleBlock("TB")
        self.block.OwningPart, self.block.IsOccurrence = nx.part, False
        self.block.AnnotationOrigin = NS(X=410.0, Y=10.0, Z=0.0)
        nx.dependent[self.block.Tag] = (1, "SHT1@0")
        self.cells = []
        for note, (rows, cols, defined) in enumerate(layout, start=1):
            for i in range(rows):
                for j in range(cols):
                    label, lock, text = defined.get((i, j), (None, False, ""))
                    self.cells.append(Cell(note=note, row=i, col=j, label=label, lock=lock,
                                           text=text, tag=note * 10000 + i * 100 + j))  # fmt: skip
        self.layout = layout
        nx.part.DraftingManager = NS(TitleBlocks=Blocks(self))
        nx.uf.Tabnot = self.tabnot()
        nx.nx.TaggedObjectManager = NS(GetTaggedObject=lambda tag: NS(Tag=tag))

    def by_tag(self, tag):
        return next(c for c in self.cells if c.tag == int(tag))

    def evaluate(self, text):
        return BOUND.sub(lambda m: self.attrs.get(m.group(1), ""), text)

    def tabnot(self):
        def stored(tag):  # NX binds a reference to the work part: <WID5*0@X>
            return BOUND.sub(lambda m: f"<WID5*0@{m.group(1)}>", self.by_tag(tag).text)

        return NS(
            AskTabularNoteOfSection=lambda s: int(s) // 1000,
            AskNmRows=lambda n: self.layout[n - 1][0],
            AskNmColumns=lambda n: self.layout[n - 1][1],
            AskNthRow=lambda n, i: n * 100 + i,
            AskNthColumn=lambda n, j: n * 100 + 50 + j,
            AskCellAtRowCol=lambda r, c: (r // 100) * 10000 + (r % 100) * 100 + (c % 100 - 50),
            AskRowOfCell=lambda t: self.by_tag(t).note * 100 + self.by_tag(t).row,
            AskColumnOfCell=lambda t: self.by_tag(t).note * 100 + 50 + self.by_tag(t).col,
            AskTabularNoteOfColumn=lambda c: int(c) // 100,
            AskCellText=stored,
            AskEvaluatedCellText=lambda t: self.evaluate(self.by_tag(t).text),
            AskNmSections=lambda tag: 0,
        )


class Blocks(list):
    def __init__(self, model):
        super().__init__([model.block])
        self.model = model
        self.builders = []

    def CreateDefineTitleBlockBuilder(self, block):
        b = DefineBuilder(self.model)
        self.builders.append(b)
        return b

    def CreateEditTitleBlockBuilder(self, blocks):
        b = EditBuilder(self.model)
        self.builders.append(b)
        return b

    def CreateCellBuilder(self, cell, lock, label):
        return NS(Cell=cell, Lock=lock, Label=label)


class CellList:
    def __init__(self, items):
        self.items = items

    def GetContents(self):
        return list(self.items)

    def Clear(self):
        self.items.clear()

    def Append(self, item):
        self.items.append(item)


class DefineBuilder:
    def __init__(self, model):
        self.model, self.destroyed = model, False
        notes = range(1, len(model.layout) + 1)
        self.Components = NS(GetArray=lambda: [NS(Tag=n * 1000) for n in notes])
        self.Cells = CellList(
            [NS(Cell=NS(Tag=c.tag), Lock=c.lock, Label=c.label) for c in model.cells if c.label]
        )

    def Commit(self):
        defined = {int(b.Cell.Tag): b for b in self.Cells.items}
        for c in self.model.cells:
            b = defined.get(c.tag)
            c.label, c.lock = (b.Label, b.Lock) if b else (None, False)
        return self.model.block

    def Destroy(self):
        self.destroyed = True


class EditBuilder:
    def __init__(self, model):
        self.model, self.values, self.destroyed = model, {}, False

    def SetCellValueForLabel(self, label, text):
        self.values[label] = text

    def Commit(self):
        for c in self.model.cells:
            if c.label in self.values and not c.lock:
                c.text = self.values[c.label]
        return self.model.block

    def Destroy(self):
        self.destroyed = True


TEMPLATE = [
    (2, 2, {(0, 0): ("Label1", True, "DRAWN BY"), (0, 1): ("Drawn By", False, ""),
            (1, 0): ("Label3", True, "TITLE"), (1, 1): ("Label4", True, "")}),
    (1, 1, {(0, 0): ("Label5", True, "SCALE 1:1")}),
]  # fmt: skip


def template_nx():
    nx = drawing_nx()
    nx.sheet_obj = nx.make_sheet()
    nx.part.Annotations = NS(PartsLists=[], BendTables=[], TableSections=[])
    model = Model(nx, TEMPLATE)
    nx.nx.NXObject = NS(AttributeType=NS(String=1, Any=0))
    nx.part.HasUserAttribute = lambda title, *_: title in model.attrs
    return nx, model, nx.ref_of(model.block, "drawing_table")


def edit(nx, ref, cells):
    return run(nx, "nx_edit_drawing_table", table=ref, kind="title_block", cells=cells)


def test_a_sheets_title_block_lists_its_cells_across_sections():
    nx, model, ref = template_nx()
    model.cells[2].text = "<W@$SH_SHEET_SIZE>"
    model.attrs["$SH_SHEET_SIZE"] = "A3"
    got = run(nx, "nx_get_drawing_table", sheet=nx.sheet_obj.ref)
    assert got.ok, got
    cells = {c["label"]: c for c in got.data["cells"]}
    assert set(cells) == {"Label1", "Drawn By", "Label3", "Label4", "Label5"}
    assert cells["Label5"] == {"label": "Label5", "section": 1, "row": 0, "column": 0,
                                    "locked": True, "text": "SCALE 1:1", "value": "SCALE 1:1"}  # fmt: skip
    assert "rows" not in got.data  # several sections: cells only
    assert run(nx, "nx_get_drawing_table", table=ref).data["cells"] == got.data["cells"]
    both = run(nx, "nx_get_drawing_table", table=ref, sheet=nx.sheet_obj.ref)
    assert both.error["code"] == "NX_INVALID_ARGUMENT"
    other = nx.make_sheet("SHT2")
    none = run(nx, "nx_get_drawing_table", sheet=other.ref)
    assert none.ok and none.data["table"] is None and none.data["tables"] == []


def test_cells_take_text_and_attribute_links_that_follow_the_attribute():
    nx, model, ref = template_nx()
    model.attrs.update(DB_PART_NO="PN-100", DB_SCALE="1:2")
    ok = edit(nx, ref, [
        {"label": "Drawn By", "text": "AGENT"},
        {"label": "Label4", "attribute": "DB_PART_NO", "lock": False},
        {"section": 1, "row": 0, "column": 0, "text": "SCALE <W@DB_SCALE>", "lock": False},
    ])  # fmt: skip
    assert ok.ok and ok.mutation_outcome == "committed", ok
    cells = {c["label"]: c for c in ok.data["cells"]}
    assert cells["Drawn By"]["value"] == "AGENT"
    title = cells["Label4"]
    assert title["text"] == "<W@DB_PART_NO>" and title["attribute"] == "DB_PART_NO"
    assert title["value"] == "PN-100" and title["locked"] is False
    assert cells["Label5"]["text"] == "SCALE <W@DB_SCALE>" and "attribute" not in cells["Label5"]
    assert all(b.destroyed for b in nx.part.DraftingManager.TitleBlocks.builders)
    model.attrs["DB_PART_NO"] = "PN-200"
    assert run(nx, "nx_update_annotations").data["refreshed"] == []  # NX re-evaluated already
    refreshed = run(nx, "nx_update_annotations", objects=[ref])
    assert [r["id"] for r in refreshed.data["refreshed"]] == [ref]
    assert model.block.updates == 2
    again = run(nx, "nx_get_drawing_table", table=ref).data["cells"]
    assert next(c for c in again if c["label"] == "Label4")["value"] == "PN-200"


def test_locked_unknown_and_repeated_cells_are_refused_before_nx():
    nx, model, ref = template_nx()
    for cells, code, field in [
        ([{"label": "Label3", "text": "X"}], "NX_READ_ONLY", "/cells/0"),
        ([{"label": "Nope", "text": "X"}], "NX_NOT_FOUND", "/cells/0"),
        ([{"row": 9, "column": 9, "text": "X"}], "NX_NOT_FOUND", "/cells/0"),
        ([{"label": "Drawn By", "text": "A"}, {"row": 0, "column": 1, "text": "B"}],
         "NX_INVALID_ARGUMENT", "/cells/1"),
        ([{"label": "Drawn By", "row": 0, "column": 1, "text": "A"}], "NX_INVALID_ARGUMENT",
         "/cells/0"),
        ([{"row": 0, "text": "A"}], "NX_INVALID_ARGUMENT", "/cells/0"),
        ([{"label": "Drawn By", "text": "A", "attribute": "B"}], "NX_INVALID_ARGUMENT",
         "/cells/0"),
        ([{"label": "Drawn By"}], "NX_INVALID_ARGUMENT", "/cells/0"),
        ([{"label": "Drawn By", "attribute": "DB_MISSING"}], "NX_NOT_FOUND", "/cells/0"),
        ([{"label": "Drawn By", "text": "<W@$SH_X> <W@DB_MISSING>"}], "NX_NOT_FOUND",
         "/cells/0"),
        ([{"label": "Drawn By", "text": "BY <W$=@DB_MISSING>"}], "NX_NOT_FOUND", "/cells/0"),
    ]:  # fmt: skip
        bad = edit(nx, ref, cells)
        assert (bad.error["code"], bad.error["field"]) == (code, field), (cells, bad)
        assert bad.mutation_outcome == "not_started"
    grid = run(nx, "nx_edit_drawing_table", table=ref, kind="title_block", rows=[["A", "B"]])
    assert grid.error["code"] == "NX_UNSUPPORTED_OPTION" and grid.error["field"] == "/rows"
    assert all(c.text != "X" for c in model.cells)


def test_a_lock_change_is_read_back():
    nx, model, ref = template_nx()
    ok = edit(nx, ref, [{"label": "Drawn By", "text": "AGENT", "lock": True}])
    assert ok.ok, ok
    cell = next(c for c in ok.data["cells"] if c["label"] == "Drawn By")
    assert cell["locked"] is True and cell["value"] == "AGENT"


def test_created_title_blocks_define_every_cell_and_keep_labels_by_place():
    nx = drawing_nx()
    model = Model(nx, [(2, 2, {})])
    tb = model.block
    title_blocks.define_cells(nx.tx, tb)
    assert [(c.label, c.lock) for c in model.cells] == [
        ("R0C0", False), ("R0C1", False), ("R1C0", False), ("R1C1", False)
    ]  # fmt: skip
    model.cells[0].label, model.cells[0].lock = "Part", True
    kept = title_blocks.clear_cells(nx.tx, tb)
    assert kept[(0, 0)] == ("Part", True) and all(c.label is None for c in model.cells)
    title_blocks.define_cells(nx.tx, tb, kept)
    assert [(c.label, c.lock) for c in model.cells][:2] == [("Part", True), ("R0C1", False)]


def test_bound_references_read_as_written():
    assert title_blocks.normalize("REV <WID12*0@DB_PART_REV>") == "REV <W@DB_PART_REV>"
    assert title_blocks.reference("DB_MATERIAL") == "<W@DB_MATERIAL>"


def test_labels_match_ignoring_case_and_a_miss_names_the_label():
    nx, model, ref = template_nx()
    ok = edit(nx, ref, [{"label": "drawn  BY", "text": "AGENT"}])
    assert ok.ok, ok
    assert next(c for c in ok.data["cells"] if c["label"] == "Drawn By")["value"] == "AGENT"
    miss = edit(nx, ref, [{"label": "drawer", "text": "X"}])
    assert miss.error["code"] == "NX_NOT_FOUND" and "'drawer'" in miss.error["message"]
    assert "Drawn By" in miss.error["details"]["labels"]
