"""Undo without NX's undo (the compensating undo, v2) for ``nx_undo_operation`` and
``nx_restore_checkpoint`` (domains/session/history.py).

NX keeps one linear undo list for the whole session: undoing an operation of part A with
NX's undo also reverts everything any part did after it. When other parts' work came
after A's operations (or a save, load or close cut NX's history), the undo of A instead
compensates them: it deletes what they created and sets back the values they changed,
which leaves every other part untouched. Only a narrow case qualifies (else the undo is
refused and says why):

- every undone operation is a committed call that either
  - created features (with their bodies and expressions) and nothing else: nothing
    deleted, changed or consumed, no sketch, curve, point or other kind (drawing views,
    sheets and their annotations that NX refreshed after the model changed count as
    derived, not as changed), or
  - set values its tool can set back (a ``@restorer`` hook: ``nx_set_attributes``,
    ``nx_set_expression``, ``nx_set_name``) and kept the before-values in its history
    entry (``restore``), and those values still read what the operation set (a later
    change of the same value is refused: ``NX_DEPENDENCY_ORDER``);
- every created object is still in A (a ref that went stale counts only when a later
  operation's receipt says it deleted the object: ``missing``);
- no feature outside the set depends on one of them (``GetChildren``, recursively).

Values are set back first (newest operation first), each verified by its restorer. The
deletion then runs through the feature deleter (``@deleter("feature")``) under the
undo's own mark and is verified on snapshots: every created feature is gone and nothing
else of A vanished except their bodies and expressions; otherwise the call fails and the
kernel rolls everything back. An expression NX keeps (still in use) is reported. NX may
renumber the journal identifiers of later features: anchors are re-checked
(``References.recheck_anchors``). The undo's own history entry is marked
``compensation`` (``core/marks.py``): it is never undone itself, so no later undo brings
the deleted objects or the changed values back.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from nx_mcp.core.catalogue import hooks
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.types import SHEET_KINDS
from nx_mcp.kit import readback

CREATED_KINDS = frozenset({"feature", "body", "expression"})
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
    allowed: set = field(default_factory=set)  # tags that may vanish with them
    expressions: list = field(default_factory=list)  # (object, ref) of created expressions
    missing: list = field(default_factory=list)  # refs a later operation already deleted
    rows: dict = field(default_factory=dict)  # feature tag -> ref (read-back messages)
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
    _committed(entry, record)
    operation, tool = entry["operation_id"], entry.get("method") or ""
    if _changes(record).get("deleted"):
        raise Refusal(
            "NX_UNSUPPORTED_OPTION",
            f"{operation} ({tool}) deleted objects: setting its values back does not bring "
            "them back",
            {"operation_id": operation, "changes": ["deleted"]},
        )
    try:
        restore(tx, data, apply=False)
    except NXError as error:
        raise Refusal(
            error.code, f"{operation} ({tool}): {error.message}", {"operation_id": operation}
        ) from None


def _creates(entry: dict, record: dict) -> list:
    """The created rows of a create-only operation, else a Refusal."""
    operation, tool = entry["operation_id"], entry.get("method") or ""
    _committed(entry, record)
    changes = _changes(record)
    created = list(changes.get("created") or [])
    other = [k for k in ("deleted", "modified", "stale_refs") if _own_changes(changes, k)]
    if other or not created:
        what = ", ".join(other) if other else "created nothing"
        raise Refusal(
            "NX_UNSUPPORTED_OPTION",
            f"{operation} ({tool}) did more than create features ({what})",
            {"operation_id": operation, "changes": other or ["created nothing"]},
        )
    kinds = sorted({str(r.get("kind")) for r in created} - CREATED_KINDS)
    if kinds or not any(r.get("kind") == "feature" for r in created):
        raise Refusal(
            "NX_UNSUPPORTED_OPTION",
            f"{operation} ({tool}) created "
            + (", ".join(kinds) if kinds else "no feature")
            + ": only features (with their bodies and expressions) are undone by deletion",
            {"operation_id": operation, "kinds": kinds or ["no feature"]},
        )
    return created


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


def plan(tx, candidates: list, later: list) -> Plan:
    """What undoing ``candidates`` (history entries of the call's part, oldest first) by
    deletion deletes; raises :class:`Refusal` when it does not qualify. ``later``: the
    history entries after the oldest candidate (their receipts prove a missing object
    gone)."""
    services = tx.services
    key = services.part_key(tx.part)
    result = Plan()
    gone_later: set | None = None
    for entry in candidates:
        record = _record(services, entry)
        found = restorer_of(entry)
        if found is not None:  # values set back (a created expression is the restorer's)
            _restorable(tx, entry, record, *found)
            result.restores.insert(0, (str(entry["operation_id"]), *found))
            continue
        for row in _creates(entry, record):
            ref, kind = str(row.get("id") or ""), row.get("kind")
            try:
                obj = tx.resolve(ref)
            except NXError as error:
                if error.code != "NX_STALE_REFERENCE":
                    raise Refusal(error.code, f"{ref}: {error.message}", {"ref": ref}) from None
                if gone_later is None:
                    gone_later = _deleted_later(services, later)
                if ref in gone_later:
                    result.missing.append(ref)
                    continue
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
            tag = _tag(obj)
            result.allowed.add(tag)
            result.kinds[kind] = result.kinds.get(kind, 0) + 1
            if kind == "feature":
                result.features.append(obj)
                result.rows[tag] = ref
            elif kind == "expression":
                result.expressions.append((obj, ref))
    for feature in result.features:
        with contextlib.suppress(Exception):
            result.allowed.update(_tag(b) for b in feature.GetBodies() or [])
        with contextlib.suppress(Exception):
            result.allowed.update(_tag(e) for e in feature.GetExpressions() or [])
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


def _present(part, obj) -> bool:
    tag = _tag(obj)
    with contextlib.suppress(Exception):
        return any(_tag(e) == tag for e in getattr(part, "Expressions", None) or [])
    return False


def run(tx, plan: Plan) -> dict[str, Any]:
    """Set the planned values back, then delete the planned features, and verify; raises
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
    if plan.features:
        hooks("deleter")["feature"](tx, list(plan.features))
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
    alive = [ref for tag, ref in plan.rows.items() if ("feature", tag) in after]
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
