"""Mirror components across a plane (tool-surface 3.8, mirror assembly).

NX 2406 findings encoded here:

- ``AssemblyManager.CreateReflectComponentBuilder(part, None)`` reflects direct
  components of the work part across a plane (``SetReflectionPlane``) and creates
  one new component per source, named like it; ``GetReflectedComponents`` returns
  only one of them, so the new direct children are paired with their sources by
  origin.
- Every new component's origin is read back against the source origin reflected
  across the plane.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Items, Plane
from nx_mcp.domains.assembly import common
from nx_mcp.domains.assembly.common import ComponentRef, ComponentRow
from nx_mcp.kit import args, assembly, builders, geom, model, readback, select


class MirroredPair(TypedDict):
    source: ComponentRow
    mirrored: ComponentRow


class MirrorData(TypedDict):
    components: list[MirroredPair]
    plane: dict


def reflect_point(p, origin, normal):
    """``p`` reflected across the plane through ``origin`` with unit ``normal``."""
    d = geom.dot(geom.sub(p, origin), normal)
    return geom.sub(p, geom.scale(normal, 2.0 * d))


@tool(
    effect="model",
    label="Mirror components",
    summary=lambda a, say: f"Mirror {say('components')}",
    keywords=("mirror assembly", "reflect component", "mirror component", "symmetric assembly"),
    requires=("work_part", "no_active_sketch", "assembly"),
    cost="heavy",
    invalidates="deleted",
    example={"components": ["o:4"], "plane": {"principal": "yz"}},
)
def nx_create_component_mirror(
    tx: Tx,
    *,
    components: Annotated[list[ComponentRef], Items(1, 100)],  # type: ignore[valid-type]
    plane: Plane,
) -> MirrorData:
    """Mirror direct components of the work part across a plane into new components. Each new
    component's origin is its source's origin reflected across the plane."""
    args.distinct("/components", components, "components")
    with tx.checks():
        values = [common.component(tx, r, f"/components/{i}") for i, r in enumerate(components)]
        _, origin, normal = select.plane(tx, plane, "/plane")
    part = tx.part
    nx = tx.nx
    before = {int(c.Tag) for c in assembly.direct_children(part)}
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
    added = [c for c in assembly.direct_children(part) if int(c.Tag) not in before]
    readback.expect(
        len(added) == len(values), "components", "NX mirrored another count",
        actual=len(added),
    )  # fmt: skip
    names = assembly.paths(part)
    out = []
    for i, source in enumerate(values):
        want = reflect_point(assembly.pose(source)[0], origin, normal)
        copy = min(added, key=lambda c: geom.dist(assembly.pose(c)[0], want))
        got = assembly.pose(copy)[0]
        readback.expect(
            geom.dist(got, want) <= 1e-6, f"origin[{i}]", "The mirrored origin is off",
            actual=got,
        )  # fmt: skip
        added.remove(copy)
        out.append(
            {
                "source": common.component_row(tx, source, names.get(int(source.Tag))),
                "mirrored": common.component_row(tx, copy, names.get(int(copy.Tag))),
            }
        )
    return {"components": out, "plane": {"origin": origin, "normal": normal}}
