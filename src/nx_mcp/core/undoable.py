"""Which agent operations an undo can revert without NX's undo, read from receipts only.

NX keeps one linear undo list for the whole session, so the undo of part A uses NX's undo
only while nothing else lies after A's undone operations. Otherwise the undo compensates
(``domains/session/compensate.py``): it deletes what A's operations created and sets back
the values they changed, which leaves every other part untouched. This module holds the
rules that need no NX object, shared by the compensating undo, the undo depth, the
checkpoint listing (``restorable``, ``blocked_by``) and the note on the call that makes a
checkpoint unrestorable (:func:`checkpoint_notes`, run from ``core/marks.settle``).

An operation is split into *units*: the call itself, or, for ``nx_run_batch`` and an
accepted ``nx_finish_preview``, each child call (its own ``changes`` in the result and its
own before-values, :meth:`nx_mcp.core.tx.Tx.note_restore`). Every unit must qualify:

- values its tool can set back: a ``@restorer`` hook for the unit's tool and the
  before-values the call kept (``tx.note_restore``); it deleted nothing but what a
  feature's state takes away (``STATE_RESULTS``: suppressing a feature takes its body),
  unless the call said its restorer brings deleted objects back (``brings_back``); a
  call noted ``after_delete`` is undone by deletion as below, then its restorer runs
  (a feature that hid its source bodies shows them again); or
- created objects of kinds an undo can delete: features (with their bodies, expressions
  and datums), sketches (with their curves, points, constraints and dimensions), curves,
  points, and every kind with a ``@deleter`` hook (components, drawing objects,
  explosions...); objects of other kinds it created must vanish with them (verified);
  nothing deleted, modified or made stale besides what deleting a created feature brings
  back: a feature consumes edges and faces (a blend, a chamfer) and may merge or delete
  bodies (a boolean) or change them; deleting the feature restores them (their old refs
  stay stale). Drawing views and sheets NX refreshed count as derived, not as changed.
  A row whose ref existed before the call (``ref_floor`` of the history entry) was not
  created by it: such a unit is refused, never deleted; or
- nothing at all, from a tool whose only change is session state (``STATE_ONLY``: a
  finished sketch, an update): nothing to undo.

A tool with a ``@restorer`` that kept no before-values is never undone by deletion (what
it changed may look created: unsuppressed features' bodies). Deletes and edits without
before-values never qualify: NX alone can revert them, and only while no other part's
work lies after them. :func:`fix_forward` words the way forward for a refused unit.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from nx_mcp.core import marks as undo_marks
from nx_mcp.core.catalogue import hooks
from nx_mcp.core.types import KIND_GROUPS, SHEET_KINDS

# Kinds an undo by deletion deletes itself (in this order: a feature takes its bodies and
# the sketch it is, a sketch its geometry).
DELETED_KINDS = ("feature", "sketch", "curve", "point")
# Kinds that go with a created feature (its bodies, expressions and datum objects).
WITH_FEATURE = frozenset({"body", "expression", *KIND_GROUPS["datum"]})
# Kinds that go with a created sketch (its constraints and dimensions).
WITH_SKETCH = frozenset({"sketch_constraint", "dimension"})
CREATED_KINDS = frozenset({*DELETED_KINDS, *WITH_FEATURE, *WITH_SKETCH})
# Objects NX refreshes when the model changes (a feature's drawing views update): a
# change to them is derived from the created features, not work of its own.
DERIVED_KINDS = SHEET_KINDS
# What deleting a created feature brings back: the bodies it merged, deleted or changed,
# and (stale refs) the faces and edges it consumed.
FEATURE_TAKES = frozenset({"body"})
FEATURE_STALE_PREFIXES = frozenset({"fc", "e", "b"})
UNDO_TOOLS = frozenset({"nx_undo_operation", "nx_restore_checkpoint", "nx_redo_operation"})
# Calls whose children are the units (each child's changes and before-values).
COMPOSITE = frozenset({"nx_run_batch", "nx_finish_preview"})
# What a feature's state change (suppression, an edit, a reorder) takes away and setting
# the state back brings back (under new refs): its bodies, datums, expressions, curves and
# points.
STATE_RESULTS = frozenset({*WITH_FEATURE, "curve", "point"})
# Tools whose only change, when they report no object, is session state (an open sketch
# finished, the model or the annotations brought up to date): a unit of one that
# created, changed and deleted nothing needs nothing undone.
STATE_ONLY = frozenset({"nx_finish_sketch", "nx_update_model", "nx_update_annotations"})


class Refusal(Exception):
    """Why an operation cannot be undone without NX's undo."""

    def __init__(
        self,
        code: str,
        reason: str,
        details: dict | None = None,
        unit: Unit | None = None,
        *,
        hint: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.details = details or {}
        self.unit = unit  # the unit refused, when one is (fix_forward words its way out)
        self.hint = hint  # the hint of the error behind it (a restorer's), kept for the caller


@dataclass
class Unit:
    """One call an undo by compensation reverts: an operation, or one child of it."""

    operation_id: str
    tool: str
    changes: dict
    restore: Any = None  # the before-values (``tx.note_restore``), None without
    part: str | None = None  # part key of a child's values; None: the operation's part
    index: int | None = None  # the child's step index, None for the call itself
    brings_back: bool = False  # its restorer brings back what it deleted (a state)
    floor: int | None = None  # refs numbered below it existed before the call
    inner: bool = False  # it changed objects earlier steps of the call created (rows dropped)
    after_delete: bool = False  # deleted as created, then its restorer runs (unhide sources)

    @property
    def what(self) -> str:
        step = f" step {self.index}" if self.index is not None else ""
        return f"{self.operation_id}{step} ({self.tool})"


# ---------------------------------------------------------------------------
# Receipts


def record_of(services, entry: dict) -> dict:
    try:
        return services.store.get(entry["operation_id"]) or {}
    except Exception:
        return {}


def changes_of(record: dict) -> dict:
    return (record.get("envelope") or {}).get("changes") or {}


def committed(entry: dict, record: dict) -> None:
    operation, tool = entry["operation_id"], entry.get("method") or ""
    if tool in UNDO_TOOLS or record.get("state") != "committed" or record.get("reverted_by"):
        raise Refusal(
            "NX_UNSUPPORTED_OPTION",
            f"{operation} ({tool}) is not a committed operation that created objects or set values",
            {"operation_id": operation},
        )


def units(services, entry: dict, record: dict | None = None) -> list[Unit]:
    """The units of a committed history entry (see the module docstring); raises
    :class:`Refusal` when it is not a committed agent operation."""
    record = record_of(services, entry) if record is None else record
    committed(entry, record)
    operation, tool = str(entry["operation_id"]), str(entry.get("method") or "")
    results = ((record.get("envelope") or {}).get("data") or {}).get("results")
    if tool in COMPOSITE and isinstance(results, list) and results:
        kept = {
            int(s["index"]): s
            for s in entry.get("child_restores") or []
            if isinstance(s, dict) and isinstance(s.get("index"), int)
        }
        out = []
        made: set = set()  # ids earlier steps of the call created
        for i, child in enumerate(results):
            child = child if isinstance(child, dict) else {}
            note = kept.get(i) or {}
            changes, inner = _without(child.get("changes") or {}, made)
            made.update(
                str(r.get("id")) for r in changes.get("created") or [] if isinstance(r, dict)
            )
            out.append(
                Unit(
                    operation,
                    str(child.get("tool") or note.get("tool") or ""),
                    changes,
                    note.get("restore"),
                    note.get("part"),
                    i,
                    bool(note.get("brings_back")),
                    _floor(entry),
                    inner,
                    bool(note.get("after_delete")),
                )
            )
        return out
    return [
        Unit(
            operation,
            tool,
            changes_of(record),
            entry.get("restore"),
            brings_back=bool(entry.get("restore_brings_back")),
            floor=_floor(entry),
            after_delete=bool(entry.get("restore_after_delete")),
        )
    ]


def _without(changes: dict, made: set) -> tuple[dict, bool]:
    """A step's ``changes`` without the rows of objects earlier steps of the same call
    created (curves added to the batch's own sketch, the sketch it then finished): undoing
    the call deletes those objects anyway. ``(changes, whether any row was dropped)``."""
    if not made:
        return changes, False
    out = dict(changes)
    dropped = False
    for name in ("modified", "stale_refs"):
        rows = list(changes.get(name) or [])
        kept = [r for r in rows if str(r.get("id") if isinstance(r, dict) else r) not in made]
        if len(kept) != len(rows):
            out[name], dropped = kept, True
    return out, dropped


def _floor(entry: dict) -> int | None:
    value = entry.get("ref_floor")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def own_changes(changes: dict, name: str) -> list:
    """``changes[name]`` without the derived objects NX refreshed (drawing views...)."""
    rows = changes.get(name) or []
    if name != "modified":
        return list(rows)
    return [r for r in rows if not (isinstance(r, dict) and r.get("kind") in DERIVED_KINDS)]


def restorer_of(unit: Unit, *, after: bool = False):
    """``(restorer, before-values)`` when ``unit``'s values can be set back, else None.
    A unit noted ``after_delete`` is undone by deletion first: it has a restorer only for
    the step after the deletion (``after``)."""
    restore = hooks("restorer").get(unit.tool)
    if unit.restore is None or restore is None or unit.after_delete != after:
        return None
    return restore, unit.restore


def settable(unit: Unit) -> None:
    """The receipt-only check of a unit whose values a restorer sets back: what it deleted
    must be what a feature's state takes away (``STATE_RESULTS``), or its call said its
    restorer brings deleted objects back (``brings_back``)."""
    taken = [r for r in unit.changes.get("deleted") or []
             if not (isinstance(r, dict) and r.get("kind") in STATE_RESULTS)]  # fmt: skip
    if taken and not unit.brings_back:
        raise Refusal(
            "NX_UNSUPPORTED_OPTION",
            f"{unit.what} deleted objects: setting its values back does not bring them back",
            {"operation_id": unit.operation_id, "changes": ["deleted"]},
            unit,
        )


def _prefix(ref) -> str:
    return str(ref).split(":", 1)[0]


def _number(ref) -> int | None:
    head, _, tail = str(ref or "").partition(":")
    return int(tail) if head and tail.isdigit() else None


def deletable_kinds() -> frozenset[str]:
    """Kinds an undo by deletion deletes itself: features, sketches, curves, points and
    every kind with a ``@deleter`` hook."""
    return frozenset({*DELETED_KINDS, *hooks("deleter")})


def no_change(unit: Unit) -> bool:
    """A unit of a ``STATE_ONLY`` tool that reported no object at all."""
    changes = unit.changes
    return unit.tool in STATE_ONLY and not any(
        changes.get(name) for name in ("created", "modified", "deleted", "stale_refs")
    )


def _empty(changes: dict) -> bool:
    return not any(changes.get(name) for name in ("created", "modified", "deleted", "stale_refs"))


def creates(unit: Unit) -> list:
    """The created rows of a create-only unit ([] for a ``STATE_ONLY`` unit that changed
    nothing), else a :class:`Refusal` (receipt only). A unit that created a feature may
    also have consumed faces and edges and merged, deleted or changed bodies: deleting the
    feature brings them back."""
    if no_change(unit) or (unit.inner and _empty(unit.changes)):
        return []  # nothing of its own: what it changed goes with the earlier steps' objects
    changes = unit.changes
    if unit.tool in hooks("restorer") and unit.restore is None and unit.floor is None:
        # without the call's ref floor, what it created cannot be told from what it
        # brought back (unsuppressed): a tool that sets values is never undone by deletion
        raise Refusal(
            "NX_UNSUPPORTED_OPTION",
            f"{unit.what} kept no values to set back (its before-values were not recorded)",
            {"operation_id": unit.operation_id, "changes": ["no before-values"]},
            unit,
        )
    created = list(changes.get("created") or [])
    feature = any(isinstance(r, dict) and r.get("kind") == "feature" for r in created)
    other = []
    for name in ("deleted", "modified"):
        rows = own_changes(changes, name)
        if feature:
            rows = [r for r in rows if not (isinstance(r, dict) and r.get("kind") in FEATURE_TAKES)]
        if rows:
            other.append(name)
    stale = list(changes.get("stale_refs") or [])
    if feature:
        stale = [r for r in stale if _prefix(r) not in FEATURE_STALE_PREFIXES]
    if stale:
        other.append("stale_refs")
    if other or not created:
        if other:
            message = f"{unit.what} changed objects it did not create ({', '.join(other)})"
        else:
            message = f"{unit.what} reported no object it created or changed"
        raise Refusal(
            "NX_UNSUPPORTED_OPTION",
            message + " and kept no values to set back",
            {"operation_id": unit.operation_id, "changes": other or ["created nothing"]},
            unit,
        )
    floor = unit.floor
    older = [
        str(r.get("id"))
        for r in created
        if floor is not None and (_number(r.get("id")) or floor) < floor
    ]
    if older:
        raise Refusal(
            "NX_UNSUPPORTED_OPTION",
            f"{unit.what} brought back {', '.join(older[:5])}, which existed before it "
            "(unsuppressed or restored, not created): deleting them would not undo it",
            {"operation_id": unit.operation_id, "refs": older[:20]},
            unit,
        )
    deletable = deletable_kinds()
    if not any(r.get("kind") in deletable for r in created):
        kinds = sorted({str(r.get("kind")).replace("_", " ") for r in created})
        raise Refusal(
            "NX_UNSUPPORTED_OPTION",
            f"{unit.what} created {', '.join(kinds)}: an undo by deletion cannot delete that kind",
            {"operation_id": unit.operation_id, "kinds": kinds},
            unit,
        )
    return created


def fix_forward(unit: Unit | None) -> str:
    """How to reach the state before ``unit`` when no undo can (receipt only): delete what
    it created (naming ``nx_delete_objects`` only for kinds it deletes), call its own tool
    again with the previous values, or create again what it deleted."""
    if unit is None:
        return (
            "Fix forward instead: delete what the operation created with nx_delete_objects, "
            "or call the tool that changed values again with the previous ones"
        )
    changes, tool = unit.changes, unit.tool
    created = [r for r in changes.get("created") or [] if isinstance(r, dict)]
    deleted = own_changes(changes, "deleted")
    modified = own_changes(changes, "modified")
    if tool.startswith("nx_delete_") or (deleted and not created and not modified):
        return (
            "Fix forward instead: create again what it deleted (only NX's undo brings "
            "deleted objects back, and only while no other part's work follows it)"
        )
    texts = []
    if created:
        deletable = deletable_kinds()
        kinds = sorted({str(r.get("kind")) for r in created})
        other = [k.replace("_", " ") for k in kinds if k not in deletable | WITH_FEATURE]
        ids = [str(r.get("id")) for r in created if r.get("kind") in deletable][:5]
        if ids:
            texts.append(f"delete what it created with nx_delete_objects ({', '.join(ids)})")
        if other:
            texts.append(
                f"remove the {', '.join(other)} it created with the delete or edit option "
                "of its own tools (nx_delete_objects cannot delete that kind)"
            )
    if modified or not created:
        if tool.startswith("nx_create_"):
            texts.append("set back by hand what it changed")
        else:
            texts.append(f"call {tool} again with the previous values (its result shows them)")
    return "Fix forward instead: " + "; ".join(texts)


def check(services, entry: dict, record: dict | None = None) -> list[Unit]:
    """The units of ``entry`` when its receipt admits an undo by compensation, else a
    :class:`Refusal` naming the first unit that does not."""
    found = units(services, entry, record)
    for unit in found:
        if restorer_of(unit) is not None:
            settable(unit)
        else:
            creates(unit)
    return found


def why_not(services, entry: dict, record: dict | None = None) -> Refusal | None:
    """The refusal of an undo by compensation of ``entry`` (receipt only), None when it
    qualifies."""
    try:
        check(services, entry, record)
    except Refusal as refusal:
        return refusal
    return None


def qualifies(services, entry: dict) -> bool:
    """Whether ``entry``'s receipt admits an undo by compensation. The NX objects are not
    read: the plan may still refuse (a dependent feature, a value changed since)."""
    return why_not(services, entry) is None


# ---------------------------------------------------------------------------
# The history, read per part


def own(item: dict, key) -> bool:
    """An operation of the part ``key`` (not a barrier, not an undo by deletion)."""
    return (
        undo_marks.barrier(item) is None
        and not item.get("compensation")
        and item.get("part_id") == key
    )


def _part_loaded(services, key) -> bool:
    from nx_mcp.core.target import part_by_key

    return part_by_key(services, key) is not None


def checkpoint_reason(services, cp: dict) -> str | None:
    """Why a checkpoint cannot be restored; None while it can (by NX's undo or by
    compensation). Only an undo or restore past it, its part's close, its part's own save
    (``marks.own_cut``) or the engineer's edits end it."""
    expired = cp.get("expired")
    if expired in ("undone_past", "part_closed", "engineer_change"):
        return expired
    key = cp.get("part_id")
    if expired and undo_marks.own_cut(cp, key):
        return expired
    if not _part_loaded(services, key):
        return "part_closed"
    for item in services.undo_history()[int(cp["index"]) :]:
        if undo_marks.barrier(item) == "engineer":
            return "engineer_change"
        if own(item, key) and undo_marks.own_cut(item, key):
            return item["expired"]
    return None


def checkpoint_compensated(services, cp: dict) -> bool:
    """Whether a restore of ``cp`` would undo the part's operations since without NX's
    undo: other parts' work, loads or closes lie after them, or NX dropped their marks."""
    key = cp.get("part_id")
    later = services.undo_history()[int(cp["index"]) :]
    mine = [h for h in later if own(h, key)]
    if not mine:
        return False
    first = next(i for i, h in enumerate(later) if h is mine[0])
    return any(not own(h, key) for h in later[first:]) or any(
        not entry_alive(services, h) for h in mine
    )


def entry_alive(services, item: dict) -> bool:
    """Whether NX's undo mark of a history entry still exists."""
    if item.get("expired"):
        return False
    try:
        return bool(services.mark_exists(item["mark"]))
    except Exception:
        return False


def checkpoint_blocker(services, cp: dict, current: dict | None = None) -> dict | None:
    """The first of the part's operations since ``cp`` that compensation cannot undo:
    ``{operation_id, tool, reason}``, else None. ``current``: ``{operation_id: record}``
    of a call whose receipt is not finished yet (:func:`pending_record`, or None: not
    judged)."""
    key = cp.get("part_id")
    for item in services.undo_history()[int(cp["index"]) :]:
        if not own(item, key):
            continue
        record = None
        operation = item.get("operation_id")
        if current is not None and operation in current:
            record = current[operation]
            if record is None:
                continue  # not known yet: judged on the next call
        refusal = why_not(services, item, record)
        if refusal is not None:
            return {"operation_id": operation, "tool": item.get("method"), "reason": refusal.reason}
    return None


def checkpoint_state(services, cp: dict, current: dict | None = None) -> dict:
    """``{reason, compensated, restorable, blocked_by}`` of a checkpoint now."""
    reason = checkpoint_reason(services, cp)
    compensated = reason is None and checkpoint_compensated(services, cp)
    blocker = checkpoint_blocker(services, cp, current) if reason is None else None
    return {
        "reason": reason,
        "compensated": compensated,
        "restorable": reason is None and not (compensated and blocker is not None),
        "blocked_by": blocker if reason is None and compensated else None,
        "fragile": blocker,  # restorable only by NX's undo, while nothing else follows
    }


def _file(part) -> str:
    from nx_mcp.core.target import file_name

    return file_name(part)


def pending_record(tx) -> dict | None:
    """The receipt-like record of a committed call whose receipt is not finished yet: its
    ``changes`` and, for a batch or an accepted preview, its steps' results (each step
    is judged on its own, as the finished receipt will be). None before its changes are
    known (a failed call)."""
    changes = getattr(tx, "changes", None)
    if changes is None:
        return None
    data = {"results": list(tx.children)} if getattr(tx, "children", None) else {}
    return {"state": "committed", "envelope": {"changes": changes, "data": data}}


def checkpoint_notes(services, tx) -> None:
    """After a committed non-read call: a note on the call when it makes a checkpoint of
    its own part unrestorable, or when it is the first operation since a checkpoint that
    only NX's undo reverts while other parts are loaded (the checkpoint then stays
    restorable only while no other part changes). Only the call that is the cause says so,
    once per checkpoint; a later call never repeats it. Never raises."""
    with contextlib.suppress(Exception):
        _checkpoint_notes(services, tx)


def _checkpoint_notes(services, tx) -> None:
    if tx is None or tx.spec.effect == "read" or tx.spec.name in UNDO_TOOLS:
        return
    target = getattr(tx, "target", None)
    key = getattr(target, "key", None)
    if key is None:
        work = services.work_part()
        key = services.part_key(work) if work is not None else None
    if key is None:
        return
    current = {tx.operation_id: pending_record(tx)}
    several = len(list(services.parts() or [])) > 1
    from nx_mcp.core.target import part_by_key

    part = part_by_key(services, key)
    for cp_id, cp in services.undo_checkpoints().items():
        if cp.get("part_id") != key:
            continue
        state = checkpoint_state(services, cp, current)
        was = cp.get("restorable", True)
        cp["restorable"] = state["restorable"]
        label = str(cp.get("label") or cp_id)
        where = _file(part) if part is not None else "its part"
        if state["reason"] is None and was and not state["restorable"]:
            blocker = state["blocked_by"] or {}
            if blocker.get("operation_id") != tx.operation_id and cp.get("fragile_noted"):
                continue  # its own call already said it was restorable only while alone
            cp["fragile_noted"] = True
            tx.warn(
                "NX_UPDATE_WARNING",
                f"Checkpoint {label!r} of {where} can no longer be restored: "
                f"{blocker.get('reason') or 'an operation since cannot be undone'}, and other "
                "parts' work or a dropped undo mark keeps NX's undo from reaching it; fix "
                "forward instead",
            )
            continue
        fragile = state["fragile"]
        if (
            state["restorable"]
            and several
            and fragile is not None
            and fragile.get("operation_id") == tx.operation_id
            and not cp.get("fragile_noted")
        ):
            cp["fragile_noted"] = True
            tx.warn(
                "NX_NOTE",
                f"Checkpoint {label!r} of {where} can now be restored only while no other "
                f"part changes: {fragile.get('reason')}, so only NX's undo reverts it",
            )
