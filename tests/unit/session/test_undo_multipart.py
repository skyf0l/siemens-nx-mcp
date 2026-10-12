"""Undo, checkpoints, redo and previews per part (domains/session/history.py, compensate.py,
preview.py) on the fake kernel with two loaded parts.

NX keeps one linear undo list for the session (tests/kit/objects.Session: ``UndoToMark``
restores every part's objects). The undo of part A uses NX's undo only while nothing of
another part lies after A's undone operations; else it deletes what A's create-only
operations made (verified), or refuses with NX_CROSS_PART naming what is in the way. The
engineer's edits are a barrier no undo crosses.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pytest

from nx_mcp.core import catalogue
from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.tx import Tx
from nx_mcp.kit.result import Row
from tests.kit import fake_nx, run
from tests.kit.kernel import services
from tests.kit.objects import Body, Feature, Part


class Made(TypedDict):
    feature: Row


def nx_create_test_block(tx: Tx) -> Made:
    """Create a block feature with its body in the work part."""
    part = tx.part
    body = Body(f"BODY({len(part.Bodies) + 1})")
    feature = Feature(f"BLOCK({len(part.Features) + 1})", [body])
    feature.Suppressed = False
    for obj in (body, feature):
        obj.OwningPart, obj.IsOccurrence = part, False
    part.Bodies.append(body)
    part.Features.append(feature)
    return {"feature": tx.ref(feature, "feature")}


def nx_create_test_sketch(tx: Tx) -> Made:
    """Create a sketch and its feature in the work part."""
    from tests.kit.objects import Sketch

    part = tx.part
    sketch = Sketch(tx.session, "SKETCH")
    feature = Feature("SKETCH_FEATURE")
    for obj in (sketch, feature):
        obj.OwningPart, obj.IsOccurrence = part, False
    part.Sketches.append(sketch)
    part.Features.append(feature)
    return {"feature": tx.ref(feature, "feature")}


def nx_create_test_datum(tx: Tx) -> Made:
    """Create a datum feature (no body) in the work part."""
    part = tx.part
    feature = Feature(f"DATUM({len(part.Features) + 1})")
    feature.OwningPart, feature.IsOccurrence, feature.Suppressed = part, False, False
    part.Features.append(feature)
    return {"feature": tx.ref(feature, "feature")}


DATUM = make_spec(
    nx_create_test_datum, effect="model", label="Test datum", keywords=("a", "b", "c"),
    domain="modeling", requires=("work_part",), previewable=True,
)  # fmt: skip
BLOCK = make_spec(
    nx_create_test_block, effect="model", label="Test block", keywords=("a", "b", "c"),
    domain="modeling", requires=("work_part",),
)  # fmt: skip
SKETCH = make_spec(
    nx_create_test_sketch, effect="model", label="Test sketch", keywords=("a", "b", "c"),
    domain="modeling", requires=("work_part",),
)  # fmt: skip


@pytest.fixture(autouse=True)
def _tools(monkeypatch):
    catalogue.load()
    monkeypatch.setitem(catalogue._REGISTRY, BLOCK.name, BLOCK)
    monkeypatch.setitem(catalogue._REGISTRY, SKETCH.name, SKETCH)
    monkeypatch.setitem(catalogue._REGISTRY, DATUM.name, DATUM)


def two():
    """``work.prt`` (A) displayed and work, ``b.prt`` (B) loaded in the background."""
    nx = fake_nx()
    root = Path(services(nx).root)
    nx.part.FullPath = str(root / "a.prt")
    parts = nx.session.Parts
    work, display = parts.Work, parts.Display
    b = Part(nx.session, root / "b.prt")
    parts.Work, parts.Display = work, display
    return nx, nx.part, b


def block(nx, part, op):
    made = run(nx, "nx_create_test_block", operation_id=op, part=part)
    assert made.ok, made
    return made


def counts(*parts):
    return [(len(p.Features), len(p.Bodies)) for p in parts]


def history(nx):
    return [(h.get("operation_id"), h.get("compensation", False)) for h in services(nx).history]


# ---------------------------------------------------------------------------
# NX undo while nothing else is in the way


def test_the_parts_own_latest_operations_use_nx_undo_and_leave_the_other_part():
    nx, a, b = two()
    block(nx, "b", "op_b_block_1")
    block(nx, "a", "op_a_block_1")
    undo = run(nx, "nx_undo_operation", operation_id="op_a_undo_1", part="a")
    assert undo.ok and "compensated" not in undo.data, undo
    assert undo.data["undone"] == ["op_a_block_1"] and undo.data["undo_depth"] == 0
    assert counts(a, b) == [(0, 0), (1, 1)]
    assert history(nx) == [("op_b_block_1", False)]
    assert run(nx, "nx_list_checkpoints", part="b").data["undo_depth"] == 1


def test_without_part_two_loaded_parts_are_ambiguous_for_undo_and_redo():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    refused = run(nx, "nx_undo_operation")
    assert refused.error["code"] == "NX_AMBIGUOUS_PART" and counts(a) == [(1, 1)]
    listed = run(nx, "nx_list_checkpoints")  # a read of the work part, never ambiguous
    assert listed.ok and listed.data["undo_depth"] == 1 and listed.part["file"] == "a.prt"


# ---------------------------------------------------------------------------
# Interleaved: undo by deletion, or NX_CROSS_PART


def test_an_interleaved_create_only_operation_is_undone_by_deletion():
    nx, a, b = two()
    made = block(nx, "a", "op_a_block_1")
    block(nx, "b", "op_b_block_1")
    undo = run(nx, "nx_undo_operation", operation_id="op_a_undo_1", part="a")
    assert undo.ok and undo.data["compensated"] is True, undo
    assert undo.data["undone"] == ["op_a_block_1"] and undo.data["redo_depth"] == 1
    assert counts(a, b) == [(0, 0), (1, 1)]  # B untouched
    deleted = {r["kind"] for r in undo.changes["deleted"]}
    assert deleted == {"feature", "body"} and undo.changes["references"] == "listed"
    assert made.data["feature"]["id"] in undo.changes["stale_refs"]
    assert any("deleted what it created" in w["message"] for w in undo.warnings)
    assert "op_b_block_1 (nx_create_test_block in b.prt)" in undo.warnings[0]["message"]
    # The undo by deletion stays in the history and is never undone itself.
    assert history(nx) == [("op_b_block_1", False), ("op_a_undo_1", True)]
    store = services(nx).store
    assert store.get("op_a_block_1")["reverted_by"] == "op_a_undo_1"


def test_a_second_undo_never_brings_the_deleted_objects_back():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    block(nx, "b", "op_b_block_1")
    assert run(nx, "nx_undo_operation", part="a").data["compensated"]
    again = run(nx, "nx_undo_operation", part="a")
    assert again.error["code"] == "NX_NOT_FOUND" and counts(a, b) == [(0, 0), (1, 1)]
    # B's own undo would revert A's undo by deletion with NX's undo: B deletes instead.
    b_undo = run(nx, "nx_undo_operation", part="b")
    assert b_undo.ok and b_undo.data["compensated"] and counts(a, b) == [(0, 0), (0, 0)]
    assert "an undo by deletion" in b_undo.warnings[0]["message"]


def test_an_interleaved_operation_that_is_not_create_only_is_refused_naming_the_other_part():
    nx, a, b = two()
    old = Feature("OLD")
    old.OwningPart, old.IsOccurrence, old.Suppressed = a, False, False
    a.Features.append(old)
    ref = nx.ref_of(old, "feature")
    assert run(nx, "nx_delete_objects", operation_id="op_a_delete_1", objects=[ref]).ok
    block(nx, "b", "op_b_block_1")
    refused = run(nx, "nx_undo_operation", part="a")
    assert refused.error["code"] == "NX_CROSS_PART" and refused.mutation_outcome == "not_started"
    error = refused.error
    assert "op_b_block_1 (nx_create_test_block in b.prt)" in error["message"]
    assert "did more than create features (deleted" in error["message"]
    assert error["details"]["operation_id"] == "op_b_block_1"
    assert error["details"]["in_the_way"] == [
        {"operation_id": "op_b_block_1", "tool": "nx_create_test_block", "part": "b.prt"}
    ]
    assert counts(a, b) == [(0, 0), (1, 1)] and len(history(nx)) == 2


def test_steps_mixing_create_only_and_other_work_are_refused_atomically():
    nx, a, b = two()
    old = Feature("OLD")
    old.OwningPart, old.IsOccurrence, old.Suppressed = a, False, False
    a.Features.append(old)
    block(nx, "a", "op_a_block_1")
    ref = nx.ref_of(old, "feature")
    assert run(nx, "nx_delete_objects", operation_id="op_a_delete_1", objects=[ref]).ok
    block(nx, "b", "op_b_block_1")
    before = counts(a, b)
    refused = run(nx, "nx_undo_operation", part="a", steps=2)
    assert refused.error["code"] == "NX_CROSS_PART" and counts(a, b) == before
    assert len(history(nx)) == 3


def test_a_feature_another_feature_uses_is_never_deleted():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    block(nx, "b", "op_b_block_1")
    child = Feature("BLEND")
    child.OwningPart, child.IsOccurrence = a, False
    a.Features[0].children.append(child)  # e.g. a blend on the block's edges
    a.Features.append(child)
    nx.ref_of(child, "feature")
    refused = run(nx, "nx_undo_operation", part="a")
    assert refused.error["code"] == "NX_CROSS_PART"
    assert refused.error["details"]["dependents"][0]["name"] == "BLEND"
    explicit = run(nx, "nx_undo_operation", part="a", mode="delete_created")
    assert explicit.error["code"] == "NX_DEPENDENCY_ORDER" and len(a.Features) == 2


def test_a_sketch_is_never_undone_by_deletion():
    nx, a, b = two()
    assert run(nx, "nx_create_test_sketch", operation_id="op_a_sketch_1", part="a").ok
    block(nx, "b", "op_b_block_1")
    refused = run(nx, "nx_undo_operation", part="a")
    assert refused.error["code"] == "NX_CROSS_PART" and "created sketch" in refused.error["message"]
    assert len(a.Sketches) == 1


def test_a_deletion_that_takes_more_than_was_created_is_rolled_back():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    block(nx, "b", "op_b_block_1")
    bystander = Body("BYSTANDER")
    bystander.OwningPart = a
    a.Bodies.append(bystander)
    real = nx.session.UpdateManager.AddToDeleteList

    def greedy(objects):
        real(objects)
        a.Bodies.remove(bystander)  # NX took another object with them

    nx.session.UpdateManager.AddToDeleteList = greedy
    refused = run(nx, "nx_undo_operation", part="a")
    assert refused.error["code"] == "NX_DEPENDENCY_ORDER", refused
    assert refused.mutation_outcome == "rolled_back"
    assert counts(a, b) == [(1, 2), (1, 1)] and bystander in a.Bodies
    assert len(history(nx)) == 2  # nothing undone


def test_a_created_feature_that_cannot_be_found_is_refused_unless_a_later_call_deleted_it():
    nx, a, b = two()
    made = block(nx, "a", "op_a_block_1")
    block(nx, "b", "op_b_block_1")
    ref = made.data["feature"]["id"]
    obj = nx.refs.pop(ref)  # a stale ref, the block still there
    refused = run(nx, "nx_undo_operation", part="a")
    assert (
        refused.error["code"] == "NX_CROSS_PART" and "cannot be found" in refused.error["message"]
    )
    assert counts(a) == [(1, 1)]
    nx.refs[ref] = obj
    # Deleted by a later own call: the undo of the create alone reports it missing.
    assert run(nx, "nx_delete_objects", operation_id="op_a_delete_1", part="a", objects=[ref]).ok
    gone = run(nx, "nx_undo_operation", part="a", operation="op_a_block_1", mode="delete_created")
    assert gone.ok and ref in gone.data["missing"] and len(gone.data["missing"]) == 2, gone
    assert "Already deleted by a later operation" in str(gone.warnings)


# ---------------------------------------------------------------------------
# operation=, mode delete_created


def test_operation_names_its_part_and_refuses_while_later_own_work_follows():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    block(nx, "b", "op_b_block_1")
    block(nx, "a", "op_a_block_2")
    later = run(nx, "nx_undo_operation", operation="op_a_block_1")  # no part: the receipt's
    assert later.error["code"] == "NX_DEPENDENCY_ORDER", later
    assert later.error["details"]["later"] == ["op_a_block_2"] and later.part["file"] == "a.prt"
    only = run(nx, "nx_undo_operation", operation="op_a_block_1", mode="delete_created")
    assert only.ok and only.data["compensated"] and counts(a, b) == [(1, 1), (1, 1)]
    assert a.Features[0].Name == "BLOCK(2)"
    other = run(nx, "nx_undo_operation", part="a", operation="op_b_block_1")
    assert other.error["code"] == "NX_WRONG_PART_CONTEXT"  # the operation is B's
    unknown = run(nx, "nx_undo_operation", operation="op_nowhere_1")
    assert unknown.error["code"] == "NX_NOT_FOUND"  # ANY: never NX_AMBIGUOUS_PART
    both = run(nx, "nx_undo_operation", part="a", operation="op_a_block_2", steps=1)
    assert both.error["code"] == "NX_INVALID_ARGUMENT"


def test_delete_created_reaches_across_a_load_but_never_across_the_engineer():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    services(nx).history.append(
        {"mark": None, "part_id": "part_x", "operation_id": "op_load", "method": "nx_open_part",
         "load": ["c.prt"]}
    )  # fmt: skip
    ok = run(nx, "nx_undo_operation", part="a")  # the default compensates across the load
    assert ok.ok and ok.data["compensated"] and counts(a) == [(0, 0)], ok
    assert any("part_loaded" not in w["message"] and "c.prt was loaded" in w["message"]
               for w in ok.warnings), ok.warnings  # fmt: skip
    block(nx, "a", "op_a_block_2")
    services(nx).engineer_changed()
    for mode in (None, "delete_created"):
        args = {"mode": mode} if mode else {}
        no = run(nx, "nx_undo_operation", part="a", **args)
        assert no.error["code"] == "NX_NOT_FOUND" and no.error["details"]["reason"] == (
            "engineer_change"
        )
    assert counts(a) == [(1, 1)]


def test_the_default_undo_of_several_steps_compensates_across_a_close():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    services(nx).history.append(
        {"mark": None, "part_id": "part_x", "operation_id": "op_close", "method": "nx_close_part",
         "close": ["c.prt"]}
    )  # fmt: skip
    block(nx, "a", "op_a_block_2")  # NX's undo reaches this one only
    both = run(nx, "nx_undo_operation", part="a", steps=2)
    assert both.ok and both.data["compensated"], both
    assert both.data["undone"] == ["op_a_block_1", "op_a_block_2"] and counts(a) == [(0, 0)]
    too_many = run(nx, "nx_undo_operation", part="a", steps=1)
    assert too_many.error["code"] == "NX_NOT_FOUND", too_many


# ---------------------------------------------------------------------------
# The engineer barrier


def test_an_undo_never_reverts_the_engineers_edit_in_another_part():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    block(nx, "b", "op_b_block_1")
    nx.session.SetUndoMark(1, "engineer")  # NX marks the engineer's edit too
    pad = Feature("ENGINEER_PAD")
    pad.OwningPart = a
    a.Features.append(pad)
    services(nx).engineer_changed()  # the host saw the mark fingerprint change
    for part in ("a", "b"):
        refused = run(nx, "nx_undo_operation", part=part)
        assert refused.error["code"] == "NX_NOT_FOUND", refused
        assert refused.error["details"]["reason"] == "engineer_change"
    assert pad in a.Features and counts(b) == [(1, 1)]
    block(nx, "b", "op_b_block_2")  # new work after the edit can be undone
    assert run(nx, "nx_undo_operation", part="b").ok and pad in a.Features
    assert counts(b) == [(1, 1)]


def test_a_checkpoint_before_the_engineers_edit_cannot_be_restored():
    nx, a, b = two()
    cp = run(nx, "nx_create_checkpoint", part="a").data["checkpoint_id"]
    block(nx, "a", "op_a_block_1")
    services(nx).engineer_changed()
    refused = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)
    assert refused.error["code"] == "NX_STALE_CHECKPOINT" and counts(a) == [(1, 1)]


# ---------------------------------------------------------------------------
# Checkpoints per part


def test_checkpoints_list_and_restore_per_part():
    nx, a, b = two()
    cp_a = run(nx, "nx_create_checkpoint", part="a", label="a start").data["checkpoint_id"]
    run(nx, "nx_create_checkpoint", part="b", label="b start")
    block(nx, "a", "op_a_block_1")
    block(nx, "b", "op_b_block_1")
    block(nx, "a", "op_a_block_2")
    listed = run(nx, "nx_list_checkpoints", part="a").data
    assert [r["label"] for r in listed["checkpoints"]] == ["a start"]
    assert listed["checkpoints"][0]["operations_after"] == 2 and listed["undo_depth"] == 2
    assert listed["last_operation"] == "op_a_block_2"
    assert len(run(nx, "nx_list_checkpoints").data["checkpoints"]) == 2  # no part: all
    restored = run(nx, "nx_restore_checkpoint", checkpoint_id=cp_a)  # its own part, no part key
    assert restored.ok and restored.data["compensated"], restored
    assert restored.data["reverted"] == ["op_a_block_1", "op_a_block_2"]
    assert counts(a, b) == [(0, 0), (1, 1)] and restored.part["file"] == "a.prt"
    again = run(nx, "nx_list_checkpoints", part="a").data["checkpoints"][0]
    assert again["available"] and again["operations_after"] == 0


def test_a_restore_after_only_other_parts_worked_changes_nothing():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    cp = run(nx, "nx_create_checkpoint", part="a").data["checkpoint_id"]
    block(nx, "b", "op_b_block_1")
    marks = set(nx.session.marks)
    ok = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)
    assert ok.ok and ok.data["reverted"] == [] and counts(a, b) == [(1, 1), (1, 1)]
    assert set(nx.session.marks) == marks and len(history(nx)) == 2


def test_a_restore_uses_nx_undo_when_the_other_parts_work_came_before_the_parts_own():
    nx, a, b = two()
    cp = run(nx, "nx_create_checkpoint", part="a").data["checkpoint_id"]
    block(nx, "b", "op_b_block_1")
    block(nx, "a", "op_a_block_1")
    ok = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)
    assert ok.ok and "compensated" not in ok.data and counts(a, b) == [(0, 0), (1, 1)]


# ---------------------------------------------------------------------------
# Redo per part


def test_redo_lists_are_per_part_and_new_work_ends_only_its_own():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    block(nx, "b", "op_b_block_1")
    assert run(nx, "nx_undo_operation", part="b").ok
    assert run(nx, "nx_undo_operation", part="a").ok
    assert counts(a, b) == [(0, 0), (0, 0)]
    ambiguous = run(nx, "nx_redo_operation")  # two parts have calls to redo
    assert ambiguous.error["code"] == "NX_AMBIGUOUS_PART"
    block(nx, "b", "op_b_block_2")  # new work in B ends B's redo list, not A's
    redo = run(nx, "nx_redo_operation")  # only A has calls to redo: its part
    assert redo.ok and redo.data["redone"] == ["op_a_block_1"] and redo.part["file"] == "a.prt"
    assert counts(a, b) == [(1, 1), (1, 1)] and redo.data["redo_depth"] == 0
    nothing = run(nx, "nx_redo_operation", part="b")
    assert nothing.error["code"] == "NX_NOT_FOUND" and "b.prt" in nothing.error["message"]


def test_redo_after_an_undo_by_deletion_lands_in_its_part_while_another_is_displayed():
    nx, a, b = two()
    parts = nx.session.Parts
    parts.Work = parts.Display = b  # the engineer looks at B
    block(nx, "a", "op_a_block_1")
    block(nx, "b", "op_b_block_1")
    assert run(nx, "nx_undo_operation", part="a").data["compensated"]
    redo = run(nx, "nx_redo_operation")
    assert redo.ok and redo.part["file"] == "a.prt" and counts(a, b) == [(1, 1), (1, 1)]
    assert parts.Work is b and parts.Display is b


# ---------------------------------------------------------------------------
# Previews


def _preview(nx, part):
    step = {"tool": "nx_create_test_datum", "arguments": {}}
    made = run(nx, "nx_preview_changes", part=part, operations=[step])
    assert made.ok, made
    return made.data["preview_id"]


def test_a_preview_is_accepted_in_its_part_after_other_parts_worked():
    nx, a, b = two()
    pid = _preview(nx, "a")
    block(nx, "b", "op_b_block_1")
    viewed = run(nx, "nx_set_highlight", part="a", objects=[])  # a view call: no epoch
    assert viewed.ok, viewed
    accepted = run(nx, "nx_finish_preview", preview_id=pid, action="accept")
    assert accepted.ok and accepted.part["file"] == "a.prt", accepted
    assert not accepted.warnings and counts(a, b) == [(1, 0), (1, 1)]
    again = run(nx, "nx_finish_preview", preview_id=pid, action="accept")
    assert (
        again.error["code"] == "NX_STALE_PREVIEW" and "already accepted" in again.error["message"]
    )


def test_a_refused_accept_keeps_the_preview_and_a_closed_part_preview_says_so():
    nx, a, b = two()
    pid = _preview(nx, "a")
    wrong = run(nx, "nx_finish_preview", preview_id=pid, action="accept", part="b")
    assert wrong.error["code"] == "NX_WRONG_PART_CONTEXT" and counts(a) == [(0, 0)]
    assert run(nx, "nx_create_test_datum", part="a").ok  # the part changed: stale, kept
    stale = run(nx, "nx_finish_preview", preview_id=pid, action="accept")
    assert stale.error["code"] == "NX_STALE_PREVIEW" and stale.mutation_outcome == "not_started"
    assert run(nx, "nx_undo_operation", part="a").ok  # the change undone: acceptable again
    ok = run(nx, "nx_finish_preview", preview_id=pid, action="accept")
    assert ok.ok and counts(a) == [(1, 0)], ok
    pid = _preview(nx, "b")
    b_key = services(nx).part_key(b)
    nx.session.Parts.remove(b)
    services(nx).forget_parts([b_key])
    closed = run(nx, "nx_finish_preview", preview_id=pid, action="accept")
    assert closed.error["code"] == "NX_STALE_PREVIEW", closed
    assert "its part was closed" in closed.error["message"]
    dropped = run(nx, "nx_finish_preview", preview_id=pid, action="discard")
    assert dropped.ok and dropped.data["state"] == "already_finished"
