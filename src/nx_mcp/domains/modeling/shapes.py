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
    AxisByPoint,
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
from nx_mcp.kit import args, builders, curve, expr, geom, measure, readback, result, select, units
from nx_mcp.kit import bodies as kbodies
from nx_mcp.kit import sketch as ksketch

PathRef = Annotated[str, RefTo("sketch", "curve", "edge")]
# Relative tolerance of the closed-form read-backs (tube volume, helix length).
FORMULA_REL = 1e-3
# NX 2406: "The target cannot properly cap the open profile smart volume."
RIB_CANNOT_CAP = 4095004
RIB_HINTS = {
    "perpendicular": "A perpendicular rib grows the curves from the sketch plane along its "
    "normal until they meet the body, so that side must face material (flip grows the other "
    "way, e.g. a sketch on a cup's rim). A gusset or web drawn in its own plane (a line from "
    "wall to plate, the plane cutting the body) is orientation parallel",
    "parallel": "A parallel rib fills, in the sketch plane, the region the curves close with "
    "the body: the plane must cut through the body (e.g. an L-bracket's mid-plane, not a side "
    "face) and the curves must run from wall to wall",
}


def _rib_cause(tx, sk, target, orientation, side, flip, thickness):
    """``(cause, message, hint)`` for NX's rib refusal, or None. NX 2406 refuses every rib
    geometry problem with the same 4095004 (live: a parallel plane beside the body, a
    thickness wider than the body, a perpendicular rib growing away from the body or from
    a sketch 40 mm off it), so the cause is read from the body's box against the plane."""
    f = ksketch.frame(sk)
    low, high = measure.bounds(tx, [target])
    corners = [[(low, high)[(i >> k) & 1][k] for k in range(3)] for i in range(8)]
    heights = [ksketch.height(f, c) for c in corners]
    lo, hi = min(heights), max(heights)  # the body's span along the sketch normal
    tol = units.part_tolerance(tx)
    gap = lo if lo >= -tol else -hi  # plane to body, when the body is on one side
    if orientation == "parallel":
        if lo >= -tol or hi <= tol:
            return (
                "plane_misses_body",
                f"The sketch plane does not cut through the body (it lies {max(gap, 0):.6g} "
                "from it)",
                RIB_HINTS["parallel"],
            )
        room = {"symmetric": 2 * min(hi, -lo), "one_side": hi, "other_side": -lo}[side]
        if float(thickness) >= room - tol:
            return (
                "thickness_too_large",
                f"thickness {float(thickness):.6g} does not fit in the body: it has "
                f"{room:.6g} across the sketch plane on the rib's side",
                f"Use a rib thinner than {room:.6g}"
                + ("" if side == "symmetric" else ", or side symmetric to centre it in the body"),
            )
        return None
    behind, ahead = hi <= tol, lo >= -tol
    away = f" and lies {gap:.6g} from it" if gap > tol else ""
    if behind and not flip:
        return (
            "wrong_side",
            f"The body lies behind the sketch plane{away}, but a rib grows along the normal",
            "Set flip true to grow the rib against the sketch normal, toward the body",
        )
    if ahead and flip:
        return (
            "wrong_side",
            f"The body lies in front of the sketch plane{away}, but flip grows the rib away "
            "from it",
            "Omit flip: the rib grows along the sketch normal, toward the body",
        )
    if (behind or ahead) and gap > tol:
        return (
            "plane_off_body",
            f"The sketch plane lies {gap:.6g} from the body",
            "Put the sketch on the body (a rim or a face plane, or a plane cutting it)",
        )
    return None


def _rib_failed(tx, error, sk, target, orientation, side, flip, thickness) -> None:
    """Name the cause of a rib refusal (:func:`_rib_cause`) in the error, else give the
    orientation's hint."""
    if error.code != "NX_BUILD_FAILED":
        return
    try:
        found = _rib_cause(tx, sk, target, orientation, side, flip, thickness)
    except Exception:  # no box: keep NX's own message
        found = None
    if found is None:
        error.hint = error.hint or RIB_HINTS[orientation]
        return
    cause, message, hint = found
    error.message = f"{message} ({error.message})"
    error.hint = hint
    error.details = {**(error.details or {}), "cause": cause}


def _new_bodies(feature, *known):
    tags = {int(b.Tag) for b in known}
    return [b for b in common.feature_bodies(feature) if int(b.Tag) not in tags]


# ---------------------------------------------------------------------------
# Rib


class RibData(common.FeatureData):
    added: float


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
        Doc(
            "parallel: a gusset/web lying in the sketch plane; perpendicular: a wall grown along "
            "the sketch normal"
        ),
    ] = "perpendicular",
    side: Annotated[
        Literal["symmetric", "one_side", "other_side"],
        Doc("Centred or one side; a parallel rib's one_side is the sketch normal side"),
    ] = "symmetric",
    draft: Annotated[Angle, Doc("Draft of a perpendicular rib's walls")] | None = None,
    combine: Annotated[
        bool, Doc("Unite the rib with the body; false: a new body, the rib minus the body")
    ] = True,
    flip: Annotated[
        bool, Doc("Perpendicular rib: grow against the sketch normal (NX grows along it)")
    ] = False,
) -> RibData:
    """Add a rib (wall, web or gusset) from open sketch curves to a solid body. parallel: the
    sketch plane cuts through the body (an L-bracket's mid-plane) and a line from wall to wall
    closes a region with it, filled to the thickness across the plane. perpendicular: curves
    on a plane at the body (a cup's rim) grow along the sketch normal to meet it; flip when
    the body lies against the normal (below a rim sketch facing up). volume is the body's
    (the rib body's if not combined); added is the rib's."""
    args.finite(thickness, "/thickness", positive=True)
    if draft is not None:
        if orientation != "perpendicular":
            fail("NX_INVALID_ARGUMENT", "Only a perpendicular rib takes a draft", field="/draft")
        if not 0 < float(draft) < 89:
            fail("NX_INVALID_ARGUMENT", "draft must be within 0..89 degrees", field="/draft")
    with tx.checks():
        sk = ksketch.resolve(tx, select.resolve(tx, sketch, "sketch", field="/sketch"), "/sketch")
        target = common.solid(tx, body)
    before = measure.volume(tx, target)
    rib = tx.nx.Features.RibBuilder
    collection = tx.part.Features.DesignFeatureCollection
    try:
        with builders.build(tx, collection.CreateRibBuilder, None) as b:
            builders.section(tx, sk, into=b.Section)
            builders.fill(tx, b.TargetBody, [target], "Body")
            b.RibType = (
                rib.RibTypes.PerpendiculartoSectionPlane
                if orientation == "perpendicular"
                else rib.RibTypes.ParalleltoSectionPlane
            )
            expr.drive(b.RibThickness, thickness)
            offsets = rib.RibThicknessOffsetOptions
            b.RibThicknessOffsetOption = (
                offsets.Symmetric if side == "symmetric" else offsets.Asymmetric
            )
            b.ReverseThicknessDirection = side == "other_side"
            if draft is None:
                b.RibDraftOption = rib.RibDraftOptions.NotSet
            else:
                b.RibDraftOption = rib.RibDraftOptions.FromCap
                expr.drive(b.DraftAngle, draft)
            b.CombineRibWithTarget = bool(combine)
            b.ReverseRibDirection = bool(flip)
            feature = b.commit()
    except NXError as error:
        _rib_failed(tx, error, sk, target, orientation, side, flip, thickness)
        raise
    if combine:
        out = common.result_of(tx, feature, [target])
        common.changed(before, out["volume"], "more")
        return {**out, "added": out["volume"] - before}
    made = _new_bodies(feature, target)
    readback.expect(bool(made), "bodies", "The rib built no body")
    if len(made) > 1:
        tx.warn(
            "NX_NOTE",
            f"The rib body came out in {len(made)} pieces: without combine NX removes the "
            "body (and earlier ribs) from it",
        )
    out = common.result_of(tx, feature, made)
    common.changed(0.0, out["volume"], "positive")
    common.changed(before, measure.volume(tx, target), "same", check="target_volume")
    return {**out, "added": out["volume"]}


# ---------------------------------------------------------------------------
# Tube


# NX 2406 refuses a tube path that is not one chain: "Gaps in string or multiple loops."
TUBE_GAPS = 1070008
CHAIN_HINT = "Make the path curves meet end to end in one chain (add the missing curve)"


def _refuse_gaps(tx, curves, field) -> None:
    """Refuse a path whose curves do not join end to end in one chain (a gap, a branch,
    or a closed curve among others) before NX does (1070008, with no location)."""
    if len(curves) < 2:
        return
    ends = []  # (curve index, point)
    for i, c in enumerate(curves):
        try:
            a, b = curve.ends(tx, c)
        except Exception:  # a point or an object UF Eval does not take: NX decides
            return
        ends += [(i, a), (i, b)]
    tol = units.part_tolerance(tx)

    def meet(p, q):
        return geom.dist(p, q) <= max(tol, geom.tol(p, q))

    free = []
    for k, (i, p) in enumerate(ends):
        if meet(p, ends[k ^ 1][1]):  # a closed curve among others: a second loop
            fail("NX_WRONG_GEOMETRY", "The path holds a closed curve among others; a path is "
                 "one chain", field=field, hint=CHAIN_HINT)  # fmt: skip
        mates = [j for j, (i2, q) in enumerate(ends) if i2 != i and meet(p, q)]
        if len(mates) > 1:
            fail("NX_WRONG_GEOMETRY",
                 f"The path branches at {[round(v, 6) for v in p]}: {len(mates) + 1} curves meet",
                 field=field, hint=CHAIN_HINT)  # fmt: skip
        if not mates:
            free.append((i, p))
    if len(free) <= 2:
        return
    pairs = [
        (geom.dist(p, q), p, q) for n, (i, p) in enumerate(free) for j, q in free[n + 1 :] if i != j
    ]
    gap, p, q = min(pairs, key=lambda g: g[0])
    fail(
        "NX_WRONG_GEOMETRY",
        f"The path has a gap of {gap:.6g} between {[round(v, 6) for v in p]} and "
        f"{[round(v, 6) for v in q]}; its curves must join end to end",
        field=field,
        hint=CHAIN_HINT,
        details={"gap": gap, "free_ends": [[round(v, 6) for v in e] for _i, e in free][:10]},
    )


def _path(tx, refs):
    """``(section filler, single curve or None)`` for a tube path: one sketch, or curves
    and edges. A path with sharp corners is refused (NX cannot tube it)."""
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
        _refuse_gaps(tx, common.sketch_curves(sk), "/path")
        common.refuse_corners(tx, common.sketch_curves(sk), "/path", "path")
        return (lambda into: builders.section(tx, sk, into=into)), None
    if len(curves) > 1:
        _refuse_gaps(tx, curves, "/path")
        common.refuse_corners(tx, curves, "/path", "path")
    single = curves[0] if len(curves) == 1 else None
    return (lambda into: builders.curve_section(tx, curves, into=into)), single


@tool(
    effect="model",
    label="Create tube",
    summary=lambda a, say: f"Tube {say('outer_diameter')} along {say('path')}",
    keywords=("tube", "pipe", "wire", "hose", "cable", "tubing", "hollow pipe", "pipe routing"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"path": "s:4", "outer_diameter": 10, "inner_diameter": 8},
)
def nx_create_tube(
    tx: Tx,
    *,
    path: Annotated[
        Annotated[list[PathRef], Items(1, 100)] | PathRef,
        Doc("One sketch, or connected curves and edges"),
    ],
    outer_diameter: Length,
    inner_diameter: Annotated[Length, Doc("0 for a solid rod")] = 0.0,
    combine: Combine | None = None,
) -> common.FeatureData:
    """Sweep a circular tube (or rod) along a smooth (G1) path, as a new body or combined with
    bodies. A tube along one curve has volume pi/4 (D^2 - d^2) L."""
    outer = args.finite(outer_diameter, "/outer_diameter", positive=True)
    inner = float(inner_diameter)
    if not 0 <= inner < outer:
        fail(
            "NX_INVALID_ARGUMENT",
            "inner_diameter must be 0 or more and below outer_diameter",
            field="/inner_diameter",
        )
    common.check_combine(combine)
    with tx.checks():
        fill, single = _path(tx, [path] if isinstance(path, str) else path)
    targets = common.combine_targets(tx, combine)
    before = common.target_state(tx, targets)
    tags, known = common.body_tags(tx), kbodies.faulty(tx, targets)
    overlapping = kbodies.clashing(tx, targets)
    try:
        with builders.build(tx, tx.part.Features.CreateTubeBuilder, None) as b:
            b.Tolerance = units.part_tolerance(tx)
            fill(b.PathSection)
            expr.drive(b.OuterDiameter, outer_diameter)  # expressions keep driving
            expr.drive(b.InnerDiameter, inner_diameter)
            b.OutputOption = tx.nx.Features.TubeBuilder.Output.MultipleSegments
            builders.boolean(tx, b.BooleanOption, combine)
            feature = b.commit()
    except NXError as error:
        if error.nx_code == TUBE_GAPS:
            error.field, error.hint = error.field or "/path", CHAIN_HINT
        common.smooth_hint(error)
        common.outside_hint(error, combine)
        raise
    common.swept_sound(tx, common.feature_bodies(feature), known, overlapping)
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
    axis: AxisByPoint
    hand: Literal["right", "left"]


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


def _face_start(face, origin, z):
    """The point of a cylinder or cone face's axis at the face's end lowest along ``z``
    (NX gives a point at mid-height), so the helix climbs over the face."""
    heights = [
        geom.dot(geom.sub(geom.xyz(v), origin), z) for e in face.GetEdges() for v in e.GetVertices()
    ]
    return geom.add(origin, geom.scale(z, min(heights))) if heights else origin


def _constant_law(tx, law, value):
    law.LawType = tx.nx.GeometricUtilities.LawBuilder.Type.Constant
    expr.drive(law.Value, value)


@tool(
    effect="model",
    label="Create helix",
    summary=lambda a, say: f"Helix r {say('radius')} pitch {say('pitch')}",
    keywords=("helix", "spiral", "coil", "spring curve", "thread path", "helical curve"),
    requires=("work_part", "no_active_sketch"),
    cost="heavy",
    previewable=True,
    example={"axis": "z", "radius": 10, "pitch": 5, "turns": 4},
)
def nx_create_helix(
    tx: Tx,
    *,
    axis: Axis = "z",
    radius: Length,
    pitch: Length,
    turns: Annotated[float, Range(gt=0, le=1000), Doc("Number of turns")] | None = None,
    height: Annotated[Length, Doc("Axial length instead of turns")] | None = None,
    hand: Literal["right", "left"] = "right",
    start_angle: Annotated[
        Angle, Doc("About the axis from world X (world Y for an axis near X)")
    ] = 0.0,
) -> HelixData:
    """Create a helix curve of constant radius and pitch climbing along an axis (default z).
    It starts on the axis origin's plane (a cylinder or cone face: at the face's lower end
    along its axis); give turns or height, not both (turns = height/pitch). Length is
    turns * sqrt((2 pi r)^2 + pitch^2)."""
    if turns is not None and height is not None:
        fail(
            "NX_INVALID_ARGUMENT",
            "turns and height are mutually exclusive: give one (turns = height / pitch)",
            field="/height",
            hint="Drop height to keep turns, or drop turns to climb by height",
        )
    if turns is None and height is None:
        fail(
            "NX_INVALID_ARGUMENT",
            "Give turns or height (turns = height / pitch)",
            field="/turns",
        )
    r = args.finite(radius, "/radius", positive=True)
    p = args.finite(pitch, "/pitch", positive=True)
    n = float(turns) if turns is not None else args.finite(height, "/height", positive=True) / p
    if not 0 < n <= 1000:
        fail("NX_INVALID_ARGUMENT", "The helix needs 0 to 1000 turns", field="/height")
    with tx.checks():
        obj, origin, z = select.axis(tx, common.principal(axis), "/axis")
        if obj is not None and hasattr(obj, "GetEdges"):  # a face: start at its end
            origin = _face_start(obj, origin, z)
    helix = tx.nx.Features.HelixBuilder
    with builders.build(tx, tx.part.Features.CreateHelixBuilder, None) as b:
        csys, x, y = _helix_csys(tx, origin, z, float(start_angle))
        b.Type = helix.Types.AlongVector
        b.OrientationOption = helix.OrientationOptions.Specified
        b.CoordinateSystem = csys
        b.SizeOption = helix.SizeOptions.Radius
        _constant_law(tx, b.SizeLaw, radius)
        _constant_law(tx, b.PitchLaw, pitch)
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
        "axis": {"origin": origin, "direction": z},
        "hand": hand,
    }


# ---------------------------------------------------------------------------
# Emboss body


# NX 2406 "Unable to perform Boolean." of an emboss whose tool only touches the target.
EMBOSS_BOOLEAN = 670033
EMBOSS_HINT = (
    "The tool must overlap the target by a real volume: a tool that only touches it (a "
    "sphere's equator on a face) fails; move it into the target a little"
)


class EmbossBodyData(common.FeatureData, total=False):
    sheet: bool  # the target is a sheet body (volume is then 0)
    area_before: float  # sheet targets: the sheet's area before
    area: float  # sheet targets: the sheet's area after
    bounds: list[Point3]  # sheet targets: [low, high] of the sheet after


def _boxes_meet(tx, a, b, gap) -> bool:
    """Whether the two bodies' boxes, ``a``'s grown by ``gap``, overlap (True when NX
    cannot box them: NX decides)."""
    try:
        (la, ha), (lb, hb) = measure.bounds(tx, [a]), measure.bounds(tx, [b])
    except Exception:  # a body NX cannot box
        return True
    return all(la[i] - gap <= hb[i] and lb[i] <= ha[i] + gap for i in range(3))


@tool(
    effect="model",
    label="Emboss body",
    summary="Emboss {target} around {tool_body}",
    keywords=("emboss body", "form around", "wrap tool", "imprint body", "clearance pocket"),
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
    clearance: Annotated[Length, Doc("Gap kept around the tool on every side")] = 0.0,
    thickness: Annotated[Length, Doc("Sheet targets only: NX's emboss thickness; it stays a sheet")]
    | None = None,
    keep_tool: Annotated[bool, Doc("Keep the tool body after the emboss")] = True,
    reverse: Annotated[bool, Doc("Emboss toward the other side of the target")] = False,
) -> EmbossBodyData:
    """Emboss a target around an overlapping tool body (not text). A solid target loses the
    overlap grown by clearance; a sheet is pushed out around the tool (reverse: the other
    way). volume: a solid target's; a sheet target reports area, area_before and bounds."""
    gap = float(clearance)
    if gap < 0:
        fail("NX_INVALID_ARGUMENT", "clearance must be 0 or more", field="/clearance")
    if thickness is not None:
        args.finite(thickness, "/thickness", positive=True)
    if target == tool_body:
        fail("NX_INVALID_ARGUMENT", "target and tool_body must differ", field="/tool_body")
    with tx.checks():  # lookups and refusals: nothing changed yet (not_started)
        goal = select.one(tx, target, "body", field="/target")
        tool_obj = select.one(tx, tool_body, "body", field="/tool_body")
        if not _boxes_meet(tx, tool_obj, goal, gap):
            fail(
                "NX_WRONG_GEOMETRY",
                "tool_body does not reach the target: their bounding boxes do not overlap",
                field="/tool_body",
                hint="Move the tool so it overlaps the target (nx_create_transform), or pick "
                "the body it crosses",
            )
    solid = bool(getattr(goal, "IsSolidBody", True))
    before = measure.volume(tx, goal) if solid else 0.0
    faces_before = [] if solid else list(goal.GetFaces())
    area_before = measure.area(tx, faces_before) if faces_before else 0.0
    try:
        with builders.build(tx, tx.part.Features.CreateEmbossBodyBuilder, None) as b:
            b.TargetBody = builders.collector(tx, [goal], "Body")
            b.ToolBody = builders.collector(tx, [tool_obj], "Body")
            expr.drive(b.Clearance, clearance)
            if thickness is not None:
                expr.drive(b.Thickness, thickness)
            b.KeepTools = bool(keep_tool)
            b.KeepTargets = False
            b.TargetMaterialSide = bool(reverse)
            feature = b.commit()
    except NXError as error:
        if error.nx_code == EMBOSS_BOOLEAN and not error.hint:
            error.hint = EMBOSS_HINT
            error.field = error.field or "/tool_body"
        raise
    bodies = [x for x in common.feature_bodies(feature) if int(x.Tag) != int(tool_obj.Tag)]
    out: EmbossBodyData = {**common.result_of(tx, feature, bodies or [goal]), "sheet": not solid}
    if solid:
        common.changed(before, out["volume"], "differs")
        return out
    sheets = bodies or [goal]
    area = measure.area(tx, [f for x in sheets for f in x.GetFaces()])
    readback.expect(
        not common.same_volume(area_before, area),
        "area",
        f"The sheet kept its area ({area:.6g}): NX did not emboss it",
        area=area,
        before=area_before,
    )
    low, high = measure.bounds(tx, sheets)
    return {**out, "area_before": area_before, "area": area, "bounds": [low, high]}
