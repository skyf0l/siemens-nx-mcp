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


def test_format_follows_the_path_suffix_when_omitted():
    nx = sm_nx()
    feature = flat(nx)
    nx.on_commit("ExportFlatPatternBuilder", writes(GEO))
    ok = run(nx, "nx_export_flat_pattern", flat_pattern=feature.ref, path="B.GEO")
    assert ok.ok, ok
    assert ok.data["format"] == "geo" and ok.data["revision"] is None
    assert nx.last("ExportFlatPatternBuilder").Type == "ExportFlatPatternBuilder.FileType.TrumpfGeo"
    nx.on_commit("ExportFlatPatternBuilder", writes(DXF))
    ok = run(nx, "nx_export_flat_pattern", flat_pattern=feature.ref, path="b.dxf")
    assert ok.ok, ok
    assert ok.data["format"] == "dxf" and ok.data["revision"] == "r2018"


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"path": "b.geo", "format": "dxf"}, "/path"),
        ({"path": "b.geo", "dxf": {"mold": "inner"}}, "/dxf"),  # .geo: format geo
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
    # Entered again although NX runs it: NX then remembers it for the displayed part.
    again = run(nx, "nx_activate_application", application="sheet_metal")
    assert again.data["changed"] is False and app.calls == ["UG_APP_SBSM", "UG_APP_SBSM"]


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


def test_an_unlicensed_application_is_refused_before_nx_switches():
    """Entering an application the seat cannot license opens NX's blocking "License Not
    Available" dialog: the license is asked first (a query, no checkout)."""
    nx = sm_nx(application="modeling")
    app = App(nx)
    asked = []
    nx.session.LicenseManager = type(
        "LM", (), {"CheckPresence": staticmethod(lambda f: asked.append(f) or False)}
    )()
    bad = run(nx, "nx_activate_application", application="sheet_metal")
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE", bad
    assert bad.mutation_outcome == "not_started" and app.calls == []
    assert asked == ["nx_sheet_metal"] and bad.error["details"]["license"] == "nx_sheet_metal"
    # Already in Modeling: nothing to enter, so nothing to license; the refusal follows
    # the argument, so the tool itself is not refused for the rest of the session.
    stay = run(nx, "nx_activate_application", application="modeling")
    assert stay.ok and stay.data["changed"] is False and asked == ["nx_sheet_metal"]


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
    assert ok.ok and ok.data["changed"] is False and app.calls == ["UG_APP_SBSM"]


def test_sheet_metal_features_refuse_before_any_builder_outside_sheet_metal():
    nx = sm_nx(application="modeling")
    nx.session.ApplicationName = "UG_APP_MODELING"
    body = nx.tab()
    bad = run(nx, "nx_create_sheet_metal_feature", operation="flange",
              parameters={"edges": [body.edges[0].ref], "length": 20})  # fmt: skip
    assert bad.error["code"] == "NX_WRONG_APPLICATION" and bad.mutation_outcome == "not_started"
    assert nx.builders == [] and nx.session.ApplicationName == "UG_APP_MODELING"


def test_background_sheet_metal_calls_make_their_part_current_in_nx():
    """Beside a displayed engineer part (crash sweep): NX 2406's flat-pattern export made
    the display part the work part (the call was undone), and the sheet-metal manager
    answered a bound read in the work part's units (2.032 mm for a 0.08 in sheet)."""
    from nx_mcp.core import catalogue

    specs = catalogue.load()
    assert specs["nx_export_flat_pattern"].switch_for({}) == "display"
    assert specs["nx_get_sheet_metal"].switch_for({}) == "work"


def test_an_existing_flat_pattern_file_is_replaced_only_with_overwrite():
    nx = sm_nx()
    root = services(nx).root
    feature = flat(nx)
    nx.on_commit("ExportFlatPatternBuilder", writes(DXF))
    (root / "old.dxf").write_text("old")
    bad = run(nx, "nx_export_flat_pattern", flat_pattern=feature.ref, path="old.dxf")
    assert bad.error["code"] == "NX_ALREADY_EXISTS" and bad.error["field"] == "/path"
    assert bad.error["details"]["path"] == "old.dxf" and bad.mutation_outcome == "not_started"
    assert nx.builders == []
    ok = run(nx, "nx_export_flat_pattern", flat_pattern=feature.ref, path="old.dxf",
             overwrite=True)  # fmt: skip
    assert ok.ok and ok.data["overwritten"] is True, ok
    assert (root / "old.dxf").read_text(encoding="latin-1").startswith("  0\nSECTION")
    new = run(nx, "nx_export_flat_pattern", flat_pattern=feature.ref, path="new.dxf")
    assert new.ok and new.data["overwritten"] is False
