"""Undo and checkpoints across part loads (core/marks.py, domains/session/history.py).

Loading a part keeps NX's undo marks (it is created or opened without display and
shown with the display switch that keeps them), but undoing across the load would
unload the part: the load is a barrier. Marks NX did drop say why (a save or close, a
display reset, eviction past NX's cap), never a save or close that did not happen.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from nx_mcp.domains.parts_files import parts as part_tools
from tests.kit import fake_nx, run
from tests.kit.kernel import services
from tests.kit.objects import Feature
from tests.kit.parts import with_parts


@pytest.fixture
def nx():
    nx = fake_nx()
    with_parts(nx, services(nx).root)

    def add_to_delete_list(objects):
        for obj in objects:
            for name in ("Bodies", "Features"):
                values = getattr(nx.part, name)
                if obj in values:
                    values.remove(obj)

    nx.session.UpdateManager.AddToDeleteList = add_to_delete_list
    return nx


def feature(nx, name):
    f = nx._own(Feature(name), "feature")
    f.Suppressed = False
    nx.part.Features.append(f)
    return f


def delete(nx, name, op):
    ok = run(nx, "nx_delete_objects", operation_id=op, objects=[feature(nx, name).ref])
    assert ok.ok, ok
    return ok


def names(nx):
    return [f.Name for f in nx.part.Features]


def checkpoints(nx):
    listed = run(nx, "nx_list_checkpoints", include_expired=True).data["checkpoints"]
    return {r["label"]: r for r in listed}


def test_create_without_display_keeps_the_work_part_display_part_and_marks(nx):
    delete(nx, "A(1)", "op_delete_a1")
    marks = set(nx.session.marks)
    ok = run(nx, "nx_create_part", path="agents/b/b.prt", display=False)
    assert ok.ok and ok.data["work"] is False and ok.data["display"] is False, ok
    assert nx.session.Parts.Work is nx.part and nx.session.Parts.Display is nx.part
    assert set(nx.session.marks) >= marks and nx.file_news[-1].MakeDisplayedPart is False
    assert nx.session.shown == []  # no display switch at all


def test_a_shown_new_part_keeps_the_marks_and_its_load_ends_no_other_parts_history(nx):
    """Display true: the load is a barrier NX's undo never crosses, but it ends neither
    the other part's history nor its checkpoints (per part)."""
    work = nx.part
    delete(nx, "A(1)", "op_delete_a1")
    cp = run(nx, "nx_create_checkpoint", label="before b").data["checkpoint_id"]
    ok = run(nx, "nx_create_part", path="b.prt", display=True)
    assert ok.ok and ok.data["work"] and ok.data["display"]
    assert nx.session.shown == [("retain", nx.session.Parts.Work)]
    history = services(nx).history
    assert all(nx.session.DoesUndoMarkExist(h["mark"]) for h in history if h.get("mark"))
    assert history[-1]["load"] == ["b.prt"] and history[-1]["method"] == "nx_create_part"
    row = checkpoints(nx)["before b"]
    assert row["available"] is True and row["expired_reason"] is None, row
    refused = run(nx, "nx_undo_operation", part="b")
    assert refused.error["code"] == "NX_NOT_FOUND" and refused.mutation_outcome == "not_started"
    assert refused.error["details"] == {"undo_depth": 0}
    assert "no operation of b.prt is in the undo history" in refused.error["message"]
    restore = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)  # nothing of work.prt since
    assert restore.ok and restore.data["reverted"] == [] and restore.part["file"] == "work.prt"
    assert len(nx.session.Parts) == 2 and work in nx.session.Parts  # b stays loaded
    crossed = run(nx, "nx_undo_operation", part="work")  # a delete behind b's load
    assert crossed.error["code"] == "NX_CROSS_PART", crossed
    assert crossed.error["details"]["in_the_way"][0]["barrier"] == "load"
    assert "b.prt loaded by" in crossed.error["message"] and len(nx.session.Parts) == 2


def test_undo_in_a_part_still_works_after_other_parts_load_and_display_switches(nx, tmp_path):
    a = nx.part
    services(nx).own(a)
    saved = nx.new_part("c.prt")  # a saved part to open later
    saved.Save()
    saved.CloseRetainUndoMarks()
    nx.session.Parts.SetActiveDisplayRetainUndoMarks(a, 0, 0)
    delete(nx, "A(1)", "op_delete_a1")
    cp = run(nx, "nx_create_checkpoint", label="a only").data["checkpoint_id"]
    assert run(nx, "nx_create_part", path="b.prt", display=False).ok
    assert run(nx, "nx_open_part", path="c.prt", work=False, display=False).ok
    delete(nx, "A(2)", "op_delete_a2")
    delete(nx, "A(3)", "op_delete_a3")
    b = run(nx, "nx_list_parts", path_prefix="b.prt").data["items"][0]["part"]["id"]
    for target in (b, nx.ref_of(a, "part")):  # B shown for the first time, then A again
        assert run(nx, "nx_activate_part", part=target).ok
    assert run(nx, "nx_list_checkpoints").data["undo_depth"] == 2
    ok = run(nx, "nx_undo_operation", part="work", steps=2)
    assert ok.ok and ok.data["undone"] == ["op_delete_a2", "op_delete_a3"], ok
    assert names(nx) == ["A(2)"] and ok.data["undo_depth"] == 0  # A(3) came after the mark
    more = run(nx, "nx_undo_operation", part="work")  # a delete behind the loads
    assert more.error["code"] == "NX_CROSS_PART" and "c.prt" in more.error["message"]
    assert len(nx.session.Parts) == 3  # a, b and c: nothing was unloaded
    row = checkpoints(nx)["a only"]  # per part: the loads of b and c do not end it
    assert row["available"] is True and row["operations_after"] == 0, row
    restored = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)
    assert restored.ok and restored.data["reverted"] == [] and len(nx.session.Parts) == 3


def test_an_undo_that_unloads_a_part_forgets_it_and_says_so(nx):
    """A model call that loaded a part (a component from a file, an import into a new
    part): undoing it unloads that part again (NX 2406, live)."""
    delete(nx, "A(1)", "op_delete_a1")
    loaded = nx.new_part("comp.prt")  # as if op_delete_a1 had loaded it
    nx.session.Parts.Display = nx.session.Parts.Work = nx.part
    ok = run(nx, "nx_undo_operation", part="work")
    assert ok.ok and loaded not in nx.session.Parts, ok
    assert [w["message"] for w in ok.warnings] == [
        "The undo also unloaded comp.prt (loaded by the undone operations); its refs are stale"
    ]
    assert services(nx).part_key(loaded) in nx.forgotten


def test_steps_past_a_load_say_where_the_history_ends(nx):
    delete(nx, "A(1)", "op_delete_a1")
    assert run(nx, "nx_create_part", path="b.prt", display=False).ok
    delete(nx, "A(2)", "op_delete_a2")
    too_far = run(nx, "nx_undo_operation", part="work", steps=2)
    assert too_far.error["code"] == "NX_CROSS_PART", too_far  # the load is in the way
    assert too_far.error["details"]["in_the_way"][0]["part"] == "b.prt"
    assert "b.prt loaded by" in too_far.error["message"]
    assert run(nx, "nx_list_checkpoints", part="work").data["undo_depth"] == 1
    beyond = run(nx, "nx_undo_operation", part="work", steps=3)
    assert beyond.error["code"] == "NX_INVALID_ARGUMENT"
    assert beyond.error["details"] == {"undo_depth": 1}


def test_no_barrier_without_anything_to_protect(nx):
    assert run(nx, "nx_create_part", path="b.prt").ok
    assert run(nx, "nx_create_part", path="c.prt", display=False).ok
    assert services(nx).history == []


def test_a_save_in_a_call_expires_marks_as_saved_or_closed(nx):
    run(nx, "nx_create_part", path="s.prt", display=True)
    owned = nx.session.Parts.Work
    for name in ("S(1)", "S(2)"):
        f = Feature(name)
        f.OwningPart, f.IsOccurrence, f.Suppressed = owned, False, False
        owned.Features.append(f)
    nx.session.UpdateManager.AddToDeleteList = lambda objs: [
        owned.Features.remove(o) for o in objs if o in owned.Features
    ]
    first = owned.Features[0]
    assert run(nx, "nx_delete_objects", objects=[nx.ref_of(first, "feature")]).ok
    run(nx, "nx_create_checkpoint", part="s", label="s")
    saved = run(nx, "nx_save_part", part=nx.ref_of(owned, "part"))
    assert saved.ok and not any(w["message"].startswith("NX dropped every") for w in saved.warnings)
    assert checkpoints(nx)["s"]["expired_reason"] == "saved_or_closed"
    refused = run(nx, "nx_undo_operation", part="s")
    assert refused.error["details"]["reason"] == "saved_or_closed"
    assert "a save or a part close" in refused.error["message"]


def test_marks_nx_evicts_are_reported_and_expire_as_mark_evicted(nx):
    nx.session.mark_cap = 4
    delete(nx, "A(1)", "op_delete_a1")
    run(nx, "nx_create_checkpoint", label="early")
    warned = []
    for i in range(2, 6):
        warned += delete(nx, f"A({i})", f"op_delete_a{i}").warnings
    notes = [w for w in warned if "180 undo marks" in w["message"]]
    assert notes and notes[0]["code"] == "NX_NOTE"
    early = checkpoints(nx)["early"]  # an evicted mark does not end it
    assert early["available"] is True and early["compensated"] is False, early
    assert services(nx).checkpoints[early["checkpoint_id"]]["expired"] == "mark_evicted"
    depth = run(nx, "nx_list_checkpoints").data["undo_depth"]
    assert 0 < depth < 5
    too_far = run(nx, "nx_undo_operation", steps=depth + 1)
    assert too_far.error["details"]["reason"] == "mark_evicted"
    # the refused undo's own mark evicted op_delete_a2's: deletes are not compensated
    restore = run(nx, "nx_restore_checkpoint", checkpoint_id="early")
    assert restore.error["code"] == "NX_NOT_FOUND", restore
    assert restore.error["details"]["reason"] == "mark_evicted"
    assert restore.mutation_outcome == "not_started"


def test_a_load_that_drops_every_mark_says_part_loaded(nx, monkeypatch):
    """A load that still displays (any future path that forgets ``show``) is named."""
    delete(nx, "A(1)", "op_delete_a1")
    run(nx, "nx_create_checkpoint", label="cp")
    real = part_tools.new_part

    def displaying(tx, target, units, *, display=True):
        part = real(tx, target, units, display=display)
        nx.session.marks.clear()
        return part

    monkeypatch.setattr(part_tools, "new_part", displaying)
    ok = run(nx, "nx_create_part", path="b.prt")
    assert ok.ok and any("NX dropped every undo mark" in w["message"] for w in ok.warnings)
    row = checkpoints(nx)["cp"]
    kept = services(nx).checkpoints[row["checkpoint_id"]]
    assert kept["expired"] == "part_loaded" and kept["expired_by"] != kept["part_id"]
    # another part's load dropped the mark: nothing of the work part since, still available
    assert row["available"] is True and row["expired_reason"] is None
    entry = services(nx).history[0]
    assert entry["expired"] == "part_loaded" and entry["expired_by"] == kept["expired_by"]


def test_an_engineer_display_switch_that_dropped_the_marks_says_display_reset(tmp_path):
    from nx_mcp.host.services import NXServices
    from nx_mcp.workspace import Workspace

    marks = {2}
    session = NS(DoesUndoMarkExist=lambda mark, _: mark in marks)
    host = NXServices(session, NS(), "test", Workspace(tmp_path))
    host.checkpoints.update({"gone": {"mark": 1}, "kept": {"mark": 2}})
    host.engineer_changed(display_changed=True)
    assert host.checkpoints["gone"]["expired"] == "display_reset"
    assert host.checkpoints["kept"]["expired"] == "engineer_change"
    host.checkpoints["late"] = {"mark": 3}
    host.engineer_changed()  # an edit without a display switch
    assert host.checkpoints["late"]["expired"] == "engineer_change"
