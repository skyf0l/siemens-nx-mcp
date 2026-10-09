"""nx_export_flat_pattern and nx_activate_application on the shared fake."""

from pathlib import Path

import pytest

from tests.kit import run
from tests.kit.kernel import services
from tests.unit.sheet_metal.fakes import sm_nx

DXF = (
    "  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1032\n  0\nENDSEC\n"
    "  0\nSECTION\n  2\nENTITIES\n"
    "  0\nLINE\n  8\nOUTLINE\n 10\n0.0\n 20\n0.0\n 11\n100.0\n 21\n0.0\n"
    "  0\nLINE\n  8\nOUTLINE\n 10\n100.0\n 20\n0.0\n 11\n100.0\n 21\n97.749115\n"
    "  0\nLINE\n  8\nBEND_UP\n 10\n0.0\n 20\n77.874558\n 11\n100.0\n 21\n77.874558\n"
    "  0\nENDSEC\n  0\nEOF\n"
)
GEO = (
    "#~1\n1.03\n0\n9.10.2026\n0.0000 -0.0000 0.0\n100.0000 97.7491 0.0\n9774.9115\n1\n##~~\n"
    "#~11\n\n\n2.0000\n\n##~~\n#~31\nP\n1\n0.0000 0.0000 0.0\n|~\nP\n2\n100.0000 97.7491 0.0\n"
    "|~\n##~~\n#~33\nLIN\n1 0\n1 2\n|~\n##~~\n#~END\n"
)


def flat(nx):
    body = nx.tab()
    feature = nx.made("FLAT_PATTERN", body, feature_type="FLAT_PATTERN")
    feature.ref = nx.ref_of(feature, "feature")
    return feature


def writes(text):
    def hook(b):
        Path(b.OutputFile).write_text(text, encoding="latin-1")

    return hook


def test_dxf_export_reads_back_entities_and_developed_extents():
    nx = sm_nx()
    root = services(nx).root
    feature = flat(nx)
    nx.on_commit("ExportFlatPatternBuilder", writes(DXF))
    ok = run(nx, "nx_export_flat_pattern", flat_pattern=feature.ref, path="out/bracket.dxf",
             dxf={"revision": "r12", "mold": "both"}, include=["bend_up"])  # fmt: skip
    assert ok.ok, ok
    data = ok.data
    assert data["path"] == "out/bracket.dxf" and (root / "out" / "bracket.dxf").is_file()
    assert data["entities"] == {"LINE": 3} and data["extents"] == [100.0, 97.749115]
    assert data["revision"] == "r12" and data["include"] == ["bend_up"]
    b = nx.last("ExportFlatPatternBuilder")
    assert b.FlatPattern.Value is feature and b.Type == "ExportFlatPatternBuilder.FileType.Dxf"
    assert (
        b.DxfRevision == "ExportFlatPatternBuilder.DxfRevisionType.R12"
        and b.InnerMold
        and b.OuterMold
    )
    assert b.BendUp is True and b.BendDown is False and b.InteriorCutout is False
    assert b.DeviationalTolerance == pytest.approx(0.01) and nx.live_builders == 0


def test_geo_export_reports_area_and_thickness():
    nx = sm_nx()
    feature = flat(nx)
    nx.on_commit("ExportFlatPatternBuilder", writes(GEO))
    ok = run(nx, "nx_export_flat_pattern", flat_pattern=feature.ref, path="b.geo", format="geo")
    assert ok.ok, ok
    assert ok.data["extents"] == [100.0, 97.7491] and ok.data["area"] == 9774.9115
    assert ok.data["thickness"] == 2.0 and ok.data["revision"] is None
    assert nx.last("ExportFlatPatternBuilder").Type == "ExportFlatPatternBuilder.FileType.TrumpfGeo"


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"path": "b.geo"}, "/path"),
        ({"path": "b.geo", "format": "geo", "dxf": {"revision": "r12"}}, "/dxf"),
        ({"path": "b.dxf", "include": ["bend_up", "bend_up"]}, "/include/1"),
        ({"path": "b.dxf", "tolerance": 0}, "/tolerance"),
    ],
)
def test_bad_export_arguments_are_refused_before_nx(args, field):
    nx = sm_nx()
    feature = flat(nx)
    bad = run(nx, "nx_export_flat_pattern", flat_pattern=feature.ref, **args)
    assert bad.error["category"] == "invalid_argument" and bad.error["field"] == field
    assert bad.mutation_outcome == "not_started" and nx.builders == []


def test_export_needs_a_flat_pattern_and_never_overwrites():
    nx = sm_nx()
    root = services(nx).root
    tab = nx.made("SB_TAB", nx.tab())
    bad = run(nx, "nx_export_flat_pattern", flat_pattern=nx.ref_of(tab, "feature"), path="a.dxf")
    assert bad.error["code"] == "NX_WRONG_KIND" and bad.mutation_outcome == "not_started"
    (root / "taken.dxf").write_text("mine")
    feature = flat(nx)
    bad = run(nx, "nx_export_flat_pattern", flat_pattern=feature.ref, path="taken.dxf")
    assert bad.error["code"] == "NX_ALREADY_EXISTS"
    assert (root / "taken.dxf").read_text() == "mine"


@pytest.mark.parametrize("content", ["", "not a dxf at all", "  0\nSECTION\n  0\nEOF\n"])
def test_an_empty_or_foreign_output_fails_and_publishes_nothing(content):
    nx = sm_nx()
    root = services(nx).root
    feature = flat(nx)
    nx.on_commit("ExportFlatPatternBuilder", writes(content))
    bad = run(nx, "nx_export_flat_pattern", flat_pattern=feature.ref, path="x.dxf")
    assert bad.error["code"] == "NX_EXPORT_FAILED", bad
    assert not (root / "x.dxf").exists() and bad.mutation_outcome == "rolled_back"


class App:
    """``Session.ApplicationName`` and ``ApplicationSwitchImmediate`` of the fake."""

    def __init__(self, nx, name="UG_APP_MODELING", sticks=False):
        self.nx, self.sticks = nx, sticks
        nx.session.ApplicationName = name
        nx.session.ApplicationSwitchImmediate = self.switch
        self.calls = []

    def switch(self, name):
        self.calls.append(name)
        if not self.sticks:
            self.nx.session.ApplicationName = name


def test_activate_application_switches_and_verifies():
    nx = sm_nx(application="modeling")
    app = App(nx)
    ok = run(nx, "nx_activate_application", application="sheet_metal")
    assert ok.ok, ok
    assert ok.data == {"application": "sheet_metal", "previous": "modeling", "changed": True}
    assert app.calls == ["UG_APP_SBSM"]
    again = run(nx, "nx_activate_application", application="sheet_metal")
    assert again.data["changed"] is False and app.calls == ["UG_APP_SBSM"]


def test_activate_application_closes_a_shown_drawing_first():
    nx = sm_nx(application="modeling")
    App(nx, "UG_APP_DRAFTING")
    nx.sheet("SHT1").Open()
    ok = run(nx, "nx_activate_application", application="modeling")
    assert ok.ok, ok
    assert nx.part.DrawingSheets.CurrentDrawingSheet is None and ok.data["previous"] == "drafting"


def test_activate_application_refusals():
    nx = sm_nx(application="modeling")
    App(nx, sticks=True)
    bad = run(nx, "nx_activate_application", application="sheet_metal")
    assert bad.error["code"] == "NX_WRONG_APPLICATION"
    other = sm_nx(application="modeling")
    App(other)
    other.session.Parts.Display = None
    bad = run(other, "nx_activate_application", application="sheet_metal")
    assert bad.error["code"] == "NX_WRONG_PART_CONTEXT" and bad.mutation_outcome == "not_started"
    assert "nx_activate_part" in bad.error["hint"]


@pytest.mark.parametrize("what", ["body", "feature"])
def test_a_sheet_metal_part_never_switches_back_to_modeling(what):
    """NX 2406 opens a blocking modal warning when a part with sheet-metal features enters
    Modeling: the switch is refused before ApplicationSwitchImmediate is called."""
    nx = sm_nx()
    app = App(nx, "UG_APP_SBSM")
    if what == "body":
        nx.tab()
    else:
        nx.made("SB_FLANGE", None, feature_type="SB_FLANGE")
    bad = run(nx, "nx_activate_application", application="modeling")
    assert bad.error["code"] == "NX_UNSUPPORTED_OPTION" and bad.error["field"] == "/application"
    assert bad.mutation_outcome == "not_started" and app.calls == []
    assert nx.session.ApplicationName == "UG_APP_SBSM"
    ok = run(nx, "nx_activate_application", application="sheet_metal")
    assert ok.ok and ok.data["changed"] is False


def test_sheet_metal_features_refuse_before_any_builder_outside_sheet_metal():
    nx = sm_nx(application="modeling")
    nx.session.ApplicationName = "UG_APP_MODELING"
    body = nx.tab()
    bad = run(nx, "nx_create_sheet_metal_feature", operation="flange",
              parameters={"edges": [body.edges[0].ref], "length": 20})  # fmt: skip
    assert bad.error["code"] == "NX_WRONG_APPLICATION" and bad.mutation_outcome == "not_started"
    assert nx.builders == [] and nx.session.ApplicationName == "UG_APP_MODELING"
