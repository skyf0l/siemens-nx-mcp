"""Camera of the displayed model view: read it, orient, set explicitly, fit (tool-surface 3.12).

NX 2406: ``origin`` is NX's native view origin (the negated view-space coordinates of
the point at the viewport centre); it round-trips exactly through ``nx_get_camera``.
``rotation`` is row-major, its columns are the view axes in world coordinates (screen
right, screen up, toward the viewer): ``rotation[i][j]`` = world component i of axis j.
The result echoes the same matrix and its columns as ``right``, ``up`` and
``view_from``. Canned orientations (``kit.display.ORIENTATION_FROM``) keep the scale
unless ``fit``; ``view_from`` gives any other direction (e.g. the other isometric
octants). A camera change does not mark the part modified (NX 2406, measured).

``view`` is the canned orientation nearest in look-from direction and ``view_angle`` the
angle in degrees between the two directions (NX keeps ``Trimetric`` as the view name). NX's
isometric looks from (1,-1,1): the rear of a part whose front faces -X (an engine with
its nose at x=0); ``view_from [-1,-1,1]`` shows that front from the same height.
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

ViewOrigin = Annotated[tuple[float, float, float], Doc("NX view origin, as nx_get_camera gives")]
Orientation = Annotated[
    Literal["top", "front", "right", "back", "left", "bottom", "isometric", "trimetric"],
    Doc("Looks from +Z top, -Z bottom, -Y front, +Y back, +X right, -X left, iso (1,-1,1)"),
]
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
    effect="read",
    label="Get camera",
    summary="Read the camera",
    keywords=("view info", "camera", "view orientation", "view matrix", "zoom scale"),
    cost="light",
    example={},
)
def nx_get_camera(tx: Tx) -> CameraData:
    """Read the displayed view: camera rotation, origin, scale and rendering style, or the
    shown drawing sheet. origin is the value nx_set_camera takes back; view is the nearest
    named view, view_angle the degrees off it."""
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
    elif a.get("origin") is not None or a.get("scale") is not None:
        text = "Pan or zoom the view"
    else:
        text = "Fit the view"
    return text + (", fit" if a.get("fit") and not text.startswith("Fit") else "")


@tool(
    effect="view",
    label="Set camera",
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
    scale: Annotated[float, Range(gt=0, le=1e6), Doc("Absolute NX view scale")] | None = None,
    fit: bool = False,
) -> CameraData:
    """Orient the model view (orientation, view_from or rotation), set origin or scale, and/or
    fit; returns restore_id. isometric is NX's (1,-1,1), the rear of a part whose front is at
    min X: view_from [-1,-1,1] shows that front. view = nearest named view, view_angle
    degrees off it."""
    given = [n for n, v in (("orientation", orientation), ("view_from", view_from),
                            ("rotation", rotation)) if v is not None]  # fmt: skip
    if not given and origin is None and scale is None and not fit:
        fail(
            "NX_INVALID_ARGUMENT",
            "Give orientation, view_from, rotation, origin, scale or fit=true",
        )
    if len(given) > 1:
        fail("NX_INVALID_ARGUMENT", f"Give one of {' and '.join(given)}", field="/" + given[-1])
    if up is not None and view_from is None:
        fail("NX_INVALID_ARGUMENT", "up goes with view_from", field="/up")
    if fit and (origin is not None or scale is not None):
        fail("NX_INVALID_ARGUMENT", "fit sets origin and scale itself", field="/fit")
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
    got = display.camera(tx, view)
    display.expect_camera(got, wanted, origin, scale)
    readback.expect(got["scale"] > 0, "camera.scale", scale=got["scale"])
    return {"part": tx.ref(part, "part"), **camera_row(got)}
