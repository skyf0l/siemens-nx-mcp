"""Recovery invariants across native failures, save boundaries and part lifetimes."""

from unittest.mock import Mock

import pytest

from nx_mcp.core import kernel
from nx_mcp.host.dispatch import services_for
from nx_mcp.runtime import NXToolError
from tests.fakes import Body, Part

pytestmark = pytest.mark.fake_nx


def mutation(rig):
    def add(fail=False):
        rig.part.Bodies.append(Body())
        rig.part.IsModified = True
        if fail:
            raise RuntimeError("after mutation")
        return {}

    rig.e._handlers["nx_test_add"] = add
    return lambda **p: rig.e.execute("nx_test_add", p)


@pytest.fixture
def call(rig, monkeypatch):
    """A catalogue call on the host (evidence gate off: these tools run untested here)."""
    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a: None)
    return lambda tool, **args: rig.e.execute(tool, args)["data"]


def test_checkpoint_restore_invalidates_objects_and_preserves_receipt(rig, call):
    add = mutation(rig)
    cp = call("nx_create_checkpoint")
    committed = add(operation_id="test-operation-001")
    ref = committed["changes"]["created"][0]["id"]
    assert call("nx_list_checkpoints")["undo_depth"] == 1
    assert call("nx_list_checkpoints")["undo_depth"] == 1  # reads keep the marks
    restored = call("nx_restore_checkpoint", checkpoint_id=cp["checkpoint_id"])
    assert restored["reverted"] == ["test-operation-001"]
    assert not rig.part.Bodies and not rig.part.IsModified
    with pytest.raises(NXToolError):
        rig.e.objects.resolve(ref)
    replay = add(operation_id="test-operation-001")
    assert replay["replayed"] and "reverted" in replay["warnings"][0]
    assert not rig.part.Bodies
    with pytest.raises(NXToolError, match="not in this session"):
        call("nx_restore_checkpoint", checkpoint_id=cp["checkpoint_id"])
    assert not rig.e._history and not rig.e._checkpoints


def test_undo_reverts_only_latest_operation_and_save_expires_marks(rig, call):
    add = mutation(rig)
    add(operation_id="first-operation")
    add(operation_id="second-operation")
    assert len(rig.part.Bodies) == 2
    assert call("nx_undo_operation")["undone_operation_id"] == "second-operation"
    assert len(rig.part.Bodies) == 1
    assert [h["operation_id"] for h in rig.e._history] == ["first-operation"]
    cp = call("nx_create_checkpoint")
    saved = call("nx_save_part")
    assert saved["saved_files"] == ["test.prt"]
    assert call("nx_list_checkpoints")["checkpoints"][0]["available"] is False
    with pytest.raises(NXToolError, match="save") as error:
        call("nx_restore_checkpoint", checkpoint_id=cp["checkpoint_id"])
    assert error.value.code == "NX_STALE_CHECKPOINT"
    with pytest.raises(NXToolError, match="No agent operation") as error:
        call("nx_undo_operation")
    assert error.value.code == "NX_NOT_FOUND"


def test_cross_part_undo_and_checkpoint_refuse_unrelated_changes(rig, call, tmp_path):
    add = mutation(rig)
    cp = call("nx_create_checkpoint")
    add()
    other = Part(rig.session, tmp_path / "other.prt")
    services_for(rig.e).own(other)  # the agent's own part: refused for crossing parts
    with pytest.raises(NXToolError) as error:
        call("nx_undo_operation")
    assert error.value.code == "NX_CROSS_PART"
    with pytest.raises(NXToolError) as error:
        call("nx_restore_checkpoint", checkpoint_id=cp["checkpoint_id"])
    assert error.value.code == "NX_CROSS_PART"
    assert len(rig.part.Bodies) == 1 and not other.Bodies


def test_set_name_renames_by_reference_and_refreshes_references(rig, call):
    body = rig.part.Bodies[0] if rig.part.Bodies else None
    add = mutation(rig)
    created = add()["changes"]["created"][0]["id"]
    renamed = call("nx_set_name", object=created, name="Renamed")
    assert renamed["name"] == "Renamed" and renamed["object"]["id"] != created
    with pytest.raises(NXToolError):
        rig.e.objects.resolve(created)
    assert body is None or body in rig.part.Bodies


def test_failed_update_rolls_back_and_rejects_retry(rig):
    add = mutation(rig)
    with pytest.raises(NXToolError) as error:
        add(fail=True, operation_id="failed-operation")
    assert error.value.details["mutation_outcome"] == "rolled_back"
    assert not rig.part.Bodies and not rig.part.IsModified
    with pytest.raises(NXToolError) as replay:
        add(fail=True, operation_id="failed-operation")
    assert replay.value.code == "NX_OPERATION_FAILED"
    assert rig.e._current_operation is None


def test_rollback_failure_is_partial_and_never_replayed_as_success(rig):
    add = mutation(rig)
    rig.session.UndoToMark = Mock(side_effect=RuntimeError("lost mark"))
    with pytest.raises(NXToolError) as error:
        add(fail=True, operation_id="partial-operation")
    assert error.value.code == "NX_ROLLBACK_FAILED"
    assert error.value.details["mutation_outcome"] == "partial"
    assert rig.e.store.get("partial-operation")["mutation_outcome"] == "partial"
    assert len(rig.part.Bodies) == 1


def test_receipt_write_failure_does_not_undo_committed_geometry(rig, monkeypatch):
    add = mutation(rig)
    put = rig.e.store.put

    def persist(record):
        if record["state"] == "committed":
            raise OSError("disk full")
        put(record)

    monkeypatch.setattr(rig.e.store, "put", persist)
    with pytest.raises(OSError):
        add(operation_id="durable-operation")
    assert len(rig.part.Bodies) == 1
    assert rig.e.store.get("durable-operation")["state"] == "running"
    rig.e.store.recover("next-session")
    assert rig.e.store.get("durable-operation")["state"] == "unknown"
    with pytest.raises(NXToolError) as error:
        add(operation_id="durable-operation")
    assert error.value.code == "NX_OPERATION_UNKNOWN"


def test_prior_session_receipt_warns_and_cannot_duplicate(rig):
    add = mutation(rig)
    add(operation_id="previous-session")
    r = rig.e.store.get("previous-session")
    r["session_id"] = "old"
    rig.e.store.put(r)
    result = add(operation_id="previous-session")
    assert any("earlier NX session" in w for w in result["warnings"])
    assert len(rig.part.Bodies) == 1


def test_session_lifecycle_and_generation_reject_closed_references(rig, tmp_path, monkeypatch):
    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a: None)  # untested catalogue tools
    part = rig.part
    body = Body()
    part.Bodies.append(body)
    old = rig.ref(body)
    p = rig.e._reference(part, "part", part, "Part")["id"]
    part.Save()  # nx_open_part reuses a loaded part by its file on disk
    opened = rig.e.execute("nx_open_part", {"path": part.FullPath})
    assert opened["data"]["already_loaded"]
    assert rig.e._activate_part(part.Name, False, False)["part"]["id"] == p
    key = "cp_" + "0" * 32
    rig.e._checkpoints[key] = {"mark": 1, "part_id": rig.e._part_id(part), "index": 0}
    closed = rig.e.execute("nx_close_part", {"part": p, "save": True})
    assert closed["data"]["closed_count"] == 1
    assert not rig.session.Parts and not rig.e._checkpoints
    with pytest.raises(NXToolError):
        rig.e.objects.resolve(old)
    rig.session.Parts.append(part)
    rig.session.Parts.Work = rig.session.Parts.Display = part
    assert rig.ref(body) != old
    path = tmp_path / "imported.prt"
    path.write_text("fixture")
    new_part = rig.e.execute("nx_open_part", {"path": str(path), "work": False, "display": False})
    assert not new_part["data"]["already_loaded"]
    assert rig.e.execute("nx_list_parts", {})["page"]["total"] == 2
    with pytest.raises(NXToolError):
        rig.e.execute("nx_open_part", {"path": str(tmp_path / "missing.prt")})
    with pytest.raises(NXToolError):
        rig.e._activate_part("missing")
    assert key not in rig.e._checkpoints


def test_snapshot_invalidation_preserves_topology_for_display_only(rig):
    b = Body()
    rig.part.Bodies.append(b)
    before = rig.e._snapshot(rig.part)
    face = rig.ref(b.faces[0], "face")
    rig.e._invalidate_deleted(before, before, False)
    assert rig.e.objects.resolve(face) is b.faces[0]
    rig.e._invalidate_deleted(before, {})
    for ref in [face, next(iter(before.values()))["id"]]:
        with pytest.raises(NXToolError):
            rig.e.objects.resolve(ref)


def test_lookup_ambiguity_ownership_and_kind_guards(rig, tmp_path):
    first = Body("A")
    second = Body("a")
    rig.part.Bodies.extend([first, second])
    ref = rig.ref(first)
    assert rig.e._resolve(first.JournalIdentifier, {"body"}) is first
    with pytest.raises(NXToolError) as error:
        rig.e._resolve("A")
    assert error.value.code == "NX_AMBIGUOUS_REFERENCE"
    with pytest.raises(NXToolError):
        rig.e._resolve(ref, {"feature"})
    with pytest.raises(NXToolError):
        rig.e._resolve("obj_missing")
    with pytest.raises(NXToolError):
        rig.e._resolve("missing")
    Part(rig.session, tmp_path / "other.prt")
    with pytest.raises(NXToolError):
        rig.e._resolve(ref)


def test_guard_failures_never_create_undo_marks(rig):
    with pytest.raises(NXToolError):
        rig.e.execute("nx_list_topology", {"unexpected": 1})
    with pytest.raises(NXToolError, match="NX_TOOL_NOT_FOUND|nx_no_such_tool"):
        rig.e.execute("nx_no_such_tool", {})
    assert not rig.session.marks


def test_capability_filters_and_unit_conventions(rig):
    from types import SimpleNamespace

    rig.session.DexManager = SimpleNamespace()
    rig.session.Measurement = SimpleNamespace()
    one = rig.e._capabilities(tool="nx_list_topology")
    assert list(one["tools"]) == ["nx_list_topology"] and one["units"] is None
    group = rig.e._capabilities(prefix="nx_sheet")
    assert all(name.startswith("nx_sheet") for name in group["tools"])
    assert group["tool_count"] < group["total_tool_count"]
    for params in [{"tool": "nx_missing"}, {"tool": "nx_list_topology", "prefix": "nx_"}]:
        with pytest.raises(NXToolError):
            rig.e._capabilities(**params)


def test_rejected_call_keeps_references_unless_something_was_created(rig):
    body = rig.ref(rig.part.Bodies[0], "body") if rig.part.Bodies else None
    rig.e._handlers["nx_test_reject"] = lambda: (_ for _ in ()).throw(
        NXToolError("NX_INVALID_ARGUMENT", "rejected before any NX work")
    )
    with pytest.raises(NXToolError) as error:
        rig.e.execute("nx_test_reject", {})
    assert error.value.details["mutation_outcome"] == "rolled_back"
    assert error.value.details["references"] == "preserved"
    if body:
        rig.e._resolve(body["id"], {"body"})  # Still valid.

    def create_then_fail():
        rig.part.Features.append(type("F", (), {"Tag": 987654, "Name": "NEW(9)"})())
        raise RuntimeError("native commit failed after creating a feature")

    rig.e._handlers["nx_test_create_fail"] = create_then_fail
    with pytest.raises(NXToolError) as error:
        rig.e.execute("nx_test_create_fail", {})
    assert error.value.details["references"] == "invalidated"
