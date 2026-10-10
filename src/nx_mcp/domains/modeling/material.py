"""Materials: list the NX material library, read and assign body materials (tool-surface
3.6, 4).

Densities are kg/m^3 (UF ``AskBodyDensity`` with ``KILOGRAMS_METERS``); an occurrence body
reports its prototype's material. Library properties come from
``GetMaterialPropertyValueAndDisplayName`` as ``"<number> <unit>"`` strings (or a
``Tabular Data`` table over temperature) and are converted to fixed units: density kg/m^3,
Young's modulus MPa, thermal expansion 1/K, conductivity W/(m*K), specific heat J/(kg*K).
A tabulated property reports the row nearest room temperature (20 degC).
"""

from __future__ import annotations

import difflib
import ntpath
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import BodyRef, Doc, Items, Offset, PageLimit, Range, Text, forbid_extra
from nx_mcp.domains.modeling import common
from nx_mcp.kit import builders, readback, result

DENSITY_REL = 1e-8
LIBRARY_DENSITY_REL = 1e-6  # the library stores four to five significant digits
DEFAULT_LIBRARY = ""  # the library the material customer defaults name
NATIVE = {3940021: "NX_ALREADY_EXISTS", 3940020: "NX_CATALOG_UNAVAILABLE"}

# field -> (NX neutral property names tried in order, dimension)
PROPERTIES: dict[str, tuple[tuple[str, ...], str]] = {
    "density": (("MassDensityConstant", "MassDensity"), "density"),
    "youngs_modulus": (("YoungsModulusConstant", "YoungsModulus"), "stress"),
    "poissons_ratio": (("PoissonsRatioConstant", "PoissonsRatio"), "ratio"),
    "thermal_expansion": (("ThermalExpansionConstant", "ThermalExpansion"), "expansion"),
    "thermal_conductivity": (("ThermalConductivity",), "conductivity"),
    "specific_heat": (("SpecificHeat",), "specific_heat"),
}

_PSI = 0.00689475729
# dimension -> {normalised NX unit: factor to the reported unit}
UNITS: dict[str, dict[str, float]] = {
    "density": {
        "kg/mm3": 1e9,
        "kg/m3": 1.0,
        "kg/cm3": 1e6,
        "g/cm3": 1e3,
        "g/mm3": 1e6,
        "t/mm3": 1e12,
        "lbm/in3": 27679.9047,
        "lb/in3": 27679.9047,
        "lbm/ft3": 16.0184634,
    },  # fmt: skip
    "stress": {
        "Pa": 1e-6,
        "kPa": 1e-3,
        "MPa": 1.0,
        "GPa": 1e3,
        "N/m2": 1e-6,
        "N/mm2": 1.0,
        "mN/mm2": 1e-3,
        "kN/mm2": 1e3,
        "psi": _PSI,
        "lbf/in2": _PSI,
        "ksi": 1e3 * _PSI,
    },  # fmt: skip
    "ratio": {"": 1.0},
    "expansion": {"1/degC": 1.0, "1/K": 1.0, "1/degF": 1.8, "1/degR": 1.8},
    "conductivity": {
        "uW/(mm*degC)": 1e-3,
        "uW/(mm*K)": 1e-3,
        "mW/(mm*degC)": 1.0,
        "mW/(mm*K)": 1.0,
        "W/(m*degC)": 1.0,
        "W/(m*K)": 1.0,
        "W/(mm*degC)": 1e3,
        "W/(mm*K)": 1e3,
        "BTU/(hr*ft*degF)": 1.730735,
    },  # fmt: skip
    "specific_heat": {
        "uJ/(kg*K)": 1e-6,
        "uJ/(kg*degC)": 1e-6,
        "mJ/(kg*K)": 1e-3,
        "mJ/(kg*degC)": 1e-3,
        "J/(kg*K)": 1.0,
        "J/(kg*degC)": 1.0,
        "kJ/(kg*K)": 1e3,
        "kJ/(kg*degC)": 1e3,
        "BTU/(lbm*degF)": 4186.8,
    },  # fmt: skip
}
ROOM = {"degC": 20.0, "K": 293.15, "degF": 68.0}


def _ascii_unit(text: str) -> str:
    """NX unit text in ASCII: ``µW/(mm·°C)`` -> ``uW/(mm*degC)``."""
    for old, new in (("\xb3", "3"), ("\xb2", "2"), ("\xb0", "deg"), ("\xba", "deg"),
                     ("\xb5", "u"), ("μ", "u"), ("\xb7", "*"), ("⋅", "*")):  # fmt: skip
        text = text.replace(old, new)
    return "".join(text.split())


def parse_property(value: str, dim: str) -> float | None:
    """One NX library property string in the reported unit, or ``None`` when unreadable."""
    text = str(value or "").strip()
    if text.startswith("Tabular Data"):
        lines = [line.split() for line in text.splitlines()[1:] if line.strip()]
        if len(lines) < 3 or len(lines[1]) < 2:
            return None
        temperature_unit, unit = _ascii_unit(lines[1][0]), _ascii_unit(" ".join(lines[1][1:]))
        rows = [
            (common.float_or_none(r[0]), common.float_or_none(r[1]))
            for r in lines[2:]
            if len(r) >= 2
        ]
        rows = [(t, v) for t, v in rows if t is not None and v is not None]
        if not rows:
            return None
        room = ROOM.get(temperature_unit, 20.0)
        number = min(rows, key=lambda row: abs(row[0] - room))[1]
    else:
        head, _, rest = text.partition(" ")
        number, unit = common.float_or_none(head), _ascii_unit(rest)
    factor = UNITS[dim].get(unit)
    if number is None or factor is None:
        return None
    return number * factor


class LibraryProperties(TypedDict):
    density: float | None
    youngs_modulus: float | None
    poissons_ratio: float | None
    thermal_expansion: float | None
    thermal_conductivity: float | None
    specific_heat: float | None


def _library_value(materials, library, name, neutral):
    try:
        return str(materials.GetMaterialPropertyValueAndDisplayName(library, name, neutral)[0])
    except Exception:  # "The material property is missing."
        return None


def library_properties(materials, library: str, name: str) -> tuple[str | None, dict]:
    """``(category, {field: value})`` of one library material."""
    category = _library_value(materials, library, name, "Category")
    values: dict = {}
    for field, (neutrals, dim) in PROPERTIES.items():
        values[field] = None
        for neutral in neutrals:
            raw = _library_value(materials, library, name, neutral)
            if raw is not None:
                values[field] = parse_property(raw, dim)
                break
    return (category.strip() or None) if category else None, values


def library_names(tx, library: str = DEFAULT_LIBRARY) -> list[str]:
    """Material names of the NX material library; an unreadable or empty one is reported."""
    materials = tx.part.MaterialManager.PhysicalMaterials
    try:
        with native(default="NX_CATALOG_UNAVAILABLE"):
            names = [str(n) for n in materials.GetMaterialsFromLibrary(library)]
    except NXError as error:
        if error.code == "NX_LICENSE_UNAVAILABLE":
            raise
        fail(
            "NX_CATALOG_UNAVAILABLE",
            f"The material library {library or '(NX default)'} cannot be read: {error.message}",
            nx_code=error.nx_code if isinstance(error.nx_code, int) else None,
            hint="Check the material library customer defaults",
        )
    if not names:
        fail(
            "NX_CATALOG_UNAVAILABLE",
            f"The material library {library or '(NX default)'} has no materials",
            hint="Check the material library customer defaults",
        )
    return names


class LibraryRow(LibraryProperties):
    name: str
    category: str | None


class LibraryData(TypedDict):
    items: list[LibraryRow]
    page: dict


@tool(
    effect="read",
    label="List library materials",
    summary=lambda a, say: (
        "Library materials" + (f" matching *{a['query']}*" if a.get("query") else "")
    ),
    keywords=(
        "material library",
        "materials",
        "steel",
        "aluminium",
        "young's modulus",
        "poisson",
        "thermal conductivity",
        "matml",
    ),  # fmt: skip
    requires=("work_part",),
    cost="light",
    example={"query": "aluminum"},
)
def nx_list_materials(
    tx: Tx,
    *,
    query: Annotated[str, Text(64), Doc("Case-insensitive name or category filter")] | None = None,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> LibraryData:
    """List NX material library entries with category, density and mechanical and thermal data.
    Units: density kg/m^3, Young's modulus MPa, thermal expansion 1/K, conductivity W/(m*K),
    specific heat J/(kg*K); missing values are null. Assign one with nx_set_material
    {library: name}."""
    source = DEFAULT_LIBRARY
    materials = tx.part.MaterialManager.PhysicalMaterials
    found = []
    for name in library_names(tx, source):
        tx.checkpoint()
        category = _library_value(materials, source, name, "Category")
        category = (category.strip() or None) if category else None
        if query and not any(
            query.casefold() in str(text).casefold() for text in (name, category or "")
        ):
            continue
        found.append((name, category))
    selected, page = result.page(found, offset, limit)
    items: list = []
    for name, category in selected:
        tx.checkpoint()
        _, values = library_properties(materials, source, name)
        items.append({"name": name, "category": category, **values})
    return {"items": items, "page": page}


class MaterialRow(TypedDict):
    body: result.Row
    material: str | None
    library: Annotated[str | None, Doc("Library file of a library material, else null")]
    category: str | None
    density: float
    youngs_modulus: float | None
    poissons_ratio: float | None
    thermal_expansion: float | None
    thermal_conductivity: float | None
    specific_heat: float | None


class MaterialData(TypedDict):
    items: list[MaterialRow]


def _density(tx, body):
    units = tx.nx.UF.Modl.DensityUnits.KILOGRAMS_METERS
    with native():
        return float(tx.uf.Modeling.AskBodyDensity(body.Tag, units))


def _material_of(prototype):
    materials = prototype.OwningPart.MaterialManager.PhysicalMaterials
    with native():
        return materials.AskMaterialOfObject(prototype)


def _library_of(material) -> str | None:
    """The library a loaded library material came from (``""`` = NX's), else ``None``."""
    try:
        if not material.IsLoadedLibraryMaterial():
            return None
        return str(material.GetLibraryName() or "")
    except Exception:  # an older or Teamcenter material
        return None


def describe(material, memo: dict) -> dict:
    """Name, library, category and library properties of an assigned material (memoised)."""
    if material is None:
        return {"material": None, "library": None, "category": None,
                **dict.fromkeys(PROPERTIES)}  # fmt: skip
    key = id(material)
    if key not in memo:
        name, library = str(material.Name), _library_of(material)
        category, values = None, dict.fromkeys(PROPERTIES)
        if library is not None:
            materials = material.OwningPart.MaterialManager.PhysicalMaterials
            category, values = library_properties(materials, library, name)
        else:
            try:
                category = str(material.GetMaterialCategory() or "") or None
            except Exception:
                category = None
        memo[key] = {
            "material": name,
            "library": None if library is None else ntpath.basename(library) or "NX default",
            "category": category,
            **values,
        }
    return memo[key]


def _row(tx, body, material, memo, density=None):
    info = describe(material, memo)
    row = {"body": tx.ref(body, kind="body"), **info}
    row["density"] = _density(tx, body) if density is None else density
    return row


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
    keywords=(
        "material",
        "density",
        "mass properties",
        "physical material",
        "material info",
        "young's modulus",
        "poisson",
    ),  # fmt: skip
    requires=("work_part",),
    cost="light",
    example={"scope": "part"},
)
def nx_get_material(
    tx: Tx,
    *,
    bodies: Annotated[list[BodyRef], Items(1, 200)] | None = None,
    scope: Annotated[
        Literal["part", "assembly"], Doc("Without bodies: work part or components")
    ] = "part",
) -> MaterialData:
    """Read the materials of bodies, the work part or the assembly with density and properties.
    Density is kg/m^3; library materials add E, Poisson's ratio and thermal data (units as
    nx_list_materials). Components report their prototype's material."""
    if bodies is not None:
        found = [tx.resolve(ref, "body") for ref in bodies]
    elif scope == "assembly":
        found = _assembly_bodies(tx)
    else:
        found = list(tx.part.Bodies)
    items: list = []
    memo: dict = {}
    for body in found:
        tx.checkpoint()
        prototype = body.Prototype if getattr(body, "IsOccurrence", False) else body
        row = _row(tx, prototype, _material_of(prototype), memo)
        row["body"] = tx.ref(body, kind="body")
        items.append(row)
    return {"items": items}


class Custom(TypedDict):
    name: Annotated[str, Text(100, r"^\S.*$"), Doc("New material name")]
    density: Annotated[float, Range(gt=0, le=1e6), Doc("Mass density in kg/m^3")]


class CustomSpec(TypedDict):
    custom: Custom


class LibrarySpec(TypedDict):
    library: Annotated[
        str, Text(128, r"^\S.*$"), Doc("Library material name from nx_list_materials")
    ]


MaterialSpec = CustomSpec | LibrarySpec

forbid_extra(Custom, CustomSpec, LibrarySpec)


def _material_text(spec) -> str:
    return spec["custom"]["name"] if "custom" in spec else spec["library"]


def _stored_density(tx, material) -> float | None:
    """The kg/m^3 a part material stores (its property table), None when unreadable."""
    try:
        value, unit = material.GetPropTable().GetBaseScalarWithDataPropertyValue("MassDensity")
        collection = tx.part.UnitCollection
        kg = collection.FindObject("KilogramPerCubicMeter")
        if unit is not None and int(unit.Tag) != int(kg.Tag):
            value = collection.Convert(unit, kg, value)
        return float(value)
    except Exception:  # an older or Teamcenter material: read back after assigning
        return None


def _clash(material, density):
    fail(
        "NX_ALREADY_EXISTS",
        f"Material {material.Name} exists with density {density:g} kg/m^3",
        field="/material/custom/name",
        hint="Use another name, or the existing density",
    )


def _custom(tx, spec, targets):
    part = tx.part
    materials = part.MaterialManager.PhysicalMaterials
    with tx.checks():  # a clashing name is refused before anything is assigned
        existing = [m for m in materials if str(m.Name).casefold() == spec["name"].casefold()]
        stored = _stored_density(tx, existing[0]) if existing else None
        if stored is not None and abs(stored - spec["density"]) > DENSITY_REL * spec["density"]:
            _clash(existing[0], stored)
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
    items, memo = [], {}
    for i, body in enumerate(targets):
        density = _density(tx, body)
        if existing and abs(density - spec["density"]) > DENSITY_REL * spec["density"]:
            _clash(chosen, density)  # unreadable before: assigned, then rolled back
        readback.expect_close(
            density, spec["density"], f"density[{i}]", tol=DENSITY_REL * spec["density"] + 1e-9
        )
        items.append(_row(tx, body, chosen, memo, density))
    return items


def _library(tx, spec, targets):
    source = DEFAULT_LIBRARY
    field = "/material/library"
    names = library_names(tx, source)
    wanted = spec["library"].casefold()
    match = [n for n in names if n.casefold() == wanted]
    if not match:
        fail(
            "NX_NOT_FOUND",
            f"No material {spec['library']!r} in the library {source or '(NX default)'}",
            field=field,
            hint="Pick a name from nx_list_materials",
            details={"candidates": difflib.get_close_matches(spec["library"], names, 5, 0.4)},
        )
    name = match[0]
    materials = tx.part.MaterialManager.PhysicalMaterials
    clash = [
        m for m in materials if str(m.Name).casefold() == name.casefold() and _library_of(m) is None
    ]
    if clash:
        fail(
            "NX_ALREADY_EXISTS",
            f"The part has a custom material named {clash[0].Name}",
            field=field,
            hint="Assign the custom material with {custom: ...}, or rename it",
        )
    with native(NATIVE):
        loaded = materials.GetLoadedLibraryMaterial(source, name)  # None until loaded
        if loaded is None:
            loaded = materials.LoadFromNxmatmllibrary(name)
    with native():
        loaded.AssignObjects(targets)
    memo: dict = {}
    wanted_density = describe(loaded, memo)["density"]
    items = []
    for i, body in enumerate(targets):
        density = _density(tx, body)
        if wanted_density is not None:
            readback.expect_close(
                density, wanted_density, f"density[{i}]", tol=LIBRARY_DENSITY_REL * wanted_density
            )
        items.append(_row(tx, body, loaded, memo, density))
    return items


@tool(
    effect="model",
    label="Set material",
    summary=lambda a, say: (
        f"Material {_material_text(a['material'])} on "
        + (common.bodies_text(a) if a.get("bodies") else "all solid bodies")
    ),
    keywords=(
        "assign material",
        "density",
        "material",
        "physical material",
        "mass",
        "material library",
        "steel",
        "aluminium",
    ),  # fmt: skip
    requires=("work_part", "no_active_sketch"),
    cost="light",
    invalidates="deleted",  # materials only: face and edge references stay valid
    example={"bodies": ["b:3"], "material": {"library": "Aluminum_6061"}},
)
def nx_set_material(
    tx: Tx,
    *,
    bodies: Annotated[list[BodyRef], Items(1, 200), Doc("Omit for every solid body")] | None = None,
    material: MaterialSpec,
) -> MaterialData:
    """Assign an NX library material (names from nx_list_materials) or a custom density material.
    Targets solid bodies of the work part, default all; mass uses its density; the result reads
    it back. A custom name is reused only with the same density."""
    if bodies is not None:
        targets = common.solids(tx, bodies)
    else:
        targets = [b for b in tx.part.Bodies if getattr(b, "IsSolidBody", True)]
        if not targets:
            fail("NX_NOT_FOUND", "The work part has no solid bodies", field="/bodies")
    if "custom" in material:
        items = _custom(tx, material["custom"], targets)  # type: ignore[typeddict-item]  # `in` narrows only @final TypedDicts
    else:
        items = _library(tx, material, targets)
    return {"items": items}
