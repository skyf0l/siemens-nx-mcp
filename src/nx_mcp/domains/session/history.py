"""Undo and checkpoints of the agent's own work (tool-surface 3.1).

``nx_undo_operation`` (was ``nx_undo``), ``nx_create_checkpoint`` (``nx_checkpoint``),
``nx_list_checkpoints`` (``nx_checkpoint_state``) and ``nx_restore_checkpoint``
(``nx_rollback``) work on the services' undo history: the agent's committed model
calls with their NX undo marks, oldest first (``Services.undo_history``). The
engineer's own work is never in it, and the interactive host empties the history and
the checkpoints whenever the engineer changes the model, so neither tool reaches
across their edits.

Checkpoints are NX undo marks of this session only. A save or a part close removes
NX's marks: the checkpoint is then listed as unavailable and cannot be restored.
Undo and restore refuse to touch another part than the work part, mark the undone
calls' receipts ``reverted_by`` (a retry of such a call is refused), and invalidate
every reference of the part: reacquire them afterwards.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Annotated, Any, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Text

CheckpointId = Annotated[str, Text(40, r"^cp_[0-9a-f]{32}$"), Doc("From nx_create_checkpoint")]
Label = Annotated[str, Text(80, r"^[^\x00-\x1f]{1,80}$"), Doc("Shown in NX's undo list")]
STALE_HINT = (
    "A save, a part close or the engineer's own edits end checkpoints; create a new "
    "checkpoint with nx_create_checkpoint"
)


class UndoData(TypedDict):
    undone_operation_id: str  # the agent's call that was undone
    tool: str  # its tool
    references: str  # part_invalidated: reacquire references of the part


class CheckpointData(TypedDict):
    checkpoint_id: str
    label: str
    operations: int  # agent operations before the checkpoint in this session


class CheckpointRow(TypedDict):
    checkpoint_id: str
    label: str
    part_id: str
    available: bool  # False once NX dropped its mark (save, part close): not restorable
    operations_after: int  # agent operations a restore would undo


class CheckpointsData(TypedDict):
    checkpoints: list[CheckpointRow]
    undo_depth: int  # agent operations nx_undo_operation can still undo
    last_operation: str | None  # the operation nx_undo_operation would undo


class RestoreData(TypedDict):
    checkpoint_id: str
    reverted: list[str]  # operation ids undone by the restore, oldest first
    references: str  # part_invalidated: reacquire references of the part


def _mark_reverted(services, history: list, by: str) -> None:
    """Receipts of undone calls: a retry with their operation_id is then refused."""
    for item in history:
        with contextlib.suppress(Exception):
            record = services.store.get(item["operation_id"])
            if "fingerprint" in record:
                record["reverted_by"] = by
                services.store.put(record)


def _drop_own_mark(tx: Tx) -> None:
    """The kernel's mark for this call lies after the restored mark: NX drops it with
    the undo; drop it here too, so it never enters the history."""
    mark: Any = tx.mark
    if mark is not None:
        with contextlib.suppress(Exception):
            tx.services.delete_mark(mark)


@tool(
    effect="model",
    label="Undo operation",
    summary="Undo the agent's last operation",
    keywords=("undo", "revert", "undo last", "take back", "cancel last change"),
    requires=("work_part",),
    cost="light",
    example={},
)
def nx_undo_operation(tx: Tx) -> UndoData:
    """Undo the agent's last committed model operation in the work part (never the
    engineer's work). Saves, part closes and the engineer's edits end the undo history.
    Invalidates the part's references."""
    services = tx.services
    history = services.undo_history()
    with tx.checks():
        if not history or not services.mark_exists(history[-1]["mark"]):
            fail(
                "NX_NOT_FOUND",
                "No agent operation can be undone: NX's undo mark is gone or there is none",
                hint="A save, a part close or the engineer's own edits end the undo history; "
                "delete or edit the objects instead",
            )
        last = history[-1]
        if last["part_id"] != services.part_key(tx.part):
            fail(
                "NX_CROSS_PART",
                "The agent's last operation changed another part",
                hint="Activate the part owning the latest operation, then undo",
                details={"part_id": last["part_id"]},
            )
    services.undo_to(last["mark"])
    _drop_own_mark(tx)
    _mark_reverted(services, [last], tx.operation_id or "")
    history.pop()
    services.invalidate_parts([tx.part])
    checkpoints = services.undo_checkpoints()
    for key in [k for k, v in checkpoints.items() if v["index"] > len(history)]:
        del checkpoints[key]
    tx.references = "part_invalidated"
    return {
        "undone_operation_id": last["operation_id"],
        "tool": last.get("method") or "",
        "references": "part_invalidated",
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
    """Set an in-session undo checkpoint in the work part. A save or part close expires
    it (create a new one after saving); disk saves are never rolled back."""
    services = tx.services
    part = tx.part
    mark = services.set_mark("NX MCP checkpoint: " + label)
    key = "cp_" + uuid.uuid4().hex
    index = len(services.undo_history())
    services.undo_checkpoints()[key] = {
        "mark": mark,
        "part_id": services.part_key(part),
        "index": index,
        "label": label,
    }
    return {"checkpoint_id": key, "label": label, "operations": index}


@tool(
    effect="read",
    label="List checkpoints",
    summary="List undo checkpoints",
    keywords=("checkpoints", "undo history", "undo depth", "restore points"),
    cost="light",
    example={},
)
def nx_list_checkpoints(tx: Tx) -> CheckpointsData:
    """List this session's checkpoints and the agent's undo depth. available=false
    once a save or part close expired a checkpoint."""
    services = tx.services
    history = services.undo_history()
    rows: list[CheckpointRow] = [
        {
            "checkpoint_id": key,
            "label": str(cp.get("label") or ""),
            "part_id": str(cp["part_id"]),
            "available": services.mark_exists(cp["mark"]),
            "operations_after": max(0, len(history) - int(cp["index"])),
        }
        for key, cp in services.undo_checkpoints().items()
    ]
    undoable = bool(history) and services.mark_exists(history[-1]["mark"])
    return {
        "checkpoints": rows,
        "undo_depth": sum(1 for h in history if services.mark_exists(h["mark"])),
        "last_operation": history[-1]["operation_id"] if undoable else None,
    }


@tool(
    effect="model",
    label="Restore checkpoint",
    summary="Roll back to an earlier checkpoint",
    keywords=("rollback", "restore checkpoint", "roll back", "revert to checkpoint"),
    requires=("work_part",),
    cost="light",
    example={"checkpoint_id": "cp_" + "0" * 32},
)
def nx_restore_checkpoint(tx: Tx, *, checkpoint_id: CheckpointId) -> RestoreData:
    """Roll the work part back to a checkpoint of this session, undoing the agent's
    operations since. Refused when it expired or would undo another part. Invalidates
    the part's references; save explicitly to persist."""
    services = tx.services
    checkpoints = services.undo_checkpoints()
    history = services.undo_history()
    with tx.checks():
        cp: dict = checkpoints.get(checkpoint_id) or {}
        if not cp:
            fail("NX_STALE_CHECKPOINT", "The checkpoint is not in this session",
                 field="/checkpoint_id", hint=STALE_HINT)  # fmt: skip
        if not services.mark_exists(cp["mark"]):
            fail("NX_STALE_CHECKPOINT",
                 "NX dropped the checkpoint's undo mark (a save or a part close)",
                 field="/checkpoint_id", hint=STALE_HINT)  # fmt: skip
        key = services.part_key(tx.part)
        later = history[cp["index"] :]
        if cp["part_id"] != key or any(h["part_id"] != key for h in later):
            fail(
                "NX_CROSS_PART",
                "Restoring would change another part than the work part",
                hint="Activate the checkpoint's part, or undo the other part's operations first",
                details={"part_id": cp["part_id"]},
            )
    services.undo_to(cp["mark"])
    _drop_own_mark(tx)
    _mark_reverted(services, later, checkpoint_id)
    del history[cp["index"] :]
    services.invalidate_parts([tx.part])
    for other in [k for k, v in checkpoints.items() if v["index"] >= cp["index"]]:
        del checkpoints[other]
    tx.references = "part_invalidated"
    return {
        "checkpoint_id": checkpoint_id,
        "reverted": [h["operation_id"] for h in later],
        "references": "part_invalidated",
    }
