"""``nx_delete_objects``: one generic delete for every deletable kind (tool-surface 3.1, #8).

Each domain registers ``@deleter(kind)`` hooks for its own kinds; a deleter is
``fn(tx, objects) -> None`` and deletes inside the call's undo mark (features, sketches
and curves through ``UpdateManager.AddToDeleteList`` + ``DoUpdate``, explosions and
section views natively). Mixed kinds are allowed: the objects are grouped by kind and
each group goes to its deleter. An object NX already removed with an earlier group (a
feature that depended on a deleted sketch) is skipped. The handler reads back that every
requested object is gone and reports the other objects NX deleted with them
(``dependents``). One undo mark covers the call; any failure rolls everything back.

In a part that is not displayed (gate 4s), features, sketches, curves, components and
tracelines are deleted in the quiet work part; a call that names an object of a
``DISPLAYED_KINDS`` kind displays its part for the call (``display_bound`` ``kind:``).
"""

from __future__ import annotations

import contextlib
from typing import Annotated, NoReturn, TypedDict

from nx_mcp.core.catalogue import deleter, hooks, tool
from nx_mcp.core.errors import fail, native, with_article
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Ref
from nx_mcp.kit import expr as kexpr
from nx_mcp.kit import readback
from nx_mcp.kit.result import Row

MAX_DEPENDENTS = 100
QUIET = frozenset({"expression"})  # parameters NX deletes with their feature
# Kinds deleted in the displayed part (gate 4s switches the display for them, else the
# target is only the quiet work part): explosions (``explosion_context`` and the views
# that show them), section views (the model work view's sectioning) and drafting objects
# (drawing tools are display-bound; their views and sheets update on the displayed part).
DISPLAYED_KINDS = (
    "explosion",
    "section_view",
    "drawing_sheet",
    "drawing_view",
    "annotation",
    "dimension",
    "drawing_table",
)
KIND_HINTS = {
    "body": "Delete the feature that made the body (nx_list_objects kind feature)",
    "expression": "A feature's expression goes with its feature (delete the feature); "
    "change its value with nx_set_expression",
    "face": "Delete curves or constraints inside a sketch with nx_edit_sketch_curves, "
    "faces with nx_edit_faces, a body by deleting its feature",
}


class DeleteData(TypedDict):
    deleted: list[Row]  # the requested objects, now gone
    dependents: list[Row]  # other objects NX deleted with them (at most 100 rows)
    dependent_count: int  # all of them, expressions not counted


def _tags(snapshot: dict) -> set[int]:
    return {int(key[1]) for key in snapshot}


def _listed(tx, kind) -> set[int] | None:
    """Tags a ``@lister`` still reports for ``kind``, None without a lister."""
    lister = hooks("lister").get(kind)
    if lister is None:
        return None
    return {int(obj.Tag) for obj, _kind, _extra in lister(tx, None, None)}


def _kind(tx, ref, obj) -> str:
    """The kind the reference was registered with (NX classes such as DynamicSection or
    Explosion are not inferred from the object); inferred for references without one."""
    entry = getattr(tx.services.references, "entry", None)
    known = entry(ref) if entry is not None else None
    return known.kind if known is not None else tx.kind_of(obj)


def _refuse_kind(tx, i: int, ref: str, obj, kind: str, deletable: list[str]) -> NoReturn:
    """NX_WRONG_KIND with the way to delete that kind: a body goes with the feature that
    made it (details.features, oldest first), sketch curves, faces and expressions have
    their own tools."""
    details: dict = {"ref": ref, "kind": kind, "deletable": deletable}
    hint = KIND_HINTS.get(kind, KIND_HINTS["face"])
    if kind == "body":
        features: list = []
        with contextlib.suppress(Exception):
            made = sorted(obj.GetFeatures(), key=lambda f: getattr(f, "Timestamp", 0))
            features = [tx.ref(f, "feature")["id"] for f in made]
        if features:
            details["features"] = features  # oldest first: the first one made the body
            hint = (
                "Delete the feature that made the body, details.features[0]; NX deletes "
                "the body and its later features (details.features) with it"
            )
    fail(
        "NX_WRONG_KIND",
        f"{ref} is {with_article(kind)}; nx_delete_objects cannot delete that kind",
        field=f"/objects/{i}",
        hint=hint,
        details=details,
    )


def _expression_users(tx, exp) -> list[str]:
    """What uses an expression: the expressions whose formula names it and the feature
    that owns it as a parameter (names; empty: unused)."""
    users = [str(getattr(e, "Name", "") or "") for e in kexpr.dependents(exp)]
    tag = int(exp.Tag)
    for feature in getattr(tx.part, "Features", None) or []:
        query = getattr(feature, "GetExpressions", None)
        with contextlib.suppress(Exception):
            if query is not None and any(int(e.Tag) == tag for e in query() or []):
                users.append(str(getattr(feature, "JournalIdentifier", "") or feature.Name))
    return users


@deleter("expression")
def delete_expressions(tx, objects):
    """A user expression nothing uses (``Expressions.Delete``); a feature's parameter goes
    with its feature."""
    for exp in objects:
        users = _expression_users(tx, exp)
        if users:
            fail(
                "NX_DEPENDENCY_ORDER",
                f"Expression {exp.Name} is used by {', '.join(users[:5])}",
                hint="Delete or edit what uses it first; a feature's parameter goes with its "
                "feature",
                details={"used_by": users[:20]},
            )
        with native(default="NX_DEPENDENCY_ORDER"):
            tx.part.Expressions.Delete(exp)


def _one_target(groups: dict) -> None:
    """A sketch listed with its own sketch feature is deleted once, through the feature
    (NX deletes the sketch with it; deleting both fails in NX)."""
    features = {int(f.Tag) for f in groups.get("feature") or []}
    sketches = groups.get("sketch")
    if not features or not sketches:
        return
    kept = []
    for sketch in sketches:
        feature = None
        with contextlib.suppress(Exception):
            feature = sketch.Feature
        if feature is None or int(feature.Tag) not in features:
            kept.append(sketch)
    if kept:
        groups["sketch"] = kept
    else:
        del groups["sketch"]


@tool(
    target="assembly",
    display_bound=tuple(f"kind:{kind}" for kind in DISPLAYED_KINDS),
    effect="model",
    label="Delete objects",
    summary="Delete {objects}",
    keywords=(
        "delete",
        "remove",
        "delete feature",
        "remove component",
        "delete explosion",
        "delete annotation",
        "delete section",
        "delete drawing view",
    ),
    requires=("work_part",),
    cost="light",
    batchable=True,
    previewable=True,
    example={"objects": ["f:12"]},
)
def nx_delete_objects(
    tx: Tx,
    *,
    objects: Annotated[
        list[Ref], Items(1, 100), Doc("Ids such as f:12 or s:3 to delete; mixed kinds allowed")
    ],
) -> DeleteData:
    """Delete 1-100 objects (ids like f:12) of the work part in one step.
    Kinds: feature, sketch, curve, component, explosion, traceline, annotation, dimension,
    drawing_table, section_view, drawing_sheet, drawing_view, unused expression; a body goes
    with its feature. Objects NX deleted with them are dependents. nx_undo_operation reverts
    a delete only until another part changes; suppress a feature to keep it recoverable."""
    deleters = hooks("deleter")
    groups: dict[str, list] = {}
    rows: dict[int, Row] = {}
    with tx.checks():
        for i, ref in enumerate(objects):
            obj = tx.resolve(ref)
            kind = _kind(tx, ref, obj)
            if kind not in deleters:
                _refuse_kind(tx, i, ref, obj, kind, sorted(deleters))
            if kind != "component":  # a component's deleter checks it is a direct child
                tx.resolve(ref, work_part=True)
            if kind == "expression":
                users = _expression_users(tx, obj)
                if users:
                    fail(
                        "NX_DEPENDENCY_ORDER",
                        f"{ref} ({obj.Name}) is used by {', '.join(users[:5])}",
                        field=f"/objects/{i}",
                        hint="Delete or edit what uses it first; a feature's parameter goes "
                        "with its feature",
                        details={"ref": ref, "used_by": users[:20]},
                    )
            tag = int(obj.Tag)
            if tag not in rows:
                rows[tag] = tx.ref(obj, kind)
                groups.setdefault(kind, []).append(obj)
        _one_target(groups)
    services = tx.services
    part = tx.part
    before = services.snapshot(part)
    known = _tags(before)
    current = known
    for index, (kind, values) in enumerate(groups.items()):
        tx.checkpoint(done=index, total=len(groups))
        if index:  # skip objects an earlier group already took with it
            current = _tags(services.snapshot(part))
            values = [v for v in values if int(v.Tag) not in known or int(v.Tag) in current]
        if values:
            deleters[kind](tx, values)
    after = services.snapshot(part)
    left = _tags(after)
    for kind, values in groups.items():
        listed = _listed(tx, kind)
        alive = [
            rows[int(v.Tag)]["id"]
            for v in values
            if int(v.Tag) in left or (listed is not None and int(v.Tag) in listed)
        ]
        readback.expect(
            not alive, "deleted", f"NX did not delete {', '.join(alive)}", objects=alive
        )
    # the work part as the deleted rows name it (short ref), never its registry key
    part_id = next(iter(rows.values()))["part"]
    dependents: list[Row] = []
    for key, entry in before.items():
        if key not in after and key[0] not in QUIET and int(key[1]) not in rows:
            row = services.describe(entry, live=False)
            row["part"] = part_id
            dependents.append(row)
    if len(list(services.parts() or [])) > 1:
        tx.warn(
            "NX_NOTE",
            "nx_undo_operation reverts this delete only until another part changes (only "
            "NX's undo brings deleted objects back)",
        )
    return {
        "deleted": list(rows.values()),
        "dependents": dependents[:MAX_DEPENDENTS],
        "dependent_count": len(dependents),
    }
