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

Each call's objects carry one name (``unique_name``: ``NX_MCP_<TYPE>_<n>``), the handle
``named`` finds them by again and ``delete`` removes them with the expressions their
builder added (``remember(..., expressions=)``: a continuity analysis adds its two
thresholds), for nx_delete_surface_analysis and the compensating undo.
"""

from __future__ import annotations

import contextlib

from nx_mcp.core.errors import fail, native

# part tag -> analysis objects the tools committed in it (cleared by delete_all)
_MADE: dict[int, list] = {}
# part tag -> analysis name (case-folded) -> expressions its builder added
_EXPRESSIONS: dict[int, dict[str, list[str]]] = {}
PREFIX = "NX_MCP_"
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


def remember(part, objects, expressions=()) -> None:
    """Record analysis ``objects`` committed in ``part`` for ``delete_all``, and the names
    of the ``expressions`` their builder added (deleted with them)."""
    found = _MADE.setdefault(_tag(part), [])
    known = {_tag(o) for o in found}
    found.extend(o for o in objects if _tag(o) not in known)
    for name in {_name(o).casefold() for o in objects} - {""}:
        _EXPRESSIONS.setdefault(_tag(part), {})[name] = list(expressions)


def _name(obj) -> str:
    try:
        return str(obj.Name or "")
    except Exception:  # a deleted object
        return ""


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


def _by_name(tx, part, name) -> list:
    """Objects of the work part called ``name`` (UF ``CycleByName``: also analyses this
    session did not make and NX does not list); [] for another part or without UF."""
    cycle = getattr(getattr(tx.uf, "Obj", None), "CycleByName", None)
    work = getattr(tx.session.Parts, "Work", None)
    if cycle is None or work is None or _tag(work) != _tag(part):
        return []
    out: list = []
    tag = 0  # the null tag starts the cycle; NX returns it (or None) at the end
    with contextlib.suppress(Exception):  # a host without the call: nothing found
        for _ in range(1000):
            tag = cycle(name, tag)
            if not tag:
                break
            out.append(tx.nx.TaggedObjectManager.GetTaggedObject(tag))
    return out


def named(tx, part, name) -> list:
    """The live analysis objects of ``part`` called ``name`` (any case)."""
    key = str(name).casefold()
    found = [o for o in of_part(tx, part) if _name(o).casefold() == key]
    known = {_tag(o) for o in found}
    if key.startswith(PREFIX.casefold()):  # names only the analysis tools give
        for obj in _by_name(tx, part, _name(found[0]) if found else str(name).upper()):
            if _tag(obj) not in known and _alive(tx, obj):
                found.append(obj)
                known.add(_tag(obj))
    return found


def names(tx, part) -> list[str]:
    """Names of the live analyses the tools made in ``part`` (``PREFIX``), sorted."""
    return sorted({n for n in map(_name, of_part(tx, part)) if n.startswith(PREFIX)})


def unique_name(tx, part, kind: str) -> str:
    """``NX_MCP_<KIND>_<n>``: the first ``n`` no live analysis of ``part`` is called."""
    base = f"{PREFIX}{kind.upper()}_"
    taken = {n.casefold() for n in names(tx, part)}
    n = 1
    while (base + str(n)).casefold() in taken or named(tx, part, base + str(n)):
        n += 1
    return base + str(n)


def _drop(tx, objects) -> None:
    """``UpdateManager`` delete + update of ``objects`` under an invisible undo mark."""
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


def delete(tx, part, name) -> tuple[int, list[str]]:
    """Delete the analysis objects of ``part`` called ``name`` and the expressions their
    builder added that nothing else uses; returns ``(objects, expressions deleted)``.
    Verified: none of them is left."""
    objects = named(tx, part, name)
    if not objects:
        return 0, []
    # remembered objects stay listed: of_part skips dead ones, and a rolled-back call
    # brings them back alive (still to delete before the part closes)
    _drop(tx, objects)
    left = [o for o in objects if _alive(tx, o)]
    if left:
        fail("NX_VERIFICATION_FAILED", f"NX kept {len(left)} of the {name} analysis objects")
    added = _EXPRESSIONS.get(_tag(part), {}).get(str(name).casefold(), [])
    return len(objects), drop_expressions(tx, part, added)


def drop_expressions(tx, part, wanted) -> list[str]:
    """Delete the expressions of ``part`` called one of ``wanted`` that no other expression
    uses (one still in use stays: never fatal); returns the names deleted."""
    out: list[str] = []
    collection = getattr(part, "Expressions", None)
    if collection is None or not wanted:
        return out
    for exp in [e for e in list(collection) if str(e.Name) in set(wanted)]:
        with contextlib.suppress(Exception):
            query = getattr(exp, "GetReferencingExpressions", None)
            if query is None or not list(query()):
                name = str(exp.Name)
                collection.Delete(exp)
                out.append(name)
    return out


def delete_all(tx, part) -> int:
    """Delete every analysis object of ``part`` (call before closing it); returns how many.
    Runs ``UpdateManager`` delete + update under its own undo mark."""
    objects = of_part(tx, part)
    _MADE.pop(_tag(part), None)
    _EXPRESSIONS.pop(_tag(part), None)
    if not objects:
        return 0
    _drop(tx, objects)
    return len(objects)
