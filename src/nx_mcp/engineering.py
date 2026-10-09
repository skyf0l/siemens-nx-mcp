"""Native engineering builders; all calls run in the existing NX transaction dispatcher."""

from __future__ import annotations

from nx_mcp.legacy_kit import unit_normal
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

    def _assembly_constraints(self, part):
        positioner = getattr(getattr(part, "ComponentAssembly", None), "Positioner", None)
        return list(positioner.Constraints) if positioner is not None else []

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
