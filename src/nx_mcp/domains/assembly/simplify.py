"""Simplify Assembly: one lightweight solid for an assembly (outer shell, holes and internal
voids filled, internal and small bodies dropped), e.g. a block model of a module for a yard
layout or a supplier. The result is a dumb body (no features behind it).

NX 2406 findings (live):

- ``AssemblyManager.CreateSimplifyBuilder()`` works while the assembly is both work and
  display part. ``ObjectsToSimplify`` takes components or occurrence solids,
  ``DestinationPart`` a fully loaded part: another part, or the assembly itself.
- Commit returns None; the destination gains one BREP feature per result body
  (``UNPARAMETERIZED_FEATURE``, plus a ``FEATURE_SET`` in another part). With
  ``UniteBodies`` overlapping and touching solids become one body: three 100 mm cubes, two
  overlapping by 10 mm and one touching, give one body of 2.9e6 mm3 (their union).
- ``MaximumHoleSize`` (diameter), ``MaximumBlendRadius`` and ``MinimumBodySize`` (box
  diagonal) are expressions; ``Use*`` toggles switch each filter on.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from nx_mcp.core import target as targets
from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Length, RefTo, Text
from nx_mcp.domains.assembly import common, structure
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import builders, expr, licenses, measure, readback, result, select
from nx_mcp.kit import parts as kparts

MAX_OBJECTS = 500
ObjectRef = Annotated[str, RefTo("component", "body", doc="Component 'o:N' or solid body ref")]


class SimplifyData(TypedDict):
    destination: str  # file name of the part holding the result
    bodies: list[result.Row]
    volume: float  # of the result bodies
    source_volume: float  # sum of the source solids (overlaps counted twice)
    sources: int  # source solids


def _sources(tx, refs) -> list:
    """Occurrence solids of the given components/bodies, or of every component."""
    assembly = {int(c.Tag): c for c, _names in kparts.components(tx.part)}
    if refs is None:
        found = [b for c in structure.direct_children(tx.part) for b in kbodies.of(tx, c)]
        found = [b for b in found if getattr(b, "IsSolidBody", False)]
        if not found:
            fail("NX_WRONG_GEOMETRY", "The assembly's components have no solid body (are "
                 "their parts loaded?)", field="/objects")  # fmt: skip
        return found
    out: list = []
    for i, ref in enumerate(refs):
        field = f"/objects/{i}"
        obj = select.resolve(tx, ref, "component", "body", field=field)
        if kbodies.is_component(obj):
            if int(obj.Tag) not in assembly:
                fail("NX_WRONG_OWNER", f"{ref} is not a component of the work part",
                     field=field, hint=common.OWNER_HINT)  # fmt: skip
            found = [b for b in kbodies.of(tx, obj) if getattr(b, "IsSolidBody", False)]
            if not found:
                fail("NX_WRONG_GEOMETRY", f"{ref} has no solid body (suppressed or not "
                     "loaded?)", field=field)  # fmt: skip
        else:
            owner = getattr(obj, "OwningComponent", None)
            if owner is None or int(owner.Tag) not in assembly:
                fail("NX_WRONG_OWNER", f"{ref} is not a component body of the work part",
                     field=field, hint="Pass occurrence bodies or components")  # fmt: skip
            if not getattr(obj, "IsSolidBody", False):
                fail("NX_WRONG_GEOMETRY", f"{ref} is a sheet body; simplify takes solids",
                     field=field)  # fmt: skip
            found = [obj]
        out.extend(found)
    unique = list({int(b.Tag): b for b in out}.values())
    if len(unique) != len(out):
        fail("NX_INVALID_ARGUMENT", "objects name a solid twice", field="/objects")
    return unique


def _destination(tx, text):
    """The part that receives the result: the work assembly (None) or another loaded part
    the agent owns."""
    part = tx.part
    if text is None:
        return part
    dest = targets.part_from_text(tx.services, text, "/destination_part")
    if kparts.same_part(dest, part):
        return part
    if any(kparts.same_part(c.Prototype, dest) for c, _n in kparts.components(part)):
        fail("NX_INVALID_ARGUMENT", "destination_part is a component of the assembly",
             field="/destination_part", hint="Create an empty part with nx_create_part")  # fmt: skip
    if not getattr(dest, "IsFullyLoaded", True):
        fail("NX_NOT_LOADED", "destination_part is only partially loaded",
             field="/destination_part")  # fmt: skip
    tx.require_owned(dest, field="/destination_part")
    return dest


def _limit(b, use, value, size) -> None:
    """Switch one size filter on with its limit, or off."""
    setattr(b, use, value is not None)
    if value is not None:
        expr.drive(getattr(b, size), value)


@tool(
    effect="model",
    label="Simplify assembly",
    summary="Simplify the assembly",
    keywords=(
        "simplify assembly",
        "simplified body",
        "outer shell",
        "envelope body",
        "fill holes",
        "lightweight model",
        "block model",
    ),  # fmt: skip
    requires=("work_part", "assembly", "no_active_sketch"),
    display_bound=True,
    cost="long",
    example={"fill_holes_up_to": 50, "destination_part": "module_block"},
)
def nx_create_simplified_assembly(
    tx: Tx,
    *,
    objects: Annotated[
        list[ObjectRef], Items(1, MAX_OBJECTS), Doc("Default: every component's solids")
    ]
    | None = None,
    destination_part: Annotated[
        str,
        Text(260),
        Doc("Loaded empty part (nx_create_part) for the result; default: the assembly"),
    ]
    | None = None,
    fill_holes_up_to: Annotated[Length, Doc("Fill holes up to this diameter")] | None = None,
    remove_blends_up_to: Annotated[Length, Doc("Remove blends up to this radius")] | None = None,
    min_body_size: Annotated[Length, Doc("Drop bodies whose box diagonal is smaller")]
    | None = None,
    remove_internal_bodies: Annotated[bool, Doc("Drop bodies enclosed by others")] = True,
    fill_voids: Annotated[bool, Doc("Fill internal enclosed voids")] = True,
    unite: Annotated[bool, Doc("Unite overlapping and touching solids into one body")] = True,
) -> SimplifyData:
    """Create one simplified solid of the work assembly (Simplify Assembly), e.g. a block model.
    Unites the solids, fills holes and removes blends up to a size, drops internal bodies and
    voids. The result is a dumb body in the assembly or destination_part; components stay."""
    limits = {"fill_holes_up_to": fill_holes_up_to, "remove_blends_up_to": remove_blends_up_to,
              "min_body_size": min_body_size}  # fmt: skip
    for key, value in limits.items():
        if value is not None and float(value) <= 0:
            fail("NX_INVALID_ARGUMENT", f"{key} must be positive", field=f"/{key}")
    with tx.checks():
        sources = _sources(tx, objects)
        dest = _destination(tx, destination_part)
        licenses.require(tx, ("assemblies",), "Simplify Assembly needs an assemblies license")
    part = tx.part
    if dest is not part:
        tx.touch_part(dest)
    source_volume = measure.volume(tx, sources)
    before = {int(b.Tag) for b in dest.Bodies}
    with builders.build(tx, part.AssemblyManager.CreateSimplifyBuilder) as b:
        with native(default="NX_BUILD_FAILED"):
            b.DestinationPart = dest
            b.ObjectsToSimplify.Add(sources)
            _limit(b, "UseHoleDiameter", fill_holes_up_to, "MaximumHoleSize")
            _limit(b, "UseBlendRadius", remove_blends_up_to, "MaximumBlendRadius")
            _limit(b, "UseBodySize", min_body_size, "MinimumBodySize")
            b.RemoveInternalBodies = bool(remove_internal_bodies)
            b.RemoveInternalVoids = bool(fill_voids)
            b.UniteBodies = bool(unite)
        b.commit()
    made = [x for x in dest.Bodies if int(x.Tag) not in before]
    readback.expect(bool(made), "bodies", "NX made no simplified body")
    volume = measure.volume(tx, [x for x in made if getattr(x, "IsSolidBody", False)])
    readback.expect(volume > 0, "volume", "The simplified bodies have no volume", volume=volume)
    return {
        "destination": kparts.file_of(dest).replace("\\", "/").rsplit("/", 1)[-1],
        "bodies": [tx.ref(x, kind="body") for x in made],
        "volume": volume,
        "source_volume": source_volume,
        "sources": len(sources),
    }
