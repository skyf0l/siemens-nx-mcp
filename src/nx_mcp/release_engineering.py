"""Native drawing authoring and persistent imported-geometry references."""

from nx_mcp.kit.drawing import work_view_is_drawing as drawing_work_view
from nx_mcp.kit.geom import xyz
from nx_mcp.runtime import NXToolError

CONSTRUCTION_WARNING = (
    "Datums or coordinate systems remain visible in this view: this NX does not erase them "
    "per view, and they share a layer with model bodies (datums on layers of their own are "
    "hidden in the view). Move them to a separate layer."
)


class ReleaseEngineeringMixin:
    def _sheet_units(self, sheet):
        import NXOpen.Drawings as D

        if sheet.Units == D.DrawingSheet.Unit.Millimeters:
            return "mm"
        if sheet.Units == D.DrawingSheet.Unit.Inches:
            return "in"
        raise NXToolError("NX_UNSUPPORTED_UNITS", "Unknown native drawing units")

    def _view_sheet(self, view):
        matches = [s for s in self._work_part().DrawingSheets if view in s.GetDraftingViews()]
        if len(matches) != 1:
            raise NXToolError("NX_OBJECT_OWNER_MISMATCH", "Select a view on one work-part sheet")
        return matches[0]

    def _open_drawing_sheet(self, sheet, part=None):
        """Make sheet the current drawing sheet and verify it.

        NX 2406 (seen in Sheet Metal) can keep a sheet as the work view while
        CurrentDrawingSheet is None; Sheet.Open() then returns without making it
        current, and drafting builders commit null views ("Null tag not allowed").
        UF Draw.OpenDrawing re-opens it in that state.
        """
        sheets = getattr(part or self._work_part(), "DrawingSheets", None)
        if not hasattr(sheets, "CurrentDrawingSheet"):  # NXOpen always reports it
            sheet.Open()
            return sheet

        def current():
            value = sheets.CurrentDrawingSheet
            return value is not None and int(value.Tag) == int(sheet.Tag)

        sheet.Open()
        if current():
            return sheet
        import NXOpen.UF

        NXOpen.UF.UFSession.GetUFSession().Draw.OpenDrawing(sheet.Tag)
        if not current():
            raise NXToolError(
                "NX_DRAWING_CONTEXT",
                f"NX could not make drawing sheet {sheet.Name!r} the current sheet",
            )
        return sheet

    def _close_drawing_sheets(self, part):
        """Leave the drawing display and verify the modeling work view is back."""
        part.Drafting.ExitDraftingApplication()
        if part.DrawingSheets.CurrentDrawingSheet is None and not drawing_work_view(part):
            return
        import NXOpen.UF

        # NX 2406 can keep the sheet as work view after ExitDraftingApplication.
        # UF display state 1 is the modeling display (2 shows the drawing).
        NXOpen.UF.UFSession.GetUFSession().Draw.SetDisplayState(1)
        if part.DrawingSheets.CurrentDrawingSheet is not None or drawing_work_view(part):
            raise NXToolError("NX_DRAWING_CONTEXT", "NX did not return to the modeling view")

    def _drawing_view_info(self, view):
        import NXOpen.UF as U

        obj = self._drawing_object(view, "drawing_view")
        sheet = self._view_sheet(obj)
        uf = U.UFSession.GetUFSession().Draw
        bounds = list(uf.AskViewBorders(obj.Tag))
        style = self._view_style(view)
        warnings = []
        if not style["hidden_lines"] or not style["self_hidden"]:
            warnings.append(
                "Hidden-line processing is disabled: occluded edges may be drawn as visible. "
                "To hide them use hidden_lines=true, self_hidden=true, hidden_font=0."
            )
        if (
            obj.HasUserAttribute(
                "NX_MCP_DRAWING_CONSTRUCTION_V1", self.nxopen.NXObject.AttributeType.String, -1
            )
            and obj.GetStringAttribute("NX_MCP_DRAWING_CONSTRUCTION_V1") == "partial"
        ):
            warnings.append(CONSTRUCTION_WARNING)
        return {
            "object": self._reference(obj, "drawing_view", self._work_part(), "View"),
            "drawing": self._reference(sheet, "drawing_sheet", self._work_part(), "Sheet"),
            "native_type": type(obj).__name__,
            "style": style,
            "warnings": warnings,
            "position": xyz(obj.GetDrawingReferencePoint())[:2],
            "scale": uf.AskViewScale(obj.Tag)[1],
            "bounds": bounds,
            "inside_sheet": bounds[0] >= 0
            and bounds[1] >= 0
            and bounds[2] <= sheet.Length
            and bounds[3] <= sheet.Height,
            "units": self._sheet_units(sheet),
            "coordinate_frame": "drawing_sheet",
            "bounds_semantics": "native drafting view border, excluding separately placed annotations",
        }

    def _sheet_point3d(self, sheet, values):
        """NXOpen placement points use part units even on a differently sized sheet."""
        factor = (25.4 if self._sheet_units(sheet) == "in" else 1.0) / (
            25.4 if self._units() == "inch" else 1.0
        )
        return self.nxopen.Point3d(*(float(v) * factor for v in values), 0.0)

    def _detail_boundary_points(self, parent, sheet, center, radius):
        import NXOpen.UF as U

        matrix = parent.Matrix
        other = [
            c + radius * d for c, d in zip(center, [matrix.Xx, matrix.Xy, matrix.Xz], strict=True)
        ]
        mapper = U.UFSession.GetUFSession().View.MapModelToDrawing
        return [self._sheet_point3d(sheet, mapper(parent.Tag, p)) for p in [center, other]]

    def _refresh_detail_boundaries(self):
        import json

        import NXOpen.UF as U

        part = self._work_part()
        managed = [
            (sheet, view)
            for sheet in getattr(part, "DrawingSheets", [])
            for view in sheet.GetDraftingViews()
            if view.HasUserAttribute(
                "NX_MCP_DETAIL_V1", self.nxopen.NXObject.AttributeType.String, -1
            )
        ]
        if not managed:
            return
        with self._drawing_save_context(part, force_display=True):
            for sheet, view in managed:
                self._open_drawing_sheet(sheet)
                record = json.loads(view.GetStringAttribute("NX_MCP_DETAIL_V1"))
                tag = int(U.UFSession.GetUFSession().Tag.AskTagOfHandle(record["parent"]))
                parent = next((v for v in sheet.GetDraftingViews() if int(v.Tag) == tag), None)
                if parent is None:
                    raise NXToolError(
                        "NX_STALE_ANNOTATION_SOURCE", "Detail-view parent no longer resolves"
                    )
                points = self._detail_boundary_points(
                    parent, sheet, record["center"], record["radius"]
                )
                builder = part.DraftingViews.CreateDetailViewBuilder(view)
                try:
                    builder.BoundaryPoint1.SetCoordinates(points[0])
                    builder.BoundaryPoint2.SetCoordinates(points[1])
                    builder.Commit()
                finally:
                    builder.Destroy()
