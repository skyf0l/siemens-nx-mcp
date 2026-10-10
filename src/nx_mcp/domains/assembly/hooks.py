"""Assembly hooks: listers (``nx_list_objects``) and deleters (``nx_delete_objects``).

``@lister(kind)`` is ``fn(tx, owner, owner_kind) -> [(obj, kind, extra)]``;
``@deleter(kind)`` is ``fn(tx, objects) -> None`` and deletes inside the caller's mark.
An explosion shown in a view is refused (``NX_IN_USE``): show the assembled state
there first with ``nx_set_explosion_display``.
"""

from __future__ import annotations

import contextlib

from nx_mcp.core.catalogue import deleter, lister
from nx_mcp.core.errors import fail, native
from nx_mcp.domains.assembly import common, constraints, exploded, patterns, structure
from nx_mcp.domains.assembly.reference_sets import members
from nx_mcp.kit import model


def _whole_part(owner):
    if owner is not None:
        fail("NX_UNSUPPORTED_OPTION", "Assembly objects are listed for the whole part",
             field="/owner")  # fmt: skip


@lister("reference_set")
def list_reference_sets(tx, owner, owner_kind):
    _whole_part(owner)
    out = []
    for value in tx.part.GetAllReferenceSets():
        details = {}
        with contextlib.suppress(Exception):
            details["member_count"] = len(members(tx, value))
        out.append((value, "reference_set", {"type": "custom", "details": details}))
    return out


@lister("explosion")
def list_explosions(tx, owner, owner_kind):
    _whole_part(owner)
    part = tx.part
    out = []
    for ex in list(getattr(part.ComponentAssembly, "Explosions", None) or []):
        details = {"component_count": len(exploded.tree(ex))}
        with contextlib.suppress(Exception):
            details["views"] = [v["id"] for v in exploded.views(tx, part, ex)]
        out.append((ex, "explosion", {"details": details}))
    return out


@lister("component_pattern")
def list_component_patterns(tx, owner, owner_kind):
    _whole_part(owner)
    collection = getattr(tx.part.ComponentAssembly, "ComponentPatterns", None)
    if collection is None or structure.root(tx.part) is None:
        return []
    out = []
    for pattern in collection.GetAllComponentPatterns():
        info = patterns.describe(tx, pattern)
        details = {k: v for k, v in info.items() if k not in ("pattern", "layout")}
        details = {k: v for k, v in details.items() if not k.startswith("_")}
        out.append((pattern, "component_pattern", {"type": info["layout"], "details": details}))
    return out


@lister("assembly_constraint")
def list_assembly_constraints(tx, owner, owner_kind):
    _whole_part(owner)
    out = []
    for c in constraints.all_constraints(tx.part):
        info = constraints.record(tx, c)
        details = {k: info[k] for k in ("alignment", "status", "suppressed") if k in info}
        if "value" in info:
            details["value"] = info["value"]
        details["components"] = [
            r["component"]["id"] for r in info["references"] if "component" in r
        ]
        out.append((c, "assembly_constraint", {"type": info["type"], "details": details}))
    return out


@deleter("component")
def delete_components(tx, objects):
    part = tx.part
    for c in objects:
        if not structure.is_direct_child(part, c):
            fail("NX_WRONG_OWNER", f"{c.Name} is not a direct component of the work part",
                 hint=common.OWNER_HINT)  # fmt: skip
    with native(default="NX_BUILD_FAILED"):
        tx.session.UpdateManager.AddToDeleteList(list(objects))
    model.update(tx)


@deleter("explosion")
def delete_explosions(tx, objects):
    part = common.explosion_context(tx)
    for ex in objects:
        shown = exploded.views(tx, part, ex)
        if shown:
            fail("NX_IN_USE", f"Explosion {ex.Name} is shown in views",
                 hint="Show the assembled state there with nx_set_explosion_display",
                 details={"views": shown})  # fmt: skip
    with native(default="NX_BUILD_FAILED"):
        for ex in objects:
            ex.Delete()


@deleter("traceline")
def delete_traces(tx, objects):
    with native(default="NX_BUILD_FAILED"):
        tx.session.UpdateManager.AddToDeleteList(list(objects))
    model.update(tx)
