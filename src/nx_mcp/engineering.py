"""Native engineering builders; all calls run in the existing NX transaction dispatcher."""

from __future__ import annotations

from nx_mcp.kit.geom import cross, dot, rows, xyz
from nx_mcp.legacy_kit import finite, unit_normal, vector
from nx_mcp.runtime import NXToolError


class EngineeringMixin:
    def _engineering_owned(self, ref, kind):
        obj = self._resolve(ref, {kind})
        if obj.IsOccurrence or obj.OwningPart != self._work_part():
            raise NXToolError("NX_OBJECT_OWNER_MISMATCH", "Select geometry owned by the work part")
        return obj

    def _engineering_section(self, sketch):
        part = self._work_part()
        section = part.Sections.CreateSection()
        options = part.ScRuleFactory.CreateRuleOptions()
        try:
            rule = part.ScRuleFactory.CreateRuleCurveFeature([sketch.Feature], None, options)
        finally:
            options.Dispose()
        section.AddToSection(
            [rule], None, None, None, sketch.Origin, self.nxopen.Section.Mode.Create, False
        )
        return section

    def _engineering_result(self, feature, modified=()):
        part = self._work_part()
        bodies = [self._reference(b, "body", part, "Body") for b in feature.GetBodies()]
        ref = self._reference(feature, "feature", part, "Feature")
        return {
            "feature": ref,
            "bodies": bodies,
            "body_count": len(bodies),
            "body": bodies[0] if bodies else None,
            "units": self._units(),
            "coordinate_frame": "work_part",
            "created": [ref],
            "modified": [self._reference(b, "body", part, "Body") for b in modified],
        }

    def _engineering_direction(self, direction, origin=(0, 0, 0)):
        return self._work_part().Directions.CreateDirection(
            self.nxopen.Point3d(*map(float, origin)),
            self.nxopen.Vector3d(*unit_normal(direction)),
            self.nxopen.SmartObject.UpdateOption.WithinModeling,
        )

    def _mass_properties(self, body=None, scope="auto"):
        import NXOpen.UF

        bodies = self._geometry(body, scope)
        if not bodies or any(not b.IsSolidBody for b in bodies):
            raise NXToolError("NX_NOT_SOLID", "Mass properties require solid bodies")
        uf = NXOpen.UF.UFSession.GetUFSession()
        mass, accuracy = uf.Modeling.AskMassProps3d(
            [b.Tag for b in bodies], len(bodies), 1, 4, 0.0, 1, [0.999] + [0.0] * 10
        )
        frame = self._work_part().WCS.CoordinateSystem
        return {
            "mass_kg": mass[2],
            "volume_m3": mass[1],
            "area_m2": mass[0],
            "center_of_gravity_m": list(mass[3:6]),
            "inertia_tensor_centroid_kg_m2": [
                [mass[12], -mass[19], -mass[21]],
                [-mass[19], mass[13], -mass[20]],
                [-mass[21], -mass[20], mass[14]],
            ],
            "principal_moments_kg_m2": list(mass[31:34]),
            "density_kg_m3": mass[46],
            "body_count": len(bodies),
            "bodies": [self._reference(b, "body", self._work_part(), "Body") for b in bodies],
            "coordinate_frame": "work_part_wcs",
            "wcs_origin_in_part_units": xyz(frame.Origin),
            "wcs_rotation": rows(frame.Orientation.Element),
            "native_error_estimates": list(accuracy),
            "semantics": "sum_of_included_bodies_with_assigned_densities",
            "warnings": [
                "Overlapping solids are counted separately. Review assigned densities before using mass results."
            ],
        }

    def _component_array(
        self,
        component,
        pattern_type,
        count,
        spacing=None,
        direction=None,
        count_y=1,
        spacing_y=None,
        direction_y=None,
        center=None,
        axis=None,
        angle=None,
    ):
        if (
            type(count) is not int
            or not 2 <= count <= 100
            or type(count_y) is not int
            or not 1 <= count_y <= 100
            or count * count_y > 100
        ):
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "Array requires 2–100 total instances including the seed"
            )
        seed = self._resolve(component, {"component"})
        part = self._work_part()
        if seed.Parent != part.ComponentAssembly.RootComponent or seed.IsSuppressed:
            raise NXToolError(
                "NX_UNSUPPORTED_SCOPE", "Select an unsuppressed immediate child occurrence"
            )
        if pattern_type == "rectangular":
            if center is not None or axis is not None or angle is not None:
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "Rectangular arrays forbid circular parameters"
                )
            dx = unit_normal(direction)
            spacing = finite(spacing, "spacing", True)
            if count_y > 1:
                dy = unit_normal(direction_y)
                spacing_y = finite(spacing_y, "spacing_y", True)
                if dot(cross(dx, dy), cross(dx, dy)) < 1e-10:
                    raise NXToolError(
                        "NX_INVALID_ARGUMENT", "Array directions must not be parallel"
                    )
            elif direction_y is not None or spacing_y is not None:
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "Second direction parameters require count_y>1"
                )
        elif pattern_type == "circular":
            if (
                spacing is not None
                or direction is not None
                or count_y != 1
                or spacing_y is not None
                or direction_y is not None
            ):
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "Circular arrays forbid rectangular parameters"
                )
            center = vector(center, "center")
            axis = unit_normal(axis)
            angle = finite(angle, "angle", True)
            if (count - 1) * angle >= 360:
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "Pitch must not repeat an angular position"
                )
        else:
            raise NXToolError("NX_INVALID_ARGUMENT", "Unknown pattern_type")
        b = part.ComponentAssembly.CreateComponentPatternBuilder(None)
        try:
            b.Associative = True
            b.ComponentPatternSet.Add(seed)
            if pattern_type == "rectangular":
                b.PatternService.PatternType = b.PatternService.PatternEnum.Linear
                d = b.PatternService.RectangularDefinition
                d.XDirection = self._engineering_direction(dx)
                d.XSpacing.NCopies.RightHandSide = str(count)
                d.XSpacing.PitchDistance.RightHandSide = str(spacing)
                d.UseYDirectionToggle = count_y > 1
                d.YSpacing.NCopies.RightHandSide = str(count_y)
                if count_y > 1:
                    d.YDirection = self._engineering_direction(dy)
                    d.YSpacing.PitchDistance.RightHandSide = str(spacing_y)
            else:
                b.PatternService.PatternType = b.PatternService.PatternEnum.Circular
                d = b.PatternService.CircularDefinition
                point = part.Points.CreatePoint(self.nxopen.Point3d(*center))
                d.RotationAxis = part.Axes.CreateAxis(
                    point,
                    self._engineering_direction(axis),
                    self.nxopen.SmartObject.UpdateOption.WithinModeling,
                )
                d.AngularSpacing.NCopies.RightHandSide = str(count)
                d.AngularSpacing.PitchAngle.RightHandSide = str(angle)
                d.RadialSpacing.NCopies.RightHandSide = "1"
            pattern = b.Commit()
        finally:
            b.Destroy()
        self._update_model()
        result = self._component_pattern_record(pattern)
        if result["total_instances"] != count * count_y:
            raise NXToolError(
                "NX_PATTERN_VERIFICATION_FAILED", "Native array count differs; rolling back"
            )
        result.update(
            pattern_type=pattern_type,
            count=count,
            count_y=count_y,
            spacing=spacing,
            spacing_y=spacing_y,
            angle_degrees=angle,
        )
        return result

    def _render_view(
        self,
        path=None,
        width=1600,
        height=1000,
        background="white",
        color=None,
        style="studio",
        lighting=None,
    ):
        import hashlib
        import struct
        import uuid

        if self.session.IsBatch:
            raise NXToolError(
                "NX_VIEWPORT_UNAVAILABLE", "Rendering requires the interactive NX bridge"
            )
        if any(type(n) is not int or not 128 <= n <= 4096 for n in [width, height]):
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "Resolution must be 128–4096 pixels per dimension"
            )
        if background not in {"white", "original", "transparent", "color"} or style not in {
            "studio",
            "shaded",
            "shaded_with_edges",
        }:
            raise NXToolError("NX_INVALID_ARGUMENT", "Unsupported rendering options")
        if background == "color":
            if (
                not isinstance(color, list)
                or len(color) != 3
                or any(not 0 <= finite(v, "color") <= 1 for v in color)
            ):
                raise NXToolError("NX_INVALID_ARGUMENT", "color requires three RGB values in [0,1]")
        elif color is not None:
            raise NXToolError("NX_INVALID_ARGUMENT", "color requires background=color")
        if lighting is not None and (type(lighting) is not int or not 1 <= lighting <= 5):
            raise NXToolError("NX_INVALID_ARGUMENT", "lighting must be a native preset number 1–5")
        file = (
            self.workspace.ensure_inside(path)
            if path
            else self.workspace.root / "captures" / ("render-" + uuid.uuid4().hex + ".png")
        )
        if file.suffix.lower() != ".png" or file.exists():
            raise NXToolError("NX_INVALID_ARGUMENT", "Choose a new .png path")
        part = self.session.Parts.Display
        if part is None:
            raise NXToolError("NX_NO_DISPLAY_PART", "Open a display part first")
        file.parent.mkdir(parents=True, exist_ok=True)
        view = part.ModelingViews.WorkView
        original_style = view.RenderingStyle
        lights = builder = None
        previous_lighting = None
        try:
            view.RenderingStyle = getattr(
                self.nxopen.View.RenderingStyleType,
                {"studio": "Studio", "shaded": "Shaded", "shaded_with_edges": "ShadedWithEdges"}[
                    style
                ],
            )
            if lighting is not None:
                lights = part.Views.CreateLighting(None)
                previous_lighting = lights.LightsShadedViewsLightingCollection
                lights.LightsShadedViewsLightingCollection = getattr(
                    lights.LightingCollectionType, "Lighting" + str(lighting)
                )
                lights.Commit()
            view.UpdateDisplay()
            camera = self._view_info()
            builder = part.Views.CreateStudioImageCaptureBuilder()
            builder.Source = builder.SourceType.WorkView
            builder.UnitsEnum = builder.UnitsEnumType.Pixels
            builder.SetImageDimensionsInteger([height, width])
            builder.NativeFileBrowser = str(file)
            builder.BackgroundOption = getattr(
                builder.BackgroundOptions,
                {
                    "white": "CustomColor",
                    "color": "CustomColor",
                    "original": "Original",
                    "transparent": "Transparent",
                }[background],
            )
            if background in {"white", "color"}:
                builder.SetCustomBackgroundColor(
                    [1.0, 1.0, 1.0] if background == "white" else color
                )
            builder.Commit()
            data = file.read_bytes()
            if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
                raise NXToolError("NX_CAPTURE_FAILED", "NX did not produce a valid PNG")
            resolution = list(struct.unpack(">II", data[16:24]))
            if resolution != [width, height]:
                raise NXToolError(
                    "NX_CAPTURE_RESOLUTION_MISMATCH",
                    "Native rendering did not honor the requested resolution",
                )
        except Exception:
            file.unlink(missing_ok=True)
            raise
        finally:
            try:
                try:
                    if builder is not None:
                        builder.Destroy()
                finally:
                    try:
                        if lights is not None:
                            try:
                                if previous_lighting is not None:
                                    lights.LightsShadedViewsLightingCollection = previous_lighting
                                    lights.Commit()
                            finally:
                                lights.Destroy()
                    finally:
                        view.RenderingStyle = original_style
                        view.UpdateDisplay()
            except Exception as error:
                file.unlink(missing_ok=True)
                raise NXToolError(
                    "NX_RENDER_RESTORE_FAILED",
                    "Native rendering cleanup failed",
                    details={"mutation_outcome": "partial", "cleanup_error": str(error)},
                ) from error
        return {
            "path": str(file),
            "artifact_path": str(file.relative_to(self.workspace.root)),
            "capture_kind": "nx_native_render",
            "model_preview": True,
            "resolution": resolution,
            "requested_resolution": [width, height],
            "camera": camera,
            "background": background,
            "color": color,
            "style": style,
            "lighting_preset": lighting,
            "rendering": "NX StudioImageCaptureBuilder",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "warnings": [],
        }

    def _assembly_constraints(self, part):
        positioner = getattr(getattr(part, "ComponentAssembly", None), "Positioner", None)
        return list(positioner.Constraints) if positioner is not None else []

    def _assembly_constraint_record(self, constraint):
        import NXOpen.Positioning

        from nx_mcp.kit.result import enum_name

        part = self._work_part()
        cls = NXOpen.Positioning.Constraint
        references = []
        for ref in constraint.GetReferences():
            obj, geom = ref.GetMovableObject(), ref.GetGeometry()
            kind = next(
                (
                    name
                    for name, typ in [
                        ("face", self.nxopen.Face),
                        ("edge", self.nxopen.Edge),
                        ("body", self.nxopen.Body),
                        ("component", self.nxopen.Assemblies.Component),
                    ]
                    if isinstance(geom, typ)
                ),
                None,
            )
            references.append(
                {
                    "component": self._reference(obj, "component", part, "Component")
                    if isinstance(obj, self.nxopen.Assemblies.Component)
                    else None,
                    "geometry": self._reference(geom, kind, part, kind.title()) if kind else None,
                    "native_geometry_type": type(geom).__name__,
                }
            )
        expression = (
            constraint.Expression
            if constraint.ConstraintType in {cls.Type.Distance, cls.Type.Angle}
            else None
        )
        return {
            "object": self._reference(
                constraint, "assembly_constraint", part, "Assembly constraint"
            ),
            "constraint_type": enum_name(constraint.ConstraintType, cls.Type),
            "alignment": enum_name(constraint.ConstraintAlignment, cls.Alignment),
            "solver_status": enum_name(constraint.GetConstraintStatus(), cls.SolverStatus),
            "suppressed": constraint.Suppressed,
            "expression": self._expression_record(expression) if expression else None,
            "references": references,
        }

    def _list_assembly_constraints(self):
        rows = [
            self._assembly_constraint_record(c)
            for c in self._assembly_constraints(self._work_part())
        ]
        return {
            "constraints": rows,
            "constraint_count": len(rows),
            "coordinate_frame": "work_part",
            "units": self._units(),
        }

    def _assembly_constraint(
        self,
        constraint_type,
        component,
        geometry=None,
        target_component=None,
        target_geometry=None,
        value=None,
        alignment="infer",
    ):
        import NXOpen.Positioning

        cls = NXOpen.Positioning.Constraint
        types = {
            "fix": "Fix",
            "touch": "Touch",
            "distance": "Distance",
            "parallel": "Parallel",
            "perpendicular": "Perpendicular",
            "angle": "Angle",
            "concentric": "Concentric",
        }
        alignments = {"infer": "InferAlign", "same": "CoAlign", "opposite": "ContraAlign"}
        if constraint_type not in types or alignment not in alignments:
            raise NXToolError("NX_INVALID_ARGUMENT", "Unsupported constraint type or alignment")
        moving = self._resolve(component, {"component"})
        if (
            moving.Parent != self._work_part().ComponentAssembly.RootComponent
            or moving.IsSuppressed
        ):
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "Select an unsuppressed immediate child component"
            )
        refs = []
        if constraint_type == "fix":
            if (
                any(v is not None for v in [geometry, target_component, target_geometry, value])
                or alignment != "infer"
            ):
                raise NXToolError("NX_INVALID_ARGUMENT", "Fix takes only component")
            refs = [(moving, moving)]
        else:
            if any(
                not isinstance(v, str) or not v
                for v in [geometry, target_component, target_geometry]
            ):
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "Provide moving/target geometry and target_component"
                )
            target = self._resolve(target_component, {"component"})
            if target == moving or target.Parent != moving.Parent or target.IsSuppressed:
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "Target must be a distinct unsuppressed sibling"
                )
            for component_obj, geometry_ref in [(moving, geometry), (target, target_geometry)]:
                geom = self._resolve(geometry_ref, {"face", "edge"})
                if not geom.IsOccurrence or geom.OwningComponent != component_obj:
                    raise NXToolError(
                        "NX_OBJECT_OWNER_MISMATCH",
                        "Select occurrence geometry belonging to the specified component",
                    )
                refs.append((component_obj, geom))
            if constraint_type in {"distance", "angle"}:
                value = finite(value, "value")
                if value < 0 or (constraint_type == "angle" and value > 180):
                    raise NXToolError(
                        "NX_INVALID_ARGUMENT",
                        "Distance must be nonnegative; angle must be 0–180 degrees",
                    )
            elif value is not None:
                raise NXToolError("NX_INVALID_ARGUMENT", "value applies only to distance/angle")
        positioner = self._work_part().ComponentAssembly.Positioner
        positioner.BeginAssemblyConstraints()
        try:
            network = positioner.EstablishNetwork()
            network.MoveObjectsState = True
            constraint = positioner.CreateConstraint(True)
            constraint.ConstraintType = getattr(cls.Type, types[constraint_type])
            constraint.ConstraintAlignment = getattr(cls.Alignment, alignments[alignment])
            for component_obj, geom in refs:
                constraint.CreateConstraintReference(component_obj, geom, False, False)
            if value is not None:
                constraint.SetExpression(str(value))
            network.AddConstraint(constraint)
            network.Solve()
            network.ApplyToModel()
            self._update_model()
            if constraint.GetConstraintStatus() != cls.SolverStatus.Solved:
                raise NXToolError(
                    "NX_CONSTRAINT_UNSATISFIED",
                    "Native assembly solver did not satisfy the constraint",
                    details=self._assembly_constraint_record(constraint),
                )
            return self._assembly_constraint_record(constraint)
        finally:
            try:
                positioner.ClearNetwork()
            finally:
                positioner.EndAssemblyConstraints()

    def _edit_assembly_constraint(self, constraint, value=None, suppressed=None, alignment=None):
        import NXOpen.Positioning

        obj = self._resolve(constraint, {"assembly_constraint"})
        cls = NXOpen.Positioning.Constraint
        if all(v is None for v in [value, suppressed, alignment]):
            raise NXToolError("NX_INVALID_ARGUMENT", "Provide value, suppressed, or alignment")
        if value is not None:
            value = finite(value, "value")
            if (
                obj.ConstraintType not in {cls.Type.Distance, cls.Type.Angle}
                or value < 0
                or (obj.ConstraintType == cls.Type.Angle and value > 180)
            ):
                raise NXToolError(
                    "NX_INVALID_ARGUMENT", "value requires distance>=0 or angle 0–180 degrees"
                )
        if suppressed is not None and type(suppressed) is not bool:
            raise NXToolError("NX_INVALID_ARGUMENT", "suppressed must be boolean")
        names = {"infer": "InferAlign", "same": "CoAlign", "opposite": "ContraAlign"}
        if alignment is not None and alignment not in names:
            raise NXToolError("NX_INVALID_ARGUMENT", "Unsupported alignment")
        positioner = self._work_part().ComponentAssembly.Positioner
        positioner.BeginAssemblyConstraints()
        try:
            if value is not None:
                obj.SetExpression(str(value))
            if suppressed is not None:
                obj.Suppressed = suppressed
            if alignment is not None:
                obj.ConstraintAlignment = getattr(cls.Alignment, names[alignment])
            network = positioner.EstablishNetwork()
            network.MoveObjectsState = True
            network.AddConstraint(obj)
            network.Solve()
            network.ApplyToModel()
            self._update_model()
            if not obj.Suppressed and obj.GetConstraintStatus() != cls.SolverStatus.Solved:
                raise NXToolError(
                    "NX_CONSTRAINT_UNSATISFIED", "Edited assembly constraint was not solved"
                )
            return self._assembly_constraint_record(obj)
        finally:
            try:
                positioner.ClearNetwork()
            finally:
                positioner.EndAssemblyConstraints()

    def _mate_component(self, component, mate_type, references=None, offset=0.0):
        offset = finite(offset, "offset")
        if mate_type not in {"touch", "align", "orient", "center", "align_angle"}:
            raise NXToolError("NX_INVALID_ARGUMENT", "Unsupported mate type")
        if not references or len(references) != 2:
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "Provide moving and target occurrence face/edge references"
            )
        target_geom = self._resolve(references[1], {"face", "edge"})
        if not target_geom.IsOccurrence:
            raise NXToolError("NX_INVALID_ARGUMENT", "Target geometry must be an occurrence")
        target = self._reference(
            target_geom.OwningComponent, "component", self._work_part(), "Component"
        )["id"]
        kind = {
            "touch": "touch",
            "align": "touch",
            "orient": "parallel",
            "center": "concentric",
            "align_angle": "angle",
        }[mate_type]
        if offset and kind not in {"touch", "angle"}:
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "Offset applies only to touch/align or align_angle"
            )
        if kind == "touch" and offset:
            kind = "distance"
        return self._assembly_constraint(
            kind,
            component,
            references[0],
            target,
            references[1],
            offset if kind in {"distance", "angle"} else None,
            "opposite" if mate_type == "touch" else "same" if mate_type == "align" else "infer",
        )

    def _drawing_object(self, ref, kind):
        if ref.startswith("obj_"):
            return self._resolve(ref, {kind})
        pool = (
            self._work_part().DrawingSheets
            if kind == "drawing_sheet"
            else self._work_part().DraftingViews
        )
        matches = [o for o in pool if o.Name.casefold() == ref.casefold()]
        if len(matches) != 1:
            raise NXToolError(
                "NX_NOT_FOUND", "Use a typed reference to an existing drawing sheet/view"
            )
        return matches[0]
