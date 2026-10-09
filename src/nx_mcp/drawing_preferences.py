"""Associative dimension formatting and native drafting view presentation."""

from nx_mcp.kit.result import enum_name
from nx_mcp.runtime import NXToolError


def visible_in_view(view, objects):
    """Keep the objects NX actually draws in view (read back, not assumed)."""
    try:
        visible = {int(o.Tag) for o in view.AskVisibleObjects()}
    except Exception:  # AttributeError in fakes; NX raises when the sheet is not shown
        return objects
    return [o for o in objects if int(o.Tag) in visible]


# Native types that are construction geometry when they sit on a layer of their own.
CONSTRUCTION_TYPES = frozenset(
    {"DatumPlane", "DatumAxis", "CartesianCoordinateSystem", "CoordinateSystem", "Point", "Sketch"}
)
CONSTRUCTION_LAYERS = "NX_MCP_DRAWING_CONSTRUCTION_LAYERS_V1"
WIDTHS = {
    "original": "Original",
    "thin": "Thin",
    "normal": "Normal",
    "thick": "Thick",
    **{
        str(i): name
        for i, name in enumerate(
            ["One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"], 1
        )
    },
}
RENDERING = {
    "fully_shaded": "FullyShaded",
    "partially_shaded": "PartiallyShaded",
    "wireframe": "Wireframe",
}
STYLE_PROPERTIES = {
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


class DrawingPreferencesMixin:
    def _drawing_construction_visibility(self, view, visible, strict=False):
        import NXOpen.UF as U

        part = self._work_part()
        uf_view = U.UFSession.GetUFSession().View
        # Part.Curves also includes section lines and other sheet-owned curves.
        # They are annotations, not model construction geometry.
        values = [c for c in part.Curves if not uf_view.AskViewDependentStatus(c.Tag)[0]]
        values += self._datum_objects()
        for component, _ in self._walk_components(part):
            if component.IsSuppressed or component.Prototype is None:
                continue
            prototype = component.Prototype
            if not hasattr(prototype, "Curves"):
                continue
            # Datums and coordinate systems are always model objects; NX 2406 rejects
            # AskViewDependentStatus on them (error 1), so only curves are queried.
            for value in (
                list(prototype.Curves) + list(prototype.Datums) + list(prototype.CoordinateSystems)
            ):
                if (
                    isinstance(value, self.nxopen.Curve)
                    and uf_view.AskViewDependentStatus(value.Tag)[0]
                ):
                    continue
                occurrence = component.FindOccurrence(value)
                if occurrence is not None:
                    values.append(occurrence)
        values = list({int(x.Tag): x for x in values}.values())
        if len(values) > 20000:
            raise NXToolError(
                "NX_OBJECT_LIMIT", "Drawing construction selection exceeds 20000 objects"
            )
        if values:
            if visible:
                view.DependentDisplay.RemoveErasureOnObjectAndSubobjects(values, False)
                self._restore_construction_layers(view)
            else:
                view.DependentDisplay.Erase(values)
                # NX 2406 accepts Erase on datums and coordinate systems but records
                # no view-dependent edit, so they stay visible. Read the edits back.
                kept = [
                    v
                    for v in values
                    if not any(
                        e is not None and int(e.ViewTag) == int(view.Tag)
                        for e in uf_view.AskVdeData(v.Tag)[1]
                    )
                ]
                if kept:
                    kept = self._hide_construction_layers(view, kept, values)
                    kept = visible_in_view(view, kept)
                if kept:
                    if strict:
                        raise NXToolError(
                            "NX_UNAVAILABLE_ON_VERSION",
                            "This NX does not erase datums/coordinate systems per drawing view; "
                            "those on a layer shared with model bodies stay visible (move them "
                            "to a layer of their own). construction_geometry=false rolled back",
                            details={
                                "not_erased": len(kept),
                                "native_types": sorted({type(v).__name__ for v in kept}),
                                "layers": sorted(
                                    {int(v.Layer) for v in kept if hasattr(v, "Layer")}
                                ),
                            },
                        )
                    view.SetAttribute("NX_MCP_DRAWING_CONSTRUCTION_V1", "partial")
                    return len(values) - len(kept)
        view.SetAttribute("NX_MCP_DRAWING_CONSTRUCTION_V1", "show" if visible else "hide")
        return len(values)

    def _hide_construction_layers(self, view, kept, values):
        """Hide, in this view only, layers that hold nothing but construction geometry.

        NX 2406 ignores per-view erasure of datums and coordinate systems. A
        view-dependent layer visibility (Format > Visible in View) hides them
        without changing the part's global layer states. Layers that also hold
        bodies or other model geometry stay visible; their datums are returned.
        """
        part = self._work_part()
        layers_api = getattr(part, "Layers", None)
        if layers_api is None or not hasattr(layers_api, "SetObjectsVisibilityOnLayer"):
            return kept
        import NXOpen.Layer as L

        construction = {int(v.Tag) for v in values}
        own = [v for v in kept if not getattr(v, "IsOccurrence", False)]
        candidates = sorted({int(v.Layer) for v in own})
        layers = [
            n
            for n in candidates
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
                previous = self._construction_layers(view)
                view.SetAttribute(
                    CONSTRUCTION_LAYERS,
                    ",".join(str(n) for n in sorted(set(previous) | set(layers))),
                )
        return [v for v in kept if getattr(v, "IsOccurrence", False) or int(v.Layer) not in layers]

    def _construction_layers(self, view):
        if not view.HasUserAttribute(
            CONSTRUCTION_LAYERS, self.nxopen.NXObject.AttributeType.String, -1
        ):
            return []
        text = view.GetStringAttribute(CONSTRUCTION_LAYERS)
        return [int(n) for n in text.split(",") if n.strip()]

    def _restore_construction_layers(self, view):
        """Undo _hide_construction_layers: show the layers it hid in this view."""
        if not hasattr(view, "HasUserAttribute"):
            return
        layers = self._construction_layers(view)
        if not layers:
            return
        import NXOpen.Layer as L

        self._work_part().Layers.SetObjectsVisibilityOnLayer(
            view, [L.StateInfo(n, L.State.Visible) for n in layers], True
        )
        view.SetAttribute(CONSTRUCTION_LAYERS, "")

    def _view_style(self, view, updates=None):
        import NXOpen.Preferences as P

        if updates and "centerlines" in updates:
            raise NXToolError(
                "NX_UNSUPPORTED_ARGUMENT",
                "NX v2606 does not persist the tested centerline preference setter; centerlines is read-only",
                details={"mutation_outcome": "not_started"},
            )
        obj = self._drawing_object(view, "drawing_view")
        b = self._work_part().SettingsManager.CreateDrawingEditViewSettingsBuilder([obj])
        try:
            b.InheritSettingsFromSelectedObjects(obj)
            if updates:
                for key, value in updates.items():
                    if key == "construction_geometry":
                        self._drawing_construction_visibility(obj, value, strict=True)
                        continue
                    if key not in STYLE_PROPERTIES:
                        raise NXToolError(
                            "NX_INVALID_ARGUMENT", f"Unsupported view-style property: {key}"
                        )
                    group, prop = STYLE_PROPERTIES[key]
                    if key.endswith("font"):
                        if type(value) is not int or not 0 <= value <= 7:
                            raise NXToolError(
                                "NX_INVALID_ARGUMENT", "Line font must be 0..7 (0 invisible)"
                            )
                        value = P.Font.ValueOf(value)
                    elif key.endswith("width"):
                        if value not in WIDTHS:
                            raise NXToolError("NX_INVALID_ARGUMENT", "Unsupported line width")
                        value = getattr(P.Width, WIDTHS[value])
                    elif key == "rendering":
                        if value not in RENDERING:
                            raise NXToolError("NX_INVALID_ARGUMENT", "Unsupported rendering style")
                        value = getattr(P.ShadingRenderingStyleOption, RENDERING[value])
                    setattr(getattr(b.ViewStyle, group), prop, value)
                b.Commit()
            result = {
                key: getattr(getattr(b.ViewStyle, group), prop)
                for key, (group, prop) in STYLE_PROPERTIES.items()
            }
            for key, value in result.items():
                if key.endswith("font"):
                    result[key] = getattr(value, "value", value)
                elif key.endswith("width"):
                    native = enum_name(value, P.Width)
                    result[key] = next((k for k, v in WIDTHS.items() if v == native), native)
                elif key == "rendering":
                    native = enum_name(value, P.ShadingRenderingStyleOption)
                    result[key] = next((k for k, v in RENDERING.items() if v == native), native)
        finally:
            b.Destroy()
        attribute = "NX_MCP_DRAWING_CONSTRUCTION_V1"
        state = (
            obj.GetStringAttribute(attribute)
            if obj.HasUserAttribute(attribute, self.nxopen.NXObject.AttributeType.String, -1)
            else None
        )
        # "partial": model curves are erased but NX kept datums/CSYS visible.
        result["construction_geometry"] = None if state in (None, "partial") else state == "show"
        if updates:
            self._update_model()
            self._work_part().DraftingViews.UpdateViews([obj])
            result = self._view_style(view)
            if any(result[k] != v for k, v in updates.items()):
                raise NXToolError(
                    "NX_VERIFICATION_FAILED",
                    "Native drafting style did not match requested settings",
                    details={"requested": updates, "actual": result},
                )
        return result
