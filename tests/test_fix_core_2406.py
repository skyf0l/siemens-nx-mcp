"""NX 2406 core regressions: provable not_started outcomes,
section delete clearing view sectioning, and solving an active sketch after a
dimension expression edit."""

import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from nx_mcp import side_effects
from nx_mcp.runtime import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace
from tests.fakes import Body


def _error(rig, method, params):
    with pytest.raises(NXToolError) as caught:
        rig.e.execute(method, params)
    return caught.value


# ---- 1. nx_create_part name conflicts: tests/unit/parts_files/test_parts.py ---


# ---- 2. not_started only when no side effect was possible --------------------
class NXList(list):
    """A C-implemented method owned by an NXOpen-module type, like NXOpen calls."""


NXList.__module__ = "NXOpen.Fake"


def _outcome(rig, handler):
    rig.e._handlers["nx_checkpoint"] = handler  # NON_MODEL: no undo mark
    error = _error(rig, "nx_checkpoint", {})
    assert sys.getprofile() is None
    return error.details["mutation_outcome"]


def _fail(*_):
    raise NXToolError("NX_INVALID_ARGUMENT", "bad")


def test_argument_errors_before_any_side_effect_are_not_started(rig):
    assert _outcome(rig, lambda: _fail()) == "not_started"
    assert _outcome(rig, lambda: float("not a number")) == "not_started"
    # A lookup that fails hands out no NX object.
    assert _outcome(rig, lambda: rig.e._resolve("obj_missing")) == "not_started"


def test_possible_side_effects_keep_the_outcome_unknown(rig, tmp_path):
    body = rig.ref(Body())
    assert _outcome(rig, lambda: _fail(rig.e.session)) == "unknown"
    assert _outcome(rig, lambda: _fail(rig.e._resolve(body))) == "unknown"
    assert _outcome(rig, lambda: _fail(NXList().append(1))) == "unknown"
    assert _outcome(rig, lambda: _fail((tmp_path / "f.txt").write_text("x"))) == "unknown"
    assert _outcome(rig, lambda: _fail((tmp_path / "f.txt").read_text())) == "not_started"


def test_nested_observers_mark_the_outer_call_touched():
    outer = side_effects.SideEffectObserver().start()
    inner = side_effects.SideEffectObserver().start()
    inner.stop()
    assert outer.touched
    outer.stop()
    assert sys.getprofile() is None


@pytest.mark.asyncio
async def test_sidecar_path_errors_are_not_started(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_MCP_DEV", "1")
    bridge = AsyncMock()
    server = create_server(bridge, Workspace(tmp_path), surface="full")
    result = await server.call_tool("nx_export_inspection_report", {"path": "../outside.zip"})
    assert result.structuredContent["error"]["code"] == "NX_PATH_OUTSIDE_WORKSPACE"
    assert result.structuredContent["mutation_outcome"] == "not_started"
    bridge.call.assert_not_called()
    bridge.call.side_effect = NXToolError("NX_BRIDGE_UNAVAILABLE", "lost")
    result = await server.call_tool("nx_export_inspection_report", {"path": "r.zip"})
    assert result.structuredContent["mutation_outcome"] == "unknown"


# ---- 3. section delete turning sectioning off: tests/unit/display/test_sections.py ----


# ---- 4. an expression edit solves the active sketch ------------------------
def _sketch_with_dimension(rig, measured, solved):
    exp = NS(Tag=7, Name="p2", Value=50.0, GetReferencingExpressions=lambda: [])
    dimension = NS(AssociatedDimension=NS(ComputedSize=measured))
    sketch = Mock()
    sketch.GetAllConstraintsOfType.return_value = [
        NS(AssociatedExpression=exp, AssociatedDimension=dimension.AssociatedDimension)
    ]
    sketch.LocalUpdate.side_effect = lambda: setattr(
        dimension.AssociatedDimension, "ComputedSize", solved
    )
    rig.session.ActiveSketch = sketch
    rig.nx.Sketch = NS(ConstraintClass=NS(Dimension=1), ConstraintType=NS(NoCon=0))
    return exp, sketch


def test_expression_edit_solves_the_active_sketch(rig):
    exp, sketch = _sketch_with_dimension(rig, measured=40.0, solved=50.0)
    rig.e._solve_active_sketch([exp])
    sketch.LocalUpdate.assert_called_once()
    sketch.Update.assert_called_once()


def test_expression_edit_rejects_an_active_sketch_that_does_not_reach_the_value(rig):
    exp, _ = _sketch_with_dimension(rig, measured=40.0, solved=40.0)
    with pytest.raises(NXToolError) as caught:
        rig.e._solve_active_sketch([exp])
    assert caught.value.code == "NX_SKETCH_CONFLICT"
    assert caught.value.details["measured"] == 40.0


def test_expression_edit_ignores_unrelated_active_sketches(rig):
    exp, sketch = _sketch_with_dimension(rig, measured=40.0, solved=50.0)
    rig.e._solve_active_sketch([NS(Tag=99, GetReferencingExpressions=lambda: [])])
    sketch.LocalUpdate.assert_not_called()
    del exp
