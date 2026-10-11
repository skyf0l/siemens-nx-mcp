"""Busy parts: a close of a part other calls are working in (D8, phase P4).

Calls carry no caller identity (no agent label; ``part`` names a part, never a caller),
so the rule reads only the order of calls. It applies while two or more of the agent's
parts are loaded (``core/target.agent_parts``, as rule G counts them); with one, every
call behaves as before.

- A part is *busy* for ``BUSY_SECONDS`` after a committed changing call worked in it:
  its target, or the part ``nx_create_part``/``nx_open_part`` made or reused
  (``tx.worked_in``), or the part a call without either loaded. Reads and view effects
  (captures, cameras, highlights) never make a part busy.
- The *stream* is the part the last committed changing call worked in.
- ``nx_close_part`` refuses a busy part with ``NX_PART_BUSY`` while the stream is another
  part that is still loaded: the previous call worked elsewhere, so the close does not
  follow the calls that worked in this part. A close right after working in the part
  (the stream is the part) closes, and so does a run of closes (once a part closes, the
  stream names a part that is gone). ``force`` closes anyway.
- ``nx_save_part`` of such a part saves with a note (``note``).
- Closing an assembly keeps a component part that a call worked in within
  ``BUSY_SECONDS`` after the assembly was loaded (``component_reason``); components the
  assembly's own calls loaded are not busy themselves.

Limits, stated in the refusal: when agents call at the same time, an agent's own close
that comes right after another agent's call in another part is refused as well (repeat
it with ``force``), and a close right after the owner's own call is not.
"""

from __future__ import annotations

import contextlib
import time

from nx_mcp.core import target as targets
from nx_mcp.core.errors import fail

BUSY_SECONDS = 300.0
CHANGING = ("model", "file", "session")


def _table(services) -> dict:
    try:
        return services.part_activity()
    except Exception:
        return {}


def _entries(services) -> dict:
    return _table(services).setdefault("parts", {})


def _tag(part) -> int | None:
    try:
        return int(part.Tag)
    except Exception:
        return None


def record(services, spec, target, tx, loaded_before: frozenset | None, now=None) -> None:
    """After a committed changing call: stamp the parts it worked in and loaded and move
    the stream. ``loaded_before``: the tags of the parts loaded before the call. Never
    raises."""
    if spec.effect not in CHANGING:
        return
    with contextlib.suppress(Exception):
        _record(services, spec, target, tx, loaded_before, time.time() if now is None else now)


def _record(services, spec, target, tx, loaded_before, now) -> None:
    entries = _entries(services)
    operation_id = getattr(tx, "operation_id", None)

    def stamp(key, *, worked: bool, loaded: bool = False) -> None:
        entry = entries.setdefault(key, {})
        if worked:
            entry.update(at=now, tool=spec.name, operation_id=operation_id)
        if loaded:
            entry["loaded_at"] = now

    stream = None
    if target is not None:
        stamp(target.key, worked=True)
        stream = target.key
    for key in getattr(tx, "extra_parts", None) or ():  # batch children in other parts
        stamp(key, worked=True)
    worked_in = getattr(tx, "worked_in", None)
    if worked_in is not None:
        key = services.part_key(worked_in)
        stamp(key, worked=True)
        stream = stream or key
    if loaded_before is not None:
        loaded = [p for p in services.parts() or [] if _tag(p) not in loaded_before]
        for part in loaded:
            stamp(services.part_key(part), worked=False, loaded=True)
        if stream is None and loaded:
            stream = services.part_key(loaded[0])
            stamp(stream, worked=True)
    if stream is not None:
        _table(services)["stream"] = stream
    for key in [k for k, e in entries.items() if now - _newest(e) > 2 * BUSY_SECONDS]:
        entries.pop(key, None)


def _newest(entry: dict) -> float:
    return max(float(entry.get("at") or 0.0), float(entry.get("loaded_at") or 0.0))


def _age(entry: dict | None, now: float) -> float | None:
    """Seconds since a call worked in the part, None when it is not busy."""
    if not entry or entry.get("at") is None:
        return None
    age = now - float(entry["at"])
    return age if age <= BUSY_SECONDS else None


def facts(services, part, now=None) -> dict | None:
    """Why a close of ``part`` is refused, or None: busy, two or more agent parts
    loaded, and the stream is another loaded part."""
    now = time.time() if now is None else now
    try:
        if len(targets.agent_parts(services)) < 2:
            return None
        key = services.part_key(part)
        entry = _entries(services).get(key)
        age = _age(entry, now)
        stream = _table(services).get("stream")
        if age is None or entry is None or stream is None or stream == key:
            return None
        other = targets.part_by_key(services, stream)
        if other is None:
            return None
    except Exception:
        return None
    return {
        "part": targets.file_name(part),
        "changed_seconds_ago": round(age, 1),
        "last_change": {"tool": entry.get("tool"), "operation_id": entry.get("operation_id")},
        "last_call_part": targets.file_name(other),
        "busy_seconds": BUSY_SECONDS,
    }


def refuse(services, part, *, field: str = "/part") -> None:
    """``NX_PART_BUSY`` when :func:`facts` says so (callers skip it with ``force``)."""
    found = facts(services, part)
    if found is None:
        return
    name = found["part"]
    fail(
        "NX_PART_BUSY",
        f"{name} was changed {found['changed_seconds_ago']:.0f} s ago "
        f"({found['last_change']['tool']}) and the last call worked in "
        f"{found['last_call_part']}: another agent may be working in {name}",
        field=field,
        hint=f"If {name} is your own part, repeat the call with force: true; otherwise "
        "leave it to the agent working in it",
        details=found,
    )


def note(services, part) -> str | None:
    """The save warning for a part :func:`facts` would refuse to close, else None."""
    found = facts(services, part)
    if found is None:
        return None
    return (
        f"{found['part']} was changed {found['changed_seconds_ago']:.0f} s ago "
        f"({found['last_change']['tool']}) while the last call worked in "
        f"{found['last_call_part']}: if another agent is working in it, this saved its "
        "current state"
    )


def loaded_at(services, part) -> float:
    """When ``part`` was loaded (0: before the table knew it)."""
    with contextlib.suppress(Exception):
        entry = _entries(services).get(services.part_key(part)) or {}
        return float(entry.get("loaded_at") or 0.0)
    return 0.0


def component_reason(services, part, since: float, now=None) -> str | None:
    """Why an assembly close keeps the component ``part``: a call worked in it within
    ``BUSY_SECONDS``, after ``since`` (the assembly's load). None otherwise."""
    now = time.time() if now is None else now
    try:
        entry = _entries(services).get(services.part_key(part))
    except Exception:
        return None
    age = _age(entry, now)
    if age is None or entry is None or float(entry["at"]) <= since:
        return None
    return (
        f"in use: {entry.get('tool')} worked in it {age:.0f} s ago, after the assembly "
        "was loaded; close it with nx_close_part when it is free"
    )
