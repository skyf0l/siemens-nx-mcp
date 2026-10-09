import pytest

from nx_mcp.core.errors import NXError
from nx_mcp.kit import drawing
from tests.kit import fake_nx


def test_on_sheet_opens_and_returns_to_modeling():
    nx = fake_nx()
    sheet = nx.sheet("SHT1", units="mm")
    with drawing.on_sheet(nx.tx, sheet.ref) as sh:
        assert nx.part.DrawingSheets.CurrentDrawingSheet is sheet and sh.units == "mm"
    assert nx.part.DrawingSheets.CurrentDrawingSheet is None
    assert not drawing.work_view_is_drawing(nx.part)


def test_on_sheet_reopens_the_previous_sheet_even_after_a_failure():
    nx = fake_nx()
    first, second = nx.sheet("A"), nx.sheet("B")
    first.Open()
    with pytest.raises(RuntimeError), drawing.on_sheet(nx.tx, second):
        assert nx.part.DrawingSheets.CurrentDrawingSheet is second
        raise RuntimeError("boom")
    assert nx.part.DrawingSheets.CurrentDrawingSheet is first


def test_open_uses_uf_when_open_leaves_no_current_sheet():
    # NX 2406: Sheet.Open() can leave CurrentDrawingSheet None.
    nx = fake_nx()
    sheet = nx.sheet(stuck=True)
    drawing.open_sheet(nx.tx, sheet)
    assert nx.uf_calls == [("OpenDrawing", sheet.Tag)]
    assert nx.part.DrawingSheets.CurrentDrawingSheet is sheet


def test_open_fails_when_nx_cannot_make_the_sheet_current():
    nx = fake_nx()
    sheet = nx.sheet(stuck=True)
    nx.uf.Draw.OpenDrawing = lambda tag: None
    with pytest.raises(NXError) as error:
        drawing.open_sheet(nx.tx, sheet)
    assert error.value.code == "NX_WRONG_APPLICATION"


def test_close_falls_back_to_uf_display_state():
    # NX 2406: ExitDraftingApplication can leave the sheet as the work view.
    nx = fake_nx()
    nx.sheet().Open()
    nx.exit_sticks = True
    drawing.close_sheets(nx.tx)
    assert nx.uf_calls[-1] == ("SetDisplayState", 1)
    assert not drawing.work_view_is_drawing(nx.part)
    nx.sheet().Open()
    nx.uf.Draw.SetDisplayState = lambda state: None
    with pytest.raises(NXError) as error:
        drawing.close_sheets(nx.tx)
    assert error.value.code == "NX_WRONG_APPLICATION"


@pytest.mark.parametrize(
    ("part", "sheet", "scale"), [("mm", "mm", 1), ("in", "mm", 1 / 25.4), ("mm", "in", 25.4)]
)
def test_sheet_points_are_placed_in_part_units(part, sheet, scale):
    nx = fake_nx(units=part)
    with drawing.on_sheet(nx.tx, nx.sheet(units=sheet)) as sh:
        p = sh.point([100, 50])
        assert pytest.approx((100 * scale, 50 * scale, 0)) == (p.X, p.Y, p.Z)
        assert sh.to_sheet(p) == pytest.approx([100, 50])


def test_views_and_refresh():
    nx = fake_nx()
    sheet = nx.sheet()
    view = nx.view(sheet)
    assert drawing.view(nx.tx, view.ref) == (view, sheet)
    nx.view(nx.sheet("OTHER"))
    stray = nx.view(nx.sheet("X"))
    nx.part.DrawingSheets[-1].views.append(view)  # shown on two sheets -> ambiguous owner
    with pytest.raises(NXError) as error:
        drawing.view(nx.tx, view.ref)
    assert error.value.code == "NX_WRONG_OWNER" and error.value.field == "/view"
    assert drawing.refresh(nx.tx, [stray]) == [stray]
    assert drawing.refresh(nx.tx, []) == []
    assert nx.part.DraftingViews.updated == [[stray]]


def test_seen_true_shape():
    assert drawing.seen_true_shape([0, 0, 1], [0, 0, -1])
    assert not drawing.seen_true_shape([0, 0, 1], [0, -1, 0])
