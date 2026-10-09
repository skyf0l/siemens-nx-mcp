"""Notes, centerlines, symbols, GD&T, annotation edits and refreshes on the fake."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.domains.drawing import annotations, gdt
from tests.kit import run
from tests.kit.objects import Object, point
from tests.unit.drawing.fakes import arc_edge, drawing_nx, install_eval, line_edge, make_view


def notes_nx(sheet_units="mm"):
    nx = drawing_nx()
    nx.sheet_obj = nx.make_sheet(units=sheet_units)
    nx.top = make_view(nx, nx.sheet_obj, pos=(100, 100))
    nx.part.Annotations = NS(
        PartsLists=[],
        BendTables=[],
        CreateDraftingNoteBuilder=lambda existing: nx.builder("DraftingNoteBuilder", existing),
        Centerlines=[],
    )

    def note(b):
        text = b.Text.TextBlock.SetText.calls[-1][0][0]
        origin = b.Origin.Origin.SetValue.calls[-1][0][2]
        obj = Object("NOTE")
        obj.GetText, obj.AnnotationOrigin = (lambda: list(text)), origin
        obj.OwningPart, obj.IsOccurrence = nx.part, False
        nx.part.Notes.append(obj)
        nx.dependent[obj.Tag] = (1, f"{nx.sheet_obj.Name}@0")
        return obj

    nx.on_commit("DraftingNoteBuilder", note)
    return nx


def test_note_text_and_origin_are_read_back_in_sheet_units():
    nx = notes_nx(sheet_units="in")
    nx.sheet_obj.Open()
    ok = run(nx, "nx_create_note", text=["BREAK EDGES", "", "S235"], origin=[1, 7.5])
    assert ok.ok, ok
    assert ok.data["text"] == ["BREAK EDGES", "", "S235"]
    assert ok.data["origin"] == pytest.approx([1, 7.5]) and ok.units["sheet_length"] == "in"
    placed = nx.last("DraftingNoteBuilder").Origin.Origin.SetValue.calls[-1][0][2]
    assert pytest.approx((25.4, 190.5)) == (placed.X, placed.Y)  # NX takes part units
    assert nx.live_builders == 0


def test_note_rules_are_checked_before_nx():
    nx = notes_nx()
    hole = arc_edge(nx, (25, 25, 20), 8)
    for args, field in [
        ({"text": [" "], "origin": [1, 2], "sheet": nx.sheet_obj.ref}, "/text"),
        ({"text": ["a\nb"], "origin": [1, 2]}, "/text/0"),
        ({"text": ["X"], "origin": [1, 2], "leader_to": hole.ref}, "/view"),
        ({"text": ["X"], "origin": [1, 2], "view": nx.top.ref}, "/view"),
    ]:
        bad = run(nx, "nx_create_note", **args)
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", (args, bad)
    no_sheet = run(nx, "nx_create_note", text=["X"], origin=[1, 2])
    assert no_sheet.error["code"] == "NX_WRONG_APPLICATION"
    assert no_sheet.mutation_outcome == "not_started"


def test_centerline_refusals():
    nx = notes_nx()
    side = make_view(
        nx, nx.sheet_obj, "Front@2", pos=(100, 40), axes=((1, 0, 0), (0, 0, 1), (0, -1, 0))
    )
    hole = arc_edge(nx, (25, 25, 20), 8)
    back = line_edge(nx, (0, 50, 20), (70, 50, 20))
    left = line_edge(nx, (0, 0, 20), (0, 50, 20))
    install_eval(nx, [hole, back, left])
    cases = [
        ({"view": side.ref, "kind": "center_mark", "objects": [hole.ref]}, "/objects/0",
         "NX_UNSUPPORTED_GEOMETRY"),
        ({"view": nx.top.ref, "kind": "center_mark", "objects": [back.ref]}, "/objects/0",
         "NX_WRONG_GEOMETRY"),
        ({"view": nx.top.ref, "kind": "between_edges", "objects": [back.ref, left.ref]}, "/objects",
         "NX_INVALID_ARGUMENT"),
        ({"view": nx.top.ref, "kind": "between_edges", "objects": [back.ref]}, "/objects",
         "NX_INVALID_ARGUMENT"),
        ({"view": nx.top.ref, "kind": "axis", "objects": [hole.ref, back.ref]}, "/objects",
         "NX_INVALID_ARGUMENT"),
    ]  # fmt: skip
    for args, field, code in cases:
        bad = run(nx, "nx_create_centerline", **args)
        assert (bad.error["field"], bad.error["code"]) == (field, code), (args, bad)
        assert bad.mutation_outcome == "not_started"


def test_symbol_variants_need_their_fields():
    nx = notes_nx()
    face = nx.face()
    weld = run(nx, "nx_create_drawing_symbol", view=nx.top.ref, object=face.ref, origin=[1, 2],
               symbol="weld", size=3)  # fmt: skip
    assert weld.error["field"] == "/weld"
    finish = run(nx, "nx_create_drawing_symbol", view=nx.top.ref, object=face.ref, origin=[1, 2],
                 symbol="surface_finish")  # fmt: skip
    assert finish.error["field"] == "/roughness_ra"
    assert annotations.number_text(3.2) == "3.2" and annotations.number_text(5.0) == "5"


def test_edit_moves_on_the_sheet_and_refuses_ignored_moves_and_wrong_shapes():
    nx = notes_nx()
    nx.sheet_obj.Open()
    note = run(nx, "nx_create_note", text=["N"], origin=[40, 40])
    ref = note.data["annotation"]["id"]
    ok = run(nx, "nx_edit_annotation", annotation=ref, position=[45, 50], name="GENERAL")
    assert ok.ok, ok
    assert ok.data["position"] == [45.0, 50.0] and ok.data["frame"] == "sheet"
    assert ok.data["name"] == "GENERAL" and ok.data["annotation"]["kind"] == "annotation"
    shape = run(nx, "nx_edit_annotation", annotation=ref, position=[1, 2, 3])
    assert shape.error["field"] == "/position" and shape.mutation_outcome == "not_started"
    mark = Object("CENTER")
    mark.OwningPart, mark.IsOccurrence = nx.part, False
    fixed = point(10.0, 10.0, 0.0)
    type(mark).AnnotationOrigin = property(lambda s: fixed, lambda s, v: None)  # NX ignores it
    nx.dependent[mark.Tag] = (1, "SHT1@0")
    moved = run(nx, "nx_edit_annotation", annotation=nx.ref_of(mark, "annotation"), position=[5, 5])
    assert (
        moved.error["code"] == "NX_UNSUPPORTED_OPTION" and moved.mutation_outcome == "rolled_back"
    )
    del type(mark).AnnotationOrigin


def test_annotations_resolve_their_sheet_for_unit_strings():
    nx = notes_nx(sheet_units="in")
    nx.sheet_obj.Open()
    note = run(nx, "nx_create_note", text=["N"], origin=[1, 1])
    other = nx.make_sheet("MM", units="mm")
    other.Open()  # the current sheet is now in mm; the note's sheet is in inches
    ok = run(nx, "nx_edit_annotation", annotation=note.data["annotation"]["id"], position=[2, 2])
    assert ok.units["sheet_length"] == "in" and ok.data["position"] == pytest.approx([2, 2])


def test_feature_control_frame_rules():
    flat = {"characteristic": "flatness", "tolerance": 0.05}
    gdt.check_frame(flat)
    for frame, field in [
        ({**flat, "datums": ["a:1"]}, "/frame/datums"),
        ({**flat, "material": "mmc"}, "/frame/material"),
        ({**flat, "projected_height": 2.0}, "/frame/material"),
        ({"characteristic": "position", "tolerance": 0.1, "datums": ["a:1", "a:1"]},
         "/frame/datums/1"),
        ({"characteristic": "position", "tolerance": 0.1, "datums": ["a:1"],
          "datum_material": ["mmc", "lmc"]}, "/frame/datum_material"),
    ]:  # fmt: skip
        with pytest.raises(NXError) as error:
            gdt.check_frame(frame)
        assert error.value.field == field, frame


def test_gdt_refuses_before_nx_and_needs_its_variant_fields():
    nx = notes_nx()
    face = nx.face()
    letter = run(nx, "nx_create_gdt", kind="datum_feature", faces=[face.ref], position=[0, 0, 0])
    assert letter.error["field"] == "/letter"
    lower = run(nx, "nx_create_gdt", kind="datum_feature", faces=[face.ref], position=[0, 0, 0],
                letter="a")  # fmt: skip
    assert lower.error["field"] == "/letter"
    frame = run(nx, "nx_create_gdt", kind="fcf", faces=[face.ref], position=[0, 0, 0])
    assert frame.error["field"] == "/frame"
    edge = nx.edge((0, 0, 0), (1, 0, 0))
    wrong = run(nx, "nx_create_gdt", kind="fcf", faces=[edge.ref], position=[0, 0, 0],
                frame={"characteristic": "flatness", "tolerance": 0.05})  # fmt: skip
    assert wrong.error["category"] == "wrong_kind" and wrong.error["field"] == "/faces/0"
    for result in (letter, lower, frame, wrong):
        assert result.mutation_outcome == "not_started"
