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
