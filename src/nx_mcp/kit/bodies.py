"""Which bodies a call covers: an owner's bodies, the work part, assembly occurrences.

    bodies.of(tx, obj)                 # body -> [body]; feature -> its bodies;
                                       # component -> occurrence bodies of it and its children
    bodies.scope(tx, "auto")           # work-part bodies (+ occurrences when an assembly)
    bodies.occurrences(component)      # [] when it or a parent is suppressed or unloaded
    bodies.solids(found, field)        # NX_WRONG_GEOMETRY for a sheet body

Occurrence bodies come from ``component.FindOccurrence(prototype_body)``, so
suppressed components, unloaded prototypes and reference sets are honoured the way
NX shows the assembly. Nested occurrences are reached depth first. ``tx.checkpoint()``
runs between components (long assembly walks stay cancellable).
"""

from __future__ import annotations

from nx_mcp.core.errors import fail
from nx_mcp.kit import parts

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


def require(found, field=None):
    """``found`` when not empty, else ``NX_NOT_FOUND`` (nothing is loaded or visible)."""
    if not found:
        fail(
            "NX_NOT_FOUND",
            "No bodies: check suppression, loaded components and reference sets",
            field=field,
        )
    return found
