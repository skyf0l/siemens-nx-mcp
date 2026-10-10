"""nx_undo_operation and the checkpoint tools (domains/session/history.py) on the fake
kernel: only the agent's own history, receipts marked reverted, references invalidated,
checkpoints expiring with NX's marks, cross-part refusals."""

from types import SimpleNamespace as NS

import pytest

from tests.kit import fake_nx, run
from tests.kit.kernel import services
from tests.kit.objects import Feature


@pytest.fixture
def nx():
    nx = fake_nx()

    def add_to_delete_list(objects):
        for obj in objects:
            for name in ("Bodies", "Features"):
                values = getattr(nx.part, name)
                if obj in values:
                    values.remove(obj)

    nx.session.UpdateManager.AddToDeleteList = add_to_delete_list
    return nx


def features(nx, *names):
    out = []
    for name in names:
        f = nx._own(Feature(name), "feature")
        f.Suppressed = False
        nx.part.Features.append(f)
        out.append(f)
    return out


def delete(nx, feature, op):
    ok = run(nx, "nx_delete_objects", operation_id=op, objects=[feature.ref])
    assert ok.ok, ok
    return ok


def names(nx):
    return [f.Name for f in nx.part.Features]


def test_undo_reverts_only_the_agents_last_operation(nx):
    a, b, c = features(nx, "A(1)", "B(2)", "C(3)")
    delete(nx, a, "op_delete_a")
    delete(nx, b, "op_delete_b")
    kept = c.ref
    ok = run(nx, "nx_undo_operation", operation_id="op_undo_last")
    assert ok.ok and ok.mutation_outcome == "committed", ok
    references = ok.data.pop("references")
    redo = ok.data.pop("redo")
    assert ok.data == {
        "undone_operation_id": "op_delete_b",
        "tool": "nx_delete_objects",
        "undone": ["op_delete_b"],
        "undo_depth": 1,
    }
    # No NX redo: the undone call and its arguments, to repeat it.
    assert redo == [
        {
            "tool": "nx_delete_objects",
            "arguments": {"objects": [b.ref]},
            "operation_id": "op_delete_b",
            "complete": True,
        }
    ]
    assert names(nx) == ["B(2)", "C(3)"]  # B back, A still deleted
    # Only refs of objects that are gone are listed stale; the part is not invalidated.
    assert references == ok.changes["references"] in ("preserved", "listed")
    assert kept in nx.refs  # refs of objects the undo kept stay valid
    history = services(nx).history
    assert [h["operation_id"] for h in history] == ["op_delete_a"]  # the undo is not in it
    assert set(nx.session.marks) == {1, 2}  # the undo's own mark (3) is gone
    store = services(nx).store
    assert store.get("op_delete_b")["reverted_by"] == "op_undo_last"
    assert "reverted_by" not in store.get("op_delete_a")
    replay = delete(nx, b, "op_delete_b")  # a retry replays, never recreates
    assert replay.replayed and "reverted" in replay.warnings[-1]["message"]


def test_undo_never_reaches_past_the_agents_history(nx):
    (a,) = features(nx, "A(1)")
    refused = run(nx, "nx_undo_operation")
    assert not refused.ok and refused.error["code"] == "NX_NOT_FOUND"
    assert refused.mutation_outcome == "not_started"
    assert refused.error["retry"] == "never"  # no fix makes the same call succeed
    delete(nx, a, "op_delete_a")
    services(nx).history.clear()  # the engineer edited the model (interactive host)
    refused = run(nx, "nx_undo_operation")
    assert refused.error["code"] == "NX_NOT_FOUND" and names(nx) == []
    assert nx.session.marks  # nothing undone; the refused call left no mark of its own


def test_undo_refuses_after_save_and_across_parts(nx):
    a, b = features(nx, "A(1)", "B(2)")
    delete(nx, a, "op_delete_a")
    services(nx).history[-1]["part_id"] = "part_other"
    refused = run(nx, "nx_undo_operation")
    assert refused.error["code"] == "NX_CROSS_PART", refused
    services(nx).history[-1]["part_id"] = services(nx).part_key(nx.part)
    nx.session.marks.clear()  # an NX save drops every undo mark
    refused = run(nx, "nx_undo_operation")
    assert refused.error["code"] == "NX_NOT_FOUND" and names(nx) == ["B(2)"]


def test_checkpoint_restore_undoes_everything_since(nx):
    a, b, c = features(nx, "A(1)", "B(2)", "C(3)")
    delete(nx, a, "op_delete_a")
    cp = run(nx, "nx_create_checkpoint", label="before b and c")
    assert cp.ok and cp.mutation_outcome == "committed", cp
    assert cp.data["label"] == "before b and c" and cp.data["undo_depth"] == 1
    checkpoint = cp.data["checkpoint_id"]
    delete(nx, b, "op_delete_b")
    delete(nx, c, "op_delete_c")
    listed = run(nx, "nx_list_checkpoints").data
    assert listed["undo_depth"] == 3 and listed["last_operation"] == "op_delete_c"
    assert listed["checkpoints"] == [
        {
            "checkpoint_id": checkpoint,
            "label": "before b and c",
            "part_id": services(nx).part_key(nx.part),
            "available": True,
            "expired_reason": None,
            "operations_after": 2,
        }
    ]
    ok = run(nx, "nx_restore_checkpoint", operation_id="op_restore", checkpoint_id=checkpoint)
    assert ok.ok, ok
    assert ok.data["reverted"] == ["op_delete_b", "op_delete_c"]
    assert ok.changes["references"] in ("preserved", "listed")  # never the whole part
    assert names(nx) == ["B(2)", "C(3)"]
    store = services(nx).store
    assert store.get("op_delete_c")["reverted_by"] == checkpoint
    assert "reverted_by" not in store.get("op_delete_a")
    listed = run(nx, "nx_list_checkpoints").data
    assert listed["undo_depth"] == 1
    (row,) = listed["checkpoints"]  # a restored checkpoint stays available
    assert row["available"] is True and row["operations_after"] == 0
    (b2,) = [nx._own(f, "feature") for f in nx.part.Features if f.Name == "B(2)"]
    delete(nx, b2, "op_delete_b_again")
    again = run(nx, "nx_restore_checkpoint", operation_id="op_again", checkpoint_id=checkpoint)
    assert again.ok and again.data["reverted"] == ["op_delete_b_again"], again
    assert names(nx) == ["B(2)", "C(3)"]


def test_undo_drops_checkpoints_after_the_undone_operation(nx):
    a, b = features(nx, "A(1)", "B(2)")
    early = run(nx, "nx_create_checkpoint").data["checkpoint_id"]
    delete(nx, a, "op_delete_a")
    late = run(nx, "nx_create_checkpoint").data["checkpoint_id"]
    assert run(nx, "nx_undo_operation").ok
    rows = {r["checkpoint_id"]: r for r in run(nx, "nx_list_checkpoints").data["checkpoints"]}
    assert rows[early]["available"] is True and rows[early]["expired_reason"] is None
    assert rows[late]["available"] is False and rows[late]["expired_reason"] == "undone_past"
    refused = run(nx, "nx_restore_checkpoint", checkpoint_id=late)
    assert refused.error["code"] == "NX_STALE_CHECKPOINT", refused
    assert "past this checkpoint" in refused.error["message"]
    assert refused.mutation_outcome == "not_started"
    del b


def test_expired_foreign_and_cross_part_checkpoints_are_refused(nx):
    (a,) = features(nx, "A(1)")
    checkpoint = run(nx, "nx_create_checkpoint").data["checkpoint_id"]
    unknown = run(nx, "nx_restore_checkpoint", checkpoint_id="cp_" + "1" * 32)
    assert unknown.error["code"] == "NX_STALE_CHECKPOINT"
    assert unknown.mutation_outcome == "not_started"
    delete(nx, a, "op_delete_a")
    services(nx).history[-1]["part_id"] = "part_other"
    crossed = run(nx, "nx_restore_checkpoint", checkpoint_id=checkpoint)
    assert crossed.error["code"] == "NX_CROSS_PART" and names(nx) == []
    nx.part.Save()  # NX drops the marks
    listed = run(nx, "nx_list_checkpoints").data
    assert listed["checkpoints"][0]["available"] is False and listed["undo_depth"] == 0
    assert listed["checkpoints"][0]["expired_reason"] == "saved_or_closed"
    assert listed["last_operation"] is None
    expired = run(nx, "nx_restore_checkpoint", checkpoint_id=checkpoint)
    assert expired.error["code"] == "NX_STALE_CHECKPOINT"
    assert "save" in expired.error["message"]
    missing = run(nx, "nx_restore_checkpoint", checkpoint_id="no such checkpoint")
    assert missing.error["code"] == "NX_NOT_FOUND", missing
    assert missing.error["details"]["checkpoints"] == [
        {"checkpoint_id": checkpoint, "label": "checkpoint", "available": False}
    ]
    assert "nx_list_checkpoints" in missing.error["hint"]
    bad = run(nx, "nx_restore_checkpoint", checkpoint_id="x" * 81)
    assert bad.error["code"] == "NX_INVALID_ARGUMENT"


def test_host_history_is_the_list_the_panel_clears(tmp_path):
    """InteractiveHost._invalidate_references clears the services' history and
    checkpoints on engineer edits; the catalogue tools must read those very objects."""
    from nx_mcp.host.services import NXServices
    from nx_mcp.workspace import Workspace

    marks = {1}
    session = NS(DoesUndoMarkExist=lambda mark, _: mark in marks)
    host = NXServices(session, NS(), "test", Workspace(tmp_path))
    host.part_key = lambda part: "part_1"
    history, checkpoints = host.history, host.checkpoints
    assert host.undo_history() is history and host.undo_checkpoints() is checkpoints
    history.append({"mark": 1})
    host.record_history(2, object(), "op_dead", "nx_undo_operation")
    host.record_history(1, object(), "op_live", "nx_delete_objects")
    assert [h.get("operation_id") for h in history] == [None, "op_live"]
    host.engineer_changed()
    assert host.undo_history() is history and not history


def test_undo_takes_several_steps_and_bounds_them(nx):
    a, b, c = features(nx, "A(1)", "B(2)", "C(3)")
    for f, op in ((a, "op_del_a"), (b, "op_del_b"), (c, "op_del_c")):
        delete(nx, f, op)
    too_many = run(nx, "nx_undo_operation", steps=4)
    assert too_many.error["code"] == "NX_INVALID_ARGUMENT", too_many
    assert too_many.error["field"] == "/steps" and too_many.error["details"]["undo_depth"] == 3
    assert too_many.mutation_outcome == "not_started" and names(nx) == []
    services(nx).history[-2]["part_id"] = "part_other"
    crossed = run(nx, "nx_undo_operation", steps=2)
    assert crossed.error["code"] == "NX_CROSS_PART"
    assert crossed.error["details"]["operation_id"] == "op_del_b" and names(nx) == []
    services(nx).history[-2]["part_id"] = services(nx).part_key(nx.part)
    ok = run(nx, "nx_undo_operation", operation_id="op_undo_two", steps=2)
    assert ok.ok, ok
    assert (
        ok.data["undone"] == ["op_del_b", "op_del_c"]
        and ok.data["undone_operation_id"] == "op_del_c"
    )
    assert ok.data["undo_depth"] == 1 and names(nx) == ["B(2)", "C(3)"]
    assert services(nx).store.get("op_del_b")["reverted_by"] == "op_undo_two"
    assert [h["operation_id"] for h in services(nx).history] == ["op_del_a"]


def test_undo_depth_counts_only_operations_nx_can_still_undo(nx):
    a, b = features(nx, "A(1)", "B(2)")
    delete(nx, a, "op_del_a")
    nx.session.marks.clear()  # a save drops the marks of the earlier operations
    delete(nx, b, "op_del_b")
    cp = run(nx, "nx_create_checkpoint", label="after save").data
    assert cp["undo_depth"] == 1  # the same count nx_list_checkpoints reports
    assert run(nx, "nx_list_checkpoints").data["undo_depth"] == 1
    refused = run(nx, "nx_undo_operation", steps=2)
    assert refused.error["field"] == "/steps"


def test_restore_by_label_takes_the_newest_restorable_checkpoint(nx):
    a, b, c = features(nx, "A(1)", "B(2)", "C(3)")
    first = run(nx, "nx_create_checkpoint", label="Before Cuts").data["checkpoint_id"]
    delete(nx, a, "op_del_a")
    second = run(nx, "nx_create_checkpoint", label="before cuts").data["checkpoint_id"]
    delete(nx, b, "op_del_b")
    ok = run(nx, "nx_restore_checkpoint", checkpoint_id="  BEFORE   cuts ")
    assert ok.ok, ok
    assert ok.data["checkpoint_id"] == second and ok.data["label"] == "before cuts"
    assert ok.data["reverted"] == ["op_del_b"] and names(nx) == ["B(2)", "C(3)"]
    ok = run(nx, "nx_restore_checkpoint", checkpoint_id="before cuts")
    assert ok.data["checkpoint_id"] == second and ok.data["reverted"] == []
    ok = run(nx, "nx_restore_checkpoint", checkpoint_id=first)
    assert ok.ok and ok.data["reverted"] == ["op_del_a"] and len(names(nx)) == 3
    rows = {r["checkpoint_id"]: r for r in run(nx, "nx_list_checkpoints").data["checkpoints"]}
    assert rows[second]["expired_reason"] == "undone_past"
    assert rows[first]["available"] is True
    ok = run(nx, "nx_restore_checkpoint", checkpoint_id="before cuts")  # only first is live
    assert ok.ok and ok.data["checkpoint_id"] == first
    del c
