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

Natural forms: ``orientation`` takes ``iso`` for ``isometric``; ``zoom`` multiplies the
current (or, with ``fit``, the fitted) scale about the view centre, so ``zoom=2`` shows
the model twice as large without knowing NX's absolute ``scale``.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Range
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


class CameraData(TypedDict, total=False):
    part: Row
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


@tool(
    target="any",  # NX's displayed view: never ambiguous, never switched (a plain read)
    effect="read",
    label="Get camera",
    summary="Read the camera",
    keywords=("view info", "camera", "view orientation", "view matrix", "zoom scale"),
    cost="light",
    example={},
)
def nx_get_camera(tx: Tx) -> CameraData:
    """Read the displayed view: camera rotation, origin, scale and rendering style, or the
    shown drawing sheet. origin is what nx_set_camera takes back; view is a named view or
    custom, view_angle the degrees off the nearest."""
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
    elif any(a.get(k) is not None for k in ("origin", "scale", "zoom")):
        text = "Pan or zoom the view"
    else:
        text = "Fit the view"
    return text + (", fit" if a.get("fit") and not text.startswith("Fit") else "")


@tool(
    effect="view",
    label="Set camera: orient, zoom, fit view",
    summary=_summary,
    keywords=("fit view", "isometric", "orient view", "rotate view", "zoom", "pan", "set view"),
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
) -> CameraData:
    """Orient (orientation, view_from or rotation), pan, zoom or fit the model view; returns
    restore_id. NX iso looks from (1,-1,1); view_from [-1,-1,1] shows a min-X front."""
    given = [n for n, v in (("orientation", orientation), ("view_from", view_from),
                            ("rotation", rotation)) if v is not None]  # fmt: skip
    if not given and origin is None and scale is None and zoom is None and not fit:
        fail(
            "NX_INVALID_ARGUMENT",
            "Nothing to change: give orientation, view_from, rotation, origin, scale, zoom "
            "or fit=true",
            hint='e.g. {"orientation": "isometric", "fit": true} or {"zoom": 2}',
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
    if fit and (origin is not None or scale is not None):
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
    if orientation is not None:
        display.orient(tx, view, orientation, fit=fit)
        wanted = display.ORIENTATION_ROTATIONS[orientation]
    if (wanted is not None and orientation is None) or origin is not None or scale is not None:
        now = display.camera(tx, view)
        display.set_camera(
            tx,
            view,
            wanted if wanted is not None else now["rotation"],
            list(origin) if origin is not None else now["origin"],
            scale if scale is not None else now["scale"],
        )
    if fit and orientation is None:
        view.Fit()
        view.UpdateDisplay()
    if zoom is not None:  # about the view centre: rotation and origin stay
        now = display.camera(tx, view)
        scale = now["scale"] * zoom
        display.set_camera(tx, view, now["rotation"], now["origin"], scale)
    got = display.camera(tx, view)
    display.expect_camera(got, wanted, origin, scale)
    readback.expect(got["scale"] > 0, "camera.scale", scale=got["scale"])
    return {"part": tx.ref(part, "part"), **camera_row(got)}
