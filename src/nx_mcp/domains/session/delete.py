"""``nx_delete_objects``: one generic delete for every deletable kind (tool-surface 3.1, #8).

Each domain registers ``@deleter(kind)`` hooks for its own kinds; a deleter is
``fn(tx, objects) -> None`` and deletes inside the call's undo mark (features, sketches
and curves through ``UpdateManager.AddToDeleteList`` + ``DoUpdate``, explosions and
section views natively). Mixed kinds are allowed: the objects are grouped by kind and
each group goes to its deleter. An object NX already removed with an earlier group (a
feature that depended on a deleted sketch) is skipped. The handler reads back that every
requested object is gone and reports the other objects NX deleted with them
(``dependents``). One undo mark covers the call; any failure rolls everything back.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import hooks, tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Ref
from nx_mcp.kit import readback
from nx_mcp.kit.result import Row

MAX_DEPENDENTS = 100
QUIET = frozenset({"expression"})  # parameters NX deletes with their feature


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


@tool(
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
    example={"objects": ["f:12"]},
)
def nx_delete_objects(
    tx: Tx,
    *,
    objects: Annotated[list[Ref], Items(1, 100), Doc("Objects to delete; mixed kinds allowed")],
) -> DeleteData:
    """Delete 1-100 objects of the work part in one undoable step; mixed kinds allowed.
    Kinds: feature, sketch, curve, component, explosion, traceline, annotation, dimension,
    drawing_table, section_view, drawing_sheet, drawing_view. Objects NX deletes with
    them are reported as dependents."""
    deleters = hooks("deleter")
    groups: dict[str, list] = {}
    rows: dict[int, Row] = {}
    with tx.checks():
        for i, ref in enumerate(objects):
            obj = tx.resolve(ref)
            kind = _kind(tx, ref, obj)
            if kind not in deleters:
                fail(
                    "NX_WRONG_KIND",
                    f"{ref} is a {kind}; nx_delete_objects cannot delete that kind",
                    field=f"/objects/{i}",
                    hint="Delete curves or constraints inside a sketch with "
                    "nx_edit_sketch_curves, faces with nx_edit_faces",
                    details={"ref": ref, "kind": kind, "deletable": sorted(deleters)},
                )
            if kind != "component":  # a component's deleter checks it is a direct child
                tx.resolve(ref, work_part=True)
            tag = int(obj.Tag)
            if tag not in rows:
                rows[tag] = tx.ref(obj, kind)
                groups.setdefault(kind, []).append(obj)
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
    dependents = [
        services.describe(entry, live=False)
        for key, entry in before.items()
        if key not in after and key[0] not in QUIET and int(key[1]) not in rows
    ]
    return {
        "deleted": list(rows.values()),
        "dependents": dependents[:MAX_DEPENDENTS],
        "dependent_count": len(dependents),
    }
