"""``nx_run_batch``: 1-100 batchable calls under one undo mark (tool-surface 3.1).

It replaces the legacy ``nx_batch``. Only tools marked ``batchable`` run
(decision 6: sketch geometry, constraints and dimensions, annotations, component
placement); heavy features stay one call each. Every step is checked before the
first one runs: an unknown or non-batchable tool, arguments that break the
tool's schema or a ``"$n.path"`` reference to a step that has not run yet refuse
the batch with nothing changed.

Each step then runs through the kernel's child gates (``Tx.run`` /
``kernel.run_child``) under this call's one mark and receipt. A child's Ref field
may name an earlier step's result, ``"$0.data.sketch"`` (0-based). Before each
step ``tx.checkpoint`` writes progress to the receipt and honours
``nx_cancel_operation`` (the receipt's ``.cancel`` file) and the cost budget. A
failing or cancelled step raises; the kernel rolls the whole batch back and the
error names the step (``details.child_index``, ``field`` under
``/operations/<i>``). Repeating a call with the same ``operation_id`` replays it.
"""

from __future__ import annotations

import re
from typing import Annotated, TypedDict

from nx_mcp.core import catalogue
from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, classify, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Arguments, Doc, Items, Text
from nx_mcp.core.validate import pointer, validate

MAX_STEPS = 100


class Step(TypedDict):
    tool: Annotated[str, Text(64, r"^nx_[a-z0-9_]+$"), Doc("A batchable catalogue tool")]
    arguments: Arguments


class StepResult(TypedDict, total=False):
    index: int  # 0-based position in operations
    tool: str
    data: dict  # the tool's own result data
    frame: str | None
    changes: dict  # created / modified / deleted by this step
    units: dict  # the length unit of this step's numbers


class BatchData(TypedDict):
    results: list[StepResult]
    count: int


def _summary(args: dict, say) -> str:
    operations = args.get("operations") or []
    count = len(operations)
    first = operations[0].get("tool") if operations and isinstance(operations[0], dict) else None
    spec = catalogue.get(first) if isinstance(first, str) else None
    head = f"Batch of {count} step{'s' if count != 1 else ''}"
    if spec is None:
        return head
    return f"{head}: {spec.label}" + (f" (+{count - 1} more)" if count > 1 else "")


def _at(index: int, error: NXError, tool_name: str | None) -> NXError:
    """Point ``error`` at step ``index`` (as ``kernel.run_child`` does for a child)."""
    error.details.setdefault("child_index", index)
    if tool_name:
        error.details.setdefault("child_tool", tool_name)
    if error.field == "/tool":
        error.field = f"/operations/{index}/tool"
    elif error.field and not error.field.startswith("/operations/"):
        error.field = f"/operations/{index}/arguments{error.field}"
    return error


def _preflight(operations: list[dict], mode: str = "batch") -> list:
    """Every step's spec, checked before any step runs; raise on the first problem.

    ``mode`` ``"batch"`` admits batchable tools, ``"preview"`` previewable ones
    (``nx_preview_changes``)."""
    registry = catalogue.load()
    specs = []
    for index, step in enumerate(operations):
        name = step["tool"]
        try:
            spec = registry.get(name)
            if spec is None:
                fail("NX_UNKNOWN_TOOL", f"Unknown tool {name}", field="/tool",
                     hint="Find batchable tools with nx_find_tools")  # fmt: skip
            admitted = spec.batchable if mode == "batch" else spec.previewable
            if not admitted or spec.runs != "nx":
                fail("NX_UNSUPPORTED_OPTION", f"{name} cannot run in a {mode}", field="/tool",
                     hint=f"Only {mode}able tools run here; find them with nx_find_tools")  # fmt: skip
            checked = validate(spec.params, step["arguments"], placeholders=True)
            for path, text in checked.placeholders:
                if int(re.match(r"^\$([0-9]+)", text).group(1)) >= index:
                    fail("NX_INVALID_ARGUMENT",
                         f"{pointer(path)}: {text} does not name an earlier step",
                         field=pointer(path),
                         hint="Refer only to steps before this one ($0 is the first)")  # fmt: skip
        except Exception as exc:
            raise _at(index, classify(exc), name) from exc
        specs.append(spec)
    return specs


@tool(
    effect="model",
    label="Run batch",
    summary=_summary,
    keywords=(
        "batch",
        "several steps",
        "multiple operations",
        "one undo",
        "atomic",
        "sequence of calls",
    ),  # fmt: skip
    requires=("work_part",),
    cost="heavy",
    refresh_annotations=True,
    example={
        "operations": [
            {"tool": "nx_create_sketch", "arguments": {"placement": {"principal": "xy"}}},
            {"tool": "nx_finish_sketch", "arguments": {"sketch": "$0.data.sketch"}},
        ]
    },
)
def nx_run_batch(
    tx: Tx,
    *,
    operations: Annotated[list[Step], Items(1, MAX_STEPS)],
) -> BatchData:
    """Run 1-100 batchable tool calls in order under one undo mark; any failure undoes all.
    A Ref argument may name an earlier step's result: "$0.data.sketch" (0-based). Heavy
    features are not batchable; call them on their own."""
    with tx.checks():  # a refused step changes nothing
        _preflight(operations)
    total = len(operations)
    tx.checkpoint(done=0, total=total)
    results: list[StepResult] = []
    try:
        for index, step in enumerate(operations):
            child = tx.run(step["tool"], step["arguments"], mode="batch")
            results.append({"index": index, **child})  # type: ignore[typeddict-item]
        tx.checkpoint(done=total)
    except NXError as error:
        if error.code == "NX_CANCELLED":
            error.details.setdefault("completed_before_rollback", len(tx.children))
        raise
    return {"results": results, "count": len(results)}
