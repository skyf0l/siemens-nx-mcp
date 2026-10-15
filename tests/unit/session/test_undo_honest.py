"""Undo, checkpoints and redo in a session with other parts' work (domains/session/history.py,
compensate.py, core/undoable.py): what compensation can and cannot undo, and saying so.

- a created feature that consumed edges or changed a body is undone by deleting it;
- a batch or an accepted preview is undone child by child (values set back, creations
  deleted);
- what an operation created in another part it named (``tx.touch_part``) is deleted there;
- nx_list_checkpoints says whether a restore works now (``restorable``, ``blocked_by``) and
  the call that breaks a checkpoint says so;
- refusals summarise the work in the way and say ``retry`` never;
- redo remaps refs, keeps a failed step redoable, can skip, and repeats an accepted
  preview's calls.
"""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Annotated, TypedDict

import pytest

from nx_mcp.core import catalogue, undoable
from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Ref
from nx_mcp.kit.result import Row
from tests.kit import kernel as kit_kernel
from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import Body, Expression, Feature, NXException, Object
from tests.unit.parts_files.test_attributes import TYPES, Info, delete_attr, get_attrs, set_attr
from tests.unit.session.test_undo_multipart import _tools, block, counts, history, two  # noqa: F401

FLAKY = {"fail": False}
TARGET: dict = {}


class Made(TypedDict):
    feature: Row


def nx_create_test_boss(tx: Tx, *, body: Annotated[Ref, Doc("Body to grow")]) -> Made:
    """Add a boss feature that changes an existing body (as a blend or a unite does)."""
    target = tx.resolve(body, "body")
    if FLAKY["fail"]:
        fail("NX_BUILD_FAILED", "The boss failed")
    part = tx.part
    feature = Feature(f"BOSS({len(part.Features) + 1})")
    feature.OwningPart, feature.IsOccurrence, feature.Suppressed = part, False, False
    part.Features.append(feature)
    tx.modified(target, "body")
    return {"feature": tx.ref(feature, "feature")}


def nx_create_test_link(tx: Tx) -> Made:
    """Create a linked feature in another part (TARGET["part"]), as a WAVE link does."""
    other = TARGET["part"]
    tx.touch_part(other)
    feature = Feature(f"LINKED({len(other.Features) + 1})")
    feature.OwningPart, feature.IsOccurrence, feature.Suppressed = other, False, False
    other.Features.append(feature)
    return {"feature": tx.ref(feature, "feature")}


BOSS = make_spec(
    nx_create_test_boss, effect="model", label="Test boss", keywords=("a", "b", "c"),
    example={"body": "b:1"},
    domain="modeling", requires=("work_part",),
)  # fmt: skip
LINK = make_spec(
    nx_create_test_link, effect="model", label="Test link", keywords=("a", "b", "c"),
    domain="modeling", requires=("work_part",),
)  # fmt: skip


@pytest.fixture(autouse=True)
def _more_tools(monkeypatch):
    catalogue.load()
    monkeypatch.setitem(catalogue._REGISTRY, BOSS.name, BOSS)
    monkeypatch.setitem(catalogue._REGISTRY, LINK.name, LINK)
    FLAKY["fail"] = False
    TARGET.clear()


@pytest.fixture
def values(monkeypatch):
    """Two parts with attribute and expression support (as test_undo_compensate_values)."""
    nx, a, b = two()
    nx.nx.NXObject = NS(AttributeType=TYPES, AttributeInformation=Info)
    nx.nx.Update = NS(Option=NS(Now=0))
    for name, fn in (("GetUserAttributes", get_attrs), ("SetUserAttribute", set_attr),
                     ("DeleteUserAttribute", delete_attr)):  # fmt: skip
        monkeypatch.setattr(Object, name, fn, raising=False)
    nx.a, nx.b = a, b
    return nx


def body_of(nx, part):
    return nx.ref_of(part.Bodies[0], "body")


def delete_old(nx, part, op):
    old = Feature("OLD")
    old.OwningPart, old.IsOccurrence, old.Suppressed = part, False, False
    part.Features.append(old)
    done = run(nx, "nx_delete_objects", operation_id=op, part="a", objects=[nx.ref_of(old)])
    assert done.ok, done
    return done


# ---------------------------------------------------------------------------
# What compensation undoes


def test_a_created_feature_that_changed_a_body_or_consumed_edges_is_undone_by_deleting_it():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    boss = run(nx, "nx_create_test_boss", operation_id="op_a_boss_1", part="a",
               body=body_of(nx, a))  # fmt: skip
    assert boss.ok and boss.changes["modified"][0]["kind"] == "body", boss
    block(nx, "b", "op_b_block_1")
    undo = run(nx, "nx_undo_operation", part="a")
    assert undo.ok and undo.data["compensated"] and undo.data["undone"] == ["op_a_boss_1"], undo
    assert counts(a, b) == [(1, 1), (1, 1)]  # the block and its body stay
    # consumed faces and edges (stale refs) do not block either
    unit = undoable.Unit("op", "nx_create_blend", {
        "created": [{"id": "f:9", "kind": "feature"}], "stale_refs": ["e:3", "fc:4", "b:5"],
    })  # fmt: skip
    assert undoable.creates(unit) == [{"id": "f:9", "kind": "feature"}]
    unit.changes["stale_refs"].append("o:6")
    with pytest.raises(undoable.Refusal, match="stale_refs"):
        undoable.creates(unit)


class Gone(Expression):
    """A feature's expression whose wrapper raises once NX deleted it (as NXOpen does)."""

    part = None

    @property
    def OwningPart(self):  # noqa: N802  # NXOpen name
        if self not in self.part.Expressions:
            raise NXException(3520016, "Invalid self parameter passed., Object no longer exists")
        return self.part


def nx_create_test_round(tx: Tx) -> Made:
    """Create a feature with its body and its radius expression in the work part."""
    part = tx.part
    body = Body(f"BODY({len(part.Bodies) + 1})")
    feature = Feature(f"ROUND({len(part.Features) + 1})", [body])
    radius = Gone(f"round_radius_{len(part.Expressions) + 1}", "2")
    radius.part = part
    feature.expressions = [radius]
    for obj in (body, feature, radius):
        obj.IsOccurrence = False
    body.OwningPart = feature.OwningPart = part
    feature.Suppressed = False
    part.Bodies.append(body)
    part.Features.append(feature)
    part.Expressions.append(radius)
    return {"feature": tx.ref(feature, "feature")}


ROUND = make_spec(
    nx_create_test_round, effect="model", label="Test round", keywords=("a", "b", "c"),
    domain="modeling", requires=("work_part",),
)  # fmt: skip


def test_a_created_features_expressions_are_never_read_after_deleting_it(monkeypatch):
    monkeypatch.setitem(catalogue._REGISTRY, ROUND.name, ROUND)
    # the fake kernel's snapshot lists expressions too, as the host's does
    monkeypatch.setattr(kit_kernel, "KINDS", (*kit_kernel.KINDS, ("Expressions", "expression")))
    nx, a, b = two()
    made = run(nx, "nx_create_test_round", operation_id="op_a_round_1", part="a")
    assert made.ok and {"expression"} <= {r["kind"] for r in made.changes["created"]}, made
    block(nx, "b", "op_b_block_1")
    undo = run(nx, "nx_undo_operation", part="a")
    assert undo.ok and undo.data["compensated"], undo
    assert undo.data["undone"] == ["op_a_round_1"] and not a.Expressions
    assert counts(a, b) == [(0, 0), (1, 1)]


def test_what_an_operation_created_in_another_part_it_named_is_deleted_there():
    nx, a, b = two()
    TARGET["part"] = b
    made = run(nx, "nx_create_test_link", operation_id="op_a_link_1", part="a")
    assert made.ok and counts(a, b) == [(0, 0), (1, 0)], made
    assert services(nx).history[-1]["parts"] == [services(nx).part_key(b)]
    block(nx, "b", "op_b_block_1")
    undo = run(nx, "nx_undo_operation", part="a", operation="op_a_link_1")
    assert undo.ok and undo.data["compensated"], undo
    assert counts(a, b) == [(0, 0), (1, 1)]  # the link went, b's own block stayed
    assert made.data["feature"]["id"] in undo.changes["stale_refs"]
    wrong = run(nx, "nx_undo_operation", part="b", operation="op_a_block_x")
    assert wrong.error["code"] == "NX_NOT_FOUND"


def test_an_accepted_preview_of_value_changes_is_undone_by_setting_them_back(values):
    nx = values
    assert run(nx, "nx_set_expression", part="a", name="p2", formula="20").ok
    preview = run(nx, "nx_preview_changes", part="a", operations=[
        {"tool": "nx_set_expression", "arguments": {"name": "p2", "formula": "30"}},
    ])  # fmt: skip
    assert preview.ok, preview
    # NX's undo of the preview sets the formula back (the fake keeps values)
    next(e for e in nx.a.Expressions if e.Name == "p2").RightHandSide = "20"
    accept = run(nx, "nx_finish_preview", operation_id="op_a_accept_1",
                 preview_id=preview.data["preview_id"], action="accept")  # fmt: skip
    assert accept.ok, accept
    entry = services(nx).history[-1]
    assert entry["child_restores"][0]["tool"] == "nx_set_expression"
    assert entry["redo_calls"][0]["arguments"]["formula"] == "30"
    block(nx, "b", "op_b_block_1")
    undo = run(nx, "nx_undo_operation", part="a")
    assert undo.ok and undo.data["restored"] == ["p2 back to 20"], undo
    assert [e.RightHandSide for e in nx.a.Expressions if e.Name == "p2"] == ["20"]


def test_a_batch_of_renames_is_undone_child_by_child(values):
    nx = values
    made = block(nx, "a", "op_a_block_1")
    feature = made.data["feature"]["id"]
    old = next(f.Name for f in nx.a.Features)
    batch = run(nx, "nx_run_batch", operation_id="op_a_batch_1", part="a", operations=[
        {"tool": "nx_set_name", "arguments": {"object": feature, "name": "COVER"}},
    ])  # fmt: skip
    assert batch.ok, batch
    block(nx, "b", "op_b_block_1")
    undo = run(nx, "nx_undo_operation", part="a")
    assert undo.ok and undo.data["undone"] == ["op_a_batch_1"], undo
    assert next(f.Name for f in nx.a.Features) == old


# ---------------------------------------------------------------------------
# Saying what cannot be undone


def test_checkpoints_say_whether_a_restore_works_now_and_the_call_that_breaks_one_warns():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    cp = run(nx, "nx_create_checkpoint", part="a", label="before delete").data["checkpoint_id"]
    delete_old(nx, a, "op_a_delete_1")
    row = run(nx, "nx_list_checkpoints", part="a").data["checkpoints"][0]
    assert row["restorable"] is True and "blocked_by" not in row  # NX's undo still reaches
    block(nx, "b", "op_b_block_1")
    listed = run(nx, "nx_list_checkpoints", part="a").data
    (row,) = listed["checkpoints"]
    assert row["checkpoint_id"] == cp and row["part"] == "a.prt"
    assert row["available"] is True and row["compensated"] is True and row["restorable"] is False
    assert row["blocked_by"]["operation_id"] == "op_a_delete_1"
    assert "deleted" in row["blocked_by"]["reason"]
    assert listed["undo_depth"] == 0 and listed["last_operation"] is None
    assert listed["undo_blocked_by"]["operation_id"] == "op_a_delete_1"
    nxt = block(nx, "a", "op_a_block_2")  # the part's next call learns it
    assert any("can no longer be restored" in w["message"] for w in nxt.warnings), nxt.warnings
    again = block(nx, "a", "op_a_block_3")
    assert not any("can no longer be restored" in w["message"] for w in again.warnings)
    refused = run(nx, "nx_restore_checkpoint", part="a", checkpoint_id=cp)
    assert refused.error["code"] == "NX_CROSS_PART" and refused.error["retry"] == "never"


def test_the_fragile_note_comes_with_the_operation_when_the_kernel_reports_its_changes():
    nx, a, b = two()
    box = services(nx)
    box.undo_checkpoints()["cp_x"] = {
        "mark": box.set_mark("cp"), "part_id": box.part_key(a), "index": 0, "label": "start"}  # fmt: skip
    box.history.append(
        {"mark": box.set_mark("op"), "part_id": box.part_key(a), "operation_id": "op_now",
         "method": "nx_delete_objects"}
    )  # fmt: skip
    warnings: list = []

    class Spec:
        effect, name = "model", "nx_delete_objects"

    class Call:
        spec, operation_id, target = Spec, "op_now", None
        changes = {"deleted": [{"id": "f:1", "kind": "feature"}]}

        def warn(self, code, message):
            warnings.append((code, message))

    undoable.checkpoint_notes(box, Call())
    assert warnings and "only while no other part changes" in warnings[0][1], warnings
    undoable.checkpoint_notes(box, Call())
    assert len(warnings) == 1  # once per checkpoint


def test_cross_part_refusals_are_summarised_and_never_retried():
    nx, a, b = two()
    delete_old(nx, a, "op_a_delete_1")
    for n in range(5):
        block(nx, "b", f"op_b_block_{n}")
    refused = run(nx, "nx_undo_operation", part="a")
    error = refused.error
    assert error["code"] == "NX_CROSS_PART" and error["retry"] == "never", error
    assert "5 later operations in 1 other part" in error["message"]
    assert "op_b_block_3" not in error["message"] and error["details"]["in_the_way_count"] == 5
    assert error["hint"].startswith("Fix forward instead: create again what it deleted")


def test_too_many_steps_counts_the_parts_history_without_blaming_the_engineer():
    nx, a, b = two()
    services(nx).history.append(
        {"mark": None, "part_id": None, "operation_id": None, "method": "engineer",
         "engineer": True}
    )  # fmt: skip
    block(nx, "a", "op_a_block_1")
    block(nx, "a", "op_a_block_2")
    refused = run(nx, "nx_undo_operation", part="a", steps=50)
    error = refused.error
    assert error["code"] == "NX_INVALID_ARGUMENT" and error["field"] == "/steps", error
    assert "the 2 operation(s) of a.prt in its undo history" in error["message"]
    assert "engineer" not in error["message"] and "reason" not in error["details"]


def test_a_closed_parts_checkpoint_expires_and_a_label_error_is_plain():
    nx, a, b = two()
    cp = run(nx, "nx_create_checkpoint", part="b", label="b start").data["checkpoint_id"]
    nx.session.Parts.remove(b)  # closed outside the host's own close path
    rows = run(nx, "nx_list_checkpoints", part="a", include_expired=True).data
    hidden = run(nx, "nx_list_checkpoints").data
    assert hidden["checkpoints"] == [] and hidden["hidden_expired"] == 1
    del rows
    services(nx).history.clear()
    refused = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)
    assert refused.error["code"] == "NX_STALE_CHECKPOINT", refused
    assert refused.error["retry"] == "never" and "was closed" in refused.error["message"]
    bad = run(nx, "nx_create_checkpoint", part="a", label="")
    assert bad.error["field"] == "/label" and "1-80 characters" in bad.error["message"]
    assert "^[" not in bad.error["message"]


def test_a_restore_with_nothing_to_undo_changes_nothing():
    nx, a, b = two()
    cp = run(nx, "nx_create_checkpoint", part="a", label="now").data["checkpoint_id"]
    block(nx, "b", "op_b_block_1")
    done = run(nx, "nx_restore_checkpoint", part="a", checkpoint_id=cp)
    assert done.ok and done.data["reverted"] == [] and done.mutation_outcome == "not_applicable"


# ---------------------------------------------------------------------------
# Redo


def test_redo_remaps_the_refs_an_earlier_redone_step_makes_again():
    nx, a, b = two()
    made = block(nx, "a", "op_a_block_1")
    old_body = body_of(nx, a)
    assert run(nx, "nx_create_test_boss", operation_id="op_a_boss_1", part="a",
               body=old_body).ok  # fmt: skip
    assert run(nx, "nx_undo_operation", part="a", steps=2).ok
    redo = run(nx, "nx_redo_operation", part="a", steps=2)
    assert redo.ok, redo
    first, second = redo.data["results"]
    assert first["run_as"] == "op_a_block_1"  # unchanged arguments: its own id
    assert second["run_as"] == "op_a_boss_1-redo1"
    assert redo.data["remapped"][old_body] == body_of(nx, a) != old_body
    assert counts(a) == [(2, 1)]
    store = services(nx).store
    assert store.get("op_a_boss_1")["redone_as"] == "op_a_boss_1-redo1"
    retry = run(nx, "nx_create_test_boss", operation_id="op_a_boss_1", part="a", body=old_body)
    assert retry.ok and retry.replayed and counts(a) == [(2, 1)], retry  # never made twice
    del made


def test_a_failed_redo_step_stays_redoable_and_skip_passes_over_it():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    assert run(nx, "nx_create_test_boss", operation_id="op_a_boss_1", part="a",
               body=body_of(nx, a)).ok  # fmt: skip
    block(nx, "a", "op_a_block_2")
    assert run(nx, "nx_undo_operation", part="a", steps=2).ok  # boss and block 2
    FLAKY["fail"] = True
    failed = run(nx, "nx_redo_operation", part="a")
    assert failed.error["code"] == "NX_BUILD_FAILED" and failed.mutation_outcome == "rolled_back"
    record = services(nx).store.get("op_a_boss_1")
    assert record["state"] == "committed" and record["reverted_by"], record
    assert record["last_redo_error"]["code"] == "NX_BUILD_FAILED"
    FLAKY["fail"] = False
    again = run(nx, "nx_redo_operation", part="a")
    assert again.ok and again.data["redone"] == ["op_a_boss_1"], again
    assert run(nx, "nx_undo_operation", part="a").ok
    skipped = run(nx, "nx_redo_operation", part="a", skip=1)
    assert skipped.ok and skipped.data["skipped"] == ["op_a_boss_1"], skipped
    assert skipped.data["redone"] == ["op_a_block_2"] and skipped.data["redo_depth"] == 0


def test_redo_refuses_up_front_a_ref_no_earlier_step_makes_again():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    assert run(nx, "nx_create_test_boss", operation_id="op_a_boss_1", part="a",
               body=body_of(nx, a)).ok  # fmt: skip
    assert run(nx, "nx_undo_operation", part="a", steps=2).ok
    assert run(nx, "nx_redo_operation", part="a", skip=1, steps=0).ok  # drop the block
    refused = run(nx, "nx_redo_operation", part="a")
    assert refused.error["code"] == "NX_STALE_REFERENCE", refused
    assert refused.mutation_outcome == "not_started" and refused.error["details"]["redoable"] == 0


def test_redo_of_an_undone_accept_runs_the_previewed_calls_again():
    nx, a, b = two()
    preview = run(nx, "nx_preview_changes", part="a",
                  operations=[{"tool": "nx_create_test_datum", "arguments": {}}])  # fmt: skip
    assert preview.ok, preview
    accept = run(nx, "nx_finish_preview", operation_id="op_a_accept_1",
                 preview_id=preview.data["preview_id"], action="accept")  # fmt: skip
    assert accept.ok and counts(a) == [(1, 0)], accept
    undo = run(nx, "nx_undo_operation", part="a")
    assert undo.ok and undo.data["redo"][0]["tool"] == "nx_create_test_datum", undo
    redo = run(nx, "nx_redo_operation", part="a")
    assert redo.ok and counts(a) == [(1, 0)], redo
    assert redo.data["results"][0]["run_as"] == "op_a_accept_1-redo1"
    assert history(nx)[-1] == ("op_a_accept_1-redo1", False)
