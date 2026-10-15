"""Features that follow the part's sheet-metal defaults, and the defaults' getter/setter
fields (formulas, bend table), on the shared fake through the real kernel."""

from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import Expression
from tests.unit.sheet_metal.fakes import sm_nx


def flange_following(nx, *names):
    """A flange feature whose bend expressions name the part defaults ``names``."""
    body = nx.tab()
    feature = nx.made("SB_FLANGE", body)
    feature.ref = nx.ref_of(feature, "feature")
    feature.expressions = [nx.expression(f"p{k}", name, 2.5) for k, name in enumerate(names, 1)]
    feature.expressions.append(nx.expression("p9", "20.0", 20.0))
    return feature


def plain_feature(nx):
    feature = nx.made("SB_TAB", nx.tab())
    feature.ref = nx.ref_of(feature, "feature")
    feature.expressions = [nx.expression("p0", "2.0", 2.0)]
    return feature


def test_a_default_change_lists_the_features_that_follow_it():
    nx = sm_nx()
    flange = flange_following(nx, "Sheet_Metal_Bend_Radius", "Sheet_Metal_Neutral_Factor")
    tab = plain_feature(nx)
    ok = run(nx, "nx_set_sheet_metal_defaults", bend_radius=4)
    assert ok.ok, ok
    modified = [r["id"] for r in ok.changes["modified"]]
    assert flange.ref in modified and tab.ref not in modified
    notes = [w["message"] for w in ok.warnings if w["code"] == "NX_NOTE"]
    assert notes and "1 existing feature(s) follow the part defaults" in notes[0]
    assert flange.ref in notes[0] and "apply_to_existing=false" in notes[0]
    assert flange.expressions[0].RightHandSide == "Sheet_Metal_Bend_Radius"  # still linked


def test_a_feature_following_through_another_expression_is_listed():
    nx = sm_nx()
    radius = nx.prefs.expressions["BendRadius"]
    mine = nx.expression("my_r", "Sheet_Metal_Bend_Radius*2", 6.0)
    radius.dependents = [mine]
    feature = nx.made("SB_FLANGE", nx.tab())
    feature.ref = nx.ref_of(feature, "feature")
    feature.expressions = [nx.expression("p1", "my_r", 6.0)]
    mine.dependents = [feature.expressions[0]]
    ok = run(nx, "nx_set_sheet_metal_defaults", bend_radius=4)
    assert ok.ok, ok
    assert [r["id"] for r in ok.changes["modified"]] == [feature.ref]


def test_an_unchanged_value_or_an_unlinked_default_reports_nothing():
    nx = sm_nx()
    flange_following(nx, "Sheet_Metal_Bend_Radius")
    same = run(nx, "nx_set_sheet_metal_defaults", bend_radius=3)  # the fake's default
    assert same.ok and same.changes["modified"] == [] and same.warnings == [], same
    other = run(nx, "nx_set_sheet_metal_defaults", thickness=2)
    assert other.ok and other.changes["modified"] == [], other


def test_apply_to_existing_false_keeps_the_followers_values():
    nx = sm_nx()
    flange = flange_following(nx, "Sheet_Metal_Bend_Radius")
    linked = flange.expressions[0]
    ok = run(nx, "nx_set_sheet_metal_defaults", bend_radius=4, apply_to_existing=False)
    assert ok.ok, ok
    assert linked.RightHandSide == "2.5" and ok.data["bend_radius"] == 4
    assert [r["kind"] for r in ok.changes["modified"]] == ["expression"]
    notes = [w["message"] for w in ok.warnings if w["code"] == "NX_NOTE"]
    assert "kept them (1 expression(s) set to numbers)" in notes[0]
    # The compensating undo sets the default and the expression's formula back.
    undo = run(nx, "nx_undo_operation", mode="compensate")
    assert undo.ok and undo.data["compensated"], undo
    assert linked.RightHandSide == "Sheet_Metal_Bend_Radius"
    assert run(nx, "nx_get_sheet_metal_defaults").data["bend_radius"] == 3


def test_apply_to_existing_false_refuses_a_bend_method_change_before_nx():
    nx = sm_nx()
    flange_following(nx, "Sheet_Metal_Neutral_Factor")
    bad = run(nx, "nx_set_sheet_metal_defaults", bend_definition="bend_allowance_formula",
              apply_to_existing=False)  # fmt: skip
    assert bad.error["field"] == "/apply_to_existing" and bad.mutation_outcome == "not_started"
    assert nx.builders == []


def test_a_bend_method_change_lists_the_features_following_the_part_k():
    nx = sm_nx()
    flange = flange_following(nx, "Sheet_Metal_Neutral_Factor")
    ok = run(nx, "nx_set_sheet_metal_defaults", bend_definition="bend_allowance_formula")
    assert ok.ok, ok
    assert [r["id"] for r in ok.changes["modified"]] == [flange.ref]


def test_both_formulas_are_set_by_their_getter_names():
    nx = sm_nx()
    ok = run(nx, "nx_set_sheet_metal_defaults", bend_allowance_formula="(Radius+Thickness)*2",
             bend_deduction_formula="Thickness*3")  # fmt: skip
    assert ok.ok, ok
    b = nx.last("SheetMetalPreferencesBuilder")
    assert b.BendAllowanceFormula == "(Radius+Thickness)*2"
    assert b.BendDeductionFormula == "Thickness*3"
    data = ok.data
    assert data["bend_allowance_formula"] == "(Radius+Thickness)*2"
    assert data["bend_deduction_formula"] == "Thickness*3"
    assert data["bend_definition"] == "neutral_factor_value"  # the method is unchanged


def test_bend_formula_and_its_named_field_must_agree():
    nx = sm_nx()
    bad = run(nx, "nx_set_sheet_metal_defaults", bend_definition="bend_allowance_formula",
              bend_formula="2*Thickness", bend_allowance_formula="3*Thickness")  # fmt: skip
    assert bad.error["field"] == "/bend_formula" and bad.mutation_outcome == "not_started"


def test_the_bend_table_reads_back_workspace_relative():
    nx = sm_nx()
    root = services(nx).root
    (root / "tables").mkdir()
    (root / "tables" / "bends.xlsx").write_bytes(b"x")
    ok = run(nx, "nx_set_sheet_metal_defaults", bend_table="tables/bends.xlsx")
    assert ok.ok, ok
    assert ok.data["bend_table"] == "tables/bends.xlsx"
    assert str(root) not in str(run(nx, "nx_get_sheet_metal_defaults").data)
    assert nx.prefs.table.endswith("bends.xlsx") and nx.prefs.table != "tables/bends.xlsx"


def test_default_expressions_are_found_by_name():
    nx = sm_nx()
    assert isinstance(nx.prefs.GetBendRadius(), Expression)
    assert nx.prefs.GetBendRadius().Name == "Sheet_Metal_Bend_Radius"
