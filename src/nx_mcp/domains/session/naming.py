"""``nx_set_name``: rename one object by reference (tool-surface 3.1, decision 2).

It replaces ``nx_rename_object``, which also accepted a unique display name: the
catalogue takes a reference only. NX may normalise the name (case, characters), so
the result reads the name back from the object. Every reference of the part is
invalidated (``changes.references`` is ``part_invalidated``); the result carries a fresh reference to the renamed object.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Ref, Text
from nx_mcp.kit import readback
from nx_mcp.kit.result import Row

NewName = Annotated[str, Text(132, r"^[^\x00-\x1f]{1,132}$"), Doc("New name, 1-132 characters")]


class SetNameData(TypedDict):
    object: Row  # fresh reference to the renamed object
    name: str  # the name NX stored (may be normalised)
    requested: str  # the name asked for
    references: str  # part_invalidated: reacquire the part's other references


def _kind(tx: Tx, ref: str, obj) -> str:
    entry = getattr(tx.services.references, "entry", None)
    known = entry(ref) if entry is not None else None
    return known.kind if known is not None else tx.kind_of(obj)


@tool(
    effect="model",
    label="Set name",
    summary='Rename {object} to "{name}"',
    keywords=("rename", "name", "set name", "rename feature", "rename body", "label object"),
    requires=("work_part",),
    cost="light",
    example={"object": "f:12", "name": "Boss"},
)
def nx_set_name(tx: Tx, *, object: Ref, name: NewName) -> SetNameData:  # noqa: A002
    """Rename any object of the work part (feature, body, sketch, component, ...) and
    return the name NX stored. Invalidates the part's references; the result has a
    fresh reference to the object."""
    with tx.checks():
        obj = tx.resolve(object)
        kind = _kind(tx, object, obj)
        if kind != "component":  # a component lives in the assembly, not as its body
            tx.resolve(object, work_part=True)
    obj.SetName(name)
    stored = str(getattr(obj, "Name", "") or "")
    readback.expect(
        bool(stored),
        "name",
        f"NX did not store a name on {object}",
        requested=name,
    )
    tx.services.invalidate_parts([tx.part])
    tx.references = "part_invalidated"
    return {
        "object": tx.ref(obj, kind),
        "name": stored or name,
        "requested": name,
        "references": "part_invalidated",
    }
