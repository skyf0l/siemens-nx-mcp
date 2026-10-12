"""Parked sketches (core/parking.py, kit.sketch, domains/sketch/sketches.py): no sketch
stays active in NX between calls, an unfinished one stays its part's open sketch.

NX has one active sketch for the session and refuses model calls in every part while it
is active; a parked sketch blocks only its own part's model tools, as before.
"""

from __future__ import annotations

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


def test_finishing_a_background_parts_sketch_never_turns_the_shown_view():
    """The finish reorients the view only on the displayed part (NX 2406 raised a General
    Fault Exception finishing an inch part's sketch while a mm part was displayed)."""
    from types import SimpleNamespace as NS

    from nx_mcp.domains.sketch import sketches

    nx, a, b = two()
    seen = []
    session = nx.session
    for owner, expected in ((b, False), (a, True)):
        sk = NS(OwningPart=owner, Deactivate=lambda r, _u: seen.append(r))
        tx = NS(nx=nx.nx, session=NS(Parts=session.Parts, ActiveSketch=None))
        sketches._deactivate(tx, sk)
        assert seen[-1] is expected
