"""Restorers the compensating undo uses for component poses and sheet-metal defaults
(``@restorer("nx_set_component_pose")``, ``@restorer("nx_set_sheet_metal_defaults")``):
an undo by deletion sets the before-values back (mode compensate here: the same path as
when other parts' later work is in the way), and refuses a value changed since."""

from __future__ import annotations

import pytest

from tests.kit import run
from tests.unit.assembly.fakes import assembly_nx, place
from tests.unit.sheet_metal.fakes import sm_nx

RZ90 = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]


def test_a_component_pose_is_set_back_by_compensation():
    nx = assembly_nx()
    b = place(nx, nx.blk, "B", (20, 0, 0))
    moved = run(nx, "nx_set_component_pose", component=b.ref, mode="absolute",
                translation=[30, 10, 0], rotation=RZ90)  # fmt: skip
    assert moved.ok, moved
    undo = run(nx, "nx_undo_operation", part="asm", mode="compensate")
    assert undo.ok and undo.data["compensated"], undo
    assert undo.data["restored"] == [f"the pose of {b.ref} back to [20.0, 0.0, 0.0]"]
    assert b.t == pytest.approx([20, 0, 0])
    assert [v for row in b.r for v in row] == pytest.approx([1, 0, 0, 0, 1, 0, 0, 0, 1])


def test_a_pose_moved_again_since_is_never_overwritten():
    nx = assembly_nx()
    b = place(nx, nx.blk, "B", (20, 0, 0))
    first = run(nx, "nx_set_component_pose", component=b.ref, mode="absolute",
                translation=[30, 0, 0])  # fmt: skip
    assert first.ok and run(nx, "nx_set_component_pose", component=b.ref, mode="relative",
                            translation=[5, 0, 0]).ok  # fmt: skip
    refused = run(nx, "nx_undo_operation", operation=first.operation_id, mode="compensate")
    assert refused.error["code"] == "NX_DEPENDENCY_ORDER", refused
    assert "moved since" in refused.error["message"] and b.t == pytest.approx([35, 0, 0])


def test_sheet_metal_defaults_are_set_back_by_compensation():
    nx = sm_nx()
    before = run(nx, "nx_get_sheet_metal_defaults").data
    done = run(nx, "nx_set_sheet_metal_defaults", thickness=2, neutral_factor=0.4)
    assert done.ok, done
    undo = run(nx, "nx_undo_operation", mode="compensate")
    assert undo.ok and undo.data["compensated"], undo
    assert undo.data["restored"][0].startswith("sheet-metal defaults back to")
    after = run(nx, "nx_get_sheet_metal_defaults").data
    assert after["thickness"] == before["thickness"] == 3
    assert after["neutral_factor"] == before["neutral_factor"]
    again = run(nx, "nx_set_sheet_metal_defaults", thickness=4)
    assert again.ok and run(nx, "nx_set_sheet_metal_defaults", thickness=5).ok
    refused = run(nx, "nx_undo_operation", operation=again.operation_id, mode="compensate")
    assert refused.error["code"] == "NX_DEPENDENCY_ORDER", refused
    assert run(nx, "nx_get_sheet_metal_defaults").data["thickness"] == 5
