"""Drawing tables, export and the drawing hooks (sheet locators, refreshers, listers)."""

import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from nx_mcp.core import catalogue
from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import Object
from tests.unit.drawing.fakes import drawing_nx, make_view


def tables_nx():
    nx = drawing_nx()
    nx.sheet_obj = nx.make_sheet()
    nx.part.Annotations = NS(PartsLists=[], BendTables=[], TableSections=[])
    return nx


def table(nx, kind_attr=None, name="TableSection"):
    obj = type(name, (Object,), {})("T")
    obj.OwningPart, obj.IsOccurrence = nx.part, False
    attrs = {"NX_MCP_DRAWING_TABLE_V1": kind_attr} if kind_attr else {}
    obj.HasUserAttribute = lambda k, *_: k in attrs
    obj.GetStringAttribute = lambda k: attrs[k]
    return obj


def test_grid_tables_need_rectangular_rows_and_positive_widths():
    nx = tables_nx()
    sheet = nx.sheet_obj.ref
    for args, field in [
        ({"rows": [["A", "B"], ["C"]], "widths": [10, 20]}, "/rows"),
        ({"rows": [["A"]], "widths": [-5]}, "/widths"),
        ({"widths": [10]}, "/rows"),
        ({"rows": [["A"]]}, "/widths"),
    ]:
        bad = run(
            nx, "nx_create_drawing_table", kind="revision", sheet=sheet, position=[1, 2], **args
        )
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", (args, bad)
    bend = run(nx, "nx_create_drawing_table", kind="bend", position=[1, 2])
    assert bend.error["field"] == "/view"
    dup = run(nx, "nx_create_drawing_table", kind="bend", view=make_view(nx, nx.sheet_obj).ref,
              position=[1, 2], columns=["id", "id"])  # fmt: skip
    assert dup.error["field"] == "/columns/1"


def test_bend_table_info_keeps_the_bend_columns(monkeypatch):
    # Regression: the hole/grid branch overwrote bend columns with widths (KeyError 'field').
    from nx_mcp.domains.drawing import tables

    tab = Mock(AskNmColumns=Mock(return_value=2), AskColumnWidth=Mock(return_value=5.0))
    tx = NS(ref=lambda o, k: "dt:1", uf=NS(Tabnot=tab))
    monkeypatch.setattr(tables, "table_tag", lambda tx, obj: 7)
    monkeypatch.setattr(tables, "cells", lambda tx, tag: [["1", "90"]])
    monkeypatch.setattr(tables.drawing, "sheet_of_annotation", lambda tx, obj: None)
    cols = [{"field": "id"}, {"field": "angle"}]
    monkeypatch.setattr(tables, "_bend_columns", lambda tx, obj: (cols, True))
    out = tables.table_info(tx, object(), "bend")
    assert out["columns"] == cols and out["automatic"] is True


def test_parts_lists_need_an_assembly_and_one_per_part_when_the_site_says_so():
    nx = tables_nx()
    no_asm = run(nx, "nx_create_drawing_table", kind="parts_list", sheet=nx.sheet_obj.ref,
                 position=[1, 2])  # fmt: skip
    assert no_asm.error["code"] == "NX_WRONG_PART_CONTEXT"
    root = NS(GetChildren=lambda: [NS(GetChildren=lambda: [], Name="C1", JournalIdentifier="C1")])
    nx.part.ComponentAssembly = NS(RootComponent=root, Explosions=[])
    existing = table(nx, name="PartsList")
    nx.part.Annotations.PartsLists.append(existing)
    nx.session.GetEnvironmentVariableValue = lambda name: "1"
    second = run(nx, "nx_create_drawing_table", kind="parts_list", sheet=nx.sheet_obj.ref,
                 position=[1, 2])  # fmt: skip
    assert second.error["code"] == "NX_ALREADY_EXISTS"
    assert second.error["details"]["table"]["kind"] == "drawing_table"
    assert second.mutation_outcome == "not_started"


def test_the_section_of_a_parts_list_or_hole_table_is_not_a_table_of_its_own():
    # Live NX 2406 lists the section of each parts list and hole table in TableSections.
    from nx_mcp.domains.drawing.tables import all_tables

    nx = tables_nx()
    plist, hole = table(nx, name="PartsList"), table(nx, name="HoleTable")
    plist_section, hole_section, note = table(nx), table(nx), table(nx)
    owner = {int(plist_section.Tag): plist.Tag, int(hole_section.Tag): hole.Tag,
             int(note.Tag): 999}  # fmt: skip
    nx.uf.Tabnot = NS(AskTabularNoteOfSection=lambda tag: owner[int(tag)])
    nx.part.Annotations.PartsLists.append(plist)
    nx.part.Annotations.HoleTables = [hole]
    nx.part.Annotations.TableSections.extend([plist_section, hole_section, note])
    tx = NS(part=nx.part, uf=nx.uf, nx=nx)
    found = {int(o.Tag): kind for o, kind in all_tables(tx)}
    assert found == {int(plist.Tag): "parts_list", int(hole.Tag): "hole", int(note.Tag): "table"}


def test_table_edits_must_match_the_kind_and_its_options():
    nx = tables_nx()
    rev = table(nx, "revision")
    nx.part.Annotations.TableSections.append(rev)
    ref = nx.ref_of(rev, "drawing_table")
    wrong = run(nx, "nx_edit_drawing_table", table=ref, kind="title_block", rows=[["A"]])
    assert wrong.error["field"] == "/kind" and wrong.error["details"]["kind"] == "revision"
    option = run(nx, "nx_edit_drawing_table", table=ref, kind="revision", automatic=True)
    assert option.error["code"] == "NX_UNSUPPORTED_OPTION" and option.error["field"] == "/automatic"
    empty = run(nx, "nx_edit_drawing_table", table=ref, kind="revision")
    assert empty.error["category"] == "invalid_argument"
    for result in (wrong, option, empty):
        assert result.mutation_outcome == "not_started"


def test_parts_list_column_edits_are_checked_before_nx():
    nx = tables_nx()
    plist = table(nx, name="PartsList")
    nx.part.Annotations.PartsLists.append(plist)
    nx.uf.Tabnot = NS(AskNmColumns=lambda tag: 3, AskNmSections=lambda tag: 0)
    ref = nx.ref_of(plist, "drawing_table")
    for columns, field in [
        ([{"action": "append", "title": "X", "width": 20}], "/columns/0/field"),
        ([{"action": "append", "index": 1, "title": "X", "width": 20, "field": "f"}],
         "/columns/0/index"),
        ([{"action": "edit", "index": 9, "title": "X"}], "/columns/0/index"),
        ([{"action": "edit", "index": 1}], "/columns/0"),
        ([{"action": "remove", "index": 1, "title": "X"}], "/columns/0"),
        ([{"action": "edit", "index": 1, "width": -1}], "/columns/0/width"),
    ]:  # fmt: skip
        bad = run(nx, "nx_edit_drawing_table", table=ref, kind="parts_list", columns=columns)
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", (columns, bad)
    rows = run(nx, "nx_edit_drawing_table", table=ref, kind="parts_list", rows=[["1", "X"]])
    assert rows.error["field"] == "/rows" and "nx_set_attributes" in rows.error["hint"]


def test_export_rules_and_a_pdf_published_from_the_staging_area():
    nx = tables_nx()
    other = nx.make_sheet("SHT2")
    root = services(nx).root
    mismatch = run(nx, "nx_export_drawing", path="out/a.dxf", format="PDF")
    assert mismatch.error["field"] == "/path" and mismatch.mutation_outcome == "not_started"
    two = run(nx, "nx_export_drawing", path="out/a.dxf")  # format follows the extension
    assert two.error["field"] == "/sheets" and "sheets: [one sheet" in two.error["hint"]
    assert [s["name"] for s in two.error["details"]["sheets"]] == ["SHT1", "SHT2"]
    assert run(nx, "nx_export_drawing", path="out/a.pdf", format="tiff").error["field"] == (
        "/format"
    )
    nx.part.PlotManager = NS(CreatePrintPdfbuilder=lambda: nx.builder("PrintPdfBuilder"))

    def plot(b):
        Path(b.Filename).write_bytes(b"%PDF-1.7 fake")

    nx.on_commit("PrintPdfBuilder", plot)
    nx.part.SaveOptions = NS(DrawingCgmData=False)
    ok = run(nx, "nx_export_drawing", path="out/a.pdf", format="PDF", sheets=["sht2"])
    assert ok.ok, ok
    assert ok.data["path"] == "out/a.pdf" and ok.data["size"] == len(b"%PDF-1.7 fake")
    assert (root / "out" / "a.pdf").read_bytes().startswith(b"%PDF-")
    assert [s["id"] for s in ok.data["sheets"]] == [other.ref]  # by its name, any case
    assert nx.live_builders == 0 and nx.part.DrawingSheets.CurrentDrawingSheet is None
    nx.on_commit("PrintPdfBuilder", lambda b: Path(b.Filename).write_bytes(b"not a pdf"))
    bad = run(nx, "nx_export_drawing", path="out/b.pdf")
    assert bad.error["code"] == "NX_EXPORT_FAILED" and bad.mutation_outcome == "rolled_back"
    assert not (root / "out" / "b.pdf").exists()


def test_export_without_sheets_is_refused():
    nx = drawing_nx()
    bad = run(nx, "nx_export_drawing", path="x.pdf")
    assert bad.error["code"] == "NX_WRONG_APPLICATION" and bad.mutation_outcome == "not_started"


def test_hooks_are_registered_for_drawing_kinds():
    catalogue.load()
    for kind in ("annotation", "dimension", "drawing_table"):
        assert kind in catalogue.hooks("sheet")
    assert {"drawing_view", "drawing_table"} <= set(catalogue.hooks("refresher"))
    assert {"drawing_sheet", "drawing_view", "dimension", "annotation", "drawing_table"} <= set(
        catalogue.hooks("lister")
    )
    assert {"annotation", "dimension", "drawing_table"} <= set(catalogue.hooks("deleter"))


def test_update_annotations_rebuilds_automatic_bend_tables_and_detail_boundaries():
    nx = tables_nx()
    view = make_view(nx, nx.sheet_obj, "Top@1", pos=(100, 100))
    detail = make_view(nx, nx.sheet_obj, "DETAIL@2", pos=(300, 100), native="DetailView")
    record = json.dumps({"parent": f"h{view.Tag}", "center": [0, 0, 0], "radius": 5})
    detail.SetAttribute("NX_MCP_DETAIL_V1", record)
    nx.part.DraftingViews.CreateDetailViewBuilder = lambda existing: nx.builder(
        "DetailViewBuilder", existing
    )
    rows = {"value": [["1", "90"]]}
    bend = table(nx, name="BendTable")
    nx.uf.Tabnot = NS(
        AskNmColumns=lambda tag: 2,
        AskNmRows=lambda tag: len(rows["value"]),
        AskNthRow=lambda tag, i: i,
        AskNthColumn=lambda tag, j: j,
        AskCellAtRowCol=lambda r, c: (r, c),
        AskEvaluatedCellText=lambda cell: rows["value"][cell[0]][cell[1]],
        AskNmSections=lambda tag: 0,
    )
    nx.part.Annotations.BendTables = Mock()
    nx.part.Annotations.BendTables.__iter__ = lambda self: iter([bend])
    rebuilt = NS(Tag=bend.Tag)

    def make_bend_builder(existing):
        b = nx.builder("BendTableBuilder", existing)
        object.__setattr__(b, "Style", NS(BendTable=NS(AutomaticUpdate=True)))
        return b

    nx.part.Annotations.BendTables.CreateBendTableBuilder = make_bend_builder

    def commit(b):
        rows["value"] = [["1", "75"]]
        return rebuilt

    nx.on_commit("BendTableBuilder", commit)
    ok = run(nx, "nx_update_annotations")
    assert ok.ok, ok
    kinds = sorted(r["kind"] for r in ok.data["refreshed"])
    assert kinds == ["drawing_table", "drawing_view"]
    assert nx.last("DetailViewBuilder").commits == 1 and nx.live_builders == 0
