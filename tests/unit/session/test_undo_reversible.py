"""The compensating undo of every created kind and of value edits (domains/session/compensate.py,
core/undoable.py): kinds with a ``@deleter`` are deleted through it, kinds without one must
vanish with them, a tool with a restorer that kept no values and a re-appearing object are
never deleted, state-only steps need nothing, hints name only routes that work, cross-part
refusals count other parts' work without naming it, and a call that drops NX's marks without
saving says so."""

from __future__ import annotations

from typing import TypedDict

import pytest

from nx_mcp.core import catalogue, undoable
from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.tx import Tx
from nx_mcp.domains.session import history as session_history
from nx_mcp.kit.result import Row
from tests.kit import kernel as kit_kernel
from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import Body, Feature, Object
from tests.unit.session.test_undo_multipart import _tools, block, counts, two  # noqa: F401


class Made(TypedDict):
    made: Row


def nx_create_test_note(tx: Tx, *, label: bool = False) -> Made:
    """Create a note (and a modeling view when asked) in the work part."""
    part = tx.part
    note = Object("NOTE")
    note.OwningPart, note.IsOccurrence = part, False
    part.Notes.append(note)
    if label:
        view = Object("VIEW")
        view.OwningPart, view.IsOccurrence = part, False
        part.Labels.append(view)
    return {"made": tx.ref(note, "annotation")}


def nx_create_test_view(tx: Tx) -> Made:
    """Create a modeling view in the work part (a kind without a deleter)."""
    part = tx.part
    view = Object("VIEW")
    view.OwningPart, view.IsOccurrence = part, False
    part.Labels.append(view)
    return {"made": tx.ref(view, "modeling_view")}


def nx_create_test_valued_block(tx: Tx) -> Made:
    """Create a block, as a tool whose restorer sets values back (it keeps none here)."""
    part = tx.part
    body = Body("BODY")
    feature = Feature("VALUED(1)", [body])
    feature.Suppressed = False
    for obj in (body, feature):
        obj.OwningPart, obj.IsOccurrence = part, False
    part.Bodies.append(body)
    part.Features.append(feature)
    return {"made": tx.ref(feature, "feature")}


def nx_create_test_dropper(tx: Tx) -> Made:
    """Create a block and drop every NX undo mark, saving nothing (a knowledge-fusion
    update or an export does that in NX)."""
    made = nx_create_test_valued_block(tx)
    tx.session.marks.clear()
    return made


def nx_create_test_batch_block(tx: Tx) -> Made:
    """Create a block (batchable)."""
    return nx_create_test_valued_block(tx)


SPECS = [
    make_spec(
        fn,
        effect="model",
        label=label,
        keywords=("a", "b", "c"),
        domain="modeling",
        requires=("work_part",),
        batchable=batchable,
    )  # fmt: skip
    for fn, label, batchable in (
        (nx_create_test_note, "Test note", False),
        (nx_create_test_view, "Test view", False),
        (nx_create_test_valued_block, "Test valued block", False),
        (nx_create_test_dropper, "Test dropper", False),
        (nx_create_test_batch_block, "Test batch block", True),
    )
]


def _delete_notes(tx, objects):
    for obj in objects:
        for part in tx.session.Parts:
            if obj in part.Notes:
                part.Notes.remove(obj)


@pytest.fixture(autouse=True)
def _kinds(monkeypatch):
    catalogue.load()
    for spec in SPECS:
        monkeypatch.setitem(catalogue._REGISTRY, spec.name, spec)
    # the fake's snapshot lists notes (annotations) and labels (modeling views)
    kinds = (*kit_kernel.KINDS, ("Notes", "annotation"), ("Labels", "modeling_view"))
    monkeypatch.setattr(kit_kernel, "KINDS", kinds)
    monkeypatch.setitem(catalogue._HOOKS["deleter"], "annotation", _delete_notes)


# ---------------------------------------------------------------------------
# Every created kind


def test_a_created_kind_with_a_deleter_is_undone_through_it_and_said_plainly():
    nx, a, b = two()
    assert run(nx, "nx_create_test_note", operation_id="op_a_note_1", part="a").ok
    block(nx, "b", "op_b_block_1")
    undo = run(nx, "nx_undo_operation", part="a")
    assert undo.ok and undo.data["compensated"] and len(a.Notes) == 0, undo
    assert counts(b) == [(1, 1)]  # the other part's work stays
    assert {r["kind"] for r in undo.changes["deleted"]} == {"annotation"}
    (note,) = undo.warnings
    assert note["code"] == "NX_NOTE"
    assert note["message"].startswith("Undone by deleting what it created (1 annotation); ")
    assert "b.prt" not in note["message"]


def test_a_checkpoint_after_a_created_annotation_is_restorable_by_compensation():
    nx, a, b = two()
    cp = run(nx, "nx_create_checkpoint", part="a", label="clean").data["checkpoint_id"]
    assert run(nx, "nx_create_test_note", part="a").ok
    block(nx, "b", "op_b_block_1")
    row = run(nx, "nx_list_checkpoints", part="a").data["checkpoints"][0]
    assert row["compensated"] is True and row["restorable"] is True and "blocked_by" not in row
    restored = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)
    assert restored.ok and restored.data["compensated"] and len(a.Notes) == 0, restored


def test_a_kind_without_a_deleter_is_refused_with_a_hint_that_works():
    nx, a, b = two()
    assert run(nx, "nx_create_test_view", operation_id="op_a_view_1", part="a").ok
    block(nx, "b", "op_b_block_1")
    refused = run(nx, "nx_undo_operation", part="a")
    error = refused.error
    assert error["code"] == "NX_CROSS_PART" and refused.mutation_outcome == "not_started"
    assert "created modeling view: an undo by deletion cannot delete that kind" in error["message"]
    assert "nx_delete_objects cannot delete that kind" in error["hint"]
    assert "remove the modeling view it created" in error["hint"]
    assert "previous values" not in error["hint"] and len(a.Labels) == 1


def test_a_created_object_without_a_deleter_must_vanish_with_the_others():
    nx, a, b = two()
    assert run(nx, "nx_create_test_note", part="a", label=True).ok
    block(nx, "b", "op_b_block_1")
    refused = run(nx, "nx_undo_operation", part="a")
    assert refused.error["code"] == "NX_VERIFICATION_FAILED", refused
    assert refused.mutation_outcome == "rolled_back"
    assert (len(a.Notes), len(a.Labels)) == (1, 1)  # rolled back: nothing deleted


def test_a_tool_with_a_restorer_that_kept_no_values_is_never_undone_by_deletion(monkeypatch):
    monkeypatch.setitem(catalogue._HOOKS["restorer"], "nx_create_test_valued_block",
                        lambda tx, data, *, apply: "nothing")  # fmt: skip
    nx, a, b = two()
    assert run(nx, "nx_create_test_valued_block", part="a").ok
    block(nx, "b", "op_b_block_1")
    refused = run(nx, "nx_undo_operation", part="a", mode="compensate")
    assert refused.error["code"] == "NX_UNSUPPORTED_OPTION", refused
    assert "kept no values to set back" in refused.error["message"]
    assert counts(a) == [(1, 1)]


def test_objects_that_existed_before_the_call_are_never_deleted(monkeypatch):
    nx, a, b = two()
    refs = services(nx).references
    monkeypatch.setattr(refs, "issued", lambda: 10**9, raising=False)  # every ref is older
    block(nx, "a", "op_a_block_1")
    assert services(nx).history[-1]["ref_floor"] == 10**9
    block(nx, "b", "op_b_block_1")
    refused = run(nx, "nx_undo_operation", part="a")
    assert refused.error["code"] == "NX_CROSS_PART", refused
    assert "which existed before it" in refused.error["message"] and counts(a) == [(1, 1)]


# ---------------------------------------------------------------------------
# Receipt rules


def _unit(tool, changes, **extra):
    return undoable.Unit("op_x", tool, changes, **extra)


def test_a_restorer_unit_may_have_taken_what_a_feature_state_takes(monkeypatch):
    monkeypatch.setitem(catalogue._HOOKS["restorer"], "nx_test_state", lambda *a, **k: "")
    body = {"deleted": [{"id": "b:1", "kind": "body"}, {"id": "pt:2", "kind": "point"}]}
    undoable.settable(_unit("nx_test_state", body, restore={"x": 1}))
    note = {"deleted": [{"id": "an:1", "kind": "annotation"}]}
    with pytest.raises(undoable.Refusal, match="does not bring them back"):
        undoable.settable(_unit("nx_test_state", note, restore={"x": 1}))
    undoable.settable(_unit("nx_test_state", note, restore={"x": 1}, brings_back=True))


def test_a_restorer_note_can_say_it_brings_deleted_objects_back():
    nx, _a, _b = two()
    entry = {"operation_id": "op_y", "method": "nx_test_state", "restore": {"x": 1},
             "restore_brings_back": True}  # fmt: skip
    record = {"state": "committed", "envelope": {"changes": {"deleted": [{"kind": "component"}]}}}
    (unit,) = undoable.units(services(nx), entry, record)
    assert unit.brings_back is True


def test_a_state_only_step_that_changed_nothing_needs_nothing_undone():
    created = [{"id": "f:1", "kind": "feature"}]
    entry = {"operation_id": "op_batch", "method": "nx_run_batch"}
    record = {
        "state": "committed",
        "envelope": {
            "changes": {"created": created},
            "data": {"results": [
                {"tool": "nx_create_sketch", "changes": {"created": created}},
                {"tool": "nx_finish_sketch", "changes": {"created": [], "modified": []}},
            ]},
        },
    }  # fmt: skip
    assert undoable.why_not(None, entry, record) is None
    finished = record["envelope"]["data"]["results"][1]
    finished["changes"]["modified"] = [{"id": "f:9", "kind": "feature"}]  # it did change one
    assert undoable.why_not(None, entry, record) is not None


def test_a_step_that_changed_only_what_earlier_steps_made_needs_nothing_of_its_own():
    sketch = {"id": "sk:1", "kind": "sketch"}
    entry = {"operation_id": "op_batch", "method": "nx_run_batch"}
    record = {
        "state": "committed",
        "envelope": {
            "changes": {"created": [sketch]},
            "data": {"results": [
                {"tool": "nx_create_sketch", "changes": {"created": [sketch]}},
                {"tool": "nx_create_test_curves", "changes": {
                    "created": [{"id": "c:2", "kind": "curve"}], "modified": [sketch]}},
                {"tool": "nx_edit_test_sketch", "changes": {"modified": [sketch]}},
            ]},
        },
    }  # fmt: skip
    assert undoable.why_not(None, entry, record) is None
    record["envelope"]["data"]["results"][2]["changes"]["modified"].append(
        {"id": "f:7", "kind": "feature"}
    )  # an object the call did not make
    assert "changed objects it did not create" in undoable.why_not(None, entry, record).reason


def test_fix_forward_names_only_routes_that_work():
    created = _unit("nx_create_test_note", {"created": [{"id": "an:1", "kind": "annotation"}]})
    assert undoable.fix_forward(created) == (
        "Fix forward instead: delete what it created with nx_delete_objects (an:1)"
    )
    view = _unit("nx_create_test_view", {"created": [{"id": "mv:1", "kind": "modeling_view"}]})
    assert "nx_delete_objects" not in undoable.fix_forward(view).split("(")[0]
    edited = _unit("nx_edit_dimension", {"modified": [{"id": "d:1", "kind": "dimension"}]})
    assert "call nx_edit_dimension again with the previous values" in undoable.fix_forward(edited)
    deleted = _unit("nx_delete_objects", {"deleted": [{"id": "f:1", "kind": "feature"}]})
    assert "create again what it deleted" in undoable.fix_forward(deleted)
    made = _unit("nx_create_test_block", {"created": [{"id": "f:2", "kind": "feature"}],
                                          "modified": [{"id": "o:3", "kind": "component"}]})  # fmt: skip
    hint = undoable.fix_forward(made)
    assert "nx_delete_objects (f:2)" in hint and "nx_create_test_block again" not in hint


# ---------------------------------------------------------------------------
# Checkpoints and the batch with a finished sketch


def test_a_batch_with_a_state_only_step_keeps_the_checkpoint_restorable():
    nx, a, b = two()
    cp = run(nx, "nx_create_checkpoint", part="a", label="start").data["checkpoint_id"]
    ops = [{"tool": "nx_create_test_batch_block", "arguments": {}},
           {"tool": "nx_finish_sketch", "arguments": {}}]  # fmt: skip
    batch = run(nx, "nx_run_batch", part="a", operations=ops)
    assert batch.ok, batch
    assert not any("can no longer be restored" in w["message"] for w in batch.warnings)
    block(nx, "b", "op_b_block_1")
    later = block(nx, "a", "op_a_block_2")  # never a note about an earlier call
    assert not any("checkpoint" in w["message"].lower() for w in later.warnings)
    row = run(nx, "nx_list_checkpoints", part="a").data["checkpoints"][0]
    assert row["compensated"] is True and row["restorable"] is True, row
    restored = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)
    assert restored.ok and counts(a, b) == [(0, 0), (1, 1)], restored


def test_a_failed_call_never_repeats_a_checkpoint_warning():
    nx, a, b = two()
    run(nx, "nx_create_checkpoint", part="a", label="start")
    old = Feature("OLD")
    old.OwningPart, old.IsOccurrence, old.Suppressed = a, False, False
    a.Features.append(old)
    deleted = run(nx, "nx_delete_objects", part="a", objects=[nx.ref_of(old)])
    assert any("only while no other part changes" in w["message"] for w in deleted.warnings)
    block(nx, "b", "op_b_block_1")
    failed = run(nx, "nx_delete_objects", part="a", objects=["fe:999999"])
    assert not failed.ok
    assert not any("checkpoint" in w["message"].lower() for w in failed.warnings or [])


def test_the_listing_uses_the_restores_own_check():
    nx, a, b = two()
    made = block(nx, "a", "op_a_block_1")
    ref = made.data["feature"]["id"]
    cp = run(nx, "nx_create_checkpoint", part="a", label="named").data["checkpoint_id"]
    assert run(nx, "nx_set_name", part="a", object=ref, name="FIRST").ok
    block(nx, "b", "op_b_block_1")
    a.Features[0].Name = "BY_HAND"  # changed outside the agent's calls
    row = run(nx, "nx_list_checkpoints", part="a").data["checkpoints"][0]
    assert row["compensated"] is True and row["restorable"] is False, row
    assert "renamed since" in row["blocked_by"]["reason"]
    refused = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)
    assert refused.error["code"] == "NX_CROSS_PART" and "renamed since" in refused.error["message"]


def test_a_value_edited_twice_since_a_checkpoint_is_set_back_step_by_step():
    nx, a, b = two()
    made = block(nx, "a", "op_a_block_1")
    ref = made.data["feature"]["id"]
    before = a.Features[0].Name
    cp = run(nx, "nx_create_checkpoint", part="a", label="named").data["checkpoint_id"]
    assert run(nx, "nx_set_name", part="a", object=ref, name="FIRST").ok
    assert run(nx, "nx_set_name", part="a", object=ref, name="SECOND").ok
    block(nx, "b", "op_b_block_1")
    restored = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)
    assert restored.ok and restored.data["compensated"], restored
    assert a.Features[0].Name == before and len(restored.data["restored"]) == 2


# ---------------------------------------------------------------------------
# Marks NX dropped without a save, and what is not in the history


def test_marks_dropped_without_a_save_are_not_called_a_save():
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    cp = run(nx, "nx_create_checkpoint", part="a", label="start").data["checkpoint_id"]
    block(nx, "a", "op_a_block_2")
    dropped = run(nx, "nx_create_test_dropper", operation_id="op_a_drop_1", part="a")
    assert dropped.ok and any("saved and closed nothing" in w["message"] for w in dropped.warnings)
    entry = next(h for h in services(nx).history if h.get("operation_id") == "op_a_block_2")
    assert entry["expired"] == "marks_dropped" and entry["expired_tool"] == "nx_create_test_dropper"
    row = run(nx, "nx_list_checkpoints", part="a").data["checkpoints"][0]
    assert row["available"] is True and row["expired_reason"] is None and row["compensated"]
    undo = run(nx, "nx_undo_operation", part="a", steps=2)  # still undone, by deletion
    assert undo.ok and undo.data["compensated"] and counts(a) == [(1, 1)], undo
    assert (
        "during nx_create_test_dropper, which saved and closed nothing"
        in (undo.warnings[0]["message"])
    )
    gone = run(nx, "nx_restore_checkpoint", checkpoint_id=cp)  # the undo went back past it
    assert gone.error["details"]["reason"] == "undone_past", gone


def test_an_operation_outside_the_history_is_explained_from_its_receipt():
    nx, a, b = two()
    unknown = run(nx, "nx_undo_operation", part="a", operation="op_never_ran")
    assert unknown.error["message"] == "No operation op_never_ran ran in this session"
    run(nx, "nx_create_checkpoint", operation_id="op_cp_one", part="a", label="x")
    session = run(nx, "nx_undo_operation", part="a", operation="op_cp_one")
    assert "changed no model" in session.error["message"], session
    block(nx, "a", "op_a_block_1")
    assert run(nx, "nx_undo_operation", operation_id="op_undo_1", part="a").ok
    again = run(nx, "nx_undo_operation", part="a", operation="op_a_block_1")
    assert "was already undone by op_undo_1" in again.error["message"]
    assert again.error["hint"] == "nx_redo_operation repeats it"


def test_an_undo_that_deletes_drawing_objects_displays_the_part():
    nx, a, b = two()
    assert run(nx, "nx_create_test_note", operation_id="op_b_note_1", part="b").ok
    block(nx, "a", "op_a_block_1")
    box = services(nx)
    assert session_history._undo_displays(box, b, {}) is True
    assert session_history._undo_displays(box, a, {}) is False  # a block: NX's undo
    assert session_history._undo_displays(box, b, {"operation": "op_unknown"}) is False


def nx_set_test_shown_value(tx: Tx) -> Made:
    """Set a value with a display-bound tool and keep the value before (an in-sketch edit)."""
    made = nx_create_test_valued_block(tx)
    tx.note_restore({"value": 1})
    return made


SHOWN = make_spec(
    nx_set_test_shown_value, effect="model", label="Set test shown value", keywords=("a", "b", "c"),
    domain="modeling", requires=("work_part",), display_bound=True,
)  # fmt: skip


def test_a_display_bound_tools_restorer_runs_in_the_displayed_part(monkeypatch):
    monkeypatch.setitem(catalogue._REGISTRY, SHOWN.name, SHOWN)
    monkeypatch.setitem(catalogue._HOOKS["restorer"], SHOWN.name, lambda tx, data, *, apply: "")
    nx, a, b = two()
    assert run(nx, "nx_set_test_shown_value", part="a").ok
    assert session_history._undo_displays(services(nx), a, {}) is False  # NX's undo
    block(nx, "b", "op_b_block_1")  # now its restorer runs: displayed for the call
    assert session_history._undo_displays(services(nx), a, {}) is True


def test_a_refusal_keeps_the_restorers_own_hint(monkeypatch):
    from nx_mcp.core.errors import fail

    def refuse(tx, data, *, apply):
        fail("NX_DEPENDENCY_ORDER", "It moved since", hint="Move it back with nx_test first")

    monkeypatch.setitem(catalogue._REGISTRY, SHOWN.name, SHOWN)
    monkeypatch.setitem(catalogue._HOOKS["restorer"], SHOWN.name, refuse)
    nx, a, b = two()
    assert run(nx, "nx_set_test_shown_value", part="a").ok
    block(nx, "b", "op_b_block_1")
    refused = run(nx, "nx_undo_operation", part="a")
    assert refused.error["code"] == "NX_CROSS_PART" and "It moved since" in refused.error["message"]
    assert refused.error["hint"] == "Move it back with nx_test first"


def nx_create_test_hiding_block(tx: Tx) -> Made:
    """Create a block that hides the part's first body (a mirror body hides its source)."""
    source = tx.part.Bodies[0]
    made = nx_create_test_valued_block(tx)
    source.IsBlanked = True
    tx.note_restore({"hidden": tx.ref(source, "body")["id"]}, after_delete=True)
    return made


def _show_again(tx, data, *, apply):
    body = tx.resolve(data["hidden"])
    if apply:
        body.IsBlanked = False
    return f"{data['hidden']} shown again"


HIDING = make_spec(
    nx_create_test_hiding_block, effect="model", label="Test hiding block",
    keywords=("a", "b", "c"), domain="modeling", requires=("work_part",),
)  # fmt: skip


def test_a_restorer_after_the_deletion_shows_hidden_sources_again(monkeypatch):
    monkeypatch.setitem(catalogue._REGISTRY, HIDING.name, HIDING)
    monkeypatch.setitem(catalogue._HOOKS["restorer"], HIDING.name, _show_again)
    nx, a, b = two()
    block(nx, "a", "op_a_block_1")
    source = a.Bodies[0]
    assert run(nx, "nx_create_test_hiding_block", part="a").ok and source.IsBlanked
    block(nx, "b", "op_b_block_1")
    undo = run(nx, "nx_undo_operation", part="a")
    assert undo.ok and undo.data["compensated"], undo
    assert counts(a) == [(1, 1)] and source.IsBlanked is False
    assert undo.data["restored"] == [f"{nx.ref_of(source)} shown again"]
