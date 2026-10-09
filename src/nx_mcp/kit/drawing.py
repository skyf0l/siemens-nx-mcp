"""Drawing sheets and views: open, restore, sheet units and placement points.

    with drawing.on_sheet(tx, sheet) as sh:
        origin = sh.point([120, 80])          # sheet units -> NX placement point

NX 2406 findings encoded here (also: ``place`` moves a view to the requested
point with ``Draw.MoveView`` and verifies it; ``from_mm`` gives defaults in the
sheet's units; UF view queries answer in sheet units):

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
from nx_mcp.kit import select, units

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
    obj = select.resolve(tx, ref, "drawing_view", field=field) if isinstance(ref, str) else ref
    return obj, view_sheet(tx, obj, field)


def refresh(tx, views):
    """Update drawing views so they show the current model."""
    views = list(views)
    if views:
        tx.part.DraftingViews.UpdateViews(views)
    return views


# ---- sheet geometry, view placement and annotation owners (drawing domain) -------------


def from_mm(sheet_units, *mm):
    """Defaults defined in millimetres, in ``sheet_units``.

    Plain numbers such as [100, 100] would be 100 in on an inch sheet (off an A3
    sheet: NX fails with "The view is not located on the drawing").
    """
    factor = units.factor("mm", sheet_units)
    return [float(v) * factor for v in mm]


def size(sheet):
    """``[length, height]`` of a sheet in its own units."""
    return [float(sheet.Length), float(sheet.Height)]


def axes(view):
    """``(x, y, z)`` of a drafting view: its horizontal, vertical and viewing directions
    in work-part coordinates (rows of ``view.Matrix``)."""
    m = view.Matrix
    return [m.Xx, m.Xy, m.Xz], [m.Yx, m.Yy, m.Yz], [m.Zx, m.Zy, m.Zz]


def to_sheet(tx, view, p3):
    """A work-part point as seen in ``view``: sheet ``[x, y]`` in sheet units.

    UF ``View.MapModelToDrawing`` (like ``Draw.AskViewBorders``) answers in the
    sheet's units; NXOpen placement points take part units (``Sheet.point``).
    """
    mapped = tx.uf.View.MapModelToDrawing(view.Tag, [float(v) for v in p3])
    return [float(mapped[0]), float(mapped[1])]


def position(view):
    """The view's reference point on its sheet, ``[x, y]`` in sheet units."""
    point = view.GetDrawingReferencePoint()
    return [float(point.X), float(point.Y)]


def scale(tx, view):
    """The view's actual scale (UF ``Draw.AskViewScale``)."""
    return float(tx.uf.Draw.AskViewScale(view.Tag)[1])


def borders(tx, view):
    """``[x0, y0, x1, y1]`` of the view border in sheet units (annotations excluded)."""
    return [float(v) for v in tx.uf.Draw.AskViewBorders(view.Tag)]


def inside(bounds, sheet):
    """True when view borders ``[x0, y0, x1, y1]`` lie on ``sheet``."""
    length, height = size(sheet)
    return bounds[0] >= 0 and bounds[1] >= 0 and bounds[2] <= length and bounds[3] <= height


def place(tx, view, point, axes=(0, 1)):
    """Move ``view``'s reference point to sheet ``point`` (sheet units) and verify it.

    NX 2406 builders and UF section views put a view at their own aligned spot;
    ``Draw.MoveView`` then places it. ``axes`` limits the check to the free axis
    of an aligned (projected) view. Returns the read-back position.
    """
    tx.uf.Draw.MoveView(view.Tag, [float(point[0]), float(point[1])])
    tx.part.DraftingViews.UpdateViews([view])
    actual = position(view)
    tol = units.tolerance(units.sheet_units(tx, view_sheet(tx, view)), 1e-4)
    if any(abs(actual[i] - float(point[i])) > tol for i in axes):
        fail(
            "NX_VERIFICATION_FAILED",
            f"NX placed the view at {actual}, not at the requested {list(point)}",
            details={"check": "position", "actual": actual, "requested": list(point)},
        )
    return actual


def drawing_view_name(sheet):
    """The name of a sheet's own drawing view (``"SHT1@0"``)."""
    member = getattr(sheet, "View", None)
    name = getattr(member, "Name", None)
    return str(name) if name else f"{sheet.Name}@0"


def sheet_of_annotation(tx, obj, part=None):
    """The work-part sheet a drafting annotation, dimension or table lies on, or None.

    Drafting annotations are view dependent in their sheet's own drawing view
    (UF ``View.AskViewDependentStatus`` -> ``(1, "SHT1@0")``); a table without an
    origin (bend table) is located by its first section. PMI and model objects are
    not dependent on a sheet's own view (None).
    """
    part = part if part is not None else tx.part
    anchor = anchor_of(tx, obj)
    try:
        status, name = tx.uf.View.AskViewDependentStatus(anchor.Tag)
    except Exception:  # NX raises for objects that cannot be view dependent
        return None
    if not status or not name:
        return None
    wanted = str(name).casefold()
    sheets = list(getattr(part, "DrawingSheets", None) or [])
    for sheet in sheets:
        if drawing_view_name(sheet).casefold() == wanted:
            return sheet
    return None


def anchor_of(tx, obj):
    """The object that carries a drafting object's position: the object itself, or for a
    table without an origin (NX 2406 bend tables) its first table section."""
    if hasattr(obj, "AnnotationOrigin"):
        return obj
    try:
        if tx.uf.Tabnot.AskNmSections(obj.Tag):
            tag = tx.uf.Tabnot.AskNthSection(obj.Tag, 0)
            return tx.nx.TaggedObjectManager.GetTaggedObject(tag)
    except Exception:  # not a tabular note
        return obj
    return obj


@contextlib.contextmanager
def in_modeling(tx, part=None):
    """Show the modeling view for the block and reopen the previous sheet afterwards.

    NX 2406 creates PMI on the displayed drawing sheet (a sheet annotation) when a sheet
    is shown; model annotations need the modeling display.
    """
    owner = part if part is not None else tx.part
    before = current(owner)
    shown = before is not None or work_view_is_drawing(owner)
    if shown:
        close_sheets(tx, owner)
    try:
        yield
    finally:
        if before is not None:
            with contextlib.suppress(Exception):
                open_sheet(tx, before, owner)
