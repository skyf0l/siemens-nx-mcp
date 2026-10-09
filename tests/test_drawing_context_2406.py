"""NX 2406 drawing context: current sheet, sheet screenshots and datum-free views."""

import struct
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.runtime import NXToolError
from tests.fakes import Body, Object

pytestmark = pytest.mark.fake_nx


class Sheets(list):
    CurrentDrawingSheet = None


def sheet_part(rig, opens=True):
    """A work part whose Sheet.Open() can, like NX 2406, leave no current sheet."""
    sheets = Sheets()
    sheet = Object("Sheet1")

    def open_sheet():
        if opens:
            sheets.CurrentDrawingSheet = sheet

    sheet.Open = Mock(side_effect=open_sheet)
    sheets.append(sheet)
    rig.part.DrawingSheets = sheets
    return sheets, sheet


@pytest.mark.parametrize("uf_opens", [True, False])
def test_open_sheet_falls_back_to_uf_and_verifies(rig, uf_opens):
    sheets, sheet = sheet_part(rig, opens=False)

    def open_drawing(tag):
        assert tag == sheet.Tag
        if uf_opens:
            sheets.CurrentDrawingSheet = sheet

    rig.uf.Draw = NS(OpenDrawing=Mock(side_effect=open_drawing))
    if uf_opens:
        assert rig.e._open_drawing_sheet(sheet) is sheet
    else:
        with pytest.raises(NXToolError) as error:
            rig.e._open_drawing_sheet(sheet)
        assert error.value.code == "NX_DRAWING_CONTEXT"
    rig.uf.Draw.OpenDrawing.assert_called_once()
    rig.uf.Draw.OpenDrawing.reset_mock()
    sheets.CurrentDrawingSheet = sheet
    rig.e._open_drawing_sheet(sheet)
    rig.uf.Draw.OpenDrawing.assert_not_called()


def test_leaving_drawing_restores_modeling_work_view(rig):
    """NX 2406 can keep the sheet as work view after ExitDraftingApplication."""
    sheets, _ = sheet_part(rig)
    state = {"drawing": True}

    class Views:
        @property
        def WorkView(self):
            if state["drawing"]:
                raise RuntimeError("The view is not a model view")
            return NS(Name="Trimetric")

    rig.part.ModelingViews = Views()
    rig.part.Drafting = NS(ExitDraftingApplication=Mock())
    rig.uf.Draw = NS(SetDisplayState=Mock(side_effect=lambda _: state.update(drawing=False)))
    rig.e._close_drawing_sheets(rig.part)
    rig.uf.Draw.SetDisplayState.assert_called_once_with(1)
    state["drawing"] = True
    rig.uf.Draw.SetDisplayState = Mock()
    with pytest.raises(NXToolError) as error:
        rig.e._close_drawing_sheets(rig.part)
    assert error.value.code == "NX_DRAWING_CONTEXT"


def png(width, height):
    return b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", width, height)


@pytest.mark.parametrize("current", [True, False])
def test_screenshot_exports_the_displayed_drawing_sheet(rig, current):
    sheets, sheet = sheet_part(rig)
    sheets.CurrentDrawingSheet = sheet if current else None

    class Views:
        @property
        def WorkView(self):
            raise RuntimeError("The view is not a model view")

    rig.part.ModelingViews = Views()
    work = NS(Name="Sheet1@0", Fit=Mock())
    builder = NS(Destroy=Mock(), SetCustomBackgroundColor=Mock())
    builder.Commit = lambda: Path(builder.FileName).write_bytes(png(320, 240))
    rig.part.Views = NS(WorkView=work, CreateImageExportBuilder=lambda: builder)
    result = rig.e._capture_view(width=320, height=240, fit=True)
    assert result["capture_kind"] == "nx_drawing_sheet" and result["camera"] is None
    assert result["resolution"] == [320, 240] and result["view_name"] == "Sheet1@0"
    assert (result["drawing"] is not None) is current
    assert any("style applies to model views" in w for w in result["warnings"])
    assert any("nx_activate_drawing_sheet" in w for w in result["warnings"]) is not current
    work.Fit.assert_called_once()
    builder.Destroy.assert_called_once()


def construction_rig(rig, monkeypatch, layer_objects):
    datum = Object("datum")
    datum.Layer = 61
    rig.part.Curves = []
    rig.e._datum_objects = lambda: [datum]
    rig.e._walk_components = lambda _: []
    rig.uf.View = NS(AskViewDependentStatus=lambda _: (0, ""), AskVdeData=lambda _: (0, [None]))
    hidden = set()

    def set_visibility(view, states, update):
        for layer, state in states:
            (hidden.add if state == "hidden" else hidden.discard)(layer)

    rig.part.Layers = NS(
        SetObjectsVisibilityOnLayer=Mock(side_effect=set_visibility),
        GetAllObjectsOnLayer=lambda n: layer_objects + [datum],
        GetVisibilitiesInView=lambda _: [
            NS(Layer=n, State="hidden" if n in hidden else "visible") for n in (1, 61)
        ],
    )
    layer_module = NS(StateInfo=lambda n, s: (n, s), State=NS(Hidden="hidden", Visible="visible"))
    monkeypatch.setitem(sys.modules, "NXOpen.Layer", layer_module)
    rig.nx.Layer = layer_module
    rig.nx.NXObject = NS(AttributeType=NS(String=5))
    attributes = {}
    view = NS(
        Tag=9,
        DependentDisplay=NS(Erase=Mock(), RemoveErasureOnObjectAndSubobjects=Mock()),
        SetAttribute=lambda k, v: attributes.__setitem__(k, v),
        HasUserAttribute=lambda k, *_: k in attributes,
        GetStringAttribute=lambda k: attributes[k],
        AskVisibleObjects=lambda: [] if datum.Layer in hidden else [datum],
    )
    return datum, view, attributes, hidden


def test_datums_on_their_own_layer_are_hidden_in_the_view_only(rig, monkeypatch):
    """NX 2406 ignores per-view erasure of datums; their layer is hidden in the view."""
    datum, view, attributes, hidden = construction_rig(rig, monkeypatch, [])
    assert rig.e._drawing_construction_visibility(view, False, strict=True) == 1
    assert hidden == {61}
    assert attributes["NX_MCP_DRAWING_CONSTRUCTION_V1"] == "hide"
    assert attributes["NX_MCP_DRAWING_CONSTRUCTION_LAYERS_V1"] == "61"
    rig.e._drawing_construction_visibility(view, True)
    assert hidden == set() and attributes["NX_MCP_DRAWING_CONSTRUCTION_V1"] == "show"
    assert attributes["NX_MCP_DRAWING_CONSTRUCTION_LAYERS_V1"] == ""


def test_datums_sharing_a_body_layer_stay_visible_and_are_reported(rig, monkeypatch):
    datum, view, attributes, hidden = construction_rig(rig, monkeypatch, [Body()])
    assert rig.e._drawing_construction_visibility(view, False) == 0
    assert hidden == set()
    assert attributes["NX_MCP_DRAWING_CONSTRUCTION_V1"] == "partial"
    with pytest.raises(NXToolError) as error:
        rig.e._drawing_construction_visibility(view, False, strict=True)
    assert error.value.code == "NX_UNAVAILABLE_ON_VERSION"
    assert error.value.details["layers"] == [61]


def projection_rig(rig):
    sheets, sheet = sheet_part(rig)
    sheet.Length, sheet.Height = 420.0, 297.0
    parent = Object("Top@1")
    parent.GetDrawingReferencePoint = lambda: NS(X=110.0, Y=190.0)
    result = Object("ORTHO@2")
    result.GetDrawingReferencePoint = lambda: NS(X=250.0, Y=190.0)
    rig.e._explosions = lambda _: []
    rig.e._drawing_object = lambda ref, kind: parent
    rig.e._view_sheet = lambda _: sheet
    rig.e._place_drawing_view = Mock()
    rig.e._sheet_units = lambda _: "mm"
    rig.e._sheet_point3d = lambda _s, p: p
    builder = NS(
        Parent=NS(View=NS()),
        Style=NS(ViewStyleProjected=NS(Scale=False)),
        Placement=NS(
            Method=NS(Horizontal=1, Vertical=2),
            Option=NS(ToView=1),
            AlignmentView=NS(),
            Placement=NS(SetValue=Mock()),
        ),
        Commit=Mock(return_value=result),
        Destroy=Mock(),
    )
    rig.part.DraftingViews = NS(CreateProjectedViewBuilder=lambda _: builder)
    return sheets, sheet, builder
