"""Native geometry inspection and viewport export, verified against installed NX APIs."""

from __future__ import annotations

import hashlib
import struct
import uuid

from nx_mcp.kit.geom import rows, xyz
from nx_mcp.runtime import NXToolError, api_error


def with_cleanup_failure(original, cleanup_error):
    """The original error, with a later cleanup/rollback failure in its details."""
    error = original if isinstance(original, NXToolError) else api_error(original)
    if isinstance(cleanup_error, NXToolError):
        failure = {"code": cleanup_error.code, "message": cleanup_error.message}
    else:
        failure = {"code": "NX_API_ERROR", "message": str(cleanup_error)}
    error.details.update(rollback_error=failure, mutation_outcome="partial")
    return error


class InspectionMixin:
    def _view_info(self):
        part = self.session.Parts.Display
        if part is None:
            raise NXToolError("NX_NO_DISPLAY_PART", "Open a display part first")
        view = part.ModelingViews.WorkView
        return {
            "part": self._reference(part, "part", part, "Display part"),
            "name": view.Name,
            "rotation": rows(view.Matrix),
            "origin": xyz(view.Origin),
            "absolute_origin": xyz(view.AbsoluteOrigin),
            "scale": view.Scale,
            "coordinate_frame": "display_part",
            "matrix_layout": "row-major 3x3; columns are NX view axes",
            "rendering_style": next(
                (
                    name
                    for name, member in [
                        ("shaded", self.nxopen.View.RenderingStyleType.Shaded),
                        (
                            "studio",
                            getattr(self.nxopen.View.RenderingStyleType, "Studio", object()),
                        ),
                        ("shaded_with_edges", self.nxopen.View.RenderingStyleType.ShadedWithEdges),
                        ("wireframe", self.nxopen.View.RenderingStyleType.StaticWireframe),
                    ]
                    if member == view.RenderingStyle
                ),
                "nx_style_" + str(view.RenderingStyle),
            ),
            "interactive": not self.session.IsBatch,
        }

    def _capture_view(
        self,
        path=None,
        width=1600,
        height=1000,
        background="white",
        style="shaded_with_edges",
        fit=False,
    ):
        if self.session.IsBatch:
            raise NXToolError(
                "NX_VIEWPORT_UNAVAILABLE",
                "Viewport export requires the interactive NX bridge; no desktop capture fallback",
            )
        if (
            type(width) is not int
            or type(height) is not int
            or not (128 <= width <= 4096 and 128 <= height <= 4096)
        ):
            raise NXToolError("NX_INVALID_ARGUMENT", "width and height must be 128–4096 pixels")
        if background not in {"white", "original", "transparent"} or style not in {
            "current",
            "shaded",
            "shaded_with_edges",
            "wireframe",
        }:
            raise NXToolError("NX_INVALID_ARGUMENT", "Unsupported background or rendering style")
        file = (
            self.workspace.ensure_inside(path)
            if path
            else self.workspace.root / "captures" / ("nx-" + uuid.uuid4().hex + ".png")
        )
        if file.suffix.lower() != ".png":
            raise NXToolError("NX_INVALID_ARGUMENT", "Viewport export requires a .png path")
        if file.exists():
            raise NXToolError("NX_FILE_EXISTS", "Choose a new capture path")
        file.parent.mkdir(parents=True, exist_ok=True)
        part = self.session.Parts.Display
        if not part:
            raise NXToolError("NX_NO_DISPLAY_PART", "Open a display part first")
        from nx_mcp.release_engineering import drawing_work_view

        sheet = getattr(getattr(part, "DrawingSheets", None), "CurrentDrawingSheet", None)
        if sheet is not None or drawing_work_view(part):
            return self._capture_drawing_sheet(
                part, sheet, file, width, height, background, style, fit
            )
        view = part.ModelingViews.WorkView
        old_style = view.RenderingStyle
        builder = None
        try:
            styles = {
                "shaded": "Shaded",
                "shaded_with_edges": "ShadedWithEdges",
                "wireframe": "StaticWireframe",
            }
            if style != "current":
                view.RenderingStyle = getattr(self.nxopen.View.RenderingStyleType, styles[style])
            if fit:
                view.Fit()
            view.UpdateDisplay()
            camera = self._view_info()
            builder = part.Views.CreateImageExportBuilder()
            self._commit_image_export(builder, file, width, height, background)
        finally:
            try:
                if builder:
                    builder.Destroy()
            finally:
                view.RenderingStyle = old_style
        data, resolution = self._read_capture(file)
        return {
            "path": str(file),
            "artifact_path": str(file.relative_to(self.workspace.root)),
            "capture_kind": "nx_model_viewport",
            "model_preview": True,
            "resolution": resolution,
            "requested_resolution": [width, height],
            "camera": camera,
            "background": background,
            "rendering": "NX viewport rasterization",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "warnings": (
                []
                if resolution == [width, height]
                else ["NX returned a different resolution than requested"]
            ),
        }

    @staticmethod
    def _commit_image_export(builder, file, width, height, background):
        import NXOpen.Gateway

        builder.FileName = str(file)
        builder.FileFormat = NXOpen.Gateway.ImageExportBuilder.FileFormats.Png
        builder.RegionMode = False
        builder.DeviceWidth = width
        builder.DeviceHeight = height
        backgrounds = NXOpen.Gateway.ImageExportBuilder.BackgroundOptions
        builder.BackgroundOption = {
            "white": backgrounds.CustomColor,
            "original": backgrounds.Original,
            "transparent": backgrounds.Transparent,
        }[background]
        if background == "white":
            builder.SetCustomBackgroundColor([1.0, 1.0, 1.0])
        builder.EnhanceEdges = True
        builder.Commit()

    @staticmethod
    def _read_capture(file):
        data = file.read_bytes()
        if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
            raise NXToolError("NX_CAPTURE_FAILED", "NX did not produce a valid PNG")
        return data, list(struct.unpack(">II", data[16:24]))

    def _capture_drawing_sheet(self, part, sheet, file, width, height, background, style, fit):
        """Export the displayed drawing sheet; NX has no model view while a sheet is shown."""
        warnings = []
        if style != "current":
            warnings.append(
                "style applies to model views; the drawing sheet was exported as displayed"
            )
        view = part.Views.WorkView
        if fit:
            view.Fit()
        builder = part.Views.CreateImageExportBuilder()
        try:
            self._commit_image_export(builder, file, width, height, background)
        finally:
            builder.Destroy()
        data, resolution = self._read_capture(file)
        if resolution != [width, height]:
            warnings.append("NX returned a different resolution than requested")
        if sheet is None:
            warnings.append(
                "NX shows a drawing sheet that is not its current sheet; "
                "call nx_activate_drawing_sheet to make it current"
            )
        return {
            "path": str(file),
            "artifact_path": str(file.relative_to(self.workspace.root)),
            "capture_kind": "nx_drawing_sheet",
            "model_preview": False,
            "drawing": (
                self._reference(sheet, "drawing_sheet", part, "Drawing sheet") if sheet else None
            ),
            "view_name": view.Name,
            "resolution": resolution,
            "requested_resolution": [width, height],
            "camera": None,
            "background": background,
            "rendering": "NX drawing sheet rasterization",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "warnings": warnings,
        }
