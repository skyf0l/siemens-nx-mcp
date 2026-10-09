"""Regressions for unloaded prototypes and drawing-owned construction curves."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from nx_mcp.runtime import NXToolError
from tests.fakes import Object


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
