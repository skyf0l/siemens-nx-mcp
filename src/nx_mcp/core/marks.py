"""NX undo marks around a call: load barriers and why marks went away (gate 14).

NX keeps one undo list for the whole session. Measured on NX 2406:

- Loading a part without displaying it (``OpenBase``, ``FileNew`` with
  ``MakeDisplayedPart`` false, ``AddComponent``) keeps every mark, but ``UndoToMark``
  to a mark set before the load unloads that part again (with its work). A load by a
  file or session call is therefore a *barrier* in the agent's undo history: an undo
  or a checkpoint restore never crosses it. A model call that loads a part (a
  component from a file, an import into a new part) needs none: undoing that call is
  meant to unload what it loaded.
- A save, a plain ``Close`` of any part, a load that displays the part and a plain
  ``SetDisplay`` drop every mark (``kit.parts.show`` keeps them).
- NX keeps about 180 marks for the whole session and drops the oldest first.

``watch(services)`` runs before a non-read call's undo mark, ``settle(...)`` after the
call, committed or not. ``settle`` records the barrier of the parts a file or session
call loaded, and when the call dropped every mark the agent tracks it gives the
dropped history entries and checkpoints their reason: ``saved_or_closed`` (a part
closed, or none loaded), ``part_loaded`` (a part loaded) or ``display_reset`` (the
display part changed). Marks NX evicted are ``mark_evicted``, with a note in the
result. Both never raise.

History entries are ``{mark, part_id, operation_id, method}``; an entry or checkpoint
whose mark is gone gets ``expired`` (the reason) and ``expired_by`` (the part key of the
call that dropped it: the part it saved, closed, loaded or worked in; None when unknown).
The history is one list for the whole session, read per part
(``domains/session/history.py``): another part's save, close or load ends neither this
part's history nor its checkpoints (``foreign_cut``), it only keeps NX's undo from
reaching back over it. Barriers (:func:`barrier`) have ``mark`` None, because NX's undo
is linear:

- ``load`` (the loaded parts' paths): a part load by a file or session call; NX's undo
  never crosses it (it would unload the part), an undo by deletion does;
- ``close`` (the closed parts' paths): a part closed with its undo marks kept (an undo
  across it would load the part again; :func:`close_barrier`); the same;
- ``engineer``: the engineer changed the model between agent calls (the host's mark
  fingerprint, :func:`engineer_change`). No agent undo or restore crosses it, so none
  reverts the engineer's work; their edits stay.

An entry with ``compensation`` is a committed undo that deleted what create-only calls
made instead of using NX's undo (other parts' work came after them). It is never undone
itself (NX's undo would bring the deleted objects back) and blocks the NX undo of every
older entry, of every part.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

LOAD_EFFECTS = ("file", "session")  # model calls undo their own loads
LOADS_LISTED = 10
BARRIERS = ("load", "close", "engineer")
MARK_CAP_TEXT = "NX keeps about 180 undo marks for the whole session and drops the oldest first"


@dataclass
class Watch:
    parts: dict  # tag -> loaded part before the call
    display: int  # display part tag (0: none, or unknown)
    newest: list = field(default_factory=list)  # newest tracked entries with a live mark
    oldest: list = field(default_factory=list)  # oldest tracked entries with a live mark


def _tag(part) -> int:
    try:
        return int(part.Tag) if part is not None else 0
    except Exception:
        return 0


def _display(services) -> int:
    try:
        return _tag(services.session.Parts.Display)
    except Exception:
        return 0


def barrier(entry: dict) -> str | None:
    """The barrier kind of a history entry (``load``, ``close``, ``engineer``), else None."""
    if entry.get("engineer"):
        return "engineer"
    if entry.get("close") is not None:
        return "close"
    if entry.get("load") is not None:
        return "load"
    return None


def remove_entries(services, entries) -> None:
    """Take ``entries`` (by identity) out of the history; checkpoints keep pointing at the
    same place (their ``index`` counts the entries before them)."""
    history = services.undo_history()
    gone = {id(e) for e in entries}
    positions = [i for i, h in enumerate(history) if id(h) in gone]
    if not positions:
        return
    for cp in services.undo_checkpoints().values():
        index = int(cp.get("index", 0))
        cp["index"] = index - sum(1 for i in positions if i < index)
    history[:] = [h for h in history if id(h) not in gone]


def engineer_change(services, display_changed: bool = False) -> None:
    """The engineer changed the model between agent calls (the host's mark fingerprint):
    a global ``engineer`` barrier ends every part's undo there (the history before it
    stays listed), every checkpoint expires (``engineer_change``; ``display_reset`` for
    marks a display switch dropped), nothing is left to redo and every part's revision
    moves. Never raises."""
    with contextlib.suppress(Exception):
        history = services.undo_history()
        for item in history:
            if (
                display_changed
                and item.get("mark") is not None
                and not item.get("expired")
                and not _alive(services, item)
            ):
                item["expired"] = "display_reset"
        for cp in services.undo_checkpoints().values():
            if display_changed and not cp.get("expired") and not _alive(services, cp):
                cp["expired"] = "display_reset"
            cp.setdefault("expired", "engineer_change")
        if history and barrier(history[-1]) != "engineer":
            history.append(
                {"mark": None, "part_id": None, "operation_id": None, "method": "engineer",
                 "engineer": True}
            )  # fmt: skip
        services.redo_steps().clear()
    bump = getattr(services, "bump_part_revision", None)
    if bump is not None:
        with contextlib.suppress(Exception):
            bump(None)


def close_barrier(services, paths: list, part_id, operation_id: str | None, method: str) -> None:
    """After a part close that kept NX's undo marks (``CloseRetainUndoMarks``): undoing
    across it would load the part again, so it ends every part's undo."""
    with contextlib.suppress(Exception):
        if has_undo_state(services):
            services.undo_history().append(
                {"mark": None, "part_id": part_id, "operation_id": operation_id,
                 "method": method, "close": list(paths)[:LOADS_LISTED]}
            )  # fmt: skip


def _tracked(services) -> tuple[list, list]:
    """``(history entries, checkpoints)`` that still name a mark not known to be gone."""
    history = [
        h
        for h in services.undo_history()
        if h.get("mark") is not None and h.get("load") is None and not h.get("expired")
    ]
    checkpoints = [cp for cp in services.undo_checkpoints().values() if not cp.get("expired")]
    return history, checkpoints


def _alive(services, entry) -> bool:
    try:
        return bool(services.mark_exists(entry["mark"]))
    except Exception:
        return True  # unknown: present


def watch(services) -> Watch | None:
    """Before a non-read call: the loaded parts, the display part and the newest and
    oldest tracked marks that still exist. Tracked marks NX already dropped are
    ``mark_evicted`` (the engineer's changes are expired by the host before)."""
    try:
        history, checkpoints = _tracked(services)
        newest = [e for e in history[-1:] + checkpoints[-1:] if _alive(services, e)]
        oldest = []
        for group in (history, checkpoints):
            for entry in group:
                if _alive(services, entry):
                    oldest.append(entry)
                    break
                entry["expired"] = "mark_evicted"
        parts = {_tag(p): p for p in services.parts() or []}
        return Watch(parts, _display(services), newest, oldest)
    except Exception:
        return None


def _present(services, entry) -> bool:
    """Still tracked: an undo removes history entries and expires checkpoints."""
    if entry.get("expired"):
        return False
    return any(entry is h for h in services.undo_history()) or any(
        entry is cp for cp in services.undo_checkpoints().values()
    )


def expire_dropped(services, reason: str, by=None) -> int:
    """Give every tracked entry whose mark NX dropped ``reason`` (and ``expired_by``: the
    part key of the call that dropped it); how many."""
    count = 0
    for group in _tracked(services):
        for entry in group:
            if not _alive(services, entry):
                entry["expired"] = reason
                entry["expired_by"] = by
                count += 1
    return count


def own_cut(entry: dict, key) -> bool:
    """Whether ``entry`` (a history entry or checkpoint of the part ``key``) lost its NX
    mark to that part's own save or close (or to a save the kernel cannot attribute):
    the agent's own decision, which ends the part's default undo and its checkpoints. A
    mark another part's save, close or load dropped, or one NX evicted, is a
    :func:`foreign_cut`: the part's undo reaches over it by deletion."""
    reason = entry.get("expired")
    if not reason or reason in ("mark_evicted", "undone_past", "engineer_change", "part_closed"):
        return False
    by = entry.get("expired_by")
    return by == key or (by is None and reason == "saved_or_closed")


def own_save(services, key) -> None:
    """After the part ``key`` was saved: its history entries and checkpoints whose marks an
    earlier foreign cut dropped are now cut by its own save (NX would have dropped them
    here). Never raises."""
    with contextlib.suppress(Exception):
        for group in (services.undo_history(), services.undo_checkpoints().values()):
            for entry in group:
                if entry.get("part_id") == key and foreign_cut(entry, key):
                    entry["expired"], entry["expired_by"] = "saved_or_closed", key


def foreign_cut(entry: dict, key) -> bool:
    """Whether ``entry``'s NX mark is gone for a reason that is not the part's own (see
    :func:`own_cut`)."""
    reason = entry.get("expired")
    return bool(reason) and reason in (
        "mark_evicted", "saved_or_closed", "part_loaded", "display_reset"
    ) and not own_cut(entry, key)  # fmt: skip


def _call_part(services, seen: Watch, tx, loaded: list, closed: list):
    """The part key of the part a call worked in (its target, else the part it loaded or
    closed), None when unknown: who dropped the marks."""
    with contextlib.suppress(Exception):
        target = getattr(tx, "target", None) if tx is not None else None
        if target is not None:
            return target.key
    with contextlib.suppress(Exception):
        if loaded:
            return services.part_key(loaded[0])
    with contextlib.suppress(Exception):
        if closed:
            return services.part_key(seen.parts[closed[0]])
    with contextlib.suppress(Exception):
        worked = getattr(tx, "worked_in", None) if tx is not None else None
        if worked is not None:
            return services.part_key(worked)
    return None


def has_undo_state(services) -> bool:
    """Whether an undo or a restore could reach back at all (else no barrier is needed)."""
    history, checkpoints = _tracked(services)
    return bool(history or checkpoints)


def settle(services, seen: Watch | None, spec, operation_id: str, tx=None) -> None:
    """After a non-read call: barriers for the parts it loaded, reasons for the marks it
    dropped, a note when NX evicted marks. Never raises."""
    if seen is None:
        return
    with contextlib.suppress(Exception):
        _settle(services, seen, spec, operation_id, tx)


def _settle(services, seen: Watch, spec, operation_id: str, tx) -> None:
    now = {_tag(p): p for p in services.parts() or []}
    loaded = [p for tag, p in now.items() if tag not in seen.parts]
    closed = [tag for tag in seen.parts if tag not in now]
    watched = [e for e in seen.newest if _present(services, e)]
    if watched and not any(_alive(services, e) for e in watched):
        if closed:
            reason = "saved_or_closed"
        elif loaded:
            reason = "part_loaded"
        elif _display(services) != seen.display:
            reason = "display_reset"
        else:
            reason = "saved_or_closed"  # a save (or a part loaded and closed again)
        expire_dropped(services, reason, _call_part(services, seen, tx, loaded, closed))
        if tx is not None and reason != "saved_or_closed":  # saves and closes say so
            tx.warn(
                "NX_NOTE",
                "NX dropped every undo mark during this call ("
                + (
                    "a part load that displayed it"
                    if reason == "part_loaded"
                    else "a display switch"
                )
                + "): earlier operations can no longer be undone",
            )
    else:
        evicted = [e for e in seen.oldest if _present(services, e) and not _alive(services, e)]
        if evicted:
            count = expire_dropped(services, "mark_evicted")
            if tx is not None and count:
                tx.warn(
                    "NX_NOTE",
                    f"{MARK_CAP_TEXT}: {count} older agent operation(s) or checkpoint(s) "
                    "can no longer be undone or restored",
                )
    if spec.effect in LOAD_EFFECTS and loaded and has_undo_state(services):
        barrier: dict[str, Any] = {
            "mark": None,
            "part_id": services.part_key(loaded[0]),
            "operation_id": operation_id,
            "method": spec.name,
            "load": [services.part_path(p) for p in loaded[:LOADS_LISTED]],
        }
        services.undo_history().append(barrier)
