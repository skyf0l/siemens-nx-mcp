"""Leader-note text, hatch edits and read-back, and the undo of drawing edits by
compensation (the restorers of nx_edit_annotation, nx_edit_drawing_view,
nx_set_view_settings, nx_edit_dimension), on the fake."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import run
from tests.kit.objects import Object
from tests.unit.drawing.test_annotations import notes_nx
from tests.unit.drawing.test_sheet_sketch_hatch import _rect, sheet_nx
from tests.unit.drawing.test_views import views_nx

Hatch = type("Hatch", (Object,), {})
Label = type("Label", (Object,), {})


def compensate(nx, result):
    return run(nx, "nx_undo_operation", operation=result.operation_id, mode="compensate")


def test_a_leader_note_takes_new_text_and_an_undo_sets_it_back():
    nx = notes_nx()
    nx.sheet_obj.Open()
    label = Label("LABEL")
    label.OwningPart, label.IsOccurrence = nx.part, False
    lines = ["OLD"]
    label.GetText = lambda: list(lines)
    label.AnnotationOrigin = NS(X=40.0, Y=40.0, Z=0.0)
    nx.part.Labels = [label]
    nx.dependent[label.Tag] = (1, "SHT1@0")

    def retext(b):
        lines[:] = b.Text.TextBlock.SetText.calls[-1][0][0]
        return label

    nx.on_commit("DraftingNoteBuilder", retext)
    ref = nx.ref_of(label, "annotation")
    ok = run(nx, "nx_edit_annotation", annotation=ref, text=["4X DIA 6 THRU", "EQ SP"])
    assert ok.ok, ok
    assert ok.data["text"] == ["4X DIA 6 THRU", "EQ SP"]
    assert nx.last("DraftingNoteBuilder").args == (label,)
    assert ref in [r["id"] for r in ok.changes["modified"]]
    undo = compensate(nx, ok)
    assert undo.ok and undo.data["compensated"], undo
    assert lines == ["OLD"]


def test_an_annotation_move_and_rename_are_set_back_and_a_later_change_is_kept():
    nx = notes_nx()
    nx.sheet_obj.Open()
    note = run(nx, "nx_create_note", text=["N"], origin=[40, 40])
    ref = note.data["annotation"]["id"]
    obj = nx.part.Notes[-1]
    moved = run(nx, "nx_edit_annotation", annotation=ref, position=[60, 70], name="gen")
    assert moved.ok, moved
    assert [w["code"] for w in moved.warnings].count("NX_NOTE") <= 1
    undo = compensate(nx, moved)
    assert undo.ok and undo.data["compensated"], undo
    assert pytest.approx((40, 40)) == (obj.AnnotationOrigin.X, obj.AnnotationOrigin.Y)
    again = run(nx, "nx_edit_annotation", annotation=ref, position=[80, 80])
    assert again.ok and run(nx, "nx_edit_annotation", annotation=ref, position=[90, 90]).ok
    refused = compensate(nx, again)
    assert refused.error["code"] == "NX_DEPENDENCY_ORDER", refused
    assert pytest.approx((90, 90)) == (obj.AnnotationOrigin.X, obj.AnnotationOrigin.Y)


def test_an_annotation_position_off_the_sheet_is_refused_before_nx():
    nx = notes_nx()
    nx.sheet_obj.Open()
    note = run(nx, "nx_create_note", text=["N"], origin=[40, 40])
    off = run(nx, "nx_edit_annotation", annotation=note.data["annotation"]["id"],
              position=[9000, -50])  # fmt: skip
    assert off.error["field"] == "/position" and off.mutation_outcome == "not_started"
    assert "off the 420 x 297 sheet" in off.error["message"]


def hatch_nx():
    """The sheet fake whose hatches are ``Hatch`` objects and take edits."""
    nx = sheet_nx()
    made = nx.hooks["HatchBuilder"]

    def hatch(b):
        existing = b.args[0]
        if isinstance(existing, Object):  # an edit: the reopened hatch takes the values
            names = ("Pattern", "Angle", "Distance", "Material", "AreafillAngle", "Scale")
            existing.values.update({n: b.__dict__[n] for n in names if n in b.__dict__})
            return existing
        h = made(b)
        h.__class__ = Hatch
        return h

    nx.on_commit("HatchBuilder", hatch)
    return nx


def test_a_hatch_is_read_back_in_the_listing_and_edited_and_set_back():
    nx = hatch_nx()
    made = run(nx, "nx_create_hatch", boundary=_rect(nx), pattern="steel", angle=30, spacing=3)
    assert made.ok, made
    ref = made.data["annotation"]["id"]
    listed = run(nx, "nx_list_objects", kind="annotation")
    row = next(r for r in listed.data["items"] if r["id"] == ref)
    assert row["details"]["hatch"] == {"type": "crosshatch", "pattern": "steel",
                                       "angle": pytest.approx(30), "spacing": pytest.approx(3)}  # fmt: skip
    edit = run(nx, "nx_edit_annotation", annotation=ref,
               hatch={"pattern": "brass", "angle": -45, "spacing": 2})  # fmt: skip
    assert edit.ok, edit
    assert edit.data["hatch"]["pattern"] == "brass"
    assert edit.data["hatch"]["spacing"] == pytest.approx(2)
    assert edit.data["hatch"]["angle"] % 360 == pytest.approx(315)
    wrong = run(nx, "nx_edit_annotation", annotation=ref, hatch={"material": "sand"})
    assert wrong.error["field"] == "/hatch/material" and wrong.mutation_outcome == "not_started"
    undo = compensate(nx, edit)
    assert undo.ok and undo.data["compensated"], undo
    back = run(nx, "nx_list_objects", kind="annotation")
    row = next(r for r in back.data["items"] if r["id"] == ref)
    assert row["details"]["hatch"]["pattern"] == "steel"
    assert row["details"]["hatch"]["spacing"] == pytest.approx(3)


def test_a_hatch_edit_of_another_annotation_is_refused():
    nx = notes_nx()
    nx.sheet_obj.Open()
    note = run(nx, "nx_create_note", text=["N"], origin=[40, 40])
    bad = run(nx, "nx_edit_annotation", annotation=note.data["annotation"]["id"],
              hatch={"angle": 10})  # fmt: skip
    assert bad.error["code"] == "NX_WRONG_KIND" and bad.error["field"] == "/hatch"
    assert bad.mutation_outcome == "not_started"


def test_a_view_move_is_set_back_by_compensation():
    nx = views_nx()
    view = nx.make_view(nx.sheet_obj, pos=(120, 200))
    moved = run(nx, "nx_edit_drawing_view", view=view.ref, position=[150, 180])
    assert moved.ok, moved
    undo = compensate(nx, moved)
    assert undo.ok and undo.data["compensated"], undo
    assert view.pos == pytest.approx([120, 200])
    again = run(nx, "nx_edit_drawing_view", view=view.ref, scale=2)
    assert again.ok and run(nx, "nx_edit_drawing_view", view=view.ref, scale=3).ok
    refused = compensate(nx, again)
    assert refused.error["code"] == "NX_DEPENDENCY_ORDER", refused
    assert view.scale == 3


def test_view_settings_are_set_back_by_compensation():
    from tests.unit.drawing.test_view_settings import settings_nx

    nx, view, _front = settings_nx()
    done = run(nx, "nx_set_view_settings", views=[view.ref],
               settings={"threads": "schematic", "smooth_edges": False})  # fmt: skip
    assert done.ok, done
    undo = compensate(nx, done)
    assert undo.ok and undo.data["compensated"], undo
    assert view.settings["ViewStyleThreads"]["StandardData"] == 4
    assert view.settings["ViewStyleSmoothEdges"]["SmoothEdge"] is True
