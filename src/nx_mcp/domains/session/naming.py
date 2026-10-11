"""``nx_set_name``: rename one object by reference (tool-surface 3.1, decision 2).

It replaces ``nx_rename_object``, which also accepted a unique display name: the
catalogue takes a reference only. NX may normalise the name (most objects store it in
upper case), so the result reads the name back from the object.

A rename keeps the object (same tag), so the part's references stay valid: only the
renamed object's own anchor is recomputed (a sketch's journal identifier is its name),
and the result returns its reference with the stored name. Being light, the rename runs
in ``nx_run_batch`` and ``nx_preview_changes`` too.
"""

from __future__ import annotations

import re
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Ref, Text
from nx_mcp.kit import readback
from nx_mcp.kit.result import Row

MAX_NAME = 132
CONTROL = re.compile(r"[\x00-\x1f]")
NewName = Annotated[
    str,
    Text(MAX_NAME),
    Doc("1-132 characters, no newlines; NX may upper-case it"),
]


class SetNameData(TypedDict):
    object: Row  # the renamed object's reference (unchanged) with the stored name
    name: str  # the name NX stored (may be normalised, e.g. upper case)
    requested: str  # the name asked for
    references: str  # preserved: the part's other references stay valid


def _kind(tx: Tx, ref: str, obj) -> str:
    entry = getattr(tx.services.references, "entry", None)
    known = entry(ref) if entry is not None else None
    return known.kind if known is not None else tx.kind_of(obj)


def _check(name: str) -> None:
    if not name.strip():
        fail("NX_INVALID_ARGUMENT", "name: must not be empty (1-132 characters)",
             field="/name", hint='Send a visible name, e.g. "Base plate"')  # fmt: skip
    bad = CONTROL.search(name)
    if bad is not None:
        fail("NX_INVALID_ARGUMENT",
             f"name: control character {bad.group()!r} at {bad.start()} is not allowed",
             field="/name", hint="Send the name on one line, without tabs or newlines")  # fmt: skip


def _forget_anchor(tx: Tx, ref: str) -> None:
    """The renamed object's anchor may embed its old name (a sketch's journal identifier
    is its name): drop it so ``tx.ref`` computes it again from the stored name."""
    entry = getattr(tx.services.references, "entry", None)
    known = entry(ref) if entry is not None else None
    if known is not None and getattr(known, "anchor", None):
        known.anchor = None


@tool(
    target="assembly",
    effect="model",
    label="Set name",
    summary='Rename {object} to "{name}"',
    keywords=("rename", "name", "set name", "rename feature", "rename body", "label object"),
    requires=("work_part",),
    cost="light",
    invalidates="deleted",  # a rename changes no topology: face and edge refs stay valid
    batchable=True,
    previewable=True,
    example={"object": "f:12", "name": "Boss"},
)
def nx_set_name(tx: Tx, *, object: Ref, name: NewName) -> SetNameData:  # noqa: A002
    """Rename an object of the work part (feature, body, sketch, component) by its ref.
    Returns the name NX stored, often upper-cased; all references stay valid."""
    with tx.checks():
        _check(name)
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
    _forget_anchor(tx, object)
    return {
        "object": tx.ref(obj, kind),
        "name": stored or name,
        "requested": name,
        "references": "preserved",
    }
