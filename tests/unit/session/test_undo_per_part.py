"""Undo history and checkpoints scoped per part (domains/session/history.py,
core/marks.py) with several agents' parts in one NX session.

Another part's save, close or load never ends a part's history or expires its
checkpoints: NX's undo cannot reach over them, so the undo or restore compensates (it
deletes what the part's create-only operations made); the part's own save does end them.
A call whose own object (operation, checkpoint, redo items) is unknown never defaults to
the engineer's part: with two or more agent parts it is NX_AMBIGUOUS_PART.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.kit import run
from tests.kit.kernel import services
from tests.kit.parts import with_parts
from tests.unit.session.test_undo_multipart import _tools, block, counts, two  # noqa: F401


@pytest.fixture
def nx():
    nx, a, b = two()
    with_parts(nx, services(nx).root)  # nx_save_part / nx_close_part seams
    nx.a, nx.b = a, b
    return nx


def history(nx):
    return [(h.get("operation_id"), h.get("expired"), h.get("expired_by")) for h in
            services(nx).history]  # fmt: skip


def test_another_parts_save_ends_neither_the_history_nor_the_checkpoints(nx):
    block(nx, "a", "op_a_block_1")
    cp = run(nx, "nx_create_checkpoint", part="a", label="start").data["checkpoint_id"]
    block(nx, "a", "op_a_block_2")
    block(nx, "b", "op_b_block_1")
    saved = run(nx, "nx_save_part", part="b")  # NX drops every mark of the session
    assert saved.ok, saved
    b_key = services(nx).part_key(nx.b)
    assert all(e == ["saved_or_closed", b_key] for _op, *e in history(nx)), history(nx)
    listed = run(nx, "nx_list_checkpoints", part="a").data
    (row,) = listed["checkpoints"]
    assert row["available"] is True and row["compensated"] is True, row
    assert row["operations_after"] == 1 and listed["undo_depth"] == 2
    undo = run(nx, "nx_undo_operation", part="a")  # the default undo compensates
    assert undo.ok and undo.data["compensated"] and undo.data["undone"] == ["op_a_block_2"]
    message = undo.warnings[0]["message"]
    assert "another part's save or close dropped its undo mark" in message, message
    assert "b.prt" not in message and counts(nx.a, nx.b) == [(1, 1), (1, 1)]
    redo = run(nx, "nx_redo_operation", part="a")
    assert redo.ok and counts(nx.a) == [(2, 2)], redo
    restored = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)  # the redo has its mark
    assert restored.ok and "compensated" not in restored.data, restored  # NX's undo
    assert restored.data["reverted"] == ["op_a_block_2"] and counts(nx.a, nx.b) == [
        (1, 1),
        (1, 1),
    ]
    again = run(nx, "nx_list_checkpoints", part="a").data["checkpoints"][0]
    assert again["available"] and again["operations_after"] == 0


def test_the_parts_own_save_ends_its_history_and_checkpoints(nx):
    block(nx, "a", "op_a_block_1")
    cp = run(nx, "nx_create_checkpoint", part="a", label="start").data["checkpoint_id"]
    block(nx, "a", "op_a_block_2")
    block(nx, "b", "op_b_block_1")
    assert run(nx, "nx_save_part", part="a").ok
    refused = run(nx, "nx_undo_operation", part="a")
    assert refused.error["code"] == "NX_NOT_FOUND", refused
    assert refused.error["details"]["reason"] == "saved_or_closed"
    assert "mode compensate" in refused.error["hint"]
    stale = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)
    assert stale.error["code"] == "NX_STALE_CHECKPOINT" and "saved" in stale.error["message"]
    # B's history is not A's: A's save is another part's save for B
    b_undo = run(nx, "nx_undo_operation", part="b")
    assert b_undo.ok and b_undo.data["compensated"] and counts(nx.b) == [(0, 0)], b_undo
    forced = run(nx, "nx_undo_operation", part="a", mode="compensate")  # past its own save
    assert forced.ok and forced.data["undone"] == ["op_a_block_2"], forced
    assert "a save or a part close" in forced.warnings[0]["message"]


def test_the_parts_own_save_after_another_parts_save_still_ends_its_checkpoints(nx):
    # Live NX: B's save had dropped every mark, so A's own save dropped none and A's
    # checkpoint stayed restorable (by compensation) past A's own save.
    block(nx, "a", "op_a_block_1")
    cp = run(nx, "nx_create_checkpoint", part="a", label="start").data["checkpoint_id"]
    block(nx, "a", "op_a_block_2")
    block(nx, "b", "op_b_block_1")
    assert run(nx, "nx_save_part", part="b").ok
    assert run(nx, "nx_save_part", part="a").ok
    a_key = services(nx).part_key(nx.a)
    row = run(nx, "nx_list_checkpoints", part="a").data["checkpoints"][0]
    assert row["available"] is False and row["expired_reason"] == "saved_or_closed", row
    stale = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)
    assert stale.error["code"] == "NX_STALE_CHECKPOINT" and stale.mutation_outcome == "not_started"
    assert ("op_a_block_2", "saved_or_closed", a_key) in history(nx)
    b_undo = run(nx, "nx_undo_operation", part="b")  # B's own save still ends B's history
    assert b_undo.error["details"]["reason"] == "saved_or_closed", b_undo


def test_another_parts_close_keeps_the_checkpoint_and_the_restore_compensates(nx):
    block(nx, "a", "op_a_block_1")
    cp = run(nx, "nx_create_checkpoint", part="a", label="before").data["checkpoint_id"]
    block(nx, "a", "op_a_block_2")
    closed = run(nx, "nx_close_part", part="b", save=False, force=True)
    assert closed.ok and closed.data["undo_marks"] == "kept", closed
    assert services(nx).history[-1]["close"] == ["b.prt"]
    block(nx, "a", "op_a_block_3")
    row = run(nx, "nx_list_checkpoints", part="a").data["checkpoints"][0]
    assert row["available"] and row["compensated"] and row["operations_after"] == 2, row
    restored = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)
    assert restored.ok and restored.data["compensated"], restored
    assert restored.data["reverted"] == ["op_a_block_2", "op_a_block_3"]
    assert counts(nx.a) == [(1, 1)] and len(nx.session.Parts) == 1  # b never came back


def test_unknown_objects_never_default_to_the_engineers_part(nx):
    """The engineer's part is the work part (the engineer tab guard); the agent has two
    parts loaded: an unknown operation, checkpoint or redo is NX_AMBIGUOUS_PART."""
    engineer = nx.part
    services(nx).foreign.add(services(nx).part_key(engineer))
    from tests.kit.objects import Part

    c = Part(nx.session, Path(nx.a.FullPath).with_name("c.prt"))
    nx.session.Parts.Work = nx.session.Parts.Display = engineer
    for tool, args in (
        ("nx_undo_operation", {"operation": "op_nowhere_1"}),
        ("nx_undo_operation", {}),
        ("nx_restore_checkpoint", {"checkpoint_id": "no such checkpoint"}),
        ("nx_restore_checkpoint", {"checkpoint_id": "cp_" + "1" * 32}),
        ("nx_redo_operation", {}),
    ):
        refused = run(nx, tool, **args)
        assert refused.error["code"] == "NX_AMBIGUOUS_PART", (tool, refused)
        assert refused.mutation_outcome == "not_started"
    # one agent part (b.prt; a.prt is the engineer's here): the call reports there
    nx.session.Parts.remove(c)
    unknown = run(nx, "nx_undo_operation", operation="op_nowhere_1")
    assert unknown.error["code"] == "NX_NOT_FOUND" and unknown.part["file"] == "b.prt", unknown


def test_a_label_names_the_target_parts_checkpoint(nx):
    cp_a = run(nx, "nx_create_checkpoint", part="a", label="start").data["checkpoint_id"]
    cp_b = run(nx, "nx_create_checkpoint", part="b", label="start").data["checkpoint_id"]
    block(nx, "a", "op_a_block_1")
    block(nx, "b", "op_b_block_1")
    ambiguous = run(nx, "nx_restore_checkpoint", checkpoint_id="start")
    assert ambiguous.error["code"] == "NX_AMBIGUOUS_PART", ambiguous
    in_b = run(nx, "nx_restore_checkpoint", checkpoint_id="start", part="b")
    assert (
        in_b.ok
        and in_b.data["checkpoint_id"] == cp_b
        and counts(nx.a, nx.b)
        == [
            (1, 1),
            (0, 0),
        ]
    )
    by_id = run(nx, "nx_restore_checkpoint", checkpoint_id=cp_a)  # its part, no part key
    assert by_id.ok and by_id.part["file"] == "a.prt" and counts(nx.a) == [(0, 0)]
    twice = run(nx, "nx_create_checkpoint", part="a", label="start")
    assert any("already has a checkpoint labelled" in w["message"] for w in twice.warnings)
    none_here = run(nx, "nx_restore_checkpoint", checkpoint_id="other", part="a")
    assert none_here.error["code"] == "NX_NOT_FOUND"
