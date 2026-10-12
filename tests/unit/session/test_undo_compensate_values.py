"""The compensating undo v2 (domains/session/compensate.py): values set back for
nx_set_attributes, nx_set_expression and nx_set_name from the before-values in their
history entries, derived drawing-view changes tolerated on create-only operations, and
the refusal hints that offer mode compensate or a fix-forward edit (benchmark bench3p).
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.session import compensate
from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import Object
from tests.unit.parts_files.test_attributes import TYPES, Info, delete_attr, get_attrs, set_attr
from tests.unit.session.test_undo_multipart import _tools, block, counts, history, two  # noqa: F401


@pytest.fixture
def nx(monkeypatch):
    nx, a, b = two()
    nx.nx.NXObject = NS(AttributeType=TYPES, AttributeInformation=Info)
    nx.nx.Update = NS(Option=NS(Now=0))
    for name, fn in (("GetUserAttributes", get_attrs), ("SetUserAttribute", set_attr),
                     ("DeleteUserAttribute", delete_attr)):  # fmt: skip
        monkeypatch.setattr(Object, name, fn, raising=False)
    nx.a, nx.b = a, b
    return nx


def attributes(part) -> dict:
    return {i.Title: i.StringValue for i in get_attrs(part) if i is not None}


def set_attrs(nx, op, **values):
    done = run(nx, "nx_set_attributes", operation_id=op, part="a", attributes=values)
    assert done.ok, done
    return done


# ---------------------------------------------------------------------------
# Attributes


def test_set_attributes_is_undone_by_setting_the_previous_values_back(nx):
    set_attrs(nx, "op_a_attr_1", PART_NO="PC-000", REV="A")
    set_attrs(nx, "op_a_attr_2", PART_NO="PC-001")
    block(nx, "b", "op_b_block_1")  # another part's later work: NX's undo would revert it
    undo = run(nx, "nx_undo_operation", operation_id="op_a_undo_1", part="a")
    assert undo.ok and undo.data["compensated"] is True, undo
    assert undo.data["undone"] == ["op_a_attr_2"]
    assert undo.data["restored"] == ["PART_NO of a.prt back to 'PC-000'"]
    assert attributes(nx.a) == {"PART_NO": "PC-000", "REV": "A"} and counts(nx.b) == [(1, 1)]
    assert any("set back PART_NO" in w["message"] for w in undo.warnings), undo.warnings
    assert history(nx)[-1] == ("op_a_undo_1", True)  # never undone itself
    first = run(nx, "nx_undo_operation", part="a")  # the titles the first call created go
    assert first.ok and first.data["undone"] == ["op_a_attr_1"], first
    assert attributes(nx.a) == {}
    redo = run(nx, "nx_redo_operation", part="a", steps=2)
    assert redo.ok and attributes(nx.a) == {"PART_NO": "PC-001", "REV": "A"}, redo


def test_a_value_changed_since_is_never_overwritten(nx):
    set_attrs(nx, "op_a_attr_1", PART_NO="PC-000")
    set_attrs(nx, "op_a_attr_2", PART_NO="PC-001")
    block(nx, "b", "op_b_block_1")
    refused = run(nx, "nx_undo_operation", part="a", operation="op_a_attr_1", mode="compensate")
    assert refused.error["code"] == "NX_DEPENDENCY_ORDER", refused
    assert (
        "changed since" in refused.error["message"]
        and "nx_set_attributes" in (refused.error["hint"])
    )
    assert attributes(nx.a) == {"PART_NO": "PC-001"}


# ---------------------------------------------------------------------------
# Expressions and names


def test_set_expression_is_undone_by_its_previous_formula_or_by_deleting_a_new_one(nx):
    made = run(nx, "nx_set_expression", operation_id="op_a_expr_1", part="a",
               name="undo_test", formula="1")  # fmt: skip
    assert made.ok and made.data["created"], made
    assert run(nx, "nx_set_expression", operation_id="op_a_expr_2", part="a",
               name="undo_test", formula="2").ok  # fmt: skip
    block(nx, "b", "op_b_block_1")
    back = run(nx, "nx_undo_operation", part="a")
    assert back.ok and back.data["restored"] == ["undo_test back to 1"], back
    assert [str(e.RightHandSide) for e in nx.a.Expressions if e.Name == "undo_test"] == ["1"]
    gone = run(nx, "nx_undo_operation", part="a")
    assert gone.ok and gone.data["restored"] == [
        "expression undo_test deleted (the operation created it)"
    ], gone
    assert not [e for e in nx.a.Expressions if e.Name == "undo_test"]


def test_set_name_is_undone_by_its_previous_name(nx):
    made = block(nx, "a", "op_a_block_1")
    feature = made.data["feature"]["id"]
    old = next(f.Name for f in nx.a.Features)
    renamed = run(nx, "nx_set_name", operation_id="op_a_name_1", part="a",
                  object=feature, name="COVER")  # fmt: skip
    assert renamed.ok, renamed
    block(nx, "b", "op_b_block_1")
    undo = run(nx, "nx_undo_operation", part="a")
    assert undo.ok and undo.data["compensated"], undo
    assert next(f.Name for f in nx.a.Features) == old
    assert undo.data["restored"] == [f"the name of {feature} back to {old!r}"]


def test_a_name_an_unnamed_feature_got_is_removed_through_uf(nx):
    """NX 2406 leaves Feature.Name empty and ignores SetName(""): UF DeleteName clears it."""
    made = block(nx, "a", "op_a_block_1")
    feature = made.data["feature"]["id"]
    target = next(iter(nx.a.Features))
    target.Name = ""
    deleted = []

    def delete_name(tag):
        deleted.append(tag)
        target.Name = ""

    nx.uf.Obj = NS(DeleteName=delete_name)
    assert run(nx, "nx_set_name", part="a", object=feature, name="COVER").ok
    block(nx, "b", "op_b_block_1")
    undo = run(nx, "nx_undo_operation", part="a")
    assert undo.ok and undo.data["restored"] == [f"the name of {feature} removed (it had none)"]
    assert target.Name == "" and deleted == [target.Tag], undo


# ---------------------------------------------------------------------------
# Derived changes and the refusal hints


def test_drawing_views_nx_refreshed_do_not_block_a_create_only_undo():
    feature = {"id": "f:9", "kind": "feature"}
    entry = {"operation_id": "op_sphere", "method": "nx_create_primitive"}
    views = [{"id": "v:1", "kind": "drawing_view"}, {"id": "sh:2", "kind": "drawing_sheet"}]
    record = {"state": "committed", "envelope": {"changes": {"created": [feature],
                                                             "modified": views}}}  # fmt: skip
    assert compensate._creates(entry, record) == [feature]
    record["envelope"]["changes"]["modified"].append({"id": "b:3", "kind": "body"})
    with pytest.raises(compensate.Refusal, match="did more than create objects"):
        compensate._creates(entry, record)


def test_an_operation_behind_another_parts_close_is_offered_and_done_by_compensation(nx):
    set_attrs(nx, "op_a_attr_1", PART_NO="PC-001")
    services(nx).history.append(
        {"mark": None, "part_id": "part_x", "operation_id": "op_close", "method": "nx_close_part",
         "close": ["c.prt"]}
    )  # fmt: skip
    # The default undo compensates across another part's close, as it does across other
    # parts' later work (bench3t: the close refused, the later work compensated).
    done = run(nx, "nx_undo_operation", part="a", operation="op_a_attr_1")
    assert done.ok and done.data["compensated"] and attributes(nx.a) == {}, done
    assert any("1 later part load or close" in w["message"] for w in done.warnings), done
    assert not any("c.prt" in w["message"] for w in done.warnings)  # no other part named


def test_an_operation_behind_a_close_that_cannot_be_compensated_is_refused_with_the_hint(nx):
    from tests.kit.objects import Feature

    old = Feature("OLD")
    old.OwningPart, old.IsOccurrence, old.Suppressed = nx.a, False, False
    nx.a.Features.append(old)
    ref = nx.ref_of(old, "feature")
    assert run(nx, "nx_delete_objects", operation_id="op_a_delete_1", objects=[ref]).ok
    services(nx).history.append(
        {"mark": None, "part_id": "part_x", "operation_id": "op_close", "method": "nx_close_part",
         "close": ["c.prt"]}
    )  # fmt: skip
    refused = run(nx, "nx_undo_operation", part="a")
    # Another part's close does not end a.prt's history: it is in the way of NX's undo
    assert refused.error["code"] == "NX_CROSS_PART", refused
    assert refused.error["details"]["in_the_way"] == [
        {"operation_id": "op_close", "tool": "nx_close_part", "part": "c.prt", "barrier": "close"}
    ]
    assert "c.prt closed by op_close (nx_close_part)" in refused.error["message"]
    assert "cannot be compensated" in refused.error["message"]
    assert refused.error["hint"].startswith("Fix forward instead")


def test_an_operation_that_qualifies_for_nothing_gets_the_fix_forward_hint(nx):
    from tests.kit.objects import Feature

    old = Feature("OLD")
    old.OwningPart, old.IsOccurrence, old.Suppressed = nx.a, False, False
    nx.a.Features.append(old)
    ref = nx.ref_of(old, "feature")
    assert run(nx, "nx_delete_objects", operation_id="op_a_delete_1", objects=[ref]).ok
    block(nx, "b", "op_b_block_1")
    refused = run(nx, "nx_undo_operation", part="a")
    assert refused.error["code"] == "NX_CROSS_PART", refused
    assert refused.error["hint"].startswith("Fix forward instead")
    assert "nx_set_attributes" in refused.error["hint"]
