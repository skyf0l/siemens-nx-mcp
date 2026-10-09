"""NX 2406 core regressions: part name conflicts, provable not_started outcomes,
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
from tests.test_display_lifecycle import section_builder


def _error(rig, method, params):
    with pytest.raises(NXToolError) as caught:
        rig.e.execute(method, params)
    return caught.value


# ---- 1. nx_create_part refuses NX name conflicts before NX is touched -------
def _guard_file_new(rig):
    rig.session.Parts.FileNew = Mock(side_effect=AssertionError("NX FileNew must not run"))
    return list(rig.session.Parts)


def test_create_part_refuses_an_existing_file(rig, tmp_path):
    loaded = _guard_file_new(rig)
    target = tmp_path / "smoke" / "existing.prt"
    target.parent.mkdir()
    target.write_bytes(b"part")
    error = _error(rig, "nx_create_part", {"path": str(target)})
    assert error.code == "NX_FILE_EXISTS"
    assert error.details["mutation_outcome"] == "not_started"
    assert list(rig.session.Parts) == loaded and target.read_bytes() == b"part"


def test_create_part_refuses_a_loaded_basename_in_another_folder(rig, tmp_path):
    loaded = _guard_file_new(rig)
    target = tmp_path / "other" / "TEST.prt"  # rig's loaded part is test.prt
    error = _error(rig, "nx_create_part", {"path": str(target)})
    assert error.code == "NX_NAME_CONFLICT"
    assert error.details["loaded_part_path"] == rig.part.FullPath
    assert error.details["mutation_outcome"] == "not_started"
    assert list(rig.session.Parts) == loaded and not target.parent.exists()


def test_create_part_refuses_an_unsaved_loaded_part_at_the_same_path(rig, tmp_path):
    _guard_file_new(rig)
    error = _error(rig, "nx_create_part", {"path": str(tmp_path / "test.prt")})
    assert error.code == "NX_FILE_EXISTS"
    assert error.details["loaded_part_path"] == rig.part.FullPath


# ---- 2. not_started only when no side effect was possible --------------------
class NXList(list):
    """A C-implemented method owned by an NXOpen-module type, like NXOpen calls."""


NXList.__module__ = "NXOpen.Fake"


def _outcome(rig, handler):
    rig.e._handlers["nx_screenshot"] = handler  # NON_MODEL: no undo mark
    error = _error(rig, "nx_screenshot", {})
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


def test_real_non_model_handlers_report_not_started_for_argument_errors(rig, tmp_path):
    cases = [
        ("nx_inspection_report", {"path": str(tmp_path / "r.txt")}),
        ("nx_copy_project", {"path": str(tmp_path / "copy"), "prefix": "a b"}),
        ("nx_export_explosion_animation", {"explosion": "x", "path": "a.gif", "frames": 1}),
        ("nx_highlight_collisions", {"obj1": "obj_missing", "obj2": "obj_missing"}),
    ]
    for method, params in cases:
        error = _error(rig, method, params)
        assert error.details["mutation_outcome"] == "not_started", method
    body = rig.ref(rig.part.Bodies[0] if rig.part.Bodies else Body())
    error = _error(rig, "nx_highlight_collisions", {"obj1": body, "obj2": body})
    assert error.details["mutation_outcome"] == "unknown"


def test_nested_observers_mark_the_outer_call_touched():
    outer = side_effects.SideEffectObserver().start()
    inner = side_effects.SideEffectObserver().start()
    inner.stop()
    assert outer.touched
    outer.stop()
    assert sys.getprofile() is None


@pytest.mark.asyncio
async def test_sidecar_path_errors_are_not_started(tmp_path):
    bridge = AsyncMock()
    server = create_server(bridge, Workspace(tmp_path))
    result = await server.call_tool(
        "nx_component_action",
        {"component": "obj_x", "action": "replace", "part_path": "../outside.prt"},
    )
    assert result.structuredContent["code"] == "NX_PATH_OUTSIDE_WORKSPACE"
    assert result.structuredContent["details"]["mutation_outcome"] == "not_started"
    bridge.call.assert_not_called()
    bridge.call.side_effect = NXToolError("NX_BRIDGE_UNAVAILABLE", "lost")
    result = await server.call_tool("nx_save_as", {"path": "copy.prt"})
    assert result.structuredContent["details"]["mutation_outcome"] == "unknown"


# ---- 3. deleting the last section switches view sectioning off --------------
def test_deleting_the_last_section_turns_view_sectioning_off(rig):
    section_builder(rig)
    ref = rig.e._section_view([1, 2, 3], [0, 0, 1])["object"]["id"]
    view = rig.part.ModelingViews.WorkView
    assert view.DisplaySectioningToggle
    rig.e._section_control(ref, "delete")
    assert not rig.part.DynamicSections and not view.DisplaySectioningToggle


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
