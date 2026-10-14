"""The sidecar checks every step of a batch or preview against its own tool's schema, so a
malformed step fails at once (nothing is sent to NX); anchors pass where refs do."""

from unittest.mock import AsyncMock

import pytest

from nx_mcp.core import catalogue, target
from nx_mcp.core.validate import child_problems, child_steps
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace
from tests.sidecar.test_call_keys import answer, every_tool_tested  # noqa: F401  (fixture)

SKETCH = {"tool": "nx_create_sketch", "arguments": {"placement": {"principal": "xy"}}}


def _curves(sketch):
    return {
        "tool": "nx_create_sketch_curves",
        "arguments": {
            "sketch": sketch,
            "curves": [{"type": "line", "start": [0, 0], "end": [1, 0]}],
        },
    }


def _bridge():
    bridge = AsyncMock()
    bridge.call.return_value = answer("nx_run_batch", "model")
    return bridge


async def _invoke(tmp_path, bridge, tool, arguments):
    server = create_server(bridge, Workspace(tmp_path), surface="agent", build="v2406")
    result = await server.call_tool("nx_invoke", {"tool": tool, "arguments": arguments})
    return result.structuredContent


@pytest.mark.usefixtures("every_tool_tested")
async def test_a_malformed_step_is_refused_before_nx(tmp_path):
    bridge = _bridge()
    steps = [SKETCH, _curves("s:PLACEHOLDER"), {"tool": "nx_finish_sketch",
             "arguments": {"sketch": "$2.data.sketch"}}]  # fmt: skip
    body = await _invoke(tmp_path, bridge, "nx_run_batch", {"operations": steps})
    error = body["error"]
    assert body["mutation_outcome"] == "not_started" and error["code"] == "NX_INVALID_ARGUMENT"
    assert error["field"] == "/operations/1/arguments/sketch"
    assert error["message"].startswith("/operations/1/arguments/sketch: expected a sketch ref")
    assert error["details"]["child_index"] == 1
    assert error["details"]["child_tool"] == "nx_create_sketch_curves"
    fields = [row["field"] for row in error["details"]["errors"]]
    assert fields == ["/operations/1/arguments/sketch", "/operations/2/arguments/sketch"]
    assert "does not name an earlier step" in error["details"]["errors"][1]["message"]
    bridge.call.assert_not_called()


@pytest.mark.usefixtures("every_tool_tested")
async def test_valid_steps_with_placeholders_and_part_keys_are_sent(tmp_path):
    bridge = _bridge()
    steps = [SKETCH, _curves("$0.data.sketch")]
    steps[1]["arguments"]["part"] = "plate"  # a step's call key is the kernel's
    body = await _invoke(tmp_path, bridge, "nx_run_batch", {"operations": steps})
    assert body["ok"], body
    sent = bridge.call.call_args.args[1]["operations"]
    assert sent[1]["arguments"]["part"] == "plate"  # forwarded untouched


@pytest.mark.usefixtures("every_tool_tested")
async def test_an_out_of_range_step_value_and_an_unknown_step_tool(tmp_path):
    bridge = _bridge()
    bad = {"tool": "nx_create_sketch", "arguments": {"placement": {"principal": "xy"},
           "curves": []}}  # fmt: skip  # curves: 1-100 items
    body = await _invoke(tmp_path, bridge, "nx_run_batch", {"operations": [bad]})
    assert body["error"]["field"] == "/operations/0/arguments/curves", body
    unknown = {"tool": "nx_make_magic", "arguments": {}}
    await _invoke(tmp_path, bridge, "nx_run_batch", {"operations": [unknown]})
    bridge.call.assert_called_once()  # an unknown step tool: the host lists the batchable ones


@pytest.mark.usefixtures("every_tool_tested")
async def test_anchors_pass_gate_one_where_refs_do(tmp_path):
    bridge = AsyncMock()
    bridge.call.return_value = answer("nx_measure_mass")
    anchor = "@smoke/run/blk.prt:EXTRUDE(2)#0"
    body = await _invoke(tmp_path, bridge, "nx_measure_mass", {"bodies": [anchor]})
    assert body["ok"], body
    assert bridge.call.call_args.args[1]["bodies"] == [anchor]


def test_child_steps_find_the_tool_arguments_of_batch_and_preview():
    specs = catalogue.load()
    for name in ("nx_run_batch", "nx_preview_changes"):
        rows = child_steps(specs[name].params, {"operations": [SKETCH]})
        assert rows == [("operations", "arguments", 0, "nx_create_sketch", SKETCH["arguments"])]
    assert child_steps(specs["nx_create_sketch"].params, SKETCH["arguments"]) == []
    problem = child_problems(
        specs["nx_run_batch"].params, {"operations": [SKETCH]}, catalogue.get,
        target.split_call_keys,
    )  # fmt: skip
    assert problem is None
