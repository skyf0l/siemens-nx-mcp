"""Undo without NX's undo (the compensating undo, v2) for ``nx_undo_operation`` and
``nx_restore_checkpoint`` (domains/session/history.py).

NX keeps one linear undo list for the whole session: undoing an operation of part A with
NX's undo also reverts everything any part did after it. When other parts' work came
after A's operations (or another part's save, load or close cut NX's history), the undo
of A instead compensates them: it deletes what they created and sets back the values
they changed, which leaves every other part untouched. Only a narrow case qualifies
(else the undo is refused and says why):

- every undone operation is a committed call that either
  - created objects and changed nothing else (nothing deleted, changed or consumed;
    drawing views, sheets and their annotations that NX refreshed after the model
    changed count as derived, not as changed): features (with their bodies, expressions
    and datums), sketches (with their curves, points, constraints and dimensions) and
    curves and points outside any sketch (``DELETED_KINDS``; curves, points, constraints
    and dimensions added to a sketch the operations did not create are in-sketch edits:
    refused, deleting them could make NX ask about the sketch's dimensions), or
  - set values its tool can set back (a ``@restorer`` hook: ``nx_set_attributes``,
    ``nx_set_expression``, ``nx_set_name``, ``nx_set_component_pose``,
    ``nx_set_sheet_metal_defaults``) and kept the before-values in its history entry
    (``restore``), and those values still read what the operation set (a later change of
    the same value is refused: ``NX_DEPENDENCY_ORDER``);
- every created object is still in A (a ref that went stale counts only when a later
  operation's receipt says it deleted the object: ``missing``); a created sketch is not
  open (active or parked: finish it first);
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

:func:`qualifies` is the receipt-only part of the rules (no NX object is read): the undo
depth counts the operations it admits.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from nx_mcp.core import parking
from nx_mcp.core.catalogue import hooks
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.types import KIND_GROUPS, SHEET_KINDS
from nx_mcp.kit import readback

# Kinds an undo by deletion deletes itself (in this order: a feature takes its bodies and
# the sketch it is, a sketch its geometry).
DELETED_KINDS = ("feature", "sketch", "curve", "point")
# Kinds that go with a created feature (its bodies, expressions and datum objects).
WITH_FEATURE = frozenset({"body", "expression", *KIND_GROUPS["datum"]})
# Kinds that go with a created sketch (its constraints and dimensions).
WITH_SKETCH = frozenset({"sketch_constraint", "dimension"})
CREATED_KINDS = frozenset({*DELETED_KINDS, *WITH_FEATURE, *WITH_SKETCH})
# Objects NX refreshes when the model changes (a feature's drawing views update): a
# change to them is derived from the created features, not work of its own.
DERIVED_KINDS = SHEET_KINDS
UNDO_TOOLS = frozenset({"nx_undo_operation", "nx_restore_checkpoint", "nx_redo_operation"})
LISTED = 10


class Refusal(Exception):
    """Why the undone operations cannot be undone by deletion."""

    def __init__(self, code: str, reason: str, details: dict | None = None) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.details = details or {}


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
    # (operation id, restorer, before-values) of the operations whose values are set
    # back, newest first
    restores: list = field(default_factory=list)


def _record(services, entry: dict) -> dict:
    try:
        return services.store.get(entry["operation_id"]) or {}
    except Exception:
        return {}


def _changes(record: dict) -> dict:
    return (record.get("envelope") or {}).get("changes") or {}


def _deleted_later(services, later: list) -> set:
    """Refs the receipts of ``later`` history entries deleted or made stale."""
    out: set = set()
    for entry in later:
        if entry.get("operation_id") is None:
            continue
        changes = _changes(_record(services, entry))
        out.update(str(r.get("id")) for r in changes.get("deleted") or [] if r.get("id"))
        out.update(str(r) for r in changes.get("stale_refs") or [])
    return out


def _tag(obj) -> int | None:
    try:
        return int(obj.Tag)
    except Exception:
        return None


def _committed(entry: dict, record: dict) -> None:
    operation, tool = entry["operation_id"], entry.get("method") or ""
    if tool in UNDO_TOOLS or record.get("state") != "committed" or record.get("reverted_by"):
        raise Refusal(
            "NX_UNSUPPORTED_OPTION",
            f"{operation} ({tool}) is not a committed operation that created objects or set values",
            {"operation_id": operation},
        )


def _own_changes(changes: dict, name: str) -> list:
    """``changes[name]`` without the derived objects NX refreshed (drawing views...)."""
    rows = changes.get(name) or []
    if name != "modified":
        return list(rows)
    return [r for r in rows if not (isinstance(r, dict) and r.get("kind") in DERIVED_KINDS)]


def restorer_of(entry: dict):
    """``(restorer, before-values)`` when ``entry``'s values can be set back, else None."""
    data = entry.get("restore")
    restore = hooks("restorer").get(str(entry.get("method") or ""))
    return (restore, data) if data is not None and restore is not None else None


def _restorable(tx, entry: dict, record: dict, restore, data) -> None:
    """Refuse an operation whose values cannot be set back now (it deleted objects, or
    its values changed since)."""
    _settable(entry, record)
    operation, tool = entry["operation_id"], entry.get("method") or ""
    try:
        restore(tx, data, apply=False)
    except NXError as error:
        raise Refusal(
            error.code, f"{operation} ({tool}): {error.message}", {"operation_id": operation}
        ) from None


def _settable(entry: dict, record: dict) -> None:
    """The receipt-only check of an operation whose values a restorer sets back."""
    _committed(entry, record)
    operation, tool = entry["operation_id"], entry.get("method") or ""
    if _changes(record).get("deleted"):
        raise Refusal(
            "NX_UNSUPPORTED_OPTION",
            f"{operation} ({tool}) deleted objects: setting its values back does not bring "
            "them back",
            {"operation_id": operation, "changes": ["deleted"]},
        )


def _creates(entry: dict, record: dict) -> list:
    """The created rows of a create-only operation, else a Refusal (receipt only)."""
    operation, tool = entry["operation_id"], entry.get("method") or ""
    _committed(entry, record)
    changes = _changes(record)
    created = list(changes.get("created") or [])
    other = [k for k in ("deleted", "modified", "stale_refs") if _own_changes(changes, k)]
    if other or not created:
        what = ", ".join(other) if other else "created nothing"
        raise Refusal(
            "NX_UNSUPPORTED_OPTION",
            f"{operation} ({tool}) did more than create objects ({what})",
            {"operation_id": operation, "changes": other or ["created nothing"]},
        )
    kinds = sorted({str(r.get("kind")) for r in created} - CREATED_KINDS)
    if kinds or not any(r.get("kind") in DELETED_KINDS for r in created):
        raise Refusal(
            "NX_UNSUPPORTED_OPTION",
            f"{operation} ({tool}) created "
            + (", ".join(kinds) if kinds else "no feature, sketch, curve or point")
            + ": only features, sketches, curves and points (with what they own) are "
            "undone by deletion",
            {"operation_id": operation, "kinds": kinds or ["no feature"]},
        )
    return created


def qualifies(services, entry: dict) -> bool:
    """Whether ``entry``'s receipt admits an undo by deletion (create-only within the
    kinds above, or values a restorer sets back). The NX objects are not read: the plan
    may still refuse (a dependent feature, a value changed since)."""
    record = _record(services, entry)
    try:
        if restorer_of(entry) is not None:
            _settable(entry, record)
        else:
            _creates(entry, record)
    except Refusal:
        return False
    return True


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
    refuses a stale ref nobody deleted and an object of another part."""
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
    if owner is None or services.part_key(owner) != key:
        raise Refusal(
            "NX_CROSS_PART",
            f"{ref}, which {entry['operation_id']} created, is not in this part",
            {"ref": ref},
        )
    return obj


def plan(tx, candidates: list, later: list) -> Plan:
    """What undoing ``candidates`` (history entries of the call's part, oldest first) by
    deletion deletes; raises :class:`Refusal` when it does not qualify. ``later``: the
    history entries after the oldest candidate (their receipts prove a missing object
    gone)."""
    services = tx.services
    part = tx.part
    key = services.part_key(part)
    result = Plan()
    gone: list = []  # [refs later operations deleted], read once when needed
    found: list = []  # (entry, kind, ref, object) of every live created object
    for entry in candidates:
        record = _record(services, entry)
        restoring = restorer_of(entry)
        if restoring is not None:  # values set back (a created expression is the restorer's)
            _restorable(tx, entry, record, *restoring)
            result.restores.insert(0, (str(entry["operation_id"]), *restoring))
            continue
        for row in _creates(entry, record):
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
            holder = _sketch_of(part, tag, geometry)
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
                f"{ref}, which {entry['operation_id']} created, is a {kind.replace('_', ' ')} "
                "in a sketch it did not create: an in-sketch edit is not undone by deletion",
                {"ref": ref, "operation_id": entry["operation_id"]},
            )
    result.allowed |= own
    for feature in result.features:
        with contextlib.suppress(Exception):
            result.allowed.update(_tag(b) for b in feature.GetBodies() or [])
        with contextlib.suppress(Exception):
            result.allowed.update(_tag(e) for e in feature.GetExpressions() or [])


def _present(part, obj) -> bool:
    tag = _tag(obj)
    with contextlib.suppress(Exception):
        return any(_tag(e) == tag for e in getattr(part, "Expressions", None) or [])
    return False


def _delete(tx, plan: Plan) -> None:
    """Features first (their sketches, bodies and datums go with them), then the sketches
    and the loose curves and points NX has not removed yet."""
    deleters = hooks("deleter")
    if plan.features:
        deleters["feature"](tx, list(plan.features))
    present = {(k, int(t)) for k, t in tx.services.snapshot(tx.part)}
    sketches = [s for s in plan.sketches if ("sketch", _tag(s)) in present]
    if sketches:
        deleters["sketch"](tx, sketches)
    loose = [obj for kind, obj in plan.loose if (kind, _tag(obj)) in present]
    if loose:
        # curves and points alike: NX's Delete through the update manager
        deleters["curve"](tx, loose)


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
    before = services.snapshot(part)
    if plan.features or plan.sketches or plan.loose:
        _delete(tx, plan)
    after = services.snapshot(part)
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
    kept = [ref for obj, ref in plan.expressions if _present(part, obj)]
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
            moved = recheck(key, {k[1] for k in vanished})
            if moved:
                out["renumbered"] = moved[:LISTED]
    return out
