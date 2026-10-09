"""Camera of the displayed model view: read it, orient, set explicitly, fit (tool-surface 3.12).

NX 2406: ``origin`` is NX's native view origin (the negated view-space coordinates of
the point at the viewport centre); it round-trips exactly through ``nx_get_camera``.
``rotation`` is row-major, its columns are the view axes (screen right, screen up,
toward the viewer). Canned orientations keep the scale unless ``fit``.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Mat3, Range
from nx_mcp.domains.display.common import camera_row
from nx_mcp.kit import display, drawing, geom, readback
from nx_mcp.kit.result import Row

ViewOrigin = Annotated[tuple[float, float, float], Doc("NX view origin, as nx_get_camera gives")]
Orientation = Literal["top", "front", "right", "back", "left", "bottom", "isometric", "trimetric"]


class CameraData(TypedDict, total=False):
    part: Row
    view: str
    rotation: list[list[float]]
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
    cost="instant",
    example={},
)
def nx_get_camera(tx: Tx) -> CameraData:
    """Read the displayed view: camera rotation, origin, scale and rendering style, or the
    shown drawing sheet. origin is the value nx_set_camera takes back."""
    part = display.shown_part(tx)
    tx.frame = "display_part_absolute"
    out: dict = {"part": tx.ref(part, "part"), "interactive": not tx.session.IsBatch}
    if display.sheet_shown(part):
        sheet = drawing.current(part)
        if sheet is not None:
            out["sheet"] = tx.ref(sheet, "drawing_sheet")
        out.update(camera_row(display.camera(tx, part.Views.WorkView)))
        return out
    out.update(camera_row(display.camera(tx, part.ModelingViews.WorkView)))
    return out


def _summary(a, say):
    if a.get("orientation"):
        text = f"View {a['orientation']}"
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
    cost="instant",
    restorable=True,
    example={"orientation": "isometric", "fit": True},
)
def nx_set_camera(
    tx: Tx,
    *,
    orientation: Orientation | None = None,
    rotation: Mat3 | None = None,
    origin: ViewOrigin | None = None,
    scale: Annotated[float, Range(gt=0, le=1e6), Doc("Absolute NX view scale")] | None = None,
    fit: bool = False,
) -> CameraData:
    """Orient the model view (named orientation or explicit rotation), set its origin or
    scale, and/or fit it. Work and display part must match; returns restore_id."""
    if orientation is None and rotation is None and origin is None and scale is None and not fit:
        fail("NX_INVALID_ARGUMENT", "Give orientation, rotation, origin, scale or fit=true")
    if orientation is not None and rotation is not None:
        fail("NX_INVALID_ARGUMENT", "Give orientation or rotation, not both", field="/rotation")
    if fit and (origin is not None or scale is not None):
        fail("NX_INVALID_ARGUMENT", "fit sets origin and scale itself", field="/fit")
    wanted = geom.rotation(rotation, "/rotation") if rotation is not None else None
    with tx.checks():
        part = display.shown_part(tx, work=True)
        view = display.model_view(tx, part)
    tx.frame = "display_part_absolute"
    tx.display.keep_camera()
    if orientation is not None:
        display.orient(tx, view, orientation, fit=fit)
        wanted = display.ORIENTATION_ROTATIONS[orientation]
    if rotation is not None or origin is not None or scale is not None:
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
