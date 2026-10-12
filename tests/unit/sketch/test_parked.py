"""Parked sketches (core/parking.py, kit.sketch, domains/sketch/sketches.py): no sketch
stays active in NX between calls, an unfinished one stays its part's open sketch.

NX has one active sketch for the session and refuses model calls in every part while it
is active; a parked sketch blocks only its own part's model tools, as before.
"""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import TypedDict

import pytest

from nx_mcp.core import catalogue, parking
from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.tx import Tx
from nx_mcp.kit.result import Row
from tests.kit import run
from tests.kit.kernel import services
from tests.kit.objects import Part
from tests.unit.sketch.fakes import sketch_nx

RECT = [{"type": "rectangle", "corner1": [0, 0], "corner2": [30, 10]}]


class Touched(TypedDict):
    part: Row


def nx_set_test_model(tx: Tx) -> Touched:
    """A model tool that needs no active sketch (like every modeling tool)."""
    return {"part": tx.ref(tx.part, "part")}


TOUCH = make_spec(
    nx_set_test_model, effect="model", label="Test model", keywords=("a", "b", "c"),
    domain="modeling", requires=("work_part", "no_active_sketch"),
)  # fmt: skip


@pytest.fixture(autouse=True)
def _tools(monkeypatch):
    catalogue.load()
    monkeypatch.setitem(catalogue._REGISTRY, TOUCH.name, TOUCH)


def two():
    """The sketch fake's ``work.prt`` (A, displayed and work) and ``b.prt`` (B)."""
    nx = sketch_nx()
    session_parts = nx.session.Parts
    work, display = session_parts.Work, session_parts.Display
    b = Part(nx.session, services(nx).root / "b.prt")
    session_parts.Work, session_parts.Display = work, display
    nx.nx.DisplayPartOption = NS(AllowAdditional=1, ReplaceExisting=0)  # tab switches
    nx.nx.PartDisplayPartWorkPartOption = NS(SameAsDisplay=0, UseLast=1)
    return nx, nx.part, b


def test_an_unfinished_sketch_is_parked_open_but_not_active_in_nx():
    nx = sketch_nx()
    made = run(nx, "nx_create_sketch", placement="xy")
    assert made.ok and made.data["active"] is True and made.data["parked"] is True, made
    sketch = nx.part.Sketches[-1]
    assert nx.session.ActiveSketch is None
    assert parking.parked(services(nx), nx.part) is sketch
    read = run(nx, "nx_get_sketch", sketch=made.data["sketch"]["id"], include=["geometry"])
    assert read.data["active"] is True and read.data["parked"] is True, read
    listed = run(nx, "nx_list_objects", kind="sketch")
    assert listed.data["items"][0]["details"] == {"active": True, "parked": True}


def test_curve_tools_open_the_parked_sketch_by_ref_for_one_call():
    nx = sketch_nx()
    sk = run(nx, "nx_create_sketch", placement="xy").data["sketch"]["id"]
    added = run(nx, "nx_create_sketch_curves", sketch=sk, curves=RECT)
    assert added.ok and len(added.data["curves"]) == 4, added
    assert nx.session.ActiveSketch is None  # deactivated again at the end of the call
    assert parking.parked(services(nx), nx.part) is nx.part.Sketches[-1]
    done = run(nx, "nx_finish_sketch", sketch=sk)
    assert done.ok and done.data["was_active"] is True and done.data["curve_count"] == 4, done
    assert parking.parked(services(nx), nx.part) is None and nx.session.ActiveSketch is None
    again = run(nx, "nx_finish_sketch", sketch=sk)
    assert again.ok and again.data["was_active"] is False


def test_the_parked_sketch_still_blocks_its_own_parts_model_tools_and_other_sketches():
    nx = sketch_nx()
    sk = run(nx, "nx_create_sketch", placement="xy").data["sketch"]["id"]
    refused = run(nx, "nx_set_test_model")
    assert (
        refused.error["code"] == "NX_SKETCH_ACTIVE" and "nx_finish_sketch" in refused.error["hint"]
    ), refused
    second = run(nx, "nx_create_sketch", placement="xz")
    assert second.error["code"] == "NX_SKETCH_ACTIVE", second
    other = nx.sketch()  # a finished sketch of the same part
    curves = run(nx, "nx_create_sketch_curves", sketch=other.ref, curves=RECT)
    assert curves.error["code"] == "NX_SKETCH_ACTIVE", curves
    finish_other = run(nx, "nx_finish_sketch", sketch=other.ref)
    assert finish_other.error["code"] == "NX_SKETCH_ACTIVE"
    assert finish_other.error["field"] == "/sketch"
    status = run(nx, "nx_get_sketch", sketch=other.ref, include=["status"])
    assert status.error["code"] == "NX_SKETCH_ACTIVE", status
    assert run(nx, "nx_finish_sketch").data["was_active"] is True  # the part's open sketch
    assert run(nx, "nx_set_test_model").ok
    assert sk


def test_a_parked_sketch_never_blocks_another_parts_calls():
    nx, a, b = two()
    made = run(nx, "nx_create_sketch", placement="xy", part="work")
    assert made.ok and made.data["parked"] is True, made
    in_b = run(nx, "nx_set_test_model", part="b")
    assert in_b.ok and in_b.part["file"] == "b.prt", in_b
    in_a = run(nx, "nx_set_test_model", part="work")
    assert in_a.error["code"] == "NX_SKETCH_ACTIVE"
    nothing = run(nx, "nx_finish_sketch", part="b")  # B has no open sketch: A's stays
    assert nothing.ok and nothing.data["was_active"] is False
    assert parking.parked(services(nx), a) is a.Sketches[-1]
    done = run(nx, "nx_finish_sketch", part="work")
    assert done.ok and done.data["was_active"] is True
    assert run(nx, "nx_set_test_model", part="work").ok
    assert len(b.Sketches) == 0


def test_an_undone_sketch_is_no_longer_parked():
    nx = sketch_nx()
    made = run(nx, "nx_create_sketch", placement="xy")
    assert parking.open_in(services(nx), nx.part)
    undo = run(nx, "nx_undo_operation")
    assert undo.ok, undo
    assert not parking.open_in(services(nx), nx.part) and list(nx.part.Sketches) == []
    assert run(nx, "nx_set_test_model").ok
    assert made.ok


def test_one_call_profile_with_finish_is_never_parked():
    nx = sketch_nx()
    made = run(nx, "nx_create_sketch", placement="xy", curves=RECT, finish=True)
    assert made.ok and made.data["active"] is False and "parked" not in made.data, made
    assert not parking.open_in(services(nx), nx.part)


def test_a_failed_create_leaves_nothing_parked():
    nx = sketch_nx()
    nx.next_sketch = ((0, 0, 1), (1, 0, 0), (0, 1, 0))  # NX ignored the origin
    bad = run(nx, "nx_create_sketch", placement="xy")
    assert bad.mutation_outcome == "rolled_back"
    assert not parking.open_in(services(nx), nx.part) and nx.session.ActiveSketch is None


def test_undoing_the_finish_of_a_parked_sketch_parks_it_again():
    nx = sketch_nx()
    sk = run(nx, "nx_create_sketch", placement="xy").data["sketch"]["id"]
    assert run(nx, "nx_create_sketch_curves", sketch=sk, curves=RECT).ok
    done = run(nx, "nx_finish_sketch", sketch=sk)
    assert done.ok and not parking.open_in(services(nx), nx.part), done
    assert services(nx).history[-1]["parked_sketch"] == sk
    undo = run(nx, "nx_undo_operation")
    assert undo.ok and undo.data["tool"] == "nx_finish_sketch", undo
    sketch = nx.part.Sketches[-1]
    assert parking.parked(services(nx), nx.part) is sketch and nx.session.ActiveSketch is None
    notes = [w["message"] for w in undo.warnings if w["code"] == "NX_NOTE"]
    assert any(sk in n and "parked" in n for n in notes), undo.warnings
    read = run(nx, "nx_get_sketch", sketch=sk, include=["geometry"])
    assert read.data["active"] is True and read.data["parked"] is True, read
    assert len(read.data["curves"]) == 4
    assert run(nx, "nx_set_test_model").error["code"] == "NX_SKETCH_ACTIVE"
    again = run(nx, "nx_finish_sketch")  # the part's open sketch: finished again
    assert again.ok and again.data["was_active"] is True and again.data["curve_count"] == 4
    assert not parking.open_in(services(nx), nx.part) and run(nx, "nx_set_test_model").ok


def test_undoing_a_sketch_with_its_finish_parks_nothing():
    nx = sketch_nx()
    sk = run(nx, "nx_create_sketch", placement="xy").data["sketch"]["id"]
    assert run(nx, "nx_finish_sketch", sketch=sk).ok
    undo = run(nx, "nx_undo_operation", steps=2)
    assert undo.ok and list(nx.part.Sketches) == [], undo
    assert not parking.open_in(services(nx), nx.part)
    assert not [w for w in undo.warnings or [] if "Sketch" in w["message"]], undo.warnings
    assert run(nx, "nx_set_test_model").ok


def test_restoring_a_checkpoint_before_a_finish_parks_the_sketch_again():
    nx = sketch_nx()
    sk = run(nx, "nx_create_sketch", placement="xy").data["sketch"]["id"]
    checkpoint = run(nx, "nx_create_checkpoint", label="open sketch")
    assert checkpoint.ok, checkpoint
    assert run(nx, "nx_finish_sketch").ok and run(nx, "nx_set_test_model").ok
    restored = run(nx, "nx_restore_checkpoint", checkpoint_id="open sketch")
    assert restored.ok, restored
    assert parking.parked(services(nx), nx.part) is nx.part.Sketches[-1]
    assert run(nx, "nx_get_sketch", sketch=sk, include=["geometry"]).data["parked"] is True


def test_a_finish_undone_after_another_sketch_was_opened_parks_the_first():
    nx = sketch_nx()
    first = run(nx, "nx_create_sketch", placement="xy").data["sketch"]["id"]
    assert run(nx, "nx_finish_sketch").ok
    second = run(nx, "nx_create_sketch", placement="xz")
    assert second.ok and second.data["parked"] is True
    assert run(nx, "nx_finish_sketch").ok
    undo = run(nx, "nx_undo_operation", steps=3)  # both finishes and the second sketch
    assert undo.ok, undo
    (sketch,) = list(nx.part.Sketches)
    assert parking.parked(services(nx), nx.part) is sketch
    assert run(nx, "nx_get_sketch", sketch=first, include=["geometry"]).data["parked"] is True


def test_undoing_a_batch_that_finished_the_parked_sketch_parks_it_again():
    nx = sketch_nx()
    sk = run(nx, "nx_create_sketch", placement="xy").data["sketch"]["id"]
    steps = [
        {"tool": "nx_create_sketch_curves", "arguments": {"sketch": sk, "curves": RECT}},
        {"tool": "nx_finish_sketch", "arguments": {"sketch": sk}},
    ]
    batch = run(nx, "nx_run_batch", operations=steps)
    assert batch.ok and not parking.open_in(services(nx), nx.part), batch
    assert run(nx, "nx_undo_operation").ok
    assert parking.parked(services(nx), nx.part) is nx.part.Sketches[-1]


ACTIVATING = (
    "nx_create_sketch_curves", "nx_edit_sketch_curves", "nx_create_sketch_constraints",
    "nx_create_sketch_dimensions", "nx_create_sketch_corner", "nx_create_sketch_copy",
    "nx_create_sketch_projection", "nx_finish_sketch", "nx_check_sketch",
)  # fmt: skip


@pytest.mark.parametrize("name", ACTIVATING)
def test_every_tool_that_activates_an_existing_sketch_is_display_bound(name):
    """NX 2406 died (General Fault Exception) activating a sketch with a dimension or an
    offset annotation in a part that was not displayed: whatever the sketch holds, the
    tools that activate one display its part (kit.sketch)."""
    assert catalogue.get(name).switch_for({}) == "display"


def test_get_sketch_displays_only_when_it_activates_the_sketch():
    spec, hidden = catalogue.get("nx_get_sketch"), {"services": object(), "part": "b"}
    assert spec.switch_for({"include": ["constraints"]}, **hidden) == "work"
    for include in (None, ["geometry"], ["status"], ["constraints", "status"]):
        assert spec.switch_for({"include": include}, **hidden) == "display"
    import_spec = catalogue.get("nx_import_model")
    assert import_spec.switch_for({"target": "sketch", "sketch": "s:1"}) == "display"
    assert import_spec.switch_for({"target": "work_part"}) == "work"


def test_an_offset_copy_in_a_background_parts_sketch_runs_displayed_then_gives_it_back():
    nx, a, b = two()
    made = run(nx, "nx_create_sketch", placement="xy", part="work", curves=RECT)
    assert made.ok, made
    ref, base = made.data["sketch"]["id"], made.data["curves"][0]["id"]
    parts = nx.session.Parts
    parts.Display = parts.Work = b  # the engineer now looks at b: work.prt is background
    log = services(nx).switch_log
    assert run(nx, "nx_get_sketch", sketch=ref, include=["constraints"]).ok
    assert (log()[-1]["mode"], log()[-1]["primitive"]) == ("work", "quiet")
    (sketch,) = list(a.Sketches)
    shown = []

    def offset(builder):
        shown.append(parts.Display)
        d = float(builder.Distance.RightHandSide)
        flipped = bool(builder.ReverseOffsetDirectionOfChain.calls)
        nx.line(sketch, (0, -d if flipped else d, 0), (30, -d if flipped else d, 0))

    nx.on_commit("SketchOffsetBuilder", offset)
    copy = run(nx, "nx_create_sketch_copy", sketch=ref, curves=[base], mode="offset", distance=3)
    assert copy.ok and copy.part["file"] == "work.prt", copy
    assert shown == [a]  # NX activated the sketch with its part displayed
    assert (log()[-1]["mode"], log()[-1]["primitive"]) == ("display", "display")
    assert (parts.Display, parts.Work) == (b, b)  # the engineer's tab is back
    done = run(nx, "nx_finish_sketch", part="work")  # the part's parked sketch, by default
    assert done.ok and done.data["was_active"] is True, done
    assert log()[-1]["mode"] == "display" and (parts.Display, parts.Work) == (b, b)


def test_a_display_bound_predicate_is_asked_only_for_a_hidden_target_and_fails_safe():
    import dataclasses

    spec = catalogue.get("nx_finish_sketch")
    asked = []

    def boom(services, part, args):
        asked.append(part)
        raise RuntimeError("cannot tell")

    bound = dataclasses.replace(spec, display_bound=(boom,))
    assert bound.switch_for({}) == spec.switch  # no services: not asked
    assert bound.switch_for({}, services=object(), part="b") == "display"  # raised: safe
    assert asked == ["b"]


def test_finishing_a_background_parts_sketch_never_turns_the_shown_view():
    """The finish reorients the view only on the displayed part (NX 2406 raised a General
    Fault Exception finishing an inch part's sketch while a mm part was displayed)."""

    from nx_mcp.domains.sketch import sketches

    nx, a, b = two()
    seen = []
    session = nx.session
    for owner, expected in ((b, False), (a, True)):
        sk = NS(OwningPart=owner, Deactivate=lambda r, _u: seen.append(r))
        tx = NS(nx=nx.nx, session=NS(Parts=session.Parts, ActiveSketch=None))
        sketches._deactivate(tx, sk)
        assert seen[-1] is expected
