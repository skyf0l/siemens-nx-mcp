"""Visual materials and true shading: the studio look NX stores in the part (tool-surface 3.12).

Visual materials come from NX's installed Siemens Visual Material library,
``<UGII_BASE_DIR>/UGPHOTO/SVM/Materials/<category>/<name>.svm`` (NX 2406: about 1,200
materials in 25 categories such as Paints, Plastics, Glasses, Woods, Metals).
``VisualMaterialCollection.CreateMaterial`` copies one into the part by its exact name
(NX 2406 measured: any other spelling fails with an unrelated native error, and a name
already in the part returns that material), so a name is matched to the part's and the
library's names first, in any case and spacing. ``AssignMaterial`` puts it on bodies or
faces; UF ``Disp.AskMaterial`` reads an object's own material back. A face without its
own material shows its body's (``from_body``).

Visual materials and true shading are part data, saved with the part and shown in the
Studio rendering style (``nx_capture_view(style='studio')``) while true shading is off; a
shaded view keeps showing object colours. They are model changes under the kernel's undo
mark: a failure rolls back and ``nx_undo_operation`` reverts a success (NX 2406 measured:
undo removes the assignment and the copied material, and reverts true-shading settings
and the Studio style a commit switched on).

True shading (``part.SHEDObjs``, ``TrueShadingBuilder``) configures the Studio style:
global material for objects without a visual material, background, floor shadow and
reflection, reflection environment. NX 2406 measured: the builder commits no object
(``Commit`` returns None) and its settings are read back from a new builder; committing
with ``ShedModeToggle`` on switches the work view to the Studio style, and
``IsTrueShadeEnabled`` turns true; setting another style turns it false, and setting the
Studio style again by itself does not turn it back on (plain Studio). Committing with the
toggle off keeps the style. ``enabled`` is reported when both hold (true shading on, view in
Studio style). ``enabled=false`` stores the settings and leaves the Studio style: NX 2406
measured, the commit with the toggle off already returns the view to the style it had
before true shading; where it stays in Studio the tool sets shaded.

NX 2406 measured (captures): a viewport capture (``nx_capture_view`` quality=viewport) in
the plain Studio style shows visual materials and decals, a shaded one shows neither. True
shading is its own look: its global material on every object, floor grid, shadow and
background, without visual materials or decals (viewport and Studio-quality captures
alike), so a capture of materials and decals needs true shading off.

NXOpen documents no licence for these calls; the ray-traced and Iray studio builders are
never used.
"""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Offset, PageLimit, RefTo, Text
from nx_mcp.domains.display.common import kind_of, targets
from nx_mcp.kit import display, readback, result
from nx_mcp.kit.result import Row

LIBRARY_DIRS = ("UGPHOTO", "SVM", "Materials")
MaterialName = Annotated[
    str, Text(96), Doc("Library or part visual material name, any case: 'Clear Glass'")
]
VisualTarget = Annotated[str, RefTo("body", "face", "feature")]
VisualTargets = Annotated[list[VisualTarget], Items(1, 200)]
# NX 2406 measured: a decal is stored as a visual material of Type 1; library ones are 0.
DECAL_TYPE = 1
_LIBRARIES: dict[str, list[tuple[str, str]]] = {}


class MaterialRow(TypedDict, total=False):
    object: Row
    material: str | None
    from_body: bool


class VisualMaterialData(TypedDict):
    material: str | None
    category: str | None
    objects: list[MaterialRow]
    count: int


class VisualMaterialInfo(TypedDict):
    objects: list[MaterialRow]
    count: int


class LibraryEntry(TypedDict):
    name: str
    category: str


class PartMaterial(TypedDict):
    name: str
    kind: Literal["material", "decal"]
    objects: int


class VisualLibraryData(TypedDict, total=False):
    items: list[LibraryEntry]
    categories: list[str]
    in_part: list[PartMaterial]
    page: dict


# ---- the installed library -----------------------------------------------------------------


def library() -> list[tuple[str, str]]:
    """``[(category, name)]`` of the installed visual material library (cached per root);
    ``[]`` when NX's installation root or the library is missing."""
    base = os.environ.get("UGII_BASE_DIR")
    if not base:
        return []
    root = Path(base).joinpath(*LIBRARY_DIRS)
    key = str(root)
    if key not in _LIBRARIES:
        found = root.glob("*/*.svm") if root.is_dir() else []
        _LIBRARIES[key] = sorted((p.parent.name, p.stem) for p in found)
    return _LIBRARIES[key]


def _material_words(text) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text).casefold())


def _material_key(text) -> str:
    """A material name without case, spaces or punctuation."""
    return "".join(_material_words(text))


def part_materials(tx, part=None) -> list[tuple[Any, str]]:
    """``[(material, name)]`` of the visual materials (and decals) stored in the part."""
    part = part or tx.part
    with native(default="NX_ERROR"):
        found, names = part.VisualMaterialCollection.GetMaterialsInPart()
    return list(zip(list(found), [str(n) for n in names], strict=False))


def is_decal(material) -> bool:
    """Whether a part's visual material is a decal sticker (NX 2406: ``Type`` 1)."""
    return result.enum_int(material.Type) == DECAL_TYPE


def resolve_material(tx, text, field="/material") -> tuple[str, str | None]:
    """``(exact name, category or None)`` of a material named loosely: the part's own
    materials first, then the library. Otherwise ``NX_NOT_FOUND`` with close names."""
    key = _material_key(text)
    for material, name in part_materials(tx):
        if _material_key(name) == key and not is_decal(material):
            return name, next((c for c, n in library() if n == name), None)
    entries = library()
    for category, name in entries:
        if _material_key(name) == key:
            return name, category
    if not entries:
        fail(
            "NX_CATALOG_UNAVAILABLE",
            "NX's visual material library was not found (UGII_BASE_DIR/UGPHOTO/SVM/Materials)",
            field=field,
        )
    lookup = {_material_key(n): n for _, n in entries}
    close = [lookup[k] for k in difflib.get_close_matches(key, list(lookup), 5, 0.6)]
    fail(
        "NX_NOT_FOUND",
        f"No visual material named {text!r}",
        field=field,
        hint="List names with nx_list_visual_materials(query=...)",
        details={"close": close},
    )


def own_material(tx, obj) -> str | None:
    """The visual material assigned to the object itself (None when it has none)."""
    with native(default="NX_ERROR"):
        tag, name = tx.uf.Disp.AskMaterial(obj.Tag)
    return str(name) if tag and name else None


def material_rows(tx, values) -> list[MaterialRow]:
    """One row per object: its own material; a face without one shows its body's."""
    rows: list[MaterialRow] = []
    for obj in values:
        kind = kind_of(tx, obj)
        row: MaterialRow = {"object": tx.ref(obj, kind), "material": own_material(tx, obj)}
        if kind == "face":
            row["from_body"] = False
            if row["material"] is None and hasattr(obj, "GetBody"):
                body = obj.GetBody()
                inherited = own_material(tx, body) if body is not None else None
                if inherited is not None:
                    row["material"], row["from_body"] = inherited, True
        rows.append(row)
    return rows


def _visual_targets(tx, refs) -> list:
    values = targets(tx, refs, expand=True)
    for i, obj in enumerate(values):
        if kind_of(tx, obj) not in ("body", "face"):
            fail("NX_WRONG_KIND", "Visual materials go on bodies and faces", field="/objects")
        if getattr(obj, "IsOccurrence", False):
            fail(
                "NX_WRONG_PART_CONTEXT",
                "Assign visual materials in the part that owns the body",
                field=f"/objects/{i}",
                hint="Make the component's part the work part (nx_activate_part) and use its refs",
            )
    return values


# ---- tools -------------------------------------------------------------------------------------


@tool(
    target="any",
    effect="read",
    label="List visual materials",
    summary=lambda a, say: (
        "Visual materials" + (f" matching *{a['query']}*" if a.get("query") else "")
    ),
    keywords=(
        "visual material",
        "render material",
        "studio material",
        "paint",
        "glass",
        "wood",
        "material look",
        "svm",
    ),  # fmt: skip
    requires=("work_part",),
    cost="light",
    example={"query": "glass"},
)
def nx_list_visual_materials(
    tx: Tx,
    *,
    query: Annotated[str, Text(64), Doc("Words in the name or category: 'red paint', 'teak'")]
    | None = None,
    offset: Offset = 0,
    limit: PageLimit = 20,
) -> VisualLibraryData:
    """List NX's library of visual (render) materials by name and category, plus those in the
    work part (in_part, decals too). Query words ignore case; best names first. Not physical
    materials (nx_list_materials). Apply with nx_set_visual_material."""
    words, key = _material_words(query or ""), _material_key(query or "")
    ranked = []
    for index, (category, name) in enumerate(library()):
        own = _material_key(name)
        if words:
            where = own + _material_key(category)
            if not all(w in where for w in words):
                continue
            order = 0 if own == key else 1 if own.startswith(key) else 2
            order += 0 if all(w in own for w in words) else 3
            ranked.append((order, index, name, category))
        else:
            ranked.append((0, index, name, category))
    found = [{"name": n, "category": c} for _, _, n, c in sorted(ranked)]
    if words and not found:
        tx.warn("NX_NOTE", f"No library visual material matches {query!r}; omit query to page")
    selected, page = result.page(found, offset, limit)
    out: VisualLibraryData = {"items": selected, "page": page}
    if offset == 0:
        out["categories"] = sorted({c for c, _ in library()})
        in_part: list[PartMaterial] = []
        for material, name in part_materials(tx):
            with native(default="NX_ERROR"):
                count = int(tx.uf.Disp.AskGeometryOfMaterial(material.Tag)[0])
            kind = "decal" if is_decal(material) else "material"
            in_part.append({"name": name, "kind": kind, "objects": count})  # type: ignore[typeddict-item]
        out["in_part"] = in_part
    return out


def _set_summary(a, say):
    if a.get("remove"):
        return f"Remove the visual material of {say('objects')}"
    return f"Visual material {a.get('material')} on {say('objects')}"


@tool(
    effect="model",
    label="Set visual material",
    summary=_set_summary,
    keywords=(
        "visual material",
        "render material",
        "studio look",
        "paint",
        "glass",
        "wood",
        "remove material look",
        "apply texture",
    ),  # fmt: skip
    requires=("work_part",),
    cost="light",
    invalidates="deleted",  # display data only: every reference stays valid
    example={"objects": ["b:3"], "material": "Clear Glass"},
)
def nx_set_visual_material(
    tx: Tx,
    *,
    objects: VisualTargets,
    material: MaterialName | None = None,
    remove: bool = False,
) -> VisualMaterialData:
    """Apply a visual (render) material from NX's library to bodies or faces, or remove=true to
    clear theirs. A face's own material wins over its body's. Seen in the Studio style with
    true shading off (nx_capture_view style=studio); saved with the part. Not density:
    nx_set_material. Names: nx_list_visual_materials."""
    if remove == (material is not None):
        fail("NX_INVALID_ARGUMENT", "Give material, or remove=true without material",
             field="/material")  # fmt: skip
    collection = tx.part.VisualMaterialCollection
    with tx.checks():
        values = _visual_targets(tx, objects)
        name, category = resolve_material(tx, material) if material is not None else (None, None)
    if name is None:
        with native(default="NX_ERROR"):
            collection.RemoveMaterial(values)
    else:
        with native(default="NX_BUILD_FAILED"):
            created = collection.CreateMaterial(name)[0]
            collection.AssignMaterial(created, values)
    for obj in values:
        got = own_material(tx, obj)
        readback.expect(got == name, "visual_material", tag=int(obj.Tag), material=got)
    rows = material_rows(tx, values)
    return {"material": name, "category": category, "objects": rows, "count": len(rows)}


@tool(
    effect="read",
    label="Get visual material",
    summary="Read the visual material of {objects}",
    keywords=(
        "visual material",
        "render material",
        "which material look",
        "assigned paint",
        "studio material",
    ),  # fmt: skip
    requires=("work_part",),
    cost="light",
    example={"objects": ["b:3"]},
)
def nx_get_visual_material(tx: Tx, *, objects: VisualTargets) -> VisualMaterialInfo:
    """Read the visual material of bodies and faces; bodies also list their faces that carry
    their own. A face without its own shows its body's (from_body=true); null: none."""
    values = targets(tx, objects, expand=True)
    rows: list[MaterialRow] = []
    for obj in values:
        if kind_of(tx, obj) not in ("body", "face"):
            fail("NX_WRONG_KIND", "Visual materials go on bodies and faces", field="/objects")
        rows.extend(material_rows(tx, [obj]))
        if kind_of(tx, obj) == "body":
            own = [f for f in obj.GetFaces() if own_material(tx, f) is not None]
            rows.extend(material_rows(tx, own))
    return {"objects": rows, "count": len(rows)}


# ---- true shading -------------------------------------------------------------------------------

GLOBAL_MATERIALS = {
    "shiny_metal_wash": "GlobalWashShinyMetal",
    "brushed_metal_wash": "GlobalWashBrushedMetal",
    "shiny_plastic_wash": "GlobalWashShinyPlastic",
    "analysis_wash": "GlobalWashAnalysis",
    "low_sheen_plastic_wash": "GlobalWashFlat",
    "red_glossy_plastic": "GlobalRedGlossyPlastic",
    "blue_glossy_plastic": "GlobalBlueGlossyPlastic",
    "green_glossy_plastic": "GlobalGreenGlossyPlastic",
    "gray_glossy_plastic": "GlobalGrayGlossyPlastic",
    "black_glossy_plastic": "GlobalBlackGlossyPlastic",
    "brown_glossy_plastic": "GlobalBrownGlossyPlastic",
    "yellow_glossy_plastic": "GlobalYellowGlossyPlastic",
    "teal_glossy_plastic": "GlobalTealGlossyPlastic",
    "white_glossy_plastic": "GlobalWhiteGlossyPlastic",
    "clear_plastic": "GlobalClearPlastic",
    "chrome": "GlobalChrome",
    "copper": "GlobalCopper",
    "gold": "GlobalGold",
    "brass": "GlobalBrass",
    "steel": "GlobalSteel",
    "brushed_chrome": "GlobalBrushedChrome",
    "brushed_aluminum": "GlobalBrushedAluminum",
    "brushed_titanium": "GlobalBrushedTitanium",
    "clear_glass": "GlobalGlassClear",
    "smokey_glass": "GlobalGlassSmokey",
    "red_metallic_paint": "GlobalMetallicPaintRed",
    "gray_metallic_paint": "GlobalMetallicPaintGray",
    "black_metallic_paint": "GlobalMetallicPaintBlack",
    "blue_metallic_paint": "GlobalMetallicPaintBlue",
    "black_rubber": "GlobalRubber",
}
BACKGROUNDS = {
    "shaded_view": "InheritShadedBackground",
    "white": "White",
    "pure_white": "PureWhite",
    "black": "Black",
    "light_graduated": "LightGraduated",
    "dark_graduated": "DarkGraduated",
}
ENVIRONMENTS = {
    "default": "Default",
    "shiny_metal_1": "MetalShiny1",
    "shiny_metal_2": "MetalShiny2",
    "brushed_metal_1": "MetalBrushed1",
    "brushed_metal_2": "MetalBrushed2",
    "glossy_1": "Glossy1",
    "glossy_2": "Glossy2",
    "photo_studio": "AutoPhotoStudio",
}
Material = Literal[
    "shiny_metal_wash",
    "brushed_metal_wash",
    "shiny_plastic_wash",
    "analysis_wash",
    "low_sheen_plastic_wash",
    "red_glossy_plastic",
    "blue_glossy_plastic",
    "green_glossy_plastic",
    "gray_glossy_plastic",
    "black_glossy_plastic",
    "brown_glossy_plastic",
    "yellow_glossy_plastic",
    "teal_glossy_plastic",
    "white_glossy_plastic",
    "clear_plastic",
    "chrome",
    "copper",
    "gold",
    "brass",
    "steel",
    "brushed_chrome",
    "brushed_aluminum",
    "brushed_titanium",
    "clear_glass",
    "smokey_glass",
    "red_metallic_paint",
    "gray_metallic_paint",
    "black_metallic_paint",
    "blue_metallic_paint",
    "black_rubber",
]
Background = Literal[
    "shaded_view", "white", "pure_white", "black", "light_graduated", "dark_graduated"
]
Environment = Literal[
    "default",
    "shiny_metal_1",
    "shiny_metal_2",
    "brushed_metal_1",
    "brushed_metal_2",
    "glossy_1",
    "glossy_2",
    "photo_studio",
]


class TrueShadingData(TypedDict, total=False):
    enabled: bool
    style: str
    material: str | None
    background: str | None
    shadows: bool
    reflections: bool
    environment: str | None
    marks_part_modified: bool


def _named(enum, names: dict, value) -> str | None:
    members = {ours: getattr(enum, nx, None) for ours, nx in names.items()}
    by_value = {result.enum_int(m): ours for ours, m in members.items() if m is not None}
    return by_value.get(result.enum_int(value))


def _shading_state(tx, part, view) -> TrueShadingData:
    """The stored true-shading settings, read from a new builder; ``enabled`` when NX
    reports true shading on and the work view is in Studio style."""
    style = display.style_name(tx, view.RenderingStyle)
    builder_type = tx.nx.Display.TrueShadingBuilder
    collection = part.SHEDObjs
    with native(default="NX_ERROR"):
        b = collection.CreateTrueShadingBuilder(tx.nx.Display.TrueShading.Null)
    try:
        with native(default="NX_ERROR"):
            state: TrueShadingData = {
                "enabled": bool(collection.IsTrueShadeEnabled()) and style == "studio",
                "style": style,
                "material": _named(
                    builder_type.MaterialType, GLOBAL_MATERIALS, b.GlobalMaterialType
                ),  # fmt: skip
                "background": _named(builder_type.BgdType, BACKGROUNDS, b.BgdTypeEnum),
                "shadows": bool(b.PlanarShadowToggle),
                "reflections": bool(b.PlanarReflectionToggle),
                "environment": _named(
                    builder_type.EnvironmentMapType, ENVIRONMENTS, b.EnvironmentMapEnum
                ),  # fmt: skip
            }
    finally:
        b.Destroy()
    return state


@tool(
    effect="model",
    label="Set true shading",
    summary=lambda a, say: (
        "Turn true shading off" if a.get("enabled") is False else "Set up true shading"
    ),
    keywords=(
        "true shading",
        "studio style",
        "realistic display",
        "shadows",
        "reflections",
        "render look",
        "background",
    ),  # fmt: skip
    requires=("work_part",),
    cost="light",
    invalidates="deleted",  # display settings only: every reference stays valid
    example={"material": "blue_metallic_paint", "shadows": True},
)
def nx_set_true_shading(
    tx: Tx,
    *,
    enabled: bool = True,
    material: Material | None = None,
    background: Background | None = None,
    shadows: bool | None = None,
    reflections: bool | None = None,
    environment: Environment | None = None,
) -> TrueShadingData:
    """Turn on true shading (a Studio look: one global material, floor shadow and reflection)
    for the shown view and set it up. It hides visual materials and decals; enabled=false
    keeps the settings and leaves it. Omitted settings keep their value. Saved with the part;
    undo with nx_undo_operation."""
    builder_type = tx.nx.Display.TrueShadingBuilder
    with tx.checks():
        part = display.shown_part(tx, work=True)
        view = display.model_view(tx, part)
    was_modified = bool(part.IsModified)
    tx.display.keep_camera()  # a failure puts the view style back too
    with native(default="NX_BUILD_FAILED"):
        b = part.SHEDObjs.CreateTrueShadingBuilder(tx.nx.Display.TrueShading.Null)
    try:
        with native(default="NX_BUILD_FAILED"):
            b.ShedModeToggle = bool(enabled)
            if material is not None:
                b.GlobalMaterialType = getattr(
                    builder_type.MaterialType, GLOBAL_MATERIALS[material]
                )
            if background is not None:
                b.BgdTypeEnum = getattr(builder_type.BgdType, BACKGROUNDS[background])
            if shadows is not None:
                b.PlanarShadowToggle = bool(shadows)
            if reflections is not None:
                b.PlanarReflectionToggle = bool(reflections)
            if environment is not None:
                b.EnvironmentMapEnum = getattr(
                    builder_type.EnvironmentMapType, ENVIRONMENTS[environment]
                )
            b.Commit()
    finally:
        b.Destroy()
    if not enabled and display.style_name(tx, view.RenderingStyle) == "studio":
        display.set_style(tx, view, "shaded")
        view.UpdateDisplay()
    state = _shading_state(tx, part, view)
    wanted = {"material": material, "background": background, "shadows": shadows,
              "reflections": reflections, "environment": environment}  # fmt: skip
    for key, value in wanted.items():
        if value is not None:
            readback.expect(state[key] == value, key, actual=state[key])  # type: ignore[literal-required]
    readback.expect(state["enabled"] == bool(enabled), "enabled", actual=state["enabled"])
    state["marks_part_modified"] = not was_modified and bool(part.IsModified)
    return state
