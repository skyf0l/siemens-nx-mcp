"""``nx_capture_view``: a PNG of the displayed model view or drawing sheet, delivered inline.

NX 2406 findings (``kit.capture``): the viewport export rasterises the graphics window
at its own size, so ``resolution`` can differ from ``requested`` (a warning says so);
Studio quality renders exactly the requested size, also for drawing sheets. Captures
never use a desktop screenshot. The rendering style, lighting and (with ``fit``) the
camera are put back afterwards, so the engineer's view is unchanged.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, PathTo, Range
from nx_mcp.domains.display.common import Camera, camera_row
from nx_mcp.kit import capture, display, drawing, files
from nx_mcp.kit.result import Row

Pixels = Annotated[int, Range(ge=128, le=4096)]
Rgb = Annotated[
    tuple[
        Annotated[float, Range(ge=0, le=1)],
        Annotated[float, Range(ge=0, le=1)],
        Annotated[float, Range(ge=0, le=1)],
    ],
    Doc("RGB 0..1"),
]


class ImageInfo(TypedDict):
    path: str
    sha256: str
    inline: bool
    mime_type: str
    width: int
    height: int


class CaptureData(TypedDict, total=False):
    path: str
    size: int
    sha256: str
    image: ImageInfo
    quality: str
    requested: list[int]
    resolution: list[int]
    style: str
    camera: Camera
    sheet: Row


def _summary(a, say):
    what = "Render" if a.get("quality") == "studio" else "Capture"
    return f"{what} the view" + (f" to {say('path')}" if a.get("path") else "")


@tool(
    effect="view",
    label="Capture view",
    summary=_summary,
    keywords=("screenshot", "render", "image", "png", "picture", "snapshot of view"),
    requires=("interactive",),
    cost="heavy",
    example={"quality": "viewport"},
)
def nx_capture_view(
    tx: Tx,
    *,
    quality: Literal["viewport", "studio"] = "viewport",
    path: Annotated[str, PathTo("new", ".png")] | None = None,
    width: Pixels = 1024,
    height: Pixels = 768,
    background: Literal["white", "original", "transparent", "color"] = "white",
    color: Rgb | None = None,
    style: Literal["current", "shaded", "shaded_with_edges", "wireframe", "studio"] = "current",
    fit: bool = False,
    lighting: Annotated[int, Range(ge=1, le=5), Doc("Studio light preset")] | None = None,
) -> CaptureData:
    """Capture the displayed view or drawing sheet as an inline PNG. studio renders the exact
    size; viewport exports the window at its own size. The view is left as it was."""
    if (background == "color") != (color is not None):
        fail("NX_INVALID_ARGUMENT", "color goes with background=color", field="/color")
    if lighting is not None and quality != "studio":
        fail("NX_INVALID_ARGUMENT", "lighting needs quality=studio", field="/lighting")
    if quality == "studio" and style == "wireframe":
        fail("NX_UNSUPPORTED_OPTION", "Studio quality renders shaded styles", field="/style")
    with tx.checks():
        part = display.shown_part(tx)
        sheet = display.sheet_shown(part)
        if sheet and style != "current":
            fail(
                "NX_UNSUPPORTED_OPTION",
                "A drawing sheet is captured as displayed; use style=current",
                field="/style",
            )
        if fit and not sheet:
            display.shown_part(tx, work=True)  # the kept camera is restored in the work part
    root = Path(tx.workspace.root)
    target = Path(path) if path else root / "captures" / f"view-{uuid.uuid4().hex[:12]}.png"
    temp = tx.files.stage(target)
    out: CaptureData = {"quality": quality, "requested": [width, height]}
    if sheet:
        current = drawing.current(part)
        if current is not None:
            out["sheet"] = tx.ref(current, "drawing_sheet")
        view = part.Views.WorkView
        if fit:
            view.Fit()
    else:
        view = part.ModelingViews.WorkView
        if fit:
            tx.display.keep_camera()
            view.Fit()
            view.UpdateDisplay()
        out["camera"] = camera_row(display.camera(tx, view))
        tx.frame = "display_part_absolute"
    chosen = None if style == "current" else style
    if quality == "studio":
        size = capture.studio_png(
            tx, temp, width, height, style=chosen, background=background, color=color,
            lighting=lighting,
        )  # fmt: skip
    else:
        size = capture.viewport_png(
            tx, temp, width, height, style=chosen, background=background, color=color
        )
    if tx.display.kept:
        tx.display.restore()  # the fitted camera goes back: the engineer's view is unchanged
    published = tx.files.publish()[0]
    meta = files.metadata(published, root)
    if list(size) != [width, height]:
        tx.warn(
            "NX_APPROXIMATED",
            f"NX exported the graphics window at {size[0]}x{size[1]}, not {width}x{height}; "
            "use quality=studio for an exact size",
        )
    out.update(
        {
            **meta,
            "resolution": list(size),
            "style": style,
            "image": {
                "path": meta["path"],
                "sha256": meta["sha256"],
                "inline": True,
                "mime_type": "image/png",
                "width": size[0],
                "height": size[1],
            },
        }
    )
    return out
