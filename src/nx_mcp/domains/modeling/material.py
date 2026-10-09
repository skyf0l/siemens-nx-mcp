"""Materials: read and assign body materials (tool-surface 3.6).

Densities are kg/m^3 (UF ``AskBodyDensity`` with ``KILOGRAMS_METERS``); an
occurrence body reports its prototype's material.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Range, RefTo, Text
from nx_mcp.domains.modeling import common
from nx_mcp.kit import builders, readback, result

DENSITY_REL = 1e-8


class MaterialRow(TypedDict):
    body: result.Row
    material: str | None
    density: float


class MaterialData(TypedDict):
    items: list[MaterialRow]


def _density(tx, body):
    units = tx.nx.UF.Modl.DensityUnits.KILOGRAMS_METERS
    with native():
        return float(tx.uf.Modeling.AskBodyDensity(body.Tag, units))


def _material_of(prototype):
    materials = prototype.OwningPart.MaterialManager.PhysicalMaterials
    with native():
        found = materials.AskMaterialOfObject(prototype)
    return str(found.Name) if found is not None else None


def _assembly_bodies(tx):
    """Occurrence bodies of every loaded, unsuppressed component (depth first)."""
    root = tx.part.ComponentAssembly.RootComponent
    out, stack = [], [root] if root is not None else []
    while stack:
        component = stack.pop()
        tx.checkpoint()
        stack.extend(reversed(list(component.GetChildren())))
        if component is root or getattr(component, "IsSuppressed", False):
            continue
        prototype = getattr(component, "Prototype", None)
        for body in getattr(prototype, "Bodies", None) or []:
            occurrence = component.FindOccurrence(body)
            if occurrence is not None:
                out.append(occurrence)
    return out


@tool(
    effect="read",
    label="Get materials",
    summary=lambda a, say: (
        "Materials of " + (common.bodies_text(a) if a.get("bodies") else a.get("scope", "part"))
    ),
    keywords=("material", "density", "mass properties", "physical material", "material info"),
    requires=("work_part",),
    cost="light",
    example={"scope": "part"},
)
def nx_get_material(
    tx: Tx,
    *,
    bodies: Annotated[list[RefTo("body")], Items(1, 200)] | None = None,  # type: ignore[valid-type]
    scope: Annotated[
        Literal["part", "assembly"], Doc("Without bodies: work part or components")
    ] = "part",
) -> MaterialData:
    """Read material names and densities (kg/m^3) of bodies, the work part or the assembly.
    Component bodies report their prototype's material."""
    if bodies is not None:
        found = [tx.resolve(ref, "body") for ref in bodies]
    elif scope == "assembly":
        found = _assembly_bodies(tx)
    else:
        found = list(tx.part.Bodies)
    items = []
    for body in found:
        prototype = body.Prototype if getattr(body, "IsOccurrence", False) else body
        items.append(
            {
                "body": tx.ref(body, kind="body"),
                "material": _material_of(prototype),
                "density": _density(tx, prototype),
            }
        )
    return {"items": items}


class Custom(TypedDict):
    name: Annotated[str, Text(100, r"^\S.*$"), Doc("New material name")]
    density: Annotated[float, Range(gt=0, le=1e6), Doc("Mass density in kg/m^3")]


class MaterialSpec(TypedDict):
    custom: Custom


for _td in (Custom, MaterialSpec):
    _td.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]


@tool(
    effect="model",
    label="Set material",
    summary=lambda a, say: (
        f"Material {a['material']['custom']['name']} on " + common.bodies_text(a)
    ),
    keywords=("assign material", "density", "material", "physical material", "mass"),
    requires=("work_part", "no_active_sketch"),
    cost="light",
    invalidates="deleted",  # densities only: face and edge references stay valid
    example={"bodies": ["b:3"], "material": {"custom": {"name": "AL6061", "density": 2700}}},
)
def nx_set_material(
    tx: Tx,
    *,
    bodies: Annotated[list[RefTo("body")], Items(1, 200)],  # type: ignore[valid-type]
    material: MaterialSpec,
) -> MaterialData:
    """Assign a custom material (name and density) to solid bodies of the work part. An existing
    material of that name is reused only with the same density."""
    spec = material["custom"]
    targets = common.solids(tx, bodies)
    part = tx.part
    materials = part.MaterialManager.PhysicalMaterials
    existing = [m for m in materials if str(m.Name).casefold() == spec["name"].casefold()]
    if existing:
        chosen = existing[0]
    else:
        kind = tx.nx.PhysicalMaterial.Type.Isotropic
        with builders.build(tx, materials.CreatePhysicalMaterialBuilder, kind) as b:
            b.Name = spec["name"]
            b.PropertyTable.SetBaseScalarWithDataPropertyValue(
                "MassDensity",
                float(spec["density"]),
                part.UnitCollection.FindObject("KilogramPerCubicMeter"),
            )
            chosen = b.commit()
    with native():
        chosen.AssignObjects(targets)
    items = []
    for i, body in enumerate(targets):
        density = _density(tx, body)
        if existing and abs(density - spec["density"]) > DENSITY_REL * spec["density"]:
            fail(
                "NX_ALREADY_EXISTS",
                f"Material {chosen.Name} exists with density {density:g} kg/m^3",
                field="/material/custom/name",
                hint="Use another name, or the existing density",
            )
        readback.expect_close(
            density, spec["density"], f"density[{i}]", tol=DENSITY_REL * spec["density"] + 1e-9
        )
        items.append(
            {"body": tx.ref(body, kind="body"), "material": str(chosen.Name), "density": density}
        )
    return {"items": items}
