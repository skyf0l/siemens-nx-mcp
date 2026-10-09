"""nx_run_batch on the fake kit: one mark, $n references, preflight, rollback, cancel."""

import json

from nx_mcp.core import catalogue
from tests.kit import run
from tests.kit.kernel import services
from tests.unit.sketch.fakes import sketch_nx

LINE = {"type": "line", "start": [0, 0], "end": [10, 0]}
SKETCH = {"tool": "nx_create_sketch", "arguments": {"placement": {"principal": "xy"}}}


def curves(sketch="$0.data.sketch", *items):
    return {"tool": "nx_create_sketch_curves", "arguments": {"sketch": sketch, "curves": list(items) or [LINE]}}  # fmt: skip


def _ops_dir(nx):
    return services(nx).root / ".nx-mcp" / "operations"


def test_sketch_curves_and_finish_through_placeholders_under_one_mark():
    nx = sketch_nx()
    marks = len(nx.undo_marks)
    ops = [
        SKETCH,
        curves(),
        curves("$0.data.sketch", {"type": "line", "start": [10, 0], "end": [10, 5]}),
        {"tool": "nx_finish_sketch", "arguments": {"sketch": "$0.data.sketch"}},
    ]
    ok = run(nx, "nx_run_batch", operation_id="op_batch_ok", operations=ops)
    assert ok.ok, ok
    assert ok.mutation_outcome == "committed" and ok.data["count"] == 4
    results = ok.data["results"]
    assert [r["index"] for r in results] == [0, 1, 2, 3]
    assert [r["tool"] for r in results] == [o["tool"] for o in ops]
    sketch_id = results[0]["data"]["sketch"]["id"]
    assert results[3]["data"]["sketch"]["id"] == sketch_id
    assert results[3]["data"]["curve_count"] == 2
    assert len(nx.undo_marks) == marks + 1  # the batch's mark only; children add none
    receipt = json.loads((_ops_dir(nx) / "op_batch_ok.json").read_text())
    assert receipt["state"] == "committed"
    assert receipt["progress"]["completed"] == 4 and receipt["progress"]["total"] == 4


def test_same_operation_id_replays_and_other_arguments_conflict():
    nx = sketch_nx()
    first = run(nx, "nx_run_batch", operation_id="op_batch_once", operations=[SKETCH])
    assert first.ok and len(nx.part.Sketches) == 1
    again = run(nx, "nx_run_batch", operation_id="op_batch_once", operations=[SKETCH])
    assert again.ok and again.replayed is True and again.data == first.data
    assert len(nx.part.Sketches) == 1
    clash = run(nx, "nx_run_batch", operation_id="op_batch_once", operations=[SKETCH, SKETCH])
    assert clash.error["code"] == "NX_IDEMPOTENCY_CONFLICT"
    assert clash.mutation_outcome == "not_started" and len(nx.part.Sketches) == 1


def test_failing_child_rolls_back_the_whole_batch_and_names_the_step():
    nx = sketch_nx()
    bad = run(nx, "nx_run_batch", operations=[SKETCH, curves(), curves("$0.data.nope")])
    assert not bad.ok and bad.mutation_outcome == "rolled_back"
    assert bad.error["code"] == "NX_INVALID_ARGUMENT"
    assert bad.error["details"]["child_index"] == 2
    assert bad.error["details"]["child_tool"] == "nx_create_sketch_curves"
    assert bad.error["field"] == "/operations/2/arguments/sketch"
    assert len(nx.part.Sketches) == 0 and nx.session.ActiveSketch is None


def test_every_step_is_checked_before_anything_runs():
    nx = sketch_nx()
    extrude = {"tool": "nx_create_extrude", "arguments": {"sketch": "$0.data.sketch"}}
    refused = run(nx, "nx_run_batch", operations=[SKETCH, extrude])
    assert refused.error["code"] == "NX_UNSUPPORTED_OPTION"
    assert refused.error["field"] == "/operations/1/tool"
    assert refused.error["details"]["child_index"] == 1
    unknown = run(nx, "nx_run_batch", operations=[SKETCH, {"tool": "nx_nope", "arguments": {}}])
    assert unknown.error["code"] == "NX_UNKNOWN_TOOL"
    assert unknown.error["field"] == "/operations/1/tool"
    schema = run(nx, "nx_run_batch", operations=[SKETCH, curves("$0.data.sketch", {"type": "x"})])
    assert schema.error["code"] == "NX_INVALID_ARGUMENT"
    assert schema.error["field"].startswith("/operations/1/arguments/curves/0")
    forward = run(nx, "nx_run_batch", operations=[curves("$1.data.sketch"), SKETCH])
    assert forward.error["code"] == "NX_INVALID_ARGUMENT"
    assert forward.error["field"] == "/operations/0/arguments/sketch"
    for bad in (refused, unknown, schema, forward):
        assert bad.mutation_outcome == "not_started"
    assert len(nx.part.Sketches) == 0


def test_cancel_before_the_first_step_changes_nothing():
    nx = sketch_nx()
    _ops_dir(nx).mkdir(parents=True, exist_ok=True)
    (_ops_dir(nx) / "op_batch_cancel_1.cancel").touch()
    bad = run(nx, "nx_run_batch", operation_id="op_batch_cancel_1", operations=[SKETCH, curves()])
    assert bad.error["code"] == "NX_CANCELLED" and bad.mutation_outcome == "not_started"
    assert len(nx.part.Sketches) == 0


def test_cancel_between_steps_rolls_back_what_ran():
    nx = sketch_nx()
    spec = catalogue.load()["nx_create_sketch"]
    original = spec.handler

    def then_cancel(tx, **kw):
        out = original(tx, **kw)
        (_ops_dir(nx) / "op_batch_cancel_2.cancel").touch()
        return out

    object.__setattr__(spec, "handler", then_cancel)
    try:
        bad = run(
            nx, "nx_run_batch", operation_id="op_batch_cancel_2", operations=[SKETCH, curves()]
        )
    finally:
        object.__setattr__(spec, "handler", original)
    assert bad.error["code"] == "NX_CANCELLED" and bad.mutation_outcome == "rolled_back"
    details = bad.error["details"]
    assert details["child_index"] == 1 and details["completed_before_rollback"] == 1
    assert len(nx.part.Sketches) == 0
    receipt = json.loads((_ops_dir(nx) / "op_batch_cancel_2.json").read_text())
    assert receipt["state"] == "failed" and receipt["progress"]["completed"] == 1


def test_summary_counts_steps_and_names_the_first():
    spec = catalogue.load()["nx_run_batch"]
    from nx_mcp.core.summary import render

    assert (
        render(spec, {"operations": [SKETCH, curves()]})
        == "Batch of 2 steps: Create sketch (+1 more)"
    )
    assert render(spec, {"operations": [SKETCH]}) == "Batch of 1 step: Create sketch"
