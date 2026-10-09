"""Native named explosions and associative assembly drawing views on NX 2606."""

from __future__ import annotations

from contextlib import contextmanager

from nx_mcp.runtime import NXToolError


class ExplodedViewsMixin:
    @contextmanager
    def _drawing_save_context(self, part, force_display=False):
        """Display native sheets for CGM-preserving saves, then restore the view."""
        sheets = list(getattr(part, "DrawingSheets", []))
        if not sheets or (not part.SaveOptions.DrawingCgmData and not force_display):
            yield
            return
        work, display = self.session.Parts.Work, self.session.Parts.Display
        if self.session.ActiveSketch:
            raise NXToolError(
                "NX_SKETCH_ACTIVE", "Finish the sketch before saving drawing preview data"
            )
        original_sheet = part.DrawingSheets.CurrentDrawingSheet
        changed_part = work != part or display != part
        try:
            if changed_part:
                self._activate_part(self._reference(part, "part", part, "Part")["id"], True, True)
            self._open_drawing_sheet(original_sheet or sheets[0], part)
            yield
        finally:
            try:
                if original_sheet is None:
                    self._close_drawing_sheets(part)
                elif part.DrawingSheets.CurrentDrawingSheet != original_sheet:
                    self._open_drawing_sheet(original_sheet, part)
                if changed_part:
                    if display:
                        self._activate_part(
                            self._reference(display, "part", display, "Part")["id"], False, True
                        )
                    if work:
                        self._activate_part(
                            self._reference(work, "part", work, "Part")["id"], True, False
                        )
            except Exception as error:
                raise NXToolError(
                    "NX_SAVE_VIEW_RESTORE_FAILED",
                    "Save presentation could not be restored",
                    details={"mutation_outcome": "partial", "restore_error": str(error)},
                ) from error

    def _save_component_drawing_previews(self, part):
        seen = set()
        for component, _ in reversed(self._walk_components(part)):
            prototype = component.Prototype
            if int(prototype.Tag) in seen:
                continue
            seen.add(int(prototype.Tag))
            if prototype.IsModified and list(getattr(prototype, "DrawingSheets", [])):
                with self._drawing_save_context(prototype):
                    status = prototype.Save(
                        self.nxopen.BasePart.SaveComponents.FalseValue,
                        self.nxopen.BasePart.CloseAfterSave.FalseValue,
                    )
                    if status:
                        status.Dispose()

    @staticmethod
    def _modeling_views(part):
        views = getattr(part, "ModelingViews", [])
        return list(views) if hasattr(views, "__iter__") else []

    def _explosions(self, part):
        return list(getattr(part.ComponentAssembly, "Explosions", []))

    def _explosion(self, reference):
        part = self._work_part()
        explosion = self._resolve(reference, {"explosion"})
        if explosion.OwningPart != part:
            raise NXToolError("NX_OBJECT_OWNER_MISMATCH", "Explosion must belong to the work part")
        return explosion

    def _explosion_context(self):
        part = self._work_part()
        if self.session.Parts.Display != part:
            raise NXToolError("NX_PART_CONTEXT", "Activate the same work and display part")
        if self.session.ActiveSketch:
            raise NXToolError("NX_SKETCH_ACTIVE", "Finish the active sketch first")
        return part

    @staticmethod
    def _explosion_uf():
        import NXOpen.UF

        return NXOpen.UF.UFSession.GetUFSession().Assem

    def _explosion_views(self, explosion):
        uf = self._explosion_uf()
        part = self._work_part()
        result = []
        for kind, views in [
            ("modeling", self._modeling_views(part)),
            ("drawing", part.DraftingViews),
        ]:
            for view in views:
                if int(uf.AskViewExplosion(view.Tag) or 0) == int(explosion.Tag):
                    result.append(
                        {
                            "name": view.Name,
                            "kind": kind,
                            "object": self._reference(
                                view,
                                "drawing_view" if kind == "drawing" else "modeling_view",
                                part,
                                "View",
                            ),
                        }
                    )
        return result

    def _delete_explosion(self, explosion):
        self._explosion_context()
        ex = self._explosion(explosion)
        views = self._explosion_views(ex)
        if views:
            raise NXToolError(
                "NX_EXPLOSION_IN_USE",
                "Detach this explosion from its views before deletion",
                details={"views": views},
            )
        ref = self._reference(ex, "explosion", self._work_part(), "Explosion")
        ex.Delete()
        return {"deleted": [ref]}
