"""Undo and checkpoints of the agent's own work (tool-surface 3.1).

``nx_undo_operation`` (was ``nx_undo``), ``nx_create_checkpoint`` (``nx_checkpoint``),
``nx_list_checkpoints`` (``nx_checkpoint_state``) and ``nx_restore_checkpoint``
(``nx_rollback``) work on the services' undo history: the agent's committed model
calls with their NX undo marks, oldest first (``Services.undo_history``). The
engineer's own work is never in it, and the interactive host empties the history and
the checkpoints whenever the engineer changes the model, so neither tool reaches
across their edits.

Checkpoints are NX undo marks of this session only. A save or a part close removes
NX's marks, and an undo or a restore back past a checkpoint takes its state off the
model's timeline: the checkpoint stays listed as unavailable with its
``expired_reason`` and cannot be restored. A restored checkpoint stays available
(NX keeps the mark it undid to), so "try, restore, try again" works. A restore takes
the checkpoint's id or its label. Undo and restore refuse to touch another part than
the work part and mark the undone calls' receipts ``reverted_by`` (a retry of such a
call is refused). References follow NX's object identity: the refs of every object the
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
from nx_mcp.core.errors import fail
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
ExpiredReason = Literal["undone_past", "saved_or_closed", "engineer_change", "part_closed"]
STALE_HINT = (
    "Create a new checkpoint with nx_create_checkpoint; nx_list_checkpoints shows "
    "which ones are available"
)
LISTED = 20  # checkpoints named in a not-found error


class RedoStep(TypedDict):
    tool: str  # the undone call's tool
    arguments: dict  # its arguments as received (receipt copy; long text cut)
    operation_id: str  # the undone call (nx_get_operation shows its result)
    complete: bool  # false: the receipt cut long text or file content; rebuild those


class UndoData(TypedDict):
    undone_operation_id: str  # the latest call undone
    tool: str  # its tool
    undone: list[str]  # every operation id undone, oldest first
    undo_depth: int  # agent operations nx_undo_operation can still undo
    references: str  # preserved / listed (stale refs in changes); part_invalidated: re-list
    # NX 2406 offers no redo to NX Open: repeat these calls in order to restore the undone
    # work; refs the undone calls created are stale, use the refs the repeats return
    redo: list[RedoStep]


class CheckpointData(TypedDict):
    checkpoint_id: str
    label: str
    undo_depth: int  # agent operations nx_undo_operation can undo (= nx_list_checkpoints)


class CheckpointRow(TypedDict):
    checkpoint_id: str
    label: str
    part_id: str
    available: bool  # False: not restorable, expired_reason says why
    expired_reason: ExpiredReason | None  # undone_past, saved_or_closed, engineer_change...
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
    """Receipts of undone calls: a retry with their operation_id is then refused."""
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
                "operation_id": str(item["operation_id"]),
                "complete": isinstance(arguments, dict)
                and "omitted" not in arguments
                and "omitted>" not in text
                and "<file content omitted" not in text,
            }
        )
    return steps


def _drop_own_mark(tx: Tx) -> None:
    """The kernel's mark for this call lies after the restored mark: NX drops it with
    the undo; drop it here too, so it never enters the history."""
    mark: Any = tx.mark
    if mark is not None:
        with contextlib.suppress(Exception):
            tx.services.delete_mark(mark)


def _depth(services, history: list) -> int:
    """The latest agent operations whose NX undo marks still exist: what undo reaches."""
    depth = 0
    for item in reversed(history):
        if not services.mark_exists(item["mark"]):
            break
        depth += 1
    return depth


def _reason(services, cp: dict) -> ExpiredReason | None:
    """Why a checkpoint cannot be restored; None while it can."""
    if cp.get("expired"):
        return cp["expired"]
    if not services.mark_exists(cp["mark"]):
        return "saved_or_closed"
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
    """Undo the agent's last steps operations (never the engineer's); data.redo lists calls
    to repeat. A save, part close or engineer edit ends the history. Remaining objects keep
    their refs."""
    services = tx.services
    history = services.undo_history()
    depth = _depth(services, history)
    with tx.checks():
        if not depth:
            fail(
                "NX_NOT_FOUND",
                "No agent operation can be undone: the undo history is empty or NX's "
                "undo marks are gone",
                hint="A save, a part close or the engineer's own edits end the undo history; "
                "delete or edit the objects instead",
                details={"undo_depth": 0},
                retry="never",  # no fix makes the same call succeed
            )
        if steps > depth:
            fail(
                "NX_INVALID_ARGUMENT",
                f"steps {steps} is more than the {depth} agent operation(s) that can be undone",
                field="/steps",
                hint=f"Pass steps from 1 to {depth}; nx_list_checkpoints shows undo_depth",
                details={"undo_depth": depth},
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
    services.undo_to(undone[0]["mark"])
    _drop_own_mark(tx)
    _mark_reverted(services, undone, tx.operation_id or "")
    del history[-steps:]
    references = tx.after_undo()
    _expire(services, checkpoints, len(history), alive)
    last = undone[-1]
    return {
        "undone_operation_id": last["operation_id"],
        "tool": last.get("method") or "",
        "undone": [h["operation_id"] for h in undone],
        "undo_depth": _depth(services, history),
        "references": references,
        "redo": redo,
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
    save, part close or undo past it expires it; disk saves are never rolled back."""
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
    available=false and expired_reason (undone_past, saved_or_closed, engineer_change or part_closed)."""
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


@tool(
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
        if reason:
            fail("NX_STALE_CHECKPOINT",
                 "NX dropped the checkpoint's undo mark (a save or a part close)",
                 field="/checkpoint_id", hint=STALE_HINT)  # fmt: skip
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
    services.undo_to(cp["mark"])
    _drop_own_mark(tx)
    _mark_reverted(services, later, key)
    del history[cp["index"] :]
    references = tx.after_undo()
    _expire(services, checkpoints, cp["index"], alive)
    return {
        "checkpoint_id": key,
        "label": str(cp.get("label") or ""),
        "reverted": [h["operation_id"] for h in later],
        "references": references,
    }
