"""nx_create_drawing_sheet and nx_activate_drawing_sheet through the real kernel."""

import pytest

from tests.kit import run
from tests.unit.drawing.fakes import drawing_nx, make_sheet


def sheet_nx(units="mm"):
    nx = drawing_nx(units)

    def commit(b):
        units_ = "in" if b.Units is b.SheetUnits.English else "mm"
        template = b.Option is b.SheetOption.UseTemplate
        size = (420.0, 297.0) if template else (b.Length, b.Height)
        name = "TEMPLATE_SHEET" if template else b.Name  # templates keep their own name
        sheet = make_sheet(nx, name, units=units_, size=size, scale=3.0 if template else
                           b.ScaleNumerator, angle="third")  # fmt: skip
        nx.committed = b
        return sheet

    nx.on_commit("DraftingDrawingSheetBuilder", commit)
    return nx


def test_standard_sizes_follow_the_sheet_units_and_default_names_are_unique():
    nx = sheet_nx()
    ok = run(
        nx, "nx_create_drawing_sheet", size="a4", units="in", scale=0.5, projection="first_angle"
    )
    assert ok.ok, ok
    assert ok.data["sheet"]["name"] == "SHT1" and ok.data["size"] == "a4"
    assert ok.data["dimensions"] == pytest.approx([297 / 25.4, 210 / 25.4])
    assert ok.data["units"] == "in" and ok.data["projection"] == "first_angle"
    assert ok.data["scale"] == 0.5 and ok.mutation_outcome == "committed"
    assert nx.part.DrawingSheets.CurrentDrawingSheet.Name == "SHT1"  # opened
    assert nx.live_builders == 0
    second = run(nx, "nx_create_drawing_sheet", size=[500, 250])
    assert second.data["sheet"]["name"] == "SHT2" and second.data["size"] is None
    assert second.data["dimensions"] == [500.0, 250.0]


def test_refusals_happen_before_nx():
    nx = sheet_nx()
    nx.make_sheet("SHT1")
    for args, field, category in [
        ({"name": "sht1"}, "/name", "already_exists"),
        ({"template": "A3", "units": "in"}, "/units", "invalid_argument"),
        ({"template": "A3", "size": [100, 100]}, "/size", "invalid_argument"),
        ({"template": "A3", "size": "a4"}, "/size", "invalid_argument"),
        ({"template": "A3", "size": "a3"}, "/size", "invalid_argument"),
        ({"size": [0, 100]}, "/size", "invalid_argument"),
        ({"template_path": "missing.prt"}, "/template_path", "not_found"),
        ({"template_path": "../outside.prt"}, "/template_path", "path_rejected"),
    ]:
        bad = run(nx, "nx_create_drawing_sheet", **args)
        assert bad.error["category"] == category and bad.error["field"] == field, (args, bad)
        assert bad.mutation_outcome == "not_started"
    assert not nx.builders


def test_installed_templates_resolve_under_ugii_base_dir(tmp_path, monkeypatch):
    nx = sheet_nx()
    monkeypatch.setenv("UGII_BASE_DIR", str(tmp_path))
    missing = run(nx, "nx_create_drawing_sheet", template="A3")
    assert missing.error["category"] == "not_found" and missing.error["field"] == "/template"
    folder = tmp_path / "DRAFTING" / "templates"
    folder.mkdir(parents=True)
    (folder / "Drawing-A3-Size2D-template.prt").write_bytes(b"prt")
    ok = run(nx, "nx_create_drawing_sheet", name="T", template="a3", scale=2,
             projection="first_angle")  # fmt: skip
    assert ok.ok, ok
    assert ok.data["sheet"]["name"] == "T" and ok.data["scale"] == 2  # renamed, rescaled
    assert ok.data["projection"] == "first_angle" and len(ok.warnings) == 2
    assert nx.committed.MetricSheetTemplateLocation.endswith("Drawing-A3-Size2D-template.prt")
    assert ok.data["template"].endswith("Drawing-A3-Size2D-template.prt")


def test_a_sheet_that_differs_from_the_request_is_rolled_back():
    nx = sheet_nx()
    nx.on_commit(
        "DraftingDrawingSheetBuilder",
        lambda b: make_sheet(nx, b.Name, units="mm", size=(400, 300)),
    )
    bad = run(nx, "nx_create_drawing_sheet", size="a3")
    assert bad.error["category"] == "verification_failed"
    assert bad.mutation_outcome == "rolled_back" and nx.live_builders == 0


def test_template_units_mismatch_is_unsupported(tmp_path, monkeypatch):
    from tests.kit import NXException

    nx = sheet_nx()
    monkeypatch.setenv("UGII_BASE_DIR", str(tmp_path))
    folder = tmp_path / "DRAFTING" / "templates"
    folder.mkdir(parents=True)
    (folder / "Drawing-B-Size2D-template.prt").write_bytes(b"prt")
    nx.errors["DraftingDrawingSheetBuilder"] = NXException(
        3520000, "Cannot transfer objects whose units are different from the displayed part"
    )
    bad = run(nx, "nx_create_drawing_sheet", template="Drawing-B-Size2D-template", units="in")
    assert bad.error["code"] == "NX_UNSUPPORTED_OPTION" and bad.error["field"] == "/template"
    assert nx.live_builders == 0


def test_activate_opens_a_sheet_and_returns_to_modeling():
    nx = drawing_nx()
    sheet = nx.make_sheet()
    nx.make_view(sheet)
    ok = run(nx, "nx_activate_drawing_sheet", sheet=sheet.ref)
    assert ok.data["sheet"]["name"] == "SHT1" and not ok.data["modeling"]
    assert ok.data["sheets"][0]["active"] and len(ok.data["sheets"][0]["views"]) == 1
    back = run(nx, "nx_activate_drawing_sheet")
    assert back.data["sheet"] is None and back.data["modeling"]
    nx.session.Parts.Display = None
    refused = run(nx, "nx_activate_drawing_sheet", sheet=sheet.ref)
    assert refused.error["code"] == "NX_WRONG_PART_CONTEXT"
    assert refused.mutation_outcome == "not_started"


def test_natural_size_scale_and_projection_spellings_are_accepted():
    nx = sheet_nx()
    ok = run(nx, "nx_create_drawing_sheet", name="Sheet1", size="A3", scale="1:2",
             projection="First Angle")  # fmt: skip
    assert ok.ok, ok
    assert ok.data["size"] == "a3" and ok.data["scale"] == 0.5
    assert (nx.committed.ScaleNumerator, nx.committed.ScaleDenominator) == (1.0, 2.0)  # 1:2
    assert ok.data["projection"] == "first_angle"
    iso = run(nx, "nx_create_drawing_sheet", size="ISO A4 landscape", scale="2/1", projection="3rd")
    assert iso.data["size"] == "a4" and iso.data["scale"] == 2.0
    assert iso.data["projection"] == "third_angle"
    for args, field in [
        ({"size": "letter"}, "/size"),
        ({"scale": "1:0"}, "/scale"),
        ({"scale": "half"}, "/scale"),
        ({"projection": "second"}, "/projection"),
    ]:
        bad = run(nx, "nx_create_drawing_sheet", **args)
        assert bad.error["field"] == field and bad.mutation_outcome == "not_started", (args, bad)
    hint = run(nx, "nx_create_drawing_sheet", projection="frist").error
    assert "did you mean 'first_angle'" in hint["message"] and "third_angle" in hint["message"]


def test_activate_takes_a_sheet_name_and_lists_the_sheets_of_an_unknown_one():
    nx = drawing_nx()
    nx.make_sheet()
    two = nx.make_sheet("Detail")
    ok = run(nx, "nx_activate_drawing_sheet", sheet="detail")
    assert ok.ok and ok.data["sheet"]["id"] == two.ref
    bad = run(nx, "nx_activate_drawing_sheet", sheet="Sheet1")
    assert bad.error["category"] == "not_found" and bad.error["field"] == "/sheet"
    assert [s["name"] for s in bad.error["details"]["sheets"]] == ["SHT1", "Detail"]


def test_activate_says_so_when_nx_flags_the_part_modified_on_a_read_only_visit():
    nx = drawing_nx()
    sheet = nx.make_sheet()
    nx.part.IsModified = False
    clean = run(nx, "nx_activate_drawing_sheet", sheet=sheet.ref)
    assert clean.ok and not [w for w in clean.warnings if "marked the part" in w["message"]]
    plain = type(sheet).Open

    def flagging(self):
        plain(self)
        nx.part.IsModified = True

    type(sheet).Open = flagging
    try:
        assert run(nx, "nx_activate_drawing_sheet").ok
        nx.part.IsModified = False
        flagged = run(nx, "nx_activate_drawing_sheet", sheet=sheet.ref)
    finally:
        type(sheet).Open = plain
    assert flagged.ok, flagged
    note = next(w["message"] for w in flagged.warnings if "marked the part" in w["message"])
    assert "unchanged" in note and "nx_capture_view with sheet" in note
