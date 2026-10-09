"""Reproducible views: camera set and restore, temporary presentation."""

from __future__ import annotations

import contextlib

from nx_mcp.legacy_kit import finite, vector
from nx_mcp.runtime import NXToolError


class ReviewToolsMixin:
    def _set_camera(self, rotation, origin, scale):
        view = self._visual_part().ModelingViews.WorkView
        matrix = self._validate_rotation(rotation)
        point = vector(origin, "origin")
        value = finite(scale, "scale", True)
        self._require_api(view, "SetRotationTranslationScale")
        view.SetRotationTranslationScale(
            self._nx_matrix(matrix), self.nxopen.Point3d(*point), value
        )
        view.UpdateDisplay()
        return self._view_info()

    def _restore_camera(self, camera):
        self._set_camera(camera["rotation"], camera["origin"], camera["scale"])
        styles = {
            "shaded": "Shaded",
            "shaded_with_edges": "ShadedWithEdges",
            "wireframe": "StaticWireframe",
        }
        if camera["rendering_style"] in styles:
            self._work_part().ModelingViews.WorkView.RenderingStyle = getattr(
                self.nxopen.View.RenderingStyleType, styles[camera["rendering_style"]]
            )

    @contextlib.contextmanager
    def _temporary_view(self):
        camera = self._view_info()
        snapshot_keys = set(getattr(self, "_display_snapshots", {}))
        mark = self.session.SetUndoMark(
            self.nxopen.Session.MarkVisibility.Invisible, "NX MCP temporary presentation"
        )
        try:
            yield
        finally:
            try:
                self.session.UndoToMark(mark, None)
                self.session.DeleteUndoMark(mark, None)
            except Exception as exc:
                raise NXToolError(
                    "NX_ROLLBACK_FAILED",
                    "Temporary presentation cleanup failed: " + str(exc),
                    details={"mutation_outcome": "partial"},
                ) from exc
            finally:
                for key in set(getattr(self, "_display_snapshots", {})) - snapshot_keys:
                    self._display_snapshots.pop(key, None)
                self._restore_camera(camera)
                self._clear_highlights()
