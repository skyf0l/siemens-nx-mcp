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

import contextlib
import difflib
import ntpath
import re
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
# A custom density string's unit (ASCII, case-insensitive, ``^`` dropped) -> kg/m^3.
DENSITY_INPUT = {
    **{unit.casefold(): factor for unit, factor in UNITS["density"].items()},
    "g/cc": 1e3,
    "g/ml": 1e3,
    "kg/l": 1e3,
    "t/m3": 1e3,
    "g/m3": 1e-3,
    "lb/ft3": 16.0184634,
}  # fmt: skip
_DENSITY_TEXT = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(.*?)\s*$")
# British spellings agents send for library names spelled the American way.
SPELLINGS = {"aluminium": "aluminum", "grey": "gray", "sulphur": "sulfur", "fibre": "fiber"}


def _ascii_unit(text: str) -> str:
    """NX unit text in ASCII: ``µW/(mm·°C)`` -> ``uW/(mm*degC)``."""
    for old, new in (("\xb3", "3"), ("\xb2", "2"), ("\xb0", "deg"), ("\xba", "deg"),
                     ("\xb5", "u"), ("μ", "u"), ("\xb7", "*"), ("⋅", "*")):  # fmt: skip
        text = text.replace(old, new)
    return "".join(text.split())


def name_words(text: str) -> list[str]:
    """Lower-case alphanumeric words of a material name or query, American spelling:
    ``"Aluminium 6061"`` -> ``["aluminum", "6061"]``."""
    text = str(text).casefold()
    for british, american in SPELLINGS.items():
        text = text.replace(british, american)
    return re.findall(r"[a-z0-9]+", text)


def name_key(text: str) -> str:
    """A name without case, spaces or punctuation: ``"aluminum 6061"`` == ``"Aluminum_6061"``."""
    return "".join(name_words(text))


def density_value(value) -> float:
    """kg/m^3 of a custom density: a number (kg/m^3) or ``"<number> <unit>"``."""
    if not isinstance(value, str):
        return float(value)
    match = _DENSITY_TEXT.match(value)
    unit = _ascii_unit(match.group(2)).replace("^", "").casefold() if match else None
    factor = 1.0 if unit == "" else DENSITY_INPUT.get(unit or "")
    if match is None or factor is None:
        fail(
            "NX_INVALID_ARGUMENT",
            f"/material/custom/density: cannot read {value!r} as a density",
            field="/material/custom/density",
            hint="Send kg/m^3 as a number (1240) or a string like '1.24 g/cm3'; units: "
            + ", ".join(sorted({u for u in DENSITY_INPUT if "lbm" not in u})),
        )
    number = float(match.group(1)) * factor
    if not 0 < number <= 1e6:
        fail(
            "NX_INVALID_ARGUMENT",
            f"/material/custom/density: {value!r} is {number:g} kg/m^3; expected > 0 and <= 1e6",
            field="/material/custom/density",
        )
    return number


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


def _rank(own: str, key: str, words: list[str]) -> int:
    """Exact name first, then a name prefix, then every word in the name, then by category."""
    if own == key:
        return 0
    if own.startswith(key):
        return 1
    return 2 if all(w in own for w in words) else 3


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
    query: Annotated[str, Text(64), Doc("Name or category words, e.g. 'steel', 'abs plastic'")]
    | None = None,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> LibraryData:
    """List NX library materials with category, density, mechanical and thermal data (null
    when missing). Query words ignore case, '_' and British spelling; best names first.
    Units: density kg/m^3, E MPa, expansion 1/K, conductivity W/(m*K), specific heat
    J/(kg*K). Assign with nx_set_material."""
    source = DEFAULT_LIBRARY
    materials = tx.part.MaterialManager.PhysicalMaterials
    names = library_names(tx, source)
    words, key = name_words(query or ""), name_key(query or "")
    ranked = []
    for index, name in enumerate(names):
        tx.checkpoint()
        category = _library_value(materials, source, name, "Category")
        category = (category.strip() or None) if category else None
        if words:
            own, kind = name_key(name), name_key(category or "")
            if not all(w in own or w in kind for w in words):
                continue
            ranked.append((_rank(own, key, words), index, name, category))
        else:
            ranked.append((0, index, name, category))
    found = [(name, category) for _, _, name, category in sorted(ranked)]
    if words and not found:
        lookup = {name_key(n): n for n in names}
        close = difflib.get_close_matches(key, list(lookup), 5, 0.5)
        tx.warn(
            "NX_NOTE",
            f"No library material matches {query!r}"
            + (f"; close names: {', '.join(lookup[c] for c in close)}" if close else "")
            + ". Omit query to page through every name.",
        )
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
    Density kg/m^3; library materials add E, Poisson's ratio and thermal data (units as
    nx_list_materials). Components report their prototype's material."""
    if bodies is not None:
        found = [tx.resolve(ref, "body") for ref in bodies]
    elif scope == "assembly":
        found = _assembly_bodies(tx)
        if not found:
            root = tx.part.ComponentAssembly.RootComponent
            tx.warn(
                "NX_NOTE",
                "No loaded, unsuppressed component has a body"
                if root is not None
                else "The work part has no components; scope 'part' reads its own bodies",
            )
    else:
        found = list(tx.part.Bodies)
        if not found:
            tx.warn("NX_NOTE", "The work part has no bodies")
    items: list = []
    memo: dict = {}
    for body in found:
        tx.checkpoint()
        prototype = body.Prototype if getattr(body, "IsOccurrence", False) else body
        row = _row(tx, prototype, _material_of(prototype), memo)
        row["body"] = tx.ref(body, kind="body")
        items.append(row)
    bare = sum(row["material"] is None for row in items)
    if bare:
        tx.warn(
            "NX_NOTE",
            f"{bare} of {len(items)} bodies have no material: their density is NX's default "
            "solid density, not a material's; assign one with nx_set_material",
        )
    return {"items": items}


class Custom(TypedDict):
    name: Annotated[str, Text(100, r"^\S.*$"), Doc("New name, or a custom one to update")]
    density: Annotated[
        Annotated[float, Range(gt=0, le=1e6)] | Annotated[str, Text(40)],
        Doc("kg/m^3, or '1.24 g/cm3', '0.045 lb/in3'"),
    ]


class MaterialSpec(TypedDict, total=False):
    """Exactly one of ``library`` or ``custom`` (checked by the tool, which names both)."""

    library: Annotated[str, Text(128, r"^\S.*$"), Doc("Library name, e.g. 'Steel'")]
    custom: Custom


forbid_extra(Custom, MaterialSpec)

LibraryName = Annotated[str, Text(128), Doc("Short for {library: name}")]
FORMS_HINT = (
    "Send a library name ('Steel' or {library: 'Steel'}; names from nx_list_materials) or "
    "{custom: {name: 'PLA', density: 1240}} (kg/m^3, or '1.24 g/cm3')"
)


def _material_text(spec) -> str:
    if isinstance(spec, str):
        return spec
    if "custom" in spec:
        return str(spec["custom"].get("name", "custom"))
    return str(spec.get("library", "?"))


def _form(material) -> tuple[str, str, str]:
    """``(form, text, field)``: ``name`` (bare string), ``library`` or ``custom``."""
    if isinstance(material, str):
        return "name", material, "/material"
    given = [key for key in ("library", "custom") if key in material]
    if len(given) != 1:
        fail(
            "NX_INVALID_ARGUMENT",
            "/material: "
            + ("send library or custom, not both" if given else "library or custom is required"),
            field="/material",
            hint=FORMS_HINT,
        )
    if given == ["custom"]:
        return "custom", material["custom"]["name"], "/material/custom"
    return "library", material["library"], "/material/library"


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


def _users(tx, material, skip) -> list:
    """Solid bodies of the work part that use ``material``, except ``skip``."""
    tags = {int(b.Tag) for b in skip}
    out = []
    for body in tx.part.Bodies:
        if int(body.Tag) in tags or not getattr(body, "IsSolidBody", True):
            continue
        with contextlib.suppress(Exception):  # a body NX cannot answer for keeps its own
            used = _material_of(body)
            if used is material or (used is not None and int(used.Tag) == int(material.Tag)):
                out.append(body)
    return out


def _set_density(tx, material, wanted) -> None:
    """Set a custom material's MassDensity in place (NX applies it on the next assign)."""
    kg = tx.part.UnitCollection.FindObject("KilogramPerCubicMeter")
    try:
        with native():
            if hasattr(material, "GetMaterialEditable") and not material.GetMaterialEditable():
                material.SetMaterialEditable(True)
            table = material.GetPropTable()
            table.SetBaseScalarWithDataPropertyValue("MassDensity", float(wanted), kg)
    except NXError as error:
        fail(
            "NX_ALREADY_EXISTS",
            f"Material {material.Name} exists and NX cannot change its density",
            field="/material/custom/name",
            hint="Send another name for a new custom material, or the bare name to reuse it",
            nx_code=error.nx_code,
        )


def _update(tx, material, wanted, stored, targets) -> list:
    """Give an existing custom material the density ``wanted`` in place and reassign it to
    ``targets`` and its other users (NX applies a density on assign); returns the others."""
    others = _users(tx, material, targets)
    _set_density(tx, material, wanted)
    with native():
        material.AssignObjects([*targets, *others])
    was = "" if stored is None else f" (was {stored:g})"
    tx.warn(
        "NX_NOTE",
        f"Material {material.Name} now has density {wanted:g} kg/m^3{was}"
        + (f"; {len(others)} other bodies use it and changed too" if others else ""),
    )
    return others


def _custom(tx, name, wanted, targets):
    part = tx.part
    materials = part.MaterialManager.PhysicalMaterials
    with tx.checks():  # a library material of that name is refused before any change
        existing = [m for m in materials if str(m.Name).casefold() == name.casefold()]
        if existing and _library_of(existing[0]) is not None:
            fail(
                "NX_ALREADY_EXISTS",
                f"{existing[0].Name} is a library material: its density is the library's",
                field="/material/custom/name",
                hint="Give the custom material another name, or assign the library material",
            )
        stored = _stored_density(tx, existing[0]) if existing else None
    others: list = []
    if existing:
        chosen = existing[0]
        if stored is not None and abs(stored - wanted) > DENSITY_REL * wanted:
            others = _update(tx, chosen, wanted, stored, targets)
        else:
            with native():
                chosen.AssignObjects(targets)
            if any(abs(_density(tx, b) - wanted) > DENSITY_REL * wanted for b in targets):
                others = _update(tx, chosen, wanted, None, targets)  # density was unreadable
    else:
        kind = tx.nx.PhysicalMaterial.Type.Isotropic
        with builders.build(tx, materials.CreatePhysicalMaterialBuilder, kind) as b:
            b.Name = name
            b.PropertyTable.SetBaseScalarWithDataPropertyValue(
                "MassDensity",
                float(wanted),
                part.UnitCollection.FindObject("KilogramPerCubicMeter"),
            )
            chosen = b.commit()
        with native():
            chosen.AssignObjects(targets)
    for i, body in enumerate(others):
        density = _density(tx, body)
        readback.expect_close(
            density, wanted, f"other_density[{i}]", tol=DENSITY_REL * wanted + 1e-9
        )
    items, memo = [], {}
    for i, body in enumerate(targets):
        density = _density(tx, body)
        readback.expect_close(density, wanted, f"density[{i}]", tol=DENSITY_REL * wanted + 1e-9)
        items.append(_row(tx, body, chosen, memo, density))
    return items


def _reuse(tx, chosen, targets):
    """Assign a custom material the part already has (named by the bare-string form)."""
    stored = _stored_density(tx, chosen)
    with native():
        chosen.AssignObjects(targets)
    items, memo = [], {}
    for i, body in enumerate(targets):
        density = _density(tx, body)
        if stored is not None:
            readback.expect_close(density, stored, f"density[{i}]", tol=DENSITY_REL * stored + 1e-9)
        items.append(_row(tx, body, chosen, memo, density))
    return items


def library_match(names: list[str], text: str) -> tuple[str | None, list[str]]:
    """``(name, candidates)``: the library name ``text`` means, ignoring case, spaces,
    punctuation and British spelling (``"aluminium 6061"`` -> ``Aluminum_6061``); else
    ``None`` and the closest names (those holding every word first)."""
    exact = [n for n in names if n.casefold() == text.casefold()]
    if exact:
        return exact[0], []
    key, words = name_key(text), name_words(text)
    same = [n for n in names if name_key(n) == key]
    if len(same) == 1:
        return same[0], []
    holding = [n for n in names if words and all(w in name_key(n) for w in words)]
    lookup = {name_key(n): n for n in names}
    close = [lookup[k] for k in difflib.get_close_matches(key, list(lookup), 5, 0.4)]
    return None, list(dict.fromkeys(same + holding[:8] + close))[:8]


def _library(tx, text, field, targets, bare):
    source = DEFAULT_LIBRARY
    names = library_names(tx, source)
    materials = tx.part.MaterialManager.PhysicalMaterials
    name, candidates = library_match(names, text)
    if name is None:
        own = [m for m in materials if str(m.Name).casefold() == text.casefold()]
        if bare and own and _library_of(own[0]) is None:
            return _reuse(tx, own[0], targets)
        fail(
            "NX_NOT_FOUND",
            f"No material {text!r} in the library {source or '(NX default)'}"
            + (f"; did you mean {', '.join(candidates[:3])}?" if candidates else ""),
            field=field,
            hint="Send a candidate name exactly"
            if candidates
            else "Find names with nx_list_materials {query: 'steel'}, or send "
            "{custom: {name, density}}",
            details={"candidates": candidates},
        )
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
    example={"bodies": ["b:3"], "material": "Aluminum_6061"},
)
def nx_set_material(
    tx: Tx,
    *,
    bodies: Annotated[list[BodyRef], Items(1, 200), Doc("Omit for every solid body")] | None = None,
    material: LibraryName | MaterialSpec,
) -> MaterialData:
    """Assign a library or custom density material to solid bodies, default all; reads it back.
    'Steel' = {library: 'Steel'} (names from nx_list_materials). {custom: {name, density}} of an
    existing custom name sets its density (every body using it); a bare name reuses it."""
    form, text, field = _form(material)
    density = None
    if form == "custom":
        density = density_value(material["custom"]["density"])  # type: ignore[index]
    if bodies is not None:
        targets = common.solids(tx, bodies)
    else:
        targets = [b for b in tx.part.Bodies if getattr(b, "IsSolidBody", True)]
        if not targets:
            fail("NX_NOT_FOUND", "The work part has no solid bodies", field="/bodies")
    if density is not None:
        items = _custom(tx, text, density, targets)
    else:
        items = _library(tx, text, field, targets, bare=form == "name")
    return {"items": items}
