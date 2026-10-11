"""Drawing views: base, projected, section, detail and flat-pattern views; edit; inspect.

NX 2406 findings encoded here:

- Base-view builders commit null views unless the sheet is current
  (``kit.drawing.on_sheet`` opens it with the UF fallback).
- Builders and UF section views place a view at their own aligned spot; every view
  is moved to the requested point with ``Draw.MoveView`` and read back.
- A projected view defaults to 1:1 on 2406: ``ViewStyleProjected.Scale = True``
  makes it inherit the parent's scale. A projected center off the sheet fails in NX
  after the commit ("The view is not located on the drawing"): refused before NX.
- ``position`` is the view's reference point (``GetDrawingReferencePoint``), not the
  centre of its border; the border (``AskViewBorders``) includes NX's view margin.
  Projected ``spacing`` is the clear gap between the two borders, set from the read-back
  borders after the commit.
- Detail-view boundaries are recorded (parent handle, model center, radius) and
  re-mapped after parent edits (``nx_update_annotations`` and view edits).
- ``Draw.SetViewScale`` may move the view's reference point (a scale from 1 to 1.5 kept
  the border centre); projected views keep their alignment to the parent's geometry
  themselves, so a parent's scale change rescales them and moves them only along their
  free axis, to keep their gap.
"""

from __future__ import annotations

import json
import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Aliases,
    BodyRef,
    Doc,
    DrawingViewRef,
    EdgeRef,
    ExplosionRef,
    FeatureRef,
    Items,
    Point3,
    Range,
    RefTo,
    SheetPoint,
    forbid_extra,
)
from nx_mcp.domains.drawing import style as view_style
from nx_mcp.domains.drawing import view_kinds as kinds
from nx_mcp.domains.drawing.common import (
    CONSTRUCTION,
    DETAIL,
    VIEW_PARENT,
    ScaleArg,
    attribute,
    open_or_only_sheet,
    scale_terms,
    scale_value,
    update,
)
from nx_mcp.domains.drawing.view_kinds import EdgePoint
from nx_mcp.kit import builders, drawing, geom, parts, readback, result, select, units

ORIENTATIONS = {
    "top": "Top",
    "front": "Front",
    "back": "Back",
    "right": "Right",
    "left": "Left",
    "bottom": "Bottom",
    "isometric": "Isometric",
}
ORIENTATION_ALIASES = {"iso": "isometric", "plan": "top", "rear": "back"}
OFFSETS = {"right": (1, 0), "left": (-1, 0), "top": (0, 1), "bottom": (0, -1)}
DIRECTION_ALIASES = {"above": "top", "up": "top", "below": "bottom", "down": "bottom"}
# The view z axis (toward the viewer, part coordinates) of NX's standard model views.
TOWARD_VIEWER = {
    "top": (0, 0, 1),
    "bottom": (0, 0, -1),
    "front": (0, -1, 0),
    "back": (0, 1, 0),
    "right": (1, 0, 0),
    "left": (-1, 0, 0),
}
DEFAULT_POSITION_MM = (100.0, 100.0)
MAX_GAP = 1000.0  # broken-view gap, sheet units
DEFAULT_GAP_MM = 20.0  # clear space between a projected view's border and its parent's
DEFAULT_DETAIL_SCALE = 2.0
NATIVE_TYPES = {
    "BaseView": "base",
    "ProjectedView": "projected",
    "SectionView": "section",
    "DetailView": "detail",
}

Vec2 = tuple[float, float]  # a sheet direction
Band = tuple[float, float]
# What each view type needs besides ``type``, said once when something is missing.
NEEDS = {
    "base": "e.g. {type: base, orientation: front, position: [120, 150]}; sheet defaults to "
    "the open sheet, body to the part's only body",
    "projected": "e.g. {type: projected, parent: <view ref>, direction: right, spacing: 20}",
    "auxiliary": "hinge: a straight edge ref seen in the parent; position: the sheet point",
    "broken": "breaks: [[start, end]] sheet coordinates along axis (x or y)",
    "break_out": "center: a model point [x, y, z]; radius, depth below center: part units",
    "detail": "center: a model point [x, y, z] and radius (part units); position: sheet point",
    "flat_pattern": "flat_pattern: the Flat Pattern feature ref",
    "section": "cut: an edge ref (nx_find_geometry) the section line passes through at "
    "cut_point (start, end or center of an arc); arrow_direction: the sheet direction the "
    "arrows point, e.g. [0, 1] (step_direction defaults to it turned 90 deg clockwise); "
    "position: the sheet point. half: + bend {edge}; stepped: cuts [{edge}, {edge}]; "
    "revolved: rotation {edge} + 2 cuts",
}


class _ViewExtras(TypedDict, total=False):
    cut_planes: list[dict]  # section views: [{normal, offset}] in part coordinates
    hinge: dict  # auxiliary: hinge edge model_length and view_length (part units)
    breaks: list[dict]  # broken: the bands removed (sheet units)
    gap: float  # projected: clear space between its border and the parent's (sheet units)
    break_out: dict  # break-out: depth read back, boundary radius


class ViewData(_ViewExtras):
    view: result.Row
    sheet: result.Row
    type: str
    position: Annotated[list[float], Doc("Reference point, not the border centre")]
    scale: float
    bounds: Annotated[list[float], Doc("Border [x0, y0, x1, y1] incl. NX's view margin")]
    center: list[float]
    inside_sheet: bool
    overlaps: Annotated[list[result.Row], Doc("Views whose border overlaps this one")]
    toward_viewer: list[float]
    orientation: str | None
    parent: result.Row | None


# ---- shared ----------------------------------------------------------------------------


def view_type(view):
    return NATIVE_TYPES.get(type(view).__name__, type(view).__name__.lower())


def orientation_of(toward):
    """The standard view name whose view direction ``toward`` is, else None."""
    for name, axis in TOWARD_VIEWER.items():
        if all(abs(a - b) <= 1e-6 for a, b in zip(toward, axis, strict=True)):
            return name
    if all(abs(abs(c) - 1 / math.sqrt(3)) <= 1e-6 for c in toward):
        return "isometric"
    return None


def parent_of(tx, view, sheet):
    """The view a derived view was made from (recorded at creation), else None."""
    handle = attribute(tx, view, VIEW_PARENT)
    detail = attribute(tx, view, DETAIL)
    if handle is None and detail:
        handle = json.loads(detail).get("parent")
    if not handle:
        return None
    try:
        tag = int(tx.uf.Tag.AskTagOfHandle(handle))
    except Exception:  # the parent was deleted
        return None
    return next((v for v in sheet.GetDraftingViews() if int(v.Tag) == tag), None)


def _overlap(a, b):
    return min(a[2], b[2]) - max(a[0], b[0]) > 1e-9 and min(a[3], b[3]) - max(a[1], b[1]) > 1e-9


def view_data(tx, view, sheet):
    bounds = drawing.borders(tx, view)
    toward = [float(v) for v in drawing.axes(view)[2]]  # view z axis, part coords
    parent = parent_of(tx, view, sheet)
    others = [v for v in sheet.GetDraftingViews() if int(v.Tag) != int(view.Tag)]
    return {
        "view": tx.ref(view, "drawing_view"),
        "sheet": tx.ref(sheet, "drawing_sheet"),
        "type": view_type(view),
        "position": drawing.position(view),
        "scale": drawing.scale(tx, view),
        "bounds": bounds,
        "center": [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2],
        "inside_sheet": drawing.inside(bounds, sheet),
        "overlaps": [
            tx.ref(v, "drawing_view") for v in others if _overlap(bounds, drawing.borders(tx, v))
        ],
        "toward_viewer": toward,
        "orientation": orientation_of(toward),
        "parent": tx.ref(parent, "drawing_view") if parent is not None else None,
    }


def warn_overlaps(tx, out):
    if out["overlaps"]:
        names = ", ".join(str(r.get("name") or r.get("id")) for r in out["overlaps"][:5])
        tx.warn(
            "NX_VIEW_OVERLAP",
            f"The view's border overlaps {len(out['overlaps'])} other view(s) ({names}): "
            "move it with nx_edit_drawing_view position (see bounds)",
        )


def _record_parent(tx, view, parent):
    view.SetAttribute(VIEW_PARENT, tx.uf.Tag.AskHandleFromTag(parent.Tag))


def _configure(tx, b, sheet, scale):
    """Ratio scale (the sheet's unless given) and dashed, self-hidden hidden lines."""
    P = tx.nx.Preferences
    numerator, denominator = scale_terms(scale) if scale is not None else sheet.GetScale()
    b.Scale.ScaleType = b.Scale.Type.Ratio
    b.Scale.Numerator = float(numerator)
    b.Scale.Denominator = float(denominator)
    hidden = b.Style.ViewStyleHiddenLines
    hidden.HiddenLine = True
    hidden.Font = P.Font.Dashed
    hidden.SelfHidden = True
    b.Style.ViewStyleVisibleLines.VisibleFont = P.Font.Solid


def _verify_scale(tx, view, scale):
    readback.expect_close(drawing.scale(tx, view), scale, "scale", 1e-8)


def _explosion_of(tx, view):
    return int(tx.uf.Assem.AskViewExplosion(view.Tag) or 0)


def _set_explosion(tx, view, tag):
    tx.uf.Assem.SetViewExplosion(view.Tag, tag)
    tx.part.DraftingViews.UpdateViews([view])
    readback.expect(
        _explosion_of(tx, view) == int(tag), "explosion", explosion=_explosion_of(tx, view)
    )


def _model_view(tx, name):
    return tx.part.ModelingViews.FindObject(name)


def _base(tx, sh, position, scale, model_view):
    with builders.build(tx, tx.part.DraftingViews.CreateBaseViewBuilder, None) as b:
        _configure(tx, b, sh.sheet, scale)
        b.SelectModelView.SelectedView = model_view
        b.Placement.Placement.SetValue(None, None, sh.point(position))
        view = b.commit()
    return view


def _construction(tx, view):
    if view_style.set_construction(tx, view, visible=False) == "partial":
        tx.warn("NX_UPDATE_WARNING", view_style.CONSTRUCTION_WARNING)


def detail_points(tx, parent, sh, center, radius):
    """Boundary points (center, rim) of a circular detail as NX placement points."""
    x_axis = drawing.axes(parent)[0]
    rim = [c + radius * d for c, d in zip(center, x_axis, strict=True)]
    return [sh.point(drawing.to_sheet(tx, parent, p)) for p in (center, rim)]


def refresh_details(tx, views=None):
    """Re-map managed detail boundaries to their parents; returns the refreshed views."""
    part = tx.part
    managed = [
        (sheet, view)
        for sheet in part.DrawingSheets
        for view in sheet.GetDraftingViews()
        if attribute(tx, view, DETAIL) and (views is None or view in views)
    ]
    out = []
    for sheet, view in managed:
        record = json.loads(attribute(tx, view, DETAIL))
        try:
            tag = int(tx.uf.Tag.AskTagOfHandle(record["parent"]))
        except Exception:
            tag = None
        parent = next((v for v in sheet.GetDraftingViews() if int(v.Tag) == tag), None)
        if parent is None:
            fail(
                "NX_STALE_SOURCE",
                f"The parent of detail view {view.Name} no longer exists",
                details={"view": tx.ref(view, "drawing_view")},
            )
        with drawing.on_sheet(tx, sheet, part) as sh:
            points = detail_points(tx, parent, sh, record["center"], record["radius"])
            with builders.build(tx, part.DraftingViews.CreateDetailViewBuilder, view) as b:
                b.BoundaryPoint1.SetCoordinates(points[0])
                b.BoundaryPoint2.SetCoordinates(points[1])
                b.commit()
        out.append(view)
    return out


# ---- create ----------------------------------------------------------------------------


def _summary(a, say):
    kind = a.get("type")
    on = f" on {say('sheet')}" if a.get("sheet") else ""
    if kind == "base":
        return f"Base view {a.get('orientation', '')}{on}"
    if kind == "projected":
        return f"Projected view {a.get('direction', '')} of {say('parent')}"
    if kind == "flat_pattern":
        return f"Flat-pattern view{on}"
    if kind in ("broken", "break_out"):
        return f"{kind.replace('_', '-').capitalize()} {say('parent')}"
    return f"{kind.replace('_', ' ').capitalize()} view of {say('parent')}"


@tool(
    effect="model",
    label="Create drawing view",
    summary=_summary,
    keywords=(
        "base view",
        "projected view",
        "section view",
        "detail view",
        "auxiliary view",
        "broken view",
        "break-out section",
        "isometric view",
    ),
    requires=("work_part", "no_active_sketch"),
    variants="type",
    cost="heavy",
    invalidates="deleted",
    example={
        "type": "base",
        "sheet": "sh:3",
        "orientation": "front",
        "body": "b:4",
        "position": [120, 150],
    },
)
def nx_create_drawing_view(
    tx: Tx,
    *,
    type: Literal[
        "base",
        "projected",
        "section",
        "detail",
        "flat_pattern",
        "auxiliary",
        "broken",
        "half_section",
        "stepped_section",
        "revolved_section",
        "break_out",
    ],
    sheet: Annotated[str, RefTo("drawing_sheet", doc="Ref; default the open sheet")] | None = None,
    parent: DrawingViewRef | None = None,
    orientation: Annotated[
        Literal["top", "front", "back", "right", "left", "bottom", "isometric"],
        Aliases(ORIENTATION_ALIASES),
    ]
    | None = None,
    body: BodyRef | None = None,
    explosion: ExplosionRef | None = None,
    direction: Annotated[Literal["right", "left", "top", "bottom"], Aliases(DIRECTION_ALIASES)]
    | None = None,
    spacing: Annotated[float, Range(gt=0), Doc("Gap between borders")] | None = None,
    hinge: EdgeRef | None = None,
    cut: EdgeRef | None = None,
    cut_point: Literal["start", "end", "center"] = "start",
    bend: EdgePoint | None = None,
    rotation: EdgePoint | None = None,
    cuts: Annotated[list[EdgePoint], Items(2, 49)] | None = None,
    step_direction: Vec2 | None = None,
    arrow_direction: Vec2 | None = None,
    axis: Literal["x", "y"] = "x",
    breaks: Annotated[list[Band], Items(1, 4), Doc("Sheet [start, end]")] | None = None,
    gap: Annotated[float, Range(gt=0), Doc("Sheet units")] | None = None,
    center: Point3 | None = None,
    radius: Annotated[float, Range(gt=0), Doc("Part units")] | None = None,
    depth: Annotated[float, Range(gt=0), Doc("Part units")] | None = None,
    flat_pattern: FeatureRef | None = None,
    position: SheetPoint | None = None,
    scale: ScaleArg | None = None,
) -> ViewData:
    """Place a drawing view at a verified sheet point; broken and break_out cut the parent.
    Derived views keep the parent scale."""
    a = {**locals(), "scale": scale_value(scale)}
    out = _create(tx, type, a)
    if type not in ("broken", "break_out"):  # those cut the parent view in place
        warn_overlaps(tx, out)
    return out


def _need(kind, **fields):
    """Every named field is given; else one error naming all the missing ones."""
    missing = [name for name, value in fields.items() if value is None]
    if missing:
        fail(
            "NX_INVALID_ARGUMENT",
            f"A {kind.replace('_', ' ')} view needs {', '.join(missing)}",
            field="/" + missing[0],
            hint=NEEDS["section" if kind.endswith("section") else kind],
        )


def _create(tx, kind, a):
    parent, position, scale = a["parent"], a["position"], a["scale"]
    center, radius = a["center"], a["radius"]
    if kind == "base":
        _need(kind, orientation=a["orientation"])
        return _create_base(tx, a["sheet"], a["orientation"], a["body"], a["explosion"],
                            position, scale)  # fmt: skip
    if kind == "projected":
        _need(kind, parent=parent, direction=a["direction"])
        return _create_projected(tx, parent, a["direction"], a["spacing"], position)
    if kind == "auxiliary":
        _need(kind, parent=parent, hinge=a["hinge"], position=position)
        return _create_auxiliary(tx, parent, a["hinge"], position)
    if kind == "broken":
        _need(kind, parent=parent, breaks=a["breaks"])
        if (a["gap"] or 0) > MAX_GAP:
            fail("NX_INVALID_ARGUMENT", f"gap must be at most {MAX_GAP:g}", field="/gap")
        return _create_broken(tx, parent, a["axis"], a["breaks"], a["gap"])
    if kind == "break_out":
        _need(kind, parent=parent, center=center, radius=radius, depth=a["depth"])
        return _create_break_out(tx, parent, center, radius, a["depth"])
    if kind.endswith("section"):
        return _section(tx, kind, a)
    if kind == "detail":
        _need(kind, parent=parent, center=center, radius=radius, position=position)
        return _create_detail(tx, parent, center, radius, position, scale)
    _need(kind, flat_pattern=a["flat_pattern"])
    return _create_flat(tx, a["sheet"], a["flat_pattern"], position, scale)


def _section(tx, type_, a):
    kind = type_.removesuffix("_section").removesuffix("section") or "simple"
    cut, cut_point, bend, rotation, cuts = (
        a[k] for k in ("cut", "cut_point", "bend", "rotation", "cuts")
    )
    parent, position, scale = a["parent"], a["position"], a["scale"]
    step_direction, arrow_direction = a["step_direction"], a["arrow_direction"]
    if step_direction is None and arrow_direction is not None:
        step_direction = (arrow_direction[1], -arrow_direction[0])  # the arrow turned clockwise
    needed: dict[str, object] = {"cut": cut} if kind in ("simple", "half") else {}
    if kind == "half":
        needed["bend"] = bend
    elif kind != "simple":
        needed.update(cuts=cuts, **({"rotation": rotation} if kind == "revolved" else {}))
    _need(type_, parent=parent, arrow_direction=arrow_direction, position=position, **needed)
    if kind == "revolved" and len(cuts or []) != 2:
        fail("NX_INVALID_ARGUMENT", "A revolved section takes one cut per leg", field="/cuts")
    step = geom.unit([*(step_direction or ()), 0.0], "/step_direction")
    arrow = geom.unit([*(arrow_direction or ()), 0.0], "/arrow_direction")
    if abs(geom.dot(step, arrow)) > 1e-8:
        fail("NX_INVALID_ARGUMENT", "step and arrow must be perpendicular",
             field="/arrow_direction")  # fmt: skip
    if kind == "simple":
        return _create_section(tx, parent, cut, cut_point, step, arrow, position, scale)
    points = _section_points(kind, cut, cut_point, bend, rotation, cuts)
    return _create_sx_kind(tx, kind, parent, points, step, arrow, position, scale)


def _section_points(kind, cut, cut_point, bend, rotation, cuts):
    """``(edge ref, at, field, at field)`` per section-line point: half (cut, bend),
    stepped (cuts), revolved (rotation, leg 1 cut, leg 2 cut)."""
    out = [(cut, cut_point, "/cut", "/cut_point")] if kind == "half" else []
    given: list[tuple[str, EdgePoint]] = []
    if kind == "half" and bend is not None:
        given.append(("/bend", bend))
    if kind == "revolved" and rotation is not None:
        given.append(("/rotation", rotation))
    given += [(f"/cuts/{i}", c) for i, c in enumerate(cuts or [])]
    out += [(c["edge"], c.get("at", "center"), f + "/edge", f + "/at") for f, c in given]
    return out


def _sheet(tx, sheet_ref):
    if sheet_ref is None:
        return open_or_only_sheet(tx)
    return select.resolve(tx, sheet_ref, "drawing_sheet", field="/sheet", work_part=True)


def _create_base(tx, sheet_ref, orientation, body_ref, explosion_ref, position, scale):
    with tx.checks():
        target_sheet = _sheet(tx, sheet_ref)
        part = tx.part
        assembly = bool(parts.components(part))
        explosion = None
        if assembly and body_ref is not None:
            fail("NX_INVALID_ARGUMENT", "An assembly view shows every component: omit body",
                 field="/body")  # fmt: skip
        if not assembly:
            if explosion_ref is not None:
                fail("NX_INVALID_ARGUMENT", "explosion needs an assembly", field="/explosion")
            if body_ref is not None:
                select.resolve(tx, body_ref, "body", field="/body", work_part=True)
            if len(list(part.Bodies)) != 1:
                fail(
                    "NX_UNSUPPORTED_OPTION",
                    "A base view of a part needs a single-body part",
                    field="/body",
                    details={"bodies": len(list(part.Bodies))},
                )
        else:
            if tx.session.Parts.Display != part:
                fail("NX_WRONG_PART_CONTEXT", "Make the assembly the work and display part",
                     hint="Activate it with nx_activate_part")  # fmt: skip
            explosion = (
                select.resolve(tx, explosion_ref, "explosion", field="/explosion")
                if explosion_ref
                else None
            )
            if explosion is not None and explosion.OwningPart != part:
                fail("NX_WRONG_OWNER", "The explosion must belong to the work part",
                     field="/explosion")  # fmt: skip
        model_view = _model_view(tx, ORIENTATIONS[orientation])
    with drawing.on_sheet(tx, target_sheet, part) as sh:
        point = list(position) if position else drawing.from_mm(sh.units, *DEFAULT_POSITION_MM)
        view = _base(tx, sh, point, scale, model_view)
        _construction(tx, view)
        drawing.place(tx, view, point)
        if assembly:
            _set_explosion(tx, view, int(explosion.Tag) if explosion else 0)
        if scale is not None:
            _verify_scale(tx, view, scale)
        return view_data(tx, view, target_sheet)


def _create_projected(tx, parent_ref, direction, spacing, position):
    dx, dy = OFFSETS[direction]
    k, sign = (0, dx) if dx else (1, dy)  # the free axis and the side of the parent
    with tx.checks():
        parent, sheet = drawing.view(tx, parent_ref, "/parent")
        if spacing is not None and position is not None:
            fail("NX_INVALID_ARGUMENT", "Give spacing or position, not both", field="/position")
        sheet_units = units.sheet_units(tx, sheet)
        center = drawing.position(parent)
        border = drawing.borders(tx, parent)
        facing = border[k + 2] if sign > 0 else border[k]  # the parent border facing the view
        limit = drawing.size(sheet)[k]
        gap = spacing if spacing is not None else drawing.from_mm(sheet_units, DEFAULT_GAP_MM)[0]
        if position is not None:
            free = float(position[k])
            if not 0 <= free <= limit or sign * (free - center[k]) <= 0:
                fail("NX_INVALID_ARGUMENT",
                     f"position must lie {direction} of the parent and on the sheet",
                     field="/position", details={"parent_position": center})  # fmt: skip
        else:
            reach = facing + sign * gap
            if not 0 <= reach <= limit:
                # NX fails after the commit with "The view is not located on the drawing".
                fail(
                    "NX_INVALID_ARGUMENT",
                    "The projected view would be off the sheet; reduce spacing or change direction",
                    field="/spacing",
                    details={"parent_bounds": border, "sheet_size": drawing.size(sheet)},
                )
            # First guess: the parent's own extent; corrected from the read-back border.
            free = min(max(reach + sign * (border[k + 2] - border[k]) / 2, 0.0), limit)
        target = list(center)
        target[k] = free
        explosions = list(getattr(tx.part.ComponentAssembly, "Explosions", None) or [])
        exploded = _explosion_of(tx, parent) if explosions else 0
        parent_scale = drawing.scale(tx, parent)
    with drawing.on_sheet(tx, sheet) as sh:
        with builders.build(tx, tx.part.DraftingViews.CreateProjectedViewBuilder, None) as b:
            b.Parent.View.Value = parent
            projected = getattr(getattr(b, "Style", None), "ViewStyleProjected", None)
            if projected is not None:
                projected.Scale = True  # inherit the parent scale (NX 2406 default is 1:1)
            method = b.Placement.Method
            b.Placement.AlignmentMethod = method.Horizontal if dx else method.Vertical
            b.Placement.AlignmentOption = b.Placement.Option.ToView
            b.Placement.AlignmentView.Value = parent
            b.Placement.Associative = True
            b.Placement.Placement.SetValue(None, None, sh.point(target))
            view = b.commit()
        _record_parent(tx, view, parent)
        # Move only along the free axis: moving an aligned view across its alignment drags
        # the parent with it (NX keeps the associative alignment).
        point = drawing.position(view)
        point[k] = free
        drawing.place(tx, view, point, axes=(k,))
        tol = units.tolerance(sh.units, 1e-4)
        if position is None:  # the requested gap between the read-back borders
            mine = drawing.borders(tx, view)
            point[k] += facing + sign * gap - (mine[k] if sign > 0 else mine[k + 2])
            if not 0 <= point[k] <= limit:
                fail("NX_INVALID_ARGUMENT", "The projected view would be off the sheet; reduce "
                     "spacing or change direction", field="/spacing",
                     details={"view_bounds": mine, "sheet_size": drawing.size(sheet)})  # fmt: skip
            drawing.place(tx, view, point, axes=(k,))
        readback.expect_close(drawing.position(parent), center, "parent_position", tol)
        mine = drawing.borders(tx, view)
        measured = sign * ((mine[k] if sign > 0 else mine[k + 2]) - facing)
        if position is None:
            readback.expect_close(measured, gap, "gap", 10 * tol)
        elif abs(float(position[1 - k]) - center[1 - k]) > tol:
            tx.warn("NX_UPDATE_WARNING", f"A {direction} projected view stays aligned with its "
                    f"parent: {'yx'[k]} = {center[1 - k]:g}, not {position[1 - k]:g}")  # fmt: skip
        if exploded:
            _set_explosion(tx, view, exploded)
        _construction(tx, view)
        _verify_scale(tx, view, parent_scale)
        out = view_data(tx, view, sheet)
        out["gap"] = measured
        return out


def _create_section(tx, parent_ref, cut, cut_point, step, arrow, position, scale):
    with tx.checks():
        parent, sheet = drawing.view(tx, parent_ref, "/parent")
        edge, at, _ = kinds.edge_point(tx, cut, cut_point, "/cut", "/cut_point")
        scale = scale if scale is not None else drawing.scale(tx, parent)
    with drawing.on_sheet(tx, sheet) as sh:
        data = kinds.drf_object(tx, edge, at, parent)
        tag = tx.uf.Draw.CreateSimpleSxview(
            sheet.Tag, float(scale), step, arrow, parent.Tag, data, list(position)
        )
        return _finish_section(tx, sh, parent, tag, arrow, position, scale)


def _create_sx_kind(tx, kind, parent_ref, given, step, arrow, position, scale):
    """Half, stepped and revolved sections: ``given`` = (edge ref, at, field, at field)."""
    with tx.checks():
        parent, sheet = drawing.view(tx, parent_ref, "/parent")
        points = [kinds.edge_point(tx, *g) for g in given]
        if kind == "revolved":
            kinds.revolved_legs(tx, parent, points[0], points[1:])
        scale = scale if scale is not None else drawing.scale(tx, parent)
    with drawing.on_sheet(tx, sheet) as sh:
        tag = kinds.create_sxview(
            tx, kind, sheet, parent, float(scale), step, arrow, points, position
        )
        return _finish_section(tx, sh, parent, tag, arrow, position, scale)


def _finish_section(tx, sh, parent, tag, arrow, position, scale):
    view = tx.nx.TaggedObjectManager.GetTaggedObject(tag)
    _record_parent(tx, view, parent)
    tx.part.DraftingViews.UpdateViews([view])
    tx.uf.Draw.SetViewScale(view.Tag, float(scale))
    drawing.place(tx, view, position)  # NX 2406 creates it at its own aligned spot
    _verify_scale(tx, view, scale)
    out = view_data(tx, view, sh.sheet)
    out["cut_planes"] = kinds.cut_planes(tx, sh, parent, view, arrow)
    return out


def _create_auxiliary(tx, parent_ref, hinge_ref, position):
    with tx.checks():
        parent, sheet = drawing.view(tx, parent_ref, "/parent")
        parent_scale = drawing.scale(tx, parent)
        found, hinge = kinds.hinge_of(tx, parent, hinge_ref, parent_scale)
        center = drawing.position(parent)
        if abs(geom.cross2(hinge, geom.sub(position, center))) <= 1e-6 * max(
            1.0, math.dist(position, center)
        ):
            fail("NX_INVALID_ARGUMENT", "Place the view to one side of the hinge line",
                 field="/position")  # fmt: skip
        model_length = math.dist(found["start"], found["end"])
    with drawing.on_sheet(tx, sheet) as sh:
        view = kinds.create_auxiliary(tx, sh, parent, hinge, position)
        _record_parent(tx, view, parent)
        _construction(tx, view)
        drawing.place(tx, view, position)
        tol = kinds.sheet_tolerance(tx, sheet)
        readback.expect_close(drawing.position(parent), center, "parent_position", tol)
        _verify_scale(tx, view, parent_scale)
        # The view looks normal to the hinge: the hinge edge is seen true length.
        seen = kinds.seen_length(tx, view, found, sh.scale)
        readback.expect_close(seen, model_length, "hinge_length", 1e-6 * max(1.0, model_length))
        out = view_data(tx, view, sheet)
        out["hinge"] = {"model_length": model_length, "view_length": seen}
        return out


def _create_broken(tx, view_ref, axis, breaks, gap):
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref, "/parent")
        gap = gap if gap is not None else kinds.default_gap(units.sheet_units(tx, sheet))
        bands = kinds.break_bands(tx, view, sheet, axis, breaks, gap)
        before = drawing.borders(tx, view)
        center = drawing.position(view)
    with drawing.on_sheet(tx, sheet) as sh:
        anchors = kinds.break_anchors(tx, view, bands)  # before the first break
        for band, pair in zip(bands, anchors, strict=True):
            kinds.add_break(tx, sh, view, band, gap, pair)
        tx.part.DraftingViews.UpdateViews([view])
        tol = kinds.sheet_tolerance(tx, sheet)
        readback.expect_close(drawing.position(view), center, "position", tol)
        after = drawing.borders(tx, view)
        width = [after[2] - after[0], after[3] - after[1]]
        wanted = kinds.expected_bounds(before, bands, gap)
        readback.expect_close(width, [wanted[2] - wanted[0], wanted[3] - wanted[1]], "size",
                              10 * tol)  # fmt: skip
        out = view_data(tx, view, sheet)
        out["breaks"] = [{"axis": "xy"[k], "start": lo, "end": hi, "gap": gap}
                         for k, lo, hi in bands]  # fmt: skip
        return out


def _create_break_out(tx, view_ref, center, radius, depth):
    with tx.checks():
        view, sheet = drawing.view(tx, view_ref, "/parent")
    with drawing.on_sheet(tx, sheet):
        tag = kinds.create_break_out(tx, view, center, radius, depth)
        tx.part.DraftingViews.UpdateViews([view])
        seen, curves = kinds.break_out_depth(tx, tag, center, view)
        readback.expect_close(seen, depth, "depth", 1e-6 * max(1.0, depth))
        out = view_data(tx, view, sheet)
        out["break_out"] = {"depth": seen, "radius": float(radius), "curves": curves}
        return out


def _create_detail(tx, parent_ref, center, radius, position, scale):
    with tx.checks():
        parent, sheet = drawing.view(tx, parent_ref, "/parent")
    scale = scale if scale is not None else DEFAULT_DETAIL_SCALE
    part = tx.part
    with drawing.on_sheet(tx, sheet, part) as sh:
        D = tx.nx.Drawings
        with builders.build(tx, part.DraftingViews.CreateDetailViewBuilder, None) as b:
            b.Parent.View.Value = parent
            b.Type = D.DetailViewBuilder.Types.Circular
            first, second = detail_points(tx, parent, sh, center, radius)
            b.BoundaryPoint1 = part.Points.CreatePoint(first)
            b.BoundaryPoint2 = part.Points.CreatePoint(second)
            b.BoundaryPoint1.Blank()
            b.BoundaryPoint2.Blank()
            b.Scale.ScaleType = D.ViewScaleBuilder.Type.Ratio
            b.Scale.Numerator, b.Scale.Denominator = scale_terms(scale)
            b.Origin.Placement.SetValue(None, None, sh.point(position))
            view = b.commit(validate=True)
        record = {
            "parent": tx.uf.Tag.AskHandleFromTag(parent.Tag),
            "center": [float(v) for v in center],
            "radius": float(radius),
        }
        view.SetAttribute(DETAIL, json.dumps(record))
        drawing.place(tx, view, position)
        _verify_scale(tx, view, scale)
        return view_data(tx, view, sheet)


def _create_flat(tx, sheet_ref, feature_ref, position, scale):
    with tx.checks():
        target_sheet = _sheet(tx, sheet_ref)
        feature = select.resolve(tx, feature_ref, "feature", field="/flat_pattern", work_part=True)
        if str(feature.FeatureType) != "FLAT_PATTERN":
            fail("NX_WRONG_KIND", "Select a Flat Pattern feature", field="/flat_pattern")
        part = tx.part
        manager = part.Features.SheetmetalManager
        with builders.build(tx, manager.CreateFlatPatternBuilder, feature) as definition:
            model_view = _model_view(tx, definition.FlatPatternViewName)
    with drawing.on_sheet(tx, target_sheet, part) as sh:
        point = list(position) if position else drawing.from_mm(sh.units, *DEFAULT_POSITION_MM)
        view = _base(tx, sh, point, scale, model_view)
        drawing.place(tx, view, point)
        if scale is not None:
            _verify_scale(tx, view, scale)
        return view_data(tx, view, target_sheet)


# ---- edit and inspect --------------------------------------------------------------------


class ViewStyle(TypedDict, total=False):
    hidden_lines: bool
    self_hidden: bool
    hidden_font: Annotated[int, Range(ge=0, le=7)]
    visible_font: Annotated[int, Range(ge=1, le=7)]
    smooth_edges: bool
    rendering: Literal["fully_shaded", "partially_shaded", "wireframe"]
    construction_geometry: bool


forbid_extra(ViewStyle)


class _EditExtras(TypedDict, total=False):
    rescaled: Annotated[list[result.Row], Doc("Projected views that followed the scale")]


class ViewInfo(ViewData, _EditExtras):
    style: dict
    construction_geometry: bool | None


def view_info(tx, view, sheet):
    out = {**view_data(tx, view, sheet), "style": view_style.read_style(tx, view)}
    out["construction_geometry"] = view_style.construction_state(tx, view)
    if not out["style"]["hidden_lines"] or not out["style"]["self_hidden"]:
        tx.warn(
            "NX_UPDATE_WARNING",
            "Hidden-line processing is off: occluded edges may be drawn as visible",
        )
    if attribute(tx, view, CONSTRUCTION) == "partial":
        tx.warn("NX_UPDATE_WARNING", view_style.CONSTRUCTION_WARNING)
    return out


@tool(
    effect="model",
    label="Edit drawing view",
    summary="Edit view {view}",
    keywords=("move view", "view scale", "hidden lines", "view style", "drafting view settings"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    invalidates="deleted",
    example={"view": "v:7", "position": [150, 120], "scale": 2},
)
def nx_edit_drawing_view(
    tx: Tx,
    *,
    view: DrawingViewRef,
    position: SheetPoint | None = None,
    scale: ScaleArg | None = None,
    style: ViewStyle | None = None,  # noqa: A002 - surface name
) -> ViewInfo:
    """Move a drawing view, change its scale or its display style (fonts 0-7, 0 invisible).
    Projected views made from it follow a scale change, keeping their gap (rescaled). Every
    change is read back."""
    changes = {k: v for k, v in (style or {}).items() if v is not None}
    ratio = scale_value(scale)
    if position is None and ratio is None and not changes:
        fail("NX_INVALID_ARGUMENT", "Give position, scale or style")
    with tx.checks():
        obj, sheet = drawing.view(tx, view)
        before = drawing.position(obj)
    construction = changes.pop("construction_geometry", None)
    with drawing.on_sheet(tx, sheet):
        others = [v for v in sheet.GetDraftingViews() if int(v.Tag) != int(obj.Tag)]
        styles = {int(v.Tag): view_style.read_style(tx, v) for v in others} if changes else {}
        followers = _followers(tx, obj, sheet) if ratio is not None else []
        if ratio is not None:
            tx.uf.Draw.SetViewScale(obj.Tag, ratio)
            for child, *_ in followers:
                tx.uf.Draw.SetViewScale(child.Tag, ratio)
        if position is not None:
            tx.uf.Draw.MoveView(obj.Tag, [float(position[0]), float(position[1])])
        if changes:
            _apply_style(tx, obj, changes)
        if construction is not None:
            view_style.set_construction(tx, obj, construction, strict=True)
        tx.part.DraftingViews.UpdateViews([obj] + [f[0] for f in followers])
        rescaled = _keep_gaps(tx, followers, ratio)
        refresh_details(tx)
        out = view_info(tx, obj, sheet)
        _side_effects(tx, others, styles)
        if rescaled:
            out["rescaled"] = rescaled
    if ratio is not None:
        readback.expect_close(out["scale"], ratio, "scale", 1e-8)
    if position is not None:
        tol = units.tolerance(units.sheet_units(tx, sheet), 1e-4)
        kept = [
            abs(out["position"][i] - before[i]) <= tol < abs(position[i] - before[i])
            for i in (0, 1)
        ]
        if any(kept) and not all(kept):
            # An aligned (projected or section) view moves only along its free axis.
            fail(
                "NX_UNSUPPORTED_OPTION",
                "The view is aligned to its parent along this axis: move only along the other "
                "axis, or move the parent",
                field="/position",
                details={"requested": list(position), "actual": out["position"]},
            )
        readback.expect_close(out["position"], list(position), "position", tol)
    if position is not None or ratio is not None:
        warn_overlaps(tx, out)
    return out


def _followers(tx, view, sheet):
    """Projected views made from ``view`` and, in turn, from those (breadth first), each
    ``(child, parent, free axis, side, gap, aligned offset per unit scale)`` read before a
    scale change. NX aligns the views' model geometry, not their reference points: the
    offset between the reference points along the aligned axis scales with the views."""
    out, queue = [], [view]
    views = list(sheet.GetDraftingViews())
    while queue:
        parent = queue.pop(0)
        handle = tx.uf.Tag.AskHandleFromTag(parent.Tag)
        for child in views:
            if attribute(tx, child, VIEW_PARENT) != handle or view_type(child) != "projected":
                continue
            if int(child.Tag) in {int(view.Tag), *(int(f[0].Tag) for f in out)}:
                continue
            a, b = drawing.borders(tx, parent), drawing.borders(tx, child)
            apart = [max(b[i] - a[i + 2], a[i] - b[i + 2]) for i in (0, 1)]
            k = 0 if apart[0] >= apart[1] else 1  # the free axis: the borders are apart along it
            sign = 1 if b[k] + b[k + 2] > a[k] + a[k + 2] else -1
            gap = b[k] - a[k + 2] if sign > 0 else a[k] - b[k + 2]
            offset = drawing.position(child)[1 - k] - drawing.position(parent)[1 - k]
            out.append((child, parent, k, sign, gap, offset / drawing.scale(tx, parent)))
            queue.append(child)
    return out


def _keep_gaps(tx, followers, ratio):
    """After a parent's scale change: each follower at the parent's scale, its gap to its
    parent kept along the free axis (NX keeps the alignment; a move across it would drag
    the parent) and its alignment checked; reported as modified."""
    rows = []
    for child, parent, k, sign, gap, offset in followers:
        a, b = drawing.borders(tx, parent), drawing.borders(tx, child)
        point = drawing.position(child)
        facing = a[k + 2] if sign > 0 else a[k]
        point[k] += facing + sign * gap - (b[k] if sign > 0 else b[k + 2])
        drawing.place(tx, child, point, axes=(k,))
        tol = units.tolerance(units.sheet_units(tx, drawing.view_sheet(tx, child)), 1e-4)
        _verify_scale(tx, child, ratio)
        aligned = drawing.position(child)[1 - k] - drawing.position(parent)[1 - k]
        readback.expect_close(aligned, offset * ratio, "alignment", 10 * tol)
        row = tx.modified(child, "drawing_view")
        rows.append(row)
        if not drawing.inside(drawing.borders(tx, child), drawing.view_sheet(tx, child)):
            tx.warn("NX_VIEW_OVERLAP", f"Projected view {row.get('id')} now runs off the "
                    "sheet: move it with nx_edit_drawing_view position")  # fmt: skip
    return rows


def _side_effects(tx, others, styles):
    """Warn about other views whose style NX changed along with this one's."""
    changed = {}
    for v in others:
        if int(v.Tag) in styles:
            now = view_style.read_style(tx, v)
            keys = sorted(k for k, value in styles[int(v.Tag)].items() if now[k] != value)
            if keys:
                changed[tx.ref(v, "drawing_view")["id"]] = keys
    if changed:
        tx.warn("NX_UPDATE_WARNING", f"NX also changed the style of other views: {changed}; "
                "set them back with nx_edit_drawing_view style")  # fmt: skip


def _apply_style(tx, view, changes):
    view_style.read_style(tx, view, changes)
    update(tx)
    tx.part.DraftingViews.UpdateViews([view])
    actual = view_style.read_style(tx, view)
    wrong = {k: actual[k] for k, v in changes.items() if actual[k] != v}
    readback.expect(not wrong, "style", "NX kept other view-style values", **wrong)


@tool(
    effect="read",
    label="Get drawing view",
    summary="View {view}",
    keywords=("view info", "view scale", "view border", "view position", "view style"),
    requires=("work_part",),
    cost="light",
    example={"view": "v:7"},
)
def nx_get_drawing_view(
    tx: Tx,
    *,
    view: DrawingViewRef,
) -> ViewInfo:
    """Read a drawing view: type, orientation, parent, position, scale and border. Also the
    views it overlaps, whether it lies on the sheet, its style and construction state. A
    projected view may read visible_font/hidden_font 0 (raw NX value) while NX draws it
    solid/dashed."""
    obj, sheet = drawing.view(tx, view)
    return view_info(tx, obj, sheet)
