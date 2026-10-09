"""Drawing sheets and views: open, restore, sheet units and placement points.

    with drawing.on_sheet(tx, sheet) as sh:
        origin = sh.point([120, 80])          # sheet units -> NX placement point

NX 2406 findings encoded here:

- A sheet can stay the work view while ``CurrentDrawingSheet`` is None;
  ``Sheet.Open()`` then returns without making it current and drafting
  builders commit null views. UF ``Draw.OpenDrawing`` re-opens it.
- ``ExitDraftingApplication()`` can leave the sheet as work view; UF
  ``Draw.SetDisplayState(1)`` returns to the modeling display.
- NXOpen placement points on a sheet are in *part* units, whatever the sheet
  units are.
"""

from __future__ import annotations

import contextlib
import math

from nx_mcp.core.errors import fail
from nx_mcp.kit import units

MODELING_DISPLAY = 1  # UF_DRAW display state: 1 modeling, 2 drawing
TRUE_SHAPE_TOL_DEG = 0.01  # Max tilt of a plane against the drawing plane.


def seen_true_shape(normal, view_z, tol_deg=TRUE_SHAPE_TOL_DEG):
    """True if a plane with this unit normal is parallel to the view's drawing plane."""
    dot = abs(sum(a * b for a, b in zip(normal, view_z, strict=True)))
    return math.degrees(math.acos(min(1.0, dot))) <= tol_deg


def work_view_is_drawing(part):
    """True when NX shows a drawing sheet as the work view (no modeling view)."""
    try:
        _ = part.ModelingViews.WorkView
    except AttributeError:
        return False
    except Exception:  # NX: "The view is not a model view"
        return True
    return False


def current(part):
    """The current drawing sheet of ``part`` or None."""
    return getattr(getattr(part, "DrawingSheets", None), "CurrentDrawingSheet", None)


def _same(a, b):
    return a is not None and b is not None and int(a.Tag) == int(b.Tag)


def open_sheet(tx, sheet, part=None):
    """Make ``sheet`` the current drawing sheet and verify it (UF fallback)."""
    sheets = getattr(part if part is not None else tx.part, "DrawingSheets", None)
    if not hasattr(sheets, "CurrentDrawingSheet"):  # NXOpen always reports it
        sheet.Open()
        return sheet
    sheet.Open()
    if _same(sheets.CurrentDrawingSheet, sheet):
        return sheet
    tx.uf.Draw.OpenDrawing(sheet.Tag)
    if not _same(sheets.CurrentDrawingSheet, sheet):
        fail(
            "NX_WRONG_APPLICATION",
            f"NX could not make drawing sheet {sheet.Name!r} the current sheet",
        )
    return sheet


def close_sheets(tx, part=None):
    """Leave the drawing display and verify the modeling work view is back."""
    part = part if part is not None else tx.part
    part.Drafting.ExitDraftingApplication()
    if current(part) is None and not work_view_is_drawing(part):
        return
    tx.uf.Draw.SetDisplayState(MODELING_DISPLAY)
    if current(part) is not None or work_view_is_drawing(part):
        fail("NX_WRONG_APPLICATION", "NX did not return to the modeling view")


class Sheet:
    """An opened sheet: units and conversion of sheet points to NX points."""

    def __init__(self, tx, sheet):
        self.tx = tx
        self.sheet = sheet
        self.units = units.sheet_units(tx, sheet)
        # Part units per sheet unit (NXOpen placement points take part units).
        self.scale = units.factor(self.units, units.part_units(tx))

    def to_part(self, p):
        """Sheet [x, y] (sheet units) -> placement [x, y, 0] in part units."""
        return [float(p[0]) * self.scale, float(p[1]) * self.scale, 0.0]

    def to_sheet(self, p3):
        """NX placement point (part units, list or Point3d) -> sheet [x, y]."""
        x, y = (p3.X, p3.Y) if hasattr(p3, "X") else (p3[0], p3[1])
        return [float(x) / self.scale, float(y) / self.scale]

    def point(self, p):
        """An NX ``Point3d`` placement point for sheet coordinates ``p``."""
        return self.tx.nx.Point3d(*self.to_part(p))


@contextlib.contextmanager
def on_sheet(tx, sheet, part=None):
    """Open ``sheet`` (ref or object) and restore the previous display afterwards.

    No current sheet before -> the modeling display is restored (also from
    NX 2406's sheet-as-work-view state); another sheet -> it is reopened.
    Restoring never hides an error raised by the block.
    """
    target = tx.resolve(sheet, "drawing_sheet") if isinstance(sheet, str) else sheet
    owner = part if part is not None else tx.part
    before = current(owner)
    open_sheet(tx, target, owner)
    try:
        yield Sheet(tx, target)
    except BaseException:
        with contextlib.suppress(Exception):
            _restore(tx, owner, before, target)
        raise
    _restore(tx, owner, before, target)


def _restore(tx, part, before, target):
    if before is None:
        close_sheets(tx, part)
    elif not _same(before, target):
        open_sheet(tx, before, part)


def sheets_showing(part, view):
    """The sheets of ``part`` that show drafting view ``view`` (normally one)."""
    return [s for s in getattr(part, "DrawingSheets", None) or [] if view in s.GetDraftingViews()]


def view_sheet(tx, view, field=None):
    """The one work-part sheet that shows ``view``."""
    matches = sheets_showing(tx.part, view)
    if len(matches) != 1:
        fail("NX_WRONG_OWNER", "Select a view on one work-part sheet", field=field)
    return matches[0]


def view(tx, ref, field="/view"):
    """``(view, sheet)`` for a drawing-view reference of the work part."""
    obj = tx.resolve(ref, "drawing_view") if isinstance(ref, str) else ref
    return obj, view_sheet(tx, obj, field)


def refresh(tx, views):
    """Update drawing views so they show the current model."""
    views = list(views)
    if views:
        tx.part.DraftingViews.UpdateViews(views)
    return views
