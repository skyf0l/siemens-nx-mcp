"""nx_get_bend_list and nx_export_bend_list on the shared fake, through the real kernel."""

import math

import pytest

from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import NXException
from tests.unit.sheet_metal.fakes import FLAT, sm_nx

BA_90 = math.pi / 2 * (3.0 + 0.33 * 2.0)  # 5.749114556...
BD_90 = 2 * (3.0 + 2.0) - BA_90


def bracket(nx):
    """A 2 mm tab with two 90 degree bends of radius 3: 100 long (top) and 80 long (bottom)."""
    body = nx.tab(100, 80, 2.0)
    first = nx.bend_edges(nx.add_bend(body), 100.0, 4.71, layer="Top")
    second = nx.bend_edges(nx.add_bend(body), 80.0, 4.71, layer="Bottom")
    return body, first, second


def test_bend_list_reads_angle_radius_length_allowance_and_top_layer_direction():
    nx = sm_nx()
    body, first, second = bracket(nx)
    ok = run(nx, "nx_get_bend_list")
    assert ok.ok, ok
    data = ok.data
    assert data["body"]["id"] == body.ref and data["thickness"] == 2.0
    assert data["flat_pattern"] is None and data["direction_from"] == "top_layer"
    assert data["sequence"] == []
    one, two = data["items"]
    assert one["index"] == 1 and one["face"]["id"] == first.ref and one["state"] == "bent"
    assert one["angle"] == 90 and one["radius"] == 3 and one["neutral_factor"] == 0.33
    assert one["length"] == pytest.approx(100.0) and two["length"] == pytest.approx(80.0)
    assert one["allowance"] == pytest.approx(BA_90) and one["deduction"] == pytest.approx(BD_90)
    assert (one["direction"], two["direction"]) == ("up", "down")
    assert ok.mutation_outcome == "not_applicable" and nx.live_builders == 0


def test_flat_bends_measure_the_long_strip_sides_not_the_thickness_ends():
    nx = sm_nx()
    body = nx.tab()
    nx.bend_edges(nx.add_bend(body, state=FLAT), 60.0, BA_90, flat=True)
    ok = run(nx, "nx_get_bend_list", body=body.ref)
    item = ok.data["items"][0]
    assert item["state"] == "flat" and item["length"] == pytest.approx(60.0)


def test_a_flat_pattern_gives_direction_and_the_bend_sequence():
    nx = sm_nx()
    body, first, second = bracket(nx)
    # NX names the inner face of an up bend and the outer face of a down bend
    feature = nx.flat_pattern(body, up=[second], down=[first.opposite],
                              sequence=[(7, "FIRST"), (3, "SECOND")])  # fmt: skip
    ok = run(nx, "nx_get_bend_list")
    assert ok.ok, ok
    data = ok.data
    assert data["flat_pattern"]["id"] == feature.ref and data["direction_from"] == "flat_pattern"
    assert [i["direction"] for i in data["items"]] == ["down", "up"]
    assert data["sequence"] == [{"id": 3, "name": "SECOND"}, {"id": 7, "name": "FIRST"}]
    assert nx.last("FlatPatternBuilder").args == (feature,)
    b = nx.last("BendListBuilder")
    assert b.PopulateBendListFromView.calls[0][0][0].Name == "FLAT-PATTERN#1"
    assert b.commits == 0 and nx.live_builders == 0


def _refuse(*_):
    raise NXException(948801, "License Error, its token feature does not exist.")


def test_a_default_flat_pattern_nx_refuses_falls_back_to_the_layers_with_a_note():
    nx = sm_nx()
    body, first, second = bracket(nx)
    feature = nx.flat_pattern(body, up=[second], down=[first.opposite], sequence=[(7, "A")])
    feature.GetBendUpCenterLines = _refuse  # needs nx_sheet_metal: a Flexible PCB seat
    ok = run(nx, "nx_get_bend_list")
    assert ok.ok, ok
    assert ok.data["flat_pattern"] is None and ok.data["direction_from"] == "top_layer"
    assert [i["direction"] for i in ok.data["items"]] == ["up", "down"]
    assert ok.data["sequence"] == []
    note = ok.warnings[0]
    assert note["code"] == "NX_NOTE" and "nx_sheet_metal" in note["message"]


def test_a_named_flat_pattern_nx_refuses_reports_the_license():
    nx = sm_nx()
    body, first, second = bracket(nx)
    feature = nx.flat_pattern(body, up=[second], down=[first.opposite])
    feature.GetBendUpCenterLines = _refuse
    bad = run(nx, "nx_get_bend_list", flat_pattern=feature.ref)
    assert bad.error["code"] == "NX_LICENSE_UNAVAILABLE"
    assert bad.error["details"]["license"] == "nx_sheet_metal"


def test_the_bend_sequence_alone_may_be_refused():
    nx = sm_nx()
    body, first, second = bracket(nx)
    nx.flat_pattern(body, up=[second], down=[first.opposite], sequence=[(7, "A")])
    nx.part.Features.SheetmetalManager.CreateBendListBuilder = _refuse
    ok = run(nx, "nx_get_bend_list")
    assert ok.ok and ok.data["direction_from"] == "flat_pattern", ok
    assert ok.data["sequence"] == [] and "no bend sequence" in ok.warnings[0]["message"]


def test_bend_list_pages_its_rows():
    nx = sm_nx()
    bracket(nx)
    ok = run(nx, "nx_get_bend_list", offset=1, limit=1)
    assert [i["index"] for i in ok.data["items"]] == [2]
    assert ok.page["total"] == 2 and ok.page["next_offset"] is None


def test_body_is_required_when_the_part_has_several_sheet_metal_bodies():
    nx = sm_nx()
    nx.tab()
    nx.tab()
    bad = run(nx, "nx_get_bend_list")
    assert bad.error["code"] == "NX_TARGET_REQUIRED" and bad.error["field"] == "/body"
    assert len(bad.error["details"]["candidates"]) == 2


def test_a_plain_solid_or_a_feature_that_is_no_flat_pattern_is_refused():
    nx = sm_nx()
    solid = nx.block(10, 10, 10)
    bad = run(nx, "nx_get_bend_list", body=solid.ref)
    assert bad.error["code"] == "NX_WRONG_GEOMETRY" and bad.error["field"] == "/body"
    body, _first, _second = bracket(nx)
    tab = nx.made("SB_TAB", body)
    tab.ref = nx.ref_of(tab, "feature")
    bad = run(nx, "nx_get_bend_list", body=body.ref, flat_pattern=tab.ref)
    assert bad.error["code"] == "NX_WRONG_KIND" and bad.error["field"] == "/flat_pattern"


def test_a_flat_pattern_of_another_body_is_refused():
    nx = sm_nx()
    body, first, _second = bracket(nx)
    other = nx.tab()
    feature = nx.flat_pattern(body, up=[first])
    bad = run(nx, "nx_get_bend_list", body=other.ref, flat_pattern=feature.ref)
    assert bad.error["code"] == "NX_WRONG_OWNER" and bad.error["field"] == "/flat_pattern"


def test_export_writes_the_rows_thickness_units_and_sequence_to_csv(tmp_path):
    nx = sm_nx()
    body, first, second = bracket(nx)
    nx.flat_pattern(body, up=[first], down=[second.opposite], sequence=[(1, "Bend 1")])
    ok = run(nx, "nx_export_bend_list", path="out/bends.csv")
    assert ok.ok, ok
    assert ok.data["bend_count"] == 2 and ok.data["direction_from"] == "flat_pattern"
    assert ok.data["overwritten"] is False and len(ok.data["sha256"]) == 64
    text = (services(nx).root / "out" / "bends.csv").read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    assert lines[0] == ("index,direction,angle,radius,length,neutral_factor,allowance,"
                        "deduction,state")  # fmt: skip
    assert lines[1].startswith("1,up,90.0,3.0,100.0,0.33,5.7491") and lines[2].startswith("2,down")
    assert "thickness,2.0" in lines and "sequence_id,sequence_name" in lines
    assert lines[-1] == "1,Bend 1"


def test_export_refuses_an_existing_file_without_overwrite():
    nx = sm_nx()
    bracket(nx)
    target = services(nx).root / "bends.csv"
    target.write_text("old")
    bad = run(nx, "nx_export_bend_list", path="bends.csv")
    assert bad.error["code"] == "NX_ALREADY_EXISTS" and bad.mutation_outcome == "not_started"
    ok = run(nx, "nx_export_bend_list", path="bends.csv", overwrite=True)
    assert ok.ok and ok.data["overwritten"] is True
    assert target.read_text(encoding="utf-8-sig").startswith("index,")


def test_a_hem_has_no_deduction_in_the_list_or_the_csv():
    nx = sm_nx()
    body = nx.tab(100, 80, 2.0)
    nx.bend_edges(nx.add_bend(body), 100.0, 4.71)
    nx.bend_edges(nx.add_bend(body, radius=0.002, angle=180.00000000000009), 100.0, 3.0)
    ok = run(nx, "nx_get_bend_list")
    assert ok.ok, ok
    flange, hem = ok.data["items"]
    assert flange["deduction"] == pytest.approx(BD_90) and hem["deduction"] is None
    assert hem["allowance"] == pytest.approx(math.pi * (0.002 + 0.33 * 2.0))
    done = run(nx, "nx_export_bend_list", path="hem.csv")
    assert done.ok, done
    lines = (services(nx).root / "hem.csv").read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].endswith(",deduction,state") and "fc:" not in "".join(lines)
    assert lines[2].split(",")[7] == "" and lines[2].endswith(",bent")


def test_a_part_without_sheet_metal_names_the_part_and_how_to_start():
    nx = sm_nx()
    nx.block(10, 10, 10)
    bad = run(nx, "nx_get_bend_list")
    assert bad.error["code"] == "NX_NOT_FOUND" and bad.error["field"] == "/body"
    assert "has no sheet-metal body" in bad.error["message"]
    assert "Give body" not in bad.error["message"]
    assert "operation=convert" in bad.error["hint"]


def test_an_existing_csv_is_named_workspace_relative():
    nx = sm_nx()
    bracket(nx)
    root = services(nx).root
    (root / "bends.csv").write_text("old")
    bad = run(nx, "nx_export_bend_list", path="bends.csv")
    assert bad.error["details"]["path"] == "bends.csv"
