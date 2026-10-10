"""Drawing views: base, projected, section, detail and flat-pattern views; edit; inspect.

NX 2406 findings encoded here:

- Base-view builders commit null views unless the sheet is current
  (``kit.drawing.on_sheet`` opens it with the UF fallback).
- Builders and UF section views place a view at their own aligned spot; every view
  is moved to the requested point with ``Draw.MoveView`` and read back.
- A projected view defaults to 1:1 on 2406: ``ViewStyleProjected.Scale = True``
  makes it inherit the parent's scale. A projected center off the sheet fails in NX
  after the commit ("The view is not located on the drawing"): refused before NX.
- Detail-view boundaries are recorded (parent handle, model center, radius) and
  re-mapped after parent edits (``nx_update_annotations`` and view edits).
"""

from __future__ import annotations

import json
import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import Doc, Items, Point3, Range, RefTo, SheetPoint
from nx_mcp.domains.drawing import style as view_style
from nx_mcp.domains.drawing import view_kinds as kinds
from nx_mcp.domains.drawing.common import (
    CONSTRUCTION,
    DETAIL,
    attribute,
    update,
)
from nx_mcp.domains.drawing.view_kinds import EdgePoint
from nx_mcp.kit import args, builders, drawing, geom, parts, readback, result, select, units

ORIENTATIONS = {
    "top": "Top",
    "front": "Front",
    "back": "Back",
    "right": "Right",
    "left": "Left",
    "bottom": "Bottom",
    "isometric": "Isometric",
}
OFFSETS = {"right": (1, 0), "left": (-1, 0), "top": (0, 1), "bottom": (0, -1)}
DEFAULT_POSITION_MM = (100.0, 100.0)
DEFAULT_SPACING_MM = 60.0
DEFAULT_DETAIL_SCALE = 2.0
NATIVE_TYPES = {
    "BaseView": "base",
    "ProjectedView": "projected",
    "SectionView": "section",
    "DetailView": "detail",
}

Vec2 = Annotated[tuple[float, float], Doc("Sheet [x, y] direction")]
Scale = Annotated[float, Range(gt=0, le=1000), Doc("e.g. 2 for 2:1")]
Band = tuple[float, float]


class _ViewExtras(TypedDict, total=False):
    cut_planes: list[dict]  # section views: [{normal, offset}] in part coordinates
    hinge: dict  # auxiliary: hinge edge model_length and view_length (part units)
    breaks: list[dict]  # broken: the bands removed (sheet units)
    break_out: dict  # break-out: depth read back, boundary radius


class ViewData(_ViewExtras):
    view: result.Row
    sheet: result.Row
    type: str
    position: list[float]
    scale: float
    bounds: list[float]
    inside_sheet: bool
    toward_viewer: list[float]


# ---- shared ----------------------------------------------------------------------------


def view_type(view):
    return NATIVE_TYPES.get(type(view).__name__, type(view).__name__.lower())


def view_data(tx, view, sheet):
    bounds = drawing.borders(tx, view)
    return {
        "view": tx.ref(view, "drawing_view"),
        "sheet": tx.ref(sheet, "drawing_sheet"),
        "type": view_type(view),
        "position": drawing.position(view),
        "scale": drawing.scale(tx, view),
        "bounds": bounds,
        "inside_sheet": drawing.inside(bounds, sheet),
        "toward_viewer": [float(v) for v in drawing.axes(view)[2]],  # view z axis, part coords
    }


def _configure(tx, b, sheet, scale):
    """Ratio scale (the sheet's unless given) and dashed, self-hidden hidden lines."""
    P = tx.nx.Preferences
    numerator, denominator = (scale, 1.0) if scale is not None else sheet.GetScale()
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
    if kind == "base":
        return f"Base view {a.get('orientation', '')} on {say('sheet')}"
    if kind == "projected":
        return f"Projected view {a.get('direction', '')} of {say('parent')}"
    if kind == "flat_pattern":
        return f"Flat-pattern view on {say('sheet')}"
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
    sheet: RefTo("drawing_sheet") | None = None,  # type: ignore[valid-type]
    parent: RefTo("drawing_view") | None = None,  # type: ignore[valid-type]
    orientation: Literal["top", "front", "back", "right", "left", "bottom", "isometric"]
    | None = None,
    body: RefTo("body") | None = None,  # type: ignore[valid-type]
    explosion: RefTo("explosion") | None = None,  # type: ignore[valid-type]
    direction: Literal["right", "left", "top", "bottom"] | None = None,
    spacing: Annotated[float, Range(gt=0, le=10000), Doc("Sheet units")] | None = None,
    hinge: RefTo("edge") | None = None,  # type: ignore[valid-type]
    cut: RefTo("edge") | None = None,  # type: ignore[valid-type]
    cut_point: Literal["start", "end", "center"] = "start",
    bend: EdgePoint | None = None,
    rotation: EdgePoint | None = None,
    cuts: Annotated[list[EdgePoint], Items(2, 49)] | None = None,
    step_direction: Vec2 | None = None,
    arrow_direction: Vec2 | None = None,
    axis: Literal["x", "y"] = "x",
    breaks: Annotated[list[Band], Items(1, 4), Doc("Sheet [start, end]")] | None = None,
    gap: Annotated[float, Range(gt=0, le=1000), Doc("Sheet units")] | None = None,
    center: Point3 | None = None,
    radius: Annotated[float, Range(gt=0, le=1e6), Doc("Part units")] | None = None,
    depth: Annotated[float, Range(gt=0, le=1e6), Doc("Below center, part units")] | None = None,
    flat_pattern: RefTo("feature") | None = None,  # type: ignore[valid-type]
    position: SheetPoint | None = None,
    scale: Scale | None = None,
) -> ViewData:
    """Place a drawing view at a verified sheet point; broken and break_out cut the parent.
    Derived views keep the parent scale."""
    if type == "base":
        args.need(sheet=sheet, orientation=orientation)
        return _create_base(tx, sheet, orientation, body, explosion, position, scale)
    if type == "projected":
        args.need(parent=parent, direction=direction)
        return _create_projected(tx, parent, direction, spacing)
    if type == "auxiliary":
        args.need(parent=parent, hinge=hinge, position=position)
        return _create_auxiliary(tx, parent, hinge, position)
    if type == "broken":
        args.need(parent=parent, breaks=breaks)
        return _create_broken(tx, parent, axis, breaks, gap)
    if type == "break_out":
        args.need(parent=parent, center=center, radius=radius, depth=depth)
        return _create_break_out(tx, parent, center, radius, depth)
    if type.endswith("section"):
        kind = type.removesuffix("_section").removesuffix("section") or "simple"
        needed: dict[str, object] = {"cut": cut} if kind in ("simple", "half") else {}
        if kind == "half":
            needed["bend"] = bend
        elif kind != "simple":
            needed.update(cuts=cuts, **({"rotation": rotation} if kind == "revolved" else {}))
        args.need(parent=parent, step_direction=step_direction,
                  arrow_direction=arrow_direction, position=position, **needed)  # fmt: skip
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
    if type == "detail":
        args.need(parent=parent, center=center, radius=radius, position=position)
        return _create_detail(tx, parent, center, radius, position, scale)
    args.need(sheet=sheet, flat_pattern=flat_pattern)
    return _create_flat(tx, sheet, flat_pattern, position, scale)


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


def _create_base(tx, sheet_ref, orientation, body_ref, explosion_ref, position, scale):
    with tx.checks():
        target_sheet = select.resolve(
            tx, sheet_ref, "drawing_sheet", field="/sheet", work_part=True
        )
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


def _create_projected(tx, parent_ref, direction, spacing):
    dx, dy = OFFSETS[direction]
    with tx.checks():
        parent, sheet = drawing.view(tx, parent_ref, "/parent")
        sheet_units = units.sheet_units(tx, sheet)
        gap = (
            spacing if spacing is not None else drawing.from_mm(sheet_units, DEFAULT_SPACING_MM)[0]
        )
        center = drawing.position(parent)
        target = [center[0] + dx * gap, center[1] + dy * gap]
        length, height = drawing.size(sheet)
        if not (0 <= target[0] <= length and 0 <= target[1] <= height):
            # NX fails after the commit with "The view is not located on the drawing".
            fail(
                "NX_INVALID_ARGUMENT",
                "The projected view would be off the sheet; reduce spacing or change direction",
                field="/spacing",
                details={"center": target, "sheet_size": [length, height]},
            )
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
        # Move only along the free axis: moving an aligned view across its alignment drags
        # the parent with it (NX keeps the associative alignment).
        now = drawing.position(view)
        free = [target[0], now[1]] if dx else [now[0], target[1]]
        drawing.place(tx, view, free, axes=(0,) if dx else (1,))
        tol = units.tolerance(sh.units, 1e-4)
        readback.expect_close(drawing.position(parent), center, "parent_position", tol)
        if exploded:
            _set_explosion(tx, view, exploded)
        _verify_scale(tx, view, parent_scale)
        return view_data(tx, view, sheet)


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
            b.Scale.Numerator = float(scale)
            b.Scale.Denominator = 1.0
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
        target_sheet = select.resolve(
            tx, sheet_ref, "drawing_sheet", field="/sheet", work_part=True
        )
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


ViewStyle.__pydantic_config__ = {"extra": "forbid"}  # type: ignore[attr-defined]


class ViewInfo(ViewData):
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
    view: RefTo("drawing_view"),  # type: ignore[valid-type]
    position: SheetPoint | None = None,
    scale: Scale | None = None,
    style: ViewStyle | None = None,  # noqa: A002 - surface name
) -> ViewInfo:
    """Move a drawing view, change its scale or its display style (fonts 0-7, 0 invisible).
    Every change is read back."""
    changes = {k: v for k, v in (style or {}).items() if v is not None}
    if position is None and scale is None and not changes:
        fail("NX_INVALID_ARGUMENT", "Give position, scale or style")
    with tx.checks():
        obj, sheet = drawing.view(tx, view)
        before = drawing.position(obj)
    construction = changes.pop("construction_geometry", None)
    with drawing.on_sheet(tx, sheet):
        if scale is not None:
            tx.uf.Draw.SetViewScale(obj.Tag, float(scale))
        if position is not None:
            tx.uf.Draw.MoveView(obj.Tag, [float(position[0]), float(position[1])])
        if changes:
            _apply_style(tx, obj, changes)
        if construction is not None:
            view_style.set_construction(tx, obj, construction, strict=True)
        tx.part.DraftingViews.UpdateViews([obj])
        refresh_details(tx)
        out = view_info(tx, obj, sheet)
    if scale is not None:
        readback.expect_close(out["scale"], scale, "scale", 1e-8)
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
    return out


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
    view: RefTo("drawing_view"),  # type: ignore[valid-type]
) -> ViewInfo:
    """Read a drawing view: type, sheet position, actual scale and border. Also whether it
    lies on the sheet, its display style and construction-geometry state."""
    obj, sheet = drawing.view(tx, view)
    return view_info(tx, obj, sheet)
