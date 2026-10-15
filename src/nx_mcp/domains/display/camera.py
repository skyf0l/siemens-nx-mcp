"""Camera of the displayed model view: read it, orient, set explicitly, fit (tool-surface 3.12).

NX 2406: ``origin`` is NX's native view origin (the negated view-space coordinates of
the point at the viewport centre); it round-trips exactly through ``nx_get_camera``.
``rotation`` is row-major, its columns are the view axes in world coordinates (screen
right, screen up, toward the viewer): ``rotation[i][j]`` = world component i of axis j.
The result echoes the same matrix and its columns as ``right``, ``up`` and
``view_from``. Canned orientations (``kit.display.ORIENTATION_FROM``) keep the scale
unless ``fit``; ``view_from`` gives any other direction (e.g. the other isometric
octants). A camera change does not mark the part modified (NX 2406, measured).

``view`` is the canned orientation nearest in look-from direction when it is within 10
degrees (``common.NAMED_WITHIN``), else ``custom`` with ``nearest_view``; ``view_angle`` is
the angle in degrees between the two directions (NX keeps ``Trimetric`` as the view name).
NX's isometric looks from (1,-1,1): the rear of a part whose front faces -X (an engine with
its nose at x=0); ``view_from [-1,-1,1]`` shows that front from the same height.

``fit`` frames the shown bodies (``kit.display.fit``: ``View.FitToObjects``), not datums,
sketches or the datum CSYS at the absolute origin that NX's own fit counts, and pans the
bodies' box centre to the viewport centre; the result's ``fit`` gives that ``centre`` and
the screen-plane ``offset`` left (part units). With no body shown NX's fit is used.

A new direction (orientation, view_from, rotation) turns the view about the point at the
viewport centre (``kit.display.pivot``), or about ``center`` when given: NX 2406 keeps the
view-space origin when only the rotation changes, which moved a model 35 mm off centre.
``center`` pans an absolute point to the viewport centre; ``style`` sets the rendering
style, which stays after the call (``nx_capture_view`` style=current uses it).

Natural forms: ``orientation`` takes ``iso`` for ``isometric``; ``zoom`` multiplies the
current (or, with ``fit``, the fitted) scale about the view centre, so ``zoom=2`` shows
the model twice as large without knowing NX's absolute ``scale``.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Point3, Range
from nx_mcp.domains.display.common import camera_row
from nx_mcp.kit import display, drawing, geom, readback
from nx_mcp.kit.result import Row

ViewOrigin = Annotated[tuple[float, float, float], Doc("NX view origin, as nx_get_camera")]
Orientation = Annotated[
    Literal["top", "front", "right", "back", "left", "bottom", "isometric", "iso", "trimetric"],
    Doc("Looks from +Z top, -Z bottom, -Y front, +Y back, +X right, -X left, iso (1,-1,1)"),
]
Zoom = Annotated[float, Range(gt=0, le=1000), Doc("x current/fitted scale; 2=closer")]
_Row = tuple[float, float, float]
ViewRotation = Annotated[
    tuple[_Row, _Row, _Row],
    Doc("Row-major; columns = right, up, view_from"),
]
ViewFrom = Annotated[tuple[float, float, float], Doc("Direction to look from, e.g. [-1,-1,1]")]
ViewUp = Annotated[tuple[float, float, float], Doc("Screen up for view_from (default +Z)")]
Style = Literal["shaded", "shaded_with_edges", "wireframe", "studio"]


class FitInfo(TypedDict, total=False):
    bodies: int
    centre: list[float]
    offset: float


class CameraData(TypedDict, total=False):
    part: Row
    fit: FitInfo
    view: str
    nearest_view: str
    view_angle: float
    rotation: list[list[float]]
    view_from: list[float]
    up: list[float]
    right: list[float]
    origin: list[float]
    absolute_origin: list[float]
    scale: float
    style: str
    sheet: Row
    interactive: bool


def _hidden(services, part, args) -> bool:
    """Gate 4s asks only for a target that is not displayed: the camera is that part's
    displayed view, so it is shown for the read and the engineer's tab comes back."""
    return True


@tool(
    target="any",  # never ambiguous: the displayed part, or the named or agent's only part
    display_bound=(_hidden,),
    effect="read",
    label="Get camera",
    summary="Read the camera",
    keywords=(
        "view info",
        "camera",
        "view orientation",
        "current view direction",
        "look direction",
        "view matrix",
        "zoom scale",
    ),
    cost="light",
    example={},
)
def nx_get_camera(tx: Tx) -> CameraData:
    """Read a part's view: camera rotation, origin, scale and rendering style, or the shown
    drawing sheet. Without part, the displayed part's; part X shows X for the read, then
    the display comes back. origin is what nx_set_camera takes back; view is a named view
    or custom, view_angle the degrees off the nearest."""
    return _camera(tx)


def _camera(tx) -> CameraData:
    part = display.shown_part(tx)
    tx.frame = "display_part_absolute"
    out: CameraData = {"part": tx.ref(part, "part"), "interactive": not tx.session.IsBatch}
    if display.sheet_shown(part):
        sheet = drawing.current(part)
        if sheet is not None:
            out["sheet"] = tx.ref(sheet, "drawing_sheet")
        out.update(camera_row(display.camera(tx, part.Views.WorkView), named=False))
        return out
    out.update(camera_row(display.camera(tx, part.ModelingViews.WorkView)))
    return out


def _summary(a, say):
    if a.get("orientation"):
        text = f"View {a['orientation']}"
    elif a.get("view_from") is not None:
        text = "View from (" + ", ".join(f"{float(v):.3g}" for v in a["view_from"]) + ")"
    elif a.get("rotation") is not None:
        text = "Rotate the view"
    elif any(a.get(k) is not None for k in ("origin", "center", "scale", "zoom")):
        text = "Pan or zoom the view"
    elif a.get("style") and not a.get("fit"):
        return f"View style {a['style']}"
    else:
        text = "Fit the view"
    framed = a.get("fit") and not text.startswith("Fit")
    return text + (", fit" if framed else "") + (f", {a['style']}" if a.get("style") else "")


@tool(
    effect="view",
    label="Set camera: orient, zoom, fit view",
    summary=_summary,
    keywords=(
        "fit view",
        "isometric",
        "orient view",
        "rotate view",
        "zoom",
        "center view on point",
        "zoom to object",
        "rendering style",
    ),
    requires=("work_part",),
    cost="light",
    restorable=True,
    example={"orientation": "isometric", "fit": True},
)
def nx_set_camera(
    tx: Tx,
    *,
    orientation: Orientation | None = None,
    view_from: ViewFrom | None = None,
    up: ViewUp | None = None,
    rotation: ViewRotation | None = None,
    origin: ViewOrigin | None = None,
    scale: Annotated[float, Range(gt=0, le=1e6), Doc("Absolute, as nx_get_camera")] | None = None,
    zoom: Zoom | None = None,
    fit: bool = False,
    center: Annotated[Point3, Doc("Absolute point to centre")] | None = None,
    style: Annotated[Style, Doc("Stays set")] | None = None,
) -> CameraData:
    """Orient, pan, zoom or fit a part's model view (part X: shown, then the display comes
    back); returns restore_id. Direction: orientation, view_from (+up) or rotation, turned
    about the view centre. scale is absolute, zoom a factor. fit frames the shown bodies
    centred; zoom only. NX iso looks from (1,-1,1); [-1,-1,1] shows a min-X
    front."""
    given = [n for n, v in (("orientation", orientation), ("view_from", view_from),
                            ("rotation", rotation)) if v is not None]  # fmt: skip
    framing = fit
    if not given and all(v is None for v in (origin, scale, zoom, center, style)) and not framing:
        fail(
            "NX_INVALID_ARGUMENT",
            "Nothing to change: give orientation, view_from, rotation, origin, center, scale, "
            "zoom, style or fit=true",
            hint='e.g. {"orientation": "isometric", "fit": true} or {"zoom": 2}',
        )
    if center is not None and (origin is not None or framing):
        fail(
            "NX_INVALID_ARGUMENT",
            "center places the view centre; origin and fit place it too: give one",
            field="/center",
        )
    if len(given) > 1:
        fail(
            "NX_INVALID_ARGUMENT",
            f"{given[0]} and {given[1]} both set the view direction; give only one",
            field="/" + given[-1],
        )
    if up is not None and view_from is None:
        fail(
            "NX_INVALID_ARGUMENT",
            "up goes with view_from (orientation and rotation fix the screen up)",
            field="/up",
        )
    if framing and (origin is not None or scale is not None):
        fail(
            "NX_INVALID_ARGUMENT",
            "fit sets origin and scale itself; add zoom to scale the fitted view",
            field="/fit",
        )
    if zoom is not None and scale is not None:
        fail(
            "NX_INVALID_ARGUMENT",
            "Give scale (absolute) or zoom (a factor on the current scale), not both",
            field="/zoom",
        )
    if orientation == "iso":
        orientation = "isometric"
    wanted = geom.rotation(rotation, "/rotation") if rotation is not None else None
    if view_from is not None:
        wanted = display.rotation_from(view_from, up)
    with tx.checks():
        part = display.shown_part(tx, work=True)
        view = display.model_view(tx, part)
    tx.frame = "display_part_absolute"
    tx.display.keep_camera()
    if style is not None:
        display.set_style(tx, view, style)
    if orientation is not None:
        wanted = display.ORIENTATION_ROTATIONS[orientation]
    if wanted is not None and origin is None and scale is None:
        # about the view centre (or the given center): NX would keep the view-space origin
        display.pivot(tx, view, wanted, center)
    elif wanted is not None or origin is not None or scale is not None or center is not None:
        now = display.camera(tx, view)
        turned = wanted if wanted is not None else now["rotation"]
        if origin is not None:
            moved = list(origin)
        elif center is not None:
            moved = display.origin_for(turned, center)
        elif wanted is not None:
            moved = display.origin_for(turned, now["absolute_origin"])
        else:
            moved = now["origin"]
        display.set_camera(tx, view, turned, moved, scale if scale is not None else now["scale"])
    fitted = display.fit(tx, view) if framing else None
    if zoom is not None:  # about the view centre: rotation and origin stay
        now = display.camera(tx, view)
        scale = now["scale"] * zoom
        display.set_camera(tx, view, now["rotation"], now["origin"], scale)
    got = display.camera(tx, view)
    display.expect_camera(got, wanted, origin, scale)
    readback.expect(got["scale"] > 0, "camera.scale", scale=got["scale"])
    if center is not None and fitted is None:
        offset = display.centre_offset(got, center)
        readback.expect(offset <= geom.tol(center, got["absolute_origin"]), "camera.center",
                        offset=offset)  # fmt: skip
    if style is not None:
        readback.expect(got["style"] == style, "camera.style", style=got["style"])
    out: CameraData = {"part": tx.ref(part, "part"), **camera_row(got)}
    if fitted is not None:
        out["fit"] = fitted  # type: ignore[typeddict-item]
    return out
