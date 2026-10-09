"""Custom reference sets of the work part (tool-surface 3.8).

NX 2406 finding encoded here: in an assembly NX seeds a new reference set with every
existing component; ``SetAddComponentsAutomatically(False, False)`` affects only later
additions. Unrequested members are removed and the membership is read back exactly.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, RefTo, Text
from nx_mcp.kit import args, assembly, model, readback, result, select

BUILT_IN = ("entire part", "empty")
Member = RefTo("body", "curve", "datum", "component", "sketch", "point")
SetName = Annotated[
    str, Text(64, r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,63}$"), Doc("New reference set name")
]


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
    objects: Annotated[list[Member], Items(1, 1000)],  # type: ignore[valid-type]
) -> ReferenceSetData:
    """Create a custom reference set of the work part with exactly these members. Members are
    bodies, curves, datums, sketches or components; apply it with nx_edit_component."""
    if name.casefold() in BUILT_IN:
        fail("NX_INVALID_ARGUMENT", "Entire Part and Empty are built in", field="/name")
    args.distinct("/objects", objects, "objects")
    part = tx.part
    with tx.checks():
        if any(str(r.Name).casefold() == name.casefold() for r in part.GetAllReferenceSets()):
            fail("NX_ALREADY_EXISTS", f"A reference set {name} already exists", field="/name")
        values = []
        for i, ref in enumerate(objects):
            obj = select.resolve(tx, ref, field=f"/objects/{i}")
            owner = getattr(obj, "OwningPart", None)
            is_component = tx.kind_of(obj) == "component"
            if (getattr(obj, "IsOccurrence", False) and not is_component) or not assembly.same(
                owner, part
            ):
                fail("NX_WRONG_OWNER", "Members must belong to the work part",
                     field=f"/objects/{i}")  # fmt: skip
            values.append(obj)
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
