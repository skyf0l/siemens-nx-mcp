"""PNG captures of the displayed view: NX viewport export or Studio Image Capture.

    size = capture.studio_png(tx, path, 960, 600, style="shaded_with_edges")
    size = capture.viewport_png(tx, path, 960, 600)        # actual size, see below

NX 2406 findings encoded here:

- ``StudioImageCaptureBuilder`` honours the requested pixel size, also while a
  drawing sheet is shown (letterboxed to the window's aspect).
- ``ImageExportBuilder`` (and UF ``Disp.CreateImage``) rasterise the graphics
  window at its own size and ignore ``DeviceWidth``/``DeviceHeight``: callers
  report the requested and the actual resolution.
- Both export the displayed drawing sheet when one is shown; only the model view
  has a rendering style (``part.ModelingViews.WorkView`` raises on a sheet).
- Every capture goes through these builders, never a desktop screenshot.

The view's rendering style and the lighting preset are restored; a file that is
not a PNG (or, for Studio, not of the requested size) is removed and reported.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from nx_mcp.core.errors import fail, native
from nx_mcp.kit import files

STYLES = {
    "studio": "Studio",
    "shaded": "Shaded",
    "shaded_with_edges": "ShadedWithEdges",
    "wireframe": "StaticWireframe",
}
BACKGROUNDS = {
    "white": "CustomColor",
    "color": "CustomColor",
    "original": "Original",
    "transparent": "Transparent",
}
WHITE = [1.0, 1.0, 1.0]


def _part(tx):
    if tx.session.IsBatch:
        fail("NX_VIEWPORT_UNAVAILABLE", "Captures need the interactive NX session")
    part = tx.session.Parts.Display
    if part is None:
        fail("NX_NO_WORK_PART", "No display part to capture")
    return part


def _background(builder, background, color):
    builder.BackgroundOption = getattr(builder.BackgroundOptions, BACKGROUNDS[background])
    if background in ("white", "color"):
        builder.SetCustomBackgroundColor([float(v) for v in (color or WHITE)])


@contextlib.contextmanager
def _styled(tx, part, style):
    """Apply a rendering style to the model work view for the block (None: unchanged)."""
    if style is None:
        yield
        return
    view = part.ModelingViews.WorkView
    original = view.RenderingStyle
    try:
        with native(default="NX_CAPTURE_FAILED"):
            view.RenderingStyle = getattr(tx.nx.View.RenderingStyleType, STYLES[style])
            view.UpdateDisplay()
        yield
    finally:
        with contextlib.suppress(Exception):
            view.RenderingStyle = original
            view.UpdateDisplay()


@contextlib.contextmanager
def _lighting(part, preset):
    """Select a shaded-view lighting preset (1-5) for the block (None: unchanged)."""
    if preset is None:
        yield
        return
    lights = previous = None
    try:
        with native(default="NX_CAPTURE_FAILED"):
            lights = part.Views.CreateLighting(None)
            previous = lights.LightsShadedViewsLightingCollection
            lights.LightsShadedViewsLightingCollection = getattr(
                lights.LightingCollectionType, f"Lighting{int(preset)}"
            )
            lights.Commit()
        yield
    finally:
        if lights is not None:
            with contextlib.suppress(Exception):
                if previous is not None:
                    lights.LightsShadedViewsLightingCollection = previous
                    lights.Commit()
            with contextlib.suppress(Exception):
                lights.Destroy()


def _written(target):
    size = files.png_size(target) if target.is_file() else None
    if size is None:
        fail("NX_CAPTURE_FAILED", "NX did not write a PNG image")
    return tuple(size)


def studio_png(
    tx,
    path,
    width,
    height,
    *,
    style="shaded_with_edges",
    background="white",
    color=None,
    lighting=None,
):
    """Render the displayed work view (or sheet) to ``path``; returns ``(width, height)``.

    ``style`` None keeps the rendering style (required while a drawing sheet is shown);
    ``background`` is white, original, transparent or color (``color`` = RGB 0..1);
    ``lighting`` selects a shaded-view lighting preset 1-5 for the render.
    """
    part = _part(tx)
    target = Path(path)
    builder = None
    try:
        with _styled(tx, part, style), _lighting(part, lighting):
            try:
                with native(default="NX_CAPTURE_FAILED"):
                    builder = part.Views.CreateStudioImageCaptureBuilder()
                    builder.Source = builder.SourceType.WorkView
                    builder.UnitsEnum = builder.UnitsEnumType.Pixels
                    builder.SetImageDimensionsInteger([int(height), int(width)])
                    builder.NativeFileBrowser = str(target)
                    _background(builder, background, color)
                    builder.Commit()
            finally:
                with contextlib.suppress(Exception):
                    if builder is not None:
                        builder.Destroy()
        size = _written(target)
        if size != (int(width), int(height)):
            fail("NX_CAPTURE_FAILED", f"NX rendered {size}, not {width}x{height}")
        return size
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        raise


def viewport_png(tx, path, width, height, *, style=None, background="white", color=None):
    """Export the displayed graphics window (model view or drawing sheet) to ``path``.

    Returns the actual ``(width, height)``: NX 2406 exports the window at its own
    size, so it can differ from the request. ``style`` None keeps the rendering style.
    """
    part = _part(tx)
    target = Path(path)
    builder = None
    try:
        with _styled(tx, part, style):
            try:
                with native(default="NX_CAPTURE_FAILED"):
                    builder = part.Views.CreateImageExportBuilder()
                    builder.FileName = str(target)
                    builder.FileFormat = builder.FileFormats.Png
                    builder.RegionMode = False
                    builder.DeviceWidth = int(width)
                    builder.DeviceHeight = int(height)
                    _background(builder, background, color)
                    builder.EnhanceEdges = True
                    builder.Commit()
            finally:
                with contextlib.suppress(Exception):
                    if builder is not None:
                        builder.Destroy()
        return _written(target)
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        raise
