"""Sheet-metal defaults and nx_get_sheet_metal on the shared fake, through the real kernel."""

import pytest

from tests.kit import run
from tests.unit.sheet_metal.fakes import FLAT, sm_nx


def test_defaults_are_read_without_enumerating_the_material_catalogue():
    nx = sm_nx(application="modeling")  # defaults work outside Sheet Metal too
    ok = run(nx, "nx_get_sheet_metal_defaults")
    assert ok.ok, ok
    data = ok.data
    assert data["thickness"] == 3 and data["neutral_factor"] == 0.33
    assert data["parameter_entry"] == "value" and data["bend_definition"] == "neutral_factor_value"
    assert data["bend_table"] == "BEND_TABLE" and data["material"] is None
    assert ok.mutation_outcome == "not_applicable"


def test_set_defaults_writes_the_builder_and_reads_every_value_back():
    nx = sm_nx()
    ok = run(
        nx,
        "nx_set_sheet_metal_defaults",
        thickness="2 mm",
        bend_radius=3,
        neutral_factor=0.4,
        bend_relief_width=1.5,
        bend_definition="bend_allowance_formula",
        bend_formula="(Radius+(Thickness*0.4))*rad(Angle)",
        material="Steel",
    )
    assert ok.ok, ok
    b = nx.last("SheetMetalPreferencesBuilder")
    assert b.MaterialThickness.RightHandSide == "2.0" and b.NeutralFactor.RightHandSide == "0.4"
    assert b.SetBendDefinitionMethod.calls[0][0][0] == (
        "BendDefinitionMethodOptions.BendAllowanceFormula"
    )
    assert b.BendAllowanceFormula == "(Radius+(Thickness*0.4))*rad(Angle)"
    assert b.SetMaterial.calls[0][0][0] == "Steel" and nx.live_builders == 0
    data = ok.data
    assert data["thickness"] == 2 and data["bend_relief_width"] == 1.5
    assert data["bend_definition"] == "bend_allowance_formula" and data["material"] == "Steel"


def test_a_default_nx_silently_kept_is_a_verification_failure():
    nx = sm_nx()
    nx.prefs.ignore.add("MaterialThickness")
    bad = run(nx, "nx_set_sheet_metal_defaults", thickness=4)
    assert bad.error["code"] == "NX_VERIFICATION_FAILED"
    assert bad.error["details"]["check"] == "thickness" and bad.mutation_outcome == "rolled_back"


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({}, None),
        ({"thickness": 0}, "/thickness"),
        ({"bend_radius": -1}, "/bend_radius"),
        ({"neutral_factor": 1.5}, "/neutral_factor"),
        ({"bend_formula": "2*Thickness", "bend_definition": "bend_table"}, "/bend_formula"),
        ({"bend_formula": "2*Thickness"}, "/bend_formula"),  # the part uses the K factor
    ],
)
def test_bad_defaults_are_refused_before_any_builder(args, field):
    nx = sm_nx()
    bad = run(nx, "nx_set_sheet_metal_defaults", **args)
    assert bad.error["category"] == "invalid_argument", bad
    assert bad.error["field"] == field
    assert bad.mutation_outcome == "not_started" and nx.builders == []


def test_bend_table_must_exist_in_the_workspace(tmp_path):
    nx = sm_nx()
    bad = run(nx, "nx_set_sheet_metal_defaults", bend_table="missing.xlsx")
    assert bad.error["code"] == "NX_FILE_NOT_FOUND" and bad.mutation_outcome == "not_started"


def test_get_sheet_metal_lists_bodies_thickness_and_bends_with_paging():
    nx = sm_nx()
    body = nx.tab()
    nx.add_bend(body, radius=3, angle=90, nf=0.33)
    nx.add_bend(body, radius=3, angle=80, nf=0.33, state=FLAT)
    plain = nx.block(10, 10, 10)
    ok = run(nx, "nx_get_sheet_metal")
    assert ok.ok, ok
    first, second = ok.data["items"]
    assert first["sheet_metal"] and first["thickness"] == 2 and first["bend_count"] == 2
    assert [b["state"] for b in first["bends"]] == ["bent", "flat"]
    assert first["bends"][1]["angle"] == 80
    assert second == {"body": second["body"], "sheet_metal": False}
    assert second["body"]["id"] == plain.ref
    page = run(nx, "nx_get_sheet_metal", limit=1)
    assert page.page["total"] == 2 and page.page["next_offset"] == 1
    one = run(nx, "nx_get_sheet_metal", body=body.ref)
    assert len(one.data["items"]) == 1
    bad = run(nx, "nx_get_sheet_metal", body=body.faces[0].ref)
    assert bad.error["code"] == "NX_WRONG_KIND"


def test_a_material_nx_drops_is_refused_with_what_to_send_instead():
    nx = sm_nx()
    nx.prefs.GetMaterialName = lambda: ""  # no standards table: NX keeps no material
    bad = run(nx, "nx_set_sheet_metal_defaults", thickness=2, material="steel")
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.error["field"] == "/material"
    assert bad.error["details"]["check"] == "material"
    assert "Leave material out" in bad.error["hint"] and bad.mutation_outcome == "rolled_back"


def test_material_reads_back_regardless_of_case():
    nx = sm_nx()
    nx.prefs.GetMaterialName = lambda: "Steel"
    ok = run(nx, "nx_set_sheet_metal_defaults", material="steel")
    assert ok.ok, ok
    assert ok.data["material"] == "Steel"


def test_a_table_parameter_entry_nx_keeps_as_value_names_the_working_choice():
    nx = sm_nx()
    nx.prefs.GetParameterEntryType = lambda: "ParameterEntryTypes.Value"
    bad = run(nx, "nx_set_sheet_metal_defaults", parameter_entry="material_table")
    assert bad.error["field"] == "/parameter_entry" and bad.mutation_outcome == "rolled_back"
    assert "use value" in bad.error["hint"]


def test_flat_pattern_bodies_are_flagged():
    nx = sm_nx()
    body = nx.tab()
    tab = nx.made("SB_TAB", body)
    flat = nx.tab()
    pattern = nx.made("FLAT_PATTERN", flat, feature_type="FLAT_PATTERN")
    body.GetFeatures = lambda: [tab, pattern]  # the bent body: not only a flat pattern
    flat.GetFeatures = lambda: [pattern]
    ok = run(nx, "nx_get_sheet_metal")
    assert ok.ok, ok
    first, second = ok.data["items"]
    assert first["flat_pattern"] is False and second["flat_pattern"] is True
    assert second["sheet_metal"] and second["thickness"] == 2


def test_flat_pattern_sub_feature_bodies_are_flagged():
    nx = sm_nx()
    flat = nx.tab()
    extract = nx.made("EXTRACT_BODY", flat, feature_type="EXTRACT_BODY")
    solid = nx.made("SB_FLAT_SOLID", flat, feature_type="SB_FLAT_SOLID")
    extract.JournalIdentifier, solid.JournalIdentifier = (
        "FLAT_PATTERN(5:1B:1B)",
        "FLAT_PATTERN(5:1B)",
    )
    flat.GetFeatures = lambda: [extract, solid]  # NX 2406's flat bodies
    ok = run(nx, "nx_get_sheet_metal")
    assert ok.ok, ok
    assert ok.data["items"][0]["flat_pattern"] is True
