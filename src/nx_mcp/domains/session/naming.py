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

from nx_mcp.core.catalogue import restorer, tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Ref, Text
from nx_mcp.kit import expr, readback
from nx_mcp.kit.result import Row

MAX_NAME = 132
CONTROL = re.compile(r"[\x00-\x1f]")
# NX 2406 SetName on an expression: the name is taken / not an identifier.
EXPRESSION_TAKEN, EXPRESSION_INVALID = 1050017, 1050011
NAME_CODES = {EXPRESSION_TAKEN: "NX_NAME_CONFLICT", EXPRESSION_INVALID: "NX_INVALID_ARGUMENT"}
IDENTIFIER_HINT = (
    "An expression name is a letter or _ followed by letters, digits or _ (at most 100), "
    "e.g. wall_thickness"
)
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


def _check_expression(tx: Tx, obj, name: str) -> None:
    """An expression's name is an identifier, unique in its part (case-sensitive): refused
    before NX, which would fail with 1050011 or 1050017."""
    if not expr.NAME.fullmatch(name):
        fail("NX_INVALID_ARGUMENT", f"name {name!r} is not a valid expression name",
             field="/name", hint=IDENTIFIER_HINT)  # fmt: skip
    if str(getattr(obj, "Name", "") or "") == name:
        return
    owner = getattr(obj, "OwningPart", None) or tx.part
    for other in list(getattr(owner, "Expressions", None) or []):
        if str(getattr(other, "Name", "") or "") == name and other is not obj:
            row: dict = {}
            try:
                row = dict(tx.ref(other, "expression"))
            except Exception:  # noqa: BLE001  # the refusal never fails on its details
                row = {"name": name}
            fail("NX_NAME_CONFLICT", f"The part already has an expression named {name}",
                 field="/name",
                 hint="Pick another name, or set the existing one with nx_set_expression",
                 details={"existing": row})  # fmt: skip


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
    keywords=(
        "rename",
        "name",
        "set name",
        "rename feature",
        "rename body",
        "rename expression",
        "label object",
    ),  # fmt: skip
    requires=("work_part",),
    cost="light",
    invalidates="deleted",  # a rename changes no topology: face and edge refs stay valid
    batchable=True,
    previewable=True,
    example={"object": "f:12", "name": "Boss"},
)
def nx_set_name(tx: Tx, *, object: Ref, name: NewName) -> SetNameData:  # noqa: A002
    """Rename an object of the work part (feature, body, sketch, component, expression) by
    its ref. Returns the name NX stored, often upper-cased; all references stay valid. An
    expression name is a unique identifier. A part is renamed by saving it as a new file
    (nx_save_part path)."""
    with tx.checks():
        _check(name)
        obj = tx.resolve(object)
        kind = _kind(tx, object, obj)
        if kind == "part":  # NX refuses SetName on a part (580014): its name is its file
            fail("NX_WRONG_KIND",
                 f"{object} is a part: a part's name is its file name, which nx_set_name "
                 "cannot change", field="/object",
                 hint="Rename a part by saving it as a new file: nx_save_part with part and "
                 "path (a new .prt in the workspace); the old file stays",
                 details={"ref": object, "kind": "part"})  # fmt: skip
        if kind != "component":  # a component lives in the assembly, not as its body
            tx.resolve(object, work_part=True)
        if kind == "expression":
            _check_expression(tx, obj, name)
    before = str(getattr(obj, "Name", "") or "")
    try:
        with native(NAME_CODES):
            obj.SetName(name)
    except NXError as error:
        if error.code in ("NX_NAME_CONFLICT", "NX_INVALID_ARGUMENT"):
            error.field = "/name"
            error.hint = error.hint or (
                IDENTIFIER_HINT if kind == "expression" else "Pick another name"
            )
        raise
    stored = str(getattr(obj, "Name", "") or "")
    readback.expect(
        bool(stored),
        "name",
        f"NX did not store a name on {object}",
        requested=name,
    )
    _forget_anchor(tx, object)
    tx.note_restore({"object": object, "kind": kind, "before": before, "after": stored})
    return {
        "object": tx.ref(obj, kind),
        "name": stored or name,
        "requested": name,
        "references": "preserved",
    }


@restorer("nx_set_name")
def _restore_name(tx: Tx, data: dict, *, apply: bool) -> str:
    """Set the name an ``nx_set_name`` replaced back (the compensating undo): refused when
    the object was renamed again since."""
    ref, before, after = data["object"], str(data["before"]), str(data["after"])
    obj = tx.resolve(ref)
    now = str(getattr(obj, "Name", "") or "")
    if now.casefold() != after.casefold():
        fail(
            "NX_DEPENDENCY_ORDER",
            f"{ref} was renamed since: it is {now!r}, the operation named it {after!r}",
            details={"object": ref, "name": now},
        )
    if apply:
        if before:
            obj.SetName(before)
        else:  # NX 2406 ignores SetName(""); UF removes the name (measured)
            with native():
                tx.uf.Obj.DeleteName(obj.Tag)
        stored = str(getattr(obj, "Name", "") or "")
        back = stored.casefold() == before.casefold()
        readback.expect(back, "name", f"{ref} reads {stored!r}, not {before!r}", name=stored)
        _forget_anchor(tx, ref)
    if not before:
        return f"the name of {ref} removed (it had none)"
    return f"the name of {ref} back to {before!r}"
