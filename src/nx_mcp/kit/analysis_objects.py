"""NX analysis objects (``GeometricAnalysis``: reflection, slope, distance, radius, combs,
continuity) the tools create, and their removal before a part closes.

NX 2406 measured (2026-10-13): an analysis object committed in a part that is NOT the
displayed part stays registered with the session's analysis display after that part is
closed unsaved. The next analysis NX displays (any part, any agent) then demand-loads the
closed part ("Load on demand called on '<part>.prt'"), and a never-saved part raises a
blocking "Could not open <part>.prt - File not found" dialog. Objects committed in the
displayed part closed cleanly (two parts in a row, live). So:

- tools create analysis objects only in the displayed part (``require_displayed``);
- ``remember`` records every object a tool commits, per part;
- ``delete_all(tx, part)`` deletes a part's analysis objects (remembered ones plus any its
  ``AnalysisManager.AnalysisObjects`` lists) before the part closes.
"""

from __future__ import annotations

import contextlib

from nx_mcp.core.errors import fail, native

# part tag -> analysis objects the tools committed in it (cleared by delete_all)
_MADE: dict[int, list] = {}
HINT = (
    "Display the part first (nx_activate_part), or get numbers without a display object: "
    "nx_analyze_faces (normals, draft), nx_measure_distance"
)


def _tag(obj) -> int:
    return int(obj.Tag)


def is_displayed(tx, part=None) -> bool:
    """Whether ``part`` (default the work part) is NX's displayed part."""
    part = tx.part if part is None else part
    shown = getattr(tx.session.Parts, "Display", None)
    return shown is not None and part is not None and _tag(shown) == _tag(part)


def require_displayed(tx, field=None) -> None:
    """``NX_WRONG_PART_CONTEXT`` unless the work part is the displayed part."""
    if not is_displayed(tx):
        fail(
            "NX_WRONG_PART_CONTEXT",
            "Analysis display objects are made only in the displayed part: NX keeps one made "
            "in a background part registered after that part closes",
            field=field,
            hint=HINT,
        )


def remember(part, objects) -> None:
    """Record analysis ``objects`` committed in ``part`` for ``delete_all``."""
    found = _MADE.setdefault(_tag(part), [])
    known = {_tag(o) for o in found}
    found.extend(o for o in objects if _tag(o) not in known)


UF_OBJ_ALIVE = 3  # UF_OBJ_ask_status: an object that exists (not deleted or undone)


def _alive(tx, obj) -> bool:
    try:
        return int(tx.uf.Obj.AskStatus(_tag(obj))) == UF_OBJ_ALIVE
    except Exception:  # a deleted or undone NX object
        return False


def of_part(tx, part) -> list:
    """The part's analysis objects: the ones tools made that still exist, then any other
    its ``AnalysisManager.AnalysisObjects`` lists (NX 2406 lists only some types)."""
    found = [o for o in _MADE.get(_tag(part), []) if _alive(tx, o)]
    known = {_tag(o) for o in found}
    collection = getattr(getattr(part, "AnalysisManager", None), "AnalysisObjects", None)
    listed: list = []
    with contextlib.suppress(Exception):  # not iterable on this host
        listed = list(collection or [])
    found.extend(o for o in listed if _alive(tx, o) and _tag(o) not in known)
    return found


def delete_all(tx, part) -> int:
    """Delete every analysis object of ``part`` (call before closing it); returns how many.
    Runs ``UpdateManager`` delete + update under its own undo mark."""
    objects = of_part(tx, part)
    _MADE.pop(_tag(part), None)
    if not objects:
        return 0
    session = tx.session
    with native(default="NX_UPDATE_FAILED"):
        mark = session.SetUndoMark(
            tx.nx.Session.MarkVisibility.Invisible, "NX MCP analysis cleanup"
        )
        session.UpdateManager.ClearErrorList()
        session.UpdateManager.AddToDeleteList(objects)
        errors = session.UpdateManager.DoUpdate(mark)
    if errors:
        fail("NX_UPDATE_FAILED", f"Deleting {len(objects)} analysis objects: {errors} errors")
    return len(objects)
