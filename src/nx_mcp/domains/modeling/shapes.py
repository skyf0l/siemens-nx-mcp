"""Rib, tube, helix and emboss body (tool-surface 4).

NX 2406 lessons kept here:

- The rib builder lives on ``Features.DesignFeatureCollection`` (not on the feature
  collection); its ``Section`` and ``TargetBody`` are the builder's own and are
  filled in place. NX's default rib type is perpendicular to the section plane;
  a draft applies only to that type. A perpendicular rib grows along the sketch
  normal; ``ReverseRibDirection`` grows it against the normal (a sketch on a cup's
  rim needs it, else "cannot properly cap the open profile", 4095004). ``RibThicknessOffsetOption`` Asymmetric puts
  the whole thickness on one side, ``ReverseThicknessDirection`` flips it.
  Without ``CombineRibWithTarget`` the feature owns both the target and the new
  rib body.
- The tube's ``PathSection`` is filled in place; ``OutputOption`` multiple segments
  keeps cylinders and tori, so a tube along a smooth curve has the exact volume
  pi (R^2 - r^2) L.
- The helix is built along the Z axis of a specified coordinate system with
  constant radius and pitch laws and the turns length method; a height becomes
  turns = height / pitch.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, TypedDict

from nx_mcp.core.catalogue import tool
from nx_mcp.core.errors import NXError, fail
from nx_mcp.core.tx import Tx
from nx_mcp.core.types import (
    Angle,
    Axis,
    BodyRef,
    Combine,
    Doc,
    Items,
    Length,
    Point3,
    Range,
    RefTo,
    SketchRef,
)
from nx_mcp.domains.modeling import common
from nx_mcp.kit import args, builders, curve, geom, measure, readback, result, select, units
from nx_mcp.kit import sketch as ksketch

PathRef = Annotated[str, RefTo("sketch", "curve", "edge")]
# Relative tolerance of the closed-form read-backs (tube volume, helix length).
FORMULA_REL = 1e-3


def _new_bodies(feature, *known):
    tags = {int(b.Tag) for b in known}
    return [b for b in common.feature_bodies(feature) if int(b.Tag) not in tags]


# ---------------------------------------------------------------------------
# Rib


@tool(
    effect="model",
    label="Create rib",
    summary=lambda a, say: f"Rib {say('sketch')} into {say('body')} {say('thickness')}",
    keywords=("rib", "web", "gusset", "stiffener", "rib wall", "reinforcement"),
    requires=("work_part", "no_active_sketch"),
    variants="orientation",
    cost="heavy",
    previewable=True,
    example={"sketch": "s:5", "body": "b:3", "thickness": 2},
)
def nx_create_rib(
    tx: Tx,
    *,
    sketch: SketchRef,
    body: BodyRef,
    thickness: Length,
    orientation: Annotated[
        Literal["perpendicular", "parallel"],
        Doc("Rib wall perpendicular to the sketch plane (grows to the body) or in it"),
    ] = "perpendicular",
    side: Annotated[
        Literal["symmetric", "one_side", "other_side"],
        Doc("Centred or one side; a parallel rib's one_side is the sketch normal side"),
    ] = "symmetric",
    draft: Annotated[Angle, Doc("Draft of a perpendicular rib's walls")] | None = None,
    combine: Annotated[bool, Doc("Unite the rib with the body; false keeps a new body")] = True,
    flip: Annotated[
        bool, Doc("Perpendicular rib: grow against the sketch normal (NX grows along it)")
    ] = False,
) -> common.FeatureData:
    """Add a rib wall of a thickness from open sketch curves into a solid body.
    The curves extend to the body. Volume is the body's after the rib, or the new rib
    body's when combine is false."""
    t = args.finite(thickness, "/thickness", positive=True)
    if draft is not None:
        if orientation != "perpendicular":
            fail("NX_INVALID_ARGUMENT", "Only a perpendicular rib takes a draft", field="/draft")
        if not 0 < float(draft) < 89:
            fail("NX_INVALID_ARGUMENT", "draft must be within 0..89 degrees", field="/draft")
    sk = ksketch.resolve(tx, sketch)
    target = common.solid(tx, body)
    before = measure.volume(tx, target)
    rib = tx.nx.Features.RibBuilder
    collection = tx.part.Features.DesignFeatureCollection
    with builders.build(tx, collection.CreateRibBuilder, None) as b:
        builders.section(tx, sk, into=b.Section)
        builders.fill(tx, b.TargetBody, [target], "Body")
        b.RibType = (
            rib.RibTypes.PerpendiculartoSectionPlane
            if orientation == "perpendicular"
            else rib.RibTypes.ParalleltoSectionPlane
        )
        b.RibThickness.RightHandSide = repr(t)
        offsets = rib.RibThicknessOffsetOptions
        b.RibThicknessOffsetOption = (
            offsets.Symmetric if side == "symmetric" else offsets.Asymmetric
        )
        b.ReverseThicknessDirection = side == "other_side"
        if draft is None:
            b.RibDraftOption = rib.RibDraftOptions.NotSet
        else:
            b.RibDraftOption = rib.RibDraftOptions.FromCap
            b.DraftAngle.RightHandSide = repr(float(draft))
        b.CombineRibWithTarget = bool(combine)
        b.ReverseRibDirection = bool(flip)
        feature = b.commit()
    if combine:
        out = common.result_of(tx, feature, [target])
        common.changed(before, out["volume"], "more")
        return out
    made = _new_bodies(feature, target)
    readback.expect(bool(made), "bodies", "The rib built no body")
    out = common.result_of(tx, feature, made)
    common.changed(0.0, out["volume"], "positive")
    common.changed(before, measure.volume(tx, target), "same", check="target_volume")
    return out


# ---------------------------------------------------------------------------
# Tube


def _path(tx, refs):
    """``(section filler, single curve or None)`` for a tube path: one sketch, or curves
    and edges."""
    args.distinct("/path", refs, "path references")
    sketches, curves = [], []
    for i, ref in enumerate(refs):
        field = f"/path/{i}"
        try:
            sketches.append((select.resolve(tx, ref, "sketch", field=field), field))
        except NXError as error:
            if error.code != "NX_WRONG_KIND":
                raise
            curves.append((select.resolve(tx, ref, "curve", "edge", field=field), field))
    if sketches and len(refs) > 1:
        fail("NX_INVALID_ARGUMENT", "A sketch path is the only path entry", field="/path")
    sketches = [ksketch.resolve(tx, sk, field) for sk, field in sketches]
    curves = [select.owned(tx, c, field, "curves") for c, field in curves]
    if sketches:
        sk = sketches[0]
        return (lambda into: builders.section(tx, sk, into=into)), None
    single = curves[0] if len(curves) == 1 else None
    return (lambda into: builders.curve_section(tx, curves, into=into)), single


@tool(
    effect="model",
    label="Create tube",
    summary=lambda a, say: f"Tube {say('outer_diameter')} along {say('path')}",
    keywords=("tube", "pipe", "wire", "hose", "cable", "tubing", "hollow pipe"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"path": ["s:4"], "outer_diameter": 10, "inner_diameter": 8},
)
def nx_create_tube(
    tx: Tx,
    *,
    path: Annotated[
        list[PathRef],
        Items(1, 100),
        Doc("One sketch, or connected curves and edges"),
    ],
    outer_diameter: Length,
    inner_diameter: Annotated[Length, Doc("0 for a solid rod")] = 0.0,
    combine: Combine | None = None,
) -> common.FeatureData:
    """Sweep a circular tube (or rod) along a path, as a new body or combined with bodies.
    A tube along one smooth curve has volume pi/4 (D^2 - d^2) L."""
    outer = args.finite(outer_diameter, "/outer_diameter", positive=True)
    inner = float(inner_diameter)
    if not 0 <= inner < outer:
        fail(
            "NX_INVALID_ARGUMENT",
            "inner_diameter must be 0 or more and below outer_diameter",
            field="/inner_diameter",
        )
    common.check_combine(combine)
    fill, single = _path(tx, path)
    targets = common.combine_targets(tx, combine)
    before = measure.volume(tx, targets) if targets else 0.0
    tags = common.body_tags(tx)
    with builders.build(tx, tx.part.Features.CreateTubeBuilder, None) as b:
        b.Tolerance = units.part_tolerance(tx)
        fill(b.PathSection)
        b.OuterDiameter.RightHandSide = repr(outer)
        b.InnerDiameter.RightHandSide = repr(inner)
        b.OutputOption = tx.nx.Features.TubeBuilder.Output.MultipleSegments
        builders.boolean(tx, b.BooleanOption, combine)
        feature = b.commit()
    if combine is not None:
        return common.combined(tx, feature, combine, targets, before, tags)
    out = common.result_of(tx, feature)
    common.changed(0.0, out["volume"], "positive")
    if single is not None:
        length = measure.length(tx, [single])
        want = math.pi / 4 * (outer**2 - inner**2) * length
        readback.expect(
            abs(out["volume"] - want) <= FORMULA_REL * want,
            "volume",
            "The tube volume is not pi/4 (D^2 - d^2) L",
            volume=out["volume"],
            expected=want,
        )
    return out


# ---------------------------------------------------------------------------
# Helix


class HelixData(TypedDict):
    feature: result.Row
    curves: list[result.Row]
    length: float
    turns: float
    start: Point3
    end: Point3


def _helix_csys(tx, origin, z, start_angle):
    """A coordinate system at ``origin`` whose Z is the axis and whose X points to the
    helix start: world X projected onto the axis plane (world Y for an axis near X),
    turned by ``start_angle`` about the axis."""
    helper = [1.0, 0.0, 0.0] if abs(z[0]) < 0.9 else [0.0, 1.0, 0.0]
    x0 = geom.unit(geom.sub(helper, geom.scale(z, geom.dot(helper, z))), "axis")
    y0 = geom.cross(z, x0)
    a = math.radians(start_angle)
    x = geom.add(geom.scale(x0, math.cos(a)), geom.scale(y0, math.sin(a)))
    y = geom.cross(z, x)
    part, nx = tx.part, tx.nx
    update = nx.SmartObject.UpdateOption.WithinModeling
    xform = part.Xforms.CreateXform(
        nx.Point3d(*(float(v) for v in origin)), nx.Vector3d(*x), nx.Vector3d(*y), update, 1.0
    )
    csys = part.CoordinateSystems.CreateCoordinateSystem(xform, update)
    return csys, x, y


def _ends(tx, curves, first):
    """The helix's start (the end of the curve chain nearest ``first``) and end."""
    a, _ = curve.ends(tx, curves[0])
    _, b = curve.ends(tx, curves[-1])
    return (a, b) if geom.dist(a, first) <= geom.dist(b, first) else (b, a)


def _constant_law(tx, law, value):
    law.LawType = tx.nx.GeometricUtilities.LawBuilder.Type.Constant
    law.Value.RightHandSide = repr(float(value))


@tool(
    effect="model",
    label="Create helix",
    summary=lambda a, say: f"Helix r {say('radius')} pitch {say('pitch')}",
    keywords=("helix", "spiral", "coil", "spring curve", "thread path", "helical curve"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"axis": {"principal": "z"}, "radius": 10, "pitch": 5, "turns": 4},
)
def nx_create_helix(
    tx: Tx,
    *,
    axis: Axis,
    radius: Length,
    pitch: Length,
    turns: Annotated[float, Range(gt=0, le=1000), Doc("Number of turns")] | None = None,
    height: Annotated[Length, Doc("Axial length instead of turns")] | None = None,
    hand: Literal["right", "left"] = "right",
    start_angle: Annotated[
        Angle, Doc("About the axis from world X (world Y for an axis near X)")
    ] = 0.0,
) -> HelixData:
    """Create a helix curve of constant radius and pitch climbing along an axis.
    It starts on the axis origin's plane; give turns or height (turns = height/pitch).
    Length is turns * sqrt((2 pi r)^2 + pitch^2)."""
    args.exactly_one(turns=turns, height=height)
    r = args.finite(radius, "/radius", positive=True)
    p = args.finite(pitch, "/pitch", positive=True)
    n = float(turns) if turns is not None else args.finite(height, "/height", positive=True) / p
    if not 0 < n <= 1000:
        fail("NX_INVALID_ARGUMENT", "The helix needs 0 to 1000 turns", field="/height")
    _obj, origin, z = select.axis(tx, axis, "/axis")
    helix = tx.nx.Features.HelixBuilder
    with builders.build(tx, tx.part.Features.CreateHelixBuilder, None) as b:
        csys, x, y = _helix_csys(tx, origin, z, float(start_angle))
        b.Type = helix.Types.AlongVector
        b.OrientationOption = helix.OrientationOptions.Specified
        b.CoordinateSystem = csys
        b.SizeOption = helix.SizeOptions.Radius
        _constant_law(tx, b.SizeLaw, r)
        _constant_law(tx, b.PitchLaw, p)
        b.LengthMethod = helix.LengthMethods.Turns
        b.Turns.RightHandSide = repr(n)
        b.StartAngle.RightHandSide = "0.0"
        b.TurnDirection = (
            helix.TurnDirections.RightHand if hand == "right" else helix.TurnDirections.LeftHand
        )
        b.DistanceTolerance = units.part_tolerance(tx)
        b.AngleTolerance = 0.5
        feature = b.commit()
    curves = [e for e in feature.GetEntities() if hasattr(e, "GetLength")]
    readback.expect(bool(curves), "curves", "The helix built no curve")
    length = measure.length(tx, curves)
    want = n * math.hypot(2 * math.pi * r, p)
    readback.expect(
        abs(length - want) <= FORMULA_REL * want,
        "length",
        "The helix length is not turns * sqrt((2 pi r)^2 + pitch^2)",
        length=length,
        expected=want,
    )
    start, end = _ends(tx, curves, geom.add(origin, geom.scale(x, r)))
    tol = max(units.part_tolerance(tx) * 10, 1e-6 * r)
    readback.expect_close(start, geom.add(origin, geom.scale(x, r)), "start", tol=tol)
    rise = geom.dot(geom.sub(end, origin), z)
    readback.expect_close([rise], [n * p], "height", tol=max(tol, FORMULA_REL * n * p))
    with curve.evaluator(tx, curves[0]) as e:
        point, tangent = e.closest(start)
    # Right hand: the tangent turns like z x radial while climbing along z.
    climb = 1.0 if geom.dot(tangent, z) >= 0 else -1.0
    turning = geom.dot(geom.cross(z, geom.sub(point, origin)), tangent) * climb
    readback.expect((turning > 0) == (hand == "right"), "hand", "NX built the other hand")
    return {
        "feature": tx.ref(feature, kind="feature"),
        "curves": [tx.ref(c, kind="curve") for c in curves],
        "length": length,
        "turns": n,
        "start": start,
        "end": end,
    }


# ---------------------------------------------------------------------------
# Emboss body


@tool(
    effect="model",
    label="Emboss body",
    summary="Emboss {target} around {tool_body}",
    keywords=("emboss", "emboss body", "form around", "wrap tool", "imprint body", "clearance"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"target": "b:3", "tool_body": "b:7", "clearance": 0.5},
)
def nx_create_emboss_body(
    tx: Tx,
    *,
    target: BodyRef,
    tool_body: BodyRef,
    clearance: Annotated[Length, Doc("Gap between the tool and the embossed wall")] = 0.0,
    thickness: Annotated[Length, Doc("Wall thickness of a sheet target's emboss")] | None = None,
    keep_tool: Annotated[bool, Doc("Keep the tool body after the emboss")] = True,
    reverse: Annotated[bool, Doc("Emboss toward the other side of the target")] = False,
) -> common.FeatureData:
    """Emboss a solid or sheet target so it follows a tool body with a clearance.
    Volume is the target's after the emboss (0 for a sheet target)."""
    gap = float(clearance)
    if gap < 0:
        fail("NX_INVALID_ARGUMENT", "clearance must be 0 or more", field="/clearance")
    if thickness is not None:
        args.finite(thickness, "/thickness", positive=True)
    if target == tool_body:
        fail("NX_INVALID_ARGUMENT", "target and tool_body must differ", field="/tool_body")
    goal = select.one(tx, target, "body", field="/target")
    tool_obj = select.one(tx, tool_body, "body", field="/tool_body")
    solid = bool(getattr(goal, "IsSolidBody", True))
    before = measure.volume(tx, goal) if solid else 0.0
    with builders.build(tx, tx.part.Features.CreateEmbossBodyBuilder, None) as b:
        b.TargetBody = builders.collector(tx, [goal], "Body")
        b.ToolBody = builders.collector(tx, [tool_obj], "Body")
        b.Clearance.RightHandSide = repr(gap)
        if thickness is not None:
            b.Thickness.RightHandSide = repr(float(thickness))
        b.KeepTools = bool(keep_tool)
        b.KeepTargets = False
        b.TargetMaterialSide = bool(reverse)
        feature = b.commit()
    bodies = [x for x in common.feature_bodies(feature) if int(x.Tag) != int(tool_obj.Tag)]
    out = common.result_of(tx, feature, bodies or [goal])
    if solid:
        common.changed(before, out["volume"], "differs")
    return out
