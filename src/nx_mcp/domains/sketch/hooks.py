"""Sketch lister for ``nx_list_objects`` and sketch/curve deleters for ``nx_delete_objects``.

A deleter is ``fn(tx, objects)`` and deletes inside the caller's mark through the update
manager, like NX's Delete: features built on a deleted sketch or curve go with it.
Curves and constraints inside a sketch can also be deleted with ``nx_edit_sketch_curves``.
"""

from __future__ import annotations

from nx_mcp.core.catalogue import deleter, lister
from nx_mcp.core.errors import fail, native
from nx_mcp.kit import model


@lister("sketch")
def sketches(tx, owner, owner_kind):
    if owner is not None:
        fail("NX_UNSUPPORTED_OPTION", "sketches are listed for the whole part", field="/owner")
    active = tx.session.ActiveSketch
    return [
        (s, "sketch", {"details": {"active": active is not None and s.Tag == active.Tag}})
        for s in tx.part.Sketches
    ]


def no_active_sketch(tx):
    if tx.session.ActiveSketch is not None:
        fail("NX_SKETCH_ACTIVE", "Finish the active sketch before deleting sketches or curves",
             hint="Finish the active sketch, then retry")  # fmt: skip


@deleter("sketch")
@deleter("curve")
def delete_sketch_geometry(tx, objects):
    no_active_sketch(tx)
    with native(default="NX_BUILD_FAILED"):
        tx.session.UpdateManager.AddToDeleteList(list(objects))
    model.update(tx)
