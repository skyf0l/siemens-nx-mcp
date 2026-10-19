"""Feature tree edits that read back honestly and set themselves back: nx_edit_feature,
nx_set_expression, nx_set_current_feature, nx_set_feature_order and
nx_set_feature_suppression keep their before-values, and the compensating undo
(``nx_undo_operation`` mode compensate) restores them or refuses a value changed since."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace as NS

from tests.kit import run
from tests.kit.objects import NXException
from tests.unit.modeling.fakes import feature, modeling_nx
from tests.unit.modeling.test_tree import timeline


def numeric_edits(nx):
    """EditExpression that evaluates plain numbers (NX keeps the old value otherwise)."""
    original = nx.part.Expressions.EditExpression

    def edit(exp, formula):
        original(exp, formula)
        with contextlib.suppress(ValueError):
            exp.Value = float(formula)

    nx.part.Expressions.EditExpression = edit


def block_feature(nx):
    f = feature(nx, "BLOCK")
    width = nx.expression("p2", "30", 30.0)
    width.Description = "Block(1) Width (YC)"
    width.Units = NS(Name="MilliMeter", Abbreviation="mm")
    f.expressions = [width]
    numeric_edits(nx)
    return f, width


def compensate(nx, **kw):
    return run(nx, "nx_undo_operation", mode="compensate", **kw)


def test_an_infinite_number_is_refused_before_nx():
    nx = modeling_nx()
    f, width = block_feature(nx)
    bad = run(nx, "nx_edit_feature", feature=nx.ref_of(f, "feature"),
              parameters=[{"name": "width", "value": "1e400"}])  # fmt: skip
    assert bad.error["code"] == "NX_INVALID_FORMULA", bad
    assert bad.error["field"] == "/parameters/0/value" and bad.mutation_outcome == "not_started"
    assert width.RightHandSide == "30"
    expr = run(nx, "nx_set_expression", name="p2", formula="1e400")
    assert expr.error["code"] == "NX_INVALID_FORMULA" and expr.mutation_outcome == "not_started"


def test_a_formula_without_a_finite_value_rolls_back():
    nx = modeling_nx()
    f, width = block_feature(nx)
    original = nx.part.Expressions.EditExpression

    def overflow(exp, formula):
        original(exp, formula)
        exp.Value = float("inf")

    nx.part.Expressions.EditExpression = overflow
    bad = run(nx, "nx_edit_feature", feature=nx.ref_of(f, "feature"),
              parameters=[{"name": "width", "value": "1e300*1e300"}])  # fmt: skip
    assert bad.error["code"] == "NX_INVALID_FORMULA" and "finite" in bad.error["message"]


def test_a_formula_nx_stores_without_evaluating_it_rolls_back():
    """NX 2406 keeps the old value for an overflowing formula; a fresh evaluation differs."""
    nx = modeling_nx()
    f, width = block_feature(nx)
    nx.expression("big", "400", 400.0)  # the fake keeps the old value for any formula
    stale = run(nx, "nx_edit_feature", feature=nx.ref_of(f, "feature"),
                parameters=[{"name": "width", "value": "big/10"}])  # fmt: skip
    assert stale.error["code"] == "NX_INVALID_FORMULA", stale
    assert stale.error["details"]["evaluates_to"] == 40
    fine = run(nx, "nx_edit_feature", feature=nx.ref_of(f, "feature"),
               parameters=[{"name": "width", "value": "big*0+30"}])  # fmt: skip
    assert fine.ok and width.RightHandSide == "big*0+30", fine


def test_a_number_written_as_text_reads_back_as_that_number():
    nx = modeling_nx()
    f, width = block_feature(nx)
    nx.part.Expressions.EditExpression = lambda e, formula: setattr(e, "RightHandSide", formula)
    stale = run(nx, "nx_edit_feature", feature=nx.ref_of(f, "feature"),
                parameters=[{"name": "width", "value": "25"}])  # fmt: skip
    assert (
        stale.error["code"] == "NX_VERIFICATION_FAILED" and stale.mutation_outcome == "rolled_back"
    )


def test_a_length_at_or_below_zero_names_the_parameter_nx_refused():
    nx = modeling_nx()
    f, width = block_feature(nx)

    def undone(mark):
        raise NXException(670024, "Update undo happened.")

    nx.session.UpdateManager = NS(DoUpdate=undone)
    bad = run(nx, "nx_edit_feature", feature=nx.ref_of(f, "feature"),
              parameters=[{"name": "width", "value": -5}])  # fmt: skip
    assert bad.error["code"] == "NX_UPDATE_FAILED" and bad.mutation_outcome == "rolled_back"
    assert bad.error["field"] == "/parameters/0/value" and "above zero" in bad.error["hint"]


def test_edit_feature_is_set_back_by_compensation_and_never_over_a_later_edit():
    nx = modeling_nx()
    f, width = block_feature(nx)
    ref = nx.ref_of(f, "feature")
    done = run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "width", "value": 40}])
    assert done.ok and width.Value == 40, done
    assert {"kind": "expression", "name": "p2"}.items() <= done.changes["modified"][0].items()
    undo = compensate(nx)
    assert undo.ok and undo.data["compensated"], undo
    assert width.RightHandSide == "30" and width.Value == 30
    assert undo.data["restored"] == [f"{ref}: p2 back to 30"]
    first = run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "p2", "value": "35"}])
    assert first.ok and run(nx, "nx_set_expression", name="p2", formula="36").ok
    refused = compensate(nx, operation=first.operation_id)
    assert refused.error["code"] == "NX_DEPENDENCY_ORDER", refused
    assert width.RightHandSide == "36"


def test_set_expression_warns_when_a_formula_stops_following_another_expression():
    nx = modeling_nx()
    nx.expression("w", "10", 10.0)
    p0 = nx.expression("p0", "w", 10.0)
    numeric_edits(nx)
    ok = run(nx, "nx_set_expression", name="p0", formula="12")
    assert ok.ok and p0.RightHandSide == "12", ok
    assert [r["name"] for r in ok.changes["modified"]] == ["p0"]
    notes = [w["message"] for w in ok.warnings if w["code"] == "NX_NOTE"]
    assert notes and "no longer follows w" in notes[0]
    again = run(nx, "nx_set_expression", name="p0", formula="13")
    assert not [w for w in again.warnings if "no longer follows" in w["message"]]


def rolling(nx):
    block, boss, hole = timeline(nx)
    for f in (block, boss, hole):
        f.MakeCurrentFeature = lambda f=f: setattr(nx.part, "CurrentFeature", f)
    nx.part.CurrentFeature = hole
    return block, boss, hole


def test_set_current_feature_is_set_back_by_compensation():
    nx = modeling_nx()
    block, boss, hole = rolling(nx)
    ok = run(nx, "nx_set_current_feature", feature=nx.ref_of(boss, "feature"))
    assert ok.ok and nx.part.CurrentFeature is boss, ok
    undo = compensate(nx)
    assert undo.ok and nx.part.CurrentFeature is hole, undo
    assert undo.data["restored"] == ["timeline rolled to its end"]
    assert run(nx, "nx_set_current_feature", feature=nx.ref_of(boss, "feature")).ok
    back = run(nx, "nx_set_current_feature", feature=nx.ref_of(block, "feature"))
    assert back.ok and run(nx, "nx_set_current_feature").ok
    refused = compensate(nx, operation=back.operation_id)
    assert refused.error["code"] == "NX_DEPENDENCY_ORDER", refused


def reorderable(nx):
    def reorder(features, anchor, how):
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


def test_nothing_moves_before_the_base_datum_csys():
    nx = modeling_nx()
    csys = feature(nx, "DATUM_CSYS")
    block = feature(nx, "BLOCK")
    for f in (csys, block):
        f.GetChildren = lambda: []
    reorderable(nx)
    bad = run(nx, "nx_set_feature_order", feature=nx.ref_of(block, "feature"),
              before=nx.ref_of(csys, "feature"))  # fmt: skip
    assert bad.error["code"] == "NX_DEPENDENCY_ORDER" and bad.error["field"] == "/before", bad
    assert bad.mutation_outcome == "not_started" and "base datum CSYS" in bad.error["message"]
    own = run(nx, "nx_set_feature_order", feature=nx.ref_of(csys, "feature"),
              after=nx.ref_of(block, "feature"))  # fmt: skip
    assert own.error["code"] == "NX_DEPENDENCY_ORDER" and own.error["field"] == "/feature"


def test_set_feature_order_is_set_back_by_compensation():
    nx = modeling_nx()
    a, b, c = (feature(nx, k) for k in ("BLOCK", "SPHERE", "CYLINDER"))
    for f in (a, b, c):
        f.GetChildren = lambda: []
    reorderable(nx)
    ok = run(nx, "nx_set_feature_order", feature=nx.ref_of(c, "feature"),
             before=nx.ref_of(a, "feature"))  # fmt: skip
    assert ok.ok and list(nx.part.Features) == [c, a, b], ok
    undo = compensate(nx)
    assert undo.ok and list(nx.part.Features) == [a, b, c], undo
    moved = run(nx, "nx_set_feature_order", feature=nx.ref_of(a, "feature"),
                after=nx.ref_of(c, "feature"))  # fmt: skip
    assert moved.ok and run(nx, "nx_set_feature_order", feature=nx.ref_of(b, "feature"),
                            after=nx.ref_of(a, "feature")).ok  # fmt: skip
    refused = compensate(nx, operation=moved.operation_id)
    assert refused.error["code"] == "NX_DEPENDENCY_ORDER", refused


def test_suppression_is_set_back_by_compensation_and_notes_parents():
    nx = modeling_nx()
    block, boss, hole = timeline(nx)
    ref = nx.ref_of(boss, "feature")
    ok = run(nx, "nx_set_feature_suppression", features=[ref], suppressed=True)
    assert ok.ok and nx.suppress_codes[int(hole.Tag)] == 1, ok
    undo = compensate(nx)
    assert undo.ok, undo
    assert nx.suppress_codes[int(boss.Tag)] == 0 and nx.suppress_codes[int(hole.Tag)] == 0
    # NX unsuppresses the suppressed parent a listed feature needs: said, not silent.
    nx.suppress_codes[int(boss.Tag)] = 1
    nx.suppress_codes[int(hole.Tag)] = 1
    nx.part.Features.UnsuppressFeatures = lambda fs: (
        [nx.suppress_codes.__setitem__(int(x.Tag), 0) for f in fs for x in (f, *f.parents)] and []
    )
    up = run(nx, "nx_set_feature_suppression", features=[nx.ref_of(hole, "feature")],
             suppressed=False)  # fmt: skip
    assert up.ok, up
    assert any("also unsuppressed" in w["message"] and boss.Name in w["message"]
               for w in up.warnings), up.warnings  # fmt: skip


def test_suppressing_the_base_datum_csys_is_said():
    nx = modeling_nx()
    csys = feature(nx, "DATUM_CSYS")
    csys.GetChildren = lambda: []
    csys.Suppressed = False
    nx.part.Features.SuppressFeatures = lambda fs: [
        nx.suppress_codes.__setitem__(int(f.Tag), 1) for f in fs
    ]
    ok = run(nx, "nx_set_feature_suppression", features=[nx.ref_of(csys, "feature")],
             suppressed=True)  # fmt: skip
    assert ok.ok and any("base datum CSYS" in w["message"] for w in ok.warnings), ok


def threaded_hole(nx):
    """A threaded hole's parameters as NX 2406 describes them, after its first update:
    the thread table locks major/minor/pitch/angle and, at a table engagement, the tap
    drill (NX unlocks the tap drill once the engagement is "Custom")."""
    f = feature(nx, "HOLE PACKAGE")
    rows = {}
    for name, what, formula, value in (
        ("p106", "Thread Size", '"M6 x 1.0"', None),
        ("p107", "Radial Engage", '"0.75"', None),
        ("p114", "Major Diameter", "6 // Used By ...", 6.0),
        ("p45", "Tap Drill Diameter", "5", 5.0),
        ("p46", "Thread Depth", "10.0", 10.0),
    ):
        e = nx.expression(name, formula, value or 0.0)
        e.Description = f"(M6 x 1.0 Threaded Hole(2) {what})"
        if value is None:
            e.Type = "String"
        else:
            e.Units = NS(Name="MilliMeter", Abbreviation="mm")
        e.IsRightHandSideLockedFromEdit = name in ("p114", "p45")
        rows[name] = e
    f.expressions = list(rows.values())
    numeric_edits(nx)
    original = nx.part.Expressions.EditExpression

    def edit(exp, formula):
        if exp.IsRightHandSideLockedFromEdit:
            raise NXException(1050027, "This expression cannot be modified because it is locked.")
        original(exp, formula)
        if exp is rows["p107"]:
            rows["p45"].IsRightHandSideLockedFromEdit = formula != '"Custom"'

    nx.part.Expressions.EditExpression = edit
    return f, rows


def test_a_threaded_hole_tap_drill_stays_editable_through_a_custom_engagement():
    nx = modeling_nx()
    f, rows = threaded_hole(nx)
    ref = nx.ref_of(f, "feature")
    data = run(nx, "nx_get_feature", feature=ref).data
    by = {p["expression"]["name"]: p for p in data["parameters"]}
    assert by["p45"]["editable"] and by["p45"]["aliases"] == ["tap", "drill"]
    assert not by["p114"]["editable"] and by["p114"]["aliases"] == ["major"]
    first = run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "tap", "value": 5.2}])
    assert first.ok and rows["p45"].Value == 5.2 and rows["p107"].RightHandSide == '"Custom"'
    assert any("Custom" in w["message"] for w in first.warnings), first.warnings
    again = run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "tap", "value": 5.0}])
    assert again.ok and rows["p45"].Value == 5.0, again
    undo = compensate(nx)
    assert undo.ok and rows["p45"].RightHandSide == "5.2", undo


def test_thread_table_values_are_refused_with_the_reason_and_the_size_takes_plain_text():
    nx = modeling_nx()
    f, rows = threaded_hole(nx)
    ref = nx.ref_of(f, "feature")
    bad = run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "major", "value": 7}])
    assert bad.error["code"] == "NX_READ_ONLY" and bad.mutation_outcome == "not_started", bad
    assert bad.error["details"]["reason"] == "thread_table" and "size" in bad.error["hint"]
    size = run(nx, "nx_edit_feature", feature=ref,
               parameters=[{"name": "size", "value": "M8 x 1.25"}])  # fmt: skip
    assert size.ok and rows["p106"].RightHandSide == '"M8 x 1.25"', size


def test_a_new_thread_size_puts_a_custom_engagement_back_to_the_table_and_undo_restores_it():
    nx = modeling_nx()
    f, rows = threaded_hole(nx)
    ref = nx.ref_of(f, "feature")
    tap = run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "tap", "value": 5.1}])
    assert tap.ok and rows["p107"].RightHandSide == '"Custom"', tap
    size = run(nx, "nx_edit_feature", feature=ref,
               parameters=[{"name": "size", "value": "M8 x 1.25"}])  # fmt: skip
    assert size.ok and rows["p107"].RightHandSide == '"0.75"', size
    assert any("Custom -> " in w["message"] for w in size.warnings), size.warnings
    undo = compensate(nx)
    assert undo.ok, undo
    assert rows["p106"].RightHandSide == '"M6 x 1.0"' and rows["p107"].RightHandSide == '"Custom"'
    assert rows["p45"].RightHandSide == "5.1"


def test_a_new_thread_size_with_a_tap_drill_keeps_the_custom_engagement():
    nx = modeling_nx()
    f, rows = threaded_hole(nx)
    ref = nx.ref_of(f, "feature")
    run(nx, "nx_edit_feature", feature=ref, parameters=[{"name": "tap", "value": 5.1}])
    both = run(nx, "nx_edit_feature", feature=ref,
               parameters=[{"name": "size", "value": "M8 x 1.25"}, {"name": "tap", "value": 6.8}])  # fmt: skip
    assert both.ok and rows["p107"].RightHandSide == '"Custom"' and rows["p45"].Value == 6.8, both
