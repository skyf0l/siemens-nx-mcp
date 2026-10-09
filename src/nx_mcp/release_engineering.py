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

    def _geometry_anchor(self, object):
        import NXOpen.UF as U

        obj = self._resolve(object, {"body", "face", "edge"})
        if obj.IsOccurrence or obj.OwningPart != self._work_part():
            raise NXToolError(
                "NX_OBJECT_OWNER_MISMATCH", "Anchor owned prototype geometry in its work part"
            )
        kind = (
            "body"
            if isinstance(obj, self.nxopen.Body)
            else "face"
            if isinstance(obj, self.nxopen.Face)
            else "edge"
        )
        part = self._work_part()
        if not part.FullPath:
            raise NXToolError("NX_UNSAVED_PART", "Save the part before creating persistent anchors")
        return {
            "anchor": {
                "version": 1,
                "owner_part": part.FullPath,
                "kind": kind,
                "handle": U.UFSession.GetUFSession().Tag.AskHandleFromTag(obj.Tag),
            },
            "semantics": "native persistent identity, survives save/reopen while entity survives; no geometric nearest-neighbor fallback",
        }

    def _resolve_geometry_anchor(self, anchor):
        import NXOpen.UF as U

        if (
            not isinstance(anchor, dict)
            or set(anchor) != {"version", "owner_part", "kind", "handle"}
            or anchor["version"] != 1
            or anchor["kind"] not in {"body", "face", "edge"}
            or not all(isinstance(anchor[k], str) and anchor[k] for k in ["owner_part", "handle"])
        ):
            raise NXToolError("NX_INVALID_ARGUMENT", "Use an intact native geometry anchor")
        part = self._work_part()
        if anchor["owner_part"].casefold() != part.FullPath.casefold():
            raise NXToolError("NX_OBJECT_OWNER_MISMATCH", "Anchor belongs to another part")
        try:
            tag = int(U.UFSession.GetUFSession().Tag.AskTagOfHandle(anchor["handle"]))
            objects = (
                list(part.Bodies)
                if anchor["kind"] == "body"
                else [
                    x
                    for b in part.Bodies
                    for x in (b.GetFaces() if anchor["kind"] == "face" else b.GetEdges())
                ]
            )
            obj = next(o for o in objects if int(o.Tag) == tag)
        except Exception as error:
            raise NXToolError(
                "NX_STALE_REFERENCE",
                "Anchored entity no longer exists; explicitly select replacement geometry",
            ) from error
        return {
            "object": self._reference(obj, anchor["kind"], part, "Anchored geometry"),
            "anchor": anchor,
            "resolution": "native persistent handle and owner/type verification",
        }

    def _update_assembly_documentation(self):
        import NXOpen.Positioning as P
        import NXOpen.UF as U

        part = self._explosion_context()
        constraints = self._assembly_constraints(part)
        if constraints:
            positioner = part.ComponentAssembly.Positioner
            positioner.BeginAssemblyConstraints()
            try:
                network = positioner.EstablishNetwork()
                network.MoveObjectsState = True
                network.Solve()
                network.ApplyToModel()
                self._update_model()
                if any(
                    c.GetConstraintStatus() != P.Constraint.SolverStatus.Solved for c in constraints
                ):
                    raise NXToolError(
                        "NX_CONSTRAINT_UNSATISFIED",
                        "Assembly constraints did not solve; documentation refresh rolled back",
                    )
            finally:
                positioner.ClearNetwork()
                positioner.EndAssemblyConstraints()
        self._update_model()
        for ex in self._explosions(part):
            self._refresh_explosion_traces(ex)
        uf = U.UFSession.GetUFSession()
        boms = list(part.Annotations.PartsLists)
        for bom in boms:
            uf.Plist.Update(bom.Tag)
        annotation_result = self._refresh_annotations()
        views = [v for s in part.DrawingSheets for v in s.GetDraftingViews()]
        if views:
            with self._drawing_save_context(part, force_display=True):
                for sheet in part.DrawingSheets:
                    self._open_drawing_sheet(sheet)
                    owned_views = list(sheet.GetDraftingViews())
                    if owned_views:
                        part.DraftingViews.UpdateViews(owned_views)
                        for view in owned_views:
                            view.UpdateAutomaticViewBound()
        retained_annotations = [
            self._reference(a, "annotation", part, "Retained annotation")
            for a in self._documentation_annotations(part)
            if getattr(a, "IsRetained", False)
        ]
        retained_dimensions = any(d.IsRetained for d in part.Dimensions)
        return {
            "constraints": [self._assembly_constraint_record(c) for c in constraints],
            "parts_lists": [
                self._parts_list_info(self._reference(b, "annotation", part, "BOM")["id"])
                for b in boms
            ],
            "views": [
                self._drawing_view_info(self._reference(v, "drawing_view", part, "View")["id"])
                for v in views
            ],
            "annotations": annotation_result,
            "dimensions": self._list_dimensions()["dimensions"],
            "retained_annotations": retained_annotations,
            "documentation_complete": not retained_dimensions and not retained_annotations,
            "warnings": [
                "Retained dimensions or annotations require explicit reassociation or replacement; rebind dimensions with nx_edit_dimension(edges=...)."
            ]
            if retained_dimensions or retained_annotations
            else [],
            "health": self._model_health(scope="assembly"),
            "semantics": "Explicit assembly update after prototype edits/replacement: solves constraints, refreshes anchored traces, BOMs and native drawing views. Missing managed anchors or unsolved mates roll back this refresh; previous prototype edits remain separate operations.",
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

    def _list_dimensions(self):
        part = self._work_part()
        return {
            "dimensions": [
                {
                    "object": self._reference(d, "dimension", part, "Dimension"),
                    "native_type": type(d).__name__,
                    "computed_value": d.ComputedSize,
                    "retained": bool(d.IsRetained),
                    "measurement_valid": not bool(d.IsRetained),
                    "origin": xyz(d.AnnotationOrigin),
                }
                for d in part.Dimensions
            ],
            "units": self._units(),
            "coordinate_frame": "native annotation frame",
        }
