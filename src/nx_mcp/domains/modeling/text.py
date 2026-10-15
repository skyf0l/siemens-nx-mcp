"""Text feature: outline curves of a string, optionally embossed or engraved.

NX 2406 lessons kept here (probed live): planar text sits in ``PlanarFrame``: a
coordinate system (origin = anchor point, X = baseline, Z = plane normal), an anchor
location and a height; ``UpdateOnCoordinateSystem`` sizes the frame length from the font,
and the outline curves fill the frame. ``SelectFont`` accepts any name; a missing font
fails only on commit (672061, or 672075 "zero text height"). Along a curve,
``OnCurvePlacementProfile`` is the path and ``FrameOnPath.AnchorPosition`` must be set
(path value, percent, ``Update``), else "Unable to find placement"; the natural
orientation needs a curved path (on a line it finds no placement, and a vector
orientation collapses the frame length to zero). ``GetEntities`` are the curves; one
curve-feature rule over the text feature feeds the relief extrude."""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Aliases,
    BodyRef,
    Doc,
    Items,
    Length,
    Plane,
    Point3,
    RefTo,
    Text,
    Vec3,
    forbid_extra,
)
from nx_mcp.domains.modeling import common
from nx_mcp.kit import (
    args,
    builders,
    expr,
    facts,
    geom,
    measure,
    readback,
    result,
    select,
    units,
)

PathRef = Annotated[str, RefTo("curve", "edge")]
Anchor = Literal[
    "bottom_left", "bottom_center", "bottom_right",
    "middle_left", "middle_center", "middle_right",
    "top_left", "top_center", "top_right",
]  # fmt: skip
Style = Literal["regular", "bold", "italic", "bold_italic"]
STYLES = {
    "regular": "Regular",
    "bold": "Bold",
    "italic": "Italic",
    "bold_italic": "BoldItalic",
}
# NX 2406 on commit with a font it cannot find: "Unable to find specified font in the
# system.", or "Zero or negative text height is not allowed." (the height is checked > 0
# first, so that one means NX could not size the glyphs of the font).
FONT_MISSING = (672061, 672075)
PATH_PERCENT = {"left": "0", "center": "50", "right": "100"}
PATH_HINT = (
    "Text along a path needs a curved path (arc, circle, spline) longer than the text; "
    "shorten the text or lower height. For a straight edge use planar text with "
    "x_direction along it"
)
PATH_ANCHORS = {"left": "Left", "center": "Center", "right": "Right"}


class Relief(TypedDict):
    kind: Annotated[Literal["emboss", "engrave"], Doc("emboss: raise and unite; engrave: cut")]
    depth: Annotated[Length, Doc("Along the plane normal (emboss) or against it (engrave)")]
    target: BodyRef


forbid_extra(Relief)


class TextData(TypedDict, total=False):
    feature: result.Row
    curves: int  # outline curves made
    length: float  # their total length
    min: list[float]  # bounds of the curves
    max: list[float]
    relief: result.Row  # the extrude feature (relief only)
    bodies: list[result.Row]
    volume: float


def _anchor_name(anchor: str) -> str:
    vertical, horizontal = anchor.split("_")
    return vertical.capitalize() + horizontal.capitalize()


def _x_axis(normal, x_direction, field="/x_direction"):
    """The baseline direction: ``x_direction`` projected into the plane, else world X
    (world Y on a plane facing X)."""
    if x_direction is not None:
        want = geom.unit(x_direction, field)
    else:
        want = [0.0, 1.0, 0.0] if abs(normal[0]) > 0.9 else [1.0, 0.0, 0.0]
    x = geom.sub(want, geom.scale(normal, geom.dot(want, normal)))
    if geom.norm(x) < 1e-9:
        fail("NX_INVALID_ARGUMENT", "x_direction is normal to the plane", field=field,
             hint="Give a direction lying in the text plane")  # fmt: skip
    return geom.unit(x, field)


def _font(tx, b, font: str, style: str) -> None:
    b.SelectFont(font, tx.nx.Features.TextBuilder.ScriptOptions.Western)
    b.FontStyle = getattr(tx.nx.Features.TextBuilder.FontStyleOptions, STYLES[style])


def _frame(tx, plane, origin, x_direction):
    """``(origin, x, y, normal)`` of planar text: the anchor point on the plane, the
    baseline and up directions, and the plane normal."""
    _, at, normal = select.plane(tx, common.principal_plane(plane), "/plane")
    if origin is not None:
        p = [float(v) for v in origin]
        at = geom.sub(p, geom.scale(normal, geom.dot(geom.sub(p, at), normal)))
    x = _x_axis(normal, x_direction)
    return at, x, geom.cross(normal, x), normal


def _planar(tx, b, frame_axes, height, anchor):
    at, x, y, _ = frame_axes
    update = tx.nx.SmartObject.UpdateOption.WithinModeling
    b.Type = tx.nx.Features.TextBuilder.Types.Planar
    frame = b.PlanarFrame
    frame.AnchorLocation = getattr(
        tx.nx.GeometricUtilities.RectangularFrameBuilder.AnchorLocationType,
        _anchor_name(anchor),
    )
    expr.drive(frame.Height, height)
    xform = tx.part.Xforms.CreateXform(
        tx.nx.Point3d(*at), tx.nx.Vector3d(*x), tx.nx.Vector3d(*y), update, 1.0
    )
    frame.CoordinateSystem = tx.part.CoordinateSystems.CreateCoordinateSystem(xform, update)
    frame.UpdateOnCoordinateSystem()


def _straight(tx, obj) -> bool:
    if facts.is_edge(obj):
        return obj.SolidEdgeType == tx.nx.Edge.EdgeType.Linear
    return type(obj).__name__ == "Line"


def _on_path(tx, b, curves, height, anchor):
    """Text along curves: natural orientation (the curve's own normal); the anchor sits at
    0, 50 or 100 percent of the first curve for left, center or right."""
    b.Type = tx.nx.Features.TextBuilder.Types.OnCurve
    builders.curve_section(tx, curves, into=b.OnCurvePlacementProfile)
    frame = b.FrameOnPath
    side = anchor.split("_")[1]
    frame.AnchorLocation = getattr(
        tx.nx.GeometricUtilities.FrameOnPathBuilder.AnchorLocationType,
        PATH_ANCHORS[side],
    )
    where = frame.AnchorPosition
    where.Path.Value = curves[0]
    where.IsPercentUsed = True
    where.Expression.RightHandSide = PATH_PERCENT[side]
    where.Update(tx.nx.GeometricUtilities.OnPathDimensionBuilder.UpdateReason.Path)
    expr.drive(frame.Height, height)


def _relief(tx, feature, relief, target, origin, normal):
    """Extrude the text curves by the relief depth and unite or subtract with the target."""
    before = measure.volume(tx, target)
    combine = {
        "operation": "unite" if relief["kind"] == "emboss" else "subtract",
        "targets": [relief["target"]],
    }
    tags = common.body_tags(tx)
    part = tx.part
    depth = expr.rhs(relief["depth"])
    types = tx.nx.GeometricUtilities.Extend.ExtendType
    with builders.build(tx, part.Features.CreateExtrudeBuilder, None) as b:
        section = part.Sections.CreateSection(
            units.part_tolerance(tx), units.part_tolerance(tx), 0.5
        )
        options = part.ScRuleFactory.CreateRuleOptions()
        try:
            rule = part.ScRuleFactory.CreateRuleCurveFeature([feature], None, options)
        finally:
            options.Dispose()
        section.AddToSection(
            [rule],
            None,
            None,
            None,
            tx.nx.Point3d(*origin),
            tx.nx.Section.Mode.Create,
            False,
        )
        b.Section = section
        sense = normal if relief["kind"] == "emboss" else geom.scale(normal, -1.0)
        b.Direction = builders.direction(tx, sense, origin, "/relief/depth")
        b.Limits.StartExtend.TrimType = types.Value
        b.Limits.StartExtend.Value.RightHandSide = "0"
        b.Limits.EndExtend.TrimType = types.Value
        b.Limits.EndExtend.Value.RightHandSide = depth
        builders.boolean(tx, b.BooleanOperation, combine, "/relief")
        extrude = b.commit()
    out = common.combined(tx, extrude, combine, [target], before, tags)
    return out


@tool(
    effect="model",
    label="Create text",
    summary=lambda a, say: (
        f"Text '{a['text'][:20]}'" + (f" {a['relief']['kind']}" if a.get("relief") else "")
    ),
    keywords=(
        "text",
        "lettering",
        "engrave text",
        "emboss text",
        "label",
        "font",
        "marking",
    ),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"text": "NX-01", "plane": "xy", "origin": [0, 0, 0], "height": 5},
)
def nx_create_text(
    tx: Tx,
    *,
    text: Annotated[str, Text(200)],
    plane: Annotated[Plane, Doc("Planar text: plane or planar face; omit with path")] | None = None,
    origin: Annotated[Point3, Doc("Planar: anchor point, projected onto the plane")] | None = None,
    x_direction: Annotated[Vec3, Doc("Planar: baseline direction; default world X (Y if facing X)")]
    | None = None,
    path: Annotated[
        list[PathRef],
        Items(1, 50),
        Doc("Text along these curved curves/edges (not a line); omit plane"),
    ]
    | None = None,
    height: Annotated[Length, Doc("Character frame height")] = 10.0,
    font: Annotated[str, Text(64), Doc("Installed TrueType font name")] = "Arial",
    style: Style = "regular",
    anchor: Annotated[
        Anchor,
        Aliases(left="bottom_left", center="bottom_center", right="bottom_right"),
        Doc("Frame point at origin; on a path only the side counts: left, center or right"),
    ] = "bottom_left",
    relief: Relief | None = None,
) -> TextData:
    """Create text outline curves in a font and height on a plane or planar face, or along
    curves. relief embosses (unite) or engraves (subtract) planar text into a target body.
    Up is plane normal x baseline. Returns the curve count, length and bounds."""
    args.exactly_one(plane=plane, path=path)
    if not text.strip():
        fail("NX_INVALID_ARGUMENT", "text is empty", field="/text")
    if float(height) <= 0:
        fail("NX_INVALID_ARGUMENT", "height must be positive", field="/height")
    if path is not None and (origin is not None or x_direction is not None):
        fail("NX_INVALID_ARGUMENT", "origin and x_direction are for planar text",
             field="/origin" if origin is not None else "/x_direction",
             hint="Text along a path starts where the anchor sits on the path")  # fmt: skip
    if relief is not None:
        if path is not None:
            fail("NX_INVALID_ARGUMENT", "relief needs planar text", field="/relief",
                 hint="Place the text on a plane or planar face to emboss or engrave it")  # fmt: skip
        if float(relief["depth"]) <= 0:
            fail("NX_INVALID_ARGUMENT", "depth must be positive", field="/relief/depth")
    with tx.checks():
        if plane is not None:
            axes = _frame(tx, plane, origin, x_direction)
        else:
            curves = [select.one(tx, ref, "curve", "edge", field=f"/path/{i}")
                      for i, ref in enumerate(path or [])]  # fmt: skip
            if all(_straight(tx, c) for c in curves):
                fail("NX_WRONG_GEOMETRY", "The path is straight: NX cannot orient text "
                     "along a line", field="/path", hint=PATH_HINT)  # fmt: skip
        target = None if relief is None else common.solid(tx, relief["target"], "/relief/target")
    try:
        with builders.build(tx, tx.part.Features.CreateTextBuilder, None) as b:
            _font(tx, b, font, style)
            b.TextString = text
            if plane is not None:
                _planar(tx, b, axes, height, anchor)
            else:
                _on_path(tx, b, curves, height, anchor)
            feature = b.commit()
    except NXError as error:
        if error.nx_code in FONT_MISSING:
            fail(
                "NX_INVALID_ARGUMENT",
                f"NX found no font {font!r} on this computer",
                field="/font",
                hint="Use an installed TrueType font name such as 'Arial', 'Calibri' or "
                "'Times New Roman' (the name, not the file)",
                nx_code=error.nx_code,
            )
        if path is not None and error.code == "NX_BUILD_FAILED" and not error.hint:
            error.hint = PATH_HINT
        raise
    made = list(feature.GetEntities())
    readback.expect(bool(made), "curves", "NX made no text curves")
    low, high = measure.bounds(tx, made)
    out: TextData = {
        "feature": tx.ref(feature, kind="feature"),
        "curves": len(made),
        "length": measure.length(tx, made),
        "min": low,
        "max": high,
    }
    if relief is not None and target is not None:
        relieved = _relief(tx, feature, relief, target, axes[0], axes[3])
        out["relief"] = relieved["feature"]
        out["bodies"] = relieved["bodies"]
        out["volume"] = relieved["volume"]
    return out
