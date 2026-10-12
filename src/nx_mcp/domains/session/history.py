"""Undo and checkpoints of the agent's own work, per part (tool-surface 3.1).

``nx_undo_operation`` (was ``nx_undo``), ``nx_create_checkpoint`` (``nx_checkpoint``),
``nx_list_checkpoints`` (``nx_checkpoint_state``), ``nx_restore_checkpoint``
(``nx_rollback``) and ``nx_redo_operation`` work on the services' undo history: the
agent's committed model calls of every part with their NX undo marks, oldest first
(``Services.undo_history``), read per part. Each tool acts on its target part (gate 3t:
the ``part`` key, the refs, the checkpoint's or operation's part, else the work part;
with two or more of the agent's parts loaded a call without either is
``NX_AMBIGUOUS_PART``) and reports that part's depths. The engineer's work is never in
the history: the host appends an ``engineer`` barrier when the engineer changes the
model (``core/marks.py``), and no undo or restore crosses it.

NX keeps one linear undo list for the session, so the undo of part A may use NX's undo
only while nothing else lies between A's undone operations and the top of that list.
Other parts' operations in between: the undo compensates A's operations instead
(``domains/session/compensate.py``: deletes what create-only operations made, nothing
depending on it, and sets back the values ``nx_set_attributes``, ``nx_set_expression``
and ``nx_set_name`` changed, all verified), or it is refused with ``NX_CROSS_PART``
naming the operations and parts in the way and how to fix forward. ``mode``
``compensate`` (``delete_created`` is the same) always compensates, also across part
loads, closes and saves that cut NX's history (never across the engineer's edits); a
refusal at such a cut says when the operations qualify. ``operation`` undoes one named
own operation (``NX_DEPENDENCY_ORDER`` while later operations of the part follow it,
unless compensated). A compensating undo stays in the history as a ``compensation``
entry that is never undone itself.

Checkpoints are NX undo marks of this session only. A save or a part close removes
NX's marks (``saved_or_closed``), NX evicts its oldest marks beyond about 180 for the
whole session (``mark_evicted``), a display switch outside NX MCP may reset them
(``display_reset``), and an undo or a restore back past a checkpoint takes its state off
the model's timeline (``undone_past``). Loading a part keeps NX's marks, but undoing
across the load would unload the part again: a load by a file or session call is a
barrier in the history (``core/marks.py``), so undo stops there and a checkpoint before
it cannot be restored (``part_loaded``); the engineer's edits are a barrier as well
(``engineer_change``). An unavailable checkpoint stays listed with its
``expired_reason`` and cannot be restored. A restored checkpoint stays available
(NX keeps the mark it undid to), so "try, restore, try again" works. A restore takes
the checkpoint's id or its label and undoes the checkpoint's part's operations since,
by the same rules as an undo. Undone calls' receipts are marked ``reverted_by``:
repeating such a call (same operation_id) runs it again, it does not replay the reverted
result. ``nx_redo_operation`` repeats a part's undone calls, next first, each as its own
operation with its receipt arguments; new committed work in that part ends its redo list
(NX 2406 offers NX Open no redo of its own). References follow NX's object identity: the
refs of every object the undo kept or restored stay valid; only refs of objects the undo
removed go stale (``changes.deleted`` and ``changes.stale_refs``). Checkpoints are never
dropped silently: an engineer edit or a part close leaves them listed as expired.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core import marks as undo_marks
from nx_mcp.core import parking
from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.marks import MARK_CAP_TEXT
from nx_mcp.core.target import ANY, file_name, part_by_key
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Range, Text
from nx_mcp.domains.session import compensate
from nx_mcp.kit import sketch as ksketch

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
OperationKey = Annotated[str, Text(128), Doc("Own operation_id to undo")]
UndoMode = Annotated[
    Literal["compensate", "delete_created"],
    Doc("No NX undo: delete what they made, set values back"),
]
COMPENSATE = ("compensate", "delete_created")  # delete_created: the v1 name, same thing
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
DELETE_HINT = "Delete the objects with nx_delete_objects"
# What an agent can do when no undo applies: change the model forward to the state wanted.
FIX_FORWARD = (
    "Fix forward instead: delete what it made with nx_delete_objects, or set the changed "
    "values back with the tool that set them (nx_set_attributes, nx_set_expression, "
    "nx_set_name, nx_set_feature_suppression, nx_edit_feature)"
)
LISTED = 20  # checkpoints named in a not-found error
IN_THE_WAY = 10  # operations named in a cross-part refusal


class RedoStep(TypedDict):
    # repeat with nx_invoke {"tool": tool, "arguments": arguments}, or nx_redo_operation
    tool: str  # the undone call's tool
    arguments: dict  # its arguments as received (receipt copy; long text cut)
    complete: bool  # false: the receipt cut long text or file content; rebuild those


class _UndoData(TypedDict):
    undone_operation_id: str  # the latest call undone
    tool: str  # its tool
    undone: list[str]  # every operation id undone, oldest first
    undo_depth: int  # the part's operations nx_undo_operation can still undo
    references: str  # preserved / listed (stale refs in changes); part_invalidated: re-list
    # The undone calls, oldest first: nx_redo_operation repeats them (steps=len(redo)), or
    # send each with nx_invoke {tool, arguments}. Refs the undone calls created are
    # stale; use the refs the repeats return
    redo: list[RedoStep]
    redo_depth: int  # the part's calls nx_redo_operation can repeat now


class UndoData(_UndoData, total=False):
    # true: undone without NX's undo (other parts' work came after them, or mode
    # compensate): what they created deleted (changes.deleted), values set back (restored)
    compensated: bool
    restored: list[str]  # the values set back, e.g. "PART_NO of cover.prt back to 'A-1'"
    missing: list[str]  # created refs a later operation had already deleted
    kept_expressions: list[str]  # created expressions NX kept (still in use)
    renumbered: list[dict]  # anchors NX renumbered: {ref, anchor, was}


class RedoneStep(TypedDict):
    operation_id: str  # the undone call's id, run again under it
    tool: str
    data: dict  # the call's result data
    changes: dict  # what this call created, modified and deleted (new refs)


class RedoData(TypedDict):
    redone: list[str]  # operation ids run again, in order
    results: list[RedoneStep]
    redo_depth: int  # the part's undone calls nx_redo_operation can still repeat
    undo_depth: int  # the part's operations nx_undo_operation can undo


class CheckpointData(TypedDict):
    checkpoint_id: str
    label: str
    undo_depth: int  # the part's operations nx_undo_operation can undo


class CheckpointRow(TypedDict):
    checkpoint_id: str
    label: str
    part_id: str
    available: bool  # False: not restorable, expired_reason says why
    expired_reason: ExpiredReason | None  # undone_past, part_loaded, mark_evicted...
    operations_after: int  # the part's operations a restore would undo


class CheckpointsData(TypedDict):
    checkpoints: list[CheckpointRow]
    undo_depth: int  # the part's operations nx_undo_operation can still undo
    last_operation: str | None  # the operation nx_undo_operation would undo


class _RestoreData(TypedDict):
    checkpoint_id: str
    label: str
    # operation ids undone by the restore, oldest first; calls an earlier undo already
    # reverted are not in the history any more, so they are not listed again
    reverted: list[str]
    references: str  # preserved / listed (stale refs in changes); part_invalidated: re-list


class RestoreData(_RestoreData, total=False):
    compensated: bool  # true: undone without NX's undo (see UndoData)
    restored: list[str]
    missing: list[str]
    kept_expressions: list[str]
    renumbered: list[dict]


# ---------------------------------------------------------------------------
# The history, read per part


def _key(services, part) -> str | None:
    if part is None:
        return None
    with contextlib.suppress(Exception):
        return services.part_key(part)
    return None


def _own(item: dict, key) -> bool:
    """An operation of the part ``key`` (not a barrier, not an undo by deletion)."""
    return (
        undo_marks.barrier(item) is None
        and not item.get("compensation")
        and item.get("part_id") == key
    )


def _walk(services, history: list, key) -> tuple[list, dict | None]:
    """``(operations, end)``: the part's latest operations NX's undo reaches, newest
    first, back to a barrier or a dropped mark (``end``; None: the start). Other parts'
    operations and undos by deletion do not end it: they are in the way (:func:`_plan`)."""
    ops: list = []
    for item in reversed(history):
        if undo_marks.barrier(item) is not None:
            return ops, item
        if not _own(item, key):
            continue
        if item.get("expired") or not services.mark_exists(item["mark"]):
            return ops, item
        ops.append(item)
    return ops, None


def _reach(history: list, key) -> tuple[list, dict | None]:
    """The part's operations an undo by deletion reaches, newest first: back to the
    engineer's last edit (no undo reverts it) or the start."""
    ops: list = []
    for item in reversed(history):
        if undo_marks.barrier(item) == "engineer":
            return ops, item
        if _own(item, key):
            ops.append(item)
    return ops, None


def _depth(services, key) -> int:
    """The part's latest operations whose NX undo marks still exist: what undo reaches."""
    if key is None:
        return 0
    return len(_walk(services, services.undo_history(), key)[0])


def _paths(item: dict, name: str) -> str:
    return ", ".join(str(f) for f in item.get(name) or []) or "a part"


def _loaded(item: dict) -> str:
    return f"{_paths(item, 'load')} was loaded after it ({item.get('method') or 'a load'})"


def _why_ended(item: dict) -> tuple[str, str]:
    """``(reason code, sentence)`` of the entry that ends a part's undo history."""
    kind = undo_marks.barrier(item)
    if kind == "load":
        return "part_loaded", _loaded(item) + ": undoing across the load would unload it"
    if kind == "close":
        return "part_closed", (
            f"{_paths(item, 'close')} was closed after it ({item.get('method') or 'a close'}): "
            "undoing across the close would load it again"
        )
    if kind == "engineer":
        return "engineer_change", WHY["engineer_change"]
    reason = str(item.get("expired") or "mark_evicted")
    return reason, WHY.get(reason, WHY["mark_evicted"])


def _file(services, key) -> str:
    part = part_by_key(services, key)
    return file_name(part) if part is not None else "a closed part"


def _in_the_way(services, entries: list) -> list[dict]:
    """Rows of the history entries an NX undo would revert besides the undone ones."""
    rows = []
    for item in entries[:IN_THE_WAY]:
        row: dict[str, Any] = {
            "operation_id": item.get("operation_id"),
            "tool": item.get("method"),
            "part": _file(services, item.get("part_id")),
        }
        if item.get("compensation"):
            row["undo_by_deletion"] = True  # NX's undo would bring back what it deleted
        rows.append(row)
    return rows


def _sentence(rows: list[dict], total: int) -> str:
    text = ", ".join(
        f"{r['operation_id']} ({r['tool']} in {r['part']}"
        + (", an undo by deletion" if r.get("undo_by_deletion") else "")
        + ")"
        for r in rows
    )
    return text + (f" and {total - len(rows)} more" if total > len(rows) else "")


# ---------------------------------------------------------------------------
# Effects shared by undo and restore


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


def _redo_of(services, key) -> list:
    """The part's redo items, next first (a view of ``Services.redo_steps``)."""
    return [item for item in services.redo_steps() if item.get("part_id") == key]


def _set_redo(services, key, items: list) -> None:
    redo = services.redo_steps()
    redo[:] = list(items) + [item for item in redo if item.get("part_id") != key]


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


def _nx_undo(tx: Tx, mark, start: int, undone: list, by: str) -> tuple[list[RedoStep], str]:
    """NX's undo to ``mark``: the history from ``start`` (only the part's ``undone``
    operations) goes. ``(redo steps, references)``."""
    services = tx.services
    history = services.undo_history()
    checkpoints = services.undo_checkpoints()
    alive = _alive(services, checkpoints)
    redo = _redo_steps(services, undone)
    _undo_to(tx, mark)
    _drop_own_mark(tx)
    _mark_reverted(services, undone, by)
    del history[start:]
    references = tx.after_undo()
    _repark(tx, undone)
    _expire(services, checkpoints, len(history), alive)
    _push_redo(services, undone)
    return redo, references


def _repark(tx: Tx, undone: list) -> None:
    """Undone finishes of open sketches (``parked_sketch``, ``nx_finish_sketch``):
    NX's undo leaves the sketch deactivated, as it was while parked, so it is parked
    again (``core/parking.py``): its part's open sketch again, ``parked`` in
    ``nx_get_sketch``, and ``nx_finish_sketch`` finishes it. The oldest undone finish
    of a part wins (the part's state before the undone operations); a sketch the undo
    removed stays gone. When it cannot be parked (another sketch is open), a warning
    says it stays finished."""
    services = tx.services
    seen: set = set()
    for item in undone:  # oldest first
        ref = item.get("parked_sketch")
        if not isinstance(ref, str) or item.get("part_id") in seen:
            continue
        seen.add(item.get("part_id"))
        try:
            sketch = services.references.resolve(ref, ("sketch",))
        except Exception:
            continue  # the undo removed it (its nx_create_sketch was undone too)
        part = getattr(sketch, "OwningPart", None) or tx.part
        active = tx.session.ActiveSketch
        other = parking.parked(services, part)
        if (active is not None and int(active.Tag) != int(sketch.Tag)) or (
            other is not None and int(other.Tag) != int(sketch.Tag)
        ):
            tx.warn(
                "NX_UPDATE_WARNING",
                f"Sketch {ref} stays finished after the undo: another sketch is open; "
                "edit it again only after finishing that one",
            )
            continue
        try:
            if active is not None:  # NX left it active: deactivate it as a park does
                ksketch.park(tx, sketch)
            else:
                parking.park(services, part, sketch)
        except Exception as error:
            tx.warn("NX_UPDATE_WARNING", f"Sketch {ref} stays finished after the undo: {error}")
            continue
        if parking.is_parked(services, part, sketch):
            tx.warn(
                "NX_NOTE",
                f"Sketch {ref} is open again (parked), as before its nx_finish_sketch: "
                f"finish it before modeling in {file_name(part)}",
            )


def _delete_created(
    tx: Tx, plan: compensate.Plan, start: int, undone: list, by: str, why: str
) -> tuple[list[RedoStep], dict]:
    """Undo ``undone`` by deleting what they created (``compensate``): other parts and
    the rest of the history stay. ``(redo steps, extra result keys)``."""
    services = tx.services
    key = services.part_key(tx.part)
    checkpoints = services.undo_checkpoints()
    redo = _redo_steps(services, undone)
    info = compensate.run(tx, plan)
    _mark_reverted(services, undone, by)
    for cp in checkpoints.values():  # the part's states after the undone operations
        if cp.get("part_id") == key and int(cp["index"]) > start and _reason(services, cp) is None:
            cp["expired"] = "undone_past"
    undo_marks.remove_entries(services, undone)
    tx.history_note = {"compensation": True, "undid": [str(h["operation_id"]) for h in undone]}
    _push_redo(services, undone)
    kinds = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in sorted(info["kinds"].items()))
    done = []
    if kinds:
        done.append(f"deleted what {'it' if len(undone) == 1 else 'they'} created ({kinds})")
    if info.get("restored"):
        done.append("set back " + "; ".join(info["restored"]))
    tx.warn("NX_UPDATE_WARNING", f"Undone without NX's undo, it {' and '.join(done)}: " + why)
    if info.get("missing"):
        tx.warn(
            "NX_NOTE",
            "Already deleted by a later operation: " + ", ".join(info["missing"]),
        )
    if info.get("kept_expressions"):
        tx.warn(
            "NX_UPDATE_WARNING",
            "NX kept the expressions " + ", ".join(info["kept_expressions"]) + " (still in use)",
        )
    if info.get("renumbered"):
        tx.warn(
            "NX_NOTE",
            "NX renumbered later objects: "
            + ", ".join(f"{m['ref']} is now {m['anchor']}" for m in info["renumbered"])
            + "; use these anchors",
        )
    extra: dict = {"compensated": True}
    for name in ("restored", "missing", "kept_expressions", "renumbered"):
        if info.get(name):
            extra[name] = info[name]
    return redo, extra


def _plan(
    tx: Tx, history: list, undone: list, *, mode: str | None, what: str
) -> tuple[str, int, compensate.Plan | None, list]:
    """How to undo ``undone`` (the part's operations, oldest first): ``("nx", start,
    None, [])`` when nothing else lies after them, else ``("delete", start, plan, in the
    way)``; refuses (``NX_CROSS_PART``) when compensation does not qualify. ``mode``
    ``compensate``: always compensated (its own refusal codes)."""
    services = tx.services
    start = next(i for i, h in enumerate(history) if h is undone[0])
    chosen = {id(h) for h in undone}
    blockers = [h for h in history[start:] if id(h) not in chosen]
    if mode not in COMPENSATE and not blockers:
        return "nx", start, None, []
    try:
        plan = compensate.plan(tx, undone, history[start:])
    except compensate.Refusal as refusal:
        if mode in COMPENSATE:
            fail(
                refusal.code,
                f"{what} cannot be undone without NX's undo (deleting what it created, "
                f"setting back the values it set): {refusal.reason}",
                hint=FIX_FORWARD,
                details={"delete_created": refusal.reason, **refusal.details},
            )
        rows = _in_the_way(services, blockers)
        fail(
            "NX_CROSS_PART",
            f"Undoing {what} with NX's undo would also revert later work: "
            f"{_sentence(rows, len(blockers))}; and it cannot be compensated (deleting what "
            f"it created, setting back the values it set): {refusal.reason}",
            hint=FIX_FORWARD + "; or undo the later operations of those parts first",
            details={
                "part_id": undone[0].get("part_id"),
                "operation_id": blockers[0].get("operation_id"),
                "in_the_way": rows,
                "delete_created": refusal.reason,
                **{k: v for k, v in refusal.details.items() if k == "dependents"},
            },
        )
    return "delete", start, plan, blockers


def _why_deleted(services, blockers: list) -> str:
    if not blockers:
        return "mode compensate, so NX's undo was not used"
    rows = _in_the_way(services, blockers)
    return f"later work ({_sentence(rows, len(blockers))}) came after it, so NX's undo was not used"


# ---------------------------------------------------------------------------
# Checkpoints


def _barrier_after(services, cp: dict) -> dict | None:
    """The first barrier recorded after the checkpoint, if any."""
    later = services.undo_history()[int(cp["index"]) :]
    return next((h for h in later if undo_marks.barrier(h) is not None), None)


def _reason(services, cp: dict) -> ExpiredReason | None:
    """Why a checkpoint cannot be restored; None while it can. A mark gone without a
    reason the kernel saw (a save, a close, a load, a display switch) is one NX evicted."""
    if cp.get("expired"):
        return cp["expired"]
    if not services.mark_exists(cp["mark"]):
        return "mark_evicted"
    barrier = _barrier_after(services, cp)
    if barrier is not None:
        return _why_ended(barrier)[0]  # type: ignore[return-value]  # an ExpiredReason
    return None


def _alive(services, checkpoints: dict) -> set[str]:
    return {k for k, cp in checkpoints.items() if _reason(services, cp) is None}


def _expire(services, checkpoints: dict, kept: int, alive: set[str]) -> None:
    """After an undo or restore down to ``kept`` history entries: the checkpoints that
    were restorable and now lie ahead of it, or whose mark NX removed with the undo, are
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


# ---------------------------------------------------------------------------
# nx_undo_operation


def _undo_part(services, args) -> Any:
    """Gate 3t: the part of the operation ``operation`` names (None without it; ANY when
    it is not in the history: the handler says so)."""
    operation = args.get("operation")
    if not isinstance(operation, str):
        return None
    for item in reversed(services.undo_history()):
        if item.get("operation_id") == operation and undo_marks.barrier(item) is None:
            if item.get("compensation"):
                break
            return part_by_key(services, item.get("part_id")) or ANY
    return ANY


def _offer(tx: Tx, history: list, key, ops: list, end: dict | None) -> str:
    """The hint for operations NX's undo no longer reaches (``ops``, oldest first, beyond
    the cut ``end``): mode compensate when they qualify, else how to fix forward."""
    if not ops or end is None or undo_marks.barrier(end) == "engineer":
        return FIX_FORWARD
    start = next((i for i, h in enumerate(history) if h is ops[0]), 0)
    try:
        plan = compensate.plan(tx, ops, history[start:])
    except compensate.Refusal as refusal:
        return f"It cannot be compensated either ({refusal.reason}). " + FIX_FORWARD
    what = []
    if plan.features:
        what.append("delete what it created")
    if plan.restores:
        what.append("set back the values it set")
    return (
        "Repeat with mode compensate: without NX's undo it will "
        + " and ".join(what or ["undo it"])
        + ", leaving other parts untouched"
    )


def _not_found(file: str, end: dict | None, older: bool, other: bool, offer: str = "") -> None:
    reason, why = _why_ended(end) if end is not None else (None, "")
    if end is not None:
        message = f"the history of {file} ends at its last operation: {why}"
    elif other:
        message = f"no operation of {file} is in the undo history (other parts' are)"
    else:
        message = "the undo history is empty"
    hint = (
        "A save, a part close or load, or the engineer's own edits end the undo "
        "history; delete or edit the objects instead"
    )
    if older and reason in ("part_loaded", "part_closed", "mark_evicted", "saved_or_closed"):
        hint = offer or (
            hint + " (mode compensate deletes what create-only operations made and sets "
            "back values they set)"
        )
    fail(
        "NX_NOT_FOUND",
        "No agent operation can be undone: " + message,
        hint=hint,
        details={"undo_depth": 0, **({"reason": reason} if reason else {})},
        retry="never",  # no fix makes the same call succeed
    )


def _too_many(steps: int, depth: int, end: dict | None, field: str = "/steps") -> None:
    reason, why = _why_ended(end) if end is not None else (None, "")
    fail(
        "NX_INVALID_ARGUMENT",
        f"steps {steps} is more than the {depth} agent operation(s) that can be undone"
        + (f" (the history ends there: {why})" if end is not None else ""),
        field=field,
        hint=f"Pass steps from 1 to {depth}; nx_list_checkpoints shows undo_depth",
        details={"undo_depth": depth, **({"reason": reason} if reason else {})},
    )


def _choose(
    tx: Tx, history: list, key, file: str, steps: int, operation: str | None, mode: str | None
) -> list:
    """The part's operations to undo, oldest first (refusals inside ``tx.checks``)."""
    services = tx.services
    ops, end = _reach(history, key) if mode in COMPENSATE else _walk(services, history, key)
    if operation is None:
        if not ops:
            reached = _reach(history, key)[0]
            other = any(undo_marks.barrier(h) is None for h in history)
            offer = _offer(tx, history, key, list(reversed(reached[:steps])), end)
            _not_found(file, end, bool(reached), other, offer)
        if steps > len(ops):
            _too_many(steps, len(ops), end)
        return list(reversed(ops[:steps]))
    found = next((h for h in history if h.get("operation_id") == operation and _own(h, key)), None)
    if found is None:
        fail(
            "NX_NOT_FOUND",
            f"{operation} is not an operation of {file} that can be undone (it is not in "
            "the part's undo history: another part's, already undone, or not a model "
            "change)",
            field="/operation",
            hint="nx_list_checkpoints shows last_operation; nx_get_operation shows what ran",
            details={"undo_depth": _depth(services, key), "operation_id": operation},
            retry="never",
        )
    if not any(h is found for h in ops):
        reason, why = _why_ended(end) if end is not None else ("mark_evicted", "")
        reached = _reach(history, key)[0]
        fail(
            "NX_NOT_FOUND",
            f"{operation} can no longer be undone with NX's undo: the history of {file} ends "
            f"after it: {why}",
            field="/operation",
            hint=_offer(tx, history, key, [found], end)
            if any(h is found for h in reached)
            else FIX_FORWARD,
            details={"undo_depth": _depth(services, key), "reason": reason},
            retry="never",
        )
    index = next(i for i, h in enumerate(ops) if h is found)  # newer ones come first
    if index and mode not in COMPENSATE:
        fail(
            "NX_DEPENDENCY_ORDER",
            f"{index} later operation(s) of {file} follow {operation}: NX's undo would "
            "revert them too",
            field="/operation",
            hint=f"Undo them too with steps {index + 1}, or pass mode compensate to "
            "delete only what it created and set back only the values it set",
            details={"later": [str(h["operation_id"]) for h in reversed(ops[:index])]},
        )
    return [found]


@tool(
    target=_undo_part,
    effect="model",
    label="Undo operation",
    summary="Undo the agent's last operation",
    keywords=("undo", "revert", "undo last", "take back", "cancel last change", "step back"),
    requires=("work_part",),
    cost="light",
    example={},
)
def nx_undo_operation(
    tx: Tx,
    *,
    steps: Steps = 1,
    operation: OperationKey | None = None,
    mode: UndoMode | None = None,
) -> UndoData:
    """Undo the part's last steps operations, or one own operation; never others' work.
    nx_redo_operation repeats them."""
    services = tx.services
    history = services.undo_history()
    key = services.part_key(tx.part)
    file = file_name(tx.part)
    with tx.checks():
        if operation is not None and tx.given("steps"):
            fail("NX_INVALID_ARGUMENT", "Pass steps or operation, not both", field="/operation",
                 hint="operation undoes that one operation; steps the part's last ones")  # fmt: skip
        undone = _choose(tx, history, key, file, steps, operation, mode)
        what = (
            f"{undone[0]['operation_id']} ({undone[0].get('method')})"
            if len(undone) == 1
            else f"{len(undone)} operations of {file} from {undone[0]['operation_id']}"
        )
        how, start, plan, blockers = _plan(tx, history, undone, mode=mode, what=what)
    by = tx.operation_id or ""
    extra: dict = {}
    if how == "nx":
        redo, references = _nx_undo(tx, undone[0]["mark"], start, undone, by)
    else:
        assert plan is not None
        redo, extra = _delete_created(tx, plan, start, undone, by, _why_deleted(services, blockers))
        references = "listed"
    last = undone[-1]
    out: UndoData = {
        "undone_operation_id": last["operation_id"],
        "tool": last.get("method") or "",
        "undone": [h["operation_id"] for h in undone],
        "undo_depth": _depth(services, key),
        "references": references,
        "redo": redo,
        "redo_depth": len(_redo_of(services, key)),
    }
    out.update(extra)  # type: ignore[typeddict-item]  # compensated, missing, ...
    return out


# ---------------------------------------------------------------------------
# Checkpoint tools


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
    part_key = services.part_key(part)
    services.undo_checkpoints()[key] = {
        "mark": mark,
        "part_id": part_key,
        "index": len(history),
        "label": label,
    }
    return {"checkpoint_id": key, "label": label, "undo_depth": _depth(services, part_key)}


@tool(
    target="any",
    effect="read",
    label="List checkpoints",
    summary="List undo checkpoints",
    keywords=("checkpoints", "undo history", "undo depth", "restore points"),
    cost="light",
    example={},
)
def nx_list_checkpoints(tx: Tx) -> CheckpointsData:
    """List this session's checkpoints (with part: that part's) and the part's undo depth.
    Expired ones show available=false and expired_reason (undone_past, saved_or_closed,
    part_loaded, display_reset, mark_evicted, engineer_change or part_closed)."""
    services = tx.services
    history = services.undo_history()
    key = _key(services, services.work_part())
    named = tx.target is not None and tx.target.field == "/part"  # part given: its own
    rows: list[CheckpointRow] = []
    for cp_key, cp in services.undo_checkpoints().items():
        if named and cp.get("part_id") != key:
            continue
        reason = _reason(services, cp)
        after = sum(1 for h in history[int(cp["index"]) :] if _own(h, cp.get("part_id")))
        rows.append(
            {
                "checkpoint_id": cp_key,
                "label": str(cp.get("label") or ""),
                "part_id": str(cp["part_id"]),
                "available": reason is None,
                "expired_reason": reason,
                "operations_after": 0 if reason else after,
            }
        )
    ops = _walk(services, history, key)[0] if key is not None else []
    return {
        "checkpoints": rows,
        "undo_depth": len(ops),
        "last_operation": ops[0]["operation_id"] if ops else None,
    }


def _checkpoint_part(services, args) -> Any:
    """Gate 3t: the part a checkpoint belongs to (ANY when the checkpoint or its part is
    gone: the handler says so; None for a malformed id)."""
    text = args.get("checkpoint_id")
    if not isinstance(text, str):
        return None
    checkpoints = services.undo_checkpoints()
    try:
        key = _lookup(services, checkpoints, text)
    except NXError:
        return ANY  # the handler reports it
    return part_by_key(services, checkpoints[key].get("part_id")) or ANY


def _restore_refusal(services, cp: dict, reason: str) -> None:
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
    barrier = _barrier_after(services, cp) if reason in ("part_loaded", "part_closed") else None
    if barrier is not None:
        kind = undo_marks.barrier(barrier)
        if kind == "load":
            text = f"{_loaded(barrier)}: restoring this checkpoint would unload it"
        else:
            text = f"{_paths(barrier, 'close')} was closed after it: restoring would load it again"
        fail("NX_STALE_CHECKPOINT", text,
             field="/checkpoint_id", hint="Delete or edit the objects instead",
             details={"reason": reason, "loaded" if kind == "load" else "closed":
                      barrier.get("load") or barrier.get("close")})  # fmt: skip
    fail("NX_STALE_CHECKPOINT",
         f"The checkpoint cannot be restored: {WHY.get(reason, reason)}",
         field="/checkpoint_id", hint=STALE_HINT,
         details={"reason": reason})  # fmt: skip


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
    """Roll the checkpoint's part back to it (id or label), undoing its agent operations
    since; it stays restorable. Refused when expired, or when other parts' later work is
    in the way. Refs of objects that remain stay valid; save to persist."""
    services = tx.services
    checkpoints = services.undo_checkpoints()
    history = services.undo_history()
    with tx.checks():
        key = _lookup(services, checkpoints, checkpoint_id)
        cp: dict = checkpoints[key]
        reason = _reason(services, cp)
        if reason:
            _restore_refusal(services, cp, reason)
        part_key = services.part_key(tx.part)
        if cp["part_id"] != part_key:
            fail(
                "NX_CROSS_PART",
                f"The checkpoint belongs to {_file(services, cp['part_id'])}, not "
                f"{file_name(tx.part)}",
                field="/checkpoint_id",
                hint="Drop part: the checkpoint names its own part",
                details={"part_id": cp["part_id"]},
            )
        later = history[int(cp["index"]) :]
        undone = [h for h in later if _own(h, part_key)]
        alone = len(undone) == len(later)  # only the part's own work since: as before
        how, start, plan = "nx", int(cp["index"]), None
        blockers: list = []
        if not alone and undone:
            what = f"{len(undone)} operation(s) of {file_name(tx.part)} since the checkpoint"
            how, start, plan, blockers = _plan(tx, history, undone, mode=None, what=what)
    extra: dict = {}
    if alone:
        _redo, references = _nx_undo(tx, cp["mark"], start, undone, key)
    elif not undone:  # only other parts worked since: nothing of this part to undo
        _drop_own_mark(tx)
        references = "preserved"
    elif how == "nx":
        _redo, references = _nx_undo(tx, undone[0]["mark"], start, undone, key)
    else:
        assert plan is not None
        _redo, extra = _delete_created(
            tx, plan, start, undone, key, _why_deleted(services, blockers)
        )
        references = "listed"
    out: RestoreData = {
        "checkpoint_id": key,
        "label": str(cp.get("label") or ""),
        "reverted": [h["operation_id"] for h in undone],
        "references": references,
    }
    out.update(extra)  # type: ignore[typeddict-item]  # compensated, missing, ...
    return out


# ---------------------------------------------------------------------------
# nx_redo_operation

NOTHING_TO_REDO = (
    "nx_redo_operation repeats calls that nx_undo_operation or nx_restore_checkpoint "
    "reverted; new committed work in the part or an engineer edit ends that list"
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
    _repark(tx, done)


def _redo_part(services, args) -> Any:
    """Gate 3t: the one part with calls to redo (ANY when no part has any: the handler
    says so; None when several have: the usual rules pick the part)."""
    keys = {item.get("part_id") for item in services.redo_steps()}
    if not keys:
        return ANY
    if len(keys) > 1:
        return None
    return part_by_key(services, next(iter(keys))) or ANY


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
    """Repeat the part's agent calls an undo or checkpoint restore reverted, next first.
    Each runs as its own operation (same operation_id, new refs). New work in the part
    ends the redo list; a failing step undoes the steps redone."""
    services = tx.services
    key = services.part_key(tx.part)
    pending = _redo_of(services, key)
    with tx.checks():
        if not pending:
            fail("NX_NOT_FOUND", f"Nothing to redo in {file_name(tx.part)}",
                 hint=NOTHING_TO_REDO, details={"redo_depth": 0}, retry="never")  # fmt: skip
        if steps > len(pending):
            fail(
                "NX_INVALID_ARGUMENT",
                f"steps {steps} is more than the {len(pending)} undone call(s) to redo",
                field="/steps",
                hint=f"Pass steps from 1 to {len(pending)}",
                details={"redo_depth": len(pending)},
            )
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
        _set_redo(services, key, todo + rest)  # the kernel ended it on the first commit
        raise
    _set_redo(services, key, rest)
    return {
        "redone": [item["operation_id"] for item in todo],
        "results": results,
        "redo_depth": len(rest),
        "undo_depth": _depth(services, key),
    }
