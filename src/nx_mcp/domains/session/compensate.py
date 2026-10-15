"""Undo without NX's undo (the compensating undo, v2) for ``nx_undo_operation`` and
``nx_restore_checkpoint`` (domains/session/history.py).

NX keeps one linear undo list for the whole session: undoing an operation of part A with
NX's undo also reverts everything any part did after it. When other parts' work came
after A's operations (or another part's save, load or close cut NX's history), the undo
of A instead compensates them: it deletes what they created and sets back the values
they changed, which leaves every other part untouched. Only a narrow case qualifies
(else the undo is refused and says why):

- every undone operation qualifies by its receipt (``core/undoable.py``: the call, or
  each child of a batch or an accepted preview, either created objects, features with
  what they own and what they consumed, sketches, curves and points, or set values its
  tool's ``@restorer`` hook sets back from the before-values it kept,
  ``tx.note_restore``), and those values still read what the operation set (a later
  change of the same value is refused: ``NX_DEPENDENCY_ORDER``); curves, points,
  constraints and dimensions added to a sketch the operations did not create are
  in-sketch edits: refused, deleting them could make NX ask about the sketch's dimensions;
- every created object is still in A, or in another part the operation named as changed
  (``parts`` of its history entry: a WAVE link into a component's part); a ref that went
  stale counts only when a later operation's receipt says it deleted the object
  (``missing``); a created sketch is not open (active or parked: finish it first);
- no feature outside the set depends on one of them (``GetChildren``, recursively).

Values are set back first (newest operation first), each verified by its restorer. The
deletion then runs through the deleters (``@deleter("feature")``, ``"sketch"``,
``"curve"``; points go the curves' way, through NX's update manager) under the undo's
own mark and is verified on snapshots: every created feature, sketch, curve and point is
gone and nothing else of A vanished except what they own; otherwise the call fails and
the kernel rolls everything back. An expression NX keeps (still in use) is reported. NX
may renumber the journal identifiers of later features: anchors are re-checked
(``References.recheck_anchors``). The undo's own history entry is marked
``compensation`` (``core/marks.py``): it is never undone itself, so no later undo brings
the deleted objects or the changed values back.

:func:`qualifies` (``core/undoable.py``) is the receipt-only part of the rules (no NX
object is read): the undo depth and the checkpoint listing count the operations it admits.
Deleting a created feature brings back the faces, edges and bodies it consumed (under new
refs). Deletes and edits without before-values never qualify.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from nx_mcp.core import parking
from nx_mcp.core.catalogue import hooks
from nx_mcp.core.errors import NXError, fail, with_article
from nx_mcp.core.undoable import (
    CREATED_KINDS,
    DELETED_KINDS,
    DERIVED_KINDS,
    UNDO_TOOLS,
    WITH_FEATURE,
    WITH_SKETCH,
    Refusal,
    Unit,
    creates,
    qualifies,
    restorer_of,
    settable,
    units,
)
from nx_mcp.core.undoable import record_of as _record
from nx_mcp.kit import readback

__all__ = [
    "CREATED_KINDS",
    "DELETED_KINDS",
    "DERIVED_KINDS",
    "UNDO_TOOLS",
    "WITH_FEATURE",
    "WITH_SKETCH",
    "Plan",
    "Refusal",
    "plan",
    "qualifies",
    "run",
]
LISTED = 10


@dataclass
class Plan:
    features: list = field(default_factory=list)  # live features to delete
    sketches: list = field(default_factory=list)  # live sketches to delete
    loose: list = field(default_factory=list)  # (kind, object) curves and points outside sketches
    allowed: set = field(default_factory=set)  # tags that may vanish with them
    expressions: list = field(default_factory=list)  # (object, ref) of created expressions
    missing: list = field(default_factory=list)  # refs a later operation already deleted
    rows: dict = field(default_factory=dict)  # (kind, tag) -> ref (read-back messages)
    kinds: dict = field(default_factory=dict)  # kind -> count of created objects found
    # (what, restorer, before-values) of the units whose values are set back, newest first
    restores: list = field(default_factory=list)
    # part key -> part: the other parts the operations created objects in (a WAVE link
    # into a component's part); the undo reads them back too
    others: dict = field(default_factory=dict)


def _deleted_later(services, later: list) -> set:
    """Refs the receipts of ``later`` history entries deleted or made stale."""
    out: set = set()
    for entry in later:
        if entry.get("operation_id") is None:
            continue
        changes = (_record(services, entry).get("envelope") or {}).get("changes") or {}
        out.update(str(r.get("id")) for r in changes.get("deleted") or [] if r.get("id"))
        out.update(str(r) for r in changes.get("stale_refs") or [])
        for child in ((_record(services, entry).get("envelope") or {}).get("data") or {}).get(
            "results"
        ) or []:
            if isinstance(child, dict):
                inner = child.get("changes") or {}
                out.update(str(r.get("id")) for r in inner.get("deleted") or [] if r.get("id"))
                out.update(str(r) for r in inner.get("stale_refs") or [])
    return out


def _tag(obj) -> int | None:
    try:
        return int(obj.Tag)
    except Exception:
        return None


def _restorable(tx, unit: Unit, restore, data) -> None:
    """Refuse a unit whose values cannot be set back now (it deleted objects, its values
    changed since, or they lie in another part)."""
    settable(unit)
    key = tx.services.part_key(tx.part)
    if unit.part is not None and unit.part != key:
        raise Refusal(
            "NX_CROSS_PART",
            f"{unit.what} set values in another part: they are not set back from here",
            {"operation_id": unit.operation_id, "step": unit.index},
        )
    try:
        restore(tx, data, apply=False)
    except NXError as error:
        raise Refusal(
            error.code, f"{unit.what}: {error.message}", {"operation_id": unit.operation_id}
        ) from None


def _children(features: list) -> list:
    """Features outside ``features`` that depend on one of them (``GetChildren``)."""
    inside = {_tag(f) for f in features}
    outside: dict = {}
    for feature in features:
        try:
            children = list(feature.GetChildren() or [])
        except Exception as error:
            raise Refusal(
                "NX_UNSUPPORTED_OPTION", f"NX could not list the features that use it: {error}"
            ) from error
        for child in children:
            tag = _tag(child)
            if tag not in inside:
                outside.setdefault(tag, child)
    return list(outside.values())


def _geometry(sketch) -> set:
    """Tags of a sketch's curves and points (``GetAllGeometry``); empty when NX cannot
    list them."""
    with contextlib.suppress(Exception):
        return {_tag(g) for g in sketch.GetAllGeometry() or []}
    return set()


def _sketch_of(part, tag, sketches: dict) -> Any:
    """The sketch of ``part`` whose geometry holds ``tag``, else None (``sketches`` caches
    each sketch's geometry tags)."""
    for sketch in list(getattr(part, "Sketches", None) or []):
        key = _tag(sketch)
        if key not in sketches:
            sketches[key] = _geometry(sketch)
        if tag in sketches[key]:
            return sketch
    return None


def _found(tx, plan: Plan, entry: dict, row: dict, key, later: list, gone: list) -> Any:
    """The live object of a created row (None: a later operation deleted it, ``missing``);
    refuses a stale ref nobody deleted and an object of a part the operation did not
    change (the call's part, or another part it named: ``parts`` of its history entry)."""
    services = tx.services
    ref = str(row.get("id") or "")
    try:
        obj = tx.resolve(ref)
    except NXError as error:
        if error.code != "NX_STALE_REFERENCE":
            raise Refusal(error.code, f"{ref}: {error.message}", {"ref": ref}) from None
        if not gone:
            gone.append(_deleted_later(services, later))
        if ref in gone[0]:
            plan.missing.append(ref)
            return None
        raise Refusal(
            "NX_STALE_REFERENCE",
            f"{ref}, which {entry['operation_id']} created, cannot be found any more "
            "(its ref is stale and no later operation deleted it)",
            {"ref": ref, "operation_id": entry["operation_id"]},
        ) from None
    owner = services.owning_part(obj)
    owner_key = services.part_key(owner) if owner is not None else None
    if owner_key is not None and owner_key != key and owner_key in (entry.get("parts") or []):
        plan.others.setdefault(owner_key, owner)
        return obj
    if owner_key != key:
        raise Refusal(
            "NX_CROSS_PART",
            f"{ref}, which {entry['operation_id']} created, is not in this part",
            {"ref": ref},
        )
    return obj


def plan(tx, candidates: list, later: list) -> Plan:
    """What undoing ``candidates`` (history entries of the call's part, oldest first) by
    compensation sets back and deletes; raises :class:`Refusal` when it does not qualify.
    ``later``: the history entries after the oldest candidate (their receipts prove a
    missing object gone)."""
    services = tx.services
    part = tx.part
    key = services.part_key(part)
    result = Plan()
    gone: list = []  # [refs later operations deleted], read once when needed
    found: list = []  # (entry, kind, ref, object) of every live created object
    for entry in candidates:
        for unit in units(services, entry):
            restoring = restorer_of(unit)
            if restoring is not None:  # values set back (a created expression is the restorer's)
                _restorable(tx, unit, *restoring)
                result.restores.insert(0, (unit.what, *restoring))
                continue
            for row in creates(unit):
                obj = _found(tx, result, entry, row, key, later, gone)
                if obj is not None:
                    found.append((entry, str(row.get("kind")), str(row.get("id") or ""), obj))
    _sort(tx, result, found, part)
    outside = _children(result.features)
    if outside:
        rows = []
        for child in outside[:LISTED]:
            with contextlib.suppress(Exception):
                rows.append(tx.ref(child))
        names = ", ".join(r.get("id") or r.get("name") or "?" for r in rows) or "features"
        raise Refusal(
            "NX_DEPENDENCY_ORDER",
            f"{names} {'uses' if len(outside) == 1 else 'use'} what it created",
            {"dependents": rows, "dependent_count": len(outside)},
        )
    return result


def _sort(tx, result: Plan, found: list, part) -> None:
    """Sort the live created objects into what the undo deletes and what goes with it;
    refuses an open sketch and an in-sketch edit of a sketch the operations did not
    create."""
    services = tx.services
    created_sketches = [obj for _e, kind, _r, obj in found if kind == "sketch"]
    open_sketches: set = set()
    with contextlib.suppress(Exception):
        open_sketches.add(_tag(services.session.ActiveSketch))
    with contextlib.suppress(Exception):
        open_sketches.add(_tag(parking.parked(services, part)))
    open_sketches.discard(None)
    own: set = set()  # geometry tags of the created sketches
    for sketch in created_sketches:
        own |= _geometry(sketch)
    geometry: dict = {}
    for entry, kind, ref, obj in found:
        tag = _tag(obj)
        result.allowed.add(tag)
        result.kinds[kind] = result.kinds.get(kind, 0) + 1
        if kind == "feature":
            result.features.append(obj)
            result.rows[("feature", tag)] = ref
        elif kind == "sketch":
            if tag in open_sketches:
                raise Refusal(
                    "NX_SKETCH_ACTIVE",
                    f"{ref}, which {entry['operation_id']} created, is still open: finish it "
                    "with nx_finish_sketch first",
                    {"ref": ref},
                )
            result.sketches.append(obj)
            result.rows[("sketch", tag)] = ref
            feature = getattr(obj, "Feature", None)
            if feature is not None and _tag(feature) not in {_tag(f) for f in result.features}:
                result.features.append(feature)  # NX deletes a sketch with its feature
                result.allowed.add(_tag(feature))
        elif kind in ("curve", "point"):
            if tag in own:
                continue  # goes with its created sketch
            holder = _sketch_of(services.owning_part(obj) or part, tag, geometry)
            if holder is not None:
                raise Refusal(
                    "NX_UNSUPPORTED_OPTION",
                    f"{ref}, which {entry['operation_id']} created, lies in a sketch it did not "
                    "create: an in-sketch edit is not undone by deletion",
                    {"ref": ref, "operation_id": entry["operation_id"]},
                )
            result.loose.append((kind, obj))
            result.rows[(kind, tag)] = ref
        elif kind == "expression":
            result.expressions.append((obj, ref))
        elif kind in WITH_SKETCH and not created_sketches:
            raise Refusal(
                "NX_UNSUPPORTED_OPTION",
                f"{ref}, which {entry['operation_id']} created, is {with_article(kind.replace('_', ' '))} "
                "in a sketch it did not create: an in-sketch edit is not undone by deletion",
                {"ref": ref, "operation_id": entry["operation_id"]},
            )
    result.allowed |= own
    for feature in result.features:
        with contextlib.suppress(Exception):
            result.allowed.update(_tag(b) for b in feature.GetBodies() or [])
        with contextlib.suppress(Exception):
            result.allowed.update(_tag(e) for e in feature.GetExpressions() or [])


def _present(part, tag) -> bool:
    with contextlib.suppress(Exception):
        return any(_tag(e) == tag for e in getattr(part, "Expressions", None) or [])
    return False


def _expression_owners(services, part, plan: Plan) -> list:
    """(owning part, tag, ref) of each created expression, read before the deletion: NX
    raises on any property of an object it deleted (a feature takes its expressions)."""
    out = []
    for obj, ref in plan.expressions:
        try:
            out.append((services.owning_part(obj) or part, _tag(obj), ref))
        except Exception:
            continue  # already gone: not kept
    return out


def _delete(tx, plan: Plan) -> None:
    """Features first (their sketches, bodies and datums go with them), then the sketches
    and the loose curves and points NX has not removed yet."""
    deleters = hooks("deleter")
    if plan.features:
        deleters["feature"](tx, list(plan.features))
    present = {(k, int(t)) for k, t in _snapshot(tx, plan)}
    sketches = [s for s in plan.sketches if ("sketch", _tag(s)) in present]
    if sketches:
        deleters["sketch"](tx, sketches)
    loose = [obj for kind, obj in plan.loose if (kind, _tag(obj)) in present]
    if loose:
        # curves and points alike: NX's Delete through the update manager
        deleters["curve"](tx, loose)


def _snapshot(tx, plan: Plan) -> dict:
    """The snapshot of the call's part and of the other parts the plan deletes in (NX
    tags are unique in the session, so the keys never collide)."""
    services = tx.services
    out = dict(services.snapshot(tx.part))
    for other in plan.others.values():
        out.update(services.snapshot(other))
    return out


def run(tx, plan: Plan) -> dict[str, Any]:
    """Set the planned values back, then delete the planned objects, and verify; raises
    (the kernel rolls back) when a value does not read back or NX did not delete them or
    deleted more."""
    services = tx.services
    part = tx.part
    key = services.part_key(part)
    restored = []
    note = tx.history_note  # a restorer may run a tool's own code, which notes values
    for _operation, restore, data in plan.restores:  # newest first
        try:
            restored.append(restore(tx, data, apply=True))
        except NXError as error:
            # tx.checks marks a refusal unchanged; earlier restores changed the model
            setattr(error, "unchanged", False)  # noqa: B010  # so the kernel rolls back
            raise
    tx.history_note = note
    for other in plan.others.values():  # the kernel reads them back and lists their changes
        tx.touch_part(other)
    expressions = _expression_owners(services, part, plan)
    before = _snapshot(tx, plan)
    if plan.features or plan.sketches or plan.loose:
        _delete(tx, plan)
    after = _snapshot(tx, plan)
    vanished = [k for k in before if k not in after]
    outside = [k for k in vanished if int(k[1]) not in plan.allowed]
    if outside:
        rows = [services.describe(before[k], live=False) for k in outside[:LISTED]]
        fail(
            "NX_DEPENDENCY_ORDER",
            f"Deleting what the operation created also deleted {len(outside)} other "
            "object(s); nothing was deleted",
            hint="Delete the objects you want gone with nx_delete_objects",
            details={"dependents": rows, "dependent_count": len(outside)},
        )
    alive = [ref for (kind, tag), ref in plan.rows.items() if (kind, tag) in after]
    readback.expect(not alive, "deleted", f"NX did not delete {', '.join(alive)}", objects=alive)
    kept = [ref for owner, tag, ref in expressions if _present(owner, tag)]
    out: dict[str, Any] = {"deleted": len(vanished), "kinds": dict(plan.kinds)}
    if restored:
        out["restored"] = restored
    if plan.missing:
        out["missing"] = list(plan.missing)
    if kept:
        out["kept_expressions"] = kept
    recheck = getattr(services.references, "recheck_anchors", None)
    if recheck is not None:
        with contextlib.suppress(Exception):
            moved: list = []
            for each in (key, *plan.others):
                moved.extend(recheck(each, {k[1] for k in vanished}) or [])
            if moved:
                out["renumbered"] = moved[:LISTED]
    return out
