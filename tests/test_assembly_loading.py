"""Regressions for unloaded prototypes and drawing-owned construction curves."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.assembly_loading import component_info, load_components, require_loaded
from nx_mcp.runtime import NXToolError
from tests.fakes import Component, Object, Part


def test_unloaded_component_inventory_retains_pose_and_path(rig):
    root = Component("root")
    child = Component("missing", parent=root)
    child.Prototype = Object("unloaded")
    root.children = [child]
    rig.part.ComponentAssembly.RootComponent = root
    rig.uf.Assem = NS(AskComponentData=lambda _: ("D:/model/child.prt", "", "", [], [], []))
    result = rig.e._list_components(compact=True)
    row = result["components"][0]
    assert row["part_path"] == "D:/model/child.prt"
    assert row["load_state"] == "unloaded"
    assert len(row["translation"]) == 3
    with pytest.raises(NXToolError) as error:
        require_loaded(rig.e, rig.part)
    assert error.value.details["mutation_outcome"] == "not_started"


def test_unknown_prototype_path_is_explicit(rig):
    rig.uf.Assem = NS(AskComponentData=Mock(side_effect=RuntimeError("Missing part")))
    info = component_info(NS(Prototype=None, Tag=1))
    assert info["part_path"] is None and info["load_state"] == "unloaded"
    assert info["load_diagnostic"] == "Missing part"


def test_close_referenced_prototype_rejected_before_save(rig, tmp_path):
    parent = rig.part
    child_part = Part(rig.session, tmp_path / "child.prt")
    root = Component("root")
    child = Component("child", parent=root)
    child.Prototype = child_part
    root.children = [child]
    parent.ComponentAssembly.RootComponent = root
    child_part.Save = Mock()
    with pytest.raises(NXToolError) as error:
        rig.e._close_part()
    assert error.value.code == "NX_PART_IN_USE"
    assert error.value.details["parent_assemblies"] == [parent.FullPath]
    child_part.Save.assert_not_called()
    assert child_part in rig.session.Parts


def test_component_loading_restores_options_on_failure(rig):
    c = NS(Tag=2, IsSuppressed=False)
    rig.e._walk_components = lambda _: [(c, ["missing"])]
    options = NS(UsePartialLoading=True)
    rig.session.Parts.LoadOptions = options
    status = NS(
        NumberUnloadedParts=1,
        GetPartName=lambda _: "missing.prt",
        GetStatus=lambda _: 123,
        Dispose=Mock(),
    )
    rig.part.ComponentAssembly.OpenOption = NS(ComponentOnly=1)
    rig.part.ComponentAssembly.OpenComponents = Mock(return_value=(status, []))
    with pytest.raises(NXToolError) as error:
        load_components(rig.e, rig.part)
    assert options.UsePartialLoading
    status.Dispose.assert_called_once()
    assert error.value.code == "NX_COMPONENT_LOAD_FAILED"
    assert error.value.details["mutation_outcome"] == "partial"


def test_construction_hiding_preserves_sheet_owned_section_curves(rig):
    model, section = Object("model"), Object("section line")
    rig.part.Curves = [model, section]
    rig.e._datum_objects = lambda: []
    rig.uf.View = NS(
        AskViewDependentStatus=lambda tag: (int(tag == section.Tag), "Sheet"),
        AskVdeData=lambda tag: (1, [NS(ViewTag=9)]),
    )
    view = NS(Tag=9, DependentDisplay=NS(Erase=Mock()), SetAttribute=Mock())
    assert rig.e._drawing_construction_visibility(view, False) == 1
    view.DependentDisplay.Erase.assert_called_once_with([model])


def test_invisible_drawing_font_is_discoverable():
    from nx_mcp.documentation_editing_server import ViewStyle

    style = ViewStyle(hidden_lines=True, self_hidden=True, hidden_font=0)
    assert style.hidden_font == 0
    with pytest.raises(ValueError):
        ViewStyle(hidden_font=-1)


def test_construction_hiding_skips_view_status_for_prototype_datums(rig):
    """NX 2406 raises error 1 from AskViewDependentStatus on datums and CSYS."""
    from tests.fakes import Curve

    rig.e.nxopen.Curve = Curve
    line, datum, csys = Curve("line"), Object("datum"), Object("csys")
    prototype = NS(Curves=[line], Datums=[datum], CoordinateSystems=[csys])
    component = NS(
        Tag=7, IsSuppressed=False, Prototype=prototype, FindOccurrence=lambda v: Object(v.Name)
    )
    rig.part.Curves = []
    rig.e._datum_objects = lambda: []
    rig.e._walk_components = lambda _: [(component, ["c"])]
    queried = []

    def status(tag):
        queried.append(tag)
        if tag != line.Tag:
            raise RuntimeError("Error number: 1")
        return (0, "")

    rig.uf.View = NS(AskViewDependentStatus=status, AskVdeData=lambda tag: (1, [NS(ViewTag=9)]))
    view = NS(Tag=9, DependentDisplay=NS(Erase=Mock()), SetAttribute=Mock())
    assert rig.e._drawing_construction_visibility(view, False) == 3
    assert queried == [line.Tag]


def test_datums_nx_does_not_erase_are_reported_or_refused(rig):
    """NX 2406 records no view-dependent edit for erased datums: they stay visible."""
    curve, datum = Object("curve"), Object("datum")
    rig.part.Curves = [curve]
    rig.e._datum_objects = lambda: [datum]
    rig.e._walk_components = lambda _: []
    edits = {curve.Tag: (1, [NS(ViewTag=9)]), datum.Tag: (0, [None])}
    rig.uf.View = NS(AskViewDependentStatus=lambda _: (0, ""), AskVdeData=lambda t: edits[t])
    view = NS(Tag=9, DependentDisplay=NS(Erase=Mock()), SetAttribute=Mock())
    assert rig.e._drawing_construction_visibility(view, False) == 1
    view.SetAttribute.assert_called_with("NX_MCP_DRAWING_CONSTRUCTION_V1", "partial")
    with pytest.raises(NXToolError) as error:
        rig.e._drawing_construction_visibility(view, False, strict=True)
    assert error.value.code == "NX_UNAVAILABLE_ON_VERSION"
    assert error.value.details["not_erased"] == 1
