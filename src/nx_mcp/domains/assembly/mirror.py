"""Mirror components across a plane (tool-surface 3.8, mirror assembly).

NX 2406 findings encoded here:

- ``AssemblyManager.CreateReflectComponentBuilder(part, None)`` reflects direct
  components of the work part across a plane (``SetReflectionPlane``) and creates
  one new component per source, named like it; ``GetReflectedComponents`` returns
  only one of them, so the new direct children are paired with their sources by
  origin.
- Every new component's origin is read back against the source origin reflected
  across the plane.
- NX names each copy like its source, so two components would share a name (and the
  name-based component anchor): a copy is renamed ``<SOURCE>_MIRROR`` (``_MIRROR_2``...
  when taken).
- The copy reuses the source's part with a reflected transform: a deformation of the
  source (an assembly ``FLEXED_PART``) is not copied, so a deformed source warns.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import ComponentRef, Items, Plane
from nx_mcp.domains.assembly import common, structure
from nx_mcp.domains.assembly.common import ComponentRow
from nx_mcp.kit import args, builders, deform, geom, model, readback, result, select


class MirroredPair(TypedDict):
    source: ComponentRow
    mirrored: ComponentRow


class MirrorData(TypedDict):
    components: list[MirroredPair]
    plane: dict


@tool(
    effect="model",
    label="Mirror components",
    summary=lambda a, say: f"Mirror {say('components')}",
    keywords=(
        "mirror assembly",
        "reflect component",
        "mirror component",
        "symmetric assembly",
        "opposite hand",
        "left hand version",
        "right hand version",
    ),
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="heavy",
    invalidates="deleted",
    example={"components": ["o:4"], "plane": {"principal": "yz"}},
)
def nx_create_component_mirror(
    tx: Tx,
    *,
    components: Annotated[list[ComponentRef], Items(1, 100)],
    plane: Plane,
) -> MirrorData:
    """Mirror direct components of the work part across a plane into new components named
    <SOURCE>_MIRROR. Each new component's origin is its source's origin reflected across the
    plane. A copy reuses the source's part file with a reflected transform (no opposite-hand
    part file). A source's deformation is not copied."""
    args.distinct("/components", components, "components")
    with tx.checks():
        values = [common.component(tx, r, f"/components/{i}") for i, r in enumerate(components)]
        _, origin, normal = select.plane(tx, plane, "/plane", occurrences=True)
    part = tx.part
    nx = tx.nx
    before = {int(c.Tag) for c in structure.direct_children(part)}
    with builders.build(tx, part.AssemblyManager.CreateReflectComponentBuilder, part, None) as b:
        with native(default="NX_BUILD_FAILED"):
            b.SetComponentsToReflect(values)
            b.SetReflectionPlane(
                part.Planes.CreatePlane(
                    nx.Point3d(*origin),
                    nx.Vector3d(*normal),
                    nx.SmartObject.UpdateOption.WithinModeling,
                )
            )
        b.commit()
    model.update(tx)
    added = [c for c in structure.direct_children(part) if int(c.Tag) not in before]
    readback.expect(
        len(added) == len(values), "components", "NX mirrored another count",
        actual=len(added),
    )  # fmt: skip
    new_tags = {int(c.Tag) for c in added}
    taken = {
        str(c.Name).upper() for c in structure.direct_children(part) if int(c.Tag) not in new_tags
    }
    pairs = []
    for i, source in enumerate(values):
        want = geom.reflect(structure.pose(source)[0], origin, normal)
        copy = min(added, key=lambda c: geom.dist(structure.pose(c)[0], want))
        got = structure.pose(copy)[0]
        readback.expect(
            geom.dist(got, want) <= 1e-6, f"origin[{i}]", "The mirrored origin is off",
            actual=got,
        )  # fmt: skip
        added.remove(copy)
        name = _mirror_name(source, taken)
        if str(copy.Name).upper() != name:
            result.rename(tx, copy, name)
        taken.add(str(copy.Name).upper())
        pairs.append((source, copy))
    names = structure.paths(part)
    out: list[MirroredPair] = [
        {
            "source": common.component_row(tx, source, names.get(int(source.Tag))),
            "mirrored": common.component_row(tx, copy, names.get(int(copy.Tag))),
        }
        for source, copy in pairs
    ]
    for i, source in enumerate(values):
        flexed = None
        with contextlib.suppress(Exception):
            flexed = deform.deformation(tx, part, source)
        if flexed is not None:
            tx.warn("NX_UPDATE_WARNING", f"{components[i]} is deformed; its mirror uses the "
                    "part's own values: deform it with nx_set_component_deformation")  # fmt: skip
    return {"components": out, "plane": {"origin": origin, "normal": normal}}


def _mirror_name(source, taken) -> str:
    """``<SOURCE>_MIRROR``, numbered when another component has that name."""
    base = f"{str(source.Name).upper()}_MIRROR"
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}_{n}"
    return name
