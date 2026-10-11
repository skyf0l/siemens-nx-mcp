"""Custom reference sets of the work part (tool-surface 3.8).

NX 2406 finding encoded here: in an assembly NX seeds a new reference set with every
existing component; ``SetAddComponentsAutomatically(False, False)`` affects only later
additions. Unrequested members are removed and the membership is read back exactly.
A set in a component's part (``component``) is made with that component as the work
component (``common.working_in``: ``SetWorkComponent``, the previous work part restored,
also on failure); occurrence members map to their prototype objects.
"""

from __future__ import annotations

import re
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import ComponentRef, Doc, Items, RefTo, Text
from nx_mcp.domains.assembly import common
from nx_mcp.kit import args, display, model, readback, result, select

BUILT_IN = ("entire part", "empty")
Member = Annotated[str, RefTo("body", "curve", "datum", "component", "sketch", "point")]
# Checked in the tool, so a refused name gets a suggestion instead of "invalid format".
NAME_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,63}$"
SetName = Annotated[str, Text(64), Doc("New set name: letters, digits, _ or - (e.g. SOLID)")]


class ReferenceSetData(TypedDict):
    reference_set: result.Row
    members: list[result.Row]
    member_count: int


def members(tx, reference_set) -> list:
    return list(reference_set.AskAllDirectMembers())


def _kind(tx, obj) -> str:
    """The reference kind of a member; sketch curves have no class of their own."""
    try:
        return tx.kind_of(obj)
    except Exception:
        return "curve"


def _work_part_member(tx, part, obj, field):
    is_component = tx.kind_of(obj) == "component"
    owner = getattr(obj, "OwningPart", None)
    if (getattr(obj, "IsOccurrence", False) and not is_component) or not display.same(owner, part):
        fail("NX_WRONG_OWNER", "Members must belong to the work part", field=field,
             hint="For a set in a component's part, pass component=<its ref>")  # fmt: skip
    return obj


def _component_member(tx, component, obj, field):
    """The prototype object of ``obj`` (an occurrence the component shows, or an object of
    its part)."""
    if tx.kind_of(obj) == "component":
        fail("NX_UNSUPPORTED_OPTION", "With component, members are bodies, curves, datums, "
             "sketches or points", field=field)  # fmt: skip
    if getattr(obj, "IsOccurrence", False):
        if not display.same(getattr(obj, "OwningComponent", None), component):
            fail("NX_WRONG_OWNER", "The member is not shown by that component", field=field)
        obj = obj.Prototype
    if not display.same(getattr(obj, "OwningPart", None), component.Prototype):
        fail("NX_WRONG_OWNER", "The member is not in the component's part", field=field)
    return obj


@tool(
    effect="model",
    label="Create reference set",
    summary="Reference set {name} with {objects}",
    keywords=("reference set", "refset", "simplified representation", "body only set"),
    requires=("work_part", "no_active_sketch"),
    cost="light",
    invalidates="deleted",
    refresh_annotations=False,
    example={"name": "SOLID", "objects": ["b:3"]},
)
def nx_create_reference_set(
    tx: Tx,
    *,
    name: SetName,
    objects: Annotated[list[Member], Items(1, 1000)],
    component: Annotated[ComponentRef, Doc("Its part gets the set")] | None = None,
) -> ReferenceSetData:
    """Create a reference set of exactly these bodies, curves, datums or sketches (or components
    of the work part). component: in that component's part, from its occurrences or part
    objects. Apply with nx_edit_component."""
    if not re.match(NAME_PATTERN, name):
        suggestion = re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_")[:64] or "SET"
        fail(
            "NX_INVALID_ARGUMENT",
            f"Reference set names use letters, digits, _ and - only, e.g. {suggestion!r}",
            field="/name",
        )
    if name.casefold() in BUILT_IN:
        fail("NX_INVALID_ARGUMENT", "Entire Part and Empty are built in", field="/name")
    args.distinct("/objects", objects, "objects")
    with tx.checks():
        comp = None
        part = tx.part
        if component is not None:
            comp = common.in_assembly(tx, component, "/component")
            part = comp.Prototype
        if any(str(r.Name).casefold() == name.casefold() for r in part.GetAllReferenceSets()):
            fail("NX_ALREADY_EXISTS", f"A reference set {name} already exists", field="/name")
        values = []
        seen: set[int] = set()
        for i, ref in enumerate(objects):
            field = f"/objects/{i}"
            obj = select.resolve(tx, ref, field=field)
            if comp is None:
                values.append(_work_part_member(tx, part, obj, field))
                continue
            obj = _component_member(tx, comp, obj, field)
            if int(obj.Tag) in seen:
                fail("NX_INVALID_ARGUMENT", f"{ref} names a member twice", field=field)
            seen.add(int(obj.Tag))
            values.append(obj)
    if comp is None:
        return _create(tx, part, name, values)
    tx.touch_part(part)  # the component's part changes: the kernel reads it back too
    with common.working_in(tx, comp):
        return _create(tx, part, name, values)


def _create(tx, part, name, values) -> ReferenceSetData:
    wanted = {int(v.Tag) for v in values}
    with native(default="NX_BUILD_FAILED"):
        created = part.CreateReferenceSet()
        created.SetName(name)
        created.SetAddComponentsAutomatically(False, False)
        seeded = [m for m in members(tx, created) if int(m.Tag) not in wanted]
        if seeded:
            created.RemoveObjectsFromReferenceSet(seeded)
        created.AddObjectsToReferenceSet(values)
    model.update(tx)
    got = members(tx, created)
    readback.expect(
        {int(m.Tag) for m in got} == wanted, "members", "Reference set members differ",
        count=len(got), wanted=len(wanted),
    )  # fmt: skip
    rows = [tx.ref(m, _kind(tx, m)) for m in got]
    return {
        "reference_set": tx.ref(created, "reference_set"),
        "members": rows,
        "member_count": len(got),
    }
