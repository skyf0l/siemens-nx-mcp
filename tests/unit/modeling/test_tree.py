"""Feature tree and expression tools on the shared fake, through the real kernel."""

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.modeling import tree
from tests.kit import run
from tests.kit.objects import NXException
from tests.unit.modeling.fakes import feature, modeling_nx


def timeline(nx):
    """block -> boss (child of block) -> hole (child of block and boss)."""
    block, boss, hole = (feature(nx, k) for k in ("EXTRUDE", "EXTRUDE", "HOLE PACKAGE"))
    boss.parents = [block]
    hole.parents = [block, boss]
    for f in (block, boss, hole):
        f.GetChildren = lambda: []  # NX 2406: one-sided
        f.Suppressed = False
    nx.part.Features.SuppressFeatures = lambda fs: [
        nx.suppress_codes.__setitem__(int(x.Tag), 1) for f in fs for x in [f, *deps(nx, f)]
    ]
    nx.part.Features.UnsuppressFeatures = lambda fs: (
        [nx.suppress_codes.__setitem__(int(f.Tag), 0) for f in fs] and []
    )  # NX returns the features it could not unsuppress
    return block, boss, hole


def deps(nx, f):
    children = tree.child_map(list(nx.part.Features))
    by_tag = {int(x.Tag): x for x in nx.part.Features}
    return [by_tag[t] for t in tree.descendants(children, [int(f.Tag)])]


def test_child_map_uses_parents_because_get_children_is_one_sided():
    nx = modeling_nx()
    block, boss, hole = timeline(nx)
    children = tree.child_map([block, boss, hole])
    assert children[int(block.Tag)] == {int(boss.Tag), int(hole.Tag)}


def test_get_feature_reports_state_relations_and_parameters():
    nx = modeling_nx()
    block, boss, hole = timeline(nx)
    nx.suppress_codes[int(hole.Tag)] = 1
    exp = nx.expression("p3", "10", 10.0)
    exp.Description = "Extrude(2) End Limit"
    boss.expressions = [exp]
    ok = run(nx, "nx_get_feature", feature=nx.ref_of(boss, "feature"))
    assert ok.ok, ok
    data = ok.data
    assert data["parents"][0]["name"] == block.Name
    assert [c["name"] for c in data["children"]] == [hole.Name]
    assert data["parameters"][0]["description"] == "Extrude(2) End Limit"
    assert data["active"] and not data["suppressed"] and ok.mutation_outcome == "not_applicable"
    sup = run(nx, "nx_get_feature", feature=nx.ref_of(hole, "feature"))
    assert sup.data["suppressed"]


def test_edit_feature_by_alias_and_name_with_readback():
    nx = modeling_nx()
    f = feature(nx, "EXTRUDE")
    end = nx.expression("p4", "10", 10.0)
    end.Description = "Extrude(1) End Limit"
    start = nx.expression("p3", "0", 0.0)
    start.Description = "Extrude(1) Start Limit"
    f.expressions = [start, end]
    original = nx.part.Expressions.EditExpression

    def edit(exp, formula):
        original(exp, formula)
        exp.Value = float(formula) if formula.replace(".", "").isdigit() else exp.Value

    nx.part.Expressions.EditExpression = edit
    ok = run(
        nx,
        "nx_edit_feature",
        feature=nx.ref_of(f, "feature"),
        parameters=[{"name": "distance", "value": 25}, {"name": "p3", "value": "2"}],
    )
    assert ok.ok, ok
    assert end.RightHandSide == "25.0" and start.RightHandSide == "2"
    # NX keeps the old value: the read-back fails and rolls back.
    nx.part.Expressions.EditExpression = original
    bad = run(
        nx, "nx_edit_feature", feature=nx.ref_of(f, "feature"),
        parameters=[{"name": "distance", "value": 30}],
    )  # fmt: skip
    assert bad.error["code"] == "NX_VERIFICATION_FAILED" and bad.mutation_outcome == "rolled_back"


def test_edit_feature_errors():
    nx = modeling_nx()
    f = feature(nx, "EXTRUDE")
    exp = nx.expression("p4", "10", 10.0)
    exp.Description = "Extrude(1) End Limit"
    f.expressions = [exp]
    ref = nx.ref_of(f, "feature")
    unknown = run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "radius", "value": 1}])
    assert unknown.error["code"] == "NX_NOT_FOUND" and unknown.mutation_outcome == "not_started"
    both = run(nx, "nx_edit_feature", feature=ref)
    assert both.error["category"] == "invalid_argument"
    formula = run(
        nx, "nx_edit_feature", feature=ref, parameters=[{"name": "p4", "value": "invalid"}]
    )
    assert formula.error["code"] == "NX_INVALID_FORMULA" and formula.error["nx_code"] == 1050017
    exp.IsNoEdit = True
    locked = run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "p4", "value": 3}])
    assert locked.error["code"] == "NX_READ_ONLY"
    plain = run(
        nx, "nx_edit_feature", feature=ref,
        definition={"transform": {"translation": [1, 0, 0]}},
    )  # fmt: skip
    assert plain.error["code"] == "NX_UNSUPPORTED_OPTION" and plain.error["field"] == "/definition"


def test_edit_feature_update_failure_rolls_back():
    nx = modeling_nx()
    f = feature(nx, "EXTRUDE")
    exp = nx.expression("p4", "10", 10.0)
    f.expressions = [exp]
    nx.update_errors = 1
    bad = run(nx, "nx_edit_feature", feature=nx.ref_of(f, "feature"),
              parameters=[{"name": "p4", "value": "12"}])  # fmt: skip
    assert bad.error["code"] == "NX_UPDATE_FAILED" and bad.mutation_outcome == "rolled_back"


def test_suppression_dependents_and_inactive_features():
    nx = modeling_nx()
    block, boss, hole = timeline(nx)
    ref = nx.ref_of(boss, "feature")
    ok = run(nx, "nx_set_feature_suppression", features=[ref], suppressed=True)
    assert ok.ok, ok
    assert {r["name"] for r in ok.data["changed"]} == {boss.Name, hole.Name}
    back = run(nx, "nx_set_feature_suppression", features=[ref], suppressed=False)
    assert [r["name"] for r in back.data["changed"]] == [boss.Name]
    assert [r["name"] for r in back.data["still_suppressed"]] == [hole.Name]
    nx.suppress_codes[int(hole.Tag)] = 16  # inactive (after the current feature)
    bad = run(
        nx, "nx_set_feature_suppression", features=[nx.ref_of(hole, "feature")], suppressed=False
    )
    assert (
        bad.error["category"] == "invalid_argument"
        and "nx_set_current_feature" in bad.error["hint"]
    )
    assert bad.mutation_outcome == "rolled_back" and bad.changes["references"] == "preserved"


def test_suppression_that_does_not_stick_rolls_back():
    nx = modeling_nx()
    block, boss, hole = timeline(nx)
    nx.part.Features.SuppressFeatures = lambda fs: None
    bad = run(
        nx, "nx_set_feature_suppression", features=[nx.ref_of(boss, "feature")], suppressed=True
    )
    assert bad.error["details"]["check"] == "suppressed" and bad.mutation_outcome == "rolled_back"


def test_set_current_feature_and_roll_to_end():
    nx = modeling_nx()
    block, boss, hole = timeline(nx)
    for f in (block, boss, hole):
        f.MakeCurrentFeature = lambda f=f: setattr(nx.part, "CurrentFeature", f)
    ok = run(nx, "nx_set_current_feature", feature=nx.ref_of(block, "feature"))
    assert ok.ok, ok
    assert [r["name"] for r in ok.data["inactive"]] == [boss.Name, hole.Name]
    end = run(nx, "nx_set_current_feature")
    assert end.data["current"]["name"] == hole.Name and end.data["inactive"] == []
    block.MakeCurrentFeature = lambda: None
    nx.part.CurrentFeature = hole
    bad = run(nx, "nx_set_current_feature", feature=nx.ref_of(block, "feature"))
    assert bad.error["details"]["check"] == "current"


def test_rolling_back_names_the_bodies_nx_removes_until_rolled_forward():
    """Live: the inactive SPHERE body was listed as deleted with no word why."""
    nx = modeling_nx()
    block, boss, hole = timeline(nx)
    sphere = nx.block(1, 1, 1)

    def roll_back():
        nx.part.CurrentFeature = block
        nx.part.Bodies.remove(sphere)  # NX drops the inactive feature's body

    block.MakeCurrentFeature = roll_back
    ok = run(nx, "nx_set_current_feature", feature=nx.ref_of(block, "feature"))
    assert ok.ok, ok
    notes = [w["message"] for w in ok.warnings if w["code"] == "NX_NOTE"]
    assert notes and "1 body(ies)" in notes[0] and "new refs" in notes[0]
    missing = run(nx, "nx_set_current_feature", feature="f:999999")
    assert missing.mutation_outcome == "not_started"


def test_reorder_checks_dependencies_before_nx():
    nx = modeling_nx()
    block, boss, hole = timeline(nx)
    tool = feature(nx, "BLEND")
    tool.parents = [boss]
    tool.GetChildren = lambda: []
    calls = []

    def reorder(features, anchor, how):
        calls.append(how)
        moved = features[0]
        order = [f for f in nx.part.Features if f is not moved]
        at = order.index(anchor) + (0 if how == "before" else 1)
        order.insert(at, moved)
        for i, f in enumerate(order, 1):
            f.Timestamp = i
        nx.part.Features[:] = order

    nx.part.Features.ReorderFeature = reorder
    kinds = nx.nx.Features.FeatureCollection.ReorderType
    kinds.Before, kinds.After = "before", "after"
    blend, hole_ref = nx.ref_of(tool, "feature"), nx.ref_of(hole, "feature")
    ok = run(nx, "nx_set_feature_order", feature=blend, anchor=hole_ref, position="before")
    assert ok.ok and ok.data["moved"] and ok.data["feature"]["index"] == 2, ok
    bad = run(
        nx,
        "nx_set_feature_order",
        feature=blend,
        anchor=nx.ref_of(boss, "feature"),
        position="before",
    )
    assert bad.error["code"] == "NX_DEPENDENCY_ORDER" and calls == ["before"]
    assert bad.mutation_outcome == "not_started"  # a refusal before NX: nothing changed
    assert bad.error["details"]["conflicts"][0]["relation"] == "parent"


def test_native_reorder_refusal_keeps_the_nx_code():
    nx = modeling_nx()
    block, boss, hole = timeline(nx)
    other = feature(nx, "DATUM_PLANE")
    other.GetChildren = lambda: []

    def refuse(*_a):
        raise NXException(670529, "Cannot reorder. Feature to be reordered is being referenced.")

    nx.part.Features.ReorderFeature = refuse
    bad = run(nx, "nx_set_feature_order", feature=nx.ref_of(other, "feature"),
              anchor=nx.ref_of(boss, "feature"), position="before")  # fmt: skip
    assert bad.error["code"] == "NX_DEPENDENCY_ORDER" and bad.error["nx_code"] == 670529


def test_set_expression_creates_edits_and_keeps_units():
    nx = modeling_nx()
    made = run(nx, "nx_set_expression", name="wall", formula="2.5", units="mm")
    assert made.ok and made.data["created"], made
    again = run(nx, "nx_set_expression", name="wall", formula="3")
    assert again.ok and not again.data["created"] and again.data["formula"] == "3"
    clash = run(nx, "nx_set_expression", name="wall", formula="3", units="deg")
    assert clash.error["field"] == "/units"
    bad = run(nx, "nx_set_expression", name="wall", formula="invalid")
    assert bad.error["code"] == "NX_INVALID_FORMULA" and bad.mutation_outcome == "rolled_back"
    name = run(nx, "nx_set_expression", name="1bad", formula="3")
    assert name.error["field"] == "/name" and name.mutation_outcome == "not_started"


def test_set_expression_solves_a_driven_active_sketch():
    nx = modeling_nx()
    exp = nx.expression("d1", "10", 10.0)
    sk = nx.sketch()
    dim = NS(AssociatedExpression=exp, AssociatedDimension=NS(ComputedSize=10.0))
    sk.constraints = [dim]
    nx.session.ActiveSketch = sk

    def edit(e, formula):
        e.RightHandSide, e.Value = formula, float(formula)

    nx.part.Expressions.EditExpression = edit
    bad = run(nx, "nx_set_expression", name="d1", formula="15")
    assert bad.error["code"] == "NX_CONSTRAINT_UNSATISFIED", bad  # solver left it at 10
    assert sk.updates[:1] == ["LocalUpdate"]
    dim.AssociatedDimension.ComputedSize = 15.0
    ok = run(nx, "nx_set_expression", name="d1", formula="15")
    assert ok.ok, ok


def test_update_model_reports_health_and_fails_on_update_errors():
    nx = modeling_nx()
    body = nx.block(1, 1, 1)
    f = feature(nx, "EXTRUDE", [body])
    f.GetFeatureWarningMessages = lambda: ["Thin wall"]
    ok = run(nx, "nx_update_model")
    assert ok.ok and ok.data["warnings"][0]["message"] == "Thin wall", ok
    assert ok.data["bodies_checked"] == 1
    nx.faults[int(body.Tag)] = 2
    assert run(nx, "nx_update_model").data["errors"][0]["message"] == "2 consistency faults"
    nx.update_errors = 3
    bad = run(nx, "nx_update_model")
    assert bad.error["code"] == "NX_UPDATE_FAILED" and bad.mutation_outcome == "rolled_back"


@pytest.mark.parametrize("position", ["before", "after"])
def test_reorder_conflict_rules(position):
    order = [1, 2, 3, 4]
    parents = {4: {2}}
    children = {2: {4}}
    assert tree.reorder_conflicts(order, parents, children, 4, 1, position) == [(2, "parent")]
    if position == "after":
        assert tree.reorder_conflicts(order, parents, children, 2, 4, "after") == [(4, "child")]
    assert tree.expected_order(order, 4, 2, position) == (
        [1, 4, 2, 3] if position == "before" else [1, 2, 4, 3]
    )


MM = NS(Name="MilliMeter", Abbreviation="mm")


def numeric(nx):
    """Fake NX that evaluates numeric formulas, so read-backs see the new value."""
    create, edit = nx.part.Expressions.CreateNumberExpression, nx.part.Expressions.EditExpression

    def made(spec, unit):
        e = create(spec, unit)
        e.Value = float(e.RightHandSide)
        return e

    def edited(e, formula):
        edit(e, formula)
        e.Value = float(formula) if formula.replace(".", "").replace("-", "").isdigit() else 0.0

    nx.part.Expressions.CreateNumberExpression = made
    nx.part.Expressions.EditExpression = edited


def test_set_expression_takes_numbers_and_unit_strings():
    nx = modeling_nx()
    numeric(nx)
    num = run(nx, "nx_set_expression", name="n", formula=25)
    assert num.ok and num.data["formula"] == "25" and num.data["value"] == 25.0, num
    made = run(nx, "nx_set_expression", name="thickness", formula="10 mm")
    assert made.ok and made.data["formula"] == "10", made
    exp = next(e for e in nx.part.Expressions if e.Name == "thickness")
    assert exp.Units.Name == "MilliMeter"  # the string's unit became the expression's
    exp.Units = MM
    inch = run(nx, "nx_set_expression", name="thickness", formula="0.5 in")
    assert inch.ok and inch.data["value"] == pytest.approx(12.7), inch
    assert exp.RightHandSide == "12.7"
    wrong = run(nx, "nx_set_expression", name="thickness", formula="3 deg")
    assert wrong.error["field"] == "/formula" and "mm" in wrong.error["hint"]
    assert wrong.mutation_outcome == "not_started"
    plain = run(nx, "nx_set_expression", name="n", formula="4 mm")
    assert plain.error["field"] == "/formula" and "unitless" in plain.error["message"]
    clash = run(nx, "nx_set_expression", name="ang", formula="10 mm", units="deg")
    assert clash.error["field"] == "/formula" and clash.mutation_outcome == "not_started"
    conv = run(nx, "nx_set_expression", name="len_in", formula="25.4 mm", units="in")
    assert conv.ok and conv.data["value"] == pytest.approx(1.0), conv
    syntax = run(nx, "nx_set_expression", name="n", formula="invalid")
    assert syntax.error["code"] == "NX_INVALID_FORMULA" and "10 mm" in syntax.error["hint"]


def test_set_expression_lists_the_features_it_drives():
    nx = modeling_nx()
    numeric(nx)
    base = nx.expression("len", "10", 10.0)
    p1 = nx.expression("p1", "len", 10.0)
    base.dependents = [p1]
    f = feature(nx, "EXTRUDE")
    f.expressions = [p1]
    feature(nx, "BLEND").expressions = []
    ok = run(nx, "nx_set_expression", name="len", formula="20")
    assert ok.ok, ok
    assert [r["name"] for r in ok.data["features"]] == [f.Name]


def extrude(nx):
    f = feature(nx, "EXTRUDE")
    start = nx.expression("p3", "0", 0.0)
    start.Description = "Extrude(1) Start Limit"
    end = nx.expression("p4", "10", 10.0)
    end.Description = "Extrude(1) End Limit"
    end.Units = MM
    f.expressions = [start, end]
    return f, start, end


def test_get_feature_lists_the_aliases_each_parameter_answers_to():
    nx = modeling_nx()
    f, _, _ = extrude(nx)
    data = run(nx, "nx_get_feature", feature=nx.ref_of(f, "feature")).data
    assert [p["aliases"] for p in data["parameters"]] == [["start"], ["end", "distance"]]


def test_edit_feature_takes_unit_strings_and_any_case_aliases():
    nx = modeling_nx()
    numeric(nx)
    f, start, end = extrude(nx)
    ref = nx.ref_of(f, "feature")
    ok = run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "Distance", "value": "1 in"}])
    assert ok.ok and end.RightHandSide == "25.4", ok
    bad = run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "end", "value": "5 deg"}])
    assert bad.error["field"] == "/parameters/0/value" and bad.mutation_outcome == "not_started"
    plain = run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "p3", "value": "2 mm"}])
    assert plain.error["code"] == "NX_INVALID_ARGUMENT" and "unitless" in plain.error["message"]
    syntax = run(
        nx, "nx_edit_feature", feature=ref, parameters=[{"name": "p4", "value": "invalid"}]
    )
    assert syntax.error["code"] == "NX_INVALID_FORMULA" and "10 mm" in syntax.error["hint"]


def test_edit_feature_unknown_name_lists_names_and_aliases():
    nx = modeling_nx()
    f, _, _ = extrude(nx)
    ref = nx.ref_of(f, "feature")
    out = run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "height", "value": 5}])
    assert out.error["code"] == "NX_NOT_FOUND" and out.error["field"] == "/parameters/0/name"
    assert out.error["details"]["aliases"] == {"start": "p3", "end": "p4", "distance": "p4"}
    assert "details.aliases" in out.error["hint"]
    sketch = feature(nx, "SKETCH")
    sketch.expressions = []
    none = run(nx, "nx_edit_feature", feature=nx.ref_of(sketch, "feature"),
               parameters=[{"name": "distance", "value": 5}])  # fmt: skip
    assert "has no parameters" in none.error["message"] and "nx_get_sketch" in none.error["hint"]


def test_edit_feature_matches_its_own_expression_names_without_case():
    nx = modeling_nx()
    numeric(nx)
    f, start, _ = extrude(nx)
    ok = run(nx, "nx_edit_feature", feature=nx.ref_of(f, "feature"),
             parameters=[{"name": "P3", "value": 2}])  # fmt: skip
    assert ok.ok and start.RightHandSide == "2.0", ok


def nx_named(features, keep_name=True):
    """Journal identifiers like NX's; NX leaves Feature.Name empty (``keep_name=False``),
    the fake's ref rows read Name."""
    for f in features:
        f.JournalIdentifier, f.Name = f.Name, f.Name if keep_name else ""


def renumbering_reorder(nx):
    """ReorderFeature that renumbers journal identifiers like NX does."""

    def reorder(features, anchor, how):
        moved = features[0]
        order = [f for f in nx.part.Features if f is not moved]
        order.insert(order.index(anchor) + (0 if how == "before" else 1), moved)
        for i, f in enumerate(order, 1):
            f.Timestamp = i
            f.JournalIdentifier = f.Name = f"{f.FeatureType}({i})"
        nx.part.Features[:] = order

    nx.part.Features.ReorderFeature = reorder
    kinds = nx.nx.Features.FeatureCollection.ReorderType
    kinds.Before, kinds.After = "before", "after"


def test_reorder_takes_before_or_after_alone_and_reports_renamed_features():
    nx = modeling_nx()
    block, boss, hole = timeline(nx)
    blend = feature(nx, "BLEND")
    blend.parents, blend.GetChildren = [boss], lambda: []
    nx_named([block, boss, hole, blend])
    renumbering_reorder(nx)
    ok = run(nx, "nx_set_feature_order", feature=nx.ref_of(blend, "feature"),
             before=nx.ref_of(hole, "feature"))  # fmt: skip
    assert ok.ok and ok.data["moved"], ok
    assert (
        ok.data["feature"]["name"] == "BLEND(3)" and ok.data["anchor"]["name"] == "HOLE PACKAGE(4)"
    )
    assert {(r["name"], r["was"]) for r in ok.data["renamed"]} == {
        ("BLEND(3)", "BLEND(4)"),
        ("HOLE PACKAGE(4)", "HOLE PACKAGE(3)"),
    }
    back = run(nx, "nx_set_feature_order", feature=nx.ref_of(blend, "feature"),
               after=nx.ref_of(hole, "feature"))  # fmt: skip
    assert back.ok and back.data["feature"]["index"] == 3, back
    same = run(nx, "nx_set_feature_order", feature=nx.ref_of(blend, "feature"),
               after=nx.ref_of(hole, "feature"))  # fmt: skip
    assert same.ok and not same.data["moved"] and same.data["renamed"] == []


def test_reorder_placement_errors_say_what_to_send():
    nx = modeling_nx()
    block, boss, hole = timeline(nx)
    nx_named([block, boss, hole], keep_name=False)
    f, a = nx.ref_of(hole, "feature"), nx.ref_of(block, "feature")
    both = run(nx, "nx_set_feature_order", feature=f, after=a, anchor=a, position="after")
    assert both.error["field"] == "/after" and both.mutation_outcome == "not_started"
    alone = run(nx, "nx_set_feature_order", feature=f, anchor=a)
    assert alone.error["field"] == "/position" and "before/after" in alone.error["message"]
    none = run(nx, "nx_set_feature_order", feature=f)
    assert none.error["field"] == "/anchor" and "after:" in none.error["hint"]
    # The conflict message names the features (NX leaves Feature.Name empty).
    bad = run(nx, "nx_set_feature_order", feature=f, before=nx.ref_of(boss, "feature"))
    assert bad.error["code"] == "NX_DEPENDENCY_ORDER" and bad.error["field"] == "/before"
    assert bad.error["message"] == (
        "Moving HOLE PACKAGE(3) before EXTRUDE(2) would put it before its parent EXTRUDE(2)"
    )


def test_internal_features_are_refused_with_their_owner_and_not_listed():
    nx = modeling_nx()
    block, boss, hole = timeline(nx)
    nx_named([block, boss, hole])
    sub = feature(nx, "SIMPLE HOLE")
    sub.IsInternal, sub.JournalIdentifier, sub.Name = True, "HOLE PACKAGE(3:1A)", ""
    datum = feature(nx, "DATUM_CSYS")
    datum.IsInternal, datum.JournalIdentifier, datum.Timestamp = True, "DATUM_CSYS(9)", 2
    bad = run(nx, "nx_set_feature_suppression", features=[nx.ref_of(sub, "feature")],
              suppressed=True)  # fmt: skip
    owner = nx.ref_of(hole, "feature")
    assert bad.error["field"] == "/features/0" and bad.error["details"]["owner"]["id"] == owner
    assert "HOLE PACKAGE(3)" in bad.error["message"] and owner in bad.error["hint"]
    by_stamp = run(nx, "nx_get_feature", feature=nx.ref_of(datum, "feature"))
    assert by_stamp.error["details"]["owner"]["name"] == "EXTRUDE(2)"
    listed = run(nx, "nx_list_objects", kind="feature")
    assert [r["name"] for r in listed.data["items"]] == [
        "EXTRUDE(1)",
        "EXTRUDE(2)",
        "HOLE PACKAGE(3)",
    ]


def test_rolled_back_features_are_inactive_not_suppressed():
    nx = modeling_nx()
    block, boss, hole = timeline(nx)
    nx.part.CurrentFeature = block
    for f in (boss, hole):
        f.Suppressed = True  # NX: True for features after the current one too
        nx.suppress_codes[int(f.Tag)] = 16
    health = run(nx, "nx_update_model")
    assert health.ok and health.data["suppressed"] == 0 and health.data["inactive"] == 2, health
    listed = run(nx, "nx_list_objects", kind="feature").data["items"]
    assert [r["details"]["suppressed"] for r in listed] == [False, False, False]
    assert [r["details"].get("inactive", False) for r in listed] == [False, True, True]
    nx.suppress_codes[int(boss.Tag)] = 1 | 16
    assert run(nx, "nx_update_model").data["suppressed"] == 1
