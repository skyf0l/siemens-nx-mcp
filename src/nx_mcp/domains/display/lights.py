"""Scene lights of the work view: list them, switch and edit one, apply a lighting preset
(tool-surface 3.12).

A part carries a fixed set of lights (``part.Lights``; NX 2406 template parts: the eight
scene lights ``Scene Ambient``, ``Scene Left Top`` ... ``Scene Right Bottom``, the
``Standard`` eye, distant, point and spot lights, ``Point 1``/``Point 2``,
``Distant 1``/``Distant 2``). NXOpen cannot create or delete lights (``NXOpen.Light``:
"creation of lights is not supported"), so adding a light means switching one of the part's
spare lights on in the work view and removing one switches it off.

NX 2406 measured (``scripts/dev_python.py``):

- ``Light.SetEnabledInView`` switches any light on or off in a view (documented for scene
  lights only, works for all); ``Light.GetEnabledInView`` and UF ``View.AskViewLight`` (the
  lights on in the view with their location, target and mode) read the result.
- ``ViewCollection.CreateLightBuilder(light)`` + ``Commit`` sets intensity and shadow type
  and places the light: ``SourcePosition`` (point and spot lights; the far end of a
  distant light's direction) and ``DestinationPosition`` (spot target; the distant light's
  other end) in model coordinates. A light fixed to the observer (UF light mode 0) stores
  them in view coordinates; one fixed to the part (mode 1, the template's default) in model
  coordinates. The builder's ``LightMode`` property is hidden by the enum of the same name,
  so the mode is read only. The builder commits placement and cone angle, and reads the
  cone angle, only while the view is in the Studio style (in a shaded view the commit keeps
  the old placement and the cone reads 0), so the tool holds the view in Studio for the
  builder and the read-back and then puts its style back; the placement stays.
- Colour has no NXOpen setter and no read-back: UF ``View.EditViewLight(name, attributes)``
  sets it with the intensity, so the tool reports the colour it applied. EditViewLight
  refuses an intensity above 1 ("The second parameter passed in was invalid"), so it gets
  at most 1 and the builder then sets the real intensity (0..10). The call resets
  the light's fall-off options to NX defaults, and its ``GenerateShadows`` means ray-traced
  hard shadows, so it is passed off and the builder puts the shadow type back. A later
  builder commit keeps the colour. The view shows it only after ``view.Regenerate()``.
- Scene lights keep every setting except intensity and on/off (``Light.Intensity``).
- ``Display.Lighting.LightsShadedViewsLightingCollection`` = ``Lighting1..5`` + ``Commit``
  through ``CreateLighting(None)`` (the work view) replaces the lights on in the view by
  NX's preset (lighting_1: scene ambient, left top, right top). A commit through
  ``CreateLighting(view)``, ``UserDefined`` and ``SetLightBuilderInList`` fault (memory
  access violation), so none is used. Opening ``CreateLighting(view)`` without a commit
  reads the preset and gives a new part's view its default lights (none is on before), so
  every tool opens it first. ``ModelingView.UseSceneLights`` switches the eight scene
  lights on, ``UseDefaultLights`` the default set.
- Captures: a strong red distant light raises the model's mean red in shaded and Studio
  ``nx_capture_view`` images by about 60 of 255, green and blue unchanged (acceptance
  ``display/lights``). True shading uses its own lighting (``nx_set_true_shading``).
- A part that is not displayed answers for the displayed part's view (its own work view
  reads no light on), so every tool shows the part it reads or changes (gate 4s).
- All of this is part data, undone by the kernel's undo mark: a failure rolls back and
  ``nx_undo_operation`` reverts a success (lights on, intensity, placement, colour). In a
  shared session the compensating undo sets on/off, intensity, shadows and placement back
  (``@restorer``); a changed colour cannot be read back, so that undo is refused.

NXOpen documents no licence for these calls; the ray-traced and Iray image based lighting
flags and hard (ray-traced) shadows are never set.
"""

from __future__ import annotations

import contextlib
import difflib
import re
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import restorer, tool
from nx_mcp.core.errors import fail, native
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Point3, Range, Text, Vec3
from nx_mcp.kit import builders, display, geom, readback, result, switching

KINDS = {0: "ambient", 1: "distant", 2: "eye", 3: "point", 4: "spot", 6: "scene"}
SCENE = {0: "ambient", 1: "left_top", 2: "top", 3: "right_top", 4: "front",
         5: "left_bottom", 6: "bottom", 7: "right_bottom"}  # fmt: skip
SHADOWED = ("distant", "point", "spot")  # NX: no shadows for ambient, eye or scene lights
SHADOW_NAMES = ("NotSet", "SoftEdged", "HardEdged", "TranslucentHard")
OBSERVER = 0  # UF light mode: fixed to the observer (view coordinates); 1 = to the part
REACH = 100.0  # distance of a distant light's far end from the origin (direction only)
EDIT_MAX = 1.0  # highest intensity UF View.EditViewLight takes (NX 2406)
PRESETS = {f"lighting_{i}": f"Lighting{i}" for i in range(1, 6)}
TAKES = {
    "color": ("ambient", "distant", "eye", "point", "spot"),
    "direction": ("distant",),
    "position": ("point", "spot"),
    "target": ("spot",),
    "cone_angle": ("spot",),
    "shadows": SHADOWED,
}
LightName = Annotated[str, Text(64), Doc("Light name from nx_list_lights, any case: 'Distant 1'")]
Rgb = Annotated[tuple[float, float, float], Doc("Light colour RGB 0..1 or 0..255")]
Preset = Literal[
    "lighting_1", "lighting_2", "lighting_3", "lighting_4", "lighting_5", "scene", "default"
]


class LightRow(TypedDict, total=False):
    name: str
    type: str
    scene: str
    on: bool
    intensity: float
    direction: list[float]
    position: list[float]
    target: list[float]
    cone_angle: float
    shadows: bool
    fixed_to: str


class LightsData(TypedDict):
    lights: list[LightRow]
    count: int
    on: list[str]
    preset: str | None


class LightData(TypedDict, total=False):
    light: LightRow
    color: list[float]
    marks_part_modified: bool


class PresetData(TypedDict):
    preset: str
    previous: str | None
    on: list[str]
    marks_part_modified: bool


# ---- reading -----------------------------------------------------------------------------------


def _key(text) -> str:
    return "".join(re.findall(r"[a-z0-9]+", str(text).casefold()))


def _lights(part) -> list:
    with native(default="NX_ERROR"):
        return list(part.Lights)


def _find(part, name, field="/name"):
    """The part's light named ``name`` (any case and spacing), else ``NX_NOT_FOUND``."""
    lights = _lights(part)
    for light in lights:
        if _key(light.Name) == _key(name):
            return light
    names = [str(light.Name) for light in lights]
    fail("NX_NOT_FOUND", f"The work part has no light named {name!r}", field=field,
         hint="nx_list_lights lists the part's lights",
         details={"close": difflib.get_close_matches(str(name), names, 5, 0.5)})  # fmt: skip


def kind_of(light) -> tuple[str, str | None]:
    """``(type, scene position or None)``; the scene ambient light is a scene light."""
    with native(default="NX_ERROR"):
        kind, scene = light.GetLightType()
    scene_index = result.enum_int(scene)
    if scene_index >= 0:
        return "scene", SCENE.get(scene_index, f"nx_{scene_index}")
    number = result.enum_int(kind)
    return KINDS.get(number, f"nx_{number}"), None


def _placed(tx, view) -> dict:
    """``{name: (location, target, mode)}`` of the lights on in ``view`` (UF view lights)."""
    with native(default="NX_ERROR"):
        lighting = tx.uf.View.AskViewLight(view.Tag)
    out = {}
    for entry in list(lighting.Lights)[: int(lighting.LightCount)]:
        out[str(entry.LightName)] = (
            [float(v) for v in entry.Location],
            [float(v) for v in entry.To],
            result.enum_int(entry.LightMode),
        )
    return out


def _settings(tx, part, light) -> dict:
    """Shadow type and cone angle (valid in the Studio style only) from a light builder
    opened on ``light`` (no commit)."""
    with builders.build(tx, part.Views.CreateLightBuilder, light) as b:
        return {"cone": float(b.ConeAngle), "shadow": result.enum_int(b.LightShadowType)}


def _row(tx, part, view, light, placed=None) -> LightRow:
    kind, scene = kind_of(light)
    with native(default="NX_ERROR"):
        row: LightRow = {"name": str(light.Name), "type": kind,
                         "on": bool(light.GetEnabledInView(view)),
                         "intensity": round(float(light.Intensity), 6)}  # fmt: skip
    if scene is not None:
        row["scene"] = scene
    if kind in SHADOWED:
        settings = _settings(tx, part, light)
        row["shadows"] = settings["shadow"] != 0
        if kind == "spot" and display.style_name(tx, view.RenderingStyle) == "studio":
            row["cone_angle"] = round(settings["cone"], 9)
    where = (placed if placed is not None else _placed(tx, view)).get(str(light.Name))
    if kind in SHADOWED and row["on"] and where is not None:
        location, target, mode = where
        rotation = geom.rows(view.Matrix)
        to_model = (lambda p: geom.matvec(rotation, p)) if mode == OBSERVER else (lambda p: p)
        row["fixed_to"] = "view" if mode == OBSERVER else "part"
        along = geom.sub(target, location)
        if geom.norm(along) > 1e-12 and kind != "point":
            row["direction"] = [round(v, 9) for v in geom.unit(to_model(along))]
        if mode != OBSERVER and kind != "distant":
            row["position"] = location
            if kind == "spot":
                row["target"] = target
    return row


def _preset(tx, part, view) -> str | None:
    """The view's last lighting preset (``user_defined`` once lights were switched by hand).

    NX 2406: opening ``CreateLighting(view)`` also gives a new part's view its default lights
    (until then none reads as on), so every tool reads this first."""
    with builders.build(tx, part.Views.CreateLighting, view) as b:
        value = b.LightsShadedViewsLightingCollection
    names = {result.enum_int(getattr(tx.nx.Display.Lighting.LightingCollectionType, nx)): ours
             for ours, nx in PRESETS.items()}  # fmt: skip
    return names.get(result.enum_int(value), "user_defined")


def _on(part, view) -> list[str]:
    with native(default="NX_ERROR"):
        return [str(light.Name) for light in _lights(part) if light.GetEnabledInView(view)]


@contextlib.contextmanager
def studio(tx, view):
    """Hold the view in the Studio style for the block (NX 2406: light builders place lights
    and keep the cone angle only there), then put its own style back."""
    original = view.RenderingStyle
    switch = display.style_name(tx, original) != "studio"
    if switch:
        display.set_style(tx, view, "studio")
    try:
        yield
    finally:
        if switch:
            with native(default="NX_ERROR"):
                view.RenderingStyle = original


# ---- tools -------------------------------------------------------------------------------------


def _shown_for_read(services, part, args) -> bool:
    """Gate 4s asks only for a target that is not displayed: lights are read through the
    displayed view, so that part is shown for the read and the engineer's tab comes back."""
    return True


@tool(
    target="any",  # the displayed part, or the named or agent's only part
    display_bound=(_shown_for_read,),
    effect="read",
    label="List lights",
    summary="List the scene lights",
    keywords=(
        "lights",
        "lighting",
        "scene light",
        "spot light",
        "light direction",
        "studio lighting",
    ),  # fmt: skip
    requires=("work_part", "interactive"),
    cost="light",
    example={"on_only": True},
)
def nx_list_lights(
    tx: Tx,
    *,
    on_only: Annotated[bool, Doc("Only the lights that are on in the work view")] = False,
) -> LightsData:
    """List a part's lights with type, on/off in its model view and intensity (part X: X is
    shown for the read). Lit distant/point/spot lights add direction, position, target
    (model coords) and shadows; spots cone_angle in Studio style. Types: ambient, distant,
    eye, point, spot, scene. preset: last lighting preset. No colour read-back. Edit:
    nx_set_light."""
    with tx.checks():
        part = display.shown_part(tx)
        view = display.model_view(tx, part)
        preset = _preset(tx, part, view)
    placed = _placed(tx, view)
    rows = [_row(tx, part, view, light, placed) for light in _lights(part)]
    if on_only:
        rows = [r for r in rows if r["on"]]
    return {"lights": rows, "count": len(rows), "on": _on(part, view), "preset": preset}


def _rgb(color) -> list[float]:
    values = [float(c) for c in color]
    if any(v > 1.0 for v in values) and all(0.0 <= v <= 255.0 for v in values):
        values = [v / 255.0 for v in values]
    if not all(0.0 <= v <= 1.0 for v in values):
        fail("NX_INVALID_ARGUMENT", "color components are 0..1 or 0..255", field="/color")
    return values


def _check(kind, given: dict) -> None:
    """Refuse a setting the light's type does not have."""
    for field, allowed in TAKES.items():
        if given.get(field) is not None and kind not in allowed:
            fail("NX_INVALID_ARGUMENT", f"A {kind} light takes no {field}", field=f"/{field}",
                 hint=f"{field} applies to {' and '.join(allowed)} lights")  # fmt: skip


@tool(
    effect="model",
    label="Set light",
    summary=lambda a, say: f"Set light {a.get('name')}",
    keywords=(
        "light",
        "add light",
        "remove light",
        "spot light",
        "light colour",
        "light direction",
        "studio lighting",
        "shadows",
    ),  # fmt: skip
    requires=("work_part", "interactive"),
    cost="light",
    invalidates="deleted",  # display data only: every reference stays valid
    example={"name": "Distant 1", "on": True, "direction": [0, 0, -1], "color": [255, 0, 0]},
)
def nx_set_light(
    tx: Tx,
    *,
    name: LightName,
    on: Annotated[bool, Doc("Switch on (add to the view) or off (remove)")] | None = None,
    intensity: Annotated[float, Range(ge=0, le=10), Doc("Brightness; scene lights 0..1")]
    | None = None,
    color: Rgb | None = None,
    direction: Annotated[Vec3, Doc("Distant: direction the light travels, [0,0,-1] = down")]
    | None = None,
    position: Annotated[Point3, Doc("Point/spot: light location, absolute part units")]
    | None = None,
    target: Annotated[Point3, Doc("Spot: the point it aims at")] | None = None,
    cone_angle: Annotated[float, Range(ge=1, le=170), Doc("Spot cone, degrees")] | None = None,
    shadows: Annotated[bool, Doc("Soft shadows (distant/point/spot)")] | None = None,
) -> LightData:
    """Switch one of the work part's lights on or off and set intensity, colour or placement.
    NX cannot create lights: add one by turning on a spare (Distant 1, Point 1, Standard Z
    Spot); on=false removes it. Scene lights take on and intensity only; ambient/eye add
    color. Placing needs the light on. Shows in shaded/Studio views and captures; saved with
    the part. Undo: nx_undo_operation (not a colour change). Names: nx_list_lights."""
    given = {"color": color, "direction": direction, "position": position, "target": target,
             "cone_angle": cone_angle, "shadows": shadows}  # fmt: skip
    if on is None and intensity is None and all(v is None for v in given.values()):
        fail("NX_INVALID_ARGUMENT", "Give on or at least one setting to change", field="/on")
    rgb = _rgb(color) if color is not None else None
    source = aim = None
    with tx.checks():
        part = display.shown_part(tx, work=True)
        view = display.model_view(tx, part)
        _preset(tx, part, view)
        light = _find(part, name)
        kind, _scene = kind_of(light)
        _check(kind, given)
        if kind == "scene" and intensity is not None and intensity > 1.0:
            fail("NX_INVALID_ARGUMENT", "Scene light intensity is 0..1", field="/intensity")
        with native(default="NX_ERROR"):
            lit = bool(light.GetEnabledInView(view)) if on is None else bool(on)
        placing = direction is not None or position is not None or target is not None
        if placing and not lit:
            fail("NX_INVALID_ARGUMENT", "Placing a light needs it on", field="/on",
                 hint="Add on=true to switch it on in the work view")  # fmt: skip
        before = _row(tx, part, view, light)
        shadow_before = _settings(tx, part, light)["shadow"] if kind in SHADOWED else 0
        before_on = _on(part, view)
        if direction is not None:
            way = geom.unit([float(v) for v in direction], "/direction")
            source, aim = geom.scale(way, -REACH), [0.0, 0.0, 0.0]
        elif kind == "point" and position is not None:
            source = [float(v) for v in position]
        elif kind == "spot" and placing:
            source = (
                [float(v) for v in position] if position is not None else before.get("position")
            )
            aim = [float(v) for v in target] if target is not None else before.get("target")
            if source is None or aim is None:
                fail("NX_INVALID_ARGUMENT", "Give both position and target for this spot light",
                     field="/target" if aim is None else "/position")  # fmt: skip
            if geom.dist(source, aim) <= geom.tol(source, aim):
                fail("NX_INVALID_ARGUMENT", "target must differ from position", field="/target")
    was_modified = bool(part.IsModified)
    row = _apply(tx, part, view, light, kind, on=on, intensity=intensity, rgb=rgb,
                 source=source, aim=aim, cone_angle=cone_angle, shadows=shadows,
                 shadow_before=shadow_before)  # fmt: skip
    _verify(row, on, intensity, given)
    tx.note_restore({"light": str(light.Name), "before": before, "after": row,
                     "before_on": before_on, "color": rgb is not None})  # fmt: skip
    out: LightData = {
        "light": row,
        "marks_part_modified": not was_modified and bool(part.IsModified),
    }
    if rgb is not None:
        out["color"] = [round(v, 6) for v in rgb]
    return out


def _apply(tx, part, view, light, kind, *, on=None, intensity=None, rgb=None, source=None,
           aim=None, cone_angle=None, shadows=None, shadow_before=0) -> LightRow:  # fmt: skip
    """Switch, colour, intensity and placement of one light, then its row read back."""
    placing = source is not None or aim is not None
    with native(default="NX_BUILD_FAILED"):
        if on is not None:
            light.SetEnabledInView(view, bool(on))
        level = float(intensity) if intensity is not None else float(light.Intensity)
        if rgb is not None:
            attributes = tx.uf.View.LightAttributes()
            attributes.Red, attributes.Green, attributes.Blue = (round(v * 255) for v in rgb)
            # NX 2406 refuses more than 1 here; the builder below sets the real intensity
            attributes.Intensity = min(level, EDIT_MAX)
            attributes.IntensityFactor = tx.uf.View.LightIntensityFactor.INTENSITY_FACTOR_NONE
            attributes.GenerateShadows = False  # True means hard (ray-traced) shadows
            tx.uf.View.EditViewLight(str(light.Name), attributes)
        if kind == "scene" and intensity is not None:
            light.Intensity = level
    with studio(tx, view):
        if kind != "scene" and (intensity is not None or rgb is not None or placing
                                or cone_angle is not None or shadows is not None):  # fmt: skip
            with builders.build(tx, part.Views.CreateLightBuilder, light) as b:
                b.Intensity = level
                _shadow(tx, b, kind, shadows, shadow_before)
                if cone_angle is not None:
                    b.ConeAngle = float(cone_angle)
                if source is not None:
                    b.SourcePosition = builders.point(tx, source)
                if aim is not None:
                    b.DestinationPosition = builders.point(tx, aim)
                b.commit()
        with native(default="NX_ERROR"):
            view.Regenerate()  # NX 2406: an edited colour shows only after a regenerate
        return _row(tx, part, view, light)


def _shadow(tx, b, kind, shadows, before) -> None:
    """Soft shadows on or off, else NX's own shadow type back (EditViewLight resets it)."""
    shadow_type = tx.nx.Display.LightBuilder.ShadowType
    if shadows is not None:
        b.LightShadowType = getattr(shadow_type, "SoftEdged" if shadows else "NotSet")
    elif kind in SHADOWED:
        members = (getattr(shadow_type, n, None) for n in SHADOW_NAMES)
        b.LightShadowType = next(
            (m for m in members if m is not None and result.enum_int(m) == before),
            shadow_type.NotSet,
        )


def _verify(row: LightRow, on, intensity, given) -> None:
    if on is not None:
        readback.expect(row["on"] == bool(on), "on", actual=row["on"])
    if intensity is not None:
        readback.expect_close(row["intensity"], float(intensity), "intensity", tol=1e-6)
    if given["shadows"] is not None:
        readback.expect(row["shadows"] == bool(given["shadows"]), "shadows", actual=row["shadows"])
    if given["cone_angle"] is not None:
        readback.expect("cone_angle" in row, "cone_angle", actual=None)
        readback.expect_close(row["cone_angle"], float(given["cone_angle"]), "cone_angle", 1e-6)
    if given["direction"] is not None:
        readback.expect("direction" in row, "direction", actual=None)
        readback.expect_same_direction(row["direction"], given["direction"], "direction")
    for field in ("position", "target"):
        if given[field] is not None:
            readback.expect(field in row, field, actual=None)
            readback.expect_close(row[field], [float(v) for v in given[field]], field)


@tool(
    effect="model",
    label="Set lighting preset",
    summary=lambda a, say: f"Lighting preset {a.get('preset')}",
    keywords=(
        "lighting preset",
        "scene lights",
        "default lights",
        "lighting scheme",
        "reset lights",
    ),  # fmt: skip
    requires=("work_part", "interactive"),
    cost="light",
    invalidates="deleted",  # display data only: every reference stays valid
    example={"preset": "scene"},
)
def nx_set_lighting_preset(tx: Tx, *, preset: Preset) -> PresetData:
    """Replace the lights on in the work view with an NX lighting preset. lighting_1 to
    lighting_5: NX's shaded view lighting sets; scene: all eight scene lights; default: NX's
    default set. Lights switched on with nx_set_light go off. Saved with the part; undo with
    nx_undo_operation (the lights on before come back), or apply the previous preset."""
    with tx.checks():
        part = display.shown_part(tx, work=True)
        view = display.model_view(tx, part)
        before_preset = _preset(tx, part, view)
        before_on = _on(part, view)
    was_modified = bool(part.IsModified)
    if preset in PRESETS:
        wanted = getattr(tx.nx.Display.Lighting.LightingCollectionType, PRESETS[preset])
        # NX 2406: a commit through CreateLighting(view) faults; None means the work view
        with builders.build(tx, part.Views.CreateLighting, None) as b:
            b.LightsShadedViewsLightingCollection = wanted
            b.commit()
        readback.expect(_preset(tx, part, view) == preset, "preset")
    else:
        with native(default="NX_BUILD_FAILED"):
            (view.UseSceneLights if preset == "scene" else view.UseDefaultLights)()
    with native(default="NX_ERROR"):
        view.Regenerate()
    on = _on(part, view)
    readback.expect(bool(on), "lights_on", actual=on)
    if preset == "scene":
        scene = [str(x.Name) for x in _lights(part) if kind_of(x)[0] == "scene"]
        readback.expect(set(scene) <= set(on), "scene_lights_on", actual=on)
    tx.note_restore({"preset": before_preset, "before_on": before_on, "after_on": on})
    return {"preset": preset, "previous": before_preset, "on": on,
            "marks_part_modified": not was_modified and bool(part.IsModified)}  # fmt: skip


# ---- compensating undo (shared sessions) ---------------------------------------------------


def _shown(tx):
    """``(part, view, switch or None)``: the work part shown (a lights' view exists only on
    the displayed part); ``switch`` puts the engineer's display back."""
    part = tx.part
    shown = tx.session.Parts.Display
    switch = None
    if not display.same(shown, part):
        switch = switching.Switch(tx, part, "display").enter()
    return part, display.model_view(tx, part), switch


def _lights_on_now(tx, data, check) -> None:
    """Refuse when the lights on differ from what the operation left (changed since)."""
    part, view, switch = _shown(tx)
    try:
        now = _on(part, view)
    finally:
        if switch is not None:
            switch.restore()
    if sorted(now) != sorted(data[check]):
        fail("NX_DEPENDENCY_ORDER", f"The lights on changed since: now {now}",
             details={"on": now})  # fmt: skip


@restorer("nx_set_light")
def _restore_light(tx, data: dict, *, apply: bool) -> str:
    """Put a light an ``nx_set_light`` changed back: on/off, intensity, shadows and its
    placement (refused after a colour change: NX has no colour read-back)."""
    name, before, after = str(data["light"]), data["before"], data["after"]
    if data.get("color"):
        fail("NX_UNSUPPORTED_OPTION", f"The colour of {name} cannot be read back, so it is not "
             "set back", hint="Set the previous colour with nx_set_light")  # fmt: skip
    part, view, switch = _shown(tx)
    try:
        light = _find(part, name)
        kind, _scene = kind_of(light)
        now = _row(tx, part, view, light)
        if (
            now.get("on") != after.get("on")
            or abs(float(now.get("intensity", 0)) - float(after.get("intensity", 0))) > 1e-6
        ):
            fail("NX_DEPENDENCY_ORDER", f"{name} changed since: on {now.get('on')}, intensity "
                 f"{now.get('intensity')}", details={"light": now})  # fmt: skip
        if not apply:
            return f"light {name}"
        source = aim = None
        placed = before.get("on") and before.get("fixed_to") != "view"  # model coordinates
        if placed and kind == "distant" and before.get("direction") is not None:
            source = geom.scale(geom.unit(before["direction"]), -REACH)
            aim = [0.0, 0.0, 0.0]
        elif placed and kind in ("point", "spot") and before.get("position"):
            source = list(before["position"])
            aim = list(before["target"]) if before.get("target") else None
        shadows = before.get("shadows") if kind in SHADOWED else None
        row = _apply(tx, part, view, light, kind, on=bool(before.get("on")),
                     intensity=float(before["intensity"]), source=source, aim=aim,
                     shadows=shadows)  # fmt: skip
        readback.expect(row["on"] == bool(before.get("on")), "on", actual=row["on"])
        readback.expect_close(row["intensity"], float(before["intensity"]), "intensity", 1e-6)
        if data.get("before_on") is not None:
            readback.expect(sorted(_on(part, view)) == sorted(data["before_on"]), "lights_on")
    finally:
        if switch is not None:
            switch.restore()
    return f"light {name} back to on={before.get('on')}, intensity {before.get('intensity')}"


@restorer("nx_set_lighting_preset")
def _restore_preset(tx, data: dict, *, apply: bool) -> str:
    """Switch back on exactly the lights that were on before an ``nx_set_lighting_preset``
    (and that preset when it was one of lighting_1..5)."""
    _lights_on_now(tx, data, "after_on")
    if not apply:
        return "the previous lights"
    part, view, switch = _shown(tx)
    try:
        wanted = set(data["before_on"])
        previous = data.get("preset")
        if previous in PRESETS:
            with builders.build(tx, part.Views.CreateLighting, None) as b:
                b.LightsShadedViewsLightingCollection = getattr(
                    tx.nx.Display.Lighting.LightingCollectionType, PRESETS[previous]
                )
                b.commit()
        with native(default="NX_BUILD_FAILED"):
            for light in _lights(part):
                if bool(light.GetEnabledInView(view)) != (str(light.Name) in wanted):
                    light.SetEnabledInView(view, str(light.Name) in wanted)
            view.Regenerate()
        now = _on(part, view)
        readback.expect(sorted(now) == sorted(wanted), "lights_on", actual=now)
    finally:
        if switch is not None:
            switch.restore()
    return f"the lights on before ({previous or 'user_defined'})"
