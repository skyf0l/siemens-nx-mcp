"""NX 2606 native patterns, geometric rules and constrained parameter editing."""

from __future__ import annotations

import math

from nx_mcp.authoring import page
from nx_mcp.kit.geom import dot, rows, xyz
from nx_mcp.kit.result import enum_name
from nx_mcp.legacy_kit import finite, unit_normal
from nx_mcp.runtime import NXToolError


class AdvancedAuthoringMixin:
    def _geometry_owner_locator(self, owner):
        obj = self._resolve(owner, {"body", "feature", "component"})
        kind = (
            "body"
            if isinstance(obj, self.nxopen.Body)
            else "feature"
            if isinstance(obj, self.nxopen.Features.Feature)
            else "component"
        )
        return self._locator(obj, kind)

    def _resolve_geometry(self, selector, tie_tolerance=0.001):
        tol = finite(tie_tolerance, "tie_tolerance", True)
        fields = {"kind", "geometry_type", "normal", "radius", "near", "order", "axis", "tolerance"}
        if (
            not isinstance(selector, dict)
            or set(selector) != {"version", "owner_part", "owner", "query"}
            or selector["version"] != 1
            or not isinstance(selector["owner_part"], str)
            or not isinstance(selector["query"], dict)
            or set(selector["query"]) != fields
        ):
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "Use the complete version 1 selector from nx_find_geometry"
            )
        if selector["owner_part"].casefold() != self._work_part().FullPath.casefold():
            raise NXToolError("NX_OBJECT_OWNER_MISMATCH", "Selector belongs to another part")
        owner = selector["owner"]
        if owner is not None:
            if (
                not isinstance(owner, dict)
                or set(owner) != {"kind", "journal_id", "owner_part"}
                or owner["kind"] not in {"body", "feature", "component"}
                or not isinstance(owner["journal_id"], str)
                or not isinstance(owner["owner_part"], str)
            ):
                raise NXToolError("NX_INVALID_ARGUMENT", "Invalid selector owner")
            obj = self._locate(owner)
            owner = self._reference(obj, owner["kind"], self._work_part(), "Owner")["id"]
        result = self._find_geometry(owner=owner, **selector["query"], limit=2)
        if not result["items"]:
            raise NXToolError("NX_NOT_FOUND", "No geometry satisfies the saved rule")
        if (
            len(result["items"]) > 1
            and abs(result["items"][0]["rank_value"] - result["items"][1]["rank_value"]) <= tol
        ):
            raise NXToolError(
                "NX_AMBIGUOUS_REFERENCE",
                "Best geometry candidates are tied; narrow the selection rule",
                details={"candidates": result["items"]},
            )
        return {
            "match": result["items"][0],
            "selector": selector,
            "resolution": "reevaluated geometric rule",
            "units": self._units(),
            "coordinate_frame": "work_part",
        }

    def _recognize_holes(self, owner=None, offset=0, limit=50):
        import NXOpen.UF

        uf = NXOpen.UF.UFSession.GetUFSession()
        self._require_api(uf.Modeling, "AskFaceUvMinmax")
        rows = []
        start = 0
        while True:
            batch = self._find_geometry(
                owner=owner, geometry_type="cylinder", order="lowest", offset=start, limit=200
            )
            for row in batch["items"]:
                if row["cylindrical_role"] != "bore":
                    continue
                face = self._resolve(row["object"]["id"], {"face"})
                uv = uf.Modeling.AskFaceUvMinmax(face.Tag)
                coverage = abs(uv[1] - uv[0])
                row.update(
                    angular_coverage_radians=coverage,
                    full_circumference=abs(coverage - 2 * math.pi) <= 1e-6,
                )
                rows.append(row)
            if batch["next_offset"] is None:
                break
            start = batch["next_offset"]
        groups = []
        for row in rows:
            axis = unit_normal(row["axis"])
            for group in groups:
                delta = [a - b for a, b in zip(row["axis_point"], group["axis_point"], strict=True)]
                axial = dot(delta, group["axis"])
                radial = math.sqrt(max(0, dot(delta, delta) - axial * axial))
                if abs(dot(axis, group["axis"])) >= 1 - 1e-8 and radial <= 0.001:
                    break
            else:
                group = {
                    "group": len(groups),
                    "axis": axis,
                    "axis_point": row["axis_point"],
                    "faces": [],
                }
                groups.append(group)
            group["faces"].append(row["object"])
            row["coaxial_group"] = group["group"]
        return {
            **page(rows, offset, limit),
            "coaxial_groups": groups,
            "units": self._units(),
            "coordinate_frame": "work_part",
            "recognition": "inward cylindrical BREP faces",
            "limitations": [
                "No blind/through, thread or manufacturing-feature inference; partial faces are reported."
            ],
        }

    def _component_pattern_record(self, pattern):
        import NXOpen.GeometricUtilities

        part = self._work_part()
        builder = part.ComponentAssembly.CreateComponentPatternBuilder(pattern)
        try:
            service = builder.PatternService
            linear = (
                service.PatternType
                == NXOpen.GeometricUtilities.PatternDefinition.PatternEnum.Linear
            )
            result = {
                "object": self._reference(pattern, "component_pattern", part, "Component pattern"),
                "native_type": "NXOpen.Assemblies.ComponentPattern",
                "pattern_type": "linear"
                if linear
                else str(enum_name(service.PatternType, service.PatternEnum)).lower(),
                "associative": bool(builder.Associative),
            }
            if linear:
                spacing = service.RectangularDefinition.XSpacing
                result.update(
                    count_expression=self._expression_record(spacing.NCopies),
                    spacing_expression=self._expression_record(spacing.PitchDistance),
                    count_includes_seed=True,
                )
            if linear:
                d = service.RectangularDefinition
                result["second_direction_enabled"] = d.UseYDirectionToggle
                if d.UseYDirectionToggle:
                    result["count_y_expression"] = self._expression_record(d.YSpacing.NCopies)
                    result["spacing_y_expression"] = self._expression_record(
                        d.YSpacing.PitchDistance
                    )
            elif service.PatternType == service.PatternEnum.Circular:
                d = service.CircularDefinition
                result["count_expression"] = self._expression_record(d.AngularSpacing.NCopies)
                result["angle_expression"] = self._expression_record(d.AngularSpacing.PitchAngle)
                result["count_includes_seed"] = True
            components = {int(c.Tag): c for c in pattern.GetComponentsToPattern()}
            for member in pattern.GetAllPatternMembers():
                for c in member.GetAllComponents():
                    components[int(c.Tag)] = c
            result["instances"] = []
            for c in components.values():
                p, m = c.GetPosition()
                result["instances"].append(
                    {
                        "object": self._reference(c, "component", part, "Component"),
                        "translation": xyz(p),
                        "rotation_matrix": rows(m),
                        "suppressed": bool(c.IsSuppressed),
                    }
                )
            result["total_instances"] = len(components)
            return result
        finally:
            builder.Destroy()

    def _component_patterns(self, part):
        assembly = part.ComponentAssembly
        if not assembly.RootComponent or not hasattr(assembly, "ComponentPatterns"):
            return []
        self._require_api(assembly.ComponentPatterns, "GetAllComponentPatterns")
        return list(assembly.ComponentPatterns.GetAllComponentPatterns())

    def _list_component_patterns(self):
        assembly = self._work_part().ComponentAssembly
        self._require_api(assembly, "ComponentPatterns", "CreateComponentPatternBuilder")
        patterns = [
            self._component_pattern_record(p) for p in self._component_patterns(self._work_part())
        ]
        return {
            "patterns": patterns,
            "count": len(patterns),
            "units": self._units(),
            "coordinate_frame": "work_part",
        }

    def _pattern_inputs(self, spacing, count):
        if count is not None and (type(count) is not int or not 2 <= count <= 100):
            raise NXToolError(
                "NX_INVALID_ARGUMENT", "count must be integer 2–100 including the seed"
            )
        if spacing is not None:
            finite(spacing, "spacing", True)

    def _native_component_pattern(self, component, direction, spacing, count):
        import NXOpen.GeometricUtilities

        self._pattern_inputs(spacing, count)
        axis = unit_normal(direction)
        part = self._work_part()
        seed = self._resolve(component, {"component"})
        if seed.Parent != part.ComponentAssembly.RootComponent or seed.IsSuppressed:
            raise NXToolError(
                "NX_UNSUPPORTED_SCOPE", "Seed must be an unsuppressed immediate child"
            )
        self._require_api(part.ComponentAssembly, "CreateComponentPatternBuilder")
        b = part.ComponentAssembly.CreateComponentPatternBuilder(None)
        try:
            b.Associative = True
            b.ComponentPatternSet.Add(seed)
            b.PatternService.PatternType = (
                NXOpen.GeometricUtilities.PatternDefinition.PatternEnum.Linear
            )
            d = b.PatternService.RectangularDefinition
            d.XDirection = part.Directions.CreateDirection(
                self.nxopen.Point3d(0.0, 0.0, 0.0),
                self.nxopen.Vector3d(*axis),
                self.nxopen.SmartObject.UpdateOption.WithinModeling,
            )
            d.XSpacing.NCopies.RightHandSide = str(count)
            d.XSpacing.PitchDistance.RightHandSide = str(float(spacing))
            d.YSpacing.NCopies.RightHandSide = "1"
            pattern = b.Commit()
        finally:
            b.Destroy()
        self._update_model()
        result = self._component_pattern_record(pattern)
        if result["total_instances"] != count or not result["associative"]:
            raise NXToolError(
                "NX_PATTERN_VERIFICATION_FAILED",
                "Native association or member count differs; rolling back",
            )
        return result

    def _edit_component_pattern(
        self, pattern, spacing=None, count=None, count_y=None, spacing_y=None, angle=None
    ):
        if all(v is None for v in [spacing, count, count_y, spacing_y, angle]):
            raise NXToolError("NX_INVALID_ARGUMENT", "Supply at least one parameter")
        self._pattern_inputs(spacing, count)
        if count_y is not None and (type(count_y) is not int or not 1 <= count_y <= 100):
            raise NXToolError("NX_INVALID_ARGUMENT", "count_y must be 1–100")
        if spacing_y is not None:
            spacing_y = finite(spacing_y, "spacing_y", True)
        if angle is not None:
            angle = finite(angle, "angle", True)
        obj = self._resolve(pattern, {"component_pattern"})
        b = self._work_part().ComponentAssembly.CreateComponentPatternBuilder(obj)
        try:
            service = b.PatternService
            if not b.Associative or len(obj.GetComponentsToPattern()) != 1:
                raise NXToolError(
                    "NX_UNSUPPORTED_EDIT", "Select an associative single-seed pattern"
                )
            if service.PatternType == service.PatternEnum.Linear:
                if angle is not None:
                    raise NXToolError("NX_INVALID_ARGUMENT", "angle requires a circular pattern")
                d = service.RectangularDefinition
                nx = count if count is not None else int(d.XSpacing.NCopies.Value)
                ny = (
                    count_y
                    if count_y is not None
                    else int(d.YSpacing.NCopies.Value)
                    if d.UseYDirectionToggle
                    else 1
                )
                if ny > 1 and not d.UseYDirectionToggle:
                    raise NXToolError(
                        "NX_UNSUPPORTED_EDIT",
                        "Create a two-direction array before increasing its second-direction count",
                    )
                if spacing_y is not None and ny == 1:
                    raise NXToolError(
                        "NX_INVALID_ARGUMENT", "spacing_y requires a second direction"
                    )
                expected = nx * ny
                if expected > 100:
                    raise NXToolError("NX_INVALID_ARGUMENT", "At most 100 total instances")
                if count is not None:
                    d.XSpacing.NCopies.RightHandSide = str(count)
                if spacing is not None:
                    d.XSpacing.PitchDistance.RightHandSide = str(float(spacing))
                if count_y is not None:
                    d.YSpacing.NCopies.RightHandSide = str(count_y)
                    d.UseYDirectionToggle = count_y > 1
                if spacing_y is not None:
                    d.YSpacing.PitchDistance.RightHandSide = str(spacing_y)
            elif service.PatternType == service.PatternEnum.Circular:
                if any(v is not None for v in [spacing, count_y, spacing_y]):
                    raise NXToolError(
                        "NX_INVALID_ARGUMENT", "Circular edits accept count and angle only"
                    )
                d = service.CircularDefinition
                expected = count if count is not None else int(d.AngularSpacing.NCopies.Value)
                pitch = angle if angle is not None else d.AngularSpacing.PitchAngle.Value
                if (expected - 1) * pitch >= 360:
                    raise NXToolError(
                        "NX_INVALID_ARGUMENT", "Angular positions must not duplicate the seed"
                    )
                if count is not None:
                    d.AngularSpacing.NCopies.RightHandSide = str(count)
                if angle is not None:
                    d.AngularSpacing.PitchAngle.RightHandSide = str(angle)
            else:
                raise NXToolError(
                    "NX_UNSUPPORTED_EDIT", "Only rectangular and circular patterns are supported"
                )
            committed = b.Commit()
        finally:
            b.Destroy()
        # NX recreates the pattern (new tag, old one deleted) for structural edits
        # such as turning off the second direction; read back what Commit returned.
        replaced = committed is not None and int(committed.Tag) != int(obj.Tag)
        if committed is not None:
            obj = committed
        self._update_model()
        result = self._component_pattern_record(obj)
        if result["total_instances"] != expected:
            raise NXToolError(
                "NX_PATTERN_VERIFICATION_FAILED", "Native member count differs; rolling back"
            )
        if replaced:
            result["warnings"] = [
                "NX recreated the pattern for this edit; the previous pattern ID is stale, use the returned ID"
            ]
        return result
