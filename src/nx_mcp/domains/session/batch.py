"""``nx_run_batch``: 1-100 batchable calls under one undo mark (tool-surface 3.1).

Only tools marked ``batchable`` run (decision 6: sketch geometry, constraints and
dimensions, annotations, component placement); heavy features stay one call each. Every step is checked before the
first one runs: an unknown or non-batchable tool, arguments that break the
tool's schema or a ``"$n.path"`` reference to a step that has not run yet refuse
the batch with nothing changed.

Each step then runs through the kernel's child gates (``Tx.run`` /
``kernel.run_child``) under this call's one mark and receipt. A step acts on the ``part``
call key in its arguments, else the part its refs name, else the batch's part; one mark
rolls back every part (NX's undo is session-wide). Under a batch given ``part``, a step
without a ``part`` of its own is checked against the batch's part as a top-level call is:
refs of another part are ``NX_WRONG_PART_CONTEXT`` (the step names its own ``part`` to
act there). Without ``part`` the batch's own part is the one part every step names (refs,
``part`` or an earlier step's result), else the usual rule (the work part, or
``NX_AMBIGUOUS_PART`` with two or more parts loaded). A child's Ref field
may name an earlier step's result, ``"$0.data.sketch"`` (0-based). Before each
step ``tx.checkpoint`` writes progress to the receipt and honours
``nx_cancel_operation`` (the receipt's ``.cancel`` file) and the cost budget. A
failing or cancelled step raises; the kernel rolls the whole batch back and the
error names the step (``details.child_index``, ``field`` under
``/operations/<i>``). Repeating a call with the same ``operation_id`` replays it.
"""

from __future__ import annotations

import difflib
import re
from typing import Annotated, NoReturn, TypedDict

from nx_mcp.core import catalogue
from nx_mcp.core import target as targets
from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, classify, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Arguments, Doc, Items, Text
from nx_mcp.core.validate import at_child, pointer

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
    return at_child(error, index)


def _admitted(spec, mode: str) -> bool:
    return spec.runs == "nx" and (spec.batchable if mode == "batch" else spec.previewable)


def admitted_tools(registry, mode: str) -> list[str]:
    """Names of the tools a batch (``mode`` ``"batch"``) or a preview runs."""
    return sorted(n for n, s in registry.items() if _admitted(s, mode))


def _unknown(name: str, registry, mode: str) -> NoReturn:
    near = difflib.get_close_matches(name, admitted_tools(registry, mode), n=3, cutoff=0.6)
    hint = f"Did you mean {', '.join(near)}?" if near else f"details.{mode}able lists them"
    fail("NX_UNKNOWN_TOOL", f"Unknown tool {name}", field="/tool", hint=hint,
         details={f"{mode}able": admitted_tools(registry, mode)})  # fmt: skip


def _refuse(spec, registry, mode: str) -> NoReturn:
    """``NX_UNSUPPORTED_OPTION`` naming what to do instead and the admitted tools."""
    name, names = spec.name, admitted_tools(registry, mode)
    if mode == "batch":
        why = "a batch runs light steps only (sketch, annotation, placement, rename, delete)"
        if spec.previewable:
            hint = (
                f"Call {name} on its own after the batch, or send all the steps to "
                "nx_preview_changes, then nx_finish_preview(action='accept') applies them "
                "under one undo mark. Batchable: " + ", ".join(names)
            )
        else:
            hint = f"Call {name} on its own. Batchable: " + ", ".join(names)
    else:
        why = "a preview runs model changes only (features, sketches, placement, renames)"
        hint = f"Call {name} on its own; details.previewable lists the tools a preview runs"
    fail("NX_UNSUPPORTED_OPTION", f"{name} cannot run in a {mode}: {why}", field="/tool",
         hint=hint, details={f"{mode}able": names})  # fmt: skip


def _preflight(operations: list[Step], mode: str = "batch", services=None) -> list:
    """Every step's spec, checked before any step runs; raise on the first problem.

    ``mode`` ``"batch"`` admits batchable tools, ``"preview"`` previewable ones
    (``nx_preview_changes``). A step's ``part`` call key must name a loaded part
    (``services``)."""
    registry = catalogue.load()
    specs = []
    for index, step in enumerate(operations):
        name = step["tool"]
        try:
            spec = registry.get(name)
            if spec is None:
                _unknown(name, registry, mode)
            if not _admitted(spec, mode):
                _refuse(spec, registry, mode)
            arguments, keys = targets.split_call_keys(spec, step["arguments"])
            if "part" in keys:
                targets.check_call_keys(keys["part"])
                if services is not None:
                    targets.part_from_text(services, keys["part"])
            checked = spec.check(arguments, placeholders=True)
            for path, text in checked.placeholders:
                step_ref = re.match(r"^\$([0-9]+)", text)  # validate matched PLACEHOLDER_PATTERN
                if step_ref is not None and int(step_ref.group(1)) >= index:
                    fail("NX_INVALID_ARGUMENT",
                         f"{pointer(path)}: {text} does not name an earlier step",
                         field=pointer(path),
                         hint="Refer only to steps before this one ($0 is the first)")  # fmt: skip
        except Exception as exc:
            raise _at(index, classify(exc), name) from exc
        specs.append(spec)
    return specs


def steps_part(services, args) -> object:
    """Gate 3t of a batch or preview without ``part``: the one part its steps name (refs,
    a step's ``part``, or an earlier step's result), else None (the usual rule). A
    ``part`` given to the batch wins (``yields_to_part``)."""
    found: dict = {}
    for step in args.get("operations") or []:
        spec = catalogue.get(str(step.get("tool"))) if isinstance(step, dict) else None
        if spec is None or not isinstance(step.get("arguments"), dict):
            return None
        arguments, keys = targets.split_call_keys(spec, step["arguments"])
        if isinstance(keys.get("part"), str):
            part = targets.part_from_text(services, keys["part"])
            found[services.part_key(part)] = part
            continue
        checked = spec.check(arguments, placeholders=True)
        pinned = targets.pinned_parts(services, checked, spec.target)
        if len(pinned) == 1:
            key, (part, *_rest) = next(iter(pinned.items()))
            found[key] = part
        elif pinned or not checked.placeholders:
            return None  # a step across parts, or a step that names no part
    return next(iter(found.values())) if len(found) == 1 else None


steps_part.yields_to_part = True  # type: ignore[attr-defined]


@tool(
    target=steps_part,
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
    """Run 1-100 light calls (sketch, annotation, placement, rename) under one undo mark;
    a failure undoes all. "$0.data.sketch" names step 0's result. Heavy features (extrude):
    call alone, or preview then accept."""
    with tx.checks():  # a refused step changes nothing
        _preflight(operations, services=tx.services)
    total = len(operations)
    tx.checkpoint(done=0, total=total)
    results: list[StepResult] = []
    try:
        for index, step in enumerate(operations):
            child = tx.run(step["tool"], step["arguments"], mode="batch")
            results.append({"index": index, **child})
        tx.checkpoint(done=total)
    except NXError as error:
        if error.code == "NX_CANCELLED":
            error.details.setdefault("completed_before_rollback", len(tx.children))
        raise
    return {"results": results, "count": len(results)}
