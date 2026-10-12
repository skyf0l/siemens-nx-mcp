"""Closing and saving with several agent parts loaded (phase P4): busy parts
(core/busy.py), closes that keep NX's undo marks (_saving.close_part), file names on
activate and save, and basename collisions.

Busy rule, no caller identity: with two or more agent parts loaded, a close of a part a
call worked in within 300 s is refused while the last call worked in another loaded
part; ``force`` closes anyway; a close right after working in the part, and a run of
closes, always close.
"""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import TypedDict

import pytest

from nx_mcp.core import busy, catalogue
from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.tx import Tx
from nx_mcp.domains.parts_files import parts as tools
from nx_mcp.kit.result import Row
from tests.kit.kernel import services
from tests.kit.objects import Body, Feature, Part
from tests.unit.parts_files.support import call


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


BLOCK = make_spec(
    nx_create_test_block, effect="model", label="Test block", keywords=("a", "b", "c"),
    domain="modeling", requires=("work_part",),
)  # fmt: skip


@pytest.fixture(autouse=True)
def _tools(monkeypatch):
    catalogue.load()
    monkeypatch.setitem(catalogue._REGISTRY, BLOCK.name, BLOCK)


def block(nx, part):
    made = call(nx, NS(spec=BLOCK), part=part)
    assert made.ok, made
    return made


def create(nx, path, **args):
    made = call(nx, tools.nx_create_part, path=path, **args)
    assert made.ok, made
    return made


def loaded(nx) -> list[str]:
    return sorted(p.Name for p in nx.session.Parts)


def _keeps_prototypes(part):
    """NX 2406 keeps a part loaded on its own after its assembly closes."""
    part.Close = lambda *_a: Part.Close(part)
    part.CloseRetainUndoMarks = lambda *_a: Part.CloseRetainUndoMarks(part)


# ---------------------------------------------------------------------------
# Busy parts


def test_a_busy_part_is_refused_while_the_last_call_worked_in_another_part(nx):
    create(nx, "mc/a.prt")
    create(nx, "mc/b.prt", display=False)
    block(nx, "a")
    block(nx, "b")
    refused = call(nx, tools.nx_close_part, part="a", save=False)
    assert refused.error["code"] == "NX_PART_BUSY", refused
    assert refused.error["category"] == "blocked" and refused.error["field"] == "/part"
    assert refused.mutation_outcome == "not_started" and "force: true" in refused.error["hint"]
    details = refused.error["details"]
    assert details["part"] == "a.prt" and details["last_call_part"] == "b.prt"
    assert details["last_change"]["tool"] == "nx_create_test_block"
    assert "a" in loaded(nx)
    forced = call(nx, tools.nx_close_part, part="a", save=False, force=True)
    assert forced.ok and [r["name"] for r in forced.data["closed"]] == ["a"], forced
    say = {"part": "a"}.get
    assert tools.nx_close_part.spec.summary({"part": "a", "force": True}, say) == (
        "Close a without saving (forced)"
    )


def test_an_own_close_right_after_working_in_the_part_closes(nx):
    create(nx, "mc/a.prt")
    create(nx, "mc/b.prt", display=False)
    block(nx, "a")
    block(nx, "b")
    closed = call(nx, tools.nx_close_part, part="b.prt", save=False)
    assert closed.ok, closed
    assert loaded(nx) == ["a", "work"]


def test_a_run_of_closes_closes_every_part(nx):
    create(nx, "mc/a.prt")
    create(nx, "mc/b.prt", display=False)
    block(nx, "a")
    block(nx, "b")
    assert call(nx, tools.nx_close_part, part="b", save=False).ok
    second = call(nx, tools.nx_close_part, part="a", save=False)  # the stream's part is gone
    assert second.ok, second


def test_one_agent_part_is_never_busy(nx):
    nx.session.Parts.remove(nx.part)  # only the agent's own part stays loaded
    create(nx, "mc/a.prt")
    block(nx, "a")
    other = Part(nx.session, services(nx).root / "eng.prt")
    services(nx).foreign.add(services(nx).part_key(other))  # the engineer's part
    nx.session.Parts.Work = nx.session.Parts.Display = nx.session.Parts[0]
    assert busy.facts(services(nx), nx.session.Parts[0]) is None


def test_work_older_than_the_busy_time_does_not_block(nx):
    create(nx, "mc/a.prt")
    create(nx, "mc/b.prt", display=False)
    block(nx, "a")
    block(nx, "b")
    entries = services(nx).part_activity()["parts"]
    for entry in entries.values():
        entry["changed_at"] -= busy.BUSY_SECONDS + 1
    assert call(nx, tools.nx_close_part, part="a", save=False).ok


def test_reads_and_view_effects_never_make_a_part_busy(nx):
    svc = services(nx)
    target = NS(key="part_x")
    for effect in ("read", "view"):
        busy.record(svc, NS(effect=effect, name="nx_capture_view"), target, None, None)
    assert svc.part_activity().get("parts", {}) == {}
    busy.record(svc, NS(effect="model", name="nx_extrude"), target, None, None)
    assert svc.part_activity()["parts"]["part_x"]["tool"] == "nx_extrude"
    assert svc.part_activity()["stream"] == "part_x"


def test_a_load_makes_the_new_part_busy_and_the_stream(nx):
    create(nx, "mc/a.prt")
    block(nx, "a")
    create(nx, "mc/b.prt", display=False)  # the last call worked in b (its load)
    refused = call(nx, tools.nx_close_part, part="a", save=False)
    assert refused.error["code"] == "NX_PART_BUSY", refused


def test_saving_a_busy_part_only_warns(nx):
    create(nx, "mc/a.prt")
    create(nx, "mc/b.prt", display=False)
    block(nx, "a")
    block(nx, "b")
    saved = call(nx, tools.nx_save_part, part="a")
    assert saved.ok, saved
    notes = [w["message"] for w in saved.warnings]
    assert any("another agent" in n and "a.prt" in n for n in notes), notes


# ---------------------------------------------------------------------------
# Closes keep NX's undo marks


def test_the_active_tab_closes_keeping_marks_and_the_previous_tab_comes_back(nx):
    create(nx, "mc/a.prt", display=True)  # its own tab, the fixture's part keeps its tab
    create(nx, "mc/b.prt", display=False)
    made = block(nx, "b")
    checkpoint = call(nx, NS(spec=catalogue.load()["nx_create_checkpoint"]), part="b")
    assert checkpoint.ok, checkpoint
    assert nx.session.Parts.Display.Name == "a"
    assert [p.Name for p in nx.session.displayed()] == ["a", "work"]
    marks = dict(nx.session.marks)
    closed = call(nx, tools.nx_close_part, part="a", save=False, force=True)
    assert closed.ok and closed.data["undo_marks"] == "kept", closed
    assert closed.data["displayed"]["id"] == nx.ref_of(nx.part, "part")
    assert nx.session.Parts.Display is nx.part and nx.session.shown[-1][0] == "retain"
    assert nx.session.displayed() == [nx.part]  # b never got a tab
    assert set(marks) <= set(nx.session.marks)  # NX dropped none
    history = services(nx).history
    assert history[-1]["close"] == ["a.prt"] and history[-1]["mark"] is None
    undo = call(nx, NS(spec=catalogue.load()["nx_undo_operation"]), part="b")
    assert undo.error["code"] == "NX_NOT_FOUND", undo
    assert undo.error["details"]["reason"] == "part_closed"
    assert "a" not in loaded(nx)  # never loaded again
    listed = call(nx, NS(spec=catalogue.load()["nx_list_checkpoints"]), part="b")
    (row,) = listed.data["checkpoints"]
    assert row["checkpoint_id"] == checkpoint.data["checkpoint_id"]
    assert row["available"] is False and row["expired_reason"] == "part_closed"  # mark kept
    assert made.ok


def test_the_last_tab_closes_keeping_marks_and_leaves_no_display_part(nx):
    nx.session.Parts.remove(nx.part)
    create(nx, "mc/a.prt")
    block(nx, "a")
    marks = dict(nx.session.marks)
    closed = call(nx, tools.nx_close_part, part="a", save=False)
    assert closed.ok and closed.data["undo_marks"] == "kept", closed
    assert "displayed" not in closed.data and nx.session.Parts.Display is None
    assert set(marks) <= set(nx.session.marks)
    assert not any("dropped every undo mark" in w["message"] for w in closed.warnings)


def test_a_close_without_the_retaining_call_drops_the_marks_and_says_so(nx):
    nx.session.Parts.remove(nx.part)
    create(nx, "mc/a.prt")
    block(nx, "a")
    part = next(p for p in nx.session.Parts if p.Name == "a")
    part.CloseRetainUndoMarks = None  # an NX without the call
    closed = call(nx, tools.nx_close_part, part="a", save=False)
    assert closed.ok and closed.data["undo_marks"] == "dropped", closed
    assert any("dropped every undo mark" in w["message"] for w in closed.warnings)


def test_a_background_part_closes_keeping_marks_without_a_display_change(nx):
    create(nx, "mc/a.prt", display=True)
    create(nx, "mc/b.prt", display=False)
    block(nx, "a")
    shown = len(nx.session.shown)
    closed = call(nx, tools.nx_close_part, part="b", save=False, force=True)
    assert closed.ok and closed.data["undo_marks"] == "kept" and "displayed" not in closed.data
    assert len(nx.session.shown) == shown and nx.session.Parts.Display.Name == "a"


# ---------------------------------------------------------------------------
# Assembly closes spare components in use


def test_an_assembly_close_keeps_a_component_a_call_worked_in_after_its_load(nx):
    early, late = nx.new_part("mc/early.prt"), nx.new_part("mc/late.prt")  # brought in
    create(nx, "mc/asm.prt", display=True)
    asm = next(p for p in nx.session.Parts if p.Name == "asm")
    nx.attach(asm, early)
    nx.attach(asm, late)
    _keeps_prototypes(asm)
    block(nx, "late")  # after the assembly's load: in use
    block(nx, "asm")
    closed = call(nx, tools.nx_close_part, part="asm", save=False)
    assert closed.ok, closed
    assert [r["name"] for r in closed.data["unloaded_components"]] == ["early"]
    (kept,) = closed.data["kept_components"]
    assert kept["part"]["name"] == "late" and kept["reason"].startswith("in use:")
    assert "late" in loaded(nx) and "early" not in loaded(nx)


def test_an_assembly_close_keeps_parts_created_or_opened_by_name(nx, root):
    """Precise unload rule: only parts the assembly brought in close with it."""
    create(nx, "mc/cover.prt", display=False)  # created by name: stays
    bolt = nx.new_part("mc/bolt.prt")
    bolt.Save()
    opened = call(nx, tools.nx_open_part, path="mc/bolt.prt")  # opened by name: stays
    assert opened.ok and opened.data["already_loaded"], opened
    pulled = nx.new_part("mc/washer.prt")  # loaded only by the assembly
    create(nx, "mc/kit.prt", display=True)
    kit = next(p for p in nx.session.Parts if p.Name == "kit")
    for name in ("cover", "bolt"):
        nx.attach(kit, next(p for p in nx.session.Parts if p.Name == name))
    nx.attach(kit, pulled)
    _keeps_prototypes(kit)
    closed = call(nx, tools.nx_close_part, part="kit", save=False)
    assert closed.ok, closed
    assert [r["name"] for r in closed.data["unloaded_components"]] == ["washer"]
    kept = {r["part"]["name"]: r["reason"] for r in closed.data["kept_components"]}
    assert set(kept) == {"cover", "bolt"}
    assert all(reason.startswith("created or opened by name") for reason in kept.values())
    assert {"cover", "bolt"} <= set(loaded(nx)) and "washer" not in loaded(nx)


# ---------------------------------------------------------------------------
# File names on activate and save; basename collisions


def test_activate_and_save_take_file_names_like_close(nx, root):
    create(nx, "mc/a.prt")
    create(nx, "mc/b.prt", display=False)
    shown = call(nx, tools.nx_activate_part, part="b", display=True)
    assert shown.ok and shown.data["display"] and shown.data["part"]["name"] == "b", shown
    saved = call(nx, tools.nx_save_part, part="a.prt")
    assert saved.ok and saved.data["path"] == "mc/a.prt", saved
    by_path = call(nx, tools.nx_save_part, part="mc/b.prt")
    assert by_path.ok and by_path.data["path"] == "mc/b.prt", by_path
    unknown = call(nx, tools.nx_activate_part, part="nope")
    assert unknown.error["code"] == "NX_NOT_FOUND" and unknown.error["field"] == "/part"
    assert unknown.mutation_outcome == "not_started"
    assert "only for display" in tools.nx_activate_part.spec.description.lower()


def test_a_taken_file_name_suggests_a_unique_one_and_never_offers_the_part(nx, root):
    create(nx, "mc/a.prt")
    clash = call(nx, tools.nx_create_part, path="other/A.prt")
    assert clash.error["code"] == "NX_NAME_CONFLICT", clash
    assert "taken" in clash.error["message"] and "activate" not in clash.error["message"]
    assert clash.error["details"]["suggested_path"] == "other/A_2.prt"
    assert "other/A_2.prt" in clash.error["hint"]
    same = call(nx, tools.nx_create_part, path="mc/a.prt")
    assert same.error["code"] == "NX_ALREADY_EXISTS" and "activate" not in same.error["message"]
    assert same.error["details"]["suggested_path"] == "mc/a_2.prt"
    (root / "mc" / "a_2.prt").write_text("on disk")
    again = call(nx, tools.nx_create_part, path="mc/a.prt")
    assert again.error["details"]["suggested_path"] == "mc/a_3.prt"


def test_the_tab_that_comes_back_after_a_close_is_shown_in_sheet_metal(nx, monkeypatch):
    from nx_mcp.kit import parts as kparts

    create(nx, "mc/bent.prt", display=True)
    create(nx, "mc/a.prt", display=True)
    real = kparts.has_sheet_metal
    monkeypatch.setattr(kparts, "has_sheet_metal", lambda p: p is not None and (
        p.Name == "bent" or real(p)))  # fmt: skip
    assert [p.Name for p in nx.session.displayed()] == ["a", "bent", "work"]
    closed = call(nx, tools.nx_close_part, part="a", save=False, force=True)
    assert closed.ok and closed.data["displayed"]["name"] == "bent", closed
    assert nx.session.Parts.Display.Name == "bent"  # the most recently active tab
    assert ("ApplicationSwitchImmediate", "UG_APP_SBSM") in nx.session.calls  # no dialog
    assert [p.Name for p in nx.session.displayed()] == ["bent", "work"]


# ---------------------------------------------------------------------------
# envelope.part of a close


def test_a_close_reports_the_closed_part_with_the_ref_it_had(nx):
    create(nx, "mc/a.prt")
    b = create(nx, "mc/b.prt", display=False)
    closed = call(nx, tools.nx_close_part, part="b.prt", save=False, force=True)
    assert closed.ok, closed
    # its ref before the close, never a fresh ref of a part that is gone
    assert closed.part == {"id": b.data["part"]["id"], "file": "b.prt", "from": "arg"}
    by_ref = create(nx, "mc/c.prt", display=False)
    gone = call(nx, tools.nx_close_part, part=by_ref.data["part"]["id"], save=False, force=True)
    assert gone.ok and gone.part["id"] == by_ref.data["part"]["id"], gone
    assert gone.part["file"] == "c.prt"
