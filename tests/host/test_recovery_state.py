"""Undo history, checkpoints and references on the host across save boundaries and
part lifetimes (agent changes come from a development tool, which records history)."""

import pytest

from nx_mcp.core import kernel
from nx_mcp.core.errors import NXError
from tests.kit.objects import Body, Part

pytestmark = pytest.mark.fake_nx


def mutation(rig):
    def add(fail=False):
        rig.part.Bodies.append(Body())
        rig.part.IsModified = True
        if fail:
            raise RuntimeError("after mutation")
        return {}

    rig.e.dev_tools["nx_dev_python"] = add
    return lambda **p: rig.run("nx_dev_python", p)


@pytest.fixture
def call(rig, monkeypatch):
    """A catalogue call on the host (evidence gate off: these tools run untested here)."""
    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a: None)
    return lambda tool, **args: rig.run(tool, args)["data"]


def test_checkpoint_restore_invalidates_objects_and_preserves_receipt(rig, call):
    add = mutation(rig)
    cp = call("nx_create_checkpoint")
    add(operation_id="test-operation-001")
    ref = rig.ref(rig.part.Bodies[-1])
    assert call("nx_list_checkpoints")["undo_depth"] == 1
    assert call("nx_list_checkpoints")["undo_depth"] == 1  # reads keep the marks
    restored = call("nx_restore_checkpoint", checkpoint_id=cp["checkpoint_id"])
    assert restored["reverted"] == ["test-operation-001"]
    assert not rig.part.Bodies and not rig.part.IsModified
    with pytest.raises(NXError):
        rig.e.objects.resolve(ref)
    with pytest.raises(NXError, match="not in this session"):
        call("nx_restore_checkpoint", checkpoint_id=cp["checkpoint_id"])
    assert not rig.e.history and not rig.e.checkpoints


def test_undo_reverts_only_latest_operation_and_save_expires_marks(rig, call):
    add = mutation(rig)
    add(operation_id="first-operation")
    add(operation_id="second-operation")
    assert len(rig.part.Bodies) == 2
    assert call("nx_undo_operation")["undone_operation_id"] == "second-operation"
    assert len(rig.part.Bodies) == 1
    assert [h["operation_id"] for h in rig.e.history] == ["first-operation"]
    cp = call("nx_create_checkpoint")
    saved = call("nx_save_part")
    assert saved["saved_files"] == ["test.prt"]
    assert call("nx_list_checkpoints")["checkpoints"][0]["available"] is False
    with pytest.raises(NXError, match="save") as error:
        call("nx_restore_checkpoint", checkpoint_id=cp["checkpoint_id"])
    assert error.value.code == "NX_STALE_CHECKPOINT"
    with pytest.raises(NXError, match="No agent operation") as error:
        call("nx_undo_operation")
    assert error.value.code == "NX_NOT_FOUND"


def test_cross_part_undo_and_checkpoint_refuse_unrelated_changes(rig, call, tmp_path):
    add = mutation(rig)
    cp = call("nx_create_checkpoint")
    add()
    other = Part(rig.session, tmp_path / "other.prt")
    rig.e.own(other)  # the agent's own part: refused for crossing parts
    with pytest.raises(NXError) as error:
        call("nx_undo_operation")
    assert error.value.code == "NX_CROSS_PART"
    with pytest.raises(NXError) as error:
        call("nx_restore_checkpoint", checkpoint_id=cp["checkpoint_id"])
    assert error.value.code == "NX_CROSS_PART"
    assert len(rig.part.Bodies) == 1 and not other.Bodies


def test_set_name_renames_by_reference_and_refreshes_references(rig, call):
    body = rig.part.Bodies[0] if rig.part.Bodies else None
    add = mutation(rig)
    add()
    created = rig.ref(rig.part.Bodies[-1])
    renamed = call("nx_set_name", object=created, name="Renamed")
    assert renamed["name"] == "Renamed" and renamed["object"]["id"] != created
    with pytest.raises(NXError):
        rig.e.objects.resolve(created)
    assert body is None or body in rig.part.Bodies


def test_session_lifecycle_and_generation_reject_closed_references(rig, tmp_path, monkeypatch):
    monkeypatch.setattr(kernel, "_gate_evidence", lambda *a: None)  # untested catalogue tools
    part = rig.part
    body = Body()
    part.Bodies.append(body)
    old = rig.ref(body)
    p = rig.e.register(part, "part", part, "Part")["id"]
    part.Save()  # nx_open_part reuses a loaded part by its file on disk
    opened = rig.run("nx_open_part", {"path": part.FullPath})
    assert opened["data"]["already_loaded"]
    key = "cp_" + "0" * 32
    rig.e.checkpoints[key] = {"mark": 1, "part_id": rig.e.part_key(part), "index": 0}
    closed = rig.run("nx_close_part", {"part": p, "save": True})
    assert closed["data"]["closed_count"] == 1
    assert not rig.session.Parts and not rig.e.checkpoints
    with pytest.raises(NXError):
        rig.e.objects.resolve(old)
    rig.session.Parts.append(part)
    rig.session.Parts.Work = rig.session.Parts.Display = part
    assert rig.ref(body) != old
    path = tmp_path / "imported.prt"
    path.write_text("fixture")
    new_part = rig.run("nx_open_part", {"path": str(path), "work": False, "display": False})
    assert not new_part["data"]["already_loaded"]
    assert rig.run("nx_list_parts", {})["page"]["total"] == 2
    with pytest.raises(NXError):
        rig.run("nx_open_part", {"path": str(tmp_path / "missing.prt")})
    assert key not in rig.e.checkpoints


def test_snapshot_invalidation_preserves_topology_for_display_only(rig):
    b = Body()
    rig.part.Bodies.append(b)
    before = rig.e.snapshot(rig.part)
    face = rig.ref(b.faces[0], "face")
    rig.e.invalidate(before, before, False)
    assert rig.e.objects.resolve(face) is b.faces[0]
    rig.e.invalidate(before, {}, True)
    for ref in [face, next(iter(before.values()))["id"]]:
        with pytest.raises(NXError):
            rig.e.objects.resolve(ref)


def test_guard_failures_never_create_undo_marks(rig):
    with pytest.raises(NXError):
        rig.run("nx_list_objects", {"unexpected": 1})
    with pytest.raises(NXError, match="NX_UNKNOWN_TOOL|nx_no_such_tool"):
        rig.run("nx_no_such_tool", {})
    assert not rig.session.marks
