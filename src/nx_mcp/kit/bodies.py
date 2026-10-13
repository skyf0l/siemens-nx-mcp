"""Which bodies a call covers: an owner's bodies, the work part, assembly occurrences.

    bodies.of(tx, obj)                 # body -> [body]; feature -> its bodies;
                                       # component -> occurrence bodies of it and its children
    bodies.scope(tx, "auto")           # work-part bodies (+ occurrences when an assembly)
    bodies.occurrences(component)      # [] when it or a parent is suppressed or unloaded
    bodies.solids(found, field)        # NX_WRONG_GEOMETRY for a sheet body
    bodies.measurable(found, scoped=True)  # (kept, skipped): no empty or topology results
    bodies.faulty(tx, found)           # tags UF body consistency faults (self-intersecting)
    bodies.expect_sound(tx, made, known=...)  # after a build: consistency faults fail
    bodies.scale_note(tx, made)        # warning text when the bodies span over 10 m
    bodies.expect_no_clash(tx, made)   # after a build: faces cutting each other fail

Occurrence bodies come from ``component.FindOccurrence(prototype_body)``, so
suppressed components, unloaded prototypes and reference sets are honoured the way
NX shows the assembly. Nested occurrences are reached depth first. ``tx.checkpoint()``
runs between components (long assembly walks stay cancellable).
"""

from __future__ import annotations

import contextlib
from typing import Any

from nx_mcp.core.errors import fail
from nx_mcp.kit import measure, parts, units

SCOPES = ("auto", "part", "assembly")


def is_component(obj) -> bool:
    return hasattr(obj, "FindOccurrence") and hasattr(obj, "GetChildren")


def is_body(obj) -> bool:
    return hasattr(obj, "IsSolidBody") and hasattr(obj, "GetFaces")


def suppressed(component) -> bool:
    """True when the component or one of its parents is suppressed."""
    node = component
    while node is not None:
        if getattr(node, "IsSuppressed", False):
            return True
        node = getattr(node, "Parent", None)
    return False


def occurrences(component) -> list:
    """Occurrence bodies of one component (not its children)."""
    if suppressed(component):
        return []
    prototype = getattr(component, "Prototype", None)
    out = []
    for body in getattr(prototype, "Bodies", None) or []:
        occurrence = component.FindOccurrence(body)
        if occurrence is not None:
            out.append(occurrence)
    return out


def _descendants(component) -> list:
    out, stack = [], [component]
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(reversed(list(node.GetChildren())))
    return out


def _unique(found) -> list:
    seen, out = set(), []
    for obj in found:
        tag = int(obj.Tag)
        if tag not in seen:
            seen.add(tag)
            out.append(obj)
    return out


def of(tx, obj) -> list:
    """Bodies of a body, a feature or a component occurrence (with its children)."""
    if is_component(obj):
        found = []
        for node in _descendants(obj):
            tx.checkpoint()
            found.extend(occurrences(node))
        return _unique(found)
    if is_body(obj):
        return [obj]
    get = getattr(obj, "GetBodies", None)
    if get is not None:
        return _unique(get())
    fail("NX_WRONG_KIND", "Select a body, a feature or a component")


def scope(tx, value="auto") -> list:
    """Bodies of the work part; ``assembly`` (or ``auto`` in an assembly) adds every
    loaded, unsuppressed occurrence body."""
    if value not in SCOPES:
        fail("NX_INVALID_ARGUMENT", "scope must be auto, part or assembly", field="/scope")
    part = tx.part
    found = list(part.Bodies)
    components = parts.components(part)
    if value == "assembly" or (value == "auto" and components):
        for component, _path in components:
            tx.checkpoint()
            found.extend(occurrences(component))
    return _unique(found)


def solids(found, field=None, what="solid bodies"):
    """``found`` when every body is solid, else ``NX_WRONG_GEOMETRY``."""
    sheets = [b for b in found if not getattr(b, "IsSolidBody", False)]
    if sheets:
        fail("NX_WRONG_GEOMETRY", f"This check needs {what}; a sheet body was given", field=field)
    return found


TOPOLOGY_FEATURE = "TopologyOptimizationFeature"


def faceless(body) -> bool:
    """True for a body without faces (False when NX cannot tell)."""
    try:
        return len(list(body.GetFaces() or [])) == 0
    except Exception:
        return False


def topology_result(body) -> bool:
    """True for a body a Topology Optimization feature owns: its result, a faceless copy of
    the design space until solved, then the optimized shape (NX 2406). It sits beside the
    design space body, so counting both counts the part twice."""
    try:
        features = list(body.GetFeatures() or [])
    except Exception:
        return False
    return any(type(f).__name__ == TOPOLOGY_FEATURE for f in features)


def measurable(found, scoped: bool) -> tuple[list, list]:
    """``(kept, skipped)``: faceless bodies (NX refuses to measure them: 875004 "Unknown
    feature type") and, for a ``scoped`` set (the part or assembly rather than chosen
    bodies), topology optimization results are skipped."""
    kept: list = []
    skipped: list = []
    for body in found:
        (skipped if faceless(body) or (scoped and topology_result(body)) else kept).append(body)
    return kept, skipped


def require(found, field=None):
    """``found`` when not empty, else ``NX_NOT_FOUND`` (nothing is loaded or visible)."""
    if not found:
        fail(
            "NX_NOT_FOUND",
            "No bodies: check suppression, loaded components and reference sets",
            field=field,
        )
    return found


# A built body spanning more than this (10 m) gets a scale warning: a value in the wrong
# units or a typo (a tube diameter 1e5) builds a valid but absurd body.
SCALE_LIMIT_MM = 10_000.0
SOUND_HINT = (
    "NX built a self-intersecting or invalid body. Check the inputs: section and guide "
    "swapped, a section larger than the path's bend radius, or an extreme size"
)


def _consistency(tx, body) -> tuple[int, list]:
    """``(fault count, [{code, message}])`` from UF ``AskBodyConsistency``; ``(0, [])``
    when UF cannot tell (no UF, or it raises)."""
    check = getattr(getattr(tx.uf, "Modeling", None), "AskBodyConsistency", None)
    if check is None:
        return 0, []
    try:
        count, codes, _tags = check(body.Tag)
    except Exception:
        return 0, []
    faults: list[dict[str, Any]] = []
    for code in list(codes or [])[:5]:
        fault: dict[str, Any] = {"code": int(code)}
        with contextlib.suppress(Exception):
            fault["message"] = str(tx.nx.NXException(int(code)).GetMessage())
        faults.append(fault)
    return int(count or 0), faults


def faulty(tx, found) -> set[int]:
    """Tags of the bodies in ``found`` UF finds inconsistent (before a change: a body that
    was already faulty does not fail the call)."""
    return {int(b.Tag) for b in found if _consistency(tx, b)[0]}


def expect_sound(tx, made, *, known=(), check="body_health") -> None:
    """After a build: ``NX_BUILD_FAILED`` (the kernel rolls back) when a body in ``made``
    has UF consistency faults (self-intersection, bad topology) it did not have before
    (``known``: tags from :func:`faulty`)."""
    skip = {int(t) for t in known}
    for body in made:
        if int(body.Tag) in skip:
            continue
        count, faults = _consistency(tx, body)
        if count:  # no body ref in details: the rollback retires it
            fail(
                "NX_BUILD_FAILED",
                f"NX built an invalid body ({count} consistency fault(s), e.g. "
                "self-intersecting faces); it was rolled back",
                hint=SOUND_HINT,
                details={"check": check, "faults": faults},
            )


def scale_note(tx, made) -> str | None:
    """A warning text when ``made`` spans more than 10 m (a value in the wrong units or a
    typo), else None."""
    if not made:
        return None
    try:
        low, high = measure.bounds(tx, list(made))
        span = max(h - lo for lo, h in zip(low, high, strict=True))
        unit = units.part_units(tx)
    except Exception:
        return None
    limit = SCALE_LIMIT_MM / units.MM_PER[unit]
    if span <= limit:
        return None
    return f"The result spans {span:.6g} {unit} (over 10 m): check the values and units"


CLASH_HINT = (
    "The body overlaps itself: parts of the path come closer than the section's size (a "
    "tube wider than its helix pitch, loops of the path crossing). Use a smaller section "
    "or move the path apart"
)


def face_clashes(tx, body) -> int:
    """Faces of ``body`` that cut its other faces (NX Examine Geometry, body face
    intersections): a self-overlapping solid, e.g. a tube wider than its helix pitch,
    which UF consistency passes (NX 2406, measured). 0 when NX cannot tell."""
    return len(clashing_faces(tx, body))


def clashing_faces(tx, body, part=None) -> list:
    """The faces :func:`face_clashes` counts (NX's failed objects of the body face
    intersection check), examined in ``part`` (default the work part); ``[]`` when NX
    cannot tell."""
    manager = getattr(part if part is not None else tx.part, "AnalysisManager", None)
    create = getattr(manager, "CreateExamineGeometryObject", None)
    if create is None:
        return []
    examine = None
    try:
        check = tx.nx.GeometricAnalysis.ExamineGeometry.Check.BodyFaceIntersections
        examine = create()
        examine.ClearAllChecks()
        examine.SetCheck(check)
        examine.ObjectsToExamine.Add([body])
        examine.Examine()
        return list(examine.GetFailedObjects(check) or [])
    except Exception:  # NX cannot examine this body: no verdict
        return []
    finally:
        if examine is not None:
            with contextlib.suppress(Exception):
                examine.Destroy()


def clashing(tx, found) -> set[int]:
    """Tags of the bodies in ``found`` whose faces already cut each other (before a
    change: such a body does not fail :func:`expect_no_clash`)."""
    return {int(b.Tag) for b in found if face_clashes(tx, b)}


def expect_no_clash(tx, made, *, known=(), check="body_health", hint=CLASH_HINT) -> None:
    """After a build: ``NX_BUILD_FAILED`` (the kernel rolls back) when a body in ``made``
    overlaps itself (:func:`face_clashes`) and did not before (``known``: tags from
    :func:`clashing`)."""
    skip = {int(t) for t in known}
    for body in made:
        if int(body.Tag) in skip:
            continue
        count = face_clashes(tx, body)
        if count:
            fail(
                "NX_BUILD_FAILED",
                f"NX built a self-overlapping body ({count} faces cut other faces of it); "
                "it was rolled back",
                hint=hint,
                details={"check": check, "face_intersections": count},
            )
