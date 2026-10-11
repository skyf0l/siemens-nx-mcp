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

History entries are ``{mark, part_id, operation_id, method}``; a barrier has
``mark`` None and ``load`` (the loaded parts' paths); an entry or checkpoint whose
mark is gone gets ``expired`` (the reason).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

LOAD_EFFECTS = ("file", "session")  # model calls undo their own loads
LOADS_LISTED = 10
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


def expire_dropped(services, reason: str) -> int:
    """Give every tracked entry whose mark NX dropped ``reason``; how many."""
    count = 0
    for group in _tracked(services):
        for entry in group:
            if not _alive(services, entry):
                entry["expired"] = reason
                count += 1
    return count


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
        expire_dropped(services, reason)
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
