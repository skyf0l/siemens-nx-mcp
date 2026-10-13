"""Undo and checkpoints of the agent's own work, per part (tool-surface 3.1).

``nx_undo_operation`` (was ``nx_undo``), ``nx_create_checkpoint`` (``nx_checkpoint``),
``nx_list_checkpoints`` (``nx_checkpoint_state``), ``nx_restore_checkpoint``
(``nx_rollback``) and ``nx_redo_operation`` work on the services' undo history: the
agent's committed model calls of every part with their NX undo marks, oldest first
(``Services.undo_history``), read per part. Each tool acts on its target part (gate 3t:
the ``part`` key, the refs, the checkpoint's or operation's part, else the work part;
with two or more of the agent's parts loaded a call without either is
``NX_AMBIGUOUS_PART``; an unknown operation or checkpoint never defaults to the
engineer's part, :func:`_unknown`) and reports that part's depths. The engineer's work is
never in the history: the host appends an ``engineer`` barrier when the engineer changes
the model (``core/marks.py``), and no undo or restore crosses it.

A part's history is its own (:func:`_reach`): other parts' operations, part loads and
closes (barriers), and NX marks another part's save or close dropped never end it; only
the engineer's edits and the part's own save or close do (``marks.own_cut``; mode
compensate reaches over the latter). NX keeps one linear undo list for the session, so
the undo of part A uses NX's undo only while A's undone operations still have their
marks and nothing else (other parts' work, a load, a close, an undo by deletion) lies
after them; else it compensates them (``domains/session/compensate.py``: deletes what
create-only operations made, features, sketches, curves and points, nothing depending on
it, and sets back the values ``nx_set_attributes``, ``nx_set_expression``, ``nx_set_name``
and the other ``@restorer`` tools changed, all verified), or it is refused with
``NX_CROSS_PART`` naming what is in the way (``NX_NOT_FOUND`` when only dropped marks are)
and how to fix forward. ``mode`` ``compensate`` (``delete_created`` is the same) always
compensates. ``operation`` undoes one named own operation (``NX_DEPENDENCY_ORDER`` while
later operations of the part follow it, unless compensated). A compensating undo stays in
the history as a ``compensation`` entry that is never undone itself.

Checkpoints are per part too: an undo or a restore back past one (``undone_past``), its
part's close (``part_closed``) or own save (``saved_or_closed``) and the engineer's edits
(``engineer_change``) end it; other parts' loads, closes and saves and marks NX evicted do
not. A restore undoes the checkpoint's part's operations since by the same rules as an
undo (NX's undo to the checkpoint's mark when nothing else came after, else
compensation; ``compensated`` in ``nx_list_checkpoints`` says which). An unavailable
checkpoint stays listed with its ``expired_reason`` and cannot be restored. A restored
checkpoint stays available, so "try, restore, try again" works. A restore takes the
checkpoint's id or its label (a label names the target part's checkpoints). Undone calls'
receipts are marked ``reverted_by``: repeating such a call (same operation_id) runs it
again, it does not replay the reverted result. ``nx_redo_operation`` repeats a part's
undone calls, next first, each as its own operation with its receipt arguments; new
committed work in that part ends its redo list (NX 2406 offers NX Open no redo of its
own). References follow NX's object identity: the refs of every object the undo kept or
restored stay valid; only refs of objects the undo removed go stale (``changes.deleted``
and ``changes.stale_refs``). Checkpoints are never dropped silently: an engineer edit or a
part close leaves them listed as expired.
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
from nx_mcp.core.target import ANY, counts, file_name, part_by_key
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
    # true: a restore now undoes them without NX's undo (other parts' later work, loads or
    # closes, or dropped marks): deletes what they created, sets values back
    compensated: bool


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


def _alive_entry(services, item: dict) -> bool:
    """Whether NX's undo mark of a history entry still exists."""
    if item.get("expired"):
        return False
    try:
        return bool(services.mark_exists(item["mark"]))
    except Exception:
        return False


def _reach(history: list, key, mode: str | None = None) -> tuple[list, dict | None]:
    """``(operations, end)``: the part's operations an undo reaches, newest first, back to
    ``end`` (None: the start). Per part: other parts' work, loads and closes and marks
    another part's save or close dropped never end it (NX's undo cannot reach over them,
    an undo by deletion can: :func:`_plan`). It ends at the engineer's last edit (no undo
    reverts it) and, for the default undo, at the part's own save or close
    (``marks.own_cut``; mode compensate reaches over that)."""
    ops: list = []
    for item in reversed(history):
        if undo_marks.barrier(item) == "engineer":
            return ops, item
        if not _own(item, key):
            continue
        if mode not in COMPENSATE and undo_marks.own_cut(item, key):
            return ops, item
        ops.append(item)
    return ops, None


def _depth(services, key) -> int:
    """How many of the part's latest operations the default undo can undo now: with NX's
    undo while they keep their marks and only they lie after the oldest of them, else by
    deletion while each of them qualifies (``compensate.qualifies``: receipts only, so a
    dependent feature or a value changed since may still refuse)."""
    if key is None:
        return 0
    history = services.undo_history()
    position = {id(h): i for i, h in enumerate(history)}
    depth, qualified, alive = 0, True, True
    for count, item in enumerate(_reach(history, key)[0], 1):  # newest first
        qualified = qualified and compensate.qualifies(services, item)
        alive = alive and _alive_entry(services, item)
        alone = alive and all(_own(h, key) for h in history[position[id(item)] :])
        if not (alone or qualified):
            break
        depth = count
    return depth


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
    """Rows of the history entries an NX undo would revert besides the undone ones: other
    parts' operations, undos by deletion, and part loads or closes (``barrier``) NX's undo
    would reverse."""
    rows = []
    for item in entries[:IN_THE_WAY]:
        kind = undo_marks.barrier(item)
        cut = kind in ("load", "close")
        row: dict[str, Any] = {
            "operation_id": item.get("operation_id"),
            "tool": item.get("method"),
            "part": _paths(item, str(kind)) if cut else _file(services, item.get("part_id")),
        }
        if cut:
            row["barrier"] = kind  # NX's undo would unload (load) or load again (close)
        if item.get("compensation"):
            row["undo_by_deletion"] = True  # NX's undo would bring back what it deleted
        rows.append(row)
    return rows


def _sentence(rows: list[dict], total: int) -> str:
    def one(r: dict) -> str:
        if r.get("barrier"):
            done = "loaded" if r["barrier"] == "load" else "closed"
            return f"{r['part']} {done} by {r['operation_id']} ({r['tool']})"
        return (
            f"{r['operation_id']} ({r['tool']} in {r['part']}"
            + (", an undo by deletion" if r.get("undo_by_deletion") else "")
            + ")"
        )

    text = ", ".join(one(r) for r in rows)
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
    key = undone[0].get("part_id")
    restorable = _restorable(services, checkpoints)
    marked = {k for k, cp in checkpoints.items() if _mark_alive(services, cp)}
    redo = _redo_steps(services, undone)
    _undo_to(tx, mark)
    _drop_own_mark(tx)
    _mark_reverted(services, undone, by)
    del history[start:]
    references = tx.after_undo()
    _repark(tx, undone)
    _expire(services, checkpoints, key, len(history), restorable, marked)
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
) -> tuple[str, int, compensate.Plan | None, list, list]:
    """How to undo ``undone`` (the part's operations, oldest first): ``("nx", start,
    None, [], [])`` when NX's undo reaches them and nothing else lies after them, else
    ``("delete", start, plan, in the way, dropped)``: other parts' work, loads or closes
    after them (``in the way``) or marks NX dropped (``dropped``: another part's save or
    close, eviction) make the undo compensate. Refuses when compensation does not qualify:
    ``NX_CROSS_PART`` (work in the way) or ``NX_NOT_FOUND`` (marks gone). ``mode``
    ``compensate``: always compensated (its own refusal codes)."""
    services = tx.services
    start = next(i for i, h in enumerate(history) if h is undone[0])
    chosen = {id(h) for h in undone}
    blockers = [h for h in history[start:] if id(h) not in chosen]
    dropped = [h for h in undone if not _alive_entry(services, h)]
    if mode not in COMPENSATE and not blockers and not dropped:
        return "nx", start, None, [], []
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
        dependents = {k: v for k, v in refusal.details.items() if k == "dependents"}
        if blockers:
            rows = _in_the_way(services, blockers)
            fail(
                "NX_CROSS_PART",
                f"Undoing {what} with NX's undo would also revert later work: "
                f"{_sentence(rows, len(blockers))}; and it cannot be compensated (deleting "
                f"what it created, setting back the values it set): {refusal.reason}",
                hint=FIX_FORWARD,
                details={
                    "part_id": undone[0].get("part_id"),
                    "operation_id": blockers[0].get("operation_id"),
                    "in_the_way": rows,
                    "delete_created": refusal.reason,
                    **dependents,
                },
            )
        reason = str(dropped[0].get("expired") or "mark_evicted")
        fail(
            "NX_NOT_FOUND",
            f"{what} can no longer be undone with NX's undo ({_dropped(dropped[0])}) and "
            "it cannot be compensated (deleting what it created, setting back the values "
            f"it set): {refusal.reason}",
            hint=FIX_FORWARD,
            details={"reason": reason, "delete_created": refusal.reason, **dependents},
            retry="never",  # no fix makes the same call succeed
        )
    return "delete", start, plan, blockers, dropped


# Why an operation's NX mark is gone when another part's call dropped it (marks.foreign_cut).
DROPPED = {
    "saved_or_closed": "another part's save or close dropped its undo mark",
    "part_loaded": "another part's load dropped its undo mark",
    "display_reset": "a display part switch dropped its undo mark",
}


def _dropped(item: dict) -> str:
    """Why NX no longer has the undo mark of ``item`` (an operation an undo compensates)."""
    reason = str(item.get("expired") or "mark_evicted")
    if undo_marks.own_cut(item, item.get("part_id")):  # mode compensate past its own save
        return WHY.get(reason, WHY["mark_evicted"])
    return DROPPED.get(reason) or WHY.get(reason, WHY["mark_evicted"])


def _why_deleted(blockers: list, dropped: list) -> str:
    """Why NX's undo was not used, without naming other parts' work (the warning of an
    undo by deletion)."""
    texts = []
    operations = sum(1 for h in blockers if undo_marks.barrier(h) is None)
    cuts = len(blockers) - operations
    if operations:
        plural = "s" if operations != 1 else ""
        texts.append(
            f"NX's undo would also revert {operations} later operation{plural} of other "
            "parts or undos by deletion"
        )
    if cuts:
        plural = "s" if cuts != 1 else ""
        texts.append(f"NX's undo no longer reaches it across {cuts} later part load{plural} "
                     f"or close{plural}")  # fmt: skip
    if dropped:
        texts.append(f"NX's undo no longer reaches it: {_dropped(dropped[0])}")
    return "; ".join(texts) or "mode compensate, so NX's undo was not used"


# ---------------------------------------------------------------------------
# Checkpoints


def _mark_alive(services, cp: dict) -> bool:
    try:
        return bool(services.mark_exists(cp["mark"]))
    except Exception:
        return False


def _reason(services, cp: dict) -> ExpiredReason | None:
    """Why a checkpoint cannot be restored; None while it can. Per part: only an undo or
    restore past it, its part's close, its part's own save (``marks.own_cut``) or the
    engineer's edits end it. Other parts' loads, closes and saves, and marks NX evicted,
    do not: the restore then deletes what the part's operations since created and sets
    their values back (:func:`_compensated`)."""
    expired = cp.get("expired")
    if expired in ("undone_past", "part_closed", "engineer_change"):
        return expired
    key = cp.get("part_id")
    if expired and undo_marks.own_cut(cp, key):
        return expired
    for item in services.undo_history()[int(cp["index"]) :]:
        if undo_marks.barrier(item) == "engineer":
            return "engineer_change"
        if _own(item, key) and undo_marks.own_cut(item, key):
            return item["expired"]
    return None


def _compensated(services, cp: dict) -> bool:
    """Whether a restore of ``cp`` would undo the part's operations since without NX's
    undo: other parts' work, loads or closes lie after them, or NX dropped their marks."""
    key = cp.get("part_id")
    later = services.undo_history()[int(cp["index"]) :]
    own = [h for h in later if _own(h, key)]
    if not own:
        return False
    first = next(i for i, h in enumerate(later) if h is own[0])
    return any(not _own(h, key) for h in later[first:]) or any(
        not _alive_entry(services, h) for h in own
    )


def _restorable(services, checkpoints: dict) -> set[str]:
    return {k for k, cp in checkpoints.items() if _reason(services, cp) is None}


def _expire(
    services, checkpoints: dict, key, kept: int, restorable: set[str], marked: set[str]
) -> None:
    """After NX's undo of the part ``key`` down to ``kept`` history entries: the part's
    checkpoints that were restorable and now lie ahead of it, or whose mark NX removed
    with the undo, are ``undone_past`` for good (NX Open has no redo). Other parts'
    checkpoints stay (only the part's own operations were undone); one that lay ahead
    now sits at the end of the history."""
    for name, cp in checkpoints.items():
        index = int(cp["index"])
        if cp.get("part_id") != key:
            cp["index"] = min(index, kept)
            continue
        removed = name in marked and not _mark_alive(services, cp)
        if name in restorable and (index > kept or removed):
            cp["expired"] = "undone_past"


def _label_key(text: str) -> str:
    return " ".join(text.split()).casefold()


def _labelled(checkpoints: dict, text: str, key=None) -> list[str]:
    """Checkpoints with the label ``text`` (case and spacing ignored), of the part
    ``key`` only when given, oldest first."""
    wanted = _label_key(text)
    return [
        k
        for k, cp in checkpoints.items()
        if _label_key(str(cp.get("label") or "")) == wanted
        and (key is None or cp.get("part_id") == key)
    ]


def _lookup(services, checkpoints: dict, text: str, key=None) -> str:
    """The checkpoint named by id or by label (case and spacing ignored); a label names
    the part ``key``'s checkpoints (other parts may use the same label); of several, the
    newest restorable one."""
    if text in checkpoints:
        return text
    matches = _labelled(checkpoints, text, key)
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
        if key is None or cp.get("part_id") == key
    ][-LISTED:]
    if re.match(CHECKPOINT_ID, text):
        fail(
            "NX_STALE_CHECKPOINT",
            "The checkpoint is not in this session",
            field="/checkpoint_id",
            hint="Closing its part or the engineer's own edits end checkpoints. " + STALE_HINT,
            details={"checkpoints": listed},
        )
    elsewhere = key is not None and bool(_labelled(checkpoints, text))
    fail(
        "NX_NOT_FOUND",
        f"No checkpoint has the id or label {text!r}"
        + (" in this part (another part has one)" if elsewhere else ""),
        field="/checkpoint_id",
        hint="Pass checkpoint_id from nx_create_checkpoint or nx_list_checkpoints, or "
        "a label from details.checkpoints"
        + (", or the part key of the part whose checkpoint it is" if elsewhere else ""),
        details={"checkpoints": listed},
    )


# ---------------------------------------------------------------------------
# nx_undo_operation


def _unknown(services) -> Any:
    """Gate 3t for a call whose own object (an operation, a checkpoint, redo items) is
    unknown or gone: ANY (the handler says so in the work part) while the work part is the
    agent's; else None: the usual rule (``part``, the agent's only part, else
    ``NX_AMBIGUOUS_PART``), so it never defaults to the engineer's part."""
    work = None
    with contextlib.suppress(Exception):
        work = services.work_part()
    if work is not None and counts(services, work):
        return ANY
    return None


def _undo_part(services, args) -> Any:
    """Gate 3t: the part of the operation ``operation`` names (None without it; when it
    is not in the history or its part is closed, :func:`_unknown`)."""
    operation = args.get("operation")
    if not isinstance(operation, str):
        return None
    for item in reversed(services.undo_history()):
        if item.get("operation_id") == operation and undo_marks.barrier(item) is None:
            if item.get("compensation"):
                break
            return part_by_key(services, item.get("part_id")) or _unknown(services)
    return _unknown(services)


SAVE_HINT = (
    "Its own save or close ends a part's undo history; mode compensate still deletes what "
    "create-only operations made and sets back values they set"
)


def _not_found(file: str, end: dict | None, other: bool) -> None:
    reason, why = _why_ended(end) if end is not None else (None, "")
    if end is not None:
        message = f"the history of {file} ends at its last operation: {why}"
    elif other:
        message = f"no operation of {file} is in the undo history (other parts' are)"
    else:
        message = "the undo history is empty"
    hint = (
        "The part's own save or close, or the engineer's own edits, end its undo history; "
        "delete or edit the objects instead"
    )
    if end is not None and reason != "engineer_change":
        hint = SAVE_HINT
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
    """The part's operations to undo, oldest first (:func:`_reach`); how is
    :func:`_plan`'s. Refusals inside ``tx.checks``."""
    reach, end = _reach(history, key, mode)
    if operation is None:
        if not reach:
            _not_found(file, end, any(undo_marks.barrier(h) is None for h in history))
        if steps > len(reach):
            _too_many(steps, _depth(tx.services, key), end)
        return list(reversed(reach[:steps]))
    found = next((h for h in history if h.get("operation_id") == operation and _own(h, key)), None)
    if found is None:
        fail(
            "NX_NOT_FOUND",
            f"{operation} is not an operation of {file} that can be undone (it is not in "
            "the part's undo history: another part's, already undone, or not a model "
            "change)",
            field="/operation",
            hint="nx_list_checkpoints shows last_operation; nx_get_operation shows what ran",
            details={"undo_depth": _depth(tx.services, key), "operation_id": operation},
            retry="never",
        )
    index = next((i for i, h in enumerate(reach) if h is found), None)
    if index is None:
        reason, why = _why_ended(end) if end is not None else ("mark_evicted", "")
        fail(
            "NX_NOT_FOUND",
            f"{operation} can no longer be undone: the history of {file} ends after it: {why}",
            field="/operation",
            hint=SAVE_HINT if reason != "engineer_change" else FIX_FORWARD,
            details={"undo_depth": _depth(tx.services, key), "reason": reason},
            retry="never",
        )
    if index and mode not in COMPENSATE:
        fail(
            "NX_DEPENDENCY_ORDER",
            f"{index} later operation(s) of {file} follow {operation}: NX's undo would "
            "revert them too",
            field="/operation",
            hint=f"Undo them too with steps {index + 1}, or pass mode compensate to "
            "delete only what it created and set back only the values it set",
            details={"later": [str(h["operation_id"]) for h in reversed(reach[:index])]},
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
        how, start, plan, blockers, dropped = _plan(tx, history, undone, mode=mode, what=what)
    by = tx.operation_id or ""
    extra: dict = {}
    if how == "nx":
        redo, references = _nx_undo(tx, undone[0]["mark"], start, undone, by)
    else:
        assert plan is not None
        why = _why_deleted(blockers, dropped)
        redo, extra = _delete_created(tx, plan, start, undone, by, why)
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
    """Set an in-session undo checkpoint in the work part (restore by id or label; a reused
    label restores its newest). Its part's own save or close, the engineer's edits or an undo
    past it expire it; other parts' work never does. Disk saves are never rolled back."""
    services = tx.services
    part = tx.part
    part_key = services.part_key(part)
    checkpoints = services.undo_checkpoints()
    same = [
        k for k in _labelled(checkpoints, label, part_key)
        if _reason(services, checkpoints[k]) is None
    ]  # fmt: skip
    mark = services.set_mark("NX MCP checkpoint: " + label)
    key = "cp_" + uuid.uuid4().hex
    checkpoints[key] = {
        "mark": mark,
        "part_id": part_key,
        "index": len(services.undo_history()),
        "label": label,
    }
    if same:
        tx.warn(
            "NX_NOTE",
            f"{file_name(part)} already has a checkpoint labelled {label!r} ({same[-1]}): a "
            "restore by label takes this newer one; restore the older one by its id",
        )
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
    """List this session's checkpoints (with the part key: only that part's) and the part's
    undo depth.
    Expired ones show available=false and expired_reason; compensated=true: a restore now
    deletes what the part made since instead of using NX's undo."""
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
                "compensated": reason is None and _compensated(services, cp),
            }
        )
    ops = _reach(history, key)[0] if key is not None else []
    return {
        "checkpoints": rows,
        "undo_depth": _depth(services, key),
        "last_operation": ops[0]["operation_id"] if ops else None,
    }


def _checkpoint_part(services, args) -> Any:
    """Gate 3t: the part a checkpoint belongs to (by id, or by a label only one part
    uses); None when parts share the label (``part``, else the usual rule, picks one);
    :func:`_unknown` when the checkpoint or its part is gone (the handler says so)."""
    text = args.get("checkpoint_id")
    if not isinstance(text, str):
        return None
    checkpoints = services.undo_checkpoints()
    if text not in checkpoints:
        owners = {checkpoints[k].get("part_id") for k in _labelled(checkpoints, text)}
        if len(owners) > 1:
            return None
    try:
        key = _lookup(services, checkpoints, text)
    except NXError:
        return _unknown(services)  # the handler reports it
    return part_by_key(services, checkpoints[key].get("part_id")) or _unknown(services)


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
             "Its part was saved or closed after it (NX dropped the checkpoint's undo mark)",
             field="/checkpoint_id", hint=STALE_HINT, details={"reason": reason})  # fmt: skip
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
    since. It stays restorable until it expires (nx_create_checkpoint says what expires it).
    Other parts' later work makes it compensate (delete what the part made, set values back),
    refused when that does not qualify. Save to persist."""
    services = tx.services
    checkpoints = services.undo_checkpoints()
    history = services.undo_history()
    with tx.checks():
        part_key = services.part_key(tx.part)
        key = _lookup(services, checkpoints, checkpoint_id, part_key)
        cp: dict = checkpoints[key]
        reason = _reason(services, cp)
        if reason:
            _restore_refusal(services, cp, reason)
        if cp["part_id"] != part_key:
            fail(
                "NX_CROSS_PART",
                f"The checkpoint belongs to {_file(services, cp['part_id'])}, not "
                f"{file_name(tx.part)}",
                field="/checkpoint_id",
                hint="Drop part: the checkpoint names its own part",
                details={"part_id": cp["part_id"]},
            )
        index = int(cp["index"])
        later = history[index:]
        undone = [h for h in later if _own(h, part_key)]
        how, start, plan = "nx", index, None
        blockers: list = []
        dropped: list = []
        alone = (
            len(undone) == len(later)
            and _mark_alive(services, cp)
            and all(_alive_entry(services, h) for h in undone)
        )
        if undone and not alone:
            what = f"{len(undone)} operation(s) of {file_name(tx.part)} since the checkpoint"
            how, start, plan, blockers, dropped = _plan(tx, history, undone, mode=None, what=what)
    extra: dict = {}
    if not undone:  # only other parts worked since: nothing of this part to undo
        _drop_own_mark(tx)
        references = "preserved"
    elif alone:
        _redo, references = _nx_undo(tx, cp["mark"], start, undone, key)
    elif how == "nx":
        _redo, references = _nx_undo(tx, undone[0]["mark"], start, undone, key)
    else:
        assert plan is not None
        why = _why_deleted(blockers, dropped)
        _redo, extra = _delete_created(tx, plan, start, undone, key, why)
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
    """Gate 3t: the one part with calls to redo (:func:`_unknown` when no loaded part has
    any: the handler says so; None when several have: the usual rules pick the part)."""
    keys = {item.get("part_id") for item in services.redo_steps()}
    if len(keys) > 1:
        return None
    found = part_by_key(services, next(iter(keys))) if keys else None
    return found or _unknown(services)


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
