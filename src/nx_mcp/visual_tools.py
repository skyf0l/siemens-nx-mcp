"""NX-native visualization and solver diagnostics. All calls run on the NX thread."""

from __future__ import annotations

import contextlib

from nx_mcp import side_effects
from nx_mcp.kit import display
from nx_mcp.kit.geom import xyz
from nx_mcp.kit.result import enum_name  # noqa: F401 - re-exported for tests
from nx_mcp.legacy_kit import unit_normal, vector
from nx_mcp.runtime import NXToolError


class VisualToolsMixin:
    @side_effects.lookup
    def _visual_part(self):
        part = self._work_part()
        if part != self.session.Parts.Display:
            raise NXToolError(
                "NX_DISPLAY_PART_MISMATCH", "Activate the target as both work and display part"
            )
        return part

    def _display_targets(self, objects, expand=False):
        if not isinstance(objects, list) or not 1 <= len(objects) <= 1000:
            raise NXToolError("NX_INVALID_ARGUMENT", "objects requires 1–1000 references")
        values = []
        for ref in objects:
            obj = self._resolve(
                ref, {"body", "face", "edge", "curve", "sketch", "component", "feature", "datum"}
            )
            if isinstance(obj, self.nxopen.Features.Feature) or (
                expand and hasattr(obj, "FindOccurrence")
            ):
                values.extend(self._geometry(ref))
            elif isinstance(obj, self.nxopen.Sketch):
                values.extend(obj.GetAllGeometry())
            else:
                values.append(obj)
        values = list({int(x.Tag): x for x in values}.values())
        if not values:
            raise NXToolError("NX_NO_TARGET_BODY", "Selection contains no displayable objects")
        return values

    def _display_ref(self, obj):
        kind = (
            "datum"
            if isinstance(
                obj,
                tuple(
                    getattr(self.nxopen, n)
                    for n in ("DatumPlane", "DatumAxis", "CoordinateSystem")
                    if hasattr(self.nxopen, n)
                ),
            )
            else "component"
            if hasattr(obj, "FindOccurrence")
            else "body"
            if isinstance(obj, self.nxopen.Body)
            else "face"
            if isinstance(obj, self.nxopen.Face)
            else "edge"
            if isinstance(obj, self.nxopen.Edge)
            else "curve"
        )
        return self._reference(obj, kind, self._work_part(), "Display object")

    def _display_record(self, obj, appearance=False):
        record = {"object": self._display_ref(obj), "blanked": bool(obj.IsBlanked)}
        if appearance:
            record["color_index"] = obj.Color
            if isinstance(obj, self.nxopen.Face):
                import NXOpen.UF

                record["transparency"] = NXOpen.UF.UFSession.GetUFSession().Obj.AskTranslucency(
                    obj.Tag
                )
        return record

    def _display_records(self, values, appearance=False):
        return [record for _, record in self._display_pairs(values, appearance)]

    def _display_pairs(self, values, appearance=False):
        """``[(obj, record)]`` of display state; bodies expand to faces with appearance."""
        if appearance:
            values = list(
                {
                    int(x.Tag): x
                    for obj in values
                    for x in (
                        [obj] + list(obj.GetFaces()) if isinstance(obj, self.nxopen.Body) else [obj]
                    )
                }.values()
            )
        if len(values) > 10000:
            raise NXToolError("NX_OBJECT_LIMIT", "Display change exceeds 10000 objects/faces")
        return [(x, self._display_record(x, appearance)) for x in values]

    def _apply_display_record(self, obj, record):
        """Put one object's saved blank state, colour and transparency back."""
        if "color_index" in record:
            self._apply_appearance([obj], record["color_index"], record.get("transparency"))
        (obj.Blank if record["blanked"] else obj.Unblank)()

    def _set_highlights(self, objects):
        """Make the agent's highlights exactly ``objects`` (restores a kept set)."""
        wanted = {int(o.Tag): o for o in objects}
        for obj in list(getattr(self, "_highlighted_objects", [])):
            if int(obj.Tag) not in wanted:
                # NX may have deleted an entity since the highlight.
                with contextlib.suppress(Exception):
                    obj.Unhighlight()
        current = {int(o.Tag) for o in getattr(self, "_highlighted_objects", [])}
        for tag, obj in wanted.items():
            if tag not in current:
                obj.Highlight()
        self._highlighted_objects = list(wanted.values())

    @property
    def _display_snapshots(self):
        """The display snapshots by restore_id (``kit.display.snapshots``)."""
        return display.snapshots.items

    def _save_display_snapshot(self, records, camera=None):
        """Store a snapshot for nx_restore_display (catalogue display domain)."""
        return display.snapshots.save(self._part_id(self._work_part()), records, camera)

    def _apply_appearance(self, values, color_index=None, transparency=None):
        display.apply_appearance(self, values, color_index, transparency)

    def _set_visibility(self, objects, mode="show"):
        part = self._visual_part()
        if mode not in {"show", "hide", "isolate"}:
            raise NXToolError("NX_INVALID_ARGUMENT", "mode must be show, hide or isolate")
        selected = self._display_targets(objects)
        if any(isinstance(x, (self.nxopen.Face, self.nxopen.Edge)) for x in selected):
            raise NXToolError(
                "NX_OBJECT_TYPE_MISMATCH",
                "Visibility targets bodies, components or curves; faces/edges are unsupported",
            )
        if mode == "isolate":
            if any(
                not isinstance(x, self.nxopen.Body) and not hasattr(x, "FindOccurrence")
                for x in selected
            ):
                raise NXToolError(
                    "NX_OBJECT_TYPE_MISMATCH", "Isolation targets bodies or components"
                )
            bodies = self._geometry(scope="assembly")
            components = [c for c, _ in self._walk_components(part)]
            values = list({int(x.Tag): x for x in bodies + components}.values())
            keep = set()
            for ref in objects:
                for obj in self._geometry(ref):
                    keep.add(int(obj.Tag))
                    component = obj.OwningComponent if obj.IsOccurrence else None
                    while component:
                        keep.add(int(component.Tag))
                        component = component.Parent
            before = self._display_records(values)
            for obj in values:
                (obj.Unblank if int(obj.Tag) in keep else obj.Blank)()
        else:
            values = selected
            before = self._display_records(values)
            for obj in values:
                (obj.Unblank if mode == "show" else obj.Blank)()
        token = self._save_display_snapshot(before)
        return {
            "restore_id": token,
            "mode": mode,
            "objects": self._display_records(values),
            "modified": [r["object"] for r in before],
            "warnings": [
                "Isolation controls loaded body/component geometry; datum and reference-curve visibility is unchanged."
            ]
            if mode == "isolate"
            else [],
        }

    def _clear_highlights(self):
        cleared = 0
        for obj in getattr(self, "_highlighted_objects", []):
            try:
                obj.Unhighlight()
                cleared += 1
            except Exception:
                pass  # NX may have deleted an entity since the highlight.
        self._highlighted_objects = []
        return {"cleared_count": cleared, "scope": "MCP-owned highlights only"}

    def _section_view(self, origin, normal, section=None, name="MCP section", cap=True):
        import NXOpen.Display

        part = self._visual_part()
        view = part.ModelingViews.WorkView
        if self.session.IsBatch:
            raise NXToolError("NX_VIEWPORT_UNAVAILABLE", "Section views require interactive NX")
        point = vector(origin, "origin")
        direction = unit_normal(normal)
        if not isinstance(name, str) or not name.strip():
            raise NXToolError("NX_INVALID_ARGUMENT", "name cannot be empty")
        target = self._resolve(section, {"section"}) if section else None
        if target is None and view.ActiveDynamicSection:
            raise NXToolError(
                "NX_SECTION_EXISTS",
                "Specify the existing section ID to edit it, or delete it first",
            )
        builder = (
            part.DynamicSections.CreateSectionBuilder(target, view)
            if target
            else part.DynamicSections.CreateSectionBuilder(view)
        )
        try:
            builder.Type = NXOpen.Display.DynamicSectionTypes.Type.OnePlane
            builder.CsysType = NXOpen.Display.DynamicSectionTypes.CoordinateSystem.Absolute
            builder.SetName(name)
            builder.SetNormal(self.nxopen.Vector3d(*direction))
            builder.SetOrigin(self.nxopen.Point3d(*point))
            builder.ClipType = NXOpen.Display.DynamicSectionTypes.Clip.Section
            builder.ShowClip = True
            builder.ShowCap = cap
            builder.ShowViewer = False
            builder.ShowGrid = False
            result = builder.Commit()
            view.ActiveDynamicSection = result
            view.DisplaySectioningToggle = True
            return {
                "object": self._reference(result, "section", part, "Section"),
                "origin": xyz(builder.GetOrigin()),
                "normal": xyz(builder.GetNormal()),
                "cap": cap,
                "coordinate_frame": "display_part",
                "retained_side": "negative_normal",
                "retained_half_space": "dot(point - origin, normal) <= 0",
                "geometry_changed": False,
                "warnings": [
                    "Native view clipping; model solids and measurements remain unchanged."
                ],
            }
        finally:
            builder.Destroy()
