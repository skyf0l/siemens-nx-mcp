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
