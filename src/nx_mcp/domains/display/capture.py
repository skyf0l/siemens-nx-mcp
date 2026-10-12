"""``nx_capture_view``: a PNG of the displayed model view or drawing sheet.

The PNG is saved in the workspace; the MCP result carries the image inline only with
``inline=true`` or when no ``path`` is given (``image.inline``, which the sidecar proxy
turns into image content), so a capture to a path costs the agent no image tokens.
``view_from`` (with ``up``) aims the camera for this capture only and fits by default;
the camera is put back afterwards. ``hide_sketches`` (default) blanks the shown
finished sketches and datums for the capture under a scratch undo mark: NX 2406
measured, blanking marks the part modified and undoing to the mark restores both the
blank state and the modified flag. Nothing is hidden while a sketch is active. The undo
also clears the modified flag a studio style or lighting would leave (measured), so the
``part_modified`` warning below only appears with ``hide_sketches=false`` or nothing to hide.

NX 2406 findings (``kit.capture``): the viewport export rasterises the graphics window
at its own size. Omitted ``width``/``height`` take that size without a warning; given
ones are honoured: when the window differs, a shaded model view is rendered again at
the exact size by Studio capture in the same style (``quality`` then says ``studio``;
measured: same look, letterboxed to the window's aspect). A wireframe view or a drawing
sheet keeps the window size with an ``NX_APPROXIMATED`` warning. Studio quality on a
drawing sheet is refused: NX 2406 renders the sheet's frame and centrelines but drops the
views' geometry (wireframe drafting curves). Captures never use a desktop screenshot. The rendering style, lighting and (with ``fit``) the
camera are put back afterwards, so the engineer's view is unchanged and the result's
``mutation_outcome`` is ``not_applicable``, as for a read. NX 2406 measured: viewport
and Studio captures in the current style, and a viewport capture in another style, leave
the part unmodified; ``style=studio`` and ``lighting`` mark it modified although both are
put back (NX stores them in the part), which ``marks_part_modified`` (true only when this
capture set the flag; a part modified before stays so) and a warning report.
``hidden_sketches`` counts the sketches and datums blanked for the capture only.

``sheet`` (a sheet ref or name of the shown part) captures that drawing sheet: it is
opened for the capture only and the part's previous sheet or model view comes back
(``kit.drawing.on_sheet``), so no ``nx_activate_drawing_sheet`` is needed first. Without
``sheet`` the capture shows what the part displays; when that is the model view of a part
with drawing sheets, an ``NX_NOTE`` names them (benchmark bench3p: a capture of a part
with a sheet silently returned the model view).

Natural forms: ``background`` defaults to ``color`` when ``color`` is given (else white)
and takes ``black``; ``color`` takes 0..1 or 0..255 components (any above 1: 0..255).

``path`` never overwrites unless ``overwrite=true``: then an existing PNG there is
replaced. ``path`` passes the kernel's ``replace`` path gate on both process boundaries
(inside the workspace, not reserved, ``.png``, a file and not a directory); the old PNG
is replaced atomically only after the new one is complete (``Files.publish``), and a
file there that is not a PNG is refused. ``overwritten`` says whether a file was replaced.
"""

from __future__ import annotations

import contextlib
import os
import re
import uuid
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import REF_PATTERN, Doc, PathTo, Range, RefKind, Text, Vec3
from nx_mcp.domains.display.common import Camera, camera_row
from nx_mcp.kit import capture, display, drawing, files
from nx_mcp.kit.result import Row

Pixels = Annotated[int, Range(ge=128, le=4096)]
SHADED = ("shaded", "shaded_with_edges", "studio")  # styles a Studio capture renders
Rgb = Annotated[tuple[float, float, float], Doc("RGB 0..1 or 0..255")]
Background = Literal["white", "black", "original", "transparent", "color"]
# One string (a ref sh:N pins the part, a name is looked up in it): a union of the two
# string forms would double the listed schema of this directly listed core tool.
SheetArg = Annotated[str, Text(128), RefKind("drawing_sheet"), Doc("Sheet ref/name to capture")]
LISTED_SHEETS = 5


class ImageInfo(TypedDict):
    path: str
    sha256: str
    inline: bool
    mime_type: str
    width: int
    height: int


class CaptureData(TypedDict, total=False):
    path: str
    overwritten: bool
    marks_part_modified: bool
    size: int
    sha256: str
    image: ImageInfo
    quality: str
    requested: list[int]
    resolution: list[int]
    style: str
    camera: Camera
    hidden_sketches: int
    sheet: Row


def _background(background, color) -> tuple[str, tuple[float, float, float] | None]:
    """``(background, color)`` from the natural forms: ``black``, a colour without a
    background, 0..255 components."""
    if background == "black":
        if color is not None:
            fail("NX_INVALID_ARGUMENT", "color goes with background=color", field="/color")
        return "color", (0.0, 0.0, 0.0)
    if background is None:
        background = "white" if color is None else "color"
    if background == "color" and color is None:
        fail("NX_INVALID_ARGUMENT", "background=color needs color [r, g, b]", field="/color")
    if background != "color" and color is not None:
        fail(
            "NX_INVALID_ARGUMENT",
            f"color goes with background=color, not {background}; or leave background out",
            field="/color",
        )
    if color is None:
        return background, None
    values = [float(c) for c in color]
    if any(v > 1.0 for v in values) and all(0.0 <= v <= 255.0 for v in values):
        values = [v / 255.0 for v in values]
    if not all(0.0 <= v <= 1.0 for v in values):
        fail("NX_INVALID_ARGUMENT", "color components are 0..1 or 0..255", field="/color")
    return background, (values[0], values[1], values[2])


def _studio_over(tx, temp, width, height, style, background, color):
    """Render the model view at the exact size over the viewport PNG at ``temp``; the
    viewport PNG stays when Studio fails. Returns the size or None."""
    studio = temp.with_name(temp.stem + ".studio.png")
    try:
        size = capture.studio_png(
            tx, studio, width, height, style=style, background=background, color=color
        )
    except Exception:  # NX could not render it: keep the window-size image
        return None
    os.replace(studio, temp)
    return size


def _sheet(tx, part, value):
    """The drawing sheet of the shown ``part`` that ``value`` names (ref or name, any case)."""
    text = str(value).strip()
    sheets = list(getattr(part, "DrawingSheets", None) or [])
    if re.match(REF_PATTERN, text):
        found = tx.resolve(text, "drawing_sheet")
        if any(int(x.Tag) == int(found.Tag) for x in sheets):
            return found
        fail("NX_WRONG_OWNER", f"{text} is not a drawing sheet of the captured part",
             field="/sheet")  # fmt: skip
    named = [x for x in sheets if str(x.Name).casefold() == text.casefold()]
    if not named:
        fail(
            "NX_NOT_FOUND",
            f"The part has no drawing sheet named {text!r}",
            field="/sheet",
            hint="Pass a sheet ref (sh:N) or a name from details.sheets",
            details={"sheets": [tx.ref(x, "drawing_sheet") for x in sheets[:20]]},
        )
    return named[0]


def _sheets_note(tx, part) -> None:
    """Name the drawing sheets a model-view capture of ``part`` leaves out."""
    with contextlib.suppress(Exception):
        sheets = list(getattr(part, "DrawingSheets", None) or [])
        if sheets:
            rows = [tx.ref(x, "drawing_sheet") for x in sheets[:LISTED_SHEETS]]
            names = ", ".join(f"{r['name']} ({r['id']})" for r in rows)
            tx.warn(
                "NX_NOTE",
                f"Captured the model view; the part also has drawing sheet(s) {names}: "
                "pass sheet to capture one",
            )


def _summary(a, say):
    what = "Render" if a.get("quality") == "studio" else "Capture"
    shown = f"sheet {say('sheet')}" if a.get("sheet") else "the view"
    if a.get("overwrite") and a.get("path"):
        return f"{what} {shown} over {say('path')}"
    return f"{what} {shown}" + (f" to {say('path')}" if a.get("path") else "")


@tool(
    target="display",
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
    path: Annotated[str, PathTo("replace", ".png")] | None = None,
    overwrite: bool = False,
    inline: bool | None = None,
    view_from: Vec3 | None = None,
    up: tuple[float, float, float] | None = None,  # a direction like view_from
    fit: bool | None = None,
    hide_sketches: bool = True,
    width: Pixels = 1024,
    height: Pixels = 768,
    background: Background | None = None,
    color: Rgb | None = None,
    style: Literal["current", "shaded", "shaded_with_edges", "wireframe", "studio"] = "current",
    lighting: Annotated[int, Range(ge=1, le=5)] | None = None,
    sheet: SheetArg | None = None,
) -> CaptureData:
    """PNG of the shown view or a sheet, to path or captures/; inline only with inline=true
    or no path. view_from/up aim it."""
    if overwrite and path is None:
        fail("NX_INVALID_ARGUMENT", "overwrite applies to path; give path", field="/overwrite")
    if up is not None and view_from is None:
        fail("NX_INVALID_ARGUMENT", "up goes with view_from", field="/up")
    aim = display.rotation_from(view_from, up) if view_from is not None else None
    fit = aim is not None if fit is None else fit
    replacing = path is not None and Path(path).exists()
    if replacing and not overwrite:
        fail(
            "NX_ALREADY_EXISTS",
            "/path: a file already exists there; set overwrite=true to replace it",
            field="/path",
            details={"path": path},
        )
    backdrop, rgb = _background(background, color)
    if lighting is not None and quality != "studio":
        fail("NX_INVALID_ARGUMENT", "lighting needs quality=studio", field="/lighting")
    if quality == "studio" and style == "wireframe":
        fail(
            "NX_UNSUPPORTED_OPTION",
            "quality=studio renders shaded styles only; use quality=viewport for wireframe",
            field="/style",
        )
    with tx.checks():
        part = display.shown_part(tx)
        wanted = _sheet(tx, part, sheet) if sheet is not None else None
        on_sheet = wanted is not None or display.sheet_shown(part)
        if on_sheet and style != "current":
            fail(
                "NX_UNSUPPORTED_OPTION",
                "A drawing sheet is captured as displayed; use style=current",
                field="/style",
            )
        if on_sheet and aim is not None:
            fail("NX_UNSUPPORTED_OPTION", "A drawing sheet has no 3D camera", field="/view_from")
        if on_sheet and quality == "studio":
            fail(
                "NX_UNSUPPORTED_OPTION",
                "Studio drops a drawing sheet's view geometry: capture sheets with "
                "quality=viewport (the window size)",
                field="/quality",
            )
        if (fit or aim is not None) and not on_sheet:
            display.shown_part(tx, work=True)  # the kept camera is restored in the work part
        if replacing and files.png_size(Path(str(path))) is None:
            fail(
                "NX_UNSUPPORTED_FILE_TYPE",
                "overwrite replaces PNG images only; the file there is not a PNG",
                field="/path",
            )
    exact = tx.given("width") or tx.given("height")
    root = Path(tx.workspace.root)
    target = Path(path) if path else root / "captures" / f"view-{uuid.uuid4().hex[:12]}.png"
    temp = tx.files.stage(target, replace=overwrite)
    out: CaptureData = {"quality": quality, "requested": [width, height]}
    was_modified = bool(part.IsModified)
    shown = drawing.on_sheet(tx, wanted, part) if wanted is not None else contextlib.nullcontext()
    with shown:  # a sheet asked for: opened for the capture, the previous display comes back
        size, why = _render(
            tx, part, out, temp, on_sheet=on_sheet, fit=fit, aim=aim, quality=quality,
            style=style, backdrop=backdrop, rgb=rgb, lighting=lighting,
            hide_sketches=hide_sketches, exact=exact, width=width, height=height,
        )  # fmt: skip
    if not on_sheet:
        _sheets_note(tx, part)
    if tx.display.kept:
        tx.display.restore()  # the fitted camera goes back: the engineer's view is unchanged
    published = tx.files.publish()[0]  # with overwrite: atomic replace of the old PNG
    meta = files.metadata(published, root)
    if exact and list(size) != [width, height]:
        tx.warn(
            "NX_APPROXIMATED",
            f"NX exported the graphics window at {size[0]}x{size[1]}, not {width}x{height}: "
            f"{why} is captured at the window size",
        )
    if not was_modified and bool(part.IsModified):
        tx.warn(
            "NX_UPDATE_WARNING",
            "NX marked the part modified (studio style or lighting is stored in the part); "
            "the view and model are unchanged",
        )
    tx.outcome = "not_applicable"  # nothing in NX changed: reported like a read
    out.update(
        {
            **meta,
            "overwritten": published in tx.files.replaced,
            "marks_part_modified": not was_modified and bool(part.IsModified),
            "resolution": list(size),
            "style": style,
            "image": {
                "path": meta["path"],
                "sha256": meta["sha256"],
                "inline": path is None if inline is None else inline,
                "mime_type": "image/png",
                "width": size[0],
                "height": size[1],
            },
        }
    )
    return out


def _render(tx, part, out, temp, *, on_sheet, fit, aim, quality, style, backdrop, rgb,
            lighting, hide_sketches, exact, width, height):  # fmt: skip
    """Write the shown sheet or model view to ``temp``: ``(size, what was captured)``."""
    why = "a drawing sheet" if on_sheet else "a wireframe view"
    if on_sheet:
        current = drawing.current(part)
        if current is not None:
            out["sheet"] = tx.ref(current, "drawing_sheet")
        view = part.Views.WorkView
        if fit:
            view.Fit()
    else:
        view = part.ModelingViews.WorkView
        if fit or aim is not None:
            tx.display.keep_camera()
        if aim is not None:
            now = display.camera(tx, view)
            display.set_camera(tx, view, aim, now["origin"], now["scale"])
        if fit:
            view.Fit()
            view.UpdateDisplay()
        out["camera"] = camera_row(display.camera(tx, view))
        tx.frame = "display_part_absolute"
    chosen = None if style == "current" else style
    hide = display.capture_clutter(tx, part) if hide_sketches and not on_sheet else []
    out["hidden_sketches"] = len(hide)
    with tx.scratch() if hide else contextlib.nullcontext():
        try:
            for obj in hide:
                obj.Blank()
            if hide:
                view.UpdateDisplay()
            if quality == "studio":
                size = capture.studio_png(
                    tx, temp, width, height, style=chosen, background=backdrop,
                    color=rgb, lighting=lighting,
                )  # fmt: skip
            else:
                size = capture.viewport_png(
                    tx, temp, width, height, style=chosen, background=backdrop, color=rgb
                )
                shown = chosen or out.get("camera", {}).get("style")
                if exact and list(size) != [width, height] and not on_sheet and shown in SHADED:
                    studio = _studio_over(tx, temp, width, height, chosen, backdrop, rgb)
                    if studio is not None:
                        size, out["quality"] = studio, "studio"
                    else:
                        why = "NX's Studio render failed, so the view"
        finally:
            for obj in hide:  # the scratch undo restores them too (and the modified flag)
                with contextlib.suppress(Exception):
                    obj.Unblank()
    if hide:
        view.UpdateDisplay()
    return size, why
