"""Gate 4 ``no_active_sketch`` and drafting sketches (core/kernel.py, kit.sketch).

NX 2406 re-activates a sheet's last sheet sketch whenever the sheet is opened from the
modeling display. That drafting sketch does not block model tools: the gate deactivates
it quietly (no view change, model update) before the call's own work. A model sketch
still refuses with NX_SKETCH_ACTIVE.
"""

from __future__ import annotations

from typing import TypedDict

import pytest

from nx_mcp.core import catalogue
from nx_mcp.core.catalogue import make_spec
from nx_mcp.core.tx import Tx
from nx_mcp.kit import sketch
from nx_mcp.kit.result import Row
from tests.kit import fake_nx, run

SEEN: list = []


class Touched(TypedDict):
    part: Row


def nx_set_test_model(tx: Tx) -> Touched:
    """A model tool that needs no active sketch (like every modeling tool)."""
    SEEN.append(tx.session.ActiveSketch)
    return {"part": tx.ref(tx.part, "part")}


TOUCH = make_spec(
    nx_set_test_model, effect="model", label="Test model", keywords=("a", "b", "c"),
    domain="modeling", requires=("work_part", "no_active_sketch"),
)  # fmt: skip


@pytest.fixture(autouse=True)
def _tools(monkeypatch):
    catalogue.load()
    monkeypatch.setitem(catalogue._REGISTRY, TOUCH.name, TOUCH)
    SEEN.clear()


def _active(nx, drafting: bool):
    sk = nx.sketch()
    sk.IsDraftingSketch = drafting
    calls: list = []
    deactivate = sk.Deactivate

    def record(*args):
        calls.append(args)
        deactivate(*args)

    sk.Deactivate = record
    sk.Activate()
    return sk, calls


def test_an_active_drafting_sketch_is_released_and_the_model_tool_runs():
    nx = fake_nx()
    sk, calls = _active(nx, drafting=True)
    assert nx.session.ActiveSketch is sk
    done = run(nx, TOUCH.name)
    assert done.ok and done.mutation_outcome == "committed", done
    assert SEEN == [None]  # deactivated before the handler ran
    assert nx.session.ActiveSketch is None
    reorient, level = nx.nx.Sketch.ViewReorient.FalseValue, nx.nx.Sketch.UpdateLevel.Model
    assert calls == [(reorient, level)]  # quietly: no view change, model update


def test_an_active_model_sketch_still_refuses():
    nx = fake_nx()
    sk, calls = _active(nx, drafting=False)
    refused = run(nx, TOUCH.name)
    assert not refused.ok and refused.error["code"] == "NX_SKETCH_ACTIVE", refused
    assert refused.mutation_outcome == "not_started" and SEEN == []
    assert nx.session.ActiveSketch is sk and calls == []


def test_a_drafting_sketch_that_does_not_deactivate_still_refuses():
    nx = fake_nx()
    sk, _calls = _active(nx, drafting=True)

    def stuck(*_args):
        raise RuntimeError("NX refused")

    sk.Deactivate = stuck
    refused = run(nx, TOUCH.name)
    assert not refused.ok and refused.error["code"] == "NX_SKETCH_ACTIVE", refused
    assert nx.session.ActiveSketch is sk


def test_release_drafting_sketch_leaves_a_model_sketch_and_no_sketch_alone():
    nx = fake_nx()
    assert sketch.release_drafting_sketch(nx.session, nx.nx) is False
    model, _ = _active(nx, drafting=False)
    assert sketch.release_drafting_sketch(nx.session, nx.nx) is False
    assert nx.session.ActiveSketch is model
