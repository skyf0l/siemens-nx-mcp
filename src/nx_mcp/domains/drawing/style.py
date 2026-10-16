"""Drafting-view display style and per-view hiding of construction geometry.

NX 2406 findings encoded here:

- ``DependentDisplay.Erase`` accepts datums and coordinate systems but records no
  view-dependent edit (UF ``View.AskVdeData`` stays empty), so they remain drawn.
  Layers that hold only construction geometry are hidden in the view instead
  (``Layers.SetObjectsVisibilityOnLayer``, read back); datums sharing a layer with
  bodies stay visible: new views get the ``partial`` marker and a warning, and an
  explicit ``construction_geometry: false`` edit is refused and rolled back.
- UF ``AskViewDependentStatus`` raises "Error number: 1" on prototype datums and
  coordinate systems of assembly components; only curves are queried.
- The centerline view preference does not persist through the settings builder;
  it is reported, never set.
"""

from __future__ import annotations

from nx_mcp.core.errors import fail
from nx_mcp.domains.drawing.common import CONSTRUCTION, CONSTRUCTION_LAYERS, attribute
from nx_mcp.kit import parts, records, result

MAX_CONSTRUCTION = 20000
CONSTRUCTION_TYPES = frozenset(
    {"DatumPlane", "DatumAxis", "CartesianCoordinateSystem", "CoordinateSystem", "Point", "Sketch"}
)
CONSTRUCTION_WARNING = (
    "Datums or coordinate systems stay visible in this view: NX 2406 does not erase them per "
    "view and they share a layer with model bodies. Move them to a layer of their own."
)
WIDTHS = {
    "original": "Original",
    "thin": "Thin",
    "normal": "Normal",
    "thick": "Thick",
    **{
        str(i): n
        for i, n in enumerate(
            ["One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"], 1
        )
    },
}
# NX line fonts by their value (``Preferences.Font``): 0 Invisible ... 7 DottedDashed.
FONTS = ("invisible", "solid", "dashed", "phantom", "centerline", "dotted", "long_dashed",
         "dotted_dashed")  # fmt: skip
RENDERING = {
    "fully_shaded": "FullyShaded",
    "partially_shaded": "PartiallyShaded",
    "wireframe": "Wireframe",
}
PROPERTIES = {
    "hidden_lines": ("ViewStyleHiddenLines", "HiddenLine"),
    "hidden_font": ("ViewStyleHiddenLines", "Font"),
    "hidden_width": ("ViewStyleHiddenLines", "Width"),
    "self_hidden": ("ViewStyleHiddenLines", "SelfHidden"),
    "visible_font": ("ViewStyleVisibleLines", "VisibleFont"),
    "visible_width": ("ViewStyleVisibleLines", "VisibleWidth"),
    "smooth_edges": ("ViewStyleSmoothEdges", "SmoothEdge"),
    "smooth_font": ("ViewStyleSmoothEdges", "Font"),
    "centerlines": ("ViewStyleGeneral", "Centerlines"),
    "rendering": ("ViewStyleShading", "RenderingStyle"),
}


# ---- style ----------------------------------------------------------------------------


def font_name(value):
    """A font as its name (``dashed``); a name or an NX value 0-7 is accepted, other NX
    values stay numbers."""
    if isinstance(value, str):
        return value
    number = int(getattr(value, "value", value))
    return FONTS[number] if 0 <= number < len(FONTS) else number


def _native(tx, key, value):
    P = tx.nx.Preferences
    if key.endswith("font"):
        return P.Font.ValueOf(FONTS.index(value) if isinstance(value, str) else int(value))
    if key.endswith("width"):
        return getattr(P.Width, WIDTHS[value])
    if key == "rendering":
        return getattr(P.ShadingRenderingStyleOption, RENDERING[value])
    return value


def _plain(tx, key, value):
    P = tx.nx.Preferences
    if key.endswith("font"):
        return font_name(value)
    if key.endswith("width"):
        native = result.enum_name(value, P.Width)
        return next((k for k, v in WIDTHS.items() if v == native), native)
    if key == "rendering":
        native = result.enum_name(value, P.ShadingRenderingStyleOption)
        return next((k for k, v in RENDERING.items() if v == native), native)
    return bool(value)


def read_style(tx, view, updates=None):
    """The view's style; ``updates`` (plain values) are committed first."""
    b = tx.part.SettingsManager.CreateDrawingEditViewSettingsBuilder([view])
    try:
        b.InheritSettingsFromSelectedObjects(view)
        if updates:
            for key, value in updates.items():
                group, prop = PROPERTIES[key]
                setattr(getattr(b.ViewStyle, group), prop, _native(tx, key, value))
            b.Commit()
        return {
            key: _plain(tx, key, getattr(getattr(b.ViewStyle, group), prop))
            for key, (group, prop) in PROPERTIES.items()
        }
    finally:
        b.Destroy()


def construction_state(tx, view):
    """True/False when construction geometry is shown/hidden; None when unknown or partial."""
    state = attribute(tx, view, CONSTRUCTION)
    return None if state in (None, "partial") else state == "show"


# ---- construction geometry ------------------------------------------------------------


def _construction_objects(tx, part):
    uf_view = tx.uf.View
    # Part.Curves also holds section lines and other sheet-owned curves (view dependent).
    values = [c for c in part.Curves if not uf_view.AskViewDependentStatus(c.Tag)[0]]
    values += list(part.Datums) + list(part.CoordinateSystems)
    curve_type = getattr(tx.nx, "Curve", None)
    for component, _ in parts.components(part):
        prototype = getattr(component, "Prototype", None)
        if component.IsSuppressed or prototype is None or not hasattr(prototype, "Curves"):
            continue
        for value in (
            list(prototype.Curves) + list(prototype.Datums) + list(prototype.CoordinateSystems)
        ):
            if (
                curve_type is not None
                and isinstance(value, curve_type)
                and uf_view.AskViewDependentStatus(value.Tag)[0]
            ):
                continue
            occurrence = component.FindOccurrence(value)
            if occurrence is not None:
                values.append(occurrence)
    values = list({int(v.Tag): v for v in values}.values())
    if len(values) > MAX_CONSTRUCTION:
        fail("NX_TOO_MANY_OBJECTS", f"More than {MAX_CONSTRUCTION} construction objects")
    return values


def _layers(tx, view):
    text = attribute(tx, view, CONSTRUCTION_LAYERS) or ""
    return [int(n) for n in text.split(",") if n.strip()]


def _visible_in_view(view, objects):
    try:
        visible = {int(o.Tag) for o in view.AskVisibleObjects()}
    except Exception:  # NX raises when the sheet is not shown
        return objects
    return [o for o in objects if int(o.Tag) in visible]


def _hide_layers(tx, view, kept, values):
    """Hide, in this view only, layers holding nothing but construction geometry."""
    layers_api = getattr(tx.part, "Layers", None)
    if layers_api is None or not hasattr(layers_api, "SetObjectsVisibilityOnLayer"):
        return kept
    L = tx.nx.Layer
    construction = {int(v.Tag) for v in values}
    own = [v for v in kept if not getattr(v, "IsOccurrence", False)]
    layers = [
        n
        for n in sorted({int(v.Layer) for v in own})
        if all(
            int(o.Tag) in construction or type(o).__name__ in CONSTRUCTION_TYPES
            for o in layers_api.GetAllObjectsOnLayer(n)
        )
    ]
    if layers:
        layers_api.SetObjectsVisibilityOnLayer(
            view, [L.StateInfo(n, L.State.Hidden) for n in layers], True
        )
        states = {int(s.Layer): s.State for s in layers_api.GetVisibilitiesInView(view)}
        layers = [n for n in layers if states.get(n) == L.State.Hidden]
        if layers:
            records.set_text(
                tx,
                view,
                CONSTRUCTION_LAYERS,
                ",".join(str(n) for n in sorted(set(_layers(tx, view)) | set(layers))),
            )
    return [v for v in kept if getattr(v, "IsOccurrence", False) or int(v.Layer) not in layers]


def _show_layers(tx, view):
    layers = _layers(tx, view)
    if layers:
        L = tx.nx.Layer
        tx.part.Layers.SetObjectsVisibilityOnLayer(
            view, [L.StateInfo(n, L.State.Visible) for n in layers], True
        )
        records.set_text(tx, view, CONSTRUCTION_LAYERS, "")


def set_construction(tx, view, visible, strict=False):
    """Show or hide model curves, datums and coordinate systems in one drafting view.

    Returns ``"show"``, ``"hide"`` or ``"partial"`` (some datums stay visible). With
    ``strict`` a partial hide raises ``NX_VERSION_UNSUPPORTED`` (the kernel rolls back).
    """
    values = _construction_objects(tx, tx.part)
    state = "show" if visible else "hide"
    if values and visible:
        view.DependentDisplay.RemoveErasureOnObjectAndSubobjects(values, False)
        _show_layers(tx, view)
    elif values:
        view.DependentDisplay.Erase(values)
        uf_view = tx.uf.View
        kept = [
            v
            for v in values
            if not any(
                e is not None and int(e.ViewTag) == int(view.Tag)
                for e in uf_view.AskVdeData(v.Tag)[1]
            )
        ]
        if kept:
            kept = _visible_in_view(view, _hide_layers(tx, view, kept, values))
        if kept:
            if strict:
                fail(
                    "NX_VERSION_UNSUPPORTED",
                    "This NX does not erase datums or coordinate systems per drawing view; "
                    "those on a layer shared with model bodies stay visible",
                    hint="Move the datums to a layer of their own, then retry",
                    details={
                        "not_erased": len(kept),
                        "native_types": sorted({type(v).__name__ for v in kept}),
                        "layers": sorted({int(v.Layer) for v in kept if hasattr(v, "Layer")}),
                    },
                )
            state = "partial"
    records.set_text(tx, view, CONSTRUCTION, state)
    return state
