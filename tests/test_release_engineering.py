"""Preflight, ownership and verification failures for native release engineering."""

from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

from nx_mcp.runtime import NXToolError
from tests.test_freeform_manufacturing import ff as _fixture


@pytest.fixture
def ff(rig, monkeypatch):
    value = _fixture.__wrapped__(rig, monkeypatch)
    value.part.Annotations = MagicMock()
    value.part.DraftingViews = MagicMock()
    value.part.Dimensions = []
    return value


@pytest.mark.parametrize(
    "anchor",
    [{}, {"version": 2}, {"version": 1, "owner_part": "part", "kind": "component", "handle": "h"}],
)
def test_anchor_schema_rejects_foreign_kinds(ff, anchor):
    with pytest.raises(NXToolError):
        ff.e._resolve_geometry_anchor(anchor)
    ff.uf.Tag.AskTagOfHandle.assert_not_called()


def test_anchor_owner_and_missing_entity_fail_closed(ff):
    part = ff.e._work_part()
    anchor = {"version": 1, "owner_part": part.FullPath, "kind": "body", "handle": "native"}
    with pytest.raises(NXToolError, match="another part"):
        ff.e._resolve_geometry_anchor({**anchor, "owner_part": "wrong"})
    ff.uf.Tag.AskTagOfHandle.return_value = 912345
    with pytest.raises(NXToolError, match="no longer exists"):
        ff.e._resolve_geometry_anchor(anchor)
    body = list(part.Bodies)[0]
    ff.uf.Tag.AskTagOfHandle.return_value = body.Tag
    assert ff.e._resolve_geometry_anchor(anchor)["object"]["kind"] == "body"


@pytest.fixture
def drawing(ff, monkeypatch):
    import sys

    from tests.fakes import Object, point

    d = NS(
        DrawingSheet=NS(Unit=NS(Millimeters=2, Inches=1)),
        DetailViewBuilder=NS(Types=NS(Circular=1)),
        ViewScaleBuilder=NS(Type=NS(Ratio=1)),
    )
    monkeypatch.setitem(sys.modules, "NXOpen.Drawings", d)
    ff.nx.Drawings = d
    ff.nx.NXObject = NS(AttributeType=NS(String=1))
    ff.uf.Tag.AskHandleFromTag.side_effect = lambda tag: str(tag)
    sheet = Object("Sheet")
    sheet.Length, sheet.Height, sheet.Units = 297, 210, 2
    sheet.Open = MagicMock()
    view = Object("Top")
    view.OwningPart = ff.part
    view.GetDrawingReferencePoint = lambda: point(100, 90, 0)
    view.Matrix = NS(Xx=1, Xy=0, Xz=0)
    view.HasUserAttribute = MagicMock(return_value=False)
    view.SetAttribute = MagicMock()
    view.UpdateAutomaticViewBound = MagicMock()
    import contextlib

    ff.e._drawing_save_context = lambda *a, **k: contextlib.nullcontext()
    sheet.GetDraftingViews = lambda: [view]
    ff.part.DrawingSheets = [sheet]
    ff.e._drawing_object = lambda ref, kind: sheet if kind == "drawing_sheet" else view
    ff.uf.Draw.AskViewBorders.return_value = [60, 70, 140, 110]
    ff.uf.Draw.AskViewScale.return_value = (0, 1.0)
    ff.uf.View.MapModelToDrawing.side_effect = lambda _, p: [p[0] + 100, p[1] + 90]
    ff.part.SettingsManager = MagicMock()
    style = ff.part.SettingsManager.CreateDrawingEditViewSettingsBuilder.return_value.ViewStyle
    from nx_mcp.drawing_preferences import STYLE_PROPERTIES

    for key, (group, prop) in STYLE_PROPERTIES.items():
        setattr(getattr(style, group), prop, 1 if key.endswith(("font", "width")) else True)
    ff.sheet, ff.view = sheet, view
    return ff


def test_sheet_units_and_physical_point_conversion(drawing):
    f = drawing
    assert f.e._drawing_view_info("v")["inside_sheet"]
    f.sheet.Units = 1
    assert f.e._sheet_units(f.sheet) == "in"
    result = f.e._sheet_point3d(f.sheet, [1, 2])
    assert (result.X, result.Y) == (25.4, 50.8)
    f.part.PartUnits = "inch"
    f.sheet.Units = 2
    assert pytest.approx(1) == f.e._sheet_point3d(f.sheet, [25.4, 50.8]).X
    f.sheet.Units = 912
    with pytest.raises(NXToolError, match="Unknown native"):
        f.e._sheet_units(f.sheet)


def test_detail_refresh_requires_surviving_parent(drawing):
    import json

    f = drawing
    f.view.HasUserAttribute.return_value = True
    f.view.GetStringAttribute = lambda _: json.dumps(
        {"parent": "handle", "center": [0, 0, 0], "radius": 5}
    )
    f.uf.Tag.AskTagOfHandle.return_value = f.view.Tag
    f.e._refresh_detail_boundaries()
    f.part.DraftingViews.CreateDetailViewBuilder.return_value.Commit.assert_called_once()
    f.uf.Tag.AskTagOfHandle.return_value = -100
    with pytest.raises(NXToolError, match="parent no longer"):
        f.e._refresh_detail_boundaries()


def test_assembly_refresh_unsatisfied_constraints_releases_network(drawing, monkeypatch):
    import sys

    f = drawing
    monkeypatch.setitem(
        sys.modules, "NXOpen.Positioning", NS(Constraint=NS(SolverStatus=NS(Solved=1)))
    )
    f.e._explosion_context = lambda: f.part
    constraint = NS(GetConstraintStatus=lambda: 2)
    f.e._assembly_constraints = lambda _: [constraint]
    positioner = MagicMock()
    f.part.ComponentAssembly.Positioner = positioner
    with pytest.raises(NXToolError, match="did not solve"):
        f.e._update_assembly_documentation()
    positioner.ClearNetwork.assert_called_once()
    positioner.EndAssemblyConstraints.assert_called_once()
    f.part.Annotations.PartsLists = []
    f.e._assembly_constraints = lambda _: []
    f.e._explosions = lambda _: []
    f.e._refresh_annotations = lambda: {"updated_count": 0}
    result = f.e._update_assembly_documentation()
    assert result["health"]["healthy"] and len(result["views"]) == 1


def test_native_mass_unit_is_explicit(ff):
    props = NS(Volume=8193.532, Dispose=MagicMock())
    ff.part.MeasureManager.NewMassProperties = lambda *a: props
    result = ff.e._measure_volume()
    assert props.InformationUnit == ff.nx.MeasureBodies.AnalysisUnit.KilogramMillimeter
    assert result["volume_mm3"] == 8193.532
    props.Dispose.assert_called_once()


def test_retained_dimensions_are_not_valid_measurements(drawing):
    from tests.fakes import Object, point

    d = Object("Retained dimension")
    d.ComputedSize, d.IsRetained, d.AnnotationOrigin = 8, True, point(0, 0, 0)
    drawing.part.Dimensions = [d]
    result = drawing.e._list_dimensions()["dimensions"][0]
    assert result["retained"] and not result["measurement_valid"]
    d.IsRetained = False
    assert drawing.e._list_dimensions()["dimensions"][0]["measurement_valid"]
