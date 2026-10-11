"""Undo and checkpoints of the agent's own work (tool-surface 3.1).

``nx_undo_operation`` (was ``nx_undo``), ``nx_create_checkpoint`` (``nx_checkpoint``),
``nx_list_checkpoints`` (``nx_checkpoint_state``) and ``nx_restore_checkpoint``
(``nx_rollback``) work on the services' undo history: the agent's committed model
calls with their NX undo marks, oldest first (``Services.undo_history``). The
engineer's own work is never in it, and the interactive host empties the history and
the checkpoints whenever the engineer changes the model, so neither tool reaches
across their edits.

Checkpoints are NX undo marks of this session only. A save or a part close removes
NX's marks (``saved_or_closed``), NX evicts its oldest marks beyond about 180 for the
whole session (``mark_evicted``), a display switch outside NX MCP may reset them
(``display_reset``), and an undo or a restore back past a checkpoint takes its state off
the model's timeline (``undone_past``). Loading a part keeps NX's marks, but undoing
across the load would unload the part again: a load by a file or session call is a
barrier in the history (``core/marks.py``), so undo stops there and a checkpoint before
it cannot be restored (``part_loaded``). An unavailable checkpoint stays listed with its
``expired_reason`` and cannot be restored. A restored checkpoint stays available
(NX keeps the mark it undid to), so "try, restore, try again" works. A restore takes
the checkpoint's id or its label. Undo and restore refuse to touch another part than
the work part and mark the undone calls' receipts ``reverted_by``: repeating such a
call (same operation_id) runs it again, it does not replay the reverted result.
``nx_redo_operation`` repeats them for the agent, next first, each as its own
operation with its receipt arguments; new committed work ends that redo list (NX 2406
offers NX Open no redo of its own). References follow NX's object identity: the refs of every object the
undo kept or restored (the part, components, untouched features, faces and edges) stay
valid; only refs of objects the undo removed go stale (``changes.deleted`` and
``changes.stale_refs``). Checkpoints are never dropped silently: an engineer edit or a
part close leaves them listed as expired.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.marks import MARK_CAP_TEXT
from nx_mcp.core.target import part_by_key
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Range, Text

CHECKPOINT_ID = r"^cp_[0-9a-f]{32}$"
CheckpointKey = Annotated[
    str,
    Text(80, r"^[^\x00-\x1f]{1,80}$"),
    Doc("checkpoint_id (cp_...) from nx_create/list_checkpoints, or its label"),
]
Label = Annotated[
    str, Text(80, r"^[^\x00-\x1f]{1,80}$"), Doc("Checkpoint name; nx_restore_checkpoint takes it")
]
Steps = Annotated[int, Range(ge=1), Doc("Operations to undo")]
RedoCount = Annotated[int, Range(ge=1), Doc("Undone operations to repeat, next first")]
ExpiredReason = Literal[
    "undone_past",
    "saved_or_closed",
    "engineer_change",
    "part_closed",
    "part_loaded",
    "display_reset",
    "mark_evicted",
]
# Why the undo history ends at an entry, or a checkpoint cannot be restored.
WHY = {
    "undone_past": "an undo or restore went back past it: its state is gone",
    "saved_or_closed": "NX dropped its undo marks (a save or a part close)",
    "engineer_change": "the engineer changed the model after it",
    "part_closed": "its part was closed",
    "part_loaded": "a part load that displayed the part dropped NX's undo marks",
    "display_reset": "a display part switch outside NX MCP dropped NX's undo marks",
    "mark_evicted": "NX dropped its undo mark: " + MARK_CAP_TEXT,
}
STALE_HINT = (
    "Create a new checkpoint with nx_create_checkpoint; nx_list_checkpoints shows "
    "which ones are available"
)
LISTED = 20  # checkpoints named in a not-found error


class RedoStep(TypedDict):
    # repeat with nx_invoke {"tool": tool, "arguments": arguments}, or nx_redo_operation
    tool: str  # the undone call's tool
    arguments: dict  # its arguments as received (receipt copy; long text cut)
    complete: bool  # false: the receipt cut long text or file content; rebuild those


class UndoData(TypedDict):
    undone_operation_id: str  # the latest call undone
    tool: str  # its tool
    undone: list[str]  # every operation id undone, oldest first
    undo_depth: int  # agent operations nx_undo_operation can still undo
    references: str  # preserved / listed (stale refs in changes); part_invalidated: re-list
    # The undone calls, oldest first: nx_redo_operation repeats them (steps=len(redo)), or
    # send each with nx_invoke {tool, arguments}. Refs the undone calls created are
    # stale; use the refs the repeats return
    redo: list[RedoStep]
    redo_depth: int  # calls nx_redo_operation can repeat now


class RedoneStep(TypedDict):
    operation_id: str  # the undone call's id, run again under it
    tool: str
    data: dict  # the call's result data
    changes: dict  # what this call created, modified and deleted (new refs)


class RedoData(TypedDict):
    redone: list[str]  # operation ids run again, in order
    results: list[RedoneStep]
    redo_depth: int  # undone calls nx_redo_operation can still repeat
    undo_depth: int  # agent operations nx_undo_operation can undo


class CheckpointData(TypedDict):
    checkpoint_id: str
    label: str
    undo_depth: int  # agent operations nx_undo_operation can undo (= nx_list_checkpoints)


class CheckpointRow(TypedDict):
    checkpoint_id: str
    label: str
    part_id: str
    available: bool  # False: not restorable, expired_reason says why
    expired_reason: ExpiredReason | None  # undone_past, part_loaded, mark_evicted...
    operations_after: int  # agent operations a restore would undo


class CheckpointsData(TypedDict):
    checkpoints: list[CheckpointRow]
    undo_depth: int  # agent operations nx_undo_operation can still undo
    last_operation: str | None  # the operation nx_undo_operation would undo


class RestoreData(TypedDict):
    checkpoint_id: str
    label: str
    # operation ids undone by the restore, oldest first; calls an earlier undo already
    # reverted are not in the history any more, so they are not listed again
    reverted: list[str]
    references: str  # preserved / listed (stale refs in changes); part_invalidated: re-list


def _mark_reverted(services, history: list, by: str) -> None:
    """Receipts of undone calls: a retry with their operation_id then runs the call
    again (core/receipts.py) instead of replaying its reverted result."""
    for item in history:
        with contextlib.suppress(Exception):
            record = services.store.get(item["operation_id"])
            if "fingerprint" in record:
                record["reverted_by"] = by
                services.store.put(record)


def _redo_steps(services, undone: list) -> list[RedoStep]:
    """The undone calls with their receipt arguments, oldest first: what to repeat to
    restore them. NX 2406's ``Session.Redo`` only redoes ``UndoLastNVisibleMarks``,
    which NX Open calls cannot use (measured: it undoes nothing from the host)."""
    steps: list[RedoStep] = []
    for item in undone:
        arguments: Any = {}
        with contextlib.suppress(Exception):
            arguments = services.store.get(item["operation_id"]).get("arguments") or {}
        text = repr(arguments)
        steps.append(
            {
                "tool": str(item.get("method") or ""),
                "arguments": arguments if isinstance(arguments, dict) else {},
                "complete": _complete(arguments, text),
            }
        )
    return steps


def _complete(arguments, text: str) -> bool:
    """Whether a receipt kept the arguments whole (no cut text or file content)."""
    return (
        isinstance(arguments, dict)
        and "omitted" not in arguments
        and "omitted>" not in text
        and "<file content omitted" not in text
    )


def _push_redo(services, reverted: list) -> None:
    """Put the reverted calls (oldest first) ahead of the redo list."""
    services.redo_steps()[:0] = [
        {
            "operation_id": str(h["operation_id"]),
            "method": str(h.get("method") or ""),
            "part_id": h.get("part_id"),
        }
        for h in reverted
    ]


def _undo_to(tx: Tx, mark) -> None:
    """Undo to ``mark``. Parts NX unloads with it (loaded by the undone calls: a component
    from a file, an import into a new part) are forgotten and named in a note."""
    services = tx.services
    before = {services.part_key(p): services.part_path(p) for p in services.parts()}
    services.undo_to(mark)
    now = {services.part_key(p) for p in services.parts()}
    gone = [key for key in before if key not in now]
    if gone:
        services.forget_parts(gone)
        tx.warn(
            "NX_NOTE",
            "The undo also unloaded "
            + ", ".join(before[key] for key in gone)
            + " (loaded by the undone operations); its refs are stale",
        )


def _drop_own_mark(tx: Tx) -> None:
    """The kernel's mark for this call lies after the restored mark: NX drops it with
    the undo; drop it here too, so it never enters the history."""
    mark: Any = tx.mark
    if mark is not None:
        with contextlib.suppress(Exception):
            tx.services.delete_mark(mark)


def _end(services, history: list) -> tuple[int, dict | None]:
    """``(undo depth, the entry that ends it)``: the latest agent operations whose NX undo
    marks still exist, back to a load barrier or a dropped mark (None: the start)."""
    depth = 0
    for item in reversed(history):
        if item.get("load") is not None or item.get("expired"):
            return depth, item
        if not services.mark_exists(item["mark"]):
            return depth, item
        depth += 1
    return depth, None


def _depth(services, history: list) -> int:
    """The latest agent operations whose NX undo marks still exist: what undo reaches."""
    return _end(services, history)[0]


def _loaded(item: dict) -> str:
    files = ", ".join(str(f) for f in item.get("load") or []) or "a part"
    return f"{files} was loaded after it ({item.get('method') or 'a load'})"


def _why_ended(item: dict) -> tuple[str, str]:
    """``(reason code, sentence)`` of the entry that ends the undo history."""
    if item.get("load") is not None:
        return "part_loaded", _loaded(item) + ": undoing across the load would unload it"
    reason = str(item.get("expired") or "mark_evicted")
    return reason, WHY.get(reason, WHY["mark_evicted"])


def _barrier_after(services, cp: dict) -> dict | None:
    """The first load barrier recorded after the checkpoint, if any."""
    later = services.undo_history()[int(cp["index"]) :]
    return next((h for h in later if h.get("load") is not None), None)


def _reason(services, cp: dict) -> ExpiredReason | None:
    """Why a checkpoint cannot be restored; None while it can. A mark gone without a
    reason the kernel saw (a save, a close, a load, a display switch) is one NX evicted."""
    if cp.get("expired"):
        return cp["expired"]
    if not services.mark_exists(cp["mark"]):
        return "mark_evicted"
    if _barrier_after(services, cp) is not None:
        return "part_loaded"
    return None


def _alive(services, checkpoints: dict) -> set[str]:
    return {k for k, cp in checkpoints.items() if _reason(services, cp) is None}


def _expire(services, checkpoints: dict, kept: int, alive: set[str]) -> None:
    """After an undo or restore down to ``kept`` operations: the checkpoints that were
    restorable and now lie ahead of it, or whose mark NX removed with the undo, are
    ``undone_past`` for good (NX Open has no redo)."""
    for key, cp in checkpoints.items():
        if key in alive and (cp["index"] > kept or not services.mark_exists(cp["mark"])):
            cp["expired"] = "undone_past"


def _label_key(text: str) -> str:
    return " ".join(text.split()).casefold()


def _lookup(services, checkpoints: dict, text: str) -> str:
    """The checkpoint named by id or by label (case and spacing ignored); of several
    with that label, the newest restorable one."""
    if text in checkpoints:
        return text
    wanted = _label_key(text)
    matches = [
        k for k, cp in checkpoints.items() if _label_key(str(cp.get("label") or "")) == wanted
    ]
    live = [k for k in matches if _reason(services, checkpoints[k]) is None]
    if matches:
        return (live or matches)[-1]
    listed = [
        {
            "checkpoint_id": k,
            "label": str(cp.get("label") or ""),
            "available": _reason(services, cp) is None,
        }
        for k, cp in checkpoints.items()
    ][-LISTED:]
    if re.match(CHECKPOINT_ID, text):
        fail(
            "NX_STALE_CHECKPOINT",
            "The checkpoint is not in this session",
            field="/checkpoint_id",
            hint="Closing its part or the engineer's own edits end checkpoints. " + STALE_HINT,
            details={"checkpoints": listed},
        )
    fail(
        "NX_NOT_FOUND",
        f"No checkpoint has the id or label {text!r}",
        field="/checkpoint_id",
        hint="Pass checkpoint_id from nx_create_checkpoint or nx_list_checkpoints, or "
        "a label from details.checkpoints",
        details={"checkpoints": listed},
    )


@tool(
    effect="model",
    label="Undo operation",
    summary="Undo the agent's last operation",
    keywords=("undo", "revert", "undo last", "take back", "cancel last change", "step back"),
    requires=("work_part",),
    cost="light",
    example={},
)
def nx_undo_operation(tx: Tx, *, steps: Steps = 1) -> UndoData:
    """Undo the agent's last steps operations (never the engineer's); nx_redo_operation
    repeats them. A save, part close or load, or engineer edit ends the history. Remaining
    objects keep their refs."""
    services = tx.services
    history = services.undo_history()
    depth, end = _end(services, history)
    reason, why = _why_ended(end) if end is not None else (None, "")
    ended = {"reason": reason} if reason else {}
    with tx.checks():
        if not depth:
            fail(
                "NX_NOT_FOUND",
                "No agent operation can be undone: "
                + (
                    f"the history ends at the agent's last operation: {why}"
                    if end is not None
                    else "the undo history is empty"
                ),
                hint="A save, a part close or load, or the engineer's own edits end the undo "
                "history; delete or edit the objects instead",
                details={"undo_depth": 0, **ended},
                retry="never",  # no fix makes the same call succeed
            )
        if steps > depth:
            fail(
                "NX_INVALID_ARGUMENT",
                f"steps {steps} is more than the {depth} agent operation(s) that can be undone"
                + (f" (the history ends there: {why})" if end is not None else ""),
                field="/steps",
                hint=f"Pass steps from 1 to {depth}; nx_list_checkpoints shows undo_depth",
                details={"undo_depth": depth, **ended},
            )
        undone = history[-steps:]
        key = services.part_key(tx.part)
        other = next((h for h in undone if h["part_id"] != key), None)
        if other is not None:
            fail(
                "NX_CROSS_PART",
                "The agent's last operation changed another part"
                if steps == 1
                else f"One of the agent's last {steps} operations changed another part",
                hint="Activate the part owning the latest operation, then undo",
                details={"part_id": other["part_id"], "operation_id": other["operation_id"]},
            )
    checkpoints = services.undo_checkpoints()
    alive = _alive(services, checkpoints)
    redo = _redo_steps(services, undone)
    _undo_to(tx, undone[0]["mark"])
    _drop_own_mark(tx)
    _mark_reverted(services, undone, tx.operation_id or "")
    del history[-steps:]
    references = tx.after_undo()
    _expire(services, checkpoints, len(history), alive)
    _push_redo(services, undone)
    last = undone[-1]
    return {
        "undone_operation_id": last["operation_id"],
        "tool": last.get("method") or "",
        "undone": [h["operation_id"] for h in undone],
        "undo_depth": _depth(services, history),
        "references": references,
        "redo": redo,
        "redo_depth": len(services.redo_steps()),
    }


@tool(
    effect="session",
    label="Create checkpoint",
    summary='Create undo checkpoint "{label}"',
    keywords=("checkpoint", "save point", "undo point", "restore point", "snapshot model"),
    requires=("work_part",),
    cost="light",
    example={"label": "before fillets"},
)
def nx_create_checkpoint(tx: Tx, *, label: Label = "checkpoint") -> CheckpointData:
    """Set an in-session undo checkpoint in the work part (restore by id or label). A
    save, part close, later part load or undo past it expires it; disk saves are never
    rolled back."""
    services = tx.services
    part = tx.part
    mark = services.set_mark("NX MCP checkpoint: " + label)
    key = "cp_" + uuid.uuid4().hex
    history = services.undo_history()
    services.undo_checkpoints()[key] = {
        "mark": mark,
        "part_id": services.part_key(part),
        "index": len(history),
        "label": label,
    }
    return {"checkpoint_id": key, "label": label, "undo_depth": _depth(services, history)}


@tool(
    effect="read",
    label="List checkpoints",
    summary="List undo checkpoints",
    keywords=("checkpoints", "undo history", "undo depth", "restore points"),
    cost="light",
    example={},
)
def nx_list_checkpoints(tx: Tx) -> CheckpointsData:
    """List this session's checkpoints and the agent's undo depth. Expired ones show
    available=false and expired_reason (undone_past, saved_or_closed, part_loaded,
    display_reset, mark_evicted, engineer_change or part_closed)."""
    services = tx.services
    history = services.undo_history()
    rows: list[CheckpointRow] = []
    for key, cp in services.undo_checkpoints().items():
        reason = _reason(services, cp)
        rows.append(
            {
                "checkpoint_id": key,
                "label": str(cp.get("label") or ""),
                "part_id": str(cp["part_id"]),
                "available": reason is None,
                "expired_reason": reason,
                "operations_after": 0 if reason else max(0, len(history) - int(cp["index"])),
            }
        )
    depth = _depth(services, history)
    return {
        "checkpoints": rows,
        "undo_depth": depth,
        "last_operation": history[-1]["operation_id"] if depth else None,
    }


def _checkpoint_part(services, args) -> Any:
    """Gate 3t: the part a checkpoint belongs to (None: unknown, the usual rules apply)."""
    text = args.get("checkpoint_id")
    if not isinstance(text, str):
        return None
    checkpoints = services.undo_checkpoints()
    try:
        key = _lookup(services, checkpoints, text)
    except NXError:
        return None  # the handler reports it
    return part_by_key(services, checkpoints[key].get("part_id"))


@tool(
    target=_checkpoint_part,
    effect="model",
    label="Restore checkpoint",
    summary="Roll back to an earlier checkpoint",
    keywords=("rollback", "restore checkpoint", "roll back", "revert to checkpoint"),
    requires=("work_part",),
    cost="light",
    example={"checkpoint_id": "before fillets"},
)
def nx_restore_checkpoint(tx: Tx, *, checkpoint_id: CheckpointKey) -> RestoreData:
    """Roll the work part back to a checkpoint (id or label), undoing the agent's
    operations since; it stays restorable. Refused when expired or across parts.
    Refs of objects that remain stay valid; save to persist."""
    services = tx.services
    checkpoints = services.undo_checkpoints()
    history = services.undo_history()
    with tx.checks():
        key = _lookup(services, checkpoints, checkpoint_id)
        cp: dict = checkpoints[key]
        reason = _reason(services, cp)
        if reason == "undone_past":
            fail("NX_STALE_CHECKPOINT",
                 "An undo or restore went back past this checkpoint: its state is gone",
                 field="/checkpoint_id", hint=STALE_HINT)  # fmt: skip
        if reason == "engineer_change":
            fail("NX_STALE_CHECKPOINT",
                 "The engineer changed the model after this checkpoint",
                 field="/checkpoint_id", hint=STALE_HINT)  # fmt: skip
        if reason == "saved_or_closed":
            fail("NX_STALE_CHECKPOINT",
                 "NX dropped the checkpoint's undo mark (a save or a part close)",
                 field="/checkpoint_id", hint=STALE_HINT)  # fmt: skip
        barrier = _barrier_after(services, cp) if reason == "part_loaded" else None
        if barrier is not None:
            fail("NX_STALE_CHECKPOINT",
                 f"{_loaded(barrier)}: restoring this checkpoint would unload it",
                 field="/checkpoint_id", hint="Delete or edit the objects instead",
                 details={"reason": reason, "loaded": barrier.get("load")})  # fmt: skip
        if reason:
            fail("NX_STALE_CHECKPOINT",
                 f"The checkpoint cannot be restored: {WHY.get(reason, reason)}",
                 field="/checkpoint_id", hint=STALE_HINT,
                 details={"reason": reason})  # fmt: skip
        part_key = services.part_key(tx.part)
        later = history[cp["index"] :]
        if cp["part_id"] != part_key or any(h["part_id"] != part_key for h in later):
            fail(
                "NX_CROSS_PART",
                "Restoring would change another part than the work part",
                hint="Activate the checkpoint's part, or undo the other part's operations first",
                details={"part_id": cp["part_id"]},
            )
    alive = _alive(services, checkpoints)
    _undo_to(tx, cp["mark"])
    _drop_own_mark(tx)
    _mark_reverted(services, later, key)
    del history[cp["index"] :]
    references = tx.after_undo()
    _expire(services, checkpoints, cp["index"], alive)
    _push_redo(services, later)
    return {
        "checkpoint_id": key,
        "label": str(cp.get("label") or ""),
        "reverted": [h["operation_id"] for h in later],
        "references": references,
    }


NOTHING_TO_REDO = (
    "nx_redo_operation repeats calls that nx_undo_operation or nx_restore_checkpoint "
    "reverted; new committed work or an engineer edit ends that list"
)


def _redo_arguments(services, item: dict, key) -> dict:
    """The receipt arguments to repeat ``item`` with; refuse when they cannot be."""
    operation = item["operation_id"]
    record = services.store.get(operation)
    if record.get("state") != "committed" or not record.get("reverted_by"):
        fail(
            "NX_NOT_FOUND",
            f"{operation} ({item['method']}) ran again since its undo "
            f"({record.get('state') or 'unknown'}): nothing of it is left to redo",
            hint=NOTHING_TO_REDO,
            details={"operation_id": operation},
            retry="never",
        )
    if item.get("part_id") != key:
        fail(
            "NX_CROSS_PART",
            f"{operation} ({item['method']}) changed another part than the work part",
            hint="Activate the part it changed, then redo",
            details={"part_id": item.get("part_id"), "operation_id": operation},
        )
    arguments = record.get("arguments")
    if not _complete(arguments, repr(arguments)):
        fail(
            "NX_UNSUPPORTED_OPTION",
            f"The receipt of {operation} ({item['method']}) cut long text or file content",
            hint="Repeat it with nx_invoke and its full arguments (nx_get_operation shows "
            "what the receipt kept)",
            details={"operation_id": operation},
        )
    return dict(arguments)


def _undo_redone(tx: Tx, start: int) -> None:
    """Undo the calls this redo committed (history from ``start``): all or nothing."""
    services = tx.services
    history = services.undo_history()
    done = history[start:]
    if not done:
        return
    _undo_to(tx, done[0]["mark"])
    _mark_reverted(services, done, tx.operation_id or "")
    del history[start:]
    tx.after_undo()


def _redo_part(services, args) -> Any:
    """Gate 3t: the part of the next call to redo (None: nothing to redo)."""
    pending = services.redo_steps()
    return part_by_key(services, pending[0].get("part_id")) if pending else None


@tool(
    target=_redo_part,
    effect="session",
    label="Redo operation",
    summary="Redo the agent's undone operation",
    keywords=("redo", "redo last", "undo the undo", "reapply undone", "repeat undone"),
    requires=("work_part",),
    cost="heavy",
    example={},
)
def nx_redo_operation(tx: Tx, *, steps: RedoCount = 1) -> RedoData:
    """Repeat the agent's calls an undo or checkpoint restore reverted, next first. Each
    runs as its own operation (same operation_id, new refs). New work ends the redo list;
    a failing step undoes the steps redone."""
    services = tx.services
    pending = services.redo_steps()
    with tx.checks():
        if not pending:
            fail("NX_NOT_FOUND", "Nothing to redo", hint=NOTHING_TO_REDO,
                 details={"redo_depth": 0}, retry="never")  # fmt: skip
        if steps > len(pending):
            fail(
                "NX_INVALID_ARGUMENT",
                f"steps {steps} is more than the {len(pending)} undone call(s) to redo",
                field="/steps",
                hint=f"Pass steps from 1 to {len(pending)}",
                details={"redo_depth": len(pending)},
            )
        key = services.part_key(tx.part)
        todo = list(pending[:steps])
        arguments = [_redo_arguments(services, item, key) for item in todo]
    rest = list(pending[steps:])
    history = services.undo_history()
    start = len(history)
    results: list[RedoneStep] = []
    tx.touched = True
    try:
        for index, (item, args) in enumerate(zip(todo, arguments, strict=True)):
            tx.checkpoint(done=index, total=steps)
            envelope = tx.call(item["method"], args, operation_id=item["operation_id"])
            if not envelope.get("ok"):
                error = envelope.get("error") or {}
                raise NXError(
                    str(error.get("code") or "NX_ERROR"),
                    f"Redo step {index} ({item['method']}, {item['operation_id']}) failed: "
                    + str(error.get("message") or ""),
                    hint=error.get("hint"),
                    details={
                        "step": index,
                        "operation_id": item["operation_id"],
                        "step_outcome": envelope.get("mutation_outcome"),
                        "redone_before_rollback": len(results),
                    },
                )
            results.append(
                {
                    "operation_id": item["operation_id"],
                    "tool": item["method"],
                    "data": envelope.get("data") or {},
                    "changes": envelope.get("changes") or {},
                }
            )
            tx.warnings.extend(
                w for w in envelope.get("warnings") or [] if item["operation_id"] not in str(w)
            )
    except NXError as error:
        outcome = error.details.get("step_outcome")
        try:
            _undo_redone(tx, start)
        except Exception as undo_error:
            tx.outcome = "partial"
            error.details["rollback_error"] = str(undo_error)
        else:
            tx.outcome = "partial" if outcome in ("partial", "unknown") else "rolled_back"
        pending[:] = todo + rest  # the kernel emptied it on the first committed step
        raise
    pending[:] = rest
    return {
        "redone": [item["operation_id"] for item in todo],
        "results": results,
        "redo_depth": len(pending),
        "undo_depth": _depth(services, history),
    }
