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
and the Studio style a commit switched on). In a shared session the compensating undo
(``@restorer``) assigns each object's previous material again (or removes it) and puts
the previous true-shading settings back.

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

NX 2406 measured: *opening* a ``TrueShadingBuilder`` switches true shading on (Studio style,
``IsTrueShadeEnabled`` true) and ``Destroy`` without a commit leaves it on, even when the
builder's toggle is set off first; the state then outlives display switches. So every
read of the settings (``shading_state``) takes the style and the enabled flag first and,
when true shading was off, puts the view's style back after the builder (setting another
style switches it off again).

NX 2406 measured (captures): a viewport capture (``nx_capture_view`` quality=viewport) in
the plain Studio style shows visual materials and decals, a shaded one shows neither. True
shading is its own look: its global material on every object, floor grid, shadow and
background, without visual materials or decals (viewport and Studio-quality captures
alike), so a capture of materials and decals needs true shading off.

NXOpen documents no licence for these calls; the ray-traced and Iray studio builders are
never used.
"""

from __future__ import annotations

import contextlib
import difflib
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from nx_mcp.core.catalogue import restorer, tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Offset, PageLimit, RefTo, Text
from nx_mcp.domains.display.common import kind_of, targets
from nx_mcp.kit import display, readback, result, select, switching
from nx_mcp.kit.result import Row

LIBRARY_DIRS = ("UGPHOTO", "SVM", "Materials")
MaterialName = Annotated[
    str, Text(96), Doc("Library or part visual material name, any case: 'Clear Glass'")
]
VisualTarget = Annotated[str, RefTo("body", "face", "feature")]
VisualTargets = Annotated[list[VisualTarget], Items(1, 200)]
# NX 2406 measured: a decal is stored as a visual material of Type 1; library ones are 0.
DECAL_TYPE = 1
WORD_MATCH = 0.75  # a query word matches a name word this close in spelling (difflib)
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


def _word_score(words: list[str], name: str) -> float:
    """How well query ``words`` match ``name``'s words, in any order: the mean of each
    word's best spelling ratio (difflib) against the name's words."""
    theirs = _material_words(name)
    if not words or not theirs:
        return 0.0
    best = [max(difflib.SequenceMatcher(None, w, t).ratio() for t in theirs) for w in words]
    return sum(best) / len(best)


def close_materials(text, names, n: int = 5) -> list[str]:
    """Names that look like ``text``: words in any order and close spellings ('Glas
    Clear' finds 'Clear Glass', 'aluminium' finds 'Aluminum ...'), best first."""
    words = _material_words(text)
    scored = sorted(
        ((round(_word_score(words, name), 6), -len(name), name) for name in names),
        reverse=True,
    )
    return [name for score, _length, name in scored[:n] if score >= WORD_MATCH]


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
    close = close_materials(text, [n for _, n in entries])
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


VISUAL_KINDS = ("body", "face", "feature")


def _visual_targets(tx, refs) -> list:
    values = targets(tx, refs, expand=True, kinds=VISUAL_KINDS)
    for i, obj in enumerate(values):
        if kind_of(tx, obj) not in ("body", "face"):
            fail("NX_WRONG_KIND", "Visual materials go on bodies and faces",
                 field=f"/objects/{i}")  # fmt: skip
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
    work part (in_part, decals too). Query words ignore case; best names first; with no name
    holding every word, the closest spellings. Not physical materials (nx_list_materials).
    Apply with nx_set_visual_material."""
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
        found = _near(words, library())
        if found:
            tx.warn("NX_NOTE", f"No library visual material has every word of {query!r}; "
                    "the closest names come first")  # fmt: skip
        else:
            tx.warn("NX_NOTE", f"No library visual material matches {query!r}; omit query "
                    "to page, or try one word or a category")  # fmt: skip
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


def _near(words, entries) -> list[dict]:
    """Library entries matching some query words in any spelling (best first): every
    word's best ratio against the name and category words, kept when one word matches."""
    scored = []
    for index, (category, name) in enumerate(entries):
        theirs = _material_words(name) + _material_words(category)
        best = [max(difflib.SequenceMatcher(None, w, t).ratio() for t in theirs) for w in words]
        hits = sum(1 for b in best if b >= WORD_MATCH)
        if hits:
            scored.append((-hits, -round(sum(best), 6), index, name, category))
    return [{"name": n, "category": c} for *_rank, n, c in sorted(scored)]


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
    true shading off (nx_capture_view style=studio; nx_get_true_shading); saved with the part.
    Not density: nx_set_material. Names: nx_list_visual_materials."""
    if remove == (material is not None):
        fail("NX_INVALID_ARGUMENT", "Give material, or remove=true without material",
             field="/material")  # fmt: skip
    collection = tx.part.VisualMaterialCollection
    with tx.checks():
        values = _visual_targets(tx, objects)
        name, category = resolve_material(tx, material) if material is not None else (None, None)
        before = [
            {"object": tx.ref(obj, kind_of(tx, obj))["id"], "material": own_material(tx, obj)}
            for obj in values
        ]
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
    tx.note_restore({"material": name, "before": before})
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
    values = targets(tx, objects, expand=True, kinds=VISUAL_KINDS)
    listed: dict[int, object] = {}  # one row per object: a face and its body list it once
    for i, obj in enumerate(values):
        if kind_of(tx, obj) not in ("body", "face"):
            fail("NX_WRONG_KIND", "Visual materials go on bodies and faces",
                 field=f"/objects/{i}")  # fmt: skip
        listed.setdefault(int(obj.Tag), obj)
        if kind_of(tx, obj) == "body":
            for face in obj.GetFaces():
                if own_material(tx, face) is not None:
                    listed.setdefault(int(face.Tag), face)
    rows = material_rows(tx, list(listed.values()))
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


def shading_state(tx, part, view) -> TrueShadingData:
    """The stored true-shading settings, read from a new builder; ``enabled`` when NX
    reports true shading on and the work view is in Studio style. Opening the builder
    switches true shading on (NX 2406): when it was off, the view's style is put back,
    which switches it off again."""
    original = view.RenderingStyle
    style = display.style_name(tx, original)
    builder_type = tx.nx.Display.TrueShadingBuilder
    collection = part.SHEDObjs
    with native(default="NX_ERROR"):
        was_on = bool(collection.IsTrueShadeEnabled())
        b = collection.CreateTrueShadingBuilder(tx.nx.Display.TrueShading.Null)
    try:
        with native(default="NX_ERROR"):
            state: TrueShadingData = {
                "enabled": was_on and style == "studio",
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
        if not was_on:
            _off_again(tx, collection, view, original)
    return state


def _off_again(tx, collection, view, original) -> None:
    """Switch true shading off again after a builder switched it on, by setting the view's
    own style back (NX 2406: another style turns it off; the same Studio style alone
    does not, so a view that was in plain Studio passes through shaded first)."""
    with native(default="NX_ERROR"):
        if not bool(collection.IsTrueShadeEnabled()):
            return
        if display.style_name(tx, original) == "studio":
            display.set_style(tx, view, "shaded")
        view.RenderingStyle = original
        view.UpdateDisplay()


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
    keeps the settings and leaves it. Omitted settings keep their value. Read without
    changing: nx_get_true_shading. Saved with the part; undo with nx_undo_operation."""
    with tx.checks():
        part = display.shown_part(tx, work=True)
        view = display.model_view(tx, part)
        before = shading_state(tx, part, view)
    was_modified = bool(part.IsModified)
    tx.display.keep_camera()  # a failure puts the view style back too
    state = _shade(tx, part, view, enabled, material=material, background=background,
                   shadows=shadows, reflections=reflections, environment=environment)  # fmt: skip
    tx.note_restore({"before": dict(before), "after": dict(state)})
    state["marks_part_modified"] = not was_modified and bool(part.IsModified)
    return state


def _shade(tx, part, view, enabled, **wanted) -> TrueShadingData:
    """Commit true shading on or off with the settings given (None: kept), then read the
    state back (``shading_state``) and check it."""
    builder_type = tx.nx.Display.TrueShadingBuilder
    material, background = wanted.get("material"), wanted.get("background")
    shadows, reflections = wanted.get("shadows"), wanted.get("reflections")
    environment = wanted.get("environment")
    original = view.RenderingStyle
    with native(default="NX_BUILD_FAILED"):
        was_on = bool(part.SHEDObjs.IsTrueShadeEnabled())
        b = part.SHEDObjs.CreateTrueShadingBuilder(tx.nx.Display.TrueShading.Null)
    committed = False
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
            committed = True
    finally:
        b.Destroy()
        if not committed and not was_on:  # the opened builder switched it on: off again
            with contextlib.suppress(Exception):
                _off_again(tx, part.SHEDObjs, view, original)
    if not enabled and display.style_name(tx, view.RenderingStyle) == "studio":
        display.set_style(tx, view, "shaded")
        view.UpdateDisplay()
    state = shading_state(tx, part, view)
    for key, value in wanted.items():
        if value is not None:
            readback.expect(state[key] == value, key, actual=state[key])  # type: ignore[literal-required]
    readback.expect(state["enabled"] == bool(enabled), "enabled", actual=state["enabled"])
    return state


def _shown_for_read(services, part, args) -> bool:
    """Gate 4s asks only for a target that is not displayed: true shading is read through
    its model view, so the part is shown for the read and the engineer's tab comes back."""
    return True


@tool(
    target="any",  # the displayed part, or the named or agent's only part
    display_bound=(_shown_for_read,),
    effect="read",
    label="Get true shading",
    summary="Read the true shading settings",
    keywords=(
        "true shading",
        "is true shading on",
        "studio settings",
        "render look",
        "global material",
    ),  # fmt: skip
    requires=("work_part",),
    cost="light",
    example={},
)
def nx_get_true_shading(tx: Tx) -> TrueShadingData:
    """Read whether true shading is on, without changing it. Also its global material,
    background, shadows, reflections and environment; part X: X is shown for the read. Set
    with nx_set_true_shading."""
    with tx.checks():
        part = display.shown_part(tx)
        view = display.model_view(tx, part)
    return shading_state(tx, part, view)


# ---- compensating undo (shared sessions) ---------------------------------------------------


@restorer("nx_set_visual_material")
def _restore_visual(tx, data: dict, *, apply: bool) -> str:
    """Give each object an ``nx_set_visual_material`` changed its previous material again
    (or none); refused when one carries another material since."""
    after = data.get("material")
    rows = []
    for row in data["before"]:
        obj = select.resolve(tx, row["object"], "body", "face", field="/objects")
        now = own_material(tx, obj)
        if now != after:
            fail("NX_DEPENDENCY_ORDER", f"{row['object']} carries {now!r} since, not {after!r}",
                 details={"object": row["object"], "material": now})  # fmt: skip
        rows.append((obj, row.get("material")))
    if apply:
        collection = tx.part.VisualMaterialCollection
        groups: dict = {}
        for obj, previous in rows:
            groups.setdefault(previous, []).append(obj)
        for previous, objs in groups.items():
            with native(default="NX_BUILD_FAILED"):
                if previous is None:
                    collection.RemoveMaterial(objs)
                else:
                    collection.AssignMaterial(collection.CreateMaterial(previous)[0], objs)
        for obj, previous in rows:
            got = own_material(tx, obj)
            readback.expect(got == previous, "visual_material", tag=int(obj.Tag), material=got)
    return f"the previous visual material of {len(rows)} objects"


@restorer("nx_set_true_shading")
def _restore_shading(tx, data: dict, *, apply: bool) -> str:
    """Put the true-shading settings an ``nx_set_true_shading`` changed back (on or off)."""
    before, after = data["before"], data["after"]
    part = tx.part
    switch = None
    if not display.same(tx.session.Parts.Display, part):
        switch = switching.Switch(tx, part, "display").enter()
    try:
        view = display.model_view(tx, part)
        now = shading_state(tx, part, view)
        keys = ("enabled", "material", "background", "shadows", "reflections", "environment")
        changed = [k for k in keys if now.get(k) != after.get(k)]
        if changed:
            fail("NX_DEPENDENCY_ORDER", f"True shading changed since ({', '.join(changed)})",
                 details={"true_shading": dict(now)})  # fmt: skip
        if apply:
            _shade(tx, part, view, bool(before.get("enabled")),
                   **{k: before.get(k) for k in keys[1:]})  # fmt: skip
    finally:
        if switch is not None:
            switch.restore()
    return f"true shading back to enabled={before.get('enabled')}"
