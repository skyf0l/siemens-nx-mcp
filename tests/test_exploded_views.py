"""Observed NX explosion contracts; native fixtures separately verify NX geometry."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.hardened import IDENTITY
from tests.fakes import Collection, Object

pytestmark = pytest.mark.fake_nx


IDENTITY4 = [IDENTITY[i] + [0] for i in range(3)] + [[0, 0, 0, 1]]


@pytest.fixture
def drawing_save(rig):
    r = rig
    sheet = Object("Sheet")
    r.part.DrawingSheets = Collection([sheet])
    r.part.DrawingSheets.CurrentDrawingSheet = None
    r.part.SaveOptions = NS(DrawingCgmData=True)

    def show():
        r.part.DrawingSheets.CurrentDrawingSheet = sheet

    def exit_drawing():
        r.part.DrawingSheets.CurrentDrawingSheet = None

    sheet.Open = Mock(side_effect=show)
    r.part.Drafting = NS(ExitDraftingApplication=Mock(side_effect=exit_drawing))
    r.sheet = sheet
    return r


def test_save_hidden_part_restores_original_work_and_display(drawing_save, monkeypatch):
    r = drawing_save
    other = Object("Original")
    other.FullPath = "original.prt"
    r.session.Parts.Work = r.session.Parts.Display = other

    def activate(ref, work, display):
        obj = r.e.objects.resolve(ref, expected_kind="part")
        if work:
            r.session.Parts.Work = obj
        if display:
            r.session.Parts.Display = obj

    monkeypatch.setattr(r.e, "_activate_part", activate)
    with r.e._drawing_save_context(r.part):
        assert r.session.Parts.Work is r.part and r.session.Parts.Display is r.part
    assert r.session.Parts.Work is other and r.session.Parts.Display is other


def test_modified_component_drawing_saved_once_before_parent(drawing_save):
    r = drawing_save
    prototype = r.part
    prototype.IsModified = True
    parent = NS()
    r.e._walk_components = lambda p: [
        (NS(Prototype=prototype), ["a"]),
        (NS(Prototype=prototype), ["b"]),
    ]
    save = Mock(wraps=prototype.Save)
    prototype.Save = save
    r.e._save_component_drawing_previews(parent)
    save.assert_called_once()
    assert not prototype.IsModified


def test_forced_drawing_display_restores_sheet_on_failure(drawing_save):
    r = drawing_save
    r.part.SaveOptions.DrawingCgmData = False
    original = r.sheet
    r.part.DrawingSheets.CurrentDrawingSheet = original
    with (
        pytest.raises(RuntimeError, match="plot failed"),
        r.e._drawing_save_context(r.part, force_display=True),
    ):
        r.part.DrawingSheets.CurrentDrawingSheet = Object("Other sheet")
        raise RuntimeError("plot failed")
    assert r.part.DrawingSheets.CurrentDrawingSheet is original


def test_forced_drawing_display_restores_modeling_view(drawing_save):
    r = drawing_save
    r.part.SaveOptions.DrawingCgmData = False
    with r.e._drawing_save_context(r.part, force_display=True):
        assert r.part.DrawingSheets.CurrentDrawingSheet is r.sheet
    assert r.part.DrawingSheets.CurrentDrawingSheet is None
