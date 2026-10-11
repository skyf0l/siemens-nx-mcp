"""Assembly hooks: listers (``nx_list_objects``) and deleters (``nx_delete_objects``).

``@lister(kind)`` is ``fn(tx, owner, owner_kind) -> [(obj, kind, extra)]``;
``@deleter(kind)`` is ``fn(tx, objects) -> None`` and deletes inside the caller's mark.
An explosion shown in a view is refused (``NX_IN_USE``): show the assembled state
there first with ``nx_set_explosion_display``.

``@refresher("assembly_constraint")`` runs after every committed geometry-domain model
call (gate 13): it reads the solver status of the constraints in the loaded assemblies
that position the work part (and of the work part's own constraints) and warns once per
status change about those that no longer solve. NX 2406 marks a constraint CannotSolve
as soon as a prototype edit consumes its edge (live: a blend on a pin edge used by a
concentric, the assembly not even displayed), so no solve is needed to see it.
"""

from __future__ import annotations

import contextlib

from nx_mcp.core.catalogue import deleter, lister, refresher
from nx_mcp.core.errors import fail, native
from nx_mcp.domains.assembly import common, constraints, exploded, patterns, structure
from nx_mcp.domains.assembly.reference_sets import members
from nx_mcp.kit import model, positioning

# services attribute: {constraint tag: last unsolved status warned about}
_SEEN = "_nx_mcp_unsolved_constraints"


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


def _considered(tx, work) -> list:
    """``[(part, constraint)]``: the work part's constraints and those of every loaded
    assembly that position an occurrence of the work part."""
    out = []
    for part in list(tx.session.Parts):
        own = int(part.Tag) == int(work.Tag)
        for c in positioning.all_constraints(part):
            if own or positioning.uses(c, work):
                out.append((part, c))
    return out


@refresher("assembly_constraint")
def check_constraints(tx, objects):
    """Warn about constraints this call left unsolved (status changes only); no rows."""
    if objects is not None:
        return []
    try:
        work = tx.part
        if work is None:
            return []
        found = _considered(tx, work)
        if not found:
            return []
        seen = getattr(tx.services, _SEEN, None)
        if seen is None:
            seen = {}
            setattr(tx.services, _SEEN, seen)
        said = " ".join(w.get("message", "") for w in tx.warnings)
        news = []
        for part, c in found:
            tag = int(c.Tag)
            if c.Suppressed or positioning.solved(tx, c):
                seen.pop(tag, None)
                continue
            status = positioning.status_name(tx, c)
            if status == "Suppressed" or seen.get(tag) == status:
                continue
            seen[tag] = status
            ref = tx.ref(c, "assembly_constraint", part=part)["id"]
            if ref not in said:
                where = tx.services.part_path(part)
                news.append(f"{ref} ({positioning.kind_name(tx, c)}: {status}) in {where}")
        if news:
            tx.warn(
                "NX_UPDATE_WARNING",
                "Assembly constraints no longer solve: " + ", ".join(news[:10])
                + "; their geometry may be gone. nx_check_model(scope='assembly') lists them; "
                "re-create or suppress them (nx_edit_assembly_constraint)",
            )  # fmt: skip
    except Exception:  # a status check never fails the call it follows
        return []
    return []
