"""Parked sketches: no sketch stays active in NX between calls (D5, phase P4).

NX has one active sketch for the whole session, and a model call in any part refuses
while one is active. So ``nx_create_sketch`` without ``finish`` deactivates its sketch at
the end of the call (``Deactivate`` with a model update, measured safe on NX 2406) and
*parks* it here: the agent still sees the sketch open (``active`` true, ``parked``
true), the sketch tools activate it by ref for each call and deactivate it again
(``kit.sketch.editing``), and ``nx_finish_sketch`` finishes it. A parked sketch counts as
the open sketch of its own part only: ``requires`` ``no_active_sketch`` and the sketch
tools refuse in that part exactly as they did while the sketch stayed active, and
calls in every other part run.

One parked sketch per part (``nx_create_sketch`` refuses while its part has one open).
The table lives on the services (``Services.parked_sketches``: ``{part_key: sketch}``);
an entry whose sketch NX no longer has (an undo, a rollback, a delete) or whose part
closed is dropped when read. Never raises.
"""

from __future__ import annotations

import contextlib

from nx_mcp.core.references import deleted


def _table(services) -> dict:
    try:
        return services.parked_sketches()
    except Exception:
        return {}


def _tag(obj) -> int | None:
    try:
        return int(obj.Tag)
    except Exception:
        return None


def _alive(sketch, part) -> bool:
    """Whether ``part`` still has ``sketch`` (NX undid or deleted it otherwise)."""
    if deleted(sketch):
        return False
    tag = _tag(sketch)
    try:
        return any(_tag(s) == tag for s in part.Sketches)
    except Exception:
        return tag is not None  # cannot list: trust a live wrapper


def parked(services, part):
    """The sketch parked in ``part``, or None (stale entries are dropped)."""
    if part is None:
        return None
    table = _table(services)
    try:
        key = services.part_key(part)
    except Exception:
        return None
    sketch = table.get(key)
    if sketch is None:
        return None
    if not _alive(sketch, part):
        table.pop(key, None)
        return None
    return sketch


def park(services, part, sketch) -> None:
    """Record ``sketch`` (deactivated by the caller) as ``part``'s open sketch."""
    with contextlib.suppress(Exception):
        _table(services)[services.part_key(part)] = sketch


def unpark(services, part) -> None:
    """``part`` has no open sketch any more (finished, or the part closed)."""
    with contextlib.suppress(Exception):
        _table(services).pop(services.part_key(part), None)


def is_parked(services, part, sketch) -> bool:
    """Whether ``sketch`` is the sketch parked in ``part``."""
    found = parked(services, part)
    return found is not None and _tag(found) == _tag(sketch)


def open_in(services, part) -> bool:
    """Whether ``part`` has a parked sketch (gate 4 ``no_active_sketch``)."""
    return parked(services, part) is not None
