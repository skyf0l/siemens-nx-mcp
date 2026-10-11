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
from nx_mcp.kit import facts, select, units

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


def out_of_date(views):
    """The views NX flags out of date (``DraftingView.IsOutOfDate``; their sheet's banner
    shows "(Out of Date)", e.g. after a part saved with stale views is reopened)."""
    stale = []
    for v in views:
        with contextlib.suppress(Exception):  # a view this NX build cannot answer for
            if bool(getattr(v, "IsOutOfDate", False)):
                stale.append(v)
    return stale


def update_out_of_date(tx, views):
    """Update the out-of-date ones of ``views``; returns them. Views NX still flags
    afterwards fail ``NX_UPDATE_FAILED``."""
    stale = out_of_date(views)
    if stale:
        tx.part.DraftingViews.UpdateViews(stale)
        left = out_of_date(stale)
        if left:
            fail(
                "NX_UPDATE_FAILED",
                f"NX left {len(left)} drawing view(s) out of date after updating them",
                details={"views": [str(v.Name) for v in left[:20]]},
            )
    return stale


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


# ---- sheet rectangles: default text spots clear of views and annotations ---------------


def overlap(a, b, eps=1e-9):
    """True when rectangles ``[x0, y0, x1, y1]`` ``a`` and ``b`` share an area."""
    return min(a[2], b[2]) - max(a[0], b[0]) > eps and min(a[3], b[3]) - max(a[1], b[1]) > eps


def contains(box, point, margin=0.0):
    """True when ``point`` lies in rectangle ``box`` grown by ``margin``."""
    return (
        box[0] - margin <= point[0] <= box[2] + margin
        and box[1] - margin <= point[1] <= box[3] + margin
    )


def outward(box, point, fallback=(1.0, 1.0)):
    """Unit sheet direction from the centre of ``box`` towards ``point`` (``fallback``,
    normalised, when ``point`` is the centre)."""
    d = [point[0] - (box[0] + box[2]) / 2, point[1] - (box[1] + box[3]) / 2]
    length = math.hypot(*d)
    if length <= 1e-9 * max(1.0, box[2] - box[0], box[3] - box[1]):
        d, length = list(fallback), math.hypot(*fallback)
    return [d[0] / length, d[1] / length]


def clear_of(box, point, direction, gap):
    """The point ``gap`` beyond where the ray from ``point`` along unit ``direction``
    leaves rectangle ``box`` (``point`` itself plus ``gap`` when it is already outside)."""
    exits = [
        ((box[i + 2] if d > 0 else box[i]) - point[i]) / d
        for i, d in enumerate(direction[:2])
        if abs(d) > 1e-12
    ]
    t = max(0.0, min(exits)) if exits else 0.0
    return [point[i] + (t + gap) * direction[i] for i in (0, 1)]


def free_spot(point, direction, step, taken, tries=20):
    """``point`` moved along ``direction`` by ``step`` until no rectangle in ``taken``
    (annotation bounds) contains it, at most ``tries`` steps."""
    spot = [float(point[0]), float(point[1])]
    for _ in range(tries):
        if not any(contains(box, spot, step / 4) for box in taken):
            break
        spot = [spot[0] + step * direction[0], spot[1] + step * direction[1]]
    return spot


# ---- annotation and table extents --------------------------------------------------------


def annotation_box(tx, obj, sh):
    """``(bounds, text)``: sheet rectangles ``[x0, y0, x1, y1]`` (sheet units) of a drafting
    annotation, dimension or table on sheet ``sh`` (a :class:`Sheet`): its whole extent
    (leaders, extension lines, all table cells) and its text block (None when NX reports no
    text). Either is None when NX cannot box the object.

    NX 2406: UF ``ModlGeneral.AskBoundingBox`` answers drafting objects in the sheet plane
    (part units); ``Drf.AskAnnotationTextBox`` gives the text's upper-left corner, length
    and height (zero for center marks; it raises for table sections).
    """
    bounds = text = None
    try:
        box = [float(v) for v in tx.uf.ModlGeneral.AskBoundingBox(anchor_of(tx, obj).Tag)]
        lo, hi = sh.to_sheet(box[0:3]), sh.to_sheet(box[3:6])
        bounds = [lo[0], lo[1], hi[0], hi[1]]
    except Exception:  # an object NX cannot box
        bounds = None
    try:
        corner, length, height = tx.uf.Drf.AskAnnotationTextBox(obj.Tag)
        if float(length) > 0 and float(height) > 0:
            x, y = sh.to_sheet(corner)
            w, h = sh.to_sheet([float(length), float(height), 0.0])
            text = [x, y - h, x + w, y]
    except Exception:  # tables and objects without text
        text = None
    return bounds, text


def sheet_annotation_bounds(tx, sheet):
    """Whole extents (sheet units) of the annotations, dimensions and tables NX shows on
    ``sheet`` (the objects visible in the sheet's own view, ``View.AskVisibleObjects``)."""
    try:
        shown = list(sheet.View.AskVisibleObjects())
    except Exception:  # a sheet whose own view NX cannot list
        return []
    sh = Sheet(tx, sheet)
    out = []
    for obj in shown:
        if hasattr(obj, "AnnotationOrigin") or type(obj).__name__ == "TableSection":
            bounds, _text = annotation_box(tx, obj, sh)
            if bounds is not None:
                out.append(bounds)
    return out


def crowding(tx, sheet, box, own=()):
    """``(views, off_sheet)`` for an annotation rectangle on ``sheet``: refs of the views
    whose border it overlaps (``own`` view tags skipped) and whether it leaves the sheet."""
    views = [
        tx.ref(v, "drawing_view")
        for v in sheet.GetDraftingViews()
        if int(v.Tag) not in own and overlap(box, borders(tx, v))
    ]
    return views, not inside(box, sheet)


# ---- model edges as drawn in a view --------------------------------------------------------


def visible_edges(tx, view):
    """``[(edge, drawn model length)]``: the model edges NX draws visible in ``view``.

    NX 2406 keeps the visible curves of a drafting view as ``DraftingBody.DraftingCurves``
    (the view's ``DraftingBodies``), each with its model edge as parent (UF
    ``Draw.AskDraftingCurveParents``); hidden lines are not among them and face
    silhouettes have no edge parent. Several curves of one edge (an edge partly hidden)
    add up their lengths.
    """
    lookup = tx.nx.TaggedObjectManager.GetTaggedObject
    found: dict[int, list] = {}
    for body in getattr(view, "DraftingBodies", None) or []:
        for drawn in body.DraftingCurves:
            _count, parents = tx.uf.Draw.AskDraftingCurveParents(drawn.Tag)
            for tag in parents or []:
                edge = lookup(tag)
                if facts.is_edge(edge):
                    entry = found.setdefault(int(tag), [edge, 0.0])
                    entry[1] += float(drawn.GetLength())
    return [(edge, length) for edge, length in found.values()]


def edge_in_view(tx, view, edge, drawn=None, part_per_sheet=1.0):
    """Facts of a model edge as drawn in ``view`` (sheet units): ``type`` (line, circle,
    other), ``start``, ``end``, ``center``; lines add ``length`` on the sheet,
    ``model_length`` (the length seen, in part units: what an aligned dimension reads) and
    ``orientation`` (horizontal, vertical, inclined; None seen end-on); circles seen true
    shape add ``radius`` (part units). ``drawn``: the model length NX draws visible, to
    flag a partly hidden edge (``partial``)."""
    found = facts.edge(tx, edge)
    start, end = to_sheet(tx, view, found["start"]), to_sheet(tx, view, found["end"])
    out: dict = {"type": {"line": "line", "arc": "circle"}.get(found["kind"], "other")}
    out.update(start=start, end=end, center=[(start[0] + end[0]) / 2, (start[1] + end[1]) / 2])
    if found["kind"] == "line":
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        out["length"] = length
        out["model_length"] = length / scale(tx, view) * part_per_sheet
        tol = 1e-6 * max(1.0, length)
        out["orientation"] = (
            None
            if length <= 1e-9
            else "horizontal"
            if abs(dy) <= tol
            else "vertical"
            if abs(dx) <= tol
            else "inclined"
        )
        total = math.dist(found["start"], found["end"])
    else:
        total = float(edge.GetLength()) if hasattr(edge, "GetLength") else None
    if found["kind"] == "arc":
        out["center"] = to_sheet(tx, view, found["center"])
        if found.get("normal") is not None and seen_true_shape(found["normal"], axes(view)[2]):
            out["radius"] = float(found["radius"])
    if drawn is not None and total and drawn < total * (1 - 1e-6):
        out["partial"] = True
    return out
